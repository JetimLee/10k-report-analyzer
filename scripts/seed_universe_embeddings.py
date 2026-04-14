"""Seed analytics.sec_universe_embeddings with Item 1 (Business) embeddings for a
broad universe of SEC-listed companies. Run manually (not part of `bruin run .`):

    python scripts/seed_universe_embeddings.py                       # S&P 500 (default)
    python scripts/seed_universe_embeddings.py --index nasdaq100     # NASDAQ-100
    python scripts/seed_universe_embeddings.py --index sp500+nasdaq100  # union
    python scripts/seed_universe_embeddings.py --tickers FILE        # custom ticker list
    python scripts/seed_universe_embeddings.py --limit 50            # smoke test

The script is resumable: tickers already present in the table are skipped unless
their latest 10-K has a newer fiscal year.
"""

import argparse
import csv
import os
import re
import sys
import time

import duckdb
import requests

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(PROJECT_ROOT, "ten_k.db")
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NASDAQ100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
MAX_CHARS = 20000
HEADERS = {
    "User-Agent": os.environ.get("SEC_USER_AGENT", "10KAnalyzer contact@example.com"),
    "Accept-Encoding": "gzip, deflate",
}

SECTION_START = re.compile(r"Item\s+1[\.\s\-\u2013\u2014]+Business", re.IGNORECASE)
SECTION_END = re.compile(r"Item\s+(?:1A|1B|1C|2|7A|8)[\.\s\-\u2013\u2014]", re.IGNORECASE)
CROSS_REF = re.compile(
    r"(?:see|per|under|in|to|of|from|pursuant to|refer to|included in|contained in)\s*$",
    re.IGNORECASE,
)
HTML_TAG = re.compile(r"<[^>]+>")
HTML_ENTITY = re.compile(r"&(?:[a-zA-Z]+|#\d+);")


