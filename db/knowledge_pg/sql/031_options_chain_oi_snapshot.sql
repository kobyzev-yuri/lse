-- Ежедневные снимки OI по страйкам для «ползунка во времени» (Option Money Map).
-- Заполнение: scripts/snapshot_options_chain_oi.py (cron, Phase 3).

CREATE TABLE IF NOT EXISTS options_chain_oi_snapshot (
    id BIGSERIAL PRIMARY KEY,
    snapshot_date DATE NOT NULL,
    ticker TEXT NOT NULL,
    expiration_date DATE NOT NULL,
    spot NUMERIC(12, 4),
    strike NUMERIC(12, 4) NOT NULL,
    contract_type TEXT NOT NULL CHECK (contract_type IN ('call', 'put')),
    open_interest INTEGER NOT NULL DEFAULT 0,
    volume INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'polygon',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_options_oi_snap_ticker_exp_date
    ON options_chain_oi_snapshot (ticker, expiration_date, snapshot_date DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_options_oi_snap_day_strike
    ON options_chain_oi_snapshot (
        snapshot_date, ticker, expiration_date, strike, contract_type
    );
