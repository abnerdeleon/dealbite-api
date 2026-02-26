import os
import re
import json
import sqlite3
import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse


app = FastAPI(title="DealBite API", version="1.1")

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.getcwd(), "dealbite.db"))


# ----------------------------
# Helpers
# ----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(s: str) -> str:
    return " ".join((s or "").split()).strip()


def money_tokens(s: str) -> List[str]:
    # Finds $4, $4.99, etc.
    return re.findall(r"\$\d+(?:\.\d{1,2})?", s or "")


def pick_price(tokens: List[str]) -> Optional[float]:
    nums = []
    for t in tokens:
        try:
            nums.append(float(t.replace("$", "")))
        except Exception:
            pass
    return min(nums) if nums else None


def extract_price_phrases(text: str) -> List[str]:
    # Pull sentences/phrases that contain a $ amount
    matches = re.findall(r"[^.]*\$\d+(?:\.\d{1,2})?[^.]*", text)
    cleaned = []
    for m in matches:
        m = normalize(m)
        if 20 <= len(m) <= 220:
            cleaned.append(m)
    # de-dupe preserve order
    return list(dict.fromkeys(cleaned))


def clean_wendys_title(text: str) -> str:
    t = normalize(text)
    t = re.sub(r"^(Order Now\s*)+", "", t, flags=re.I)
    t = re.sub(r"Cover All Cravings\s*", "", t, flags=re.I)

    # Some Wendy's page copy has “?” then the actual headline
    if "?" in t:
        after = t.split("?", 1)[1].strip()
        if len(after) >= 18:
            t = after

    # Special-case: the biggie deal price-point line
    if "value price points" in t.lower() or ("$4" in t and "$6" in t and "$8" in t):
        return "Biggie Deals price points: $4 Biggie Bites, $6 Biggie Bag, $8 Biggie Bundle"

    # Trim after common boilerplate segments
    t = re.split(r"\b(?:Within|Choice of|includes|customers|available|Each)\b", t, maxsplit=1)[0].strip()
    t = t.strip(" -:;,.")
    if len(t) > 90:
        t = t[:90].rstrip(" -:;,.")
    return t


def make_deal_id(deal: Dict[str, Any]) -> str:
    # Stable across refreshes as long as these fields stay the same
    canonical = "|".join([
        str(deal.get("restaurant", "")).strip().lower(),
        str(deal.get("market", "")).strip().lower(),
        str(deal.get("title", "")).strip().lower(),
        str(deal.get("source_url", "")).strip().lower(),
    ])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def estimate_savings_from_prices(starting_price: Optional[float], all_prices: List[str]) -> Optional[float]:
    """
    Best-effort: if the phrase contains multiple distinct $ prices (like $4, $6, $8),
    treat (max - min) as a rough 'range' savings signal.
    This is NOT true savings vs regular menu price (we don't have that yet), but it's useful as a first metric.
    """
    nums = []
    for p in all_prices or []:
        try:
            nums.append(float(str(p).replace("$", "")))
        except Exception:
            pass

    nums = sorted(set(nums))
    if len(nums) >= 2:
        return round(nums[-1] - nums[0], 2)

    # If we only have one price token, no savings estimate
    return None


def compute_value_score(starting_price: Optional[float], estimated_savings: Optional[float]) -> float:
    """
    Simple, stable V1 scoring:
    - cheaper deals score higher
    - deals with a bigger price-range signal get a boost
    Output: 0..10
    """
    price = starting_price if (starting_price is not None and starting_price >= 0) else 999.0

    # Base: cheaper is better (price->score curve)
    base = 10.0 / (1.0 + (price / 5.0))  # $5 ~ base=5, $10 ~ base=3.33, etc.

    boost = 0.0
    if estimated_savings is not None:
        boost = min(3.0, estimated_savings)  # cap boost so it doesn't dominate

    score = base + boost
    if score < 0:
        score = 0.0
    if score > 10:
        score = 10.0
    return round(score, 2)


# ----------------------------
# Database
# ----------------------------
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db() -> None:
    conn = db_connect()
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
            created_at TEXT NOT NULL,
            UNIQUE(restaurant, market, title, source_url)
        )
    """)
    conn.commit()
    conn.close()


def upsert_deals(deals: List[Dict[str, Any]]) -> int:
    ensure_db()
    conn = db_connect()
    cur = conn.cursor()
    created_at = now_iso()
    added = 0

    for d in deals:
        title = normalize(d.get("title", ""))
        if not title:
            continue

        cur.execute("""
            INSERT OR IGNORE INTO deals
            (restaurant, market, title, starting_price, all_prices, source_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            d.get("restaurant", ""),
            d.get("market", ""),
            title,
            d.get("starting_price"),
            json.dumps(d.get("all_prices", [])),
            d.get("source_url"),
            created_at
        ))
        if cur.rowcount == 1:
            added += 1

    conn.commit()
    conn.close()
    return added


