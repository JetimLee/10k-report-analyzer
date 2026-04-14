""" @bruin
name: analytics.text_sentiment
type: python
depends:
  - raw.filing_text_sections
columns:
  - name: ticker
    type: string
    checks:
      - name: not_null
  - name: section_type
    type: string
    checks:
      - name: not_null
@bruin """

import os
import re
import time

import duckdb

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

# Loughran-McDonald financial sentiment word lists (curated subset)
# Full lists: https://sraf.nd.edu/loughranmcdonald-master-dictionary/

POSITIVE_WORDS = {
    "achieve", "achievement", "attain", "benefit", "beneficial", "best",
    "bolster", "breakthrough", "compliment", "confident", "creative",
    "delight", "dependable", "diligent", "distinction", "diverse",
    "efficient", "empower", "enable", "enhance", "excellent", "exceptional",
    "excitement", "exclusive", "favorable", "gain", "great", "greatest",
    "growth", "highest", "improve", "improvement", "impressive", "increase",
    "incredible", "innovative", "invention", "leadership", "lucrative",
    "maximize", "optimistic", "outperform", "outstanding", "perfect",
    "pleasant", "positive", "proactive", "proficiency", "profitability",
    "profitable", "progress", "prosper", "rebound", "record", "reward",
    "robust", "solid", "stability", "strength", "strong", "succeed",
    "success", "successful", "superior", "surpass", "tremendous", "upturn",
    "valuable", "versatile", "victory", "win",
}

NEGATIVE_WORDS = {
    "abandon", "adverse", "against", "allegation", "annul", "assault",
    "bankruptcy", "breach", "burden", "catastrophe", "caution", "cease",
    "claim", "closure", "collapse", "concern", "condemn", "conflict",
    "costly", "damage", "decline", "default", "deficit", "delay",
    "deplete", "deteriorate", "difficulty", "diminish", "disadvantage",
    "discontinue", "disruption", "doubt", "downturn", "erode", "erosion",
    "escalate", "eviction", "exacerbate", "excessive", "expose", "fail",
    "failure", "forbid", "forfeit", "fraud", "hamper", "hardship", "harm",
    "hinder", "impair", "impairment", "impediment", "inability", "insolvent",
    "instability", "interrupt", "investigation", "jeopardize", "lawsuit",
    "liability", "liquidate", "litigation", "loss", "losses", "misstate",
    "noncompliance", "obstacle", "penalties", "penalty", "plague",
    "prohibit", "recession", "recourse", "restructuring", "retrench",
    "revoke", "risk", "sanction", "setback", "shortfall", "shutdown",
    "slowdown", "stagnate", "sue", "suspend", "terminate", "threat",
    "turmoil", "uncertain", "uncertainty", "underperform", "unfavorable",
    "unprofitable", "unstable", "violation", "volatile", "volatility",
    "weak", "weakness", "worsen", "writedown", "writeoff",
}

UNCERTAINTY_WORDS = {
    "almost", "ambiguity", "ambiguous", "anticipate", "apparent",
    "approximately", "assume", "assumption", "believe", "cautious",
    "conceivable", "conditional", "confuse", "contingency", "contingent",
    "could", "depend", "doubt", "doubtful", "estimate", "exposure",
    "fluctuate", "forecast", "generally", "hope", "imprecise",
    "indefinite", "indicate", "inexact", "instability", "intend",
    "likelihood", "maybe", "might", "nearly", "occasionally",
    "pending", "perhaps", "possible", "possibly", "potential",
    "predict", "preliminary", "presume", "probable", "probably",
    "project", "random", "reassess", "reconsider", "revise",
    "risky", "roughly", "seems", "somewhat", "speculate",
    "sudden", "suggest", "suppose", "suspect", "tentative",
    "uncertain", "uncertainty", "unclear", "undecided", "undefined",
    "unforeseeable", "unknown", "unlikely", "unpredictable",
    "unresolved", "unsettled", "unspecified", "variable", "variability",
}

