# 10-K Analyzer — Data Engineering Capstone

An end-to-end data pipeline built with **Bruin** and **DuckDB** that ingests SEC 10-K filings, extracts financial data via XBRL, computes key accounting ratios, tracks year-over-year trends, and performs text sentiment analysis on risk factors and MD&A sections. A Streamlit dashboard lets you explore the data, search the full SEC ticker universe, suggest industry peers, and trigger pipeline runs from the browser.

## Pipeline Architecture

```
SEC EDGAR API
      │
      ▼
┌─────────────────────── INGEST (Python) ───────────────────────┐
│  sec_filings          → Filing metadata (CIK, dates, URLs)    │
│  financial_statements → XBRL fact data (IS, BS, CF line items)│
│  filing_text_sections → MD&A and Risk Factors raw text        │
└───────────────────────────────┬────────────────────────────────┘
                                ▼
┌─────────────────────── STAGING (SQL) ─────────────────────────┐
│  financial_metrics    → Deduped, pivoted: 1 row per co/year   │
│                         with quality checks                    │
└───────────────────────────────┬────────────────────────────────┘
                                ▼
┌─────────────────────── ANALYTICS (SQL + Python) ──────────────┐
│  financial_ratios     → Profitability, liquidity, leverage     │
│  yoy_trends           → YoY growth, margin deltas, DuPont ROE │
│  text_sentiment       → Loughran-McDonald sentiment scoring    │
└───────────────────────────────────────────────────────────────-┘
                                │
                                ▼
                          DuckDB (ten_k.db)
                                │
                                ▼
                     Streamlit Dashboard (dashboard.py)
```

## Key Metrics Computed

| Category | Metrics |
|---|---|
| **Profitability** | Gross margin, operating margin, net margin, ROE, ROA |
| **Liquidity** | Current ratio, quick ratio, cash position |
| **Leverage** | Debt-to-equity, debt-to-assets, LT debt/equity |
| **Efficiency** | Asset turnover, DSO, DIO |
| **Cash Flow** | FCF, operating CF / net income |
| **Trends** | YoY revenue growth, margin deltas, DuPont decomposition |
| **Text** | Sentiment score, risk theme detection (cyber, climate, etc.) |

## Quick Start — Docker (recommended)

Everything — Python, Poetry, Bruin CLI, and pinned dependencies — is baked into the image.

```bash
# Build once, then start the dashboard at http://localhost:8501
docker compose up --build

# Run the Bruin pipeline as a one-shot container
docker compose --profile run run --rm pipeline
```

The compose file bind-mounts the project directory, so `ten_k.db`, `.sic_cache.json`, and `tickers.csv` all persist on the host between runs.

## Quick Start — Local (Poetry)

```bash
# Install Bruin CLI
brew install bruin-data/tap/bruin   # macOS
# or: curl -sSL https://raw.githubusercontent.com/bruin-data/bruin/main/install.sh | bash

# Install Python dependencies (exact pinned versions)
poetry install

# Run the full pipeline
poetry run bruin run .

# Run a single asset
poetry run bruin run assets/analytics/financial_ratios.sql

# Validate quality checks
poetry run bruin validate .

# Launch the dashboard
poetry run streamlit run dashboard.py
```

## Dashboard Features

- **Full SEC ticker search** — the sidebar multiselect is backed by `company_tickers.json`, so you can pick any SEC-listed company, not just the five in `tickers.csv`.
- **Generate Report** — clicking the button writes the current selection to `tickers.csv` and runs `bruin run .` in-process, streaming logs into the UI.
- **Similar companies** — given an anchor ticker, the sidebar fetches its SIC code from the SEC submissions API and suggests peers that share it. A "Discover more peers" button expands the search by name-similarity and caches results to `.sic_cache.json` so subsequent lookups are instant.
- **Chart captions** — every section has a short explainer so the dashboard reads as a narrative, not just a wall of charts.
- **Cleaned data** — queries filter nulls, drop inf/NaN, and winsorize ratios/growth at the 1–99% tails so a single bad filing can't distort an axis.

## Configuration

- **Tickers** — edit `tickers.csv` at the project root (one ticker per line with a `ticker` header) or manage them from the dashboard sidebar.
- **SEC User-Agent** — set in the ingest scripts (SEC requires an identifying string).
- **Bruin connections** — see `.bruin.yml`.

## Pinned Versions

Dependencies are pinned to the exact versions validated during development:

| Package | Version |
|---|---|
| Python | 3.10.15 |
| duckdb | 1.5.2 |
| requests | 2.33.1 |
| streamlit | 1.56.0 |
| plotly | 6.7.0 |
| pandas | 2.3.3 |
| numpy | 2.2.6 |
| ruff | 0.11.13 |
| Bruin CLI | v0.11.528 |
| Poetry | 1.8.3 |

## Project Structure

```
ten-k-analyzer/
├── Dockerfile                              # Python 3.10 + Poetry + Bruin
├── docker-compose.yml                      # dashboard + one-shot pipeline services
├── .bruin.yml                              # Bruin project config
├── pipeline.yml                            # Pipeline definition
├── pyproject.toml                          # Pinned Python deps
├── poetry.lock
├── dashboard.py                            # Streamlit app
├── tickers.csv                             # Companies to ingest
├── assets/
│   ├── ingest/
│   │   ├── sec_filings.py                  # Pull filing metadata from EDGAR
│   │   ├── financial_statements.py         # Extract XBRL financial data
│   │   └── filing_text_sections.py         # Extract MD&A + Risk Factors text
│   ├── staging/
│   │   └── financial_metrics.sql           # Clean, dedupe, pivot to wide format
│   └── analytics/
│       ├── financial_ratios.sql            # Compute all financial ratios
│       ├── yoy_trends.sql                  # Year-over-year trend analysis
│       └── text_sentiment.py               # Loughran-McDonald sentiment scoring
└── README.md
```

## Troubleshooting

- **`IsADirectoryError: .sic_cache.json`** — an older compose config bind-mounted this file before it existed, so Docker created it as a directory. Fix: `docker compose down && rm -rf .sic_cache.json && docker compose up`.
- **SEC 403 errors** — rate limiting. Wait a minute and retry; all ingest scripts already sleep 0.15s between requests.
- **Port 8501 in use** — another Streamlit is already running. Stop it, or edit the port mapping in `docker-compose.yml`.
