-- Снимок прогноза гэпа на премаркете и факт на открытии RTH (калибровка OLS / премаркет).

CREATE TABLE IF NOT EXISTS game5m_gap_forecast_daily (
  trade_date                  DATE         NOT NULL,
  symbol                      VARCHAR(64)  NOT NULL,
  exchange                    VARCHAR(16)  NOT NULL DEFAULT 'US',

  snapshot_ts_premarket       TIMESTAMPTZ,
  premarket_last              NUMERIC(20, 8),
  prev_close                  NUMERIC(20, 8),
  premarket_gap_pct           NUMERIC(12, 6),

  pred_sector_gap_pct         NUMERIC(12, 6),
  sector_proxy                VARCHAR(32),
  macro_risk_level            VARCHAR(16),
  macro_equity_gap_bias       VARCHAR(16),
  macro_indicators_json       JSONB,

  open_filled_ts              TIMESTAMPTZ,
  rth_open_price              NUMERIC(20, 8),
  open_gap_pct                NUMERIC(12, 6),

  error_pred_vs_open_pct      NUMERIC(12, 6),
  error_premarket_vs_open_pct NUMERIC(12, 6),

  source_premarket            VARCHAR(64),
  source_open                 VARCHAR(64),
  created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  PRIMARY KEY (trade_date, symbol)
);

COMMENT ON TABLE game5m_gap_forecast_daily IS
  'Премаркет-прогноз гэпа (OLS sector + гэп тикера) и факт open vs prev_close — для арбитра анализатора.';

CREATE INDEX IF NOT EXISTS game5m_gap_forecast_daily_date
  ON game5m_gap_forecast_daily (trade_date DESC);

CREATE INDEX IF NOT EXISTS game5m_gap_forecast_daily_symbol_date
  ON game5m_gap_forecast_daily (symbol, trade_date DESC);
