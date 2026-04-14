""" @bruin
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
@bruin """

import os
import duckdb
import requests
import time

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "ten_k.db"))


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

TICKERS_CSV = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "tickers.csv"))


def load_tickers():
    """Load ticker symbols from tickers.csv at the project root."""
    if not os.path.exists(TICKERS_CSV):
        print(f"Warning: {TICKERS_CSV} not found, no tickers to process")
        return []
    with open(TICKERS_CSV) as f:
        import csv
        reader = csv.DictReader(f)
        tickers = [row["ticker"].strip().upper() for row in reader if row["ticker"].strip()]
    print(f"Loaded {len(tickers)} tickers from tickers.csv: {', '.join(tickers)}")
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
            filings.append({
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
            })
    return filings


def materialize(context):
    con = connect_db()
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")

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

    tickers = load_tickers()
    if not tickers:
        print("No tickers to process, exiting")
        con.close()
        return

    cik_map = get_cik_map()
    time.sleep(0.15)

    rows = []
    for ticker in tickers:
        if ticker not in cik_map:
            print(f"Warning: {ticker} not found in SEC CIK map, skipping")
            continue

        info = cik_map[ticker]
        cik = info["cik"]
        company_name = info["company_name"]

        print(f"Fetching 10-K filings for {ticker} (CIK: {cik})...")
        filings = get_10k_filings(cik)

        for f in filings:
            rows.append((
                ticker,
                company_name,
                cik,
                f["accession_number"],
                f["filing_date"],
                f["report_date"],
                f["primary_doc"],
                f["filing_url"],
                f["index_url"],
            ))

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
