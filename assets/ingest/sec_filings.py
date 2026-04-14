"""@bruin
name: raw.sec_filings
type: python
columns:
  - name: ticker
    type: string
    checks:
      - name: not_null
  - name: cik
    type: string
    checks:
      - name: not_null
  - name: accession_number
    type: string
    checks:
      - name: not_null
custom_checks:
  - name: ticker_accession_unique
    description: "A filing (accession_number) may legitimately be shared across multiple tickers for the same CIK (e.g. common stock + warrants), but the (ticker, accession_number) pair must be unique."
    query: |
      SELECT COUNT(*) FROM (
        SELECT ticker, accession_number, COUNT(*) c
        FROM raw.sec_filings
        GROUP BY ticker, accession_number
        HAVING c > 1
      )
    value: 0
@bruin"""

import os
import duckdb
import requests
import time

DB_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ten_k.db")
)


def connect_db(retries=15, delay=2):
    for attempt in range(retries):
        try:
            return duckdb.connect(DB_PATH)
        except duckdb.IOException:
            if attempt < retries - 1:
                print(f"  DB locked, retrying in {delay}s...")
                time.sleep(delay)
            else:
                raise


def load_tickers(con):
    """Load ticker symbols from config.selected_tickers (source of truth)."""
    con.execute("CREATE SCHEMA IF NOT EXISTS config")
    con.execute("""
        CREATE TABLE IF NOT EXISTS config.selected_tickers (
            ticker VARCHAR PRIMARY KEY,
            added_at TIMESTAMP DEFAULT now()
        )
    """)
    tickers = [
        r[0]
        for r in con.execute(
            "SELECT ticker FROM config.selected_tickers ORDER BY ticker"
        ).fetchall()
    ]
    if tickers:
        print(
            f"Loaded {len(tickers)} tickers from config.selected_tickers: {', '.join(tickers)}"
        )
    else:
        print("config.selected_tickers is empty — no tickers to process")
    return tickers


HEADERS = {
    "User-Agent": os.environ.get("SEC_USER_AGENT", "10KAnalyzer contact@example.com"),
    "Accept-Encoding": "gzip, deflate",
}

# SEC requires CIK to be zero-padded to 10 digits
TICKER_TO_CIK_URL = "https://www.sec.gov/files/company_tickers.json"


