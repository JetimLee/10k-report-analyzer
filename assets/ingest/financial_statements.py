"""@bruin
name: raw.financial_statements
type: python
depends:
  - raw.sec_filings
columns:
  - name: ticker
    type: string
    checks:
      - name: not_null
  - name: metric_name
    type: string
    checks:
      - name: not_null
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


HEADERS = {
    "User-Agent": os.environ.get("SEC_USER_AGENT", "10KAnalyzer contact@example.com"),
    "Accept-Encoding": "gzip, deflate",
}

# XBRL tags we want to extract, mapped to friendly names
XBRL_TAGS = {
    # Income Statement
    "Revenues": "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "CostOfGoodsAndServicesSold": "CostOfGoodsSold",
    "CostOfRevenue": "CostOfGoodsSold",
    "GrossProfit": "GrossProfit",
    "OperatingIncomeLoss": "OperatingIncome",
    "NetIncomeLoss": "NetIncome",
    "EarningsPerShareDiluted": "EarningsPerShareDiluted",
    # Balance Sheet
    "Assets": "TotalAssets",
    "AssetsCurrent": "TotalCurrentAssets",
    "Liabilities": "TotalLiabilities",
    "LiabilitiesCurrent": "TotalCurrentLiabilities",
    "StockholdersEquity": "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue": "CashAndEquivalents",
    "LongTermDebt": "TotalDebt",
    "LongTermDebtNoncurrent": "TotalDebt",
    "InventoryNet": "Inventory",
    "AccountsReceivableNetCurrent": "AccountsReceivable",
    # Cash Flow
    "NetCashProvidedByUsedInOperatingActivities": "OperatingCashFlow",
    "PaymentsToAcquirePropertyPlantAndEquipment": "CapitalExpenditures",
}


def get_company_facts(cik):
    """Fetch XBRL companyfacts from EDGAR."""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.json()


def extract_facts(facts_json, ticker, company_name):
    """Extract relevant financial facts from the XBRL JSON."""
    rows = []
    us_gaap = facts_json.get("facts", {}).get("us-gaap", {})

    for xbrl_tag, metric_name in XBRL_TAGS.items():
        tag_data = us_gaap.get(xbrl_tag, {})
        units = tag_data.get("units", {})

        # Financial values are in USD; per-share values in USD/shares
        for unit_key in ["USD", "USD/shares"]:
            entries = units.get(unit_key, [])
            for entry in entries:
                form = entry.get("form", "")
                if form != "10-K":
                    continue

                fiscal_year = entry.get("fy")
                fiscal_period = entry.get("fp", "")
                value = entry.get("val")
                period_end = entry.get("end")
                filed = entry.get("filed")

                if value is None or fiscal_year is None:
                    continue

                rows.append(
                    (
                        ticker,
                        company_name,
                        metric_name,
                        float(value),
                        fiscal_year,
                        fiscal_period,
                        period_end,
                        filed,
                    )
                )
    return rows


def materialize(context):
    con = connect_db()
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")

    con.execute("""
        CREATE TABLE IF NOT EXISTS raw.financial_statements (
            ticker VARCHAR,
            company_name VARCHAR,
            metric_name VARCHAR,
            value DOUBLE,
            fiscal_year INTEGER,
            fiscal_period VARCHAR,
            period_end DATE,
            filing_date DATE
        )
    """)
    con.execute("DELETE FROM raw.financial_statements")

    # Read filings to get CIK + company names
    filings = con.execute("""
        SELECT DISTINCT ticker, company_name, cik
        FROM raw.sec_filings
    """).fetchall()

    all_rows = []
    for ticker, company_name, cik in filings:
        print(f"Fetching XBRL facts for {ticker} (CIK: {cik})...")
        try:
            facts = get_company_facts(cik)
            rows = extract_facts(facts, ticker, company_name)
            all_rows.extend(rows)
            print(f"  Extracted {len(rows)} fact entries")
        except Exception as e:
            print(f"  Error fetching facts for {ticker}: {e}")
        time.sleep(0.15)

    if all_rows:
        con.executemany(
            "INSERT INTO raw.financial_statements VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            all_rows,
        )

    count = con.execute("SELECT count(*) FROM raw.financial_statements").fetchone()[0]
    print(f"Loaded {count} financial statement records")
    con.close()


if __name__ == "__main__":
    materialize(None)
