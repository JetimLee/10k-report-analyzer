# 10-K Analyzer — Data Engineering Capstone

An end-to-end data platform that turns raw SEC 10-K filings into decision-ready financial analysis. Built on **Bruin** (orchestration + data quality), **DuckDB** (analytical store), **Streamlit** (exploration UI), and **sentence-transformers** (peer discovery).

## Business Problems It Solves

Financial analysts, investors, and equity researchers spend enormous amounts of time doing the same manual work over and over: pulling filings from EDGAR, reconciling XBRL tags across companies, recomputing ratios in spreadsheets, and searching Google to figure out who a given company's competitors actually are. This platform automates the drudgery:

| Problem | How the platform solves it |
|---|---|
| **"How is this company actually performing?"** — The 10-K contains the answer but is 200+ pages of prose and tables. | Automated XBRL extraction + ratio computation surfaces profitability, liquidity, leverage, and cash-flow quality on a single screen. |
| **"How has performance changed over time?"** — Point-in-time metrics hide the trajectory. | YoY trend analysis + DuPont ROE decomposition reveals *why* returns are moving: margin, efficiency, or leverage. |
| **"Is management quietly signaling trouble?"** — Tone often shifts in Risk Factors and MD&A before the numbers do. | Loughran-McDonald finance-specific sentiment scoring on each filing's narrative sections, with risk-theme tagging (cyber, climate, supply chain, etc.). |
| **"Who should I benchmark this company against?"** — SIC codes are a 1980s taxonomy that group Palantir with 800 unrelated software shops. | MiniLM sentence embeddings on each 10-K's Item 1 (Business) section; cosine similarity surfaces *actual* business-model peers. |
| **"How do I onboard a new company into analysis?"** — Traditionally this takes hours of manual data entry. | One click in the dashboard adds any SEC-listed ticker to the pipeline and ingests its full filing history. |
| **"Can non-engineers run this?"** — Most data pipelines require a SQL IDE and a terminal. | Streamlit dashboard lets stakeholders kick off ingestion, seed peer universes, and browse results without touching code. |

## Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│                            EXTERNAL SOURCES                               │
│   SEC EDGAR (filings, XBRL, submissions API)     Wikipedia (S&P 500)      │
└──────────────────────────────┬────────────────────────────────────────────┘
                               │ HTTP (rate-limited 10 req/s)
                               ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                           BRUIN PIPELINE                                  │
│                                                                           │
│  ┌────────────────── INGEST (Python) ──────────────────┐                  │
│  │ raw.sec_filings          Filing metadata            │                  │
│  │ raw.financial_statements XBRL facts (IS/BS/CF)      │                  │
│  │ raw.filing_text_sections Item 1 / MD&A / Risk Facts │                  │
│  └───────────────────────┬──────────────────────────────┘                 │
│                          ▼                                                │
│  ┌────────────────── STAGING (SQL) ─────────────────────┐                 │
│  │ staging.financial_metrics  Dedupe + pivot → wide     │                 │
│  │                            1 row per company-year    │                 │
│  └───────────────────────┬──────────────────────────────┘                 │
│                          ▼                                                │
│  ┌────────────────── ANALYTICS (SQL + Python) ─────────────────────┐      │
│  │ analytics.financial_ratios     Profitability, liquidity, lev.   │      │
│  │ analytics.yoy_trends           YoY growth + DuPont ROE          │      │
│  │ analytics.text_sentiment       Loughran-McDonald scoring        │      │
│  │ analytics.business_embeddings  MiniLM vectors of Item 1         │      │
│  └───────────────────────┬──────────────────────────────────────────┘     │
│                          ▼                                                │
│                  Data-quality checks                                      │
│                  (not_null, unique, accepted_values, custom)              │
└──────────────────────────┬────────────────────────────────────────────────┘
                           ▼
              ┌───────────────────────────┐
              │   DuckDB (ten_k.db)       │
              │   columnar, file-backed   │
              └──────────────┬────────────┘
                             │
      ┌──────────────────────┼──────────────────────────┐
      ▼                      ▼                          ▼
