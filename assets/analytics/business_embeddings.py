""" @bruin
name: analytics.business_embeddings
type: python
depends:
  - raw.filing_text_sections
columns:
  - name: ticker
    type: string
    checks:
      - name: not_null
      - name: unique
  - name: fiscal_year
    type: integer
    checks:
      - name: not_null
@bruin """

import os
import time

import duckdb

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "ten_k.db"))
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"  # 384-dim, ~90MB
MAX_CHARS = 20000  # Clip Item 1 to first ~20k chars; MiniLM truncates anyway


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


def materialize(context):
    from sentence_transformers import SentenceTransformer

    con = connect_db()
    con.execute("CREATE SCHEMA IF NOT EXISTS analytics")

    con.execute("""
        CREATE TABLE IF NOT EXISTS analytics.business_embeddings (
            ticker VARCHAR,
            fiscal_year INTEGER,
            embedding FLOAT[],
            text_length INTEGER
        )
    """)
    con.execute("DELETE FROM analytics.business_embeddings")

    # Take the most recent Business section per ticker
    rows = con.execute("""
        SELECT ticker, fiscal_year, section_text
        FROM (
            SELECT ticker, fiscal_year, section_text,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY fiscal_year DESC) AS rn
            FROM raw.filing_text_sections
            WHERE section_type = 'BUSINESS'
              AND section_text IS NOT NULL
              AND LENGTH(section_text) > 500
        )
        WHERE rn = 1
    """).fetchall()

    if not rows:
        print("No BUSINESS sections available; skipping embedding generation.")
        con.close()
        return

    print(f"Loading {MODEL_NAME}…")
    model = SentenceTransformer(MODEL_NAME)

    print(f"Embedding {len(rows)} business descriptions…")
    tickers, years, texts = zip(*rows)
    clipped = [t[:MAX_CHARS] for t in texts]
    vectors = model.encode(
        list(clipped),
        batch_size=8,
        show_progress_bar=False,
        normalize_embeddings=True,  # so cosine sim = dot product
    )

    out = [
        (ticker, int(year), vec.tolist(), len(text))
        for (ticker, year, text), vec in zip(rows, vectors)
    ]

    con.executemany(
        "INSERT INTO analytics.business_embeddings VALUES (?, ?, ?, ?)",
        out,
    )

    count = con.execute("SELECT count(*) FROM analytics.business_embeddings").fetchone()[0]
    print(f"Wrote {count} embedding rows")
    con.close()


if __name__ == "__main__":
    materialize(None)
