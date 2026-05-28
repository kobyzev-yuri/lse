-- Event / earnings analytics & ML dataset tables (LSE).
-- Применение: scripts/migrate_ml_event_analytics.py или вручную через psql.
-- См. docs/earnings-event-agent-lse/EARNINGS_EVENT_AGENT_DESIGN.md §4

-- Детализация earnings поверх строки knowledge_base (1:1 по kb id)
CREATE TABLE IF NOT EXISTS earnings_event_detail (
    knowledge_base_id INTEGER PRIMARY KEY REFERENCES knowledge_base (id) ON DELETE CASCADE,
    fiscal_period VARCHAR(32),
    eps_actual NUMERIC(20, 6),
    eps_estimate NUMERIC(20, 6),
    revenue_actual NUMERIC(22, 4),
    revenue_estimate NUMERIC(22, 4),
    guidance_summary JSONB,
    affected_tickers JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Реестр первичных материалов отчёта/call.
-- Сюда кладём ссылки и статус скачивания/парсинга до LLM extraction.
CREATE TABLE IF NOT EXISTS earnings_material (
    id BIGSERIAL PRIMARY KEY,
    knowledge_base_id INTEGER REFERENCES knowledge_base (id) ON DELETE SET NULL,
    symbol VARCHAR(16) NOT NULL,
    event_date DATE,
    fiscal_period VARCHAR(32),
    material_type VARCHAR(32) NOT NULL,
    source_name VARCHAR(80),
    source_url TEXT NOT NULL,
    title TEXT,
    published_at TIMESTAMPTZ,
    local_path TEXT,
    content_sha256 VARCHAR(64),
    content_text TEXT,
    parse_status VARCHAR(24) NOT NULL DEFAULT 'registered',
    parse_error TEXT,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (material_type IN (
        'ir_event_page',
        'press_release',
        'presentation',
        'transcript',
        'follow_up_transcript',
        'sec_filing',
        'third_party_transcript',
        'other'
    )),
    CHECK (parse_status IN (
        'registered',
        'downloaded',
        'parsed',
        'extracted',
        'failed',
        'skipped'
    ))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_earnings_material_symbol_event_type_url
    ON earnings_material (symbol, COALESCE(event_date, DATE '1900-01-01'), material_type, source_url);
CREATE INDEX IF NOT EXISTS idx_earnings_material_symbol_date ON earnings_material (symbol, event_date);
CREATE INDEX IF NOT EXISTS idx_earnings_material_kb ON earnings_material (knowledge_base_id);
CREATE INDEX IF NOT EXISTS idx_earnings_material_type ON earnings_material (material_type);
CREATE INDEX IF NOT EXISTS idx_earnings_material_parse_status ON earnings_material (parse_status);

-- Рёбра графа аналогов / тематических связей
CREATE TABLE IF NOT EXISTS peer_graph_edge (
    id BIGSERIAL PRIMARY KEY,
    source_ticker VARCHAR(16) NOT NULL,
    target_ticker VARCHAR(16) NOT NULL,
    relation_type VARCHAR(32) NOT NULL DEFAULT 'peer',
    weight NUMERIC(14, 8) NOT NULL DEFAULT 1.0,
    valid_from DATE,
    valid_to DATE,
    meta JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_ticker, target_ticker, relation_type, valid_from)
);

CREATE INDEX IF NOT EXISTS idx_peer_graph_source ON peer_graph_edge (source_ticker);
CREATE INDEX IF NOT EXISTS idx_peer_graph_target ON peer_graph_edge (target_ticker);

-- Дневной снимок режима рынка (индексы, VIX, флаги)
CREATE TABLE IF NOT EXISTS market_regime_daily (
    trade_date DATE PRIMARY KEY,
    spy_close NUMERIC(18, 6),
    ndx_close NUMERIC(18, 6),
    dia_close NUMERIC(18, 6),
    vix_close NUMERIC(18, 6),
    regime_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    features_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Материализованные строки для ML: признаки до события + исходы после
CREATE TABLE IF NOT EXISTS event_reaction_dataset (
    id BIGSERIAL PRIMARY KEY,
    knowledge_base_id INTEGER REFERENCES knowledge_base (id) ON DELETE SET NULL,
    symbol VARCHAR(16) NOT NULL,
    event_time_et TIMESTAMPTZ NOT NULL,
    event_type VARCHAR(48) NOT NULL DEFAULT 'EARNINGS',
    fiscal_period VARCHAR(32),
    features_before JSONB NOT NULL DEFAULT '{}'::jsonb,
    outcomes_after JSONB NOT NULL DEFAULT '{}'::jsonb,
    final_label VARCHAR(72),
    label_source VARCHAR(32),
    ticker_price_regime VARCHAR(32),
    market_regime_date DATE,
    dataset_version VARCHAR(24) NOT NULL DEFAULT 'v0',
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, event_time_et, event_type, dataset_version)
);

CREATE INDEX IF NOT EXISTS idx_erd_symbol ON event_reaction_dataset (symbol);
CREATE INDEX IF NOT EXISTS idx_erd_event_time ON event_reaction_dataset (event_time_et);
CREATE INDEX IF NOT EXISTS idx_erd_kb ON event_reaction_dataset (knowledge_base_id);
CREATE INDEX IF NOT EXISTS idx_erd_final_label ON event_reaction_dataset (final_label);
CREATE INDEX IF NOT EXISTS idx_erd_dataset_version ON event_reaction_dataset (dataset_version);
