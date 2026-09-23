#!/usr/bin/env python3
import asyncio, json, os, re, sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

DATA = Path("carswitch_data.json")
TIMEOUT = int(os.getenv("PAGE_TIMEOUT_MS", "60000"))
WAIT_MS = int(os.getenv("WAIT_AFTER_LOAD_MS", "3000"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
TEST_URLS = [u.strip() for u in os.getenv("TEST_URLS", "").split(",") if u.strip()]


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def grab(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1)
    return None


def parse_text(text):
    price = grab(
        [
            r"cash\s+price\s*[:\-]?\s*(?:SAR|ر\.س\.?|ريال)\s*([\d,]+(?:\.\d+)?)",
            r"(?:SAR|ر\.س\.?|ريال)\s*([\d,]+(?:\.\d+)?)\s*(?=cash\s+price)",
            r"السعر\s+النقدي\s*[:\-]?\s*(?:SAR|ر\.س\.?|ريال)?\s*([\d,]+(?:\.\d+)?)",
        ],
        text,
    )
    mileage = grab(
        [
            r"([\d,]{2,})\s*(?:KM|km|كم|كيلومتر)",
            r"(?:mileage|odometer)\s*[:=]\s*([\d,]{2,})",
        ],
        text,
    )
    year = grab(
        [
            r"\b((?:19|20)\d{2})\b",
            r"(?:year|model year)\s*[:=]\s*((?:19|20)\d{2})",
        ],
        text,
    )
    return (
        int(float(price.replace(",", ""))) if price else None,
        int(mileage.replace(",", "")) if mileage else None,
        int(year) if year else None,
    )


def unavailable(text):
    t = text.lower()
    return any(
        x in t
        for x in [
            "page not found", "listing not found", "car not found", "vehicle not found",
            "listing is no longer available", "no longer available",
            "this listing has been removed", "listing removed",
            "غير متوفر", "غير متاحة", "تم حذف الإعلان", "الإعلان غير متوفر",
        ]
    )


def looks_like_listing(title, body, url, car):
    sample = f"{title} {body[:20000]} {url}".lower()
    brand = str(car.get("brand", "")).lower()
    model = str(car.get("model", "")).lower()
    signals = [
        bool(re.search(r"\b(?:19|20)\d{2}\b", sample)),
        bool(re.search(r"cash\s+price|السعر\s+النقدي", sample, re.I)),
        bool(re.search(r"[\d,]{2,}\s*(?:km|كم|كيلومتر)\b", sample, re.I)),
        bool(brand and brand in sample),
        bool(model and model in sample),
    ]
    return sum(signals) >= 1


def current_gone_streak(car):
    try:
        return max(0, int(car.get("gone_streak", 0) or 0))
    except (TypeError, ValueError):
        return 0


def apply_availability_state(car, state):
    streak_before = current_gone_streak(car)

    if state == "gone":
        streak_after = streak_before + 1
        if streak_after >= 2:
            return None, "delete", streak_before, streak_after
        new = deepcopy(car)
        new["availability_status"] = "suspected_gone"
        new["gone_streak"] = streak_after
        return new, "retain", streak_before, streak_after

    if state == "active":
        new = deepcopy(car)
        new["availability_status"] = "active"
        new["gone_streak"] = 0
        return new, "retain", streak_before, 0

    # Do not reset a previously confirmed gone streak on a transient fetch
    # failure. A temporary/unparsed result is not evidence that the car returned.
    new = deepcopy(car)
    new["availability_status"] = "needs_recheck"
    new["gone_streak"] = streak_before
    return new, "retain", streak_before, streak_before


def self_test():
    base = {"url": "https://example.test/1", "price": 100}

    first, action1, before1, after1 = apply_availability_state(base, "gone")
    assert action1 == "retain"
    assert before1 == 0 and after1 == 1
    assert first["availability_status"] == "suspected_gone"
    assert first["gone_streak"] == 1

    second, action2, before2, after2 = apply_availability_state(first, "gone")
    assert second is None
    assert action2 == "delete"
    assert before2 == 1 and after2 == 2

    interrupted, action3, before3, after3 = apply_availability_state(first, "temporary")
    assert action3 == "retain"
    assert interrupted["gone_streak"] == 1
    assert before3 == 1 and after3 == 1

    active, action4, before4, after4 = apply_availability_state(first, "active")
    assert action4 == "retain"
    assert active["gone_streak"] == 0
    assert active["availability_status"] == "active"
    assert before4 == 1 and after4 == 0

    print("Gone-streak self-test passed: deletion requires two consecutive definitive 'gone' checks.")


async def check(page, car):
    url = car.get("url") or car.get("listing_url") or car.get("link")
    if not url:
        return car, "unparsed", "missing url"

    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT)
        await page.wait_for_timeout(WAIT_MS)

        status = response.status if response else None
        title = clean(await page.title())
        body = clean(await page.locator("body").inner_text(timeout=15000))
        combined = f"{title} {body}"

        if status in (403, 429, 500, 502, 503, 504):
            return car, "temporary", f"HTTP {status}"
        if status in (404, 410):
            return car, "gone", f"HTTP {status}"
        if status not in (200, 301, 302):
            return car, "temporary", f"HTTP {status}"
        if unavailable(combined):
            return car, "gone", "explicit unavailable marker"

        price, mileage, year = parse_text(combined)
        if not looks_like_listing(title, body, url, car):
            return car, "unparsed", "page reachable but listing fields not recognized"

        new = deepcopy(car)
        changed = []
        if price is not None and price != car.get("price"):
            new["price"] = price
            changed.append("price")
        if mileage is not None and mileage != car.get("mileage"):
            new["mileage"] = mileage
            changed.append("mileage")
        if year is not None and year != car.get("year"):
            new["year"] = year
            changed.append("year")

        new["last_checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return new, "active", "verified" + (
            f"; changed={','.join(changed)}" if changed else "; no field changes"
        )

    except PlaywrightTimeoutError:
        return car, "temporary", "timeout"
    except Exception as e:
        return car, "temporary", f"{type(e).__name__}: {e}"


async def main():
    if not DATA.exists():
        raise SystemExit(f"Missing dataset: {DATA}")

    payload = json.loads(DATA.read_text(encoding="utf-8"))
    all_cars = payload.get("cars", [])

    if not isinstance(all_cars, list) or not all_cars:
        raise SystemExit("Dataset contains no cars; refusing to modify it.")

    cars = all_cars
    if TEST_URLS:
        by_url = {
            c.get("url") or c.get("listing_url") or c.get("link"): c for c in all_cars
        }
        missing = [u for u in TEST_URLS if u not in by_url]
        if missing:
            raise SystemExit("TEST_URLS not found in dataset: " + ", ".join(missing))
        cars = [by_url[u] for u in TEST_URLS]

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="ar-SA",
            viewport={"width": 1440, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/140.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        for i, car in enumerate(cars, 1):
            new, state, reason = await check(page, car)
            carried = new if new is not None else car
            transitioned, action, streak_before, streak_after = apply_availability_state(
                carried, state
            )
            results.append(
                (
                    car,
                    new,
                    state,
                    reason,
                    transitioned,
                    action,
                    streak_before,
                    streak_after,
                )
            )
            print(
                f"[{i}/{len(cars)}] {state} | {car.get('url')} | {reason} "
                f"| gone_streak={streak_after} | action={action}"
            )

        await browser.close()

    states = [r[2] for r in results]
    counts = {s: states.count(s) for s in sorted(set(states))}
    would_delete = sum(1 for r in results if r[5] == "delete")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    report = {
        "generated_at": now,
        "checked": len(results),
        "dataset_total": len(all_cars),
        "counts": counts,
        "deleted": 0,
        "would_delete": would_delete,
        "dry_run": DRY_RUN,
        "test_urls": TEST_URLS,
        "policy": (
            "A record is deleted only after two consecutive definitive 'gone' checks. "
            "A temporary or unparsed check never causes deletion and preserves the current "
            "gone streak for the next definitive check."
        ),
        "changes": [
            {
                "url": old.get("url"),
                "state": state,
                "reason": reason,
                "action": action,
                "gone_streak_before": streak_before,
                "gone_streak_after": streak_after,
                "old_price": old.get("price"),
                "new_price": new.get("price"),
                "old_mileage": old.get("mileage"),
                "new_mileage": new.get("mileage"),
                "old_year": old.get("year"),
                "new_year": new.get("year"),
            }
            for old, new, state, reason, transitioned, action, streak_before, streak_after in results
        ],
    }

    Path("carswitch_update_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if DRY_RUN:
        print("DRY RUN: dataset NOT modified.")
        print(counts)
        print(f"Would delete: {would_delete}")
        print("Deleted: 0")
        return

    if TEST_URLS:
        raise SystemExit(
            "Refusing non-dry-run when TEST_URLS is set; clear TEST_URLS before production updates."
        )

    updated = []
    deleted = 0
    result_by_url = {
        (old.get("url") or old.get("listing_url") or old.get("link")): r
        for r in results
    }

    for car in all_cars:
        key = car.get("url") or car.get("listing_url") or car.get("link")
        old, new, state, reason, transitioned, action, streak_before, streak_after = result_by_url[key]

        if action == "delete":
            deleted += 1
            continue

        item = deepcopy(car)

        if state == "active":
            item.update(new)
            item["availability_status"] = "active"
            item["gone_streak"] = 0
        elif state == "gone":
            item["availability_status"] = "suspected_gone"
            item["gone_streak"] = streak_after
        else:
            item["availability_status"] = "needs_recheck"
            item["gone_streak"] = streak_after

        updated.append(item)

    payload["cars"] = updated
    payload["count"] = len(updated)
    payload["updated_at"] = now
    payload["update_stats"] = {
        "checked": len(results),
        "counts": counts,
        "deleted": deleted,
        "policy": (
            "delete only after two consecutive definitive 'gone' checks; "
            "temporary/unparsed never causes deletion and preserves the gone streak"
        ),
    }

    DATA.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(counts)
    print(f"Deleted: {deleted}")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        asyncio.run(main())
