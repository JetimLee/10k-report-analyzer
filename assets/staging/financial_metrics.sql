/* @bruin
name: staging.financial_metrics
type: duckdb.sql
materialization:
  type: table
depends:
  - raw.financial_statements
columns:
  - name: ticker
    type: string
    checks:
      - name: not_null
  - name: fiscal_year
    type: int
    checks:
      - name: not_null
      - name: positive
custom_checks:
  - name: no_duplicate_company_years
    query: |
      SELECT count(*) - count(DISTINCT (ticker, fiscal_year))
      FROM staging.financial_metrics
@bruin */

WITH deduped AS (
    SELECT
        ticker,
        company_name,
        metric_name,
        value,
        -- Derive actual fiscal year from the period end date, not the XBRL fy field.
        -- XBRL fy is the filing year, so a FY2025 filing includes FY2023/FY2024
        -- comparative data all tagged fy=2025 but with different period_end dates.
        YEAR(period_end) AS fiscal_year,
        period_end,
        filing_date,
        ROW_NUMBER() OVER (
            PARTITION BY ticker, YEAR(period_end), metric_name
            ORDER BY filing_date DESC
        ) AS rn
    FROM raw.financial_statements
    WHERE fiscal_period = 'FY'
      AND period_end IS NOT NULL
      AND value IS NOT NULL
)
SELECT
    ticker,
    company_name,
    fiscal_year,
    MAX(period_end) AS period_end,
    MAX(CASE WHEN metric_name = 'Revenues' THEN value
             WHEN metric_name = 'RevenueFromContractWithCustomerExcludingAssessedTax' THEN value
        END) AS revenue,
    MAX(CASE WHEN metric_name = 'CostOfGoodsSold' THEN value END) AS cogs,
    MAX(CASE WHEN metric_name = 'GrossProfit' THEN value END) AS gross_profit,
    MAX(CASE WHEN metric_name = 'OperatingIncome' THEN value END) AS operating_income,
    MAX(CASE WHEN metric_name = 'NetIncome' THEN value END) AS net_income,
    MAX(CASE WHEN metric_name = 'EarningsPerShareDiluted' THEN value END) AS eps_diluted,
    MAX(CASE WHEN metric_name = 'TotalAssets' THEN value END) AS total_assets,
    MAX(CASE WHEN metric_name = 'TotalCurrentAssets' THEN value END) AS current_assets,
    MAX(CASE WHEN metric_name = 'TotalLiabilities' THEN value END) AS total_liabilities,
    MAX(CASE WHEN metric_name = 'TotalCurrentLiabilities' THEN value END) AS current_liabilities,
    MAX(CASE WHEN metric_name = 'StockholdersEquity' THEN value END) AS stockholders_equity,
    MAX(CASE WHEN metric_name = 'CashAndEquivalents' THEN value END) AS cash_and_equivalents,
    MAX(CASE WHEN metric_name = 'TotalDebt' THEN value END) AS long_term_debt,
    MAX(CASE WHEN metric_name = 'Inventory' THEN value END) AS inventory,
    MAX(CASE WHEN metric_name = 'AccountsReceivable' THEN value END) AS accounts_receivable,
    MAX(CASE WHEN metric_name = 'OperatingCashFlow' THEN value END) AS operating_cash_flow,
    MAX(CASE WHEN metric_name = 'CapitalExpenditures' THEN value END) AS capex
FROM deduped
WHERE rn = 1
GROUP BY ticker, company_name, fiscal_year
