-- Интрадей OHLCV для бэктеста GAME_5m и пополнения из yfinance (начальное окно ~7 дней, далее append).
-- Время бара: bar_start_utc — открытие бара в UTC (как market_bars_1h).

CREATE TABLE IF NOT EXISTS market_bars_5m (
  exchange      VARCHAR(16)  NOT NULL,
  symbol        VARCHAR(64)  NOT NULL,
  bar_start_utc TIMESTAMPTZ  NOT NULL,
  open          NUMERIC(20, 8),
  high          NUMERIC(20, 8),
  low           NUMERIC(20, 8),
  close         NUMERIC(20, 8),
  volume        BIGINT,
  source        VARCHAR(64)  NOT NULL DEFAULT 'yfinance',
  ingested_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  PRIMARY KEY (exchange, symbol, bar_start_utc)
);

COMMENT ON TABLE market_bars_5m IS '5m OHLCV UTC bar open — лимит Yahoo см. MAX_DAYS_5M в recommend_5m.py';

CREATE INDEX IF NOT EXISTS market_bars_5m_sym_ts
  ON market_bars_5m (symbol, bar_start_utc DESC);

CREATE TABLE IF NOT EXISTS market_bars_30m (
  exchange      VARCHAR(16)  NOT NULL,
  symbol        VARCHAR(64)  NOT NULL,
  bar_start_utc TIMESTAMPTZ  NOT NULL,
  open          NUMERIC(20, 8),
  high          NUMERIC(20, 8),
  low           NUMERIC(20, 8),
  close         NUMERIC(20, 8),
  volume        BIGINT,
  source        VARCHAR(64)  NOT NULL DEFAULT 'yfinance',
  ingested_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  PRIMARY KEY (exchange, symbol, bar_start_utc)
);

COMMENT ON TABLE market_bars_30m IS '30m OHLCV UTC bar open — тот же горизонт что и 5m при ingest';

CREATE INDEX IF NOT EXISTS market_bars_30m_sym_ts
  ON market_bars_30m (symbol, bar_start_utc DESC);