# Risk theme keywords
RISK_THEMES = {
    "cyber": ["cyber", "cybersecurity", "data breach", "ransomware", "hacking",
              "information security", "phishing"],
    "climate": ["climate", "greenhouse", "carbon", "emissions", "sustainability",
                "environmental", "renewable"],
    "regulatory": ["regulatory", "regulation", "compliance", "legislation",
                   "government", "antitrust", "SEC", "FTC"],
    "supply_chain": ["supply chain", "supplier", "shortage", "logistics",
                     "procurement", "inventory risk", "disruption"],
    "macro": ["inflation", "recession", "interest rate", "macroeconomic",
              "geopolitical", "tariff", "trade war", "pandemic"],
}


def score_text(text):
    """Score text using Loughran-McDonald word lists."""
    words = re.findall(r"[a-z]+", text.lower())
    total = len(words) if words else 1

    pos_count = sum(1 for w in words if w in POSITIVE_WORDS)
    neg_count = sum(1 for w in words if w in NEGATIVE_WORDS)
    unc_count = sum(1 for w in words if w in UNCERTAINTY_WORDS)

    return {
        "word_count": len(words),
        "positive_count": pos_count,
        "negative_count": neg_count,
        "uncertainty_count": unc_count,
        "positive_pct": round(pos_count / total, 6),
        "negative_pct": round(neg_count / total, 6),
        "uncertainty_pct": round(unc_count / total, 6),
        "net_sentiment": round((pos_count - neg_count) / total, 6),
    }


def detect_risk_themes(text):
    """Detect risk theme mentions in the text."""
    text_lower = text.lower()
    detected = []
    for theme, keywords in RISK_THEMES.items():
        count = sum(text_lower.count(kw) for kw in keywords)
        if count > 0:
            detected.append(theme)
    return ",".join(sorted(detected)) if detected else None


def materialize(context):
    con = connect_db()
    con.execute("CREATE SCHEMA IF NOT EXISTS analytics")

    con.execute("""
        CREATE TABLE IF NOT EXISTS analytics.text_sentiment (
            ticker VARCHAR,
            company_name VARCHAR,
            fiscal_year INTEGER,
            section_type VARCHAR,
            word_count INTEGER,
            positive_count INTEGER,
            negative_count INTEGER,
            uncertainty_count INTEGER,
            positive_pct DOUBLE,
            negative_pct DOUBLE,
            uncertainty_pct DOUBLE,
            net_sentiment DOUBLE,
            risk_themes VARCHAR
        )
    """)
    con.execute("DELETE FROM analytics.text_sentiment")

    sections = con.execute("""
        SELECT ticker, company_name, fiscal_year, section_type, section_text
        FROM raw.filing_text_sections
        WHERE section_text IS NOT NULL
        ORDER BY ticker, fiscal_year, section_type
    """).fetchall()

    rows = []
    for ticker, company_name, fiscal_year, section_type, section_text in sections:
        scores = score_text(section_text)
        themes = detect_risk_themes(section_text)
        rows.append((
            ticker,
            company_name,
            fiscal_year,
            section_type,
            scores["word_count"],
            scores["positive_count"],
            scores["negative_count"],
            scores["uncertainty_count"],
            scores["positive_pct"],
            scores["negative_pct"],
            scores["uncertainty_pct"],
            scores["net_sentiment"],
            themes,
        ))
        print(
            f"{ticker} FY{fiscal_year} {section_type}: "
            f"sentiment={scores['net_sentiment']:.4f}, themes={themes}"
        )

    if rows:
        con.executemany(
            "INSERT INTO analytics.text_sentiment VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    count = con.execute("SELECT count(*) FROM analytics.text_sentiment").fetchone()[0]
    print(f"Scored {count} text sections")
    con.close()


if __name__ == "__main__":
    materialize(None)
