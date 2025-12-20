import asyncio
import csv
import json
import re
import sqlite3
from urllib.parse import urljoin

from playwright.async_api import async_playwright

START_URL = "https://classifieds.ksl.com/v2/search/marketType/Service"
DB_PATH = "ksl_services.sqlite"
CSV_PATH = "ksl_services.csv"

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ksl_services_listings (
  listing_id TEXT PRIMARY KEY,
  title TEXT,
  category TEXT,
  sub_category TEXT,
  market_type TEXT,
  price REAL,
  price_type TEXT,
  city TEXT,
  state TEXT,
  posted_at TEXT,
  url TEXT,
  raw_json TEXT
);
"""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(CREATE_SQL)
    conn.commit()
    return conn


def upsert(conn, row):
    conn.execute(
        """
        INSERT OR REPLACE INTO ksl_services_listings
        (listing_id, title, category, sub_category, market_type, price, price_type,
         city, state, posted_at, url, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        row,
    )


def safe_get(d, *keys):
    cur = d
    for k in keys:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return None
    return cur


def normalize_listing(item):
    listing_id = str(
        item.get("id")
        or item.get("listingId")
        or item.get("listing_id")
        or ""
    )
    title = item.get("title") or item.get("name")
    market_type = item.get("marketType") or item.get("market_type") or "Service"

    category = safe_get(item, "category", "name") or item.get("categoryName") or item.get(
        "category"
    )
    sub_category = safe_get(item, "subcategory", "name") or item.get(
        "subCategoryName"
    ) or item.get("subCategory")

    price = item.get("price")
    try:
        price = float(price) if price is not None else None
    except Exception:
        price = None

    price_type = item.get("priceType") or item.get("price_type")

    city = safe_get(item, "location", "city") or item.get("city")
    state = safe_get(item, "location", "state") or item.get("state")

    posted_at = item.get("postedTime") or item.get("createTime") or item.get(
        "createdTime"
    ) or item.get("posted_at")

    url_path = item.get("url") or item.get("detailUrl") or item.get("link")
    url = urljoin("https://classifieds.ksl.com", url_path) if url_path else None

    raw_json = json.dumps(item, ensure_ascii=False)

    if not listing_id:
        return None

    return (
        listing_id,
        title,
        category,
        sub_category,
        market_type,
        price,
        price_type,
        city,
        state,
        posted_at,
        url,
        raw_json,
    )


async def run():
    conn = init_db()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        page_payloads = []

        async def on_response(resp):
            try:
                url = resp.url
                ct = (resp.headers.get("content-type") or "").lower()
                if "application/json" in ct and "search" in url and "classifieds.ksl.com" in url:
                    body = await resp.text()
                    if body and (body.startswith("{") or body.startswith("[")):
                        page_payloads.append((url, body))
            except Exception:
                pass

        page.on("response", on_response)

        await page.goto(START_URL, wait_until="networkidle")

        def extract_total_and_items(payload_text):
            data = json.loads(payload_text)
            if isinstance(data, dict):
                items = data.get("items") or data.get("results") or data.get("listings") or []
                total = data.get("total") or data.get("totalCount") or data.get("count")
                return total, items, data
            if isinstance(data, list):
                return None, data, {"items": data}
            return None, [], {}

        chosen = None
        for url, body in list(page_payloads)[-10:]:
            total, items, _ = extract_total_and_items(body)
            if items:
                chosen = (url, body)
                break

        if not chosen:
            raise RuntimeError(
                "Could not detect the JSON results response. KSL may have changed their frontend."
            )

        seed_url, seed_body = chosen
        total, items, seed_data = extract_total_and_items(seed_body)

        def guess_page_params(url):
            m_limit = re.search(r"(perPage|limit)=([0-9]+)", url)
            m_page = re.search(r"(page|pageNumber|offset)=([0-9]+)", url)
            return (
                m_page.group(1) if m_page else None,
                int(m_page.group(2)) if m_page else None,
                m_limit.group(1) if m_limit else None,
                int(m_limit.group(2)) if m_limit else None,
            )

        page_key, page_val, limit_key, limit_val = guess_page_params(seed_url)

        if limit_key is None:
            limit_key = "perPage"
            limit_val = 96

        if page_key is None:
            page_key = "page"
            page_val = 1

        if total is None:
            total = seed_data.get("total") or seed_data.get("totalCount") or len(items)

        if not isinstance(total, int):
            try:
                total = int(total)
            except Exception:
                total = None

        if total is None:
            total = 1500

        def set_query_param(url, key, value):
            if re.search(rf"({re.escape(key)}=)", url):
                return re.sub(rf"({re.escape(key)}=)([^&]+)", rf"\1{value}", url)
            joiner = "&" if "?" in url else "?"
            return f"{url}{joiner}{key}={value}"

        base_url = set_query_param(seed_url, limit_key, limit_val)
        base_url = set_query_param(base_url, page_key, page_val)

        seen_ids = set()

        def ingest(items_list):
            nonlocal seen_ids
            n = 0
            for it in items_list:
                row = normalize_listing(it)
                if not row:
                    continue
                if row[0] in seen_ids:
                    continue
                seen_ids.add(row[0])
                upsert(conn, row)
                n += 1
            conn.commit()
            return n

        ingested = ingest(items)
        print(f"Seed ingested: {ingested}, total seen: {len(seen_ids)}")

        pages = (total + limit_val - 1) // limit_val
        for page_num in range(page_val + 1, pages + 1):
            page_payloads.clear()
            url = set_query_param(base_url, page_key, page_num)

            await page.goto(url, wait_until="networkidle")

            chosen = None
            for u, body in list(page_payloads)[-10:]:
                t, it, _ = extract_total_and_items(body)
                if it:
                    chosen = (u, body)
                    break

            if not chosen:
                html = await page.content()
                if "Forbidden" in html or "Access Denied" in html:
                    raise RuntimeError(
                        "Blocked mid run. Reduce rate, use residential IP, or run logged in."
                    )
                continue

            _, body = chosen
            _, it, _ = extract_total_and_items(body)
            ing = ingest(it)

            if page_num % 5 == 0:
                print(f"Page {page_num}/{pages}, ingested {ing}, total seen {len(seen_ids)}")

            await asyncio.sleep(0.6)

        await browser.close()

    with sqlite3.connect(DB_PATH) as conn2:
        cur = conn2.execute(
            """
          SELECT listing_id, title, category, sub_category, market_type, price, price_type,
                 city, state, posted_at, url
          FROM ksl_services_listings
          ORDER BY posted_at DESC
        """
        )
        rows = cur.fetchall()

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "listing_id",
                "title",
                "category",
                "sub_category",
                "market_type",
                "price",
                "price_type",
                "city",
                "state",
                "posted_at",
                "url",
            ]
        )
        w.writerows(rows)

    print(f"Done. Rows: {len(rows)}")
    print(f"SQLite: {DB_PATH}")
    print(f"CSV: {CSV_PATH}")


if __name__ == "__main__":
    asyncio.run(run())
