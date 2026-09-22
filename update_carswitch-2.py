#!/usr/bin/env python3
"""
Safe CarSwitch updater - individual listing URLs only.

For every listing URL already stored in data/carswitch_data.json:
- 200/valid listing: refresh fields that can be read.
- 404 or an explicit "not available" page: remove it.
- timeout/403/429/parser uncertainty: KEEP the old record.
- Never replace the dataset if the verification result is suspiciously small.

This version does NOT crawl brand pages.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "carswitch_data.json"
MIN_ACTIVE_RATE = float(os.getenv("MIN_ACTIVE_RATE", "0.50"))
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
DELAY = float(os.getenv("REQUEST_DELAY", "0.4"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}

DEAD_MARKERS = (
    "page not found",
    "listing not found",
    "car not found",
    "vehicle not found",
    "no longer available",
    "not available",
    "غير متوفر",
    "غير متاحة",
)

def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()

def number(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("٬", "").replace("٫", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None

def jsonld(soup):
    out = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            obj = json.loads(tag.string or tag.get_text())
            out.extend(obj if isinstance(obj, list) else [obj])
        except Exception:
            pass
    return out

def extract(url, old):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None, "temporary"

    if r.status_code == 404:
        return None, "gone"

    if r.status_code in (403, 429, 500, 502, 503, 504):
        return None, "temporary"

    if r.status_code != 200:
        return None, "temporary"

    final_url = r.url
    soup = BeautifulSoup(r.text, "html.parser")
    text = clean(soup.get_text(" ", strip=True))
    low = text.lower()

    # Only treat a listing as deleted when the page explicitly indicates it.
    if any(marker in low for marker in DEAD_MARKERS):
        # Avoid false positives from navigation text by requiring a strong marker.
        strong = ("page not found", "listing not found", "no longer available",
                  "car not found", "vehicle not found", "غير متوفر", "غير متاحة")
        if any(marker in low for marker in strong):
            return None, "gone"

    result = dict(old)
    result["url"] = final_url if "/used-car/" in final_url else url

    found = 0

    # JSON-LD is the preferred source.
    for obj in jsonld(soup):
        if not isinstance(obj, dict):
            continue
        typ = clean(obj.get("@type")).lower()
        if typ not in ("vehicle", "product", "car") and not any(
            k in obj for k in ("mileageFromOdometer", "vehicleModelDate", "offers")
        ):
            continue

        brand = obj.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        model = obj.get("model") or obj.get("name")
        year = obj.get("vehicleModelDate") or obj.get("productionDate")
        mileage = obj.get("mileageFromOdometer")
        if isinstance(mileage, dict):
            mileage = mileage.get("value")
        offers = obj.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = offers.get("price") if isinstance(offers, dict) else None

        if brand:
            result["brand"] = clean(brand); found += 1
        if model:
            result["model"] = clean(model); found += 1
        if year:
            result["year"] = int(number(year))
        if mileage is not None:
            result["mileage"] = number(mileage)
        if price is not None:
            result["price"] = number(price)

    # Text fallbacks for fields JSON-LD does not expose.
    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True) if h1 else "")
    years = re.findall(r"\b(?:19|20)\d{2}\b", title + " " + text)
    if years and not result.get("year"):
        result["year"] = int(years[0])

    if result.get("mileage") is None:
        m = re.search(r"([\d,]+)\s*(?:KM|km|kilometers|كيلومتر|كم)\b", text)
        if m:
            result["mileage"] = number(m.group(1))

    if result.get("price") is None:
        m = re.search(r"(?:SAR|ريال)\s*([\d,]+)", text, re.I)
        if m:
            result["price"] = number(m.group(1))

    # A valid HTTP 200 listing counts as verified even if one optional field
    # could not be parsed. We retain the old value for fields not found.
    if "/used-car/" in final_url and (title or found):
        result["last_checked"] = datetime.now(timezone.utc).isoformat()
        result["source"] = "CarSwitch"
        return result, "active"

    return None, "unparsed"

def main():
    if not DATA.exists():
        raise SystemExit(f"Missing {DATA}")

    payload = json.loads(DATA.read_text(encoding="utf-8"))
    cars = payload.get("cars", []) if isinstance(payload, dict) else payload

    if not isinstance(cars, list) or not cars:
        raise SystemExit("No cars found; refusing to modify dataset.")

    updated = []
    active = gone = temporary = unparsed = 0

    for i, old in enumerate(cars, 1):
        url = old.get("url") or old.get("listing_url") or old.get("link")

        if not url:
            updated.append(old)
            temporary += 1
            continue

        new, status = extract(url, old)

        if status == "active":
            updated.append(new)
            active += 1
        elif status == "gone":
            gone += 1
        elif status == "temporary":
            updated.append(old)
            temporary += 1
        else:
            updated.append(old)
            unparsed += 1

        print(f"[{i}/{len(cars)}] {status}: {url}")
        time.sleep(DELAY)

    active_rate = active / len(cars)

    print("\n=== CarSwitch update report ===")
    print(f"Previous: {len(cars)}")
    print(f"Verified active: {active}")
    print(f"Confirmed gone: {gone}")
    print(f"Temporary failures: {temporary}")
    print(f"Unparsed: {unparsed}")
    print(f"Active verification rate: {active_rate:.1%}")

    # Safety gate: never replace the existing dataset after a bad scrape.
    if active_rate < MIN_ACTIVE_RATE:
        print("SAFE MODE: verification rate too low. Existing dataset preserved.")
        raise SystemExit(3)

    payload["cars"] = updated
    payload["count"] = len(updated)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload["update_stats"] = {
        "previous_count": len(cars),
        "verified_active": active,
        "confirmed_gone": gone,
        "temporary_failures": temporary,
        "unparsed": unparsed,
    }

    tmp = DATA.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DATA)

    print(f"SUCCESS: saved {len(updated)} cars.")

if __name__ == "__main__":
    main()
