/* @bruin
name: analytics.yoy_trends
type: duckdb.sql
materialization:
  type: table
depends:
  - staging.financial_metrics
columns:
  - name: ticker
    type: string
    checks:
      - name: not_null
  - name: fiscal_year
    type: int
    checks:
      - name: not_null
@bruin */

WITH base AS (
    SELECT
        ticker,
        company_name,
        fiscal_year,
        period_end,
        revenue,
        net_income,
        gross_profit,
        operating_income,
        total_assets,
        stockholders_equity,
        operating_cash_flow,
        capex,
        LAG(revenue)            OVER w AS prev_revenue,
        LAG(net_income)         OVER w AS prev_net_income,
        LAG(gross_profit)       OVER w AS prev_gross_profit,
        LAG(operating_income)   OVER w AS prev_operating_income,
        LAG(total_assets)       OVER w AS prev_total_assets,
        LAG(stockholders_equity) OVER w AS prev_stockholders_equity
    FROM staging.financial_metrics
    WINDOW w AS (PARTITION BY ticker ORDER BY fiscal_year)
)
SELECT
    ticker,
    company_name,
    fiscal_year,
    period_end,

    -- YoY growth rates
    ROUND((revenue - prev_revenue) / NULLIF(ABS(prev_revenue), 0), 4)
        AS revenue_growth,
    ROUND((net_income - prev_net_income) / NULLIF(ABS(prev_net_income), 0), 4)
        AS net_income_growth,
    ROUND((operating_income - prev_operating_income) / NULLIF(ABS(prev_operating_income), 0), 4)
        AS operating_income_growth,

    -- Current year margins
    ROUND(gross_profit / NULLIF(revenue, 0), 4)     AS gross_margin,
    ROUND(operating_income / NULLIF(revenue, 0), 4)  AS operating_margin,
    ROUND(net_income / NULLIF(revenue, 0), 4)        AS net_margin,

    -- Prior year margins
    ROUND(prev_gross_profit / NULLIF(prev_revenue, 0), 4)     AS prev_gross_margin,
    ROUND(prev_operating_income / NULLIF(prev_revenue, 0), 4) AS prev_operating_margin,
    ROUND(prev_net_income / NULLIF(prev_revenue, 0), 4)       AS prev_net_margin,

    -- Margin deltas (bps change)
    ROUND(
        (gross_profit / NULLIF(revenue, 0)) -
        (prev_gross_profit / NULLIF(prev_revenue, 0)),
    4) AS gross_margin_delta,
    ROUND(
        (operating_income / NULLIF(revenue, 0)) -
        (prev_operating_income / NULLIF(prev_revenue, 0)),
    4) AS operating_margin_delta,
    ROUND(
        (net_income / NULLIF(revenue, 0)) -
        (prev_net_income / NULLIF(prev_revenue, 0)),
    4) AS net_margin_delta,

    -- DuPont decomposition: ROE = Net Margin x Asset Turnover x Equity Multiplier
    ROUND(net_income / NULLIF(revenue, 0), 4) AS dupont_net_margin,
    ROUND(revenue / NULLIF(total_assets, 0), 4) AS dupont_asset_turnover,
    ROUND(total_assets / NULLIF(stockholders_equity, 0), 4) AS dupont_equity_multiplier,
    ROUND(
        (net_income / NULLIF(revenue, 0)) *
        (revenue / NULLIF(total_assets, 0)) *
        (total_assets / NULLIF(stockholders_equity, 0)),
    4) AS dupont_roe,

    -- Free cash flow trend
    (operating_cash_flow - ABS(COALESCE(capex, 0))) AS free_cash_flow

FROM base
WHERE prev_revenue IS NOT NULL
ORDER BY ticker, fiscal_year
