-- Дополнительные индексы под выборки KB для NYSE-контура и временных окон.

CREATE INDEX IF NOT EXISTS knowledge_base_ts_brin
  ON knowledge_base USING brin (ts);

CREATE INDEX IF NOT EXISTS knowledge_base_ticker_ts
  ON knowledge_base (ticker, ts DESC);

CREATE INDEX IF NOT EXISTS knowledge_base_exchange_ts
  ON knowledge_base (exchange, ts DESC)
  WHERE exchange IS NOT NULL;

CREATE INDEX IF NOT EXISTS knowledge_base_symbol_ts
  ON knowledge_base (symbol, ts DESC)
  WHERE symbol IS NOT NULL;

-- Поиск по сырому JSON (осторожно с нагрузкой; для админ-запросов)
CREATE INDEX IF NOT EXISTS knowledge_base_raw_payload_gin
  ON knowledge_base USING gin (raw_payload jsonb_path_ops)
  WHERE raw_payload IS NOT NULL;
