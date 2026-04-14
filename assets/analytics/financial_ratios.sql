/* @bruin
name: analytics.financial_ratios
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

SELECT
    ticker,
    company_name,
    fiscal_year,
    period_end,

    -- Revenue & profitability
    revenue,
    net_income,
    ROUND(gross_profit / NULLIF(revenue, 0), 4) AS gross_margin,
    ROUND(operating_income / NULLIF(revenue, 0), 4) AS operating_margin,
    ROUND(net_income / NULLIF(revenue, 0), 4) AS net_profit_margin,
    ROUND(net_income / NULLIF(stockholders_equity, 0), 4) AS return_on_equity,
    ROUND(net_income / NULLIF(total_assets, 0), 4) AS return_on_assets,

    -- Liquidity
    ROUND(current_assets / NULLIF(current_liabilities, 0), 4) AS current_ratio,
    ROUND((current_assets - COALESCE(inventory, 0)) / NULLIF(current_liabilities, 0), 4) AS quick_ratio,
    cash_and_equivalents,

    -- Leverage
    ROUND(total_liabilities / NULLIF(total_assets, 0), 4) AS debt_to_assets,
    ROUND(total_liabilities / NULLIF(stockholders_equity, 0), 4) AS debt_to_equity,
    ROUND(long_term_debt / NULLIF(stockholders_equity, 0), 4) AS lt_debt_to_equity,

    -- Efficiency
    ROUND(revenue / NULLIF(total_assets, 0), 4) AS asset_turnover,
    ROUND(accounts_receivable / NULLIF(revenue / 365, 0), 2) AS days_sales_outstanding,
    ROUND(inventory / NULLIF(cogs / 365, 0), 2) AS days_inventory_outstanding,

    -- Cash flow
    operating_cash_flow,
    capex,
    (operating_cash_flow - ABS(COALESCE(capex, 0))) AS free_cash_flow,
    ROUND(operating_cash_flow / NULLIF(net_income, 0), 4) AS cash_flow_to_income

FROM staging.financial_metrics
