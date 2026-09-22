#!/usr/bin/env python3
"""
CarSwitch updater using Playwright/Chromium.

SAFE MODE:
- By default tests ONLY the first 3 listings.
- Set TEST_LIMIT=0 to process all listings.
- Never deletes a listing unless the page is explicitly unavailable.
- Temporary access failures keep the old record.
- Dataset is written atomically and only after the safety threshold passes.
"""

import asyncio
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

DATA = Path("data/carswitch_data.json")
TEST_LIMIT = int(os.getenv("TEST_LIMIT", "3"))
TIMEOUT = int(os.getenv("PAGE_TIMEOUT_MS", "60000"))
WAIT_MS = int(os.getenv("WAIT_AFTER_LOAD_MS", "5000"))

def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def first_match(patterns, text, flags=re.I):
    for p in patterns:
        m = re.search(p, text, flags)
        if m:
            return clean(m.group(1))
    return None

def parse_title(title):
    # Typical CarSwitch title pattern:
    # "Audi A3 for sale: SAR 89,250 - 44,000 KM, 2022 | CarSwitch"
    price = first_match([
        r"(?:SAR|ريال)\s*([\d,]+)",
        r"for sale:\s*([\d,]+)\s*(?:SAR|ريال)",
    ], title)
    mileage = first_match([
        r"[-–]\s*([\d,]+)\s*KM\b",
        r"([\d,]+)\s*KM\b",
        r"([\d,]+)\s*كم\b",
    ], title)
    year = first_match([r"\b((?:19|20)\d{2})\b"], title)

    return {
        "price": int(price.replace(",", "")) if price else None,
        "mileage": int(mileage.replace(",", "")) if mileage else None,
        "year": int(year) if year else None,
    }

def explicit_unavailable(text, title):
    hay = f"{title} {text}".lower()
    strong = [
        "page not found",
        "listing not found",
        "car not found",
        "vehicle not found",
        "listing is no longer available",
        "no longer available",
        "غير متوفر",
        "غير متاحة",
    ]
    return any(x in hay for x in strong)

async def inspect(page, old):
    url = old.get("url") or old.get("listing_url") or old.get("link")
    if not url:
        return old, "temporary", "missing URL"

    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=TIMEOUT,
        )
        await page.wait_for_timeout(WAIT_MS)

        status = response.status if response else None
        title = clean(await page.title())
        body = clean(await page.locator("body").inner_text(timeout=15000))

        if status == 404:
            return old, "gone", f"HTTP {status}"

        if status in (403, 429, 500, 502, 503, 504):
            return old, "temporary", f"HTTP {status}"

        if status != 200:
            return old, "temporary", f"HTTP {status}"

        if explicit_unavailable(body, title):
            return old, "gone", "explicit unavailable marker"

        parsed = parse_title(title)

        # Require the page to look like an actual CarSwitch listing.
        if "carswitch" not in title.lower() and "carswitch" not in body[:5000].lower():
            return old, "unparsed", "not recognized as CarSwitch listing"

        # A listing is considered verified when the page is reachable and
        # contains at least the year or a recognizable listing title.
        if not parsed["year"] and not title:
            return old, "unparsed", "no listing identity found"

        new = deepcopy(old)
        new["url"] = url
        new["source"] = "CarSwitch"
        new["last_checked"] = datetime.now(timezone.utc).isoformat()

        if parsed["year"] is not None:
            new["year"] = parsed["year"]
        if parsed["price"] is not None:
            new["price"] = parsed["price"]
        if parsed["mileage"] is not None:
            new["mileage"] = parsed["mileage"]

        # Keep existing values when a page omits a field.
        return new, "active", f"title={title!r}"

    except PlaywrightTimeoutError:
        return old, "temporary", "timeout"
    except Exception as e:
        return old, "temporary", f"{type(e).__name__}: {e}"

async def main():
    if not DATA.exists():
        raise SystemExit(f"Missing {DATA}")

    payload = json.loads(DATA.read_text(encoding="utf-8"))
    cars = payload.get("cars", []) if isinstance(payload, dict) else payload

    if not isinstance(cars, list) or not cars:
        raise SystemExit("No cars found; refusing to modify dataset.")

    selected = cars if TEST_LIMIT == 0 else cars[:TEST_LIMIT]

    print(f"Total records: {len(cars)}")
    print(f"Testing now: {len(selected)}")
    print("MODE: Playwright/Chromium SAFE TEST")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="ar-SA",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        results = []
        for i, old in enumerate(selected, 1):
            print(f"\n[{i}/{len(selected)}] {old.get('url')}")
            new, status, detail = await inspect(page, old)
            results.append((old, new, status, detail))
            print(f"STATUS: {status}")
            print(f"DETAIL: {detail}")

        await browser.close()

    active = sum(s == "active" for _, _, s, _ in results)
    gone = sum(s == "gone" for _, _, s, _ in results)
    temporary = sum(s == "temporary" for _, _, s, _ in results)
    unparsed = sum(s == "unparsed" for _, _, s, _ in results)

    print("\n=== TEST REPORT ===")
    print(f"Active: {active}")
    print(f"Gone: {gone}")
    print(f"Temporary: {temporary}")
    print(f"Unparsed: {unparsed}")

    # TEST MODE: do not write the dataset.
    if TEST_LIMIT != 0:
        print("\nTEST MODE: data/carswitch_data.json was NOT modified.")
        if active < len(results):
            raise SystemExit(2)
        print("TEST PASSED: all selected listings were verified.")
        return

    # Full mode safety gate.
    if active / len(results) < 0.50:
        raise SystemExit("SAFE MODE: verification rate below 50%; dataset preserved.")

    by_url = {old.get("url"): new for old, new, status, _ in results if status == "active"}
    gone_urls = {old.get("url") for old, _, status, _ in results if status == "gone"}

    output = []
    for car in cars:
        url = car.get("url")
        if url in gone_urls:
            continue
        output.append(by_url.get(url, car))

    payload["cars"] = output
    payload["count"] = len(output)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload["update_stats"] = {
        "checked": len(results),
        "verified_active": active,
        "confirmed_gone": gone,
        "temporary_failures": temporary,
        "unparsed": unparsed,
    }

    tmp = DATA.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DATA)

    print(f"SUCCESS: saved {len(output)} records.")

if __name__ == "__main__":
    asyncio.run(main())
