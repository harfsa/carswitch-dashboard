#!/usr/bin/env python3
"""
CarSwitch safe updater.

Reads data/carswitch_data.json, visits the individual listing URLs already
stored in the dataset, extracts current listing information, and only writes
changes when enough listings were successfully verified.

This script is deliberately conservative:
- A temporary/network/parsing failure never deletes a car.
- A listing is removed only after a successful page response explicitly
  indicates that the listing is unavailable.
- If the successful verification rate is too low, the old dataset is kept.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

DATA_FILE = Path("data/carswitch_data.json")
BACKUP_FILE = Path("data/carswitch_data.backup.json")

MIN_SUCCESS_RATE = float(os.getenv("MIN_SUCCESS_RATE", "0.60"))
MIN_SUCCESS_COUNT = int(os.getenv("MIN_SUCCESS_COUNT", "20"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
SLEEP_SECONDS = float(os.getenv("REQUEST_DELAY", "0.35"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Cache-Control": "no-cache",
}

UNAVAILABLE_MARKERS = [
    "car not found",
    "listing not found",
    "vehicle not found",
    "page not found",
    "no longer available",
    "listing is no longer available",
    "this car is no longer available",
    "this vehicle is no longer available",
    "404 - not found",
]

def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()

def to_number(value):
    if value is None:
        return None
    s = clean_text(value).replace(",", "").replace("٫", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        n = float(m.group())
        return int(n) if n.is_integer() else n
    except ValueError:
        return None

def first_number(text):
    return to_number(text)

def find_value(text, labels):
    low = text.lower()
    for label in labels:
        i = low.find(label.lower())
        if i >= 0:
            chunk = text[i:i + 180]
            n = first_number(chunk)
            if n is not None:
                return n
    return None

def jsonld_objects(soup):
    objects = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, list):
            objects.extend(data)
        elif isinstance(data, dict):
            objects.append(data)
    return objects

def walk_jsonld(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_jsonld(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_jsonld(v)

def extract_listing(html, url):
    soup = BeautifulSoup(html, "html.parser")
    visible = clean_text(soup.get_text(" ", strip=True))

    low = visible.lower()
    unavailable = any(marker in low for marker in UNAVAILABLE_MARKERS)

    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = clean_text(title_tag.get_text())

    canonical = ""
    c = soup.find("link", rel="canonical")
    if c and c.get("href"):
        canonical = urljoin(url, c["href"])

    data = {}
    for obj in jsonld_objects(soup):
        for item in walk_jsonld(obj):
            if not isinstance(item, dict):
                continue
            typ = str(item.get("@type", "")).lower()
            if "vehicle" in typ or "car" in typ or "product" in typ:
                data.update(item)

    # JSON-LD / metadata first.
    name = clean_text(data.get("name"))
    brand = data.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    brand = clean_text(brand)

    model = clean_text(data.get("model"))
    year = data.get("vehicleModelDate") or data.get("modelDate")
    price = None
    offers = data.get("offers")
    if isinstance(offers, dict):
        price = offers.get("price")
    elif isinstance(offers, list) and offers:
        if isinstance(offers[0], dict):
            price = offers[0].get("price")

    mileage = None
    odo = data.get("mileageFromOdometer")
    if isinstance(odo, dict):
        mileage = odo.get("value")
    elif odo is not None:
        mileage = odo

    # Page-text fallbacks.
    if not year:
        m = re.search(r"\b(19\d{2}|20\d{2})\b", visible)
        year = m.group(1) if m else None

    if price is None:
        price = find_value(visible, ["price", "SAR", "ريال"])

    if mileage is None:
        mileage = find_value(
            visible,
            ["mileage", "odometer", "km", "kilometers", "كم"]
        )

    if not name:
        h1 = soup.find("h1")
        name = clean_text(h1.get_text()) if h1 else title

    # If the page returned HTML but contains no meaningful listing identity,
    # treat it as an unparsed response, not as a deleted listing.
    meaningful = bool(name or brand or model or price is not None or mileage is not None)

    if unavailable and not meaningful:
        return {"status": "unavailable"}

    if not meaningful:
        return {"status": "unparsed"}

    return {
        "status": "active",
        "name": name,
        "brand": brand,
        "model": model,
        "year": to_number(year),
        "price": to_number(price),
        "mileage": to_number(mileage),
        "canonical_url": canonical or url,
    }

def normalize_car(car):
    return {
        "id": car.get("id"),
        "url": car.get("url") or car.get("listing_url") or car.get("link"),
        "brand": car.get("brand") or car.get("make") or "",
        "model": car.get("model") or car.get("description") or "",
        "year": car.get("year"),
        "mileage": car.get("mileage"),
        "price": car.get("price"),
    }

def apply_update(car, result):
    out = dict(car)
    if result.get("brand"):
        out["brand"] = result["brand"]
    if result.get("model"):
        out["model"] = result["model"]
    if result.get("name") and not out.get("model"):
        out["model"] = result["name"]
    if result.get("year") is not None:
        out["year"] = result["year"]
    if result.get("mileage") is not None:
        out["mileage"] = result["mileage"]
    if result.get("price") is not None:
        out["price"] = result["price"]
    if result.get("canonical_url"):
        out["url"] = result["canonical_url"]
    out["last_checked"] = datetime.now(timezone.utc).isoformat()
    out["source"] = "CarSwitch"
    return out

def main():
    if not DATA_FILE.exists():
        print(f"ERROR: {DATA_FILE} not found", file=sys.stderr)
        sys.exit(1)

    with DATA_FILE.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    cars = payload.get("cars", [])
    if not isinstance(cars, list) or not cars:
        print("ERROR: current dataset contains no cars; refusing to update.")
        sys.exit(2)

    # Keep a local backup before any successful write.
    BACKUP_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    session = requests.Session()
    session.headers.update(HEADERS)

    updated = []
    active = 0
    unavailable = 0
    unparsed = 0
    failed = 0

    for index, original in enumerate(cars, 1):
        car = dict(original)
        url = normalize_car(car)["url"]

        if not url or "carswitch.com" not in url:
            failed += 1
            updated.append(car)
            continue

        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)

            if response.status_code == 404:
                unavailable += 1
                # A genuine HTTP 404 is sufficient evidence to remove it.
                continue

            if response.status_code in (403, 429, 500, 502, 503, 504):
                failed += 1
                updated.append(car)
                continue

            response.raise_for_status()
            result = extract_listing(response.text, response.url)

            if result["status"] == "active":
                active += 1
                updated.append(apply_update(car, result))
            elif result["status"] == "unavailable":
                unavailable += 1
                # Remove only when the page itself clearly says unavailable.
            else:
                unparsed += 1
                updated.append(car)

        except requests.RequestException as exc:
            failed += 1
            updated.append(car)
            print(f"[{index}/{len(cars)}] request failed: {exc}")
        except Exception as exc:
            failed += 1
            updated.append(car)
            print(f"[{index}/{len(cars)}] parse failed: {exc}")

        if SLEEP_SECONDS:
            time.sleep(SLEEP_SECONDS)

    checked = active + unavailable + unparsed + failed
    success_rate = active / max(1, len(cars))

    print("---- CarSwitch update report ----")
    print(f"Previous cars: {len(cars)}")
    print(f"Verified active: {active}")
    print(f"Confirmed unavailable: {unavailable}")
    print(f"Unparsed: {unparsed}")
    print(f"Request/other failures: {failed}")
    print(f"Successful active rate: {success_rate:.1%}")

    # Never replace a healthy dataset with a partial scrape.
    if active < MIN_SUCCESS_COUNT or success_rate < MIN_SUCCESS_RATE:
        print(
            "SAFE MODE: verification threshold not met. "
            "Keeping the existing dataset unchanged."
        )
        sys.exit(3)

    payload["cars"] = updated
    payload["count"] = len(updated)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload["update_stats"] = {
        "previous_count": len(cars),
        "verified_active": active,
        "confirmed_unavailable": unavailable,
        "unparsed": unparsed,
        "failed": failed,
    }

    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    tmp.replace(DATA_FILE)

    print(f"SUCCESS: wrote {len(updated)} verified/retained cars.")

if __name__ == "__main__":
    main()
