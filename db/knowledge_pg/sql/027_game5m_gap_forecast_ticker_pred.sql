-- Прогноз гэпа по тикеру (v2) vs факт open — для арбитра game5m_gap_forecast_arbiter.

ALTER TABLE game5m_gap_forecast_daily
  ADD COLUMN IF NOT EXISTS pred_ticker_gap_pct NUMERIC(12, 6);

ALTER TABLE game5m_gap_forecast_daily
  ADD COLUMN IF NOT EXISTS pred_ticker_source VARCHAR(64);

ALTER TABLE game5m_gap_forecast_daily
  ADD COLUMN IF NOT EXISTS pred_ticker_model_version VARCHAR(32);

ALTER TABLE game5m_gap_forecast_daily
  ADD COLUMN IF NOT EXISTS error_pred_ticker_vs_open_pct NUMERIC(12, 6);

COMMENT ON COLUMN game5m_gap_forecast_daily.pred_ticker_gap_pct IS
  'Прогноз гэпа на open по тикеру (OLS v2: сектор+макро, опц. blend с премаркетом).';

COMMENT ON COLUMN game5m_gap_forecast_daily.error_pred_ticker_vs_open_pct IS
  'open_gap_pct − pred_ticker_gap_pct (заполняется при phase open).';
