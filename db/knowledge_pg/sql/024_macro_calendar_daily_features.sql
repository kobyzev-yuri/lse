-- Daily macro economic calendar features (region-level, not per ticker).
-- Source: knowledge_base / Investing calendar (ingest: scripts/ingest_macro_calendar_daily_features.py).

CREATE TABLE IF NOT EXISTS macro_calendar_daily_features (
  exchange                    VARCHAR(16)  NOT NULL DEFAULT 'US',
  region                      VARCHAR(16)  NOT NULL DEFAULT 'US',
  trade_date                  DATE         NOT NULL,
  snapshot_label              VARCHAR(32)  NOT NULL DEFAULT 'latest',
  snapshot_ts_utc             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  high_impact_fwd_1d          INTEGER      NOT NULL DEFAULT 0,
  high_impact_fwd_3d          INTEGER      NOT NULL DEFAULT 0,
  high_impact_back_1d         INTEGER      NOT NULL DEFAULT 0,
  hours_to_next_high_impact   NUMERIC(10, 2),
  hours_since_last_high_impact NUMERIC(10, 2),

  calendar_events_used        INTEGER      NOT NULL DEFAULT 0,
  source                      VARCHAR(64)  NOT NULL DEFAULT 'investing_calendar_kb',
  created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  PRIMARY KEY (exchange, region, trade_date, snapshot_label)
);

COMMENT ON TABLE macro_calendar_daily_features IS
  'Macro calendar density around trade_date for multiday ridge. Forward counts use scheduled times only.';

CREATE INDEX IF NOT EXISTS macro_calendar_daily_features_region_date
  ON macro_calendar_daily_features (region, trade_date DESC);
