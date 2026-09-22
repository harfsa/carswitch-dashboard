#!/usr/bin/env python3
"""Update the CarSwitch luxury-car dataset.

Strategy:
1. Read the current dataset as a seed.
2. Validate/discover listing URLs from CarSwitch search pages.
3. Keep only the configured luxury brands.
4. Re-fetch each listing page and extract current year/mileage/price.
5. Drop listings that are no longer live.
6. Write an atomic JSON file with update metadata.

The script is deliberately conservative: if a listing page is temporarily
unavailable, it is not immediately deleted. It is removed only after a
confirmed 404/redirect-away/dead-listing signal.
"""
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
SEARCH = "https://ksa.carswitch.com/en/saudi/used-cars/search?page={}"
BRANDS = {"BMW", "Lexus", "Range Rover", "Audi", "Genesis", "Land Rover", "Porsche", "Infiniti", "Cadillac", "Maserati"}
BRAND_ALIASES = {
    "bmw":"BMW", "lexus":"Lexus", "range-rover":"Range Rover", "range_rover":"Range Rover",
    "audi":"Audi", "genesis":"Genesis", "land-rover":"Land Rover", "land_rover":"Land Rover",
    "porsche":"Porsche", "infiniti":"Infiniti", "cadillac":"Cadillac", "maserati":"Maserati"
}

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; CarSwitchDashboardUpdater/1.0; +https://github.com/)",
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
    try:
        i = parts.index("used-car")
        slug = parts[i+1]
    except (ValueError, IndexError):
        return None
    return BRAND_ALIASES.get(slug.lower())


def request(url, timeout=25):
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
    if r is None:
        return None, "temporary"
    if r.status_code == 404:
        return None, "gone"
    final_url = r.url
    if "/used-car/" not in final_url:
        return None, "gone"
    soup = BeautifulSoup(r.text, "html.parser")
    text = " ".join(soup.stripped_strings)

    brand = brand_from_url(final_url) or (fallback or {}).get("brand")
    model = (fallback or {}).get("model")
    year = (fallback or {}).get("year")
    mileage = (fallback or {}).get("mileage")
    price = (fallback or {}).get("price")

    for item in extract_jsonld(soup):
        if not isinstance(item, dict):
            continue
        offers = item.get("offers") or {}
        if isinstance(offers, list): offers = offers[0] if offers else {}
        if not model:
            model = item.get("model") or item.get("name")
        price = norm_num(offers.get("price")) or price
        if not year:
            year = norm_num(item.get("vehicleModelDate") or item.get("productionDate")) or year
        if not mileage:
            mileage = norm_num(item.get("mileageFromOdometer", {}).get("value") if isinstance(item.get("mileageFromOdometer"), dict) else item.get("mileageFromOdometer")) or mileage

    title = soup.find("h1")
    title_text = title.get_text(" ", strip=True) if title else ""
    if title_text:
        m = re.search(r"(\d{4})\s+(.+)", title_text)
        if m and not year: year = int(m.group(1))
        if m and not model: model = m.group(2).strip()

    if not year:
        m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
        year = int(m.group(1)) if m else year
    if not mileage:
        m = re.search(r"([\d,]+)\s*(?:KM|km)", text)
        mileage = norm_num(m.group(1)) if m else mileage
    if not price:
        m = re.search(r"(?:SAR|ريال)\s*([\d,]+)", text)
        price = norm_num(m.group(1)) if m else price

    if not (brand and model and year and mileage is not None and price is not None):
        dead_words = ["no longer available", "not available", "page not found", "غير متاحة", "غير متوفر"]
        if any(w in text.lower() for w in dead_words):
            return None, "gone"
        return None, "unparsed"

    return {
        "brand": brand,
        "model": str(model).strip(),
        "year": int(float(year)),
        "mileage": float(mileage),
        "price": float(price),
        "url": final_url,
    }, "ok"


def discover_urls(max_pages=140):
    found = {}
    empty_pages = 0
    for page in range(1, max_pages + 1):
        r = request(SEARCH.format(page), timeout=30)
        if not r or r.status_code >= 400:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        links = []
        for a in soup.select('a[href*="/used-car/"]'):
            href = urljoin(BASE, a.get("href", ""))
            if "/used-car/" in href and listing_id(href):
                b = brand_from_url(href)
                if b in BRANDS:
                    links.append(href)
                    found[listing_id(href)] = href
        if not links:
            empty_pages += 1
            if empty_pages >= 2: break
        else:
            empty_pages = 0
        time.sleep(random.uniform(0.5, 1.2))
    return found


def main():
    if DATA.exists():
        payload = json.loads(DATA.read_text(encoding="utf-8"))
        seed = payload.get("cars", payload) if isinstance(payload, dict) else payload
    else:
        seed = []
    seed_by_id = {listing_id(x.get("url")): x for x in seed if listing_id(x.get("url"))}

    discovered = discover_urls()
    urls = dict((k, v) for k, v in ((listing_id(x.get("url")), x.get("url")) for x in seed) if k)
    urls.update(discovered)

    updated = []
    gone = []
    temporary = []
    unparsed = []
    for lid, url in urls.items():
        item, status = parse_detail(url, seed_by_id.get(lid))
        if status == "ok": updated.append(item)
        elif status == "gone": gone.append(lid)
        elif status == "temporary": temporary.append(lid)
        else: unparsed.append(lid)
        time.sleep(random.uniform(0.35, 0.8))

    by_id = {listing_id(x["url"]): x for x in updated}
    for lid in temporary + unparsed:
        if lid in seed_by_id and lid not in by_id:
            by_id[lid] = seed_by_id[lid]

    cars = sorted(by_id.values(), key=lambda x: (x["brand"], x["model"], x["year"], x["price"]))
    result = {
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "CarSwitch KSA",
        "count": len(cars),
        "removed_confirmed": len(gone),
        "temporary_failures": len(temporary),
        "unparsed": len(unparsed),
        "cars": cars,
    }
    tmp = DATA.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DATA)
    print(json.dumps({k: result[k] for k in result if k != "cars"}, ensure_ascii=False))

if __name__ == "__main__":
    main()
