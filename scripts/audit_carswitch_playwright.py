# This audit wrapper imports the existing Playwright updater logic,
# but keeps the dataset read-only and writes a report only.
import asyncio
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

DATA = Path("data/carswitch_data.json")
LIMIT = int(os.getenv("TEST_LIMIT", "207"))
TIMEOUT = int(os.getenv("PAGE_TIMEOUT_MS", "60000"))
WAIT_MS = int(os.getenv("WAIT_AFTER_LOAD_MS", "4000"))

def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def match(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1)
    return None

def parse_title(title):
    price = match([r"(?:SAR|ريال)\s*([\d,]+)",
                   r"for sale:\s*([\d,]+)\s*(?:SAR|ريال)"], title)
    mileage = match([r"[-–]\s*([\d,]+)\s*KM\b",
                     r"([\d,]+)\s*KM\b",
                     r"([\d,]+)\s*كم\b"], title)
    year = match([r"\b((?:19|20)\d{2})\b"], title)
    return {
        "price": int(price.replace(",", "")) if price else None,
        "mileage": int(mileage.replace(",", "")) if mileage else None,
        "year": int(year) if year else None,
    }

def explicit_unavailable(text):
    t = text.lower()
    markers = [
        "page not found", "listing not found", "car not found",
        "vehicle not found", "listing is no longer available",
        "no longer available", "غير متوفر", "غير متاحة"
    ]
    return any(x in t for x in markers)

async def inspect(page, car):
    url = car.get("url") or car.get("listing_url") or car.get("link")
    if not url:
        return {"status": "unparsed", "reason": "missing URL"}

    try:
        response = await page.goto(url, wait_until="domcontentloaded",
                                   timeout=TIMEOUT)
        await page.wait_for_timeout(WAIT_MS)
        status = response.status if response else None
        title = clean(await page.title())
        body = clean(await page.locator("body").inner_text(timeout=15000))
        parsed = parse_title(title)

        if status == 404:
            return {"status": "gone", "reason": "HTTP 404", "title": title}
        if status in (403, 429, 500, 502, 503, 504):
            return {"status": "temporary", "reason": f"HTTP {status}", "title": title}
        if status != 200:
            return {"status": "temporary", "reason": f"HTTP {status}", "title": title}

        if explicit_unavailable(f"{title} {body}"):
            return {"status": "gone", "reason": "explicit unavailable marker",
                    "title": title}

        if "carswitch" not in title.lower() and "carswitch" not in body[:5000].lower():
            return {"status": "unparsed", "reason": "not recognized as CarSwitch",
                    "title": title}

        if not parsed["year"] and not title:
            return {"status": "unparsed", "reason": "no listing identity",
                    "title": title}

        return {
            "status": "active",
            "title": title,
            "year": parsed["year"],
            "price": parsed["price"],
            "mileage": parsed["mileage"],
        }

    except PlaywrightTimeoutError:
        return {"status": "temporary", "reason": "timeout"}
    except Exception as e:
        return {"status": "temporary",
                "reason": f"{type(e).__name__}: {e}"}

async def main():
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    cars = payload.get("cars", payload) if isinstance(payload, dict) else payload
    selected = cars[:LIMIT]

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="ar-SA",
            viewport={"width": 1440, "height": 1000},
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
        )
        page = await context.new_page()

        for i, car in enumerate(selected, 1):
            print(f"[{i}/{len(selected)}] {car.get('url')}")
            result = await inspect(page, car)
            result["url"] = car.get("url")
            result["old_price"] = car.get("price")
            result["old_mileage"] = car.get("mileage")
            results.append(result)
            print(result)

        await browser.close()

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_dataset": len(cars),
        "checked": len(results),
        "counts": counts,
        "results": results,
        "dataset_modified": False,
    }

    Path("car-switch-audit-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "CarSwitch full audit",
        f"Checked: {len(results)} / {len(cars)}",
        f"Counts: {counts}",
        "Dataset modified: NO",
        ""
    ]
    for r in results:
        lines.append(
            f'{r.get("status"):10} | {r.get("url")} | '
            f'price={r.get("price")} mileage={r.get("mileage")} '
            f'year={r.get("year")} reason={r.get("reason","")}'
        )
    Path("car-switch-audit-report.txt").write_text("\n".join(lines), encoding="utf-8")

    print("\n=== FINAL AUDIT ===")
    print(f"Checked: {len(results)} / {len(cars)}")
    print(f"Counts: {counts}")
    print("Dataset modified: NO")

asyncio.run(main())
