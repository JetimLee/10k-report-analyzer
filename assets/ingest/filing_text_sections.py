"""@bruin
name: raw.filing_text_sections
type: python
depends:
  - raw.sec_filings
  - analytics.financial_ratios
  - analytics.yoy_trends
columns:
  - name: ticker
    type: string
    checks:
      - name: not_null
  - name: section_type
    type: string
    checks:
      - name: not_null
      - name: accepted_values
        value:
          - "MDA"
          - "RISK_FACTORS"
          - "BUSINESS"
@bruin"""

import os
import duckdb
import re
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

# Section headers for 10-K extraction
SECTION_PATTERNS = {
    "BUSINESS": [
        r"Item\s+1[\.\s\-\u2013\u2014]+Business",
    ],
    "MDA": [
        r"Item\s+7[^A][\.\s\-\u2013\u2014]*Management.{0,30}Discussion",
    ],
    "RISK_FACTORS": [
        r"Item\s+1A[\.\s\-\u2013\u2014]*Risk\s+Factors",
    ],
}

# Patterns indicating the start of the next section (to know where to stop)
NEXT_SECTION = [
    r"Item\s+(?:1A|1B|1C|2|7A|8)[\.\s\-\u2013\u2014]",
]

# Words that, when they precede an "Item X" token, indicate a cross-reference
# (e.g. "See Item 1A of Part I Risk Factors") rather than a section boundary.
CROSS_REF_PREFIXES = re.compile(
    r"(?:see|per|under|in|to|of|from|pursuant to|refer to|included in|contained in)\s*$",
    re.IGNORECASE,
)


def is_cross_reference(text, match_start, window=30):
    """True if the match is preceded by a cross-reference word like 'See'."""
    before = text[max(0, match_start - window) : match_start]
    return bool(CROSS_REF_PREFIXES.search(before))


def clean_html(text):
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_section(full_text, section_type):
    """Extract a section from the filing text.

    Cleans HTML first so that iXBRL tags don't fragment section headers.
    Uses the last match of the section header to skip Table of Contents entries.
    """
    cleaned = clean_html(full_text)
    start_patterns = SECTION_PATTERNS.get(section_type, [])

    # Collect all matches; the last one is the actual section (not the TOC entry)
    all_matches = []
    for pattern in start_patterns:
        all_matches.extend(re.finditer(pattern, cleaned, re.IGNORECASE))

    if not all_matches:
        return None

    # Sort by position, try from last to first (actual section before TOC)
    all_matches.sort(key=lambda m: m.start(), reverse=True)

    for start_match in all_matches:
        remaining = cleaned[start_match.end() :]

        # Find the first NEXT_SECTION match that is NOT a cross-reference.
        end_match = None
        for pattern in NEXT_SECTION:
            for candidate in re.finditer(pattern, remaining, re.IGNORECASE):
                if is_cross_reference(remaining, candidate.start()):
                    continue
                if end_match is None or candidate.start() < end_match.start():
                    end_match = candidate
                break  # first real boundary wins for this pattern

        if end_match:
            section_text = remaining[: end_match.start()].strip()
        else:
            section_text = remaining[:50000].strip()

        if len(section_text) > 200:
            return section_text[:50000]

    return None


def materialize(context):
    con = connect_db()
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")

    con.execute("""
        CREATE TABLE IF NOT EXISTS raw.filing_text_sections (
            ticker VARCHAR,
            company_name VARCHAR,
            fiscal_year INTEGER,
            accession_number VARCHAR,
            section_type VARCHAR,
            section_text VARCHAR,
            text_length INTEGER
        )
    """)
    con.execute("DELETE FROM raw.filing_text_sections")

    # Get the most recent 3 filings per ticker to keep runtime reasonable
    filings = con.execute("""
        SELECT ticker, company_name, accession_number, filing_url,
               YEAR(report_date) AS fiscal_year
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY ticker ORDER BY filing_date DESC
            ) AS rn
            FROM raw.sec_filings
        )
        WHERE rn <= 3
        ORDER BY ticker, fiscal_year DESC
    """).fetchall()

    rows = []
    for ticker, company_name, accession, filing_url, fiscal_year in filings:
        print(f"Fetching filing text for {ticker} FY{fiscal_year}...")
        try:
            resp = requests.get(filing_url, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            full_text = resp.text

            for section_type in ["BUSINESS", "MDA", "RISK_FACTORS"]:
                section_text = extract_section(full_text, section_type)
                if section_text and len(section_text) > 100:
                    rows.append(
                        (
                            ticker,
                            company_name,
                            fiscal_year,
                            accession,
                            section_type,
                            section_text,
                            len(section_text),
                        )
                    )
                    print(f"  {section_type}: {len(section_text)} chars")
                else:
                    print(f"  {section_type}: not found or too short")
        except Exception as e:
            print(f"  Error: {e}")

        time.sleep(0.2)

    if rows:
        con.executemany(
            "INSERT INTO raw.filing_text_sections VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    count = con.execute("SELECT count(*) FROM raw.filing_text_sections").fetchone()[0]
    print(f"Loaded {count} text section records")
    con.close()


if __name__ == "__main__":
    materialize(None)
