# 10-K Analyzer — Bruin + DuckDB Capstone

## What This Is
An end-to-end data pipeline that ingests SEC 10-K filings, extracts financial data via XBRL, computes accounting ratios, tracks YoY trends, and runs sentiment analysis on filing text. A Streamlit dashboard (`dashboard.py`) sits on top for exploration, ticker search, peer discovery, and in-browser pipeline runs. Built with [Bruin](https://github.com/bruin-data/bruin) and DuckDB.

## Project Layout
```
Dockerfile                              # Python 3.10 + Poetry + Bruin CLI image
docker-compose.yml                      # dashboard + pipeline + seed-universe services
.bruin.yml                              # Bruin project config (DuckDB connection)
pipeline.yml                            # Pipeline definition (schedule, connections)
pyproject.toml / poetry.lock            # Pinned Python deps
dashboard.py                            # Streamlit UI
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
    business_embeddings.py              # analytics.business_embeddings — MiniLM vectors of Item 1 for ingested tickers
scripts/
  seed_universe_embeddings.py           # One-shot seeder for analytics.sec_universe_embeddings
                                        # (S&P 500 / NASDAQ-100 / combined via --index)
```

## DAG / Dependency Order
```
config.selected_tickers  (written by dashboard; seeded with defaults on first run)
  │
  ▼
sec_filings
  ├── financial_statements → financial_metrics → financial_ratios
  │                                            → yoy_trends
  └── filing_text_sections ──────────────────→ text_sentiment
                                             └─► business_embeddings
```

Out-of-band (not part of `bruin run .`):
`scripts/seed_universe_embeddings.py` → `analytics.sec_universe_embeddings`
(triggered from the dashboard's "Peer universe" expander or CLI)

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
- Sidebar multiselect is backed by SEC `company_tickers.json` — any SEC-listed company is selectable. Already-ingested tickers are pre-selected.
- **Pick by category** expander offers curated industry buckets (Big Tech, Banking, Defense & Aerospace, Pharma, Energy, Retail, Automotive, Semiconductors, Cloud & SaaS, Streaming, Airlines, Telecom, Consumer Staples, Insurance, Payments & Fintech) — selecting a category appends its tickers to the selection. Defined in `CATEGORIES` at the top of `dashboard.py`.
- **Generate Report** button sits directly under the Companies multiselect (always visible, disabled until at least one ticker is picked). Writes the current selection to `config.selected_tickers` in DuckDB and runs `bruin run .` via `subprocess.Popen`, streaming stdout into an `st.status` loader. Clears the Streamlit cache and reruns on success. Falls back to plain `bruin` when Poetry isn't on PATH (inside the Docker image).
- **Similar companies** has two tabs:
  - *By business description*: cosine similarity over MiniLM embeddings of each 10-K's Item 1. The top-N slider (10–100, default 25) is important — cosine similarity is symmetric but top-N cutoffs aren't, so widening the list surfaces peers that sit just outside a dense cluster.
  - *By SIC code*: uses SEC submissions API, persists to `.sic_cache.json`. "Discover more peers" ranks the full SEC universe via company-name token overlap (stopwords stripped) and fetches SIC for the top ~40.
- **Peer universe** expander kicks off `scripts/seed_universe_embeddings.py` from the UI: pick an index (S&P 500 / NASDAQ-100 / combined ~570) and a scope (20 / 100 / full). Progress is streamed from `[i/N]` lines in the script's stdout. The combined index is what catches tech names like SNOW/MDB that aren't in the S&P 500.
- **Top navbar** — the page body is organized into `st.tabs([...])`: Overview, Deep Dive, Performance, Solvency, Sentiment, Data. Streamlit highlights the active tab automatically. Sidebar controls (ticker picker, Generate Report, peer universe) live outside the tabs and apply to all.
- **Company Deep Dive** — a dedicated single-company section (dropdown selects one of the currently selected/ingested tickers). Renders: FY snapshot KPIs (revenue / NI / FCF / ROE / current ratio), Piotroski-inspired 8-point fundamental health scorecard (pass/fail table + total score + Strong/Mixed/Weak grade), income-statement waterfall + common-size table, balance-sheet composition bar (current vs non-current), cash-flow quality (OCF vs NI bar + OCF/NI ratio + FCF margin), working-capital efficiency (DSO / DIO / asset turnover), and a rule-based red-flag checklist. Built straight off `staging.financial_metrics` and `analytics.financial_ratios`.
- Charts use `st.caption()` explainers so the dashboard tells a story rather than being a wall of numbers.
- `clean_df()` strips inf/NaN; `clip_outliers()` winsorizes ratios/growth at 1–99% so a single anomalous filing can't squash an axis.

## Conventions
- **Bruin asset headers**: Every `.sql` and `.py` file has a `@bruin` block defining name, type, materialization, dependencies, and column checks.
- **SQL style**: CTEs over subqueries. NULLIF for safe division. ROUND to 4 decimal places for ratios.
- **Python assets**: Use `materialize(context)` entry point. Access DuckDB via `context.duckdb`. CREATE TABLE IF NOT EXISTS + DELETE for idempotency.
- **SEC rate limit**: 10 requests/sec max. All ingest scripts and dashboard peer lookups use `time.sleep(0.15)` between requests.
- **Tickers**: Stored in the `config.selected_tickers` DuckDB table, managed entirely by the dashboard sidebar. Starts empty — no default seeding. Both Generate Report and Clear all tickers write to the table so the selection persists across reloads exactly as the user left it.
- **Ingest diagnostics**: `sec_filings.py` writes per-ticker status to `config.ingest_status` (`ingested`, `unknown_ticker`, `not_10k_filer`, `no_10k`, `no_filings`, `error`) with a human-readable message. Example: `SVRE` (SaverOne 2014) files 20-F under IFRS, not 10-K — it gets `not_10k_filer` with a clear explanation. The dashboard reads this table to surface per-ticker warnings instead of a generic "no data" message.
- **Dependency pinning**: All Python deps are pinned to exact versions in `pyproject.toml` (no carets). Bruin CLI and Poetry versions are pinned in the `Dockerfile`. Update pins deliberately — don't let them float.

## Troubleshooting
- If `bruin run` fails on Python assets, ensure `duckdb` and `requests` are installed in the Python environment Bruin uses.
- SEC EDGAR may rate-limit aggressively. If you see 403 errors, wait a minute and retry.
- The `User-Agent` header in ingest scripts must identify your application per SEC policy. Set `SEC_USER_AGENT="YourApp your-email@example.com"` in `.env` (or export it before running locally). Falls back to a generic placeholder if unset — don't rely on the placeholder for real ingestion, SEC may block it.
- **`IsADirectoryError: .sic_cache.json`**: a prior docker-compose config bind-mounted this file before it existed, causing Docker to create it as an empty directory. Fix: `docker compose down && rm -rf .sic_cache.json && docker compose up`. Current `docker-compose.yml` bind-mounts the whole project directory instead to avoid this class of bug.
- **`duckdb.IOException: Conflicting lock is held`**: another process (usually a leftover bruin subprocess from a prior pipeline run) holds `ten_k.db`. `write_selected_tickers()` in `dashboard.py` retries for ~30s via `_connect_writable()`. If it still fails, find the offender with `ps aux | grep python` and kill it.