def fetch_sp500():
    """Scrape the S&P 500 constituents list from Wikipedia."""
    print("Fetching S&P 500 list from Wikipedia…")
    resp = requests.get(WIKI_SP500_URL, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
    resp.raise_for_status()
    # Quick-and-dirty: find all <td><a ...>TICKER</a> patterns in the first table
    rows = re.findall(r'<td><a [^>]*>([A-Z][A-Z0-9\.\-]{0,5})</a>', resp.text)
    tickers = sorted({t.replace(".", "-") for t in rows})
    print(f"  Found {len(tickers)} tickers")
    return tickers


def fetch_nasdaq100():
    """Scrape the NASDAQ-100 constituents list from Wikipedia."""
    print("Fetching NASDAQ-100 list from Wikipedia…")
    resp = requests.get(
        WIKI_NASDAQ100_URL, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30
    )
    resp.raise_for_status()
    # The constituents table uses <td><a ...>TICKER</a> for the symbol column.
    rows = re.findall(r'<td><a [^>]*>([A-Z][A-Z0-9\.\-]{0,5})</a>', resp.text)
    tickers = sorted({t.replace(".", "-") for t in rows})
    print(f"  Found {len(tickers)} tickers")
    return tickers


def fetch_index(name):
    name = name.lower()
    if name == "sp500":
        return fetch_sp500()
    if name == "nasdaq100":
        return fetch_nasdaq100()
    if name in ("sp500+nasdaq100", "nasdaq100+sp500", "combined", "union"):
        return sorted(set(fetch_sp500()) | set(fetch_nasdaq100()))
    raise ValueError(f"Unknown index: {name}")


def load_tickers_file(path):
    with open(path) as f:
        reader = csv.DictReader(f) if path.endswith(".csv") else None
        if reader and "ticker" in reader.fieldnames:
            return [r["ticker"].strip().upper() for r in reader if r["ticker"].strip()]
        f.seek(0)
        return [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]


def get_cik_map():
    resp = requests.get(SEC_TICKERS_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return {
        e["ticker"].upper(): {
            "cik": str(e["cik_str"]).zfill(10),
            "company_name": e["title"],
        }
        for e in resp.json().values()
    }


def latest_10k(cik):
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    recent = data.get("filings", {}).get("recent", {})
    for i, form in enumerate(recent.get("form", [])):
        if form == "10-K":
            acc = recent["accessionNumber"][i].replace("-", "")
            doc = recent["primaryDocument"][i]
            report_date = recent["reportDate"][i]
            return {
                "url": f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc}/{doc}",
                "fiscal_year": int(report_date[:4]),
                "accession": recent["accessionNumber"][i],
            }
    return None


def extract_business(html):
    text = HTML_TAG.sub(" ", html)
    text = HTML_ENTITY.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    matches = list(SECTION_START.finditer(text))
    if not matches:
        return None
    for m in reversed(matches):
        remaining = text[m.end():]
        end = None
        for cand in SECTION_END.finditer(remaining):
            before = remaining[max(0, cand.start() - 30):cand.start()]
            if CROSS_REF.search(before):
                continue
            end = cand
            break
        body = remaining[: end.start()] if end else remaining[:50000]
        body = body.strip()
        if len(body) > 500:
            return body[:50000]
    return None


def connect_db(retries=15, delay=2):
    for attempt in range(retries):
        try:
            return duckdb.connect(DB_PATH)
        except duckdb.IOException:
            if attempt < retries - 1:
                print(f"  DB locked, retrying in {delay}s…")
                time.sleep(delay)
            else:
                raise


def ensure_table(con):
    con.execute("CREATE SCHEMA IF NOT EXISTS analytics")
    con.execute("""
        CREATE TABLE IF NOT EXISTS analytics.sec_universe_embeddings (
            ticker VARCHAR PRIMARY KEY,
            cik VARCHAR,
            company_name VARCHAR,
            fiscal_year INTEGER,
            embedding FLOAT[],
            text_length INTEGER,
            embedded_at TIMESTAMP
        )
    """)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", help="Path to a CSV or newline-delimited ticker list")
    ap.add_argument(
        "--index",
        default="sp500",
        choices=["sp500", "nasdaq100", "sp500+nasdaq100"],
        help="Which index to seed (ignored if --tickers is given)",
    )
    ap.add_argument("--limit", type=int, help="Only process first N tickers (smoke test)")
    ap.add_argument("--force", action="store_true", help="Re-embed even if already cached")
    args = ap.parse_args()

    if args.tickers:
        tickers = load_tickers_file(args.tickers)
    else:
        tickers = fetch_index(args.index)
    if args.limit:
        tickers = tickers[: args.limit]

    print(f"Resolving CIKs for {len(tickers)} tickers…")
    cik_map = get_cik_map()
    time.sleep(0.15)

    con = connect_db()
    ensure_table(con)

    already = {}
    for t, fy in con.execute(
        "SELECT ticker, fiscal_year FROM analytics.sec_universe_embeddings"
    ).fetchall():
        already[t] = fy

    from sentence_transformers import SentenceTransformer
    print(f"Loading {MODEL_NAME}…")
    model = SentenceTransformer(MODEL_NAME)

    stats = {"embedded": 0, "skipped": 0, "no_cik": 0, "no_filing": 0, "no_section": 0, "error": 0}
    to_commit = []

    for i, ticker in enumerate(tickers, 1):
        if ticker not in cik_map:
            stats["no_cik"] += 1
            continue

        info = cik_map[ticker]
        cik = info["cik"]

        try:
            filing = latest_10k(cik)
            time.sleep(0.15)
        except Exception as e:
            print(f"[{i}/{len(tickers)}] {ticker}: submissions error — {e}")
            stats["error"] += 1
            continue

        if not filing:
            stats["no_filing"] += 1
            continue

        if not args.force and already.get(ticker, 0) >= filing["fiscal_year"]:
            stats["skipped"] += 1
            continue

        try:
            resp = requests.get(filing["url"], headers=HEADERS, timeout=60)
            resp.raise_for_status()
            body = extract_business(resp.text)
            time.sleep(0.2)
        except Exception as e:
            print(f"[{i}/{len(tickers)}] {ticker}: filing fetch error — {e}")
            stats["error"] += 1
            continue

        if not body:
            print(f"[{i}/{len(tickers)}] {ticker}: Item 1 extraction failed")
            stats["no_section"] += 1
            continue

        vec = model.encode(body[:MAX_CHARS], normalize_embeddings=True).tolist()
        to_commit.append((
            ticker, cik, info["company_name"], filing["fiscal_year"],
            vec, len(body),
        ))
        stats["embedded"] += 1
        print(f"[{i}/{len(tickers)}] {ticker} FY{filing['fiscal_year']} ✓ ({len(body)} chars)")

        # Flush every 25 records so a crash doesn't lose everything
        if len(to_commit) >= 25:
            con.executemany(
                """INSERT OR REPLACE INTO analytics.sec_universe_embeddings
                   VALUES (?, ?, ?, ?, ?, ?, now())""",
                to_commit,
            )
            to_commit = []

    if to_commit:
        con.executemany(
            """INSERT OR REPLACE INTO analytics.sec_universe_embeddings
               VALUES (?, ?, ?, ?, ?, ?, now())""",
            to_commit,
        )

    total = con.execute("SELECT count(*) FROM analytics.sec_universe_embeddings").fetchone()[0]
    con.close()

    print("\n=== Summary ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"Table now holds {total} rows.")


if __name__ == "__main__":
    sys.exit(main())
