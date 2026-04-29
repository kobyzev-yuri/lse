-- Compact premarket features for ML.
-- One row per exchange/symbol/trade_date/snapshot_label.

CREATE TABLE IF NOT EXISTS premarket_daily_features (
  exchange                    VARCHAR(16)  NOT NULL DEFAULT 'US',
  symbol                      VARCHAR(64)  NOT NULL,
  trade_date                  DATE         NOT NULL,
  snapshot_label              VARCHAR(32)  NOT NULL DEFAULT 'latest',
  snapshot_ts_utc             TIMESTAMPTZ  NOT NULL,
  snapshot_time_et            TIMESTAMP    NOT NULL,
  minutes_until_open          INTEGER,

  prev_close                  NUMERIC(20, 8),
  daily_volatility_5          NUMERIC(20, 8),

  premarket_open              NUMERIC(20, 8),
  premarket_high              NUMERIC(20, 8),
  premarket_low               NUMERIC(20, 8),
  premarket_last              NUMERIC(20, 8),
  premarket_vwap              NUMERIC(20, 8),
  premarket_volume            BIGINT,
  premarket_bar_count         INTEGER,

  premarket_gap_pct           NUMERIC(12, 6),
  premarket_return_pct        NUMERIC(12, 6),
  premarket_range_pct         NUMERIC(12, 6),
  gap_vs_daily_volatility     NUMERIC(12, 6),

  source                      VARCHAR(64)  NOT NULL DEFAULT 'yfinance_1m_prepost',
  created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  PRIMARY KEY (exchange, symbol, trade_date, snapshot_label)
);

COMMENT ON TABLE premarket_daily_features IS
  'Aggregated premarket context for ML: gap/fade/follow-through features, not an execution table.';

CREATE INDEX IF NOT EXISTS premarket_daily_features_symbol_date
  ON premarket_daily_features (symbol, trade_date DESC);
