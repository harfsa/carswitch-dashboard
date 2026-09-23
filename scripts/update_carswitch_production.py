#!/usr/bin/env python3
import asyncio, json, os, re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

DATA = Path("data/carswitch_data.json")
TIMEOUT = int(os.getenv("PAGE_TIMEOUT_MS", "60000"))
WAIT_MS = int(os.getenv("WAIT_AFTER_LOAD_MS", "3000"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

def clean(s): return re.sub(r"\s+", " ", s or "").strip()
def grab(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m: return m.group(1)
    return None

def parse_text(text):
    price = grab([r"(?:SAR|ر\.س\.?|ريال)\s*([\d,]{3,})", r"([\d,]{3,})\s*(?:SAR|ر\.س\.?|ريال)", r"price\s*[:=]\s*([\d,]{3,})"], text)
    mileage = grab([r"([\d,]{2,})\s*(?:KM|km|كم|كيلومتر)", r"(?:mileage|odometer)\s*[:=]\s*([\d,]{2,})"], text)
    year = grab([r"\b((?:19|20)\d{2})\b", r"(?:year|model year)\s*[:=]\s*((?:19|20)\d{2})"], text)
    return (int(price.replace(",", "")) if price else None, int(mileage.replace(",", "")) if mileage else None, int(year) if year else None)

def unavailable(text):
    t=text.lower()
    return any(x in t for x in ["page not found","listing not found","car not found","vehicle not found","listing is no longer available","no longer available","this listing has been removed","listing removed","غير متوفر","غير متاحة","تم حذف الإعلان","الإعلان غير متوفر"])

def looks_like_listing(title, body, url, car):
    sample=f"{title} {body[:20000]} {url}".lower()
    brand=str(car.get("brand","")).lower(); model=str(car.get("model","")).lower()
    signals=[bool(re.search(r"\b(?:19|20)\d{2}\b",sample)), bool(re.search(r"(?:sar|ر\.س|ريال)\s*[\d,]{3,}",sample,re.I)), bool(re.search(r"[\d,]{2,}\s*(?:km|كم|كيلومتر)\b",sample,re.I)), bool(brand and brand in sample), bool(model and model in sample)]
    return sum(signals)>=1

async def check(page,car):
    url=car.get("url") or car.get("listing_url") or car.get("link")
    if not url: return car,"unparsed","missing url"
    try:
        response=await page.goto(url,wait_until="domcontentloaded",timeout=TIMEOUT)
        await page.wait_for_timeout(WAIT_MS)
        status=response.status if response else None
        title=clean(await page.title())
        body=clean(await page.locator("body").inner_text(timeout=15000))
        combined=f"{title} {body}"
        if status in (403,429,500,502,503,504): return car,"temporary",f"HTTP {status}"
        if status not in (200,301,302): return car,"temporary",f"HTTP {status}"
        if status in (404,410) or unavailable(combined): return car,"gone","explicit unavailable marker"
        price,mileage,year=parse_text(combined)
        if not looks_like_listing(title,body,url,car): return car,"unparsed","page reachable but listing fields not recognized"
        new=deepcopy(car); changed=[]
        if price is not None and price!=car.get("price"): new["price"]=price; changed.append("price")
        if mileage is not None and mileage!=car.get("mileage"): new["mileage"]=mileage; changed.append("mileage")
        if year is not None and year!=car.get("year"): new["year"]=year; changed.append("year")
        new["last_checked"]=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return new,"active","verified"+(f"; changed={','.join(changed)}" if changed else "; no field changes")
    except PlaywrightTimeoutError: return car,"temporary","timeout"
    except Exception as e: return car,"temporary",f"{type(e).__name__}: {e}"

async def main():
    if not DATA.exists(): raise SystemExit(f"Missing dataset: {DATA}")
    payload=json.loads(DATA.read_text(encoding="utf-8")); cars=payload.get("cars",[])
    if not isinstance(cars,list) or not cars: raise SystemExit("Dataset contains no cars; refusing to modify it.")
    results=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        ctx=await browser.new_context(locale="ar-SA",viewport={"width":1440,"height":1000},user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36")
        page=await ctx.new_page()
        for i,car in enumerate(cars,1):
            new,state,reason=await check(page,car); results.append((car,new,state,reason)); print(f"[{i}/{len(cars)}] {state} | {car.get('url')} | {reason}")
        await browser.close()
    states=[r[2] for r in results]; counts={s:states.count(s) for s in sorted(set(states))}; now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report={"generated_at":now,"checked":len(results),"counts":counts,"deleted":0,"dry_run":DRY_RUN,"policy":"Only verified active records would be updated; no records deleted.","changes":[{"url":old.get("url"),"state":state,"reason":reason,"old_price":old.get("price"),"new_price":new.get("price"),"old_mileage":old.get("mileage"),"new_mileage":new.get("mileage"),"old_year":old.get("year"),"new_year":new.get("year")} for old,new,state,reason in results if state=="active"]}
    Path("carswitch_update_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    if DRY_RUN:
        print("DRY RUN: dataset NOT modified."); print(counts); print("Deleted: 0"); return
    updated=[new if state=="active" else old for old,new,state,_ in results]
    payload["cars"]=updated; payload["count"]=len(updated); payload["updated_at"]=now; payload["update_stats"]={"checked":len(results),"counts":counts,"deleted":0,"policy":"only verified active records updated; no records deleted"}
    DATA.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8"); print(counts); print("Deleted: 0")

if __name__=="__main__": asyncio.run(main())
