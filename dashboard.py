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
SIC_CACHE_PATH = ".sic_cache.json"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_HEADERS = {
    "User-Agent": os.environ.get("SEC_USER_AGENT", "10KAnalyzer contact@example.com"),
    "Accept-Encoding": "gzip, deflate",
}
CATEGORIES = {
    "Big Tech": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "ORCL"],
    "AI Leaders": ["NVDA", "MSFT", "GOOGL", "META", "AMZN", "AAPL", "PLTR", "AMD", "AVGO", "ORCL", "CRM", "SNOW", "IBM", "ARM", "TSM"],
    "Banking": ["JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC"],
    "Defense & Aerospace": ["LMT", "RTX", "NOC", "GD", "BA", "LHX", "HII"],
    "Pharma & Biotech": ["JNJ", "PFE", "MRK", "LLY", "ABBV", "BMY", "AMGN", "GILD"],
    "Energy (Oil & Gas)": ["XOM", "CVX", "COP", "SLB", "EOG", "PSX", "MPC", "OXY"],
    "Retail": ["WMT", "COST", "TGT", "HD", "LOW", "KR", "DG"],
    "Automotive": ["TSLA", "GM", "F", "STLA", "RIVN", "LCID"],
    "Semiconductors": ["NVDA", "AMD", "INTC", "AVGO", "QCOM", "TXN", "MU", "AMAT", "LRCX"],
    "Cloud & SaaS": ["CRM", "NOW", "SNOW", "MDB", "DDOG", "NET", "PLTR", "WDAY"],
    "Streaming & Media": ["NFLX", "DIS", "CMCSA", "WBD", "PARA", "SPOT"],
    "Airlines": ["DAL", "UAL", "AAL", "LUV", "ALK"],
    "Telecom": ["VZ", "T", "TMUS", "CHTR"],
    "Consumer Staples": ["PG", "KO", "PEP", "MDLZ", "CL", "KMB", "GIS"],
    "Insurance": ["BRK-B", "PGR", "TRV", "ALL", "AIG", "MET", "PRU"],
    "Payments & Fintech": ["V", "MA", "PYPL", "SQ", "FIS", "FISV", "AXP", "COF"],
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


def _connect_writable(retries=15, delay=2):
    """Open a writable DuckDB connection, retrying if another process holds the lock."""
    for attempt in range(retries):
        try:
            return duckdb.connect(DB_PATH)
        except duckdb.IOException:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise


def write_selected_tickers(tickers):
    """Write the selected ticker list to DuckDB (config.selected_tickers)."""
    con = _connect_writable()
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS config")
        con.execute("""
            CREATE TABLE IF NOT EXISTS config.selected_tickers (
                ticker VARCHAR PRIMARY KEY,
                added_at TIMESTAMP DEFAULT now()
            )
        """)
        con.execute("DELETE FROM config.selected_tickers")
        if tickers:
            con.executemany(
                "INSERT INTO config.selected_tickers(ticker) VALUES (?)",
                [(t,) for t in tickers],
            )
    finally:
        con.close()


PIPELINE_ASSETS = [
    "raw.sec_filings",
    "raw.financial_statements",
    "raw.filing_text_sections",
    "staging.financial_metrics",
    "analytics.financial_ratios",
    "analytics.yoy_trends",
    "analytics.text_sentiment",
    "analytics.business_embeddings",
]

# Strip ANSI color codes Bruin emits
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def parse_asset_event(line):
    """Return (asset_name, event) where event is 'start', 'done', 'fail', or None."""
    clean = _ANSI_RE.sub("", line)
    for asset in PIPELINE_ASSETS:
        if asset in clean:
            lower = clean.lower()
            if any(k in lower for k in ("failed", "error")):
                return asset, "fail"
            if any(k in lower for k in ("completed", "finished", "success", "✓", "done")):
                return asset, "done"
            if any(k in lower for k in ("running", "starting", "executing", "▶")):
                return asset, "start"
            return asset, "seen"
    return None, None


