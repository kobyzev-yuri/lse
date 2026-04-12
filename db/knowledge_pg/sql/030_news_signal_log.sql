-- Лог решений «новостной импульс» для офлайн-разметки и калибровки (см. tradenews + news_impulse_plan).

CREATE TABLE IF NOT EXISTS news_signal_log (
  id                BIGSERIAL PRIMARY KEY,
  decision_ts_utc   TIMESTAMPTZ NOT NULL,
  exchange          VARCHAR(16),
  symbol            VARCHAR(64) NOT NULL,
  model_id          VARCHAR(128) NOT NULL,
  bias              NUMERIC(10, 6),
  confidence        NUMERIC(10, 6),
  knowledge_base_ids INTEGER[],
  prompt_version    VARCHAR(64),
  tech_bias         NUMERIC(10, 6),
  trade_id          INTEGER,
  extra             JSONB,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE news_signal_log IS 'Снимок сигнала LLM + ссылки на строки knowledge_base; к меткам forward-return джойнится офлайн';

CREATE INDEX IF NOT EXISTS news_signal_log_decision
  ON news_signal_log (decision_ts_utc DESC);

CREATE INDEX IF NOT EXISTS news_signal_log_symbol_decision
  ON news_signal_log (symbol, decision_ts_utc DESC);