def get_cik_map():
    """Fetch the SEC ticker-to-CIK mapping."""
    resp = requests.get(TICKER_TO_CIK_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    mapping = {}
    for entry in data.values():
        ticker = entry["ticker"].upper()
        cik = str(entry["cik_str"]).zfill(10)
        mapping[ticker] = {"cik": cik, "company_name": entry["title"]}
    return mapping


def get_10k_filings(cik):
    """Get 10-K filing metadata from EDGAR for a given CIK."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    filings = []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])
    report_dates = recent.get("reportDate", [])

    for i, form in enumerate(forms):
        if form == "10-K":
            accession_no = accessions[i]
            accession_dashed = accession_no.replace("-", "")
            filings.append(
                {
                    "accession_number": accession_no,
                    "filing_date": filing_dates[i],
                    "report_date": report_dates[i],
                    "primary_doc": primary_docs[i],
                    "filing_url": (
                        f"https://www.sec.gov/Archives/edgar/data/"
                        f"{cik.lstrip('0')}/{accession_dashed}/{primary_docs[i]}"
                    ),
                    "index_url": (
                        f"https://www.sec.gov/Archives/edgar/data/"
                        f"{cik.lstrip('0')}/{accession_dashed}/"
                    ),
                }
            )
    form_counts = {}
    for f in forms:
        form_counts[f] = form_counts.get(f, 0) + 1
    return filings, form_counts


def record_status(con, ticker, status, message):
    con.execute("""
        CREATE TABLE IF NOT EXISTS config.ingest_status (
            ticker VARCHAR PRIMARY KEY,
            status VARCHAR,
            message VARCHAR,
            updated_at TIMESTAMP DEFAULT now()
        )
    """)
    con.execute(
        "INSERT OR REPLACE INTO config.ingest_status(ticker, status, message, updated_at) "
        "VALUES (?, ?, ?, now())",
        [ticker, status, message],
    )


def materialize(context):
    con = connect_db()
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute("CREATE SCHEMA IF NOT EXISTS config")

    con.execute("""
        CREATE TABLE IF NOT EXISTS raw.sec_filings (
            ticker VARCHAR,
            company_name VARCHAR,
            cik VARCHAR,
            accession_number VARCHAR,
            filing_date DATE,
            report_date DATE,
            primary_doc VARCHAR,
            filing_url VARCHAR,
            index_url VARCHAR
        )
    """)
    con.execute("DELETE FROM raw.sec_filings")

    tickers = load_tickers(con)
    if not tickers:
        print("No tickers to process, exiting")
        con.close()
        return

    cik_map = get_cik_map()
    time.sleep(0.15)

    # Clear prior status rows for this run
    con.execute("""
        CREATE TABLE IF NOT EXISTS config.ingest_status (
            ticker VARCHAR PRIMARY KEY,
            status VARCHAR,
            message VARCHAR,
            updated_at TIMESTAMP DEFAULT now()
        )
    """)
    con.execute(
        "DELETE FROM config.ingest_status WHERE ticker IN (SELECT ticker FROM config.selected_tickers)"
    )

    rows = []
    for ticker in tickers:
        if ticker not in cik_map:
            msg = "Ticker not found in SEC company_tickers.json (unknown or delisted)."
            print(f"Warning: {ticker}: {msg}")
            record_status(con, ticker, "unknown_ticker", msg)
            continue

        info = cik_map[ticker]
        cik = info["cik"]
        company_name = info["company_name"]

        print(f"Fetching 10-K filings for {ticker} (CIK: {cik})...")
        try:
            filings, form_counts = get_10k_filings(cik)
        except Exception as e:
            msg = f"SEC submissions fetch failed: {e}"
            print(f"  {ticker}: {msg}")
            record_status(con, ticker, "error", msg)
            time.sleep(0.15)
            continue

        if not filings:
            if form_counts.get("20-F", 0) > 0 or form_counts.get("20-F/A", 0) > 0:
                msg = (
                    "Foreign private issuer — files 20-F (IFRS), not 10-K. "
                    "This pipeline currently only supports 10-K / US GAAP filers."
                )
                status = "not_10k_filer"
            elif form_counts.get("40-F", 0) > 0:
                msg = "Canadian filer — files 40-F, not 10-K. Not supported by this pipeline."
                status = "not_10k_filer"
            elif form_counts.get("10-K/A", 0) > 0 and not form_counts.get("10-K"):
                msg = (
                    "Only 10-K/A amendments found; no original 10-K in recent history."
                )
                status = "no_10k"
            elif not form_counts:
                msg = "No filings returned by SEC submissions API."
                status = "no_filings"
            else:
                top_forms = ", ".join(
                    f"{k} ({v})"
                    for k, v in sorted(form_counts.items(), key=lambda kv: -kv[1])[:4]
                )
                msg = f"No 10-K in recent filings. Found: {top_forms}."
                status = "no_10k"
            print(f"  {ticker}: {msg}")
            record_status(con, ticker, status, msg)
            time.sleep(0.15)
            continue

        for f in filings:
            rows.append(
                (
                    ticker,
                    company_name,
                    cik,
                    f["accession_number"],
                    f["filing_date"],
                    f["report_date"],
                    f["primary_doc"],
                    f["filing_url"],
                    f["index_url"],
                )
            )
        record_status(
            con, ticker, "ingested", f"{len(filings)} 10-K filing(s) ingested."
        )

        # SEC rate limit: 10 req/sec
        time.sleep(0.15)

    if rows:
        con.executemany(
            "INSERT INTO raw.sec_filings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    count = con.execute("SELECT count(*) FROM raw.sec_filings").fetchone()[0]
    print(f"Loaded {count} 10-K filing records")
    con.close()


if __name__ == "__main__":
    materialize(None)
