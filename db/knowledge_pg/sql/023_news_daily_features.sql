-- Daily news sentiment aggregates for multiday ridge (per symbol).
-- Source: knowledge_base (ingest: scripts/ingest_news_daily_features.py).

CREATE TABLE IF NOT EXISTS news_daily_features (
  exchange              VARCHAR(16)  NOT NULL DEFAULT 'US',
  symbol                VARCHAR(64)  NOT NULL,
  trade_date            DATE         NOT NULL,
  snapshot_label        VARCHAR(32)  NOT NULL DEFAULT 'latest',
  snapshot_ts_utc       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  article_count         INTEGER      NOT NULL DEFAULT 0,
  sentiment_mean        NUMERIC(8, 6),
  sentiment_min         NUMERIC(8, 6),
  sentiment_max         NUMERIC(8, 6),
  negative_count        INTEGER      NOT NULL DEFAULT 0,
  very_negative_count   INTEGER      NOT NULL DEFAULT 0,
  positive_count        INTEGER      NOT NULL DEFAULT 0,

  kb_rows_used          INTEGER      NOT NULL DEFAULT 0,
  cutoff_ts_utc         TIMESTAMPTZ,
  source                VARCHAR(64)  NOT NULL DEFAULT 'knowledge_base_agg',
  created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  PRIMARY KEY (exchange, symbol, trade_date, snapshot_label)
);

COMMENT ON TABLE news_daily_features IS
  'Per-symbol daily news features for multiday ridge (as-of session close, no outcome_json).';

CREATE INDEX IF NOT EXISTS news_daily_features_symbol_date
  ON news_daily_features (symbol, trade_date DESC);
