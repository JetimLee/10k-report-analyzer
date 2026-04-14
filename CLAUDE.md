# 10-K Analyzer — Bruin + DuckDB Capstone

## What This Is
An end-to-end data pipeline that ingests SEC 10-K filings, extracts financial data via XBRL, computes accounting ratios, tracks YoY trends, and runs sentiment analysis on filing text. A Streamlit dashboard (`dashboard.py`) sits on top for exploration, ticker search, peer discovery, and in-browser pipeline runs. Built with [Bruin](https://github.com/bruin-data/bruin) and DuckDB.

## Project Layout
```
Dockerfile                              # Python 3.10 + Poetry + Bruin CLI image
docker-compose.yml                      # dashboard service + one-shot pipeline service
.bruin.yml                              # Bruin project config (DuckDB connection)
pipeline.yml                            # Pipeline definition (schedule, connections)
pyproject.toml / poetry.lock            # Pinned Python deps
dashboard.py                            # Streamlit UI
tickers.csv                             # Companies to ingest
.sic_cache.json                         # Disk cache of ticker→SIC lookups (gitignored)
assets/
  ingest/
    sec_filings.py                      # raw.sec_filings — filing metadata from EDGAR
    financial_statements.py             # raw.financial_statements — XBRL facts
    filing_text_sections.py             # raw.filing_text_sections — MD&A + Risk Factors text
  staging/
    financial_metrics.sql               # staging.financial_metrics — dedupe + pivot to wide format
  analytics/
    financial_ratios.sql                # analytics.financial_ratios — profitability, liquidity, leverage
    yoy_trends.sql                      # analytics.yoy_trends — growth rates, margin deltas, DuPont
    text_sentiment.py                   # analytics.text_sentiment — Loughran-McDonald scoring
```

## DAG / Dependency Order
```
sec_filings
  ├── financial_statements → financial_metrics → financial_ratios
  │                                            → yoy_trends
  └── filing_text_sections ──────────────────→ text_sentiment
```

## How to Run

### Prerequisites (local)
```bash
brew install bruin-data/tap/bruin       # Bruin CLI (v0.11.528 is the validated version)
poetry install                          # Python deps (creates .venv automatically)
```

### Docker (preferred for end-to-end reproducibility)
```bash
docker compose up --build               # dashboard on http://localhost:8501
docker compose --profile run run --rm pipeline   # one-shot `bruin run .`
```
The compose file bind-mounts the project root so the DuckDB file, SIC cache, and tickers list all persist on the host.

### Run the full pipeline locally
```bash
poetry run bruin run .
```

### Run a single asset
```bash
poetry run bruin run assets/ingest/sec_filings.py
poetry run bruin run assets/staging/financial_metrics.sql
poetry run bruin run assets/analytics/financial_ratios.sql
```

### Launch the dashboard locally
```bash
poetry run streamlit run dashboard.py
```

### Validate quality checks
```bash
poetry run bruin validate .
```

### Lint
```bash
poetry run ruff check .
poetry run ruff format --check .
```

### Inspect the database manually
```bash
poetry run python -c "
import duckdb
con = duckdb.connect('ten_k.db')
for t in con.execute(\"SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema != 'information_schema'\").fetchall():
    schema, name = t
    count = con.execute(f'SELECT count(*) FROM {schema}.{name}').fetchone()[0]
    print(f'{schema}.{name}: {count} rows')
con.close()
"
```

## Dashboard (`dashboard.py`)
- Sidebar multiselect is backed by SEC `company_tickers.json` — any SEC-listed company is selectable, not just those in `tickers.csv`. Already-ingested tickers are pre-selected.
- **Generate Report** button writes the current selection to `tickers.csv` and runs `bruin run .` via `subprocess.Popen`, streaming stdout into an `st.status` loader. Clears the Streamlit cache and reruns on success. Falls back to plain `bruin` when Poetry isn't on PATH (inside the Docker image).
- **Similar companies** sidebar section uses the SEC submissions API to fetch each ticker's SIC code and suggests peers that share it. Results are persisted to `.sic_cache.json` so a given CIK is only fetched once per machine. A "Discover more peers" button seeds the cache by ranking the full SEC universe via company-name token overlap (stopwords stripped) and fetching SIC for the top ~40.
- Charts use `st.caption()` explainers so the dashboard tells a story rather than being a wall of numbers.
- `clean_df()` strips inf/NaN; `clip_outliers()` winsorizes ratios/growth at 1–99% so a single anomalous filing can't squash an axis.

## Conventions
- **Bruin asset headers**: Every `.sql` and `.py` file has a `@bruin` block defining name, type, materialization, dependencies, and column checks.
- **SQL style**: CTEs over subqueries. NULLIF for safe division. ROUND to 4 decimal places for ratios.
- **Python assets**: Use `materialize(context)` entry point. Access DuckDB via `context.duckdb`. CREATE TABLE IF NOT EXISTS + DELETE for idempotency.
- **SEC rate limit**: 10 requests/sec max. All ingest scripts and dashboard peer lookups use `time.sleep(0.15)` between requests.
- **Tickers**: Configured in `tickers.csv` at the project root (one ticker per line with a `ticker` header). Default: AAPL, MSFT, GOOGL, AMZN, META.
- **Dependency pinning**: All Python deps are pinned to exact versions in `pyproject.toml` (no carets). Bruin CLI and Poetry versions are pinned in the `Dockerfile`. Update pins deliberately — don't let them float.

## Troubleshooting
- If `bruin run` fails on Python assets, ensure `duckdb` and `requests` are installed in the Python environment Bruin uses.
- SEC EDGAR may rate-limit aggressively. If you see 403 errors, wait a minute and retry.
- The `User-Agent` header in ingest scripts must identify your application per SEC policy.
- **`IsADirectoryError: .sic_cache.json`**: a prior docker-compose config bind-mounted this file before it existed, causing Docker to create it as an empty directory. Fix: `docker compose down && rm -rf .sic_cache.json && docker compose up`. Current `docker-compose.yml` bind-mounts the whole project directory instead to avoid this class of bug.
