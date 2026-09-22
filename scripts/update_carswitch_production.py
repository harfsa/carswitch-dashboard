#!/usr/bin/env python3
import asyncio, json, os, re, tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

DATA = Path("carswitch_data.json")
TIMEOUT = int(os.getenv("PAGE_TIMEOUT_MS", "60000"))
WAIT_MS = int(os.getenv("WAIT_AFTER_LOAD_MS", "4000"))


def clean(s): return re.sub(r"\s+", " ", s or "").strip()

def grab(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m: return m.group(1)
    return None

def parse(title):
    price = grab([r"(?:SAR|ريال)\s*([\d,]+)", r"for sale:\s*([\d,]+)\s*(?:SAR|ريال)"], title)
    mileage = grab([r"[-–]\s*([\d,]+)\s*KM\b", r"([\d,]+)\s*KM\b", r"([\d,]+)\s*كم\b"], title)
    year = grab([r"\b((?:19|20)\d{2})\b"], title)
    return (int(price.replace(",", "")) if price else None,
            int(mileage.replace(",", "")) if mileage else None,
            int(year) if year else None)

def unavailable(text):
    t = text.lower()
    return any(x in t for x in ["page not found", "listing not found", "car not found", "vehicle not found", "listing is no longer available", "no longer available", "غير متوفر", "غير متاحة"])

async def check(page, car):
    url = car.get("url")
    if not url: return car, "unparsed", "missing url"
    try:
        r = await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT)
        await page.wait_for_timeout(WAIT_MS)
        status = r.status if r else None
        title = clean(await page.title())
        body = clean(await page.locator("body").inner_text(timeout=15000))
        if status == 404: return car, "gone", "HTTP 404"
        if status in (403,429,500,502,503,504) or status != 200:
            return car, "temporary", f"HTTP {status}"
        if unavailable(f"{title} {body}"): return car, "gone", "explicit unavailable marker"
        if "carswitch" not in (title + " " + body[:5000]).lower():
            return car, "unparsed", "not recognized as CarSwitch"
        price, mileage, year = parse(title)
        new = deepcopy(car)
        if price is not None: new["price"] = price
        if mileage is not None: new["mileage"] = mileage
        if year is not None: new["year"] = year
        return new, "active", "verified"
    except PlaywrightTimeoutError: return car, "temporary", "timeout"
    except Exception as e: return car, "temporary", f"{type(e).__name__}: {e}"

async def main():
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    cars = payload["cars"]
    results=[]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="ar-SA", viewport={"width":1440,"height":1000}, user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36")
        page = await ctx.new_page()
        for i, car in enumerate(cars,1):
            new, state, reason = await check(page, car)
            results.append((car,new,state,reason))
            print(f"[{i}/{len(cars)}] {state} | {car.get('url')} | {reason}")
        await browser.close()

    counts={s:sum(1 for *_, st, _ in results if st==s) for s in {r[2] for r in results}}
    updated=[]
    for old,new,state,_ in results:
        # Production safety: only verified active records are updated.
        # Gone/temporary/unparsed records remain unchanged; nothing is deleted.
        updated.append(new if state=="active" else old)

    payload["cars"]=updated
    payload["count"]=len(updated)
    payload["updated_at"]=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["update_stats"]={"checked":len(results),"counts":counts,"deleted":0}

    fd,tmp=tempfile.mkstemp(prefix="carswitch_",suffix=".json",dir=str(DATA.parent)); os.close(fd)
    Path(tmp).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    os.replace(tmp,DATA)
    Path("carswitch_update_report.json").write_text(json.dumps({"generated_at":payload["updated_at"],"counts":counts,"deleted":0,"note":"Only verified active records were updated; no records deleted."},ensure_ascii=False,indent=2),encoding="utf-8")
    print("=== FINAL ===")
    print(counts)
    print("Deleted: 0")

if __name__ == "__main__": asyncio.run(main())
