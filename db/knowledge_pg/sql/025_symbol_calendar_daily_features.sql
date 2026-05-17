-- Per-symbol calendar features (earnings proximity, etc.) for multiday ridge.
-- Source: knowledge_base EARNINGS*, earnings_event_detail (ingest: scripts/ingest_symbol_calendar_daily_features.py).

CREATE TABLE IF NOT EXISTS symbol_calendar_daily_features (
  exchange                VARCHAR(16)  NOT NULL DEFAULT 'US',
  symbol                  VARCHAR(64)  NOT NULL,
  trade_date              DATE         NOT NULL,
  snapshot_label          VARCHAR(32)  NOT NULL DEFAULT 'latest',
  snapshot_ts_utc         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  days_to_next_earnings   INTEGER,
  days_since_last_earnings INTEGER,
  is_earnings_day         SMALLINT     NOT NULL DEFAULT 0,
  earnings_within_3d      SMALLINT     NOT NULL DEFAULT 0,
  next_earnings_importance VARCHAR(16),

  events_used             INTEGER      NOT NULL DEFAULT 0,
  source                  VARCHAR(64)  NOT NULL DEFAULT 'knowledge_base_earnings',
  created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  PRIMARY KEY (exchange, symbol, trade_date, snapshot_label)
);

COMMENT ON TABLE symbol_calendar_daily_features IS
  'Issuer calendar (earnings window) per trade_date for multiday ridge X.';

CREATE INDEX IF NOT EXISTS symbol_calendar_daily_features_symbol_date
  ON symbol_calendar_daily_features (symbol, trade_date DESC);
