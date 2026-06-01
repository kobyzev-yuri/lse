-- Rule labels + pre-open feature snapshots for open-path scenario classifier (GAME_5M).

CREATE TABLE IF NOT EXISTS game5m_open_path_labels (
  trade_date                  DATE         NOT NULL,
  symbol                      VARCHAR(64)  NOT NULL,
  exchange                    VARCHAR(16)  NOT NULL DEFAULT 'US',

  scenario_label              VARCHAR(64)  NOT NULL,
  label_source                VARCHAR(32)  NOT NULL DEFAULT 'rule_open_path_v0',
  rule_version                VARCHAR(32)  NOT NULL DEFAULT 'open_path_v0',

  open_gap_pct                NUMERIC(12, 6),
  rth_open_price              NUMERIC(20, 8),
  rth_close_price             NUMERIC(20, 8),
  close_open_log_ret          NUMERIC(12, 8),
  fade_from_gap_pct           NUMERIC(12, 6),

  features_before             JSONB,
  feature_builder_version     VARCHAR(64),

  label_status                VARCHAR(32)  NOT NULL DEFAULT 'ok',
  created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  updated_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

  PRIMARY KEY (exchange, symbol, trade_date)
);

COMMENT ON TABLE game5m_open_path_labels IS
  'Open-path scenario labels (rule after close) + pre-open features for CatBoost multiclass.';

CREATE INDEX IF NOT EXISTS game5m_open_path_labels_date
  ON game5m_open_path_labels (trade_date DESC);

CREATE INDEX IF NOT EXISTS game5m_open_path_labels_label
  ON game5m_open_path_labels (scenario_label, trade_date DESC);
