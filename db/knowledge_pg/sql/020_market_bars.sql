-- Универсальные свечи (NYSE и др.) — рядом с legacy quotes, без ломания старых запросов.

CREATE TABLE IF NOT EXISTS market_bars_daily (
  exchange      VARCHAR(16)  NOT NULL,
  symbol        VARCHAR(64)  NOT NULL,
  trade_date    DATE         NOT NULL,
  open          NUMERIC(20, 8),
  high          NUMERIC(20, 8),
  low           NUMERIC(20, 8),
  close         NUMERIC(20, 8),
  volume        BIGINT,
  vwap          NUMERIC(20, 8),
  source        VARCHAR(64)  NOT NULL DEFAULT 'yfinance',
  ingested_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  PRIMARY KEY (exchange, symbol, trade_date)
);

COMMENT ON TABLE market_bars_daily IS 'Дневные OHLCV для разметки forward log-return и сверки с tradenews.valuation';

CREATE TABLE IF NOT EXISTS market_bars_1h (
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

COMMENT ON TABLE market_bars_1h IS 'Часовые бары (UTC); опционально для внутридневной техники и уточнения t';

CREATE INDEX IF NOT EXISTS market_bars_daily_sym_dt
  ON market_bars_daily (symbol, trade_date DESC);

CREATE INDEX IF NOT EXISTS market_bars_1h_sym_ts
  ON market_bars_1h (symbol, bar_start_utc DESC);
