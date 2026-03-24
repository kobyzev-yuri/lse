-- Время фактической загрузки записи в knowledge_base (крон).
-- Поле ts остаётся датой публикации/события из источника (RSS и т.д.).
ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ;

COMMENT ON COLUMN knowledge_base.ingested_at IS 'Когда строка попала в KB; ts — дата публикации из источника';
