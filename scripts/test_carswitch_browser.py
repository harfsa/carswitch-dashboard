import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

DATA = Path("data/carswitch_data.json")

def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

async def main():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    cars = data.get("cars", data)
    urls = [c.get("url") for c in cars if c.get("url")][:3]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            locale="ar-SA",
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
        )

        results = []
        for i, url in enumerate(urls, 1):
            print(f"\n=== TEST {i}/3 ===")
            print(url)
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(5000)
                title = clean(await page.title())
                text = clean(await page.locator("body").inner_text(timeout=15000))
                status = response.status if response else None

                # Look for the key values in rendered page text.
                year = re.search(r"\b(20\d{2}|19\d{2})\b", text)
                mileage = re.search(r"([\d,]+)\s*كم\b", text)
                price = re.search(r"(?:ريال|SAR)\s*([\d,]+)", text, re.I)

                print(f"HTTP status: {status}")
                print(f"Title: {title}")
                print(f"Year found: {year.group(1) if year else 'NO'}")
                print(f"Mileage found: {mileage.group(1) if mileage else 'NO'}")
                print(f"Price found: {price.group(1) if price else 'NO'}")

                results.append({
                    "url": url,
                    "status": status,
                    "title": title,
                    "year_found": bool(year),
                    "mileage_found": bool(mileage),
                    "price_found": bool(price),
                    "page_text_length": len(text),
                })
            except Exception as e:
                print(f"ERROR: {type(e).__name__}: {e}")
                results.append({"url": url, "error": f"{type(e).__name__}: {e}"})

        await browser.close()

    passed = sum(
        bool(r.get("title")) and r.get("year_found") and
        r.get("mileage_found") and r.get("price_found")
        for r in results
    )
    print("\n=== FINAL RESULT ===")
    print(f"Passed: {passed}/{len(results)}")
    print(json.dumps(results, ensure_ascii=False, indent=2))

    if passed < len(results):
        raise SystemExit(2)

asyncio.run(main())