┌───────────────┐   ┌───────────────────┐    ┌────────────────────────────┐
│  STREAMLIT    │   │ SEED SCRIPT       │    │  analytics.sec_universe    │
│  DASHBOARD    │◄──┤ scripts/seed_     │───►│  _embeddings (500+ pre-    │
│  (dashboard.py)│  │ universe_embed..  │    │  computed S&P 500 peers)   │
│               │   │ MiniLM embeddings │    └────────────────────────────┘
│ • Charts      │   └───────────────────┘
│ • Peer search │
│ • Kick off    │   All services run in Docker (docker-compose.yml):
│   pipeline    │     - dashboard (Streamlit, port 8501)
│ • Kick off    │     - pipeline  (one-shot bruin run .)
│   seed        │     - seed-universe (one-shot embedding seeder)
└───────────────┘
```

### Design choices

- **DuckDB over Postgres/Snowflake** — this is single-machine analytical workload on < 10M rows. Columnar + embedded = zero infra cost, SQL joins across all schemas, ship the DB file with the repo for demos.
- **Bruin over Airflow/Dagster** — quality checks (`not_null`, `unique`, `accepted_values`, custom SQL) are first-class in asset definitions, not a separate framework. DAG dependencies are declared inline.
- **MiniLM for peer discovery** — 90MB, CPU-friendly, sufficient for short business-description similarity. No GPU, no external API.
- **Sentence embeddings, not just SIC** — industry codes fail for modern, multi-segment businesses. Embedding the actual prose of Item 1 captures business-model similarity the way a human analyst would.

### Why Item 1 embeddings — and why they're efficient

**What we embed:** every 10-K begins with **Item 1 ("Business")** — 5k–50k words in which management, under legal obligation to be accurate, describes *exactly* what the company does: products, segments, customers, markets, distribution, competition, and strategy. It is the single highest-signal, lowest-noise text in the corpus for modeling business-model similarity.

**What we explicitly *don't* embed:**
- MD&A — too anchored to the current fiscal year's financial performance.
- Risk Factors — legally defensive and highly correlated across all public companies ("cybersecurity", "macroeconomic conditions", etc.).
- Financial statements — captured separately as structured XBRL facts.
- Full 10-K — dilutes the signal; Item 1 is where business-model vocabulary actually lives.

**Extraction pipeline.** HTML is stripped → Item 1 header located → section body captured up to the next *real* section boundary. A cross-reference filter (added to handle Amazon-style filings where management writes "*See Item 1A of Part I Risk Factors*" inside the Business section) prevents in-text citations from truncating the body. The extracted text is clipped to 20,000 characters — MiniLM truncates at 512 tokens (~2k characters) anyway, so most of the 20k acts as a safe margin for where the salient vocabulary sits.

**Why MiniLM (all-MiniLM-L6-v2) is the right tool:**

| Property | Value | Why it matters |
|---|---|---|
| Model size | 22M params, ~90MB on disk | Baked into the Docker image at build time — no runtime download, no HuggingFace dependency at query time. |
| Embedding dim | 384 | 384 × 4 bytes = **1.5KB per company**. 500 companies = 750KB. Fits in DuckDB as a `FLOAT[]` column with no vector-DB overhead. |
| Runtime | ~30ms/doc on CPU | Embedding the full S&P 500 takes ~30 seconds of model time (hours of wall time go to SEC fetches, not compute). |
| Normalization | L2-normalized at ingest | Cosine similarity collapses to a single dot product — rank 500 peers in **~1ms** via one `numpy` matmul. No ANN index, no Qdrant, no FAISS. |
| License + cost | Apache 2.0, local | Zero per-query cost, no API key, works offline. |

**Why this scales.** DuckDB holds all 500 vectors in memory as a dense `(500, 384)` float32 matrix (~750KB). Computing every pairwise similarity for ranking is a single `mat @ anchor` matmul — exact, not approximate, and faster than a round-trip to a vector DB. Scaling to 10k companies would still be a 15MB matrix and a ~5ms matmul — we simply don't need approximate-nearest-neighbor infrastructure at this size.

**Why the one-time seed is actually one-time.** Companies file 10-Ks once per fiscal year. The seed script is idempotent — it skips tickers whose stored `fiscal_year` already matches the latest filing — so re-running it after a company's annual filing becomes a small delta job, not a full re-seed.

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

# Seed peer-suggestion embeddings for ~500 S&P 500 companies (~1 hour, one-time)
docker compose --profile seed run --rm seed-universe

# Or for a quick smoke test
docker compose --profile seed run --rm seed-universe python scripts/seed_universe_embeddings.py --limit 20
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

## Dashboard Features (for non-technical stakeholders)

- **Full SEC ticker search** — the sidebar multiselect is backed by `company_tickers.json`, so any SEC-listed company can be added — not just the five shipped in `tickers.csv`.
- **Generate Report** — writes the current ticker selection to `tickers.csv` and runs the full Bruin pipeline in-process. Shows a progress bar + current-asset label; full log available behind a Developer-mode toggle.
- **Peer universe seeding** — a sidebar panel lets stakeholders kick off a one-time embedding seed for 20 / 100 / ~500 S&P 500 companies, with live ETA and per-ticker progress. No CLI needed.
- **Similar companies** — two peer-discovery modes:
  - *By business description* (primary): MiniLM sentence embeddings of each 10-K Item 1, ranked by cosine similarity. Pulls from both user-ingested tickers and the precomputed universe.
  - *By SIC code* (fallback): coarse industry filter; useful for discovering small-cap tickers outside the embedding universe.
- **Story-driven charts** — every section has a short caption explaining what to look for (e.g. "Divergence between revenue and net-income lines usually flags margin pressure or one-time charges").
- **Cleaned data** — queries filter nulls, drop inf/NaN, and winsorize ratios/growth at the 1–99% tails so a single bad filing can't distort an axis.

## Configuration

- **Tickers** — edit `tickers.csv` at the project root (one ticker per line with a `ticker` header) or manage them from the dashboard sidebar.
- **SEC User-Agent** — set via the `SEC_USER_AGENT` env var (SEC requires a header identifying your app + contact email). Copy `.env.example` to `.env` and fill it in; `docker compose` picks it up automatically.
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
