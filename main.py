import os, re, json, sqlite3
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI(title="DealBite API", version="1.0")

# Render sets PORT automatically; DB file will be recreated if Render restarts
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.getcwd(), "dealbite.db"))

def ensure_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant TEXT NOT NULL,
        market TEXT NOT NULL,
        title TEXT NOT NULL,
        starting_price REAL,
        all_prices TEXT,
        source_url TEXT,
        created_at TEXT NOT NULL
    )
    """)
    
    conn.commit()
    conn.close()

def fetch_deals(market: Optional[str]=None, restaurant: Optional[str]=None):
    ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    q = "SELECT restaurant, market, title, starting_price, all_prices, source_url, created_at FROM deals"
    params, where = [], []
    if market:
        where.append("market = ?")
        params.append(market)
    if restaurant:
        where.append("LOWER(restaurant) = ?")
        params.append(restaurant.lower())
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT 200"

    cur.execute(q, params)
    rows = cur.fetchall()
    conn.close()

    out = []
    for restaurant, market, title, starting_price, all_prices, source_url, created_at in rows:
        if not title or not title.strip():
            continue
        out.append({
            "restaurant": restaurant,
            "market": market,
            "title": title,
            "starting_price": starting_price,
            "all_prices": json.loads(all_prices) if all_prices else [],
            "source_url": source_url,
            "created_at": created_at
        })
    return out

def normalize(s: str) -> str:
    return " ".join((s or "").split()).strip()

def money_tokens(s: str):
    return re.findall(r"\$\d+(?:\.\d{1,2})?", s)

def extract_price_phrases(text: str):
    matches = re.findall(r"[^.]*\$\d+(?:\.\d{1,2})?[^.]*", text)
    cleaned = []
    for m in matches:
        m = normalize(m)
        if 20 <= len(m) <= 220:
            cleaned.append(m)
    return list(dict.fromkeys(cleaned))

def pick_price(prices):
    nums = []
    for p in prices:
        try: nums.append(float(p.replace("$","")))
        except: pass
    return min(nums) if nums else None

def clean_wendys_title(text: str) -> str:
    t = normalize(text)
    t = re.sub(r"^(Order Now\s*)+", "", t, flags=re.I)
    t = re.sub(r"Cover All Cravings\s*", "", t, flags=re.I)
    if "?" in t:
        after = t.split("?", 1)[1].strip()
        if len(after) >= 18:
            t = after
    if "value price points" in t.lower() or ("$4" in t and "$6" in t and "$8" in t):
        return "Biggie Deals price points: $4 Biggie Bites, $6 Biggie Bag, $8 Biggie Bundle"
    t = re.split(r"\b(?:Within|Choice of|includes|customers|available|Each)\b", t, maxsplit=1)[0].strip()
    t = t.strip(" -:;,.")
    if len(t) > 90:
        t = t[:90].rstrip(" -:;,.")
    return t

def upsert_deals(deals):
    ensure_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    created_at = datetime.now(timezone.utc).isoformat()
    added = 0
    for d in deals:
        if not d["title"].strip():
            continue
        cur.execute("""SELECT id FROM deals WHERE restaurant=? AND market=? AND title=? AND source_url=?""",
                    (d["restaurant"], d["market"], d["title"], d["source_url"]))
        if cur.fetchone() is None:
            cur.execute("""
            INSERT INTO deals (restaurant, market, title, starting_price, all_prices, source_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                d["restaurant"], d["market"], d["title"],
                d.get("starting_price"),
                json.dumps(d.get("all_prices", [])),
                d.get("source_url"),
                created_at
            ))
            added += 1
    conn.commit()
    conn.close()
    return added

def refresh_wendys_scrape():
    url = "https://www.wendys.com/mealdeals"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    r.raise_for_status()
    text = normalize(BeautifulSoup(r.text, "lxml").get_text(" "))
    phrases = extract_price_phrases(text)

    deals = []
    for ph in phrases:
        prices = money_tokens(ph)
        title = clean_wendys_title(ph)
        if not title:
            continue
        deals.append({
            "restaurant": "Wendy's",
            "market": "cleveland-oh",
            "title": title,
            "starting_price": pick_price(prices),
            "all_prices": prices,
            "source_url": url
        })

    # de-dupe
    seen, out = set(), []
    for d in deals:
        key = (d["title"], tuple(d["all_prices"]))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out

@app.get("/deals")
def get_deals(market: Optional[str]=None, restaurant: Optional[str]=None):
    deals = fetch_deals(market=market, restaurant=restaurant)
    return {"count": len(deals), "deals": deals}

@app.post("/refresh/wendys")
def refresh_wendys():
    deals = refresh_wendys_scrape()
    upsert_deals(deals)
    return RedirectResponse(url="/", status_code=303)

@app.get("/", response_class=HTMLResponse)
def dashboard():
    cle = fetch_deals(market="cleveland-oh")
    nat = fetch_deals(market="national-us")

    def table(rows, title):
        if not rows:
            return f"<h2>{title}</h2><p>No deals yet.</p>"
        html = f"<h2>{title}</h2><table border='1' cellpadding='6' cellspacing='0'>"
        html += "<tr><th>Restaurant</th><th>Title</th><th>Starting Price</th><th>Source</th></tr>"
        for d in rows:
            price = d["starting_price"] if d["starting_price"] is not None else ""
            html += "<tr>"
            html += f"<td>{d['restaurant']}</td>"
            html += f"<td>{d['title']}</td>"
            html += f"<td>{price}</td>"
            html += f'<td><a href="{d["source_url"]}" target="_blank">link</a></td>'
            html += "</tr>"
        html += "</table>"
        return html

    page = "<h1>DealBite Dashboard</h1>"
    page += f"<p><b>DB:</b> {DB_PATH}</p>"
    page += """
<form action="/refresh/wendys" method="post">
  <button type="submit">Refresh wendy's (Cleveland)</button>
</form>
<hr/>
"""
    page += table(cle, "Cleveland, OH")
    page += "<br/>"
    page += table(nat, "National Featured")
    return page

    cursor.execute("""
        SELECT price, recorded_at
        FROM price_history
        WHERE deal_id = ?
        ORDER BY recorded_at ASC
    """, (deal_id,))
    
    rows = cursor.fetchall()
    conn.close()

    return [{"price": row[0], "recorded_at": row[1]} for row in rows]
    cur.execute("""
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER,
    price REAL,
    recorded_at TEXT
)
""")
    deal_id = cur.lastrowid

cur.execute("""
INSERT INTO price_history (deal_id, price, recorded_at)
VALUES (?, ?, ?)
""", (deal_id, starting_price, created_at))
