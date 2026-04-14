import csv
import json
import os
import re
import subprocess
import time

import duckdb
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

DB_PATH = "ten_k.db"
TICKERS_CSV = "tickers.csv"
SIC_CACHE_PATH = ".sic_cache.json"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_HEADERS = {
    "User-Agent": "10KAnalyzer gavincoulson1@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}
STOPWORDS = {
    "inc", "corp", "corporation", "company", "co", "ltd", "llc", "holdings",
    "group", "plc", "the", "and", "of", "international", "industries", "systems",
    "technologies", "technology", "services", "solutions", "global", "new",
    "class", "common", "stock", "a", "b", "c",
}

st.set_page_config(page_title="10-K Analyzer", layout="wide")
st.title("10-K Financial Analyzer")


def query(sql):
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute(sql).df()
    con.close()
    return clean_df(df)


def clean_df(df):
    """Replace inf with NaN and coerce numerics so charts don't blow up."""
    if df.empty:
        return df
    numeric_cols = df.select_dtypes(include=[np.number]).columns
    if len(numeric_cols):
        df[numeric_cols] = df[numeric_cols].replace([np.inf, -np.inf], np.nan)
    return df


def clip_outliers(df, col, lo=0.01, hi=0.99):
    """Winsorize a column so a single bad row doesn't squash the chart's y-axis."""
    if df.empty or col not in df.columns or df[col].dropna().empty:
        return df
    q_lo, q_hi = df[col].quantile([lo, hi])
    out = df.copy()
    out[col] = out[col].clip(q_lo, q_hi)
    return out


@st.cache_data(ttl=3600)
def fetch_sec_tickers():
    """Fetch the full SEC ticker/CIK map."""
    resp = requests.get(SEC_TICKERS_URL, headers=SEC_HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    rows = [
        {
            "ticker": e["ticker"].upper(),
            "company_name": e["title"],
            "cik": str(e["cik_str"]).zfill(10),
        }
        for e in data.values()
    ]
    return pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)


def load_sic_cache():
    if os.path.exists(SIC_CACHE_PATH):
        try:
            with open(SIC_CACHE_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_sic_cache(cache):
    with open(SIC_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def fetch_sic(cik):
    """Fetch SIC code + description for a CIK. Caller handles rate limiting."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = requests.get(url, headers=SEC_HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {
        "sic": str(data.get("sic") or ""),
        "sic_description": data.get("sicDescription") or "",
    }


def get_sic_bulk(tickers, cik_lookup, cache, status_cb=None):
    """Fetch SIC for a list of tickers, writing through the cache. Returns updated cache."""
    new = 0
    for t in tickers:
        if t in cache:
            continue
        cik = cik_lookup.get(t)
        if not cik:
            cache[t] = {"sic": "", "sic_description": ""}
            continue
        try:
            cache[t] = fetch_sic(cik)
            new += 1
            if status_cb:
                status_cb(t, cache[t])
            time.sleep(0.15)  # SEC 10 req/sec cap
        except Exception as e:
            if status_cb:
                status_cb(t, {"error": str(e)})
    if new:
        save_sic_cache(cache)
    return cache


def name_tokens(name):
    toks = re.findall(r"[A-Za-z]+", (name or "").lower())
    return {t for t in toks if t not in STOPWORDS and len(t) > 2}


def rank_name_similar(target_ticker, target_name, sec_df, limit=50):
    """Rank SEC tickers by shared non-stopword tokens with the target company name."""
    target = name_tokens(target_name)
    if not target:
        return []
    scored = []
    for _, row in sec_df.iterrows():
        if row["ticker"] == target_ticker:
            continue
        overlap = len(target & name_tokens(row["company_name"]))
        if overlap > 0:
            scored.append((overlap, row["ticker"]))
    scored.sort(reverse=True)
    return [t for _, t in scored[:limit]]


def ingested_tickers():
    try:
        return query(
            "SELECT DISTINCT ticker FROM analytics.financial_ratios ORDER BY ticker"
        )["ticker"].tolist()
    except Exception:
        return []


def write_tickers_csv(tickers):
    with open(TICKERS_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker"])
        for t in tickers:
            w.writerow([t])


def run_bruin_pipeline(log_container):
    """Stream `bruin run` output line-by-line into the given container."""
    # In the Docker image poetry isn't on PATH at runtime; fall back to plain `bruin`.
    import shutil
    cmd = (
        ["poetry", "run", "bruin", "run", "."]
        if shutil.which("poetry")
        else ["bruin", "run", "."]
    )
    proc = subprocess.Popen(
        cmd,
        cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines = []
    for line in proc.stdout:
        lines.append(line.rstrip())
        log_container.code("\n".join(lines[-200:]))
    proc.wait()
    return proc.returncode, "\n".join(lines)


# --- Sidebar: ticker picker ---
st.sidebar.header("Companies")

try:
    sec_df = fetch_sec_tickers()
    sec_options = sec_df["ticker"].tolist()
    label_map = dict(zip(sec_df["ticker"], sec_df["company_name"]))
    cik_lookup = dict(zip(sec_df["ticker"], sec_df["cik"]))
except Exception as e:
    st.sidebar.error(f"Couldn't load SEC ticker list: {e}")
    sec_df = pd.DataFrame(columns=["ticker", "company_name", "cik"])
    sec_options = []
    label_map = {}
    cik_lookup = {}

already_ingested = ingested_tickers()

if "selection" not in st.session_state:
    st.session_state.selection = list(already_ingested)

# Pending additions from the peer-suggestion UI show up on the next rerun
for t in st.session_state.pop("_add_to_selection", []):
    if t not in st.session_state.selection:
        st.session_state.selection.append(t)

selected = st.sidebar.multiselect(
    "Select tickers (search any SEC-listed company)",
    options=sec_options or already_ingested,
    key="selection",
    format_func=lambda t: f"{t} — {label_map.get(t, t)}",
)

missing = [t for t in selected if t not in already_ingested]
if missing:
    st.sidebar.warning(
        f"Not yet ingested: {', '.join(missing)}. Click below to fetch from SEC."
    )

# --- Similar-company suggestions ---
sic_cache = load_sic_cache()

if selected and not sec_df.empty:
    st.sidebar.divider()
    st.sidebar.subheader("Similar companies")
    anchor = st.sidebar.selectbox(
        "Find peers of",
        selected,
        format_func=lambda t: f"{t} — {label_map.get(t, t)}",
    )

    # Make sure we know the anchor's SIC, and populate cache for ingested tickers once.
    need_lookup = [t for t in list(dict.fromkeys([anchor, *already_ingested])) if t not in sic_cache]
    if need_lookup:
        with st.sidebar.status(f"Fetching SIC for {len(need_lookup)} tickers…", expanded=False):
            get_sic_bulk(need_lookup, cik_lookup, sic_cache)

    anchor_sic = sic_cache.get(anchor, {}).get("sic", "")
    anchor_desc = sic_cache.get(anchor, {}).get("sic_description", "")

    if anchor_sic:
        st.sidebar.caption(f"SIC **{anchor_sic}** — {anchor_desc}")
        peers = sorted(
            t for t, v in sic_cache.items()
            if v.get("sic") == anchor_sic and t != anchor and t in label_map
        )
        if peers:
            not_yet = [t for t in peers if t not in selected]
            if not_yet:
                to_add = st.sidebar.multiselect(
                    "Peers (same SIC)",
                    options=not_yet,
                    format_func=lambda t: f"{t} — {label_map.get(t, t)}",
                    key="peer_add",
                )
                if st.sidebar.button("Add peers to selection", disabled=not to_add):
                    st.session_state["_add_to_selection"] = to_add
                    st.rerun()
            else:
                st.sidebar.caption("All known peers are already selected.")
        else:
            st.sidebar.caption("No peers cached yet — run Discover below.")

        scan_size = st.sidebar.slider("Scan batch size", 10, 100, 40, step=10)
        if st.sidebar.button("Discover more peers"):
            # Start with name-similar candidates, then top up with any uncached tickers.
            ranked = rank_name_similar(anchor, label_map.get(anchor, ""), sec_df, limit=scan_size)
            uncached = [t for t in ranked if t not in sic_cache]
            if len(uncached) < scan_size:
                extras = [t for t in sec_df["ticker"] if t not in sic_cache and t not in uncached]
                uncached.extend(extras[: scan_size - len(uncached)])
            if uncached:
                with st.sidebar.status(
                    f"Scanning {len(uncached)} candidates for SIC {anchor_sic}…", expanded=True
                ) as s:
                    hits = []

                    def _cb(ticker, info):
                        if info.get("sic") == anchor_sic:
                            hits.append(ticker)
                            s.update(label=f"Found {len(hits)} peer(s), scanning…")

                    get_sic_bulk(uncached, cik_lookup, sic_cache, status_cb=_cb)
                    s.update(label=f"Done — {len(hits)} new peer(s) found", state="complete")
                st.rerun()
            else:
                st.sidebar.info("Entire SEC universe already cached — no candidates left.")
    else:
        st.sidebar.caption("Couldn't determine SIC for this ticker.")

st.sidebar.divider()

if st.sidebar.button("Generate Report", type="primary", disabled=not selected):
    write_tickers_csv(selected)
    with st.status("Running pipeline… (fetching filings, parsing XBRL, scoring text)", expanded=True) as status:
        log_box = st.empty()
        rc, output = run_bruin_pipeline(log_box)
        if rc == 0:
            status.update(label="Pipeline complete", state="complete")
            st.cache_data.clear()
            time.sleep(1)
            st.rerun()
        else:
            status.update(label=f"Pipeline failed (exit {rc})", state="error")

if not selected:
    st.info("Select at least one ticker in the sidebar, then click **Generate Report**.")
    st.stop()

# Only chart tickers that actually have data
available = [t for t in selected if t in already_ingested]
if not available:
    st.warning("None of your selected tickers have been ingested yet. Click **Generate Report**.")
    st.stop()

placeholders = ", ".join(f"'{t}'" for t in available)

# --- Overview ---
st.header("Financial Overview")
st.caption(
    "Top-level snapshot across the companies you selected: how many we're tracking, "
    "the most recent fiscal year on file, and combined revenue for that year."
)

latest = query(f"""
    SELECT ticker, fiscal_year, revenue, net_income,
           gross_margin, operating_margin, net_profit_margin,
           return_on_equity, return_on_assets,
           current_ratio, debt_to_equity, free_cash_flow
    FROM analytics.financial_ratios
    WHERE ticker IN ({placeholders})
      AND fiscal_year IS NOT NULL
    ORDER BY fiscal_year DESC
""")

latest_year = latest.groupby("ticker").first().reset_index()

col1, col2, col3 = st.columns(3)
with col1:
    st.metric("Companies Tracked", len(available))
with col2:
    if not latest_year.empty and latest_year["fiscal_year"].notna().any():
        st.metric("Latest Fiscal Year", int(latest_year["fiscal_year"].max()))
with col3:
    total_rev = latest_year["revenue"].sum(skipna=True)
    if total_rev and total_rev > 0:
        st.metric("Combined Revenue", f"${total_rev / 1e9:.0f}B")

# --- Revenue & Net Income ---
st.header("Revenue & Net Income")
st.caption(
    "The top-line (revenue) tells you scale; the bottom-line (net income) tells you "
    "how much actually makes it through after costs, interest, and taxes. Divergence "
    "between the two lines usually flags margin pressure or one-time charges."
)

rev_data = query(f"""
    SELECT ticker, fiscal_year, revenue, net_income
    FROM analytics.financial_ratios
    WHERE ticker IN ({placeholders})
      AND fiscal_year IS NOT NULL
      AND revenue IS NOT NULL AND revenue > 0
    ORDER BY fiscal_year
""")

col1, col2 = st.columns(2)
with col1:
    fig = px.line(
        rev_data, x="fiscal_year", y="revenue", color="ticker",
        title="Revenue Over Time", labels={"revenue": "Revenue ($)", "fiscal_year": "Year"},
        markers=True,
    )
    st.plotly_chart(fig, use_container_width=True)

with col2:
    fig = px.line(
        rev_data.dropna(subset=["net_income"]),
        x="fiscal_year", y="net_income", color="ticker",
        title="Net Income Over Time", labels={"net_income": "Net Income ($)", "fiscal_year": "Year"},
        markers=True,
    )
    st.plotly_chart(fig, use_container_width=True)

# --- Profitability Margins ---
st.header("Profitability Margins")
st.caption(
    "Margins show how much of each revenue dollar survives at each layer of the income statement — "
    "gross (after COGS), operating (after SG&A/R&D), and net (after everything). "
    "Rising margins = pricing power or operating leverage; falling margins = cost pressure or mix shift."
)

margins = query(f"""
    SELECT ticker, fiscal_year, gross_margin, operating_margin, net_profit_margin
    FROM analytics.financial_ratios
    WHERE ticker IN ({placeholders})
      AND fiscal_year IS NOT NULL
    ORDER BY fiscal_year
""")

margin_type = st.selectbox("Margin", ["operating_margin", "net_profit_margin", "gross_margin"])
margin_plot = clip_outliers(margins.dropna(subset=[margin_type]), margin_type)
fig = px.line(
    margin_plot, x="fiscal_year", y=margin_type, color="ticker",
    title=f"{margin_type.replace('_', ' ').title()} Over Time",
    labels={margin_type: "Ratio", "fiscal_year": "Year"},
    markers=True,
)
fig.update_layout(yaxis_tickformat=".1%")
st.plotly_chart(fig, use_container_width=True)

# --- YoY Growth ---
st.header("Year-over-Year Growth")
st.caption(
    "Growth rates let you compare companies of very different sizes on the same axis. "
    "Watch for revenue growth outpacing net-income growth (margin erosion) or the reverse "
    "(operating leverage kicking in)."
)

yoy = query(f"""
    SELECT ticker, fiscal_year, revenue_growth, net_income_growth, operating_income_growth
    FROM analytics.yoy_trends
    WHERE ticker IN ({placeholders})
      AND fiscal_year IS NOT NULL
    ORDER BY fiscal_year
""")
yoy = clip_outliers(clip_outliers(yoy, "revenue_growth"), "net_income_growth")

col1, col2 = st.columns(2)
with col1:
    fig = px.bar(
        yoy.dropna(subset=["revenue_growth"]),
        x="fiscal_year", y="revenue_growth", color="ticker",
        barmode="group", title="Revenue Growth (YoY)",
        labels={"revenue_growth": "Growth Rate", "fiscal_year": "Year"},
    )
    fig.update_layout(yaxis_tickformat=".1%")
    st.plotly_chart(fig, use_container_width=True)

with col2:
    fig = px.bar(
        yoy.dropna(subset=["net_income_growth"]),
        x="fiscal_year", y="net_income_growth", color="ticker",
        barmode="group", title="Net Income Growth (YoY)",
        labels={"net_income_growth": "Growth Rate", "fiscal_year": "Year"},
    )
    fig.update_layout(yaxis_tickformat=".1%")
    st.plotly_chart(fig, use_container_width=True)

# --- DuPont ROE Decomposition ---
st.header("DuPont ROE Decomposition")
st.caption(
    "DuPont breaks Return on Equity into three drivers: **Net Margin** (profitability), "
    "**Asset Turnover** (efficiency), and **Equity Multiplier** (leverage). "
    "Two companies with the same ROE can get there very differently — this chart shows how."
)

dupont = query(f"""
    SELECT ticker, fiscal_year,
           dupont_net_margin, dupont_asset_turnover,
           dupont_equity_multiplier, dupont_roe
    FROM analytics.yoy_trends
    WHERE ticker IN ({placeholders})
      AND fiscal_year IS NOT NULL
    ORDER BY fiscal_year
""")

dupont_ticker = st.selectbox("Company (DuPont)", available)
dt = dupont[dupont["ticker"] == dupont_ticker].dropna(
    subset=["dupont_net_margin", "dupont_asset_turnover", "dupont_equity_multiplier"],
    how="all",
)

if not dt.empty:
    fig = go.Figure()
    fig.add_trace(go.Bar(x=dt["fiscal_year"], y=dt["dupont_net_margin"], name="Net Margin"))
    fig.add_trace(go.Bar(x=dt["fiscal_year"], y=dt["dupont_asset_turnover"], name="Asset Turnover"))
    fig.add_trace(go.Bar(x=dt["fiscal_year"], y=dt["dupont_equity_multiplier"], name="Equity Multiplier"))
    fig.add_trace(go.Scatter(x=dt["fiscal_year"], y=dt["dupont_roe"], name="ROE", mode="lines+markers"))
    fig.update_layout(title=f"{dupont_ticker} — DuPont Decomposition", barmode="group")
    st.plotly_chart(fig, use_container_width=True)

# --- Liquidity & Leverage ---
st.header("Liquidity & Leverage")
st.caption(
    "**Current ratio** (current assets ÷ current liabilities) measures short-term solvency — "
    "can the company pay its bills next year? **Debt-to-equity** measures how much of the capital "
    "structure is borrowed; higher means more financial risk but also more potential upside."
)

ratios = query(f"""
    SELECT ticker, fiscal_year, current_ratio, quick_ratio,
           debt_to_equity, debt_to_assets
    FROM analytics.financial_ratios
    WHERE ticker IN ({placeholders})
      AND fiscal_year IS NOT NULL
    ORDER BY fiscal_year
""")
ratios = clip_outliers(clip_outliers(ratios, "current_ratio"), "debt_to_equity")

col1, col2 = st.columns(2)
with col1:
    fig = px.line(
        ratios.dropna(subset=["current_ratio"]),
        x="fiscal_year", y="current_ratio", color="ticker",
        title="Current Ratio", labels={"current_ratio": "Ratio", "fiscal_year": "Year"},
        markers=True,
    )
    st.plotly_chart(fig, use_container_width=True)

with col2:
    fig = px.line(
        ratios.dropna(subset=["debt_to_equity"]),
        x="fiscal_year", y="debt_to_equity", color="ticker",
        title="Debt-to-Equity", labels={"debt_to_equity": "Ratio", "fiscal_year": "Year"},
        markers=True,
    )
    st.plotly_chart(fig, use_container_width=True)

# --- Sentiment Analysis ---
st.header("Filing Sentiment Analysis")
st.caption(
    "Loughran-McDonald finance-specific sentiment scoring applied to the **Risk Factors** and **MD&A** "
    "sections of each 10-K. Management word choice often shifts before the numbers do — a sharp drop in "
    "net sentiment is worth a second look."
)

sentiment = query(f"""
    SELECT ticker, fiscal_year, section_type,
           net_sentiment, positive_pct, negative_pct,
           uncertainty_pct, risk_themes, word_count
    FROM analytics.text_sentiment
    WHERE ticker IN ({placeholders})
      AND fiscal_year IS NOT NULL
    ORDER BY ticker, fiscal_year
""")

if not sentiment.empty:
    col1, col2 = st.columns(2)
    with col1:
        rf = sentiment[sentiment["section_type"] == "RISK_FACTORS"].dropna(subset=["net_sentiment"])
        if not rf.empty:
            fig = px.line(
                rf, x="fiscal_year", y="net_sentiment", color="ticker",
                title="Risk Factors — Net Sentiment",
                labels={"net_sentiment": "Net Sentiment", "fiscal_year": "Year"},
                markers=True,
            )
            st.plotly_chart(fig, use_container_width=True)

    with col2:
        mda = sentiment[sentiment["section_type"] == "MDA"].dropna(subset=["net_sentiment"])
        if not mda.empty:
            fig = px.line(
                mda, x="fiscal_year", y="net_sentiment", color="ticker",
                title="MD&A — Net Sentiment",
                labels={"net_sentiment": "Net Sentiment", "fiscal_year": "Year"},
                markers=True,
            )
            st.plotly_chart(fig, use_container_width=True)

    st.subheader("Risk Themes Detected")
    st.caption("Keyword-tagged risk categories surfaced from each filing's Risk Factors section.")
    themes = sentiment[sentiment["risk_themes"].notna()][
        ["ticker", "fiscal_year", "section_type", "risk_themes"]
    ]
    if not themes.empty:
        st.dataframe(themes, use_container_width=True, hide_index=True)
else:
    st.info("No sentiment data available for selected companies.")

# --- Raw Data Explorer ---
st.header("Data Explorer")
st.caption("Browse the underlying tables. Useful for sanity-checking anything above.")

table = st.selectbox("Table", [
    "analytics.financial_ratios",
    "analytics.yoy_trends",
    "analytics.text_sentiment",
    "staging.financial_metrics",
    "raw.sec_filings",
])

explorer_df = query(f"""
    SELECT * FROM {table}
    WHERE ticker IN ({placeholders})
    ORDER BY ticker, fiscal_year
""")
st.dataframe(explorer_df, use_container_width=True, hide_index=True)
