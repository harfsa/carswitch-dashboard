#!/usr/bin/env python3
"""Safely refresh the CarSwitch luxury-car dataset."""
from __future__ import annotations
import json, os, re, time, random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "carswitch_data.json"
BASE = "https://ksa.carswitch.com"
BRANDS = {"BMW", "Lexus", "Range Rover", "Audi", "Genesis", "Land Rover", "Porsche", "Infiniti", "Cadillac", "Maserati"}
BRAND_SLUGS = {
    "BMW":"bmw", "Lexus":"lexus", "Range Rover":"range-rover", "Audi":"audi", "Genesis":"genesis",
    "Land Rover":"land-rover", "Porsche":"porsche", "Infiniti":"infiniti", "Cadillac":"cadillac", "Maserati":"maserati"
}
BRAND_ALIASES = {v:k for k,v in BRAND_SLUGS.items()}

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
})

def norm_num(s):
    if s is None: return None
    s = str(s).replace(",", "").replace("٬", "").replace("٫", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None

def listing_id(url):
    m = re.search(r"/(\d+)/?$", url or "")
    return m.group(1) if m else ""

def brand_from_url(url):
    parts = [x for x in urlparse(url).path.split("/") if x]
    if "used-car" not in parts:
        return None
    i = parts.index("used-car")
    if i + 1 >= len(parts): return None
    return BRAND_ALIASES.get(parts[i+1].lower())

def request(url, timeout=30):
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        return r
    except requests.RequestException:
        return None

def extract_jsonld(soup):
    vals = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or tag.get_text())
            vals.extend(data if isinstance(data, list) else [data])
        except Exception:
            pass
    return vals

def parse_detail(url, fallback=None):
    r = request(url)
    if r is None: return None, "temporary"
    if r.status_code == 404: return None, "gone"
    final_url = r.url
    if "/used-car/" not in final_url: return None, "gone"
    soup = BeautifulSoup(r.text, "html.parser")
    text = " ".join(soup.stripped_strings)
    fallback = fallback or {}
    brand = brand_from_url(final_url) or fallback.get("brand")
    model, year = fallback.get("model"), fallback.get("year")
    mileage, price = fallback.get("mileage"), fallback.get("price")

    for item in extract_jsonld(soup):
        if not isinstance(item, dict): continue
        offers = item.get("offers") or {}
        if isinstance(offers, list): offers = offers[0] if offers else {}
        model = model or item.get("model") or item.get("name")
        price = norm_num(offers.get("price")) or price
        year = norm_num(item.get("vehicleModelDate") or item.get("productionDate")) or year
        odo = item.get("mileageFromOdometer")
        if isinstance(odo, dict): odo = odo.get("value")
        mileage = norm_num(odo) or mileage

    title = soup.find("h1")
    title_text = title.get_text(" ", strip=True) if title else ""
    if title_text:
        m = re.search(r"\b(19\d{2}|20\d{2})\b\s+(.+)", title_text)
        if m:
            year = year or int(m.group(1))
            model = model or m.group(2).strip()
    if not year:
        m = re.search(r"\b(19\d{2}|20\d{2})\b", text); year = int(m.group(1)) if m else year
    if mileage is None:
        m = re.search(r"([\d,]+)\s*(?:KM|km)", text); mileage = norm_num(m.group(1)) if m else mileage
    if price is None:
        m = re.search(r"(?:SAR|ريال)\s*([\d,]+)", text); price = norm_num(m.group(1)) if m else price

    if not (brand and model and year and mileage is not None and price is not None):
        dead_words = ["no longer available", "not available", "page not found", "غير متاحة", "غير متوفر"]
        return (None, "gone") if any(w in text.lower() for w in dead_words) else (None, "unparsed")
    return {"brand":brand,"model":str(model).strip(),"year":int(float(year)),"mileage":float(mileage),"price":float(price),"url":final_url}, "ok"

def discover_urls(max_pages=30):
    found = {}
    for brand, slug in BRAND_SLUGS.items():
        empty = 0
        for page in range(1, max_pages + 1):
            url = f"{BASE}/en/saudi/used-cars/{slug}" + (f"?page={page}" if page > 1 else "")
            r = request(url)
            if not r or r.status_code >= 400: break
            soup = BeautifulSoup(r.text, "html.parser")
            links = 0
            for a in soup.select('a[href*="/used-car/"]'):
                href = urljoin(BASE, a.get("href", ""))
                if "/used-car/" in href and listing_id(href) and brand_from_url(href) == brand:
                    found[listing_id(href)] = href; links += 1
            if not links:
                empty += 1
                if empty >= 2: break
            else: empty = 0
            time.sleep(random.uniform(0.4, 0.9))
    return found

def main():
    seed = []
    if DATA.exists():
        try:
            payload = json.loads(DATA.read_text(encoding="utf-8"))
            seed = payload.get("cars", payload) if isinstance(payload, dict) else payload
        except Exception:
            seed = []
    seed_by_id = {listing_id(x.get("url")):x for x in seed if isinstance(x,dict) and listing_id(x.get("url"))}

    discovered = discover_urls()
    urls = {listing_id(x.get("url")):x.get("url") for x in seed if isinstance(x,dict) and listing_id(x.get("url"))}
    urls.update(discovered)

    updated, gone, temporary, unparsed = [], [], [], []
    for lid, url in urls.items():
        item, status = parse_detail(url, seed_by_id.get(lid))
        if status == "ok": updated.append(item)
        elif status == "gone": gone.append(lid)
        elif status == "temporary": temporary.append(lid)
        else: unparsed.append(lid)
        time.sleep(random.uniform(0.25, 0.6))

    by_id = {listing_id(x["url"]):x for x in updated}
    for lid in temporary + unparsed:
        if lid in seed_by_id and lid not in by_id: by_id[lid] = seed_by_id[lid]

    cars = sorted(by_id.values(), key=lambda x:(x["brand"],x["model"],x["year"],x["price"]))

    # Fail-safe: never replace a healthy dataset with an empty/suspiciously small result.
    if seed and (len(cars) == 0 or len(cars) < max(10, int(len(seed) * 0.50))):
        print(json.dumps({"status":"ABORTED_SAFE","reason":"result too small; existing dataset preserved","seed_count":len(seed),"candidate_count":len(cars),"unparsed":len(unparsed),"temporary_failures":len(temporary)}, ensure_ascii=False))
        return 2
    if not seed and not cars:
        print(json.dumps({"status":"ABORTED_SAFE","reason":"no initial dataset and no cars discovered"}, ensure_ascii=False))
        return 2

    result = {"updated_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),"source":"CarSwitch KSA","count":len(cars),"removed_confirmed":len(gone),"temporary_failures":len(temporary),"unparsed":len(unparsed),"cars":cars}
    tmp = DATA.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DATA)
    print(json.dumps({k:result[k] for k in result if k != "cars"}, ensure_ascii=False))
    return 0

if __name__ == "__main__": raise SystemExit(main())
