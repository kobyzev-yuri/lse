-- Дополнения к существующей knowledge_base (совместимо с lse init_db.py).
-- Тикер в legacy-колонке ticker часто VARCHAR(10); для длинных символов используйте symbol.

ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS exchange VARCHAR(16);
COMMENT ON COLUMN knowledge_base.exchange IS 'Биржа/рынок: NYSE, NASDAQ, LSE, COMEX, …';

ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS symbol VARCHAR(64);
COMMENT ON COLUMN knowledge_base.symbol IS 'Унифицированный символ (может совпадать с ticker; для futures/options длиннее)';

ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS external_id VARCHAR(512);
COMMENT ON COLUMN knowledge_base.external_id IS 'Стабильный id из источника или детерминированный ключ дедупа';

ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS content_sha256 CHAR(64);
COMMENT ON COLUMN knowledge_base.content_sha256 IS 'SHA-256 нормализованного текста для дедупа без учёта URL';

ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS raw_payload JSONB;
COMMENT ON COLUMN knowledge_base.raw_payload IS 'Сырой объект от провайдера (NewsAPI, Alpha Vantage, RSS, …)';

-- Заполнить symbol из ticker там, где пусто (однократная логика применяется безопасно при каждом прогоне)
UPDATE knowledge_base SET symbol = ticker WHERE symbol IS NULL AND ticker IS NOT NULL;

-- Дедуп по внешнему id (только непустые)
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_base_external_id_uq
  ON knowledge_base (external_id)
  WHERE external_id IS NOT NULL AND length(trim(external_id)) > 0;

-- Дедуп по ссылке + тикеру уже используется в коде; усилим уникальность при непустой link
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_base_link_ticker_uq
  ON knowledge_base (ticker, link)
  WHERE link IS NOT NULL AND length(trim(link)) > 0;