def fetch_deals(market: Optional[str] = None, restaurant: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    ensure_db()
    conn = db_connect()
    cur = conn.cursor()

    q = "SELECT restaurant, market, title, starting_price, all_prices, source_url, created_at FROM deals"
    params = []
    where = []

    if market:
        where.append("market = ?")
        params.append(market)

    if restaurant:
        where.append("LOWER(restaurant) = ?")
        params.append(restaurant.lower())

    if where:
        q += " WHERE " + " AND ".join(where)

    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    cur.execute(q, params)
    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        all_prices = json.loads(r["all_prices"]) if r["all_prices"] else []
        deal = {
            "restaurant": r["restaurant"],
            "market": r["market"],
            "title": r["title"],
            "starting_price": r["starting_price"],
            "all_prices": all_prices,
            "source_url": r["source_url"],
            "created_at": r["created_at"],
        }

        # add intelligence fields
        deal["id"] = make_deal_id(deal)
        deal["estimated_savings"] = estimate_savings_from_prices(deal["starting_price"], deal["all_prices"])
        deal["value_score"] = compute_value_score(deal["starting_price"], deal["estimated_savings"])

        out.append(deal)

    return out


# ----------------------------
# Scrapers
# ----------------------------
def refresh_wendys_scrape(market: str = "cleveland-oh") -> List[Dict[str, Any]]:
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
            "market": market,
            "title": title,
            "starting_price": pick_price(prices),
            "all_prices": prices,
            "source_url": url,
        })

    # de-dupe (title + prices)
    seen = set()
    out = []
    for d in deals:
        key = (d["title"], tuple(d["all_prices"]))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


# ----------------------------
# API Routes
# ----------------------------
@app.get("/deals")
def get_deals_api(market: Optional[str] = None, restaurant: Optional[str] = None):
    deals = fetch_deals(market=market, restaurant=restaurant)
    return {"count": len(deals), "deals": deals}


@app.get("/best")
def best_deal_api(market: Optional[str] = None, restaurant: Optional[str] = None):
    deals = fetch_deals(market=market, restaurant=restaurant)
    if not deals:
        return {"best": None, "reason": "No deals available yet."}

    deals_sorted = sorted(deals, key=lambda d: (d.get("value_score", 0), -(d.get("estimated_savings") or 0)), reverse=True)
    best = deals_sorted[0]

    reason_parts = []
    if best.get("estimated_savings") is not None:
        reason_parts.append(f"highest value signal (range ≈ ${best['estimated_savings']})")
    if best.get("starting_price") is not None:
        reason_parts.append(f"starting at ${best['starting_price']}")
    if not reason_parts:
        reason_parts.append("best available ranking")

    return {
        "best": best,
        "reason": "Ranked best because: " + ", ".join(reason_parts) + "."
    }


@app.post("/refresh/wendys")
def refresh_wendys():
    deals = refresh_wendys_scrape(market="cleveland-oh")
    added = upsert_deals(deals)
    # Redirect back to dashboard so you can see results immediately
    return RedirectResponse(url="/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    cle = fetch_deals(market="cleveland-oh")
    best = None
    if cle:
        best = sorted(cle, key=lambda d: d.get("value_score", 0), reverse=True)[0]

    def table(rows: List[Dict[str, Any]], title: str) -> str:
        if not rows:
            return f"<h2>{title}</h2><p>No deals yet.</p>"
        html = f"<h2>{title}</h2><table border='1' cellpadding='6' cellspacing='0'>"
        html += "<tr><th>Restaurant</th><th>Title</th><th>Start $</th><th>Score</th><th>Est. Savings</th><th>Source</th></tr>"
        for d in rows[:50]:
            price = "" if d["starting_price"] is None else d["starting_price"]
            score = d.get("value_score", "")
            sav = "" if d.get("estimated_savings") is None else d["estimated_savings"]
            html += "<tr>"
            html += f"<td>{d['restaurant']}</td>"
            html += f"<td>{d['title']}</td>"
            html += f"<td>{price}</td>"
            html += f"<td>{score}</td>"
            html += f"<td>{sav}</td>"
            html += f'<td><a href="{d["source_url"]}" target="_blank">link</a></td>'
            html += "</tr>"
        html += "</table>"
        return html

    page = "<h1>DealBite Dashboard</h1>"
    page += f"<p><b>DB:</b> {DB_PATH}</p>"
    page += """
<form action="/refresh/wendys" method="post">
  <button type="submit">Refresh Wendy's (Cleveland)</button>
</form>
<hr/>
"""

    if best:
        page += "<h2>Best Deal (Cleveland)</h2>"
        page += f"<p><b>{best['restaurant']}</b> — {best['title']}<br/>"
        page += f"Start: {best.get('starting_price')} | Score: {best.get('value_score')} | Est. Savings: {best.get('estimated_savings')}</p>"
        page += "<hr/>"

    page += table(cle, "Cleveland, OH (latest 50)")
    return page