def _spawn(cmd):
    return subprocess.Popen(
        cmd,
        cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def run_seed_script(index, limit, progress_bar, status_text, log_container):
    """Run scripts/seed_universe_embeddings.py, streaming [i/N] progress."""
    cmd = ["python", "scripts/seed_universe_embeddings.py", "--index", index]
    if limit:
        cmd += ["--limit", str(limit)]
    proc = _spawn(cmd)
    lines = []
    progress_re = re.compile(r"\[(\d+)/(\d+)\]\s*(\S+)")
    started = time.time()
    last_i = 0
    last_total = limit or 500

    for line in proc.stdout:
        lines.append(line.rstrip())
        if log_container is not None:
            log_container.code("\n".join(lines[-400:]))
        m = progress_re.search(_ANSI_RE.sub("", line))
        if m:
            i, total, ticker = int(m.group(1)), int(m.group(2)), m.group(3)
            last_i, last_total = i, total
            progress_bar.progress(min(i / total, 1.0))
            elapsed = time.time() - started
            rate = i / elapsed if elapsed > 0 else 0
            eta_s = (total - i) / rate if rate else 0
            eta = f"{int(eta_s // 60)}m {int(eta_s % 60)}s remaining" if rate else ""
            status_text.markdown(f"Embedding **{ticker}** — {i}/{total} · {eta}")

    proc.wait()
    if proc.returncode == 0:
        progress_bar.progress(1.0)
    return proc.returncode, "\n".join(lines), last_i, last_total


def run_bruin_pipeline(progress_bar, status_text, log_container):
    """Stream `bruin run` output, updating progress UI and (optionally) a log.
    Returns (returncode, full_output, completed_assets, failed_assets)."""
    import shutil
    cmd = (
        ["poetry", "run", "bruin", "run", "."]
        if shutil.which("poetry")
        else ["bruin", "run", "."]
    )
    proc = _spawn(cmd)
    lines = []
    completed = set()
    failed = set()
    current = None
    total = len(PIPELINE_ASSETS)

    for line in proc.stdout:
        lines.append(line.rstrip())
        if log_container is not None:
            log_container.code("\n".join(lines[-400:]))

        asset, event = parse_asset_event(line)
        if asset:
            if event == "done":
                completed.add(asset)
                if current == asset:
                    current = None
            elif event == "fail":
                failed.add(asset)
                if current == asset:
                    current = None
            elif event == "start":
                current = asset

        done_count = len(completed)
        progress_bar.progress(min(done_count / total, 1.0))
        label = f"Running: **{current}**" if current else f"{done_count} of {total} assets complete"
        status_text.markdown(f"{label}")

    proc.wait()
    return proc.returncode, "\n".join(lines), completed, failed


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
    st.session_state["selection"] = list(already_ingested)

# Pending additions from the peer-suggestion UI show up on the next rerun.
# Reassign the list rather than mutating in place so the multiselect widget
# actually picks up the new values.
pending = st.session_state.pop("_add_to_selection", [])
if pending:
    current = list(st.session_state["selection"])
    for t in pending:
        if t not in current:
            current.append(t)
    st.session_state["selection"] = current

with st.sidebar.expander("Pick by category", expanded=False):
    st.caption("Curated sets of well-known tickers for each industry.")
    chosen_cats = st.multiselect(
        "Categories",
        options=list(CATEGORIES.keys()),
        key="category_picker",
    )
    col_r, col_a = st.columns(2)
    with col_r:
        if st.button("Replace selection", disabled=not chosen_cats, key="replace_cats"):
            st.session_state["selection"] = sorted(
                {t for c in chosen_cats for t in CATEGORIES[c]}
            )
            st.rerun()
    with col_a:
        if st.button("Add to selection", disabled=not chosen_cats, key="add_cats"):
            st.session_state["_add_to_selection"] = sorted(
                {t for c in chosen_cats for t in CATEGORIES[c]}
            )
            st.rerun()

if st.sidebar.button("Clear all tickers", disabled=not st.session_state.get("selection")):
    st.session_state["selection"] = []
    st.rerun()

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

generate_clicked = st.sidebar.button(
    "Generate Report", type="primary", disabled=not selected, key="gen_report_btn"
)

# --- Similar-company suggestions ---
@st.cache_data(ttl=300)
def load_embeddings():
    """Union the user-ingested embeddings with the precomputed universe.
    When a ticker is in both, prefer the ingested row (has more 10-K data)."""
    frames = []
    try:
        a = query("SELECT ticker, embedding, 'ingested' AS source FROM analytics.business_embeddings")
        frames.append(a)
    except Exception:
        pass
    try:
        b = query("SELECT ticker, embedding, 'universe' AS source FROM analytics.sec_universe_embeddings")
        frames.append(b)
    except Exception:
        pass
    if not frames:
        return pd.DataFrame(columns=["ticker", "embedding", "source"])
    out = pd.concat(frames, ignore_index=True)
    out["_rank"] = out["source"].map({"ingested": 0, "universe": 1})
    out = out.sort_values("_rank").drop_duplicates("ticker", keep="first")
    return out.drop(columns=["_rank"]).reset_index(drop=True)


def embedding_peers(anchor, df, top_k=10):
    """Rank ingested tickers by cosine similarity to the anchor's 10-K Business section.
    Embeddings are already L2-normalized at ingest time, so cosine = dot product."""
    if df.empty or anchor not in df["ticker"].values:
        return pd.DataFrame()
    mat = np.vstack(df["embedding"].to_numpy())
    anchor_idx = df.index[df["ticker"] == anchor][0]
    sims = mat @ mat[anchor_idx]
    out = pd.DataFrame({"ticker": df["ticker"], "similarity": sims})
    out = out[out["ticker"] != anchor].sort_values("similarity", ascending=False)
    return out.head(top_k).reset_index(drop=True)


sic_cache = load_sic_cache()
emb_df = load_embeddings()

if selected and not sec_df.empty:
    st.sidebar.divider()
    st.sidebar.subheader("Similar companies")
    anchor = st.sidebar.selectbox(
        "Find peers of",
        selected,
        format_func=lambda t: f"{t} — {label_map.get(t, t)}",
    )

    tab_emb, tab_sic = st.sidebar.tabs(["By business description", "By SIC code"])

    # --- Embedding-based peers (ingested only) ---
    with tab_emb:
        st.caption(
            f"Cosine similarity on MiniLM embeddings of each 10-K Item 1 (Business) section. "
            f"Universe currently holds **{len(emb_df)}** companies."
        )
        if anchor not in set(emb_df["ticker"]):
            st.info(
                "No embedding yet for this ticker. Click **Generate Report** to ingest it, "
                "or seed the universe via `python scripts/seed_universe_embeddings.py`."
            )
        else:
            top_k = st.slider(
                "How many peers to show",
                min_value=10, max_value=100, value=25, step=5,
                key="peer_top_k",
                help="Cosine similarity is symmetric, but a fixed cutoff isn't — "
                     "if A has many close neighbors, B may sit just outside A's top-N "
                     "even when A is in B's top-N. Widen the list to see more.",
            )
            ranked = embedding_peers(anchor, emb_df, top_k=top_k)
            if ranked.empty:
                st.info("No other ingested tickers to compare against yet.")
            else:
                ranked["label"] = ranked["ticker"].apply(
                    lambda t: f"{t} — {label_map.get(t, t)} ({ranked.loc[ranked['ticker'] == t, 'similarity'].iloc[0]:.2f})"
                )
                options = ranked["ticker"].tolist()
                to_add_emb = st.multiselect(
                    "Ranked peers (higher score = more similar)",
                    options=options,
                    format_func=lambda t: ranked.loc[ranked["ticker"] == t, "label"].iloc[0],
                    key="peer_add_emb",
                )
                if st.button("Add to selection", disabled=not to_add_emb, key="add_emb"):
                    st.session_state["_add_to_selection"] = to_add_emb
                    st.rerun()

    # --- SIC-based peer discovery (for finding brand-new tickers outside the ingested set) ---
    with tab_sic:
        st.caption("Coarse industry filter. Use this to surface candidates you haven't ingested yet.")
        need_lookup = [t for t in list(dict.fromkeys([anchor, *already_ingested])) if t not in sic_cache]
        if need_lookup:
            with st.status(f"Fetching SIC for {len(need_lookup)} tickers…", expanded=False):
                get_sic_bulk(need_lookup, cik_lookup, sic_cache)

        anchor_sic = sic_cache.get(anchor, {}).get("sic", "")
        anchor_desc = sic_cache.get(anchor, {}).get("sic_description", "")

        if anchor_sic:
            st.caption(f"SIC **{anchor_sic}** — {anchor_desc}")
            peers = sorted(
                t for t, v in sic_cache.items()
                if v.get("sic") == anchor_sic and t != anchor and t in label_map
            )
            not_yet = [t for t in peers if t not in selected]
            if not_yet:
                to_add_sic = st.multiselect(
                    "Peers (same SIC)",
                    options=not_yet,
                    format_func=lambda t: f"{t} — {label_map.get(t, t)}",
                    key="peer_add_sic",
                )
                if st.button("Add to selection", disabled=not to_add_sic, key="add_sic"):
                    st.session_state["_add_to_selection"] = to_add_sic
                    st.rerun()
            else:
                st.caption("No new SIC peers cached — run Discover below.")

            scan_size = st.slider("Scan batch size", 10, 100, 40, step=10, key="scan_size")
            if st.button("Discover more peers", key="discover_sic"):
                ranked_c = rank_name_similar(anchor, label_map.get(anchor, ""), sec_df, limit=scan_size)
                uncached = [t for t in ranked_c if t not in sic_cache]
                if len(uncached) < scan_size:
                    extras = [t for t in sec_df["ticker"] if t not in sic_cache and t not in uncached]
                    uncached.extend(extras[: scan_size - len(uncached)])
                if uncached:
                    with st.status(
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
                    st.info("Entire SEC universe already cached — no candidates left.")
        else:
            st.caption("Couldn't determine SIC for this ticker.")

st.sidebar.divider()

dev_mode = st.sidebar.toggle("Developer mode", value=False, help="Show the full pipeline log")

# --- Seed peer-suggestion universe ---
st.sidebar.divider()
with st.sidebar.expander("Peer universe", expanded=False):
    st.caption(
        f"Precomputed 10-K Item 1 embeddings used for peer ranking. "
        f"Currently: **{len(emb_df)}** companies."
    )
    index_label = st.radio(
        "Index",
        options=["S&P 500 (~500)", "NASDAQ-100 (~100)", "S&P 500 + NASDAQ-100 (~570)"],
        index=0,
        help="Which universe to pull from. Combined catches tech names like SNOW/MDB that aren't in the S&P 500.",
    )
    index_arg = {
        "S&P 500 (~500)": "sp500",
        "NASDAQ-100 (~100)": "nasdaq100",
        "S&P 500 + NASDAQ-100 (~570)": "sp500+nasdaq100",
    }[index_label]

    seed_scope = st.radio(
        "Seed scope",
        options=["Quick smoke test (20)", "Broad (100)", "Full index"],
        index=0,
        help="Each ticker takes ~5–10s due to SEC rate limits. Start small to verify.",
    )
    scope_limit = {"Quick smoke test (20)": 20, "Broad (100)": 100, "Full index": None}[seed_scope]

    if st.button("Start seeding", key="seed_btn"):
        with st.status(f"Seeding {index_label} ({seed_scope})…", expanded=True) as seed_status:
            seed_progress = st.progress(0.0)
            seed_text = st.empty()
            seed_log = None
            if dev_mode:
                with st.expander("Seed log", expanded=False):
                    seed_log = st.empty()
            rc, output, done, total = run_seed_script(
                index_arg, scope_limit, seed_progress, seed_text, seed_log
            )
            if rc == 0:
                summary_line = next(
                    (line for line in output.splitlines() if line.strip().startswith("embedded:")),
                    f"{done}/{total} processed",
                )
                seed_text.success(f"✓ Seed complete — {summary_line}")
                seed_status.update(label="Seed complete", state="complete")
                st.cache_data.clear()
                time.sleep(1)
                st.rerun()
            else:
                seed_status.update(label="Seed failed", state="error")
                seed_text.error(f"✗ Seed failed (exit {rc}). Turn on Developer mode for the full log.")

if generate_clicked:
    write_selected_tickers(selected)
    with st.status("Running pipeline…", expanded=True) as status:
        progress_bar = st.progress(0.0)
        status_text = st.empty()
        log_box = None
        if dev_mode:
            with st.expander("Pipeline log", expanded=False):
                log_box = st.empty()
        rc, output, completed, failed = run_bruin_pipeline(progress_bar, status_text, log_box)

        if rc == 0:
            progress_bar.progress(1.0)
            status_text.success(f"✓ Pipeline complete — {len(completed)} of {len(PIPELINE_ASSETS)} assets built")
            status.update(label="Pipeline complete", state="complete")
            st.cache_data.clear()
            time.sleep(1)
            st.rerun()
        else:
            status.update(label="Pipeline failed", state="error")
            if failed:
                status_text.error(
                    "✗ Pipeline failed. Failed assets:\n" + "\n".join(f"- `{a}`" for a in sorted(failed))
                )
            else:
                status_text.error(f"✗ Pipeline failed (exit {rc}). Turn on Developer mode to see the log.")

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
