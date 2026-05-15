from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from report_generator import get_engine, load_trade_history, compute_closed_trade_pnls
from services.recommend_5m import fetch_5m_ohlc
from services.deal_params_5m import normalize_entry_context
from services.analyzer_ml_arbiter import build_ml_production_arbiter, build_multiday_lr_reality_check
from services.analyzer_product_ideas_arbiter import build_product_ideas_arbiter
from config_loader import get_config_value, load_config, is_editable_config_env_key

# Дольше — считаем позицию «подвисшей» для пристрастного разбора порогов входа vs выхода.
LONG_HOLD_MINUTES = 7 * 24 * 60
ENTRY_REASONING_ANALYZER_MAX = 420
# Фаза B: пороги выборки для TIME_EXIT_EARLY (анализатор).
TIME_EXIT_EARLY_MIN_ROWS_ML = 15
TIME_EXIT_EARLY_MIN_ROWS_SOFT_CAP = 5
TIME_EXIT_EARLY_MIN_ROWS_HARD_CAP = 2

# Фаза C: псевдо-датасет recovery (удержание GAME_5M → метки по будущим 5m барам; фичи только до t).
RECOVERY_ML_SCHEMA_VERSION = "1"
RECOVERY_ML_DEFAULT_HORIZONS_MINUTES = (120, 390)
RECOVERY_ML_DEFAULT_EPS_UP_PCT = 0.5
RECOVERY_ML_DEFAULT_MAX_ADVERSE_PCT = -3.0
RECOVERY_ML_MAX_ROWS_PER_TRADE = 48
RECOVERY_ML_MAX_TOTAL_ROWS = 80_000
RECOVERY_ML_ROW_STRIDE_DEFAULT = 1

# Документация контракта строки (train_game5m_recovery_catboost.py и др.).
RECOVERY_ML_SCHEMA: Dict[str, Any] = {
    "version": RECOVERY_ML_SCHEMA_VERSION,
    "description": (
        "Одна строка = момент t внутри удержания long GAME_5M (5m бар, close эталонная цена). "
        "Фичи известны на закрытии бара t; метки считаются по OHLC строго после t до t+H минут (wall-clock ET), "
        "по полному ряду 5m (включая время после фактического exit сделки — контрфакт «если бы подождали H минут с рынка»)."
    ),
    "horizons_minutes": list(RECOVERY_ML_DEFAULT_HORIZONS_MINUTES),
    "label_eps_up_pct": RECOVERY_ML_DEFAULT_EPS_UP_PCT,
    "label_max_adverse_pct": RECOVERY_ML_DEFAULT_MAX_ADVERSE_PCT,
    "feature_columns": [
        "trade_id (int)",
        "ticker (str)",
        "bar_ts_et (ISO)",
        "ref_close (float, цена на t)",
        "entry_price (float)",
        "pnl_pct (float, ref_close vs entry)",
        "hold_minutes (float)",
        "minutes_after_rth_open (float|None)",
        "dow (int 0=Mon)",
        "hour_et (int)",
        "entry_rsi_5m (float|None)",
        "entry_vol_5m_pct (float|None)",
        "entry_momentum_2h_pct (float|None)",
        "entry_decision (str, усечённый)",
        "exit_signal (str, факт сделки — известен постфактум; можно выкинуть при обучении если утечка)",
    ],
    "label_columns_per_horizon": [
        "h{H}_mfe_fwd_pct — max High в (t, t+H] vs ref_close, %",
        "h{H}_mae_fwd_pct — min Low в том же окне vs ref_close, %",
        "h{H}_y_recovery — 1 если mfe_fwd >= eps_up и mae_fwd >= max_adverse (отриц. порог просадки от ref)",
    ],
}

# Пояснения для UI/LLM (исторически «late_polling» вводит в заблуждение — это не опрос Telegram/cron).
ANALYZER_METRIC_DEFINITIONS: Dict[str, str] = {
    "late_polling_signals": (
        "Число сделок, где одновременно: (1) цена выхода отстает от максимума High в 5m-окне "
        "вход→выход более чем на ~0.4% (|exit−MFE|/MFE>0.004), (2) missed_upside_pct>0.3%. "
        "Прокси «вышли заметно ниже пика окна» (часто норма для лимитного тейка). "
        "НЕ связано с интервалом cron GAME_5M_SIGNAL_CRON_MINUTES."
    ),
    "exit_below_window_mfe_count": (
        "То же значение, что late_polling_signals — предпочтительное имя для интерпретации."
    ),
    "sum_avoidable_loss_pct": (
        "Сумма по сделкам max(0, realized_pct − preventable_worst_pct), "
        "где preventable_worst_pct = (min Low окна / entry − 1)·100. "
        "На прибыльных long сумма может быть большой: вы сильно лучше, чем «если бы вышли на минимуме окна» — "
        "это не «сумма убытков, которых можно было избежать»."
    ),
    "sum_missed_upside_pct": (
        "Сумма max(0, potential_best_pct − realized_pct), potential_best от max High окна; "
        "характеризует недобор до пика окна после фиксации."
    ),
    "likely_late_polling": (
        "Поле по сделке (boolean): |exit−max High окна|/max High > 0.004. "
        "Историческое имя; семантика — «выход не у пика MFE окна», не задержка опроса."
    ),
    "high_vol_losses_count": (
        "Число убыточных сделок, где на входе entry_vol_5m_pct ≥ 0.6 (волатильность из context_json)."
    ),
    "weak_prob_up_losses_count": (
        "Убыточные сделки с entry_prob_up < 0.55 — модель/прогноз на входе не давали высокой вероятности роста."
    ),
    "by_exit_signal": (
        "Агрегаты по типу выхода (exit_signal): count, avg_realized_pct, avg_missed_pct. "
        "Показывает, какой сигнал закрытия доминирует и насколько он «дорогой» по missed upside."
    ),
    "game5m_hanger_tune_json_review": (
        "Пайплайн hanger JSON: путь GAME_5M_HANGER_TUNE_JSON, возраст файла, капы remediation_take_cap по тикеру, "
        "смесь TAKE_PROFIT vs TAKE_PROFIT_SUSPEND в окне; кандидаты на перегенерацию JSON (offline) и на "
        "GAME_5M_HANGER_TUNE_APPLY_TAKE. Решения по эффективности — здесь и в LLM/practical, а не разрозненные проверки."
    ),
    "catboost_entry_backtest": (
        "Проверка CatBoost entry (GAME_5M): берём сохранённый BUY context_json → считаем P(благоприятный исход) "
        "и сопоставляем с фактом realized_pct. Нужен файл модели .cbm + meta.json и включение GAME_5M_CATBOOST_ENABLED."
    ),
    "multiday_lr_reality_check": (
        "Walk-forward OOS ridge multiday (как в live): средний RMSE log-return и доля верного знака по горизонтам 1/2/3 дня "
        "по дневным quotes для тикеров GAME_5M; trade_alignment_sample — прогноз на день входа vs факт log-ret и realized_pct "
        "сделки (интрадей — другой масштаб, справочно)."
    ),
    "ml_production_arbiter": (
        "Сводный вердикт готовности ML к продакшену: multiday ridge OOS, CatBoost entry, портфельный CatBoost (meta RMSE), "
        "recovery .cbm. Поля overall_verdict, verdicts, conclusion_ru — ориентир для оператора; не меняют config."
    ),
    "product_ideas_arbiter": (
        "Вердикт по продуктовым идеям песочницы (макро VIX/Forex, прогноз гэпа сектора, defer early exit): "
        "сравнение PnL по macro_risk_level / entry_advice на закрытых сделках. "
        "verdict: insufficient_data | keep | caution | remove. Не включает флаги в config автоматически."
    ),
    "game5m_catboost_fusion_entry_review": (
        "Таблица по закрытым сделкам: что было сохранено в BUY context_json при входе (technical_decision_core/effective, P, статус). "
        "Поле would_hold_at_runtime_p_threshold — оценка «не вошли бы сейчас» при текущем GAME_5M_CATBOOST_HOLD_BELOW_P и сохранённом P; "
        "сделка уже в БД — это не факт блокировки в прошлом, а удобный контрфакт без чтения логов крона."
    ),
    "catboost_signal_status": (
        "Статус CatBoost-сигнала в payload сделки/проверки: ok|disabled|no_model_file|feature_mismatch|predict_error|… "
        "При ошибке CatBoost не ломает решение правил — остаётся базовый сигнал."
    ),
    "catboost_entry_proba_good": (
        "P(благоприятный исход) по CatBoost на входе. Используется как рекомендательный скор и (опционально) "
        "как осторожный фильтр BUY→HOLD при GAME_5M_CATBOOST_FUSION=hold_if_buy_below_p."
    ),
    "technical_decision_core": (
        "Решение чистых правил (RSI/импульс/качество входа/новости KB и т.д.). Выход из позиции (тейк/стоп/SELL) "
        "в кроне должен опираться на core, а не на ML-effective."
    ),
    "technical_decision_effective": (
        "Итоговый сигнал для входа/LLM после опционального слияния с CatBoost (GAME_5M_CATBOOST_FUSION). "
        "По умолчанию совпадает с core; в режиме hold_if_buy_below_p может понизить BUY/STRONG_BUY до HOLD."
    ),
    "catboost_fusion_mode": "Режим слияния CatBoost с правилами (none|hold_if_buy_below_p).",
    "catboost_fusion_note": "Короткое пояснение, если CatBoost изменил effective сигнал (например BUY→HOLD).",
    "portfolio_catboost_status": (
        "Статус портфельной CatBoost-модели (daily expected return): наличие файлов модели, включение PORTFOLIO_CATBOOST_ENABLED, "
        "и последние метрики обучения (RMSE/MAE/top-decile) из meta.json/JSONL отчёта. Модель advisory: не открывает/не закрывает позиции."
    ),
    "game5m_catboost_status": (
        "Статус CatBoost entry (GAME_5M): trained_at/n_train/n_valid/AUC, исключения (например false_take_profit_by_session_high), "
        "и оценка trust_level. Используется для понимания, насколько аккуратно применять ML-слияние (BUY→HOLD) и как интерпретировать P."
    ),
    "time_exit_early_review": (
        "Контрфакт для TIME_EXIT_EARLY: что было бы с ценой после фактического выхода. "
        "Считаем post-exit MFE/MAE по 5m OHLC на горизонте 1 часа (и опционально до конца дня) относительно цены выхода, "
        "плюс отскок относительно цены входа (recovery к безубытку), минуты после открытия RTH (ET) и время до break-even по exit. "
        "config_candidates.proposals — числовые уровни GAME_5M_* (stale_reversal / early_derisk), когда отскоки после выхода систематичны; "
        "дублируются в practical_parameter_suggestions для auto_config_override."
    ),
    "time_exit_early_action_summary": (
        "Сводка для оператора: сколько TIME_EXIT_EARLY по exit_detail, доля whipsaw за 1h после выхода, "
        "есть ли предложения с уверенностью high/medium, приоритет ветки stale_reversal vs early_derisk, "
        "insufficient_data_for_ml при малых n. См. time_exit_early_review для деталей."
    ),
    "game5m_hold_recovery_dataset_stats": (
        "Фаза C: псевдо-строки по окнам удержания GAME_5M из trade_effects + 5m OHLC. "
        "Статистика объёма/баланса меток h{H}_y_recovery для горизонтов H (по умолчанию 120 и 390 мин). "
        "Экспорт JSONL — при export_recovery_ml=true (вызов анализатора или ?export_recovery_ml=1 в API); "
        "опционально фиксированный путь — ANALYZER_RECOVERY_ML_EXPORT_PATH, иначе LOG_DIR + timestamp. "
        "Контракт колонок: см. RECOVERY_ML_SCHEMA в trade_effectiveness_analyzer."
    ),
    "game5m_recovery_model_status": (
        "Фаза D2: CatBoost recovery (удержание): путь GAME_5M_RECOVERY_CATBOOST_MODEL_PATH, meta.json (AUC, n_train, горизонт метки), "
        "флаг GAME_5M_RECOVERY_ML_ENABLED (прод-использование в game_5m — отдельно). Обучение: scripts/train_game5m_recovery_catboost.py."
    ),
    "recovery_scenario_backtest": (
        "Фаза D3: для сделок TIME_EXIT_EARLY (GAME_5M) — скор recovery на последнем баре удержания; если P < τ, "
        "считаем упрощённый контрфакт «выход на K 5m-баров позже» по Close (без комиссий). Пороги τ и K — GAME_5M_RECOVERY_SCENARIO_*."
    ),
    "recovery_ml_d4a_live_review": (
        "Фаза D4a: по закрытым SELL с `recovery_ml_time_exit_early` в `exit_context_json` — сопоставление live P, "
        "`would_defer_exit` с пост-выходными метриками (`time_exit_early_review.detail_rows`) и контрфактом «K баров позже» "
        "(как планируемый D4b, `GAME_5M_RECOVERY_LIVE_DEFER_BARS`). Таблица `tau_sweep`: при политике крона defer iff P≥τ "
        "(не путать с `recovery_scenario_backtest`, где триггер по другому порогу P<τ). "
        "Поле `tau_sweep_by_k` / `best_tau_by_k` — то же по нескольким K из `GAME_5M_RECOVERY_D4A_STATS_K_BARS` для подбора τ и горизонта пролонгации. "
        "`shallow_gate_by_window_days` + `window_suggestion` — лёгкий пересмотр: хватает ли **меньшего** календарного окна по числу TIME_EXIT_EARLY с gate (без OHLC); крон и опционально анализатор (`GAME_5M_RECOVERY_D4A_STATS_ATTACH_TO_ANALYZER`)."
    ),
    "realized_pct": (
        "Результат сделки в % от cost basis из net_pnl (как в отчёте); для рядов также считается realized_log_return = log(1+p/100)."
    ),
}


def _load_last_jsonl_record(path: str, *, max_bytes: int = 256_000) -> Optional[Dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        # читаем хвост файла: достаточно для последней строки
        data = p.read_bytes()
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        text = data.decode("utf-8", errors="ignore")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return None
        return json.loads(lines[-1])
    except Exception:
        return None


def _build_portfolio_catboost_status() -> Dict[str, Any]:
    """
    Lightweight status block: exposes whether the portfolio ML is configured and what the last training run reported.
    Does not train or query external services.
    """
    try:
        from config_loader import get_config_value

        enabled = (get_config_value("PORTFOLIO_CATBOOST_ENABLED", "false") or "false").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        model_path = (get_config_value("PORTFOLIO_CATBOOST_MODEL_PATH", "") or "").strip()
        if not model_path:
            model_path = str(Path(__file__).resolve().parents[1] / "local" / "models" / "portfolio_return_catboost.cbm")
        meta_path = str(Path(model_path).with_suffix(".meta.json"))
        report_path = (
            (get_config_value("PORTFOLIO_ML_REPORT_JSONL", "") or "").strip()
            or ("/app/logs/ml/logs/portfolio_daily_ml_report.jsonl" if Path("/app/logs").exists() else str(Path(__file__).resolve().parents[1] / "local" / "logs" / "portfolio_daily_ml_report.jsonl"))
        )
        meta = None
        try:
            if Path(meta_path).is_file():
                meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        except Exception:
            meta = None
        last = _load_last_jsonl_record(report_path)
        return {
            "enabled": bool(enabled),
            "model_path": model_path,
            "meta_path": meta_path,
            "model_file_exists": Path(model_path).is_file(),
            "meta_file_exists": Path(meta_path).is_file(),
            "last_training_jsonl_path": report_path,
            "last_training": last,
            "meta_summary": {
                "trained_at": (meta or {}).get("trained_at"),
                "horizon_days": (meta or {}).get("horizon_days"),
                "n_train": (meta or {}).get("n_train"),
                "n_valid": (meta or {}).get("n_valid"),
                "metrics": (meta or {}).get("metrics"),
            }
            if isinstance(meta, dict)
            else None,
        }
    except Exception as e:
        return {"error": str(e)}


def _trust_level_game5m_catboost(meta: Optional[Dict[str, Any]], last: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        auc = None
        n_valid = None
        if isinstance(meta, dict):
            auc = meta.get("auc_valid")
            n_valid = meta.get("n_valid")
        if auc is None and isinstance(last, dict):
            cb = (last.get("catboost_entry") or {}) if isinstance(last.get("catboost_entry"), dict) else {}
            auc = cb.get("auc_valid")
            n_valid = cb.get("n_valid")
        auc_f = float(auc) if auc is not None else None
        nv = int(n_valid) if n_valid is not None else None
    except Exception:
        auc_f = None
        nv = None

    # Conservative: small holdout + modest AUC => keep as advisory/guard only.
    if auc_f is None or nv is None:
        return {"trust_level": "unknown", "trust_reason": "Нет метрик AUC/n_valid (meta/jsonl отсутствуют)."}
    if nv < 80:
        return {"trust_level": "low", "trust_reason": f"Мало validation строк (n_valid={nv}); использовать только как мягкий BUY→HOLD фильтр."}
    if auc_f < 0.58:
        return {"trust_level": "low", "trust_reason": f"AUC={auc_f:.3f} < 0.58; сигнал слабый, только рекомендательный."}
    if auc_f < 0.65:
        return {"trust_level": "medium", "trust_reason": f"AUC={auc_f:.3f} при n_valid={nv}; допустим осторожный guard, без агрессивных правил."}
    return {"trust_level": "high", "trust_reason": f"AUC={auc_f:.3f} при n_valid={nv}; можно обсуждать расширение влияния после walk-forward."}


def _trust_level_game5m_recovery(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(meta, dict):
        return {"recovery_trust_level": "unknown", "recovery_trust_reason": "Нет meta.json recovery-модели."}
    auc = meta.get("auc_valid")
    nv = meta.get("n_valid")
    try:
        auc_f = float(auc) if auc is not None else None
    except (TypeError, ValueError):
        auc_f = None
    try:
        nv_i = int(nv) if nv is not None else None
    except (TypeError, ValueError):
        nv_i = None
    if auc_f is None or nv_i is None:
        return {"recovery_trust_level": "unknown", "recovery_trust_reason": "В meta нет auc_valid/n_valid."}
    if nv_i < 200:
        return {
            "recovery_trust_level": "low",
            "recovery_trust_reason": f"Мало валидационных строк (n_valid={nv_i}); сценарный бэктест только ориентир.",
        }
    if auc_f < 0.55:
        return {
            "recovery_trust_level": "low",
            "recovery_trust_reason": f"AUC={auc_f:.3f} < 0.55; слабая модель для решений о задержке выхода.",
        }
    if auc_f < 0.62:
        return {
            "recovery_trust_level": "medium",
            "recovery_trust_reason": f"AUC={auc_f:.3f}; осторожная калибровка порога τ.",
        }
    return {
        "recovery_trust_level": "high",
        "recovery_trust_reason": f"AUC={auc_f:.3f} при n_valid={nv_i}; можно подбирать τ по recovery_scenario_backtest.",
    }


def _build_game5m_recovery_model_status() -> Dict[str, Any]:
    try:
        from services.game5m_recovery_catboost import default_recovery_catboost_model_path, load_recovery_model_meta

        prod_enabled = (get_config_value("GAME_5M_RECOVERY_ML_ENABLED", "false") or "false").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        model_path = (get_config_value("GAME_5M_RECOVERY_CATBOOST_MODEL_PATH", "") or "").strip()
        if not model_path:
            model_path = str(default_recovery_catboost_model_path())
        meta_path = str(Path(model_path).with_suffix(".meta.json"))
        meta = load_recovery_model_meta(model_path)
        trust = _trust_level_game5m_recovery(meta if isinstance(meta, dict) else None)
        return {
            "prod_enabled_config": bool(prod_enabled),
            "model_path": model_path,
            "meta_path": meta_path,
            "model_file_exists": Path(model_path).is_file(),
            "meta_file_exists": Path(meta_path).is_file(),
            "meta_summary": {
                "trained_at": (meta or {}).get("trained_at"),
                "n_train": (meta or {}).get("n_train"),
                "n_valid": (meta or {}).get("n_valid"),
                "n_total": (meta or {}).get("n_total"),
                "label_column": (meta or {}).get("label_column"),
                "horizon_minutes": (meta or {}).get("horizon_minutes"),
                "auc_valid": (meta or {}).get("auc_valid"),
                "jsonl_source": (meta or {}).get("jsonl_source"),
            }
            if isinstance(meta, dict)
            else None,
            **trust,
            "note": "Прод-логика в game_5m только с GAME_5M_RECOVERY_ML_ENABLED и отдельным ревью (фаза D4).",
        }
    except Exception as e:
        return {"error": str(e)}


def _build_game5m_catboost_status() -> Dict[str, Any]:
    try:
        enabled = (get_config_value("GAME_5M_CATBOOST_ENABLED", "false") or "false").strip().lower() in ("1", "true", "yes")
        model_path = (get_config_value("GAME_5M_CATBOOST_MODEL_PATH", "") or "").strip()
        if not model_path:
            model_path = str(Path(__file__).resolve().parents[1] / "local" / "models" / "game5m_entry_catboost.cbm")
        meta_path = str(Path(model_path).with_suffix(".meta.json"))
        report_path = (
            (get_config_value("DAILY_ML_REPORT_JSONL", "") or "").strip()
            or ("/app/logs/ml/logs/game5m_daily_ml_report.jsonl" if Path("/app/logs").exists() else str(Path(__file__).resolve().parents[1] / "local" / "logs" / "game5m_daily_ml_report.jsonl"))
        )
        meta = None
        try:
            if Path(meta_path).is_file():
                meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        except Exception:
            meta = None
        last = _load_last_jsonl_record(report_path)
        trust = _trust_level_game5m_catboost(meta if isinstance(meta, dict) else None, last if isinstance(last, dict) else None)
        return {
            "enabled": bool(enabled),
            "model_path": model_path,
            "meta_path": meta_path,
            "model_file_exists": Path(model_path).is_file(),
            "meta_file_exists": Path(meta_path).is_file(),
            "last_training_jsonl_path": report_path,
            "last_training": last,
            "meta_summary": {
                "trained_at": (meta or {}).get("trained_at"),
                "n_train": (meta or {}).get("n_train"),
                "n_valid": (meta or {}).get("n_valid"),
                "n_total": (meta or {}).get("n_total"),
                "excluded_false_take_profit_by_session_high": (meta or {}).get("excluded_false_take_profit_by_session_high"),
                "label": (meta or {}).get("label"),
                "min_train_rows_config": (meta or {}).get("min_train_rows_config"),
                "auc_valid": (meta or {}).get("auc_valid"),
            }
            if isinstance(meta, dict)
            else None,
            **trust,
        }
    except Exception as e:
        return {"error": str(e)}

# Краткий конспект для LLM: как устроен отчёт и где живёт торговая логика (без выдумывания путей).
ANALYZER_LLM_ALGORITHM_DIGEST: Dict[str, Any] = {
    "report_role": (
        "Постфактум по закрытым сделкам: загрузка истории, 5m OHLC на интервал вход→выход, "
        "сравнение факта выхода с max/min ценами окна и снимком context_json на входе."
    ),
    "not_in_report": (
        "Отчёт не воспроизводит внутрибаровый тайминг исполнения лимитов и не знает реальной задержки брокера; "
        "не выводить «медленный cron» из late_polling_signals / exit_below_window_mfe_count."
    ),
    "window_metrics": {
        "ohlc_source": "services.recommend_5m.fetch_5m_ohlc, таймзона баров America/New_York",
        "slice": "строки 5m где entry_ts <= datetime <= exit_ts",
        "mfe_price": "max(High) окна → potential_best_pct = (mfe/entry−1)*100",
        "mae_price": "min(Low) окна → preventable_worst_pct = (mae/entry−1)*100",
        "missed_upside_pct": "max(0, potential_best_pct − realized_pct)",
        "avoidable_loss_pct": "max(0, realized_pct − preventable_worst_pct)",
        "exit_vs_mfe_flag": "|exit−mfe|/mfe > 0.004 → likely_late_polling (переименование в UI: ниже MFE окна)",
        "late_polling_signals_summary": (
            "число сделок с likely_late_polling И missed_upside_pct > 0.003 (0.3%)"
        ),
    },
    "entry_snapshot": (
        "Поля входа (rsi_5m, prob_up, decision_rule_params, …) из context_json сделки через "
        "services.deal_params_5m.normalize_entry_context — это снимок на момент входа, не текущий config."
    ),
    "runtime_trading_logic": (
        "Живые сигналы входа: services.recommend_5m.get_decision_5m (версия и пороги — "
        "current_decision_rule_params в meta). Управление позицией/стоп/тейк/время: services.game_5m "
        "(эффективные stop/take и max_position — в current_decision_rule_params.exit_strategy)."
    ),
    "code_map": [
        {
            "path": "services/trade_effectiveness_analyzer.py",
            "role": "Сбор отчёта, агрегаты summary, эвристики practical_parameter_suggestions, game_5m_config_hints, game5m_hanger_tune_json_review (JSON капы vs TAKE_PROFIT_SUSPEND), вызов LLM.",
        },
        {
            "path": "services/recommend_5m.py",
            "role": "Правила STRONG_BUY/BUY/HOLD/SELL, чтение GAME_5M_* порогов, get_decision_5m_rule_thresholds.",
        },
        {
            "path": "services/game_5m.py",
            "role": "Стоп-лосс, тейк-профит, лимиты удержания, выход по времени сессии и связанные ветки.",
        },
        {
            "path": "services/deal_params_5m.py",
            "role": "Нормализация context_json при записи/чтении сделки.",
        },
        {
            "path": "services/game5m_param_hypothesis_backtest.py",
            "role": "Офлайн 5m-реплей: открытые BUY в trade_history (или legacy JSON), недобор (missed upside) в bundle анализатора → mergeable_recommendations; "
            "CLI: scripts/backtest_game5m_param_hypotheses.py --mode open|json|bundle.",
        },
        {
            "path": "report_generator.py",
            "role": "load_trade_history, compute_closed_trade_pnls — источник списка закрытых сделок.",
        },
    ],
    "advice_discipline": [
        "Сначала проверь metric_definitions и этот digest — не путай метрики окна с инфраструктурой cron.",
        "Привязывай выводы к конкретным trade_id / ticker из top_cases, entry_underperformance_review или trade_effects.",
        "Перенастройка: логическое имя параметра → env_key из algorithm_context.parameter_to_env_key (или полное GAME_5M_*).",
        "Если проблема в ветвлении/формуле, а не в пороге — algorithm_change_proposals с path из code_map и именем функции/ветки.",
        "Оценка impact в expected_impact — осторожные числа; validation_plan — повторный прогон анализатора на следующем окне или ограниченный paper-run.",
    ],
    # Как крон решает тейк (без полного исходника — достаточно для подбора GAME_5M_*).
    "game_5m_take_exit_runtime": {
        "primary_code": "services/game_5m.py: _effective_take_profit_pct, _effective_stop_loss_pct, should_close_position",
        "cron_entrypoint": "scripts/send_sndk_signal_cron.py вызывает should_close_position(..., momentum_2h_pct с карточки 5m)",
        "take_threshold_formula": (
            "cap = GAME_5M_TAKE_PROFIT_PCT или GAME_5M_TAKE_PROFIT_PCT_<TICKER> (потолок). "
            "Если momentum_2h_pct >= GAME_5M_TAKE_PROFIT_MIN_PCT: "
            "эффективный_тейк = min(momentum_2h_pct × GAME_5M_TAKE_MOMENTUM_FACTOR, cap). "
            "Иначе эффективный_тейк = cap. "
            "Закрытие TAKE_PROFIT если нереализованный % (по max(close, bar_high) vs entry) >= эффективный_тейк − 0.05."
        ),
        "env_keys_take": [
            "GAME_5M_TAKE_PROFIT_PCT",
            "GAME_5M_TAKE_PROFIT_PCT_<TICKER>",
            "GAME_5M_TAKE_PROFIT_MIN_PCT",
            "GAME_5M_TAKE_MOMENTUM_FACTOR",
        ],
        "env_keys_stop_time": [
            "GAME_5M_STOP_LOSS_ENABLED",
            "GAME_5M_STOP_LOSS_PCT",
            "GAME_5M_STOP_TO_TAKE_RATIO",
            "GAME_5M_STOP_LOSS_MIN_PCT",
            "GAME_5M_MAX_POSITION_MINUTES",
            "GAME_5M_MAX_POSITION_MINUTES_<TICKER>",
            "GAME_5M_MAX_POSITION_DAYS",
            "GAME_5M_MAX_POSITION_DAYS_<TICKER>",
            "GAME_5M_SESSION_END_EXIT_MINUTES",
            "GAME_5M_SESSION_END_MIN_PROFIT_PCT",
            "GAME_5M_EXIT_ONLY_TAKE",
            "GAME_5M_EARLY_DERISK_*",
            "GAME_5M_ALLOW_PYRAMID_BUY",
            "GAME_5M_SOFT_TAKE_NEAR_HIGH_ENABLED",
            "GAME_5M_SOFT_TAKE_NEAR_HIGH_MIN_PCT",
            "GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT",
        ],
        "note_on_meta_take": (
            "В report.meta.current_decision_rule_params.exit_strategy числа take_profit_pct_effective / stop_loss_pct_effective "
            "— снимок при momentum_2h=None (часто совпадает с потолком тейка). Реальный порог в кроне зависит от текущего momentum_2h на баре."
        ),
        "sell_does_not_close": (
            "Сигнал решения SELL по 5m не закрывает уже открытый long; только TAKE_PROFIT / STOP_LOSS / TIME_EXIT / TIME_EXIT_EARLY (см. should_close_position)."
        ),
        "tuning_hint_from_report": (
            "Много TAKE_PROFIT и высокий sum_missed_upside_pct → чаще трогают TAKE_MOMENTUM_FACTOR или потолок тейка; "
            "много убытков при vol — VOLATILITY_*, SELL_CONFIRM_BARS; долгое удержание — MAX_POSITION_*."
        ),
    },
    "game_5m_hanger_tune_json": {
        "code": (
            "services/game_5m.py: _hanger_tune_min_cap_pct (JSON hanger_hypotheses[].remediation_take_cap.proposed_cap_pct), "
            "_apply_hanger_take_cap_to_base; при live apply_hanger_json тейк может закрыться как TAKE_PROFIT_SUSPEND."
        ),
        "config": "GAME_5M_HANGER_TUNE_JSON (путь к JSON), GAME_5M_HANGER_TUNE_APPLY_TAKE=true — сужение потолка тейка по капу из файла.",
        "offline_regen": "scripts/backtest_game5m_param_hypotheses.py --mode bundle (или open/json) — обновление JSON; сравнивайте mtime файла с политикой свежести (напр. 7 дней).",
        "analyzer_block": "report.game5m_hanger_tune_json_review — метрики окна + кандидаты; см. metric_definitions.game5m_hanger_tune_json_review.",
    },
}


def _analyzer_state_path() -> Path:
    """
    Куда писать «память» анализатора о последних параметрах.
    По умолчанию — local/analyzer_state.json (внутри repo). Можно переопределить env ANALYZER_STATE_PATH.
    """
    raw = (os.environ.get("ANALYZER_STATE_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    root = Path(__file__).resolve().parent.parent
    return root / "local" / "analyzer_state.json"


def _load_analyzer_state() -> Dict[str, Any]:
    p = _analyzer_state_path()
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_analyzer_state(state: Dict[str, Any]) -> None:
    p = _analyzer_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # Не валим API из‑за невозможности сохранить «память» (read-only FS, sandbox, etc.)
        return


def _extract_game5m_config_snapshot(current_rules: Dict[str, Any]) -> Dict[str, Any]:
    cfg = current_rules.get("config") if isinstance(current_rules.get("config"), dict) else {}
    exit_s = current_rules.get("exit_strategy") if isinstance(current_rules.get("exit_strategy"), dict) else {}
    return {
        "rule_version": current_rules.get("rule_version"),
        "signal_cron_minutes": current_rules.get("signal_cron_minutes"),
        "config": dict(cfg) if isinstance(cfg, dict) else {},
        "exit_strategy": {
            k: exit_s.get(k)
            for k in (
                "max_position_minutes",
                "stop_loss_enabled",
                "stop_loss_pct_effective",
                "take_profit_pct_effective",
                "GAME_5M_TAKE_MOMENTUM_FACTOR",
                "GAME_5M_EXIT_ONLY_TAKE",
                "GAME_5M_SESSION_END_EXIT_MINUTES",
                "GAME_5M_SESSION_END_MIN_PROFIT_PCT",
                "GAME_5M_EARLY_DERISK_ENABLED",
                "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES",
                "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT",
                "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW",
                "GAME_5M_ALLOW_PYRAMID_BUY",
                "GAME_5M_SOFT_TAKE_NEAR_HIGH_ENABLED",
                "GAME_5M_SOFT_TAKE_NEAR_HIGH_MIN_PCT",
                "GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT",
            )
        },
    }


def _diff_flat_config(prev: Dict[str, Any], cur: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Плоский diff по ключам GAME_5M_* в meta: что изменилось со времени прошлого прогона анализатора."""
    out: List[Dict[str, Any]] = []
    p_cfg = prev.get("config") if isinstance(prev.get("config"), dict) else {}
    c_cfg = cur.get("config") if isinstance(cur.get("config"), dict) else {}
    keys = sorted(set([*p_cfg.keys(), *c_cfg.keys()]))
    for k in keys:
        pv = p_cfg.get(k)
        cv = c_cfg.get(k)
        if pv != cv:
            out.append({"env_key": k, "prev": pv, "current": cv})
    p_ex = prev.get("exit_strategy") if isinstance(prev.get("exit_strategy"), dict) else {}
    c_ex = cur.get("exit_strategy") if isinstance(cur.get("exit_strategy"), dict) else {}
    keys2 = sorted(set([*p_ex.keys(), *c_ex.keys()]))
    for k in keys2:
        pv = p_ex.get(k)
        cv = c_ex.get(k)
        if pv != cv:
            out.append({"env_key": f"exit_strategy.{k}", "prev": pv, "current": cv})
    return out


@dataclass
class TradeEffect:
    trade_id: int
    ticker: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    hold_minutes: float
    qty: float
    entry_price: float
    exit_price: float
    net_pnl: float
    realized_pct: float
    realized_log_return: float
    exit_signal: str
    exit_strategy: str
    potential_best_pct: Optional[float]
    preventable_worst_pct: Optional[float]
    missed_upside_pct: Optional[float]
    avoidable_loss_pct: Optional[float]
    likely_late_polling: bool
    entry_rsi_5m: Optional[float]
    entry_vol_5m_pct: Optional[float]
    entry_momentum_2h_pct: Optional[float]
    entry_price_forecast_5m_summary: Optional[str]
    entry_prob_up: Optional[float]
    entry_prob_down: Optional[float]
    entry_news_impact: Optional[str]
    entry_advice: Optional[str]
    entry_decision: Optional[str]
    entry_reasoning: Optional[str]
    decision_rule_version: Optional[str]
    decision_rule_params: Optional[Dict[str, Any]]
    exit_detail: Optional[str]
    position_state_v2: Optional[Dict[str, Any]]
    continuation_gate: Optional[Dict[str, Any]]


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if math.isfinite(f):
            return f
    except Exception:
        return None
    return None


def _json_dict(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            data = json.loads(v)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _as_et(ts: pd.Timestamp) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("America/New_York")
    return t.tz_convert("America/New_York")


def _load_closed_trades(days: int, strategy_name: Optional[str]) -> List[Any]:
    engine = get_engine()
    if strategy_name and strategy_name.upper() != "ALL":
        raw = load_trade_history(engine, strategy_name=strategy_name)
    else:
        raw = load_trade_history(engine)
    closed = compute_closed_trade_pnls(raw)
    if not closed:
        return []
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    out = []
    for t in closed:
        ts = pd.Timestamp(t.ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        if ts >= cutoff:
            out.append(t)
    return out


def _filter_closed_trades_for_focus(
    closed: List[Any],
    tickers: Optional[List[str]] = None,
    trade_ids: Optional[List[int]] = None,
) -> List[Any]:
    """Узкая выборка: по списку тикеров и/или id закрывающей сделки (trade_id из TradePnL)."""
    if not tickers and not trade_ids:
        return closed
    tid_set = {int(x) for x in trade_ids} if trade_ids else None
    tkr_set = {str(t).strip().upper() for t in tickers if str(t).strip()} if tickers else None
    out: List[Any] = []
    for t in closed:
        if tid_set is not None:
            try:
                tid = int(getattr(t, "trade_id", 0) or 0)
            except (TypeError, ValueError):
                tid = 0
            if tid not in tid_set:
                continue
        if tkr_set is not None:
            tkr = str(getattr(t, "ticker", "") or "").strip().upper()
            if tkr not in tkr_set:
                continue
        out.append(t)
    return out


def _read_market_bars_5m_cached(
    cache: Dict[str, Optional[pd.DataFrame]],
    ticker: str,
    *,
    days_back: int,
) -> Optional[pd.DataFrame]:
    """
    Загрузка 5m из Postgres (market_bars_5m), опционально для офлайн-анализа.

    При TRADE_ANALYZER_5M_OHLC_SOURCE=postgres_first кэш собирается сначала отсюда; иначе —
    fallback после yfinance (игра и рекомендации всегда на fetch_5m_ohlc).

    Кешируем по тикеру в переданном dict cache.
    """
    t = (ticker or "").strip().upper()
    if not t:
        return None
    if t in cache:
        return cache[t]
    days_back = int(days_back or 1)
    # Analyzer может смотреть окна entry→exit на 2–4 недели; берём шире, чем 7 дней.
    days_back = min(max(1, days_back), 30)
    engine = get_engine()
    # Берём с запасом: анализатор может смотреть окна entry→exit, где entry может быть чуть раньше cutoff по дням.
    cutoff_utc = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_back + 2)
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            df = pd.read_sql(
                text(
                    """
                    SELECT bar_start_utc AS datetime,
                           open AS "Open",
                           high AS "High",
                           low  AS "Low",
                           close AS "Close",
                           volume AS "Volume"
                    FROM public.market_bars_5m
                    WHERE exchange = 'US'
                      AND symbol = :sym
                      AND bar_start_utc >= :cutoff
                    ORDER BY bar_start_utc ASC
                    """
                ),
                conn,
                params={"sym": t, "cutoff": cutoff_utc},
            )
    except Exception:
        cache[t] = None
        return None
    if df is None or df.empty:
        cache[t] = None
        return None
    try:
        d = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert("America/New_York")
        df = df.copy()
        df["datetime"] = d
    except Exception:
        pass
    cache[t] = df.reset_index(drop=True)
    return cache[t]


def _trade_analyzer_5m_ohlc_db_first() -> bool:
    """
    Источник 5m для офлайн-анализа сделок (не live-игра).

    По умолчанию — как в игре: сначала yfinance (fetch_5m_ohlc), Postgres — только fallback.
    postgres_first — прежнее поведение (сначала market_bars_5m для воспроизводимости).
    """
    raw = (get_config_value("TRADE_ANALYZER_5M_OHLC_SOURCE", "yfinance") or "yfinance").strip().lower()
    if raw in ("postgres_first", "db_first", "database", "market_bars", "postgres", "pg", "db"):
        return True
    if raw in (
        "yfinance_first",
        "yfinance",
        "yahoo",
        "yf",
        "game",
        "game_aligned",
    ):
        return False
    return False


def _prepare_ohlc_cache(tickers: List[str], days: int) -> Dict[str, Optional[pd.DataFrame]]:
    cache: Dict[str, Optional[pd.DataFrame]] = {}
    fetch_days = min(30, max(3, days + 2))
    db_first = _trade_analyzer_5m_ohlc_db_first()
    for t in sorted(set(tickers)):
        try:
            df: Optional[pd.DataFrame] = None
            if db_first:
                df_db = _read_market_bars_5m_cached(cache, t, days_back=fetch_days)
                df = df_db
                if df is None or getattr(df, "empty", True):
                    df = fetch_5m_ohlc(t, days=fetch_days)
            else:
                df = fetch_5m_ohlc(t, days=fetch_days)
                if df is None or getattr(df, "empty", True):
                    df_db = _read_market_bars_5m_cached(cache, t, days_back=fetch_days)
                    df = df_db
            if df is None or df.empty:
                cache[t] = None
                continue
            d = df.copy()
            dt = pd.to_datetime(d["datetime"])
            if dt.dt.tz is None:
                dt = dt.dt.tz_localize("America/New_York", ambiguous="infer")
            else:
                dt = dt.dt.tz_convert("America/New_York")
            d["datetime"] = dt
            cache[t] = d.sort_values("datetime").reset_index(drop=True)
        except Exception:
            cache[t] = None
    return cache


def _slice_window(df: Optional[pd.DataFrame], start_et: pd.Timestamp, end_et: pd.Timestamp) -> Optional[pd.DataFrame]:
    if df is None or df.empty or end_et <= start_et:
        return None
    m = (df["datetime"] >= start_et) & (df["datetime"] <= end_et)
    w = df.loc[m]
    if w.empty:
        return None
    return w


def _minutes_after_rth_open_et(ts_et: pd.Timestamp) -> Optional[float]:
    """Минуты после 09:30 ET в тот же календарный день (для контекста открытия сессии)."""
    try:
        day = ts_et.normalize()
        open_et = day + pd.Timedelta(hours=9, minutes=30)
        return float((ts_et - open_et) / pd.Timedelta(minutes=1))
    except Exception:
        return None


def _dedupe_time_exit_proposals(proposals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_k: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for p in proposals:
        if not isinstance(p, dict):
            continue
        k = str(p.get("env_key") or "").strip()
        if not k:
            continue
        if k not in by_k:
            order.append(k)
        by_k[k] = p
    return [by_k[k] for k in order]


TIME_EXIT_EARLY_ENV_TO_PRACTICAL_PARAMETER: Dict[str, str] = {
    "GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES": "stale_reversal_min_age_minutes",
    "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT": "stale_reversal_max_pnl_pct",
    "GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW": "stale_reversal_momentum_below",
    "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES": "early_derisk_min_age_minutes",
    "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT": "early_derisk_max_loss_pct",
    "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW": "early_derisk_momentum_below",
    "GAME_5M_EXIT_GUARD_FIRST_MINUTES": "exit_guard_first_minutes",
}


def _practical_suggestions_from_time_exit_early_review(review: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Переводит config_candidates.proposals в формат practical_parameter_suggestions (auto_config_override)."""
    out: List[Dict[str, Any]] = []
    cc = review.get("config_candidates") if isinstance(review, dict) else None
    if not isinstance(cc, dict):
        return out
    for p in cc.get("proposals") or []:
        if not isinstance(p, dict):
            continue
        env_key = str(p.get("env_key") or "").strip()
        if not env_key:
            continue
        param = TIME_EXIT_EARLY_ENV_TO_PRACTICAL_PARAMETER.get(env_key)
        if not param:
            param = env_key if env_key.startswith("GAME_5M_") else env_key
        conf = str(p.get("sample_confidence") or "")
        reason = str(p.get("reason") or "")
        if conf == "low":
            reason = f"{reason} [уверенность: низкая — мало сделок]"
        elif conf == "medium_low":
            reason = f"{reason} [уверенность: ниже средней]"
        out.append(
            {
                "parameter": param,
                "current": p.get("current"),
                "proposed": p.get("proposed"),
                "why": reason,
                "expected_effect": (
                    "Смягчить преждевременный TIME_EXIT_EARLY при типичном отскоке после выхода (post-exit MFE / recovery к входу)."
                ),
            }
        )
    return out


def _config_hints_from_time_exit_early_review(review: Dict[str, Any]) -> List[Dict[str, Any]]:
    hints: List[Dict[str, Any]] = []
    cc = review.get("config_candidates") if isinstance(review, dict) else None
    if not isinstance(cc, dict):
        return hints
    for p in cc.get("proposals") or []:
        if not isinstance(p, dict):
            continue
        env_key = str(p.get("env_key") or "").strip()
        if not env_key:
            continue
        hints.append(
            {
                "env_key": env_key,
                "direction": "time_exit_early_counterfactual",
                "evidence": str(p.get("reason") or "")[:420],
                "rationale": (
                    "Порог предложен по time_exit_early_review (5m OHLC после exit). Проверьте sample_confidence и by_exit_detail."
                ),
            }
        )
    return hints


def _time_exit_proposal_confidence_rank(sample_confidence: Optional[str]) -> int:
    c = str(sample_confidence or "").strip().lower()
    return {"high": 4, "medium": 3, "medium_low": 2, "low": 1}.get(c, 0)


def _cap_time_exit_early_proposals_by_rank(
    proposals: List[Dict[str, Any]],
    *,
    max_n: int,
) -> List[Dict[str, Any]]:
    valid = [p for p in proposals if isinstance(p, dict)]
    if len(valid) <= max_n:
        return valid
    valid.sort(
        key=lambda p: _time_exit_proposal_confidence_rank(str(p.get("sample_confidence"))),
        reverse=True,
    )
    return valid[:max_n]


def _build_time_exit_early_action_summary(te_review: Dict[str, Any]) -> Dict[str, Any]:
    """Фаза B1/B3: одна сводка для оператора по TIME_EXIT_EARLY (без внешних таблиц)."""
    if not isinstance(te_review, dict):
        return {"enabled": False}
    out: Dict[str, Any] = {"enabled": bool(te_review.get("enabled", True))}
    count = int(te_review.get("count") or 0)
    rows_ohlc = int(te_review.get("rows_with_ohlc") or 0)
    whipsaw_total = int(te_review.get("premature_whipsaw_1h_count") or 0)
    whipsaw_rate = round(float(whipsaw_total) / float(rows_ohlc), 4) if rows_ohlc > 0 else None

    exit_detail_counts: Dict[str, int] = {}
    by_d = te_review.get("by_exit_detail") or {}
    if isinstance(by_d, dict):
        for k, v in by_d.items():
            if isinstance(v, dict):
                exit_detail_counts[str(k)] = int(v.get("count") or 0)

    top_candidates = te_review.get("top_early_exit_candidates_1h") or []
    top_tickers: List[Dict[str, Any]] = []
    seen_t = set()
    for r in top_candidates[:10]:
        if not isinstance(r, dict):
            continue
        t = str(r.get("ticker") or "").strip().upper()
        if not t or t in seen_t:
            continue
        seen_t.add(t)
        top_tickers.append(
            {
                "ticker": t,
                "trade_id": r.get("trade_id"),
                "post_exit_mfe_pct_1h": r.get("post_exit_mfe_pct_1h"),
                "exit_detail": r.get("exit_detail"),
            }
        )

    proposals = (te_review.get("config_candidates") or {}).get("proposals") or []
    actionable = [
        p
        for p in proposals
        if isinstance(p, dict) and str(p.get("sample_confidence") or "") in ("high", "medium")
    ]
    has_actionable = len(actionable) > 0

    sr = int(exit_detail_counts.get("stale_reversal", 0))
    ed = int(exit_detail_counts.get("early_derisk", 0))
    known = sr + ed
    total_detail = sum(exit_detail_counts.values())
    unknown = max(0, total_detail - known)
    dominant: Optional[str] = None
    tune_hint: Optional[str] = None
    if known > 0 and unknown <= max(1, int(0.35 * max(known, 1))):
        if sr >= ed * 1.5 and sr > 0:
            dominant = "stale_reversal"
            tune_hint = (
                "Приоритет правок: ветка stale_reversal (GAME_5M_STALE_REVERSAL_*) — доля среди известных exit_detail выше."
            )
        elif ed >= sr * 1.5 and ed > 0:
            dominant = "early_derisk"
            tune_hint = (
                "Приоритет правок: ветка early_derisk (GAME_5M_EARLY_DERISK_*) — доля среди известных exit_detail выше."
            )
        elif sr > 0 and ed > 0:
            dominant = "mixed"
            tune_hint = "Обе ветки заметны — согласованно пересмотреть STALE_REVERSAL_* и EARLY_DERISK_*."
        elif sr > 0:
            dominant = "stale_reversal"
            tune_hint = "Только stale_reversal в выборке — крутить `GAME_5M_STALE_REVERSAL_*`."
        elif ed > 0:
            dominant = "early_derisk"
            tune_hint = "Только early_derisk в выборке — крутить `GAME_5M_EARLY_DERISK_*`."

    focused_rows = te_review.get("focused_trade_rows")
    focused_n = len(focused_rows) if isinstance(focused_rows, list) else 0

    out.update(
        {
            "time_exit_early_trades_total": count,
            "rows_with_ohlc": rows_ohlc,
            "exit_detail_counts": exit_detail_counts,
            "premature_whipsaw_1h_count": whipsaw_total,
            "premature_whipsaw_rate_given_ohlc": whipsaw_rate,
            "top_rebound_candidates": top_tickers[:5],
            "has_actionable_proposals_high_or_medium": has_actionable,
            "actionable_proposals_count": len(actionable),
            "dominant_exit_detail": dominant,
            "tune_priority_hint": tune_hint,
            "insufficient_data_for_ml": bool(te_review.get("insufficient_data_for_ml")),
            "proposals_capped_small_sample": bool(te_review.get("proposals_capped_small_sample")),
            "ready_for_parameter_review": bool(
                rows_ohlc >= 3 and (has_actionable or whipsaw_total >= 2)
            ),
            "focused_trade_rows_matched": focused_n,
        }
    )
    return out


def _build_time_exit_early_review(
    effects: List[TradeEffect],
    ohlc_cache: Dict[str, Optional[pd.DataFrame]],
    *,
    limit: int = 12,
    focused_trade_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    exits = [e for e in effects if str(e.exit_signal or "").upper() == "TIME_EXIT_EARLY"]
    if not exits:
        return {
            "enabled": True,
            "count": 0,
            "rows_with_ohlc": 0,
            "premature_whipsaw_1h_count": 0,
            "insufficient_data_for_ml": True,
            "proposals_capped_small_sample": False,
            "by_exit_detail": {},
            "config_candidates": {
                "stale_reversal_enabled": False,
                "early_derisk_enabled": False,
                "proposals": [],
            },
            "note": "Нет сделок TIME_EXIT_EARLY в выборке.",
            "detail_rows": [],
        }

    rows: List[Dict[str, Any]] = []
    for e in exits:
        df = ohlc_cache.get(e.ticker)
        if df is None or df.empty:
            continue
        exit_px = float(e.exit_price or 0.0)
        entry_px = float(e.entry_price or 0.0)
        if exit_px <= 0:
            continue
        t0_raw = pd.Timestamp(e.exit_ts)
        if t0_raw.tzinfo is None:
            t0_exit = t0_raw.tz_localize(
                "America/New_York", ambiguous="infer", nonexistent="shift_forward"
            )
        else:
            t0_exit = t0_raw.tz_convert("America/New_York")
        post_exit_anchor_mode = "on_exit_ts"
        # Если exit_ts попал в «дыру» (нет 5m-бара >= exit, типично ночь/выход между сессиями),
        # якорим контрфакт на первый бар после последнего бара <= exit_ts (как и при RTH-only Yahoo).
        try:
            nxt = df.loc[df["datetime"] >= t0_exit]
            if nxt is not None and not nxt.empty:
                t0_eff = pd.Timestamp(nxt["datetime"].iloc[0])
            else:
                prev = df.loc[df["datetime"] <= t0_exit]
                if prev is None or prev.empty:
                    continue
                last_at_or_before = pd.Timestamp(prev["datetime"].iloc[-1])
                after_prev = df.loc[df["datetime"] > last_at_or_before]
                if after_prev is None or after_prev.empty:
                    continue
                t0_eff = pd.Timestamp(after_prev["datetime"].iloc[0])
                post_exit_anchor_mode = "first_bar_after_gap"
        except Exception:
            continue
        start_shift_min = None
        try:
            start_shift_min = float((t0_eff - t0_exit) / pd.Timedelta(minutes=1))
        except Exception:
            start_shift_min = None
        t0 = t0_eff
        t1 = t0 + pd.Timedelta(hours=1)
        w1h = _slice_window(df, t0, t1)
        post_mfe_1h = post_mae_1h = None
        post_recovery_entry_pct_1h = None
        minutes_to_breakeven = None
        if w1h is not None and not w1h.empty:
            try:
                high1 = float(w1h["High"].max())
                low1 = float(w1h["Low"].min())
                post_mfe_1h = (high1 / exit_px - 1.0) * 100.0
                post_mae_1h = (low1 / exit_px - 1.0) * 100.0
                if entry_px > 0:
                    post_recovery_entry_pct_1h = (high1 / entry_px - 1.0) * 100.0
                # break-even: first bar where High >= exit_px
                hits = w1h.loc[w1h["High"] >= exit_px]
                if hits is not None and not hits.empty:
                    ts_hit = hits["datetime"].iloc[0]
                    try:
                        minutes_to_breakeven = float((pd.Timestamp(ts_hit) - t0) / pd.Timedelta(minutes=1))
                    except Exception:
                        minutes_to_breakeven = None
            except Exception:
                post_mfe_1h = post_mae_1h = None

        # Optional: to end of RTH day (16:00 ET of same date)
        post_mfe_eod = post_mae_eod = None
        try:
            day = t0.normalize()
            eod = day + pd.Timedelta(hours=16)
            if eod > t0 and (eod - t0) <= pd.Timedelta(hours=14):
                we = _slice_window(df, t0, eod)
                if we is not None and not we.empty:
                    hi = float(we["High"].max())
                    lo = float(we["Low"].min())
                    post_mfe_eod = (hi / exit_px - 1.0) * 100.0
                    post_mae_eod = (lo / exit_px - 1.0) * 100.0
        except Exception:
            post_mfe_eod = post_mae_eod = None

        minutes_after_rth_open = _minutes_after_rth_open_et(t0)
        likely_premature_1h = False
        if post_mfe_1h is not None and post_mae_1h is not None:
            likely_premature_1h = float(post_mfe_1h) >= 1.2 and float(post_mae_1h) > -2.0

        rows.append(
            {
                "trade_id": e.trade_id,
                "ticker": e.ticker,
                "exit_ts": e.exit_ts.isoformat(),
                "exit_detail": e.exit_detail,
                "realized_pct": round(float(e.realized_pct), 3),
                "entry_price": round(entry_px, 4) if entry_px > 0 else None,
                "exit_price": round(exit_px, 4),
                "minutes_after_rth_open_et": None
                if minutes_after_rth_open is None
                else round(float(minutes_after_rth_open), 1),
                "post_exit_start_bar_et": str(t0),
                "post_exit_anchor_mode": post_exit_anchor_mode,
                "post_exit_start_shift_min": None if start_shift_min is None else round(float(start_shift_min), 1),
                "post_exit_mfe_pct_1h": None if post_mfe_1h is None else round(float(post_mfe_1h), 3),
                "post_exit_mae_pct_1h": None if post_mae_1h is None else round(float(post_mae_1h), 3),
                "post_exit_recovery_entry_pct_1h": None
                if post_recovery_entry_pct_1h is None
                else round(float(post_recovery_entry_pct_1h), 3),
                "likely_premature_whipsaw_1h": bool(likely_premature_1h),
                "minutes_to_break_even_1h": None if minutes_to_breakeven is None else round(float(minutes_to_breakeven), 1),
                "post_exit_mfe_pct_eod": None if post_mfe_eod is None else round(float(post_mfe_eod), 3),
                "post_exit_mae_pct_eod": None if post_mae_eod is None else round(float(post_mae_eod), 3),
            }
        )

    if not rows:
        return {
            "enabled": True,
            "count": len(exits),
            "rows_with_ohlc": 0,
            "premature_whipsaw_1h_count": 0,
            "insufficient_data_for_ml": True,
            "proposals_capped_small_sample": False,
            "by_exit_detail": {},
            "config_candidates": {"proposals": []},
            "note": (
                "TIME_EXIT_EARLY есть, но ни одна сделка не сопоставилась с рядом 5m: нет кэша по тикеру, "
                "exit_ts вне окна загрузки, или нет баров после exit (расширьте days; проверьте exit_ts и fetch_5m_ohlc)."
            ),
            "detail_rows": [],
        }

    def _avg(vals: List[Optional[float]]) -> Optional[float]:
        xs = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
        return round(float(np.mean(xs)), 4) if xs else None

    by_detail: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        k = str(r.get("exit_detail") or "unknown")
        by_detail.setdefault(k, []).append(r)
    by_detail_summary = {}
    for k, lst in by_detail.items():
        premature_n = sum(1 for x in lst if x.get("likely_premature_whipsaw_1h"))
        by_detail_summary[k] = {
            "count": len(lst),
            "premature_whipsaw_1h_count": premature_n,
            "avg_post_exit_mfe_pct_1h": _avg([x.get("post_exit_mfe_pct_1h") for x in lst]),
            "avg_post_exit_mae_pct_1h": _avg([x.get("post_exit_mae_pct_1h") for x in lst]),
            "avg_post_exit_recovery_entry_pct_1h": _avg([x.get("post_exit_recovery_entry_pct_1h") for x in lst]),
            "avg_minutes_to_break_even_1h": _avg([x.get("minutes_to_break_even_1h") for x in lst]),
        }

    early_candidates = sorted(rows, key=lambda r: float(r.get("post_exit_mfe_pct_1h") or 0.0), reverse=True)[:limit]
    protective_candidates = sorted(rows, key=lambda r: float(r.get("post_exit_mae_pct_1h") or 0.0))[:limit]
    whipsaw_rows = [r for r in rows if r.get("likely_premature_whipsaw_1h")]

    # Candidate tuning proposals: числовые уровни GAME_5M_* при систематич. отскоке после TIME_EXIT_EARLY.
    proposals: List[Dict[str, Any]] = []
    try:
        from config_loader import get_config_value

        def _cfg_int(key: str, default: int) -> int:
            try:
                return int((get_config_value(key, str(default)) or str(default)).strip())
            except Exception:
                return int(default)

        def _cfg_float(key: str, default: float) -> float:
            try:
                return float(
                    ((get_config_value(key, str(default)) or str(default)).strip().replace(",", "."))
                )
            except Exception:
                return float(default)

        cur_min_age_default = _cfg_int("GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES", 390)
        cur_max_pnl = _cfg_float("GAME_5M_STALE_REVERSAL_MAX_PNL_PCT", -1.5)
        cur_mom_below = _cfg_float("GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW", 0.0)
        cur_enabled = (get_config_value("GAME_5M_STALE_REVERSAL_EXIT_ENABLED", "false") or "false").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        cur_ed_min_age = _cfg_int("GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES", 180)
        cur_ed_max_loss = _cfg_float("GAME_5M_EARLY_DERISK_MAX_LOSS_PCT", -2.0)
        cur_ed_mom = _cfg_float("GAME_5M_EARLY_DERISK_MOMENTUM_BELOW", 0.0)
        cur_ed_on = (get_config_value("GAME_5M_EARLY_DERISK_ENABLED", "false") or "false").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        cur_exit_guard = _cfg_int("GAME_5M_EXIT_GUARD_FIRST_MINUTES", 5)
    except Exception:
        cur_min_age_default, cur_max_pnl, cur_mom_below, cur_enabled = 390, -1.5, 0.0, False
        cur_ed_min_age, cur_ed_max_loss, cur_ed_mom, cur_ed_on = 180, -2.0, 0.0, False
        cur_exit_guard = 5

    tickers_in_exits = sorted({str(r.get("ticker") or "").strip().upper() for r in rows if str(r.get("ticker") or "").strip()})
    cur_min_age_by_ticker: Dict[str, int] = {}
    for t in tickers_in_exits:
        try:
            key_t = f"GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES_{t}"
            raw_t = ""
            try:
                from config_loader import get_config_value as _gcv

                raw_t = (_gcv(key_t, "") or "").strip()
            except Exception:
                raw_t = ""
            cur_min_age_by_ticker[t] = int(raw_t) if raw_t else int(cur_min_age_default)
        except Exception:
            cur_min_age_by_ticker[t] = int(cur_min_age_default)

    def _stale_premature_tier(n: int, mfe: Optional[float], mae: Optional[float]) -> Optional[str]:
        if n <= 0 or mfe is None:
            return None
        if n >= 5 and mfe >= 3.0 and (mae is None or mae > -1.0):
            return "high"
        if n >= 3 and mfe >= 2.2 and (mae is None or mae > -1.2):
            return "medium"
        if n >= 2 and mfe >= 2.8 and (mae is None or mae > -1.5):
            return "medium_low"
        return None

    def _append_stale_proposals(sample_confidence: str, n: int, avg_mfe: Optional[float], avg_mae: Optional[float]) -> None:
        mae_disp = "—" if avg_mae is None else f"{avg_mae:.2f}%"
        base_reason = (
            f"stale_reversal (n={n}): средний post-exit MFE за 1h={avg_mfe:.2f}% от цены выхода при MAE={mae_disp} — "
            f"частые отскоки после выхода; смягчаем условия реза."
        )
        no_override = [t for t in tickers_in_exits if cur_min_age_by_ticker.get(t, cur_min_age_default) == cur_min_age_default]
        if no_override:
            p_min_age = int(min(24 * 60, max(cur_min_age_default, cur_min_age_default + 180)))
            if p_min_age != cur_min_age_default:
                proposals.append(
                    {
                        "env_key": "GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES",
                        "current": cur_min_age_default,
                        "proposed": p_min_age,
                        "applies_to": {"tickers_without_override": no_override},
                        "reason": base_reason,
                        "sample_confidence": sample_confidence,
                    }
                )
        p_mom = float(min(cur_mom_below, -0.3)) if cur_mom_below >= -0.3 else float(cur_mom_below)
        if p_mom != cur_mom_below:
            proposals.append(
                {
                    "env_key": "GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW",
                    "current": cur_mom_below,
                    "proposed": p_mom,
                    "reason": (
                        "stale_reversal: требовать более отрицательный momentum_2h, чтобы не резать при «плоском» импульсе перед отскоком."
                    ),
                    "sample_confidence": sample_confidence,
                }
            )
        p_pnl = float(min(cur_max_pnl, cur_max_pnl - 0.5))
        if abs(p_pnl - cur_max_pnl) > 1e-9:
            proposals.append(
                {
                    "env_key": "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT",
                    "current": cur_max_pnl,
                    "proposed": p_pnl,
                    "reason": (
                        "stale_reversal: порог MAX_PNL_PCT сделать глубже (ещё −0.5 п.п.): резать только при худшей просадке."
                    ),
                    "sample_confidence": sample_confidence,
                }
            )

    stale_list = by_detail.get("stale_reversal", [])
    n_stale = len(stale_list)
    agg_st = by_detail_summary.get("stale_reversal", {}) if isinstance(by_detail_summary, dict) else {}
    avg_mfe_s = _safe_float(agg_st.get("avg_post_exit_mfe_pct_1h")) if isinstance(agg_st, dict) else None
    avg_mae_s = _safe_float(agg_st.get("avg_post_exit_mae_pct_1h")) if isinstance(agg_st, dict) else None
    tier = _stale_premature_tier(n_stale, avg_mfe_s, avg_mae_s)
    if tier and avg_mfe_s is not None:
        _append_stale_proposals(tier, n_stale, avg_mfe_s, avg_mae_s)
    elif n_stale == 1 and stale_list:
        r0 = stale_list[0]
        mfe0 = _safe_float(r0.get("post_exit_mfe_pct_1h"))
        mae0 = _safe_float(r0.get("post_exit_mae_pct_1h"))
        rec0 = _safe_float(r0.get("post_exit_recovery_entry_pct_1h"))
        single_ok = (
            mfe0 is not None
            and mfe0 >= 2.0
            and (mae0 is None or mae0 > -2.0)
            and (rec0 is None or rec0 >= -0.3 or mfe0 >= 3.0)
        )
        if single_ok and mfe0 is not None:
            _append_stale_proposals("low", 1, mfe0, mae0)

    def _early_premature_tier(n: int, mfe: Optional[float], mae: Optional[float]) -> Optional[str]:
        if n <= 0 or mfe is None:
            return None
        if n >= 4 and mfe >= 1.8 and (mae is None or mae > -1.8):
            return "high"
        if n >= 2 and mfe >= 1.5 and (mae is None or mae > -2.0):
            return "medium"
        return None

    def _append_early_derisk_proposals(sample_confidence: str, n: int, avg_mfe: Optional[float], avg_mae: Optional[float]) -> None:
        if not cur_ed_on:
            return
        mae_disp = "—" if avg_mae is None else f"{avg_mae:.2f}%"
        base = (
            f"early_derisk (n={n}): средний post-exit MFE 1h={avg_mfe:.2f}% при MAE={mae_disp} — выходы часто опережают отскок; "
            f"смягчаем de-risk."
        )
        p_loss = float(max(-5.0, cur_ed_max_loss - 0.5))
        if p_loss < cur_ed_max_loss - 1e-6:
            proposals.append(
                {
                    "env_key": "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT",
                    "current": cur_ed_max_loss,
                    "proposed": round(p_loss, 2),
                    "reason": base + " Углубление MAX_LOSS_PCT на 0.5 п.п. (реже срабатывание в мягкой просадке).",
                    "sample_confidence": sample_confidence,
                }
            )
        p_age = int(min(24 * 60, cur_ed_min_age + 60))
        if p_age != cur_ed_min_age:
            proposals.append(
                {
                    "env_key": "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES",
                    "current": cur_ed_min_age,
                    "proposed": p_age,
                    "reason": base + " Позже разрешаем de-risk (+60 мин к min_age).",
                    "sample_confidence": sample_confidence,
                }
            )
        p_em = float(min(cur_ed_mom, -0.35)) if cur_ed_mom >= -0.25 else float(cur_ed_mom)
        if p_em < cur_ed_mom - 1e-6:
            proposals.append(
                {
                    "env_key": "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW",
                    "current": cur_ed_mom,
                    "proposed": round(p_em, 2),
                    "reason": base + " Более отрицательный порог momentum_2h.",
                    "sample_confidence": sample_confidence,
                }
            )

    ed_list = by_detail.get("early_derisk", [])
    n_ed = len(ed_list)
    agg_ed = by_detail_summary.get("early_derisk", {}) if isinstance(by_detail_summary, dict) else {}
    avg_mfe_e = _safe_float(agg_ed.get("avg_post_exit_mfe_pct_1h")) if isinstance(agg_ed, dict) else None
    avg_mae_e = _safe_float(agg_ed.get("avg_post_exit_mae_pct_1h")) if isinstance(agg_ed, dict) else None
    ed_tier = _early_premature_tier(n_ed, avg_mfe_e, avg_mae_e)
    if ed_tier and avg_mfe_e is not None:
        _append_early_derisk_proposals(ed_tier, n_ed, avg_mfe_e, avg_mae_e)
    elif n_ed == 1 and ed_list:
        r0 = ed_list[0]
        mfe0 = _safe_float(r0.get("post_exit_mfe_pct_1h"))
        mae0 = _safe_float(r0.get("post_exit_mae_pct_1h"))
        rec0 = _safe_float(r0.get("post_exit_recovery_entry_pct_1h"))
        if (
            mfe0 is not None
            and mfe0 >= 2.0
            and (mae0 is None or mae0 > -2.2)
            and (rec0 is None or rec0 >= -0.5)
        ):
            _append_early_derisk_proposals("low", 1, mfe0, mae0)

    open_flush = [
        r
        for r in rows
        if _safe_float(r.get("minutes_after_rth_open_et")) is not None
        and 0 <= float(r["minutes_after_rth_open_et"]) <= 45
        and r.get("likely_premature_whipsaw_1h")
    ]
    if len(open_flush) >= 2 or (len(open_flush) >= 1 and len(rows) <= 3):
        p_g = int(min(30, max(cur_exit_guard + 10, 15)))
        if p_g > cur_exit_guard:
            proposals.append(
                {
                    "env_key": "GAME_5M_EXIT_GUARD_FIRST_MINUTES",
                    "current": cur_exit_guard,
                    "proposed": p_g,
                    "reason": (
                        f"TIME_EXIT_EARLY в первые ~45 мин RTH с отскоком после выхода: {len(open_flush)} кейс(ов) — "
                        f"расширить EXIT_GUARD у открытия (меньше решений на «шумной» сетке сразу после 09:30 ET)."
                    ),
                    "sample_confidence": "medium_low" if len(open_flush) < 2 else "medium",
                }
            )

    n_ohlc = len(rows)
    insufficient_data_for_ml = n_ohlc < TIME_EXIT_EARLY_MIN_ROWS_ML
    proposals_capped_small_sample = False
    if n_ohlc > 0:
        if n_ohlc < TIME_EXIT_EARLY_MIN_ROWS_HARD_CAP and len(proposals) > 1:
            proposals = _cap_time_exit_early_proposals_by_rank(proposals, max_n=1)
            proposals_capped_small_sample = True
        elif n_ohlc < TIME_EXIT_EARLY_MIN_ROWS_SOFT_CAP and len(proposals) > 2:
            proposals = _cap_time_exit_early_proposals_by_rank(proposals, max_n=2)
            proposals_capped_small_sample = True

    proposals = _dedupe_time_exit_proposals(proposals)

    out: Dict[str, Any] = {
        "enabled": True,
        "count": len(exits),
        "rows_with_ohlc": n_ohlc,
        "premature_whipsaw_1h_count": len(whipsaw_rows),
        "insufficient_data_for_ml": insufficient_data_for_ml,
        "proposals_capped_small_sample": proposals_capped_small_sample,
        "by_exit_detail": dict(sorted(by_detail_summary.items(), key=lambda kv: (-int(kv[1]["count"]), kv[0]))),
        "top_early_exit_candidates_1h": early_candidates,
        "top_protective_exits_1h": protective_candidates,
        "config_candidates": {
            "stale_reversal_enabled": bool(cur_enabled),
            "early_derisk_enabled": bool(cur_ed_on),
            "exit_guard_first_minutes_current": int(cur_exit_guard),
            "current_min_age_default": int(cur_min_age_default),
            "current_min_age_by_ticker": cur_min_age_by_ticker,
            "proposals": proposals,
        },
        "note": (
            "post-exit MFE/MAE — от цены выхода; post_exit_recovery_entry_pct_1h — насколько за 1h поднялся High относительно входа. "
            "Контрфакт, без учёта проскальзывания/исполнения."
        ),
        # Полные строки TE для сопоставления с D4a (recovery_ml_d4a_live_review); на UI не выводятся целиком — см. JSON.
        "detail_rows": rows[:300],
    }
    if focused_trade_ids:
        fs = {int(x) for x in focused_trade_ids}
        out["focused_trade_rows"] = [r for r in rows if int(r.get("trade_id") or -1) in fs]
        out["focused_trade_ids_filter"] = [int(x) for x in focused_trade_ids]
    return out


def _effects_game5m_for_recovery(effects: List[TradeEffect], strategy: str) -> List[TradeEffect]:
    su = (strategy or "").strip().upper()
    if su not in ("GAME_5M", "ALL"):
        return []
    return [e for e in effects if str(e.exit_strategy or "").strip().upper() == "GAME_5M"]


def _recovery_ml_horizons_from_config() -> Tuple[int, ...]:
    raw = (get_config_value("GAME_5M_RECOVERY_ML_HORIZONS_MINUTES", "") or "").strip()
    if not raw:
        return RECOVERY_ML_DEFAULT_HORIZONS_MINUTES
    out: List[int] = []
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            v = int(p)
            if v >= 15:
                out.append(v)
        except ValueError:
            continue
    return tuple(out) if out else RECOVERY_ML_DEFAULT_HORIZONS_MINUTES


def _recovery_ml_near_early_filter_from_config() -> Optional[float]:
    raw = (get_config_value("GAME_5M_RECOVERY_DATASET_NEAR_EARLY_PCT", "") or "").strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", "."))
    except (ValueError, TypeError):
        return None


def _default_recovery_ml_export_path() -> Path:
    log_dir = (get_config_value("LOG_DIR", "logs") or "logs").strip()
    p = Path(log_dir)
    p.mkdir(parents=True, exist_ok=True)
    fn = f"game5m_recovery_ml_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    return p / fn


def _export_game5m_recovery_ml_jsonl(
    rows: List[Dict[str, Any]],
    *,
    export_path: Optional[str] = None,
) -> Dict[str, Any]:
    if not rows:
        return {"status": "skipped", "reason": "no rows"}
    path = Path(export_path).expanduser() if export_path else _default_recovery_ml_export_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            n += 1
    return {
        "status": "ok",
        "path": str(path.resolve()),
        "lines_written": n,
    }


def _empty_game5m_hold_recovery_stats(reason: str) -> Dict[str, Any]:
    return {
        "enabled": False,
        "skip_reason": reason,
        "schema_version": RECOVERY_ML_SCHEMA_VERSION,
        "schema_doc": RECOVERY_ML_SCHEMA,
    }


def _attach_game5m_hold_recovery_to_payload(
    payload: Dict[str, Any],
    effects: List[TradeEffect],
    ohlc_cache: Dict[str, Optional[pd.DataFrame]],
    *,
    strategy: str,
    export_recovery_ml: bool = False,
    recovery_ml_export_path: Optional[str] = None,
) -> None:
    rec_stats, rec_rows = _build_game5m_hold_recovery_dataset_stats(
        effects,
        ohlc_cache,
        strategy=strategy,
        collect_rows_for_export=bool(export_recovery_ml),
    )
    payload["game5m_hold_recovery_dataset_stats"] = rec_stats
    if export_recovery_ml and rec_rows:
        path_override = (recovery_ml_export_path or "").strip() or None
        if not path_override:
            path_override = (get_config_value("ANALYZER_RECOVERY_ML_EXPORT_PATH", "") or "").strip() or None
        payload["game5m_hold_recovery_export"] = _export_game5m_recovery_ml_jsonl(
            rec_rows, export_path=path_override
        )
    elif export_recovery_ml:
        payload["game5m_hold_recovery_export"] = {
            "status": "skipped",
            "reason": "no rows to export (OHLC/horizon coverage)",
        }
    else:
        payload["game5m_hold_recovery_export"] = None


def _build_game5m_hold_recovery_dataset_stats(
    effects: List[TradeEffect],
    ohlc_cache: Dict[str, Optional[pd.DataFrame]],
    *,
    strategy: str,
    collect_rows_for_export: bool = False,
    near_early_pnl_pct_max: Optional[float] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Фаза C1/C2: статистика псевдо-датасета recovery; опционально накопление строк для JSONL.
    """
    ge = _effects_game5m_for_recovery(effects, strategy)
    if not ge:
        return (
            {
                "enabled": False,
                "skip_reason": "no GAME_5M trades in analyzer scope",
                "schema_version": RECOVERY_ML_SCHEMA_VERSION,
                "schema_doc": RECOVERY_ML_SCHEMA,
            },
            [],
        )

    raw_eps = (
        get_config_value("GAME_5M_RECOVERY_ML_EPS_UP_PCT", str(RECOVERY_ML_DEFAULT_EPS_UP_PCT))
        or str(RECOVERY_ML_DEFAULT_EPS_UP_PCT)
    ).strip().replace(",", ".")
    try:
        eps_up = float(raw_eps)
    except (ValueError, TypeError):
        eps_up = RECOVERY_ML_DEFAULT_EPS_UP_PCT
    raw_adv = (
        get_config_value("GAME_5M_RECOVERY_ML_MAX_ADVERSE_PCT", str(RECOVERY_ML_DEFAULT_MAX_ADVERSE_PCT))
        or str(RECOVERY_ML_DEFAULT_MAX_ADVERSE_PCT)
    ).strip().replace(",", ".")
    try:
        max_adv = float(raw_adv)
    except (ValueError, TypeError):
        max_adv = RECOVERY_ML_DEFAULT_MAX_ADVERSE_PCT

    try:
        max_per_trade = int(
            (get_config_value("GAME_5M_RECOVERY_ML_MAX_ROWS_PER_TRADE", str(RECOVERY_ML_MAX_ROWS_PER_TRADE)) or "").strip()
            or RECOVERY_ML_MAX_ROWS_PER_TRADE
        )
    except (ValueError, TypeError):
        max_per_trade = RECOVERY_ML_MAX_ROWS_PER_TRADE
    max_per_trade = max(4, min(200, max_per_trade))

    try:
        stride = int(
            (get_config_value("GAME_5M_RECOVERY_ML_ROW_STRIDE", str(RECOVERY_ML_ROW_STRIDE_DEFAULT)) or "").strip()
            or RECOVERY_ML_ROW_STRIDE_DEFAULT
        )
    except (ValueError, TypeError):
        stride = RECOVERY_ML_ROW_STRIDE_DEFAULT
    stride = max(1, min(12, stride))

    try:
        max_total = int(
            (get_config_value("GAME_5M_RECOVERY_ML_MAX_TOTAL_ROWS", str(RECOVERY_ML_MAX_TOTAL_ROWS)) or "").strip()
            or RECOVERY_ML_MAX_TOTAL_ROWS
        )
    except (ValueError, TypeError):
        max_total = RECOVERY_ML_MAX_TOTAL_ROWS
    max_total = max(1000, min(500_000, max_total))

    horizons = _recovery_ml_horizons_from_config()
    near_filter = near_early_pnl_pct_max
    if near_filter is None:
        near_filter = _recovery_ml_near_early_filter_from_config()

    rows_out: List[Dict[str, Any]] = []
    trades_used = 0
    trades_skipped_no_ohlc = 0
    trades_skipped_short = 0
    label_sum: Dict[int, int] = {h: 0 for h in horizons}
    label_n: Dict[int, int] = {h: 0 for h in horizons}
    by_ticker: Dict[str, int] = {}

    for e in ge:
        if len(rows_out) >= max_total:
            break
        df = ohlc_cache.get(e.ticker)
        if df is None or getattr(df, "empty", True):
            trades_skipped_no_ohlc += 1
            continue
        entry_ts = _as_et(pd.Timestamp(e.entry_ts))
        exit_ts = _as_et(pd.Timestamp(e.exit_ts))
        if exit_ts <= entry_ts:
            trades_skipped_short += 1
            continue
        try:
            m = (df["datetime"] >= entry_ts) & (df["datetime"] < exit_ts)
            sub = df.loc[m].reset_index(drop=True)
        except Exception:
            trades_skipped_no_ohlc += 1
            continue
        if len(sub) < 2:
            trades_skipped_short += 1
            continue
        trades_used += 1
        n_trade_rows = 0
        entry = float(e.entry_price)
        if entry <= 0:
            continue

        ed_dec = (e.entry_decision or "")[:64] if e.entry_decision else None

        for i in range(0, len(sub), stride):
            if len(rows_out) >= max_total or n_trade_rows >= max_per_trade:
                break
            try:
                bar_time = pd.Timestamp(sub["datetime"].iloc[i])
                if bar_time.tzinfo is None:
                    bar_time = bar_time.tz_localize("America/New_York", ambiguous="infer")
                else:
                    bar_time = bar_time.tz_convert("America/New_York")
            except Exception:
                continue
            try:
                ref_close = float(sub["Close"].iloc[i])
            except Exception:
                continue
            if ref_close <= 0:
                continue
            pnl_pct = (ref_close / entry - 1.0) * 100.0
            if near_filter is not None and pnl_pct > near_filter:
                continue
            hold_min = float((bar_time - entry_ts) / pd.Timedelta(minutes=1))

            row_base: Dict[str, Any] = {
                "schema_version": RECOVERY_ML_SCHEMA_VERSION,
                "trade_id": int(e.trade_id),
                "ticker": str(e.ticker).strip().upper(),
                "bar_ts_et": bar_time.isoformat(),
                "ref_close": round(ref_close, 6),
                "entry_price": round(entry, 6),
                "pnl_pct": round(pnl_pct, 4),
                "hold_minutes": round(hold_min, 2),
                "minutes_after_rth_open": _minutes_after_rth_open_et(bar_time),
                "dow": int(bar_time.dayofweek),
                "hour_et": int(bar_time.hour),
                "entry_rsi_5m": e.entry_rsi_5m,
                "entry_vol_5m_pct": e.entry_vol_5m_pct,
                "entry_momentum_2h_pct": e.entry_momentum_2h_pct,
                "entry_decision": ed_dec,
                "exit_signal": str(e.exit_signal or ""),
                "horizons_minutes": list(horizons),
                "label_eps_up_pct": eps_up,
                "label_max_adverse_pct": max_adv,
            }

            labels_done: Dict[int, Tuple[float, float, int]] = {}
            ok_all_h = True
            for H in horizons:
                end_t = bar_time + pd.Timedelta(minutes=int(H))
                try:
                    fwd = df.loc[(df["datetime"] > bar_time) & (df["datetime"] <= end_t)]
                except Exception:
                    ok_all_h = False
                    break
                if fwd is None or fwd.empty:
                    ok_all_h = False
                    break
                try:
                    hi = float(fwd["High"].max())
                    lo = float(fwd["Low"].min())
                except Exception:
                    ok_all_h = False
                    break
                mfe_pct = (hi / ref_close - 1.0) * 100.0
                mae_pct = (lo / ref_close - 1.0) * 100.0
                y_rec = 1 if (mfe_pct >= eps_up and mae_pct >= max_adv) else 0
                labels_done[int(H)] = (mfe_pct, mae_pct, y_rec)

            if not ok_all_h or len(labels_done) != len(horizons):
                continue

            row = dict(row_base)
            for H, (mfe_pct, mae_pct, y_rec) in sorted(labels_done.items()):
                row[f"h{H}_mfe_fwd_pct"] = round(mfe_pct, 4)
                row[f"h{H}_mae_fwd_pct"] = round(mae_pct, 4)
                row[f"h{H}_y_recovery"] = y_rec
                label_sum[int(H)] += y_rec
                label_n[int(H)] += 1

            rows_out.append(row)
            n_trade_rows += 1
            tk = row["ticker"]
            by_ticker[tk] = by_ticker.get(tk, 0) + 1

    if not collect_rows_for_export:
        rows_for_file: List[Dict[str, Any]] = []
    else:
        rows_for_file = list(rows_out)

    label_rate: Dict[str, Any] = {}
    for h in horizons:
        n = label_n.get(h, 0)
        label_rate[str(h)] = None if n <= 0 else round(float(label_sum[h]) / float(n), 4)

    top_tickers = sorted(by_ticker.items(), key=lambda kv: -kv[1])[:12]

    stats: Dict[str, Any] = {
        "enabled": True,
        "schema_version": RECOVERY_ML_SCHEMA_VERSION,
        "schema_doc": RECOVERY_ML_SCHEMA,
        "horizons_minutes": list(horizons),
        "label_eps_up_pct": eps_up,
        "label_max_adverse_pct": max_adv,
        "near_early_pnl_pct_max_filter": near_filter,
        "row_stride": stride,
        "max_rows_per_trade": max_per_trade,
        "max_total_rows_cap": max_total,
        "total_pseudo_rows": len(rows_out),
        "trades_used": trades_used,
        "trades_skipped_no_ohlc": trades_skipped_no_ohlc,
        "trades_skipped_short_hold": trades_skipped_short,
        "label_positive_rate_by_horizon": label_rate,
        "label_counts_by_horizon": {str(h): {"positive": label_sum[h], "n": label_n[h]} for h in horizons},
        "rows_top_tickers": [{"ticker": t, "rows": n} for t, n in top_tickers],
        "sample_rows_preview": rows_out[:3] if rows_out else [],
        "note": (
            "Метки по полному 5m ряду после t (включая время после фактического exit). "
            "Колонка exit_signal — постфактум; при обучении recovery можно исключить как утечку."
        ),
    }
    return stats, rows_for_file


def _estimate_trade_effects(closed_trades: List[Any], ohlc_cache: Dict[str, Optional[pd.DataFrame]]) -> List[TradeEffect]:
    effects: List[TradeEffect] = []
    for t in closed_trades:
        if not t.entry_ts:
            continue
        entry_ts = _as_et(pd.Timestamp(t.entry_ts))
        exit_ts = _as_et(pd.Timestamp(t.ts))
        if exit_ts <= entry_ts:
            continue
        qty = float(t.quantity)
        entry = float(t.entry_price)
        exit_p = float(t.exit_price)
        if qty <= 0 or entry <= 0 or exit_p <= 0:
            continue

        cost_basis = qty * entry
        realized_pct = (float(t.net_pnl) / cost_basis) * 100 if cost_basis > 0 else 0.0
        realized_log_return = float(np.log1p(realized_pct / 100.0)) if realized_pct > -100 else -999.0

        window = _slice_window(ohlc_cache.get(t.ticker), entry_ts, exit_ts)
        potential_best_pct = preventable_worst_pct = None
        missed_upside_pct = avoidable_loss_pct = None
        likely_late_polling = False
        if window is not None and not window.empty:
            try:
                mfe_price = float(window["High"].max())
                mae_price = float(window["Low"].min())
                potential_best_pct = ((mfe_price / entry) - 1.0) * 100.0
                preventable_worst_pct = ((mae_price / entry) - 1.0) * 100.0
                missed_upside_pct = max(0.0, potential_best_pct - realized_pct)
                avoidable_loss_pct = max(0.0, realized_pct - preventable_worst_pct)
                # Историческое имя likely_late_polling: фактически «выход не у MFE high окна», не задержка cron.
                likely_late_polling = abs(exit_p - mfe_price) / mfe_price > 0.004 if mfe_price > 0 else False
            except Exception:
                pass

        entry_ctx = normalize_entry_context(getattr(t, "context_json", None))
        exit_ctx = _json_dict(getattr(t, "exit_context_json", None))
        position_state_v2 = exit_ctx.get("position_state_v2") if isinstance(exit_ctx.get("position_state_v2"), dict) else None
        continuation_gate = exit_ctx.get("continuation_gate") if isinstance(exit_ctx.get("continuation_gate"), dict) else None
        exit_detail = exit_ctx.get("exit_detail") or exit_ctx.get("exit_condition")
        exit_detail = str(exit_detail).strip() if exit_detail is not None and str(exit_detail).strip() else None
        raw_decision = entry_ctx.get("decision")
        entry_decision = str(raw_decision).strip() if raw_decision is not None and str(raw_decision).strip() else None
        raw_reason = entry_ctx.get("reasoning")
        entry_reasoning: Optional[str] = None
        if isinstance(raw_reason, str):
            rs = raw_reason.strip()
            if rs:
                entry_reasoning = (
                    rs[:ENTRY_REASONING_ANALYZER_MAX] + "…" if len(rs) > ENTRY_REASONING_ANALYZER_MAX else rs
                )
        effects.append(
            TradeEffect(
                trade_id=int(t.trade_id),
                ticker=str(t.ticker),
                entry_ts=entry_ts,
                exit_ts=exit_ts,
                hold_minutes=float((exit_ts - entry_ts) / pd.Timedelta(minutes=1)),
                qty=qty,
                entry_price=entry,
                exit_price=exit_p,
                net_pnl=float(t.net_pnl),
                realized_pct=realized_pct,
                realized_log_return=realized_log_return,
                exit_signal=str(getattr(t, "signal_type", "") or ""),
                exit_strategy=str(getattr(t, "exit_strategy", "") or ""),
                potential_best_pct=potential_best_pct,
                preventable_worst_pct=preventable_worst_pct,
                missed_upside_pct=missed_upside_pct,
                avoidable_loss_pct=avoidable_loss_pct,
                likely_late_polling=likely_late_polling,
                entry_rsi_5m=_safe_float(entry_ctx.get("rsi_5m")),
                entry_vol_5m_pct=_safe_float(entry_ctx.get("volatility_5m_pct")),
                entry_momentum_2h_pct=_safe_float(entry_ctx.get("momentum_2h_pct")),
                entry_price_forecast_5m_summary=(
                    (lambda v: (str(v).strip()[:2000] or None) if v is not None else None)(
                        entry_ctx.get("price_forecast_5m_summary")
                    )
                ),
                entry_prob_up=_safe_float(entry_ctx.get("prob_up")),
                entry_prob_down=_safe_float(entry_ctx.get("prob_down")),
                entry_news_impact=(entry_ctx.get("kb_news_impact") or None),
                entry_advice=(entry_ctx.get("entry_advice") or None),
                entry_decision=entry_decision,
                entry_reasoning=entry_reasoning,
                decision_rule_version=(entry_ctx.get("decision_rule_version") or None),
                decision_rule_params=(entry_ctx.get("decision_rule_params") if isinstance(entry_ctx.get("decision_rule_params"), dict) else None),
                exit_detail=exit_detail,
                position_state_v2=position_state_v2,
                continuation_gate=continuation_gate,
            )
        )
    return effects


def _aggregate(effects: List[TradeEffect]) -> Dict[str, Any]:
    if not effects:
        return {"total": 0}
    realized = [e.realized_pct for e in effects]
    net_pnl = [e.net_pnl for e in effects]
    logrets = [e.realized_log_return for e in effects if e.realized_log_return > -900]
    missed = [e.missed_upside_pct for e in effects if e.missed_upside_pct is not None]
    avoidable = [e.avoidable_loss_pct for e in effects if e.avoidable_loss_pct is not None]
    wins = [e for e in effects if e.realized_pct > 0]
    losses = [e for e in effects if e.realized_pct <= 0]

    by_exit: Dict[str, Dict[str, float]] = {}
    for e in effects:
        k = e.exit_signal or "UNKNOWN"
        by_exit.setdefault(k, {"count": 0, "avg_realized_pct": 0.0, "avg_missed_pct": 0.0})
        by_exit[k]["count"] += 1
        by_exit[k]["avg_realized_pct"] += e.realized_pct
        by_exit[k]["avg_missed_pct"] += e.missed_upside_pct or 0.0
    for v in by_exit.values():
        c = max(1, int(v["count"]))
        v["avg_realized_pct"] = round(v["avg_realized_pct"] / c, 3)
        v["avg_missed_pct"] = round(v["avg_missed_pct"] / c, 3)

    late_polling_count = sum(1 for e in effects if e.likely_late_polling and (e.missed_upside_pct or 0) > 0.3)
    high_vol_losses = [e for e in losses if e.entry_vol_5m_pct is not None and e.entry_vol_5m_pct >= 0.6]
    weak_prob_entries = [e for e in losses if e.entry_prob_up is not None and e.entry_prob_up < 0.55]
    neg_news_losses = [e for e in losses if (e.entry_news_impact or "").lower().startswith("негатив")]

    losses_with_allow = [e for e in losses if (e.entry_advice or "").upper() == "ALLOW"]
    losses_with_high_prob = [e for e in losses if e.entry_prob_up is not None and e.entry_prob_up >= 0.60]
    losses_with_high_rsi = [e for e in losses if e.entry_rsi_5m is not None and e.entry_rsi_5m >= 60]
    rule_versions = sorted({(e.decision_rule_version or "unknown") for e in effects})
    missed_on_wins = [(e.missed_upside_pct or 0.0) for e in wins if e.missed_upside_pct is not None]
    wins_missed_ge_1 = [e for e in wins if (e.missed_upside_pct or 0.0) >= 1.0]
    stuck_ge_7d = [e for e in effects if e.hold_minutes >= LONG_HOLD_MINUTES]
    stuck_poor = [
        e
        for e in stuck_ge_7d
        if e.realized_pct <= 0.15
    ]
    missing_entry_decision = sum(1 for e in effects if not (e.entry_decision or "").strip())
    return {
        "total": len(effects),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(effects), 2),
        "sum_net_pnl_usd": round(float(sum(net_pnl)), 2),
        "avg_realized_pct": round(float(np.mean(realized)), 3),
        "median_realized_pct": round(float(np.median(realized)), 3),
        "avg_log_return": round(float(np.mean(logrets)), 5) if logrets else None,
        "sum_missed_upside_pct": round(float(sum(missed)), 3),
        "avg_missed_upside_pct": round(float(np.mean(missed)), 3) if missed else None,
        "sum_missed_upside_pct_on_wins": round(float(sum(missed_on_wins)), 3) if missed_on_wins else 0.0,
        "avg_missed_upside_pct_on_wins": round(float(np.mean(missed_on_wins)), 3) if missed_on_wins else None,
        "wins_with_missed_upside_ge_1pct_count": len(wins_missed_ge_1),
        "sum_avoidable_loss_pct": round(float(sum(avoidable)), 3),
        "avg_avoidable_loss_pct": round(float(np.mean(avoidable)), 3) if avoidable else None,
        "late_polling_signals": late_polling_count,
        "exit_below_window_mfe_count": late_polling_count,
        "high_vol_losses_count": len(high_vol_losses),
        "weak_prob_up_losses_count": len(weak_prob_entries),
        "negative_news_losses_count": len(neg_news_losses),
        "losses_with_allow_entry_count": len(losses_with_allow),
        "losses_with_high_prob_up_count": len(losses_with_high_prob),
        "losses_with_high_rsi_count": len(losses_with_high_rsi),
        "decision_rule_versions": rule_versions,
        "by_exit_signal": by_exit,
        "long_hold_ge_7d_count": len(stuck_ge_7d),
        "long_hold_ge_7d_poor_outcome_count": len(stuck_poor),
        "trades_missing_entry_decision_count": missing_entry_decision,
    }


def _suggested_entry_env_keys(e: TradeEffect) -> List[str]:
    """
    Какие ключи config.env логично пересмотреть, если результат входа плохий или позиция долго «висела».
    Тип входа (STRONG_BUY vs BUY) задаёт разные ветки в recommend_5m; точную ветку без reasoning не восстановить —
    для BUY перечисляем оба семейства порогов.
    """
    d = (e.entry_decision or "").strip().upper()
    keys: List[str] = []
    if d == "STRONG_BUY":
        keys = ["GAME_5M_MOMENTUM_STRONG_BUY_MIN", "GAME_5M_RSI_STRONG_BUY_MAX"]
    elif d == "BUY":
        keys = [
            "GAME_5M_RTH_MOMENTUM_BUY_MIN",
            "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX",
            "GAME_5M_RSI_BUY_MAX",
            "GAME_5M_PRICE_TO_LOW5D_MULT_MAX",
            "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY",
            "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN",
            "GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW",
        ]
    else:
        keys = [
            "GAME_5M_MOMENTUM_STRONG_BUY_MIN",
            "GAME_5M_RSI_STRONG_BUY_MAX",
            "GAME_5M_RTH_MOMENTUM_BUY_MIN",
            "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX",
        ]
    if e.hold_minutes >= LONG_HOLD_MINUTES:
        keys = keys + [
            "GAME_5M_MAX_POSITION_DAYS",
            "GAME_5M_MAX_POSITION_MINUTES",
            "GAME_5M_SESSION_END_EXIT_MINUTES",
            "GAME_5M_SESSION_END_MIN_PROFIT_PCT",
        ]
    seen: set[str] = set()
    out: List[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _build_entry_underperformance_review(
    effects: List[TradeEffect],
    *,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """
    Сделки почти без профита / в минусе и особенно с удержанием ≥7 суток —
    явная привязка к тексту причины входа и к порогам из decision_rule_params на момент BUY.
    """
    scored: List[tuple[float, TradeEffect]] = []
    for e in effects:
        long_hold = e.hold_minutes >= LONG_HOLD_MINUTES
        poor = e.realized_pct <= 0.15
        loss = e.realized_pct < 0
        score = 0.0
        if loss:
            score += 2.0 + max(0.0, -e.realized_pct) / 15.0
        elif poor:
            score += 0.8
        if long_hold:
            score += 1.2
            if poor or loss:
                score += 1.5
        if score < 0.75:
            continue
        scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    out: List[Dict[str, Any]] = []
    for _, e in scored[:limit]:
        hold_d = round(e.hold_minutes / (24 * 60), 2)
        poor_out = e.realized_pct <= 0.15
        long_hold = e.hold_minutes >= LONG_HOLD_MINUTES
        if long_hold and poor_out:
            note = (
                "Долгое удержание (≥7 суток) при слабом или отрицательном результате: "
                "в первую очередь проверить пороги входа и лимиты удержания (см. suggested_config_env_review)."
            )
        elif long_hold:
            note = "Долгое удержание: даже при плюсе стоит проверить MAX_POSITION_* и условия выхода по времени."
        else:
            note = "Слабый или отрицательный результат — пересмотреть пороги ветки входа (см. suggested_config_env_review и reasoning)."
        out.append(
            {
                "trade_id": e.trade_id,
                "ticker": e.ticker,
                "hold_days": hold_d,
                "hold_minutes": round(e.hold_minutes, 1),
                "long_hold_ge_7d": e.hold_minutes >= LONG_HOLD_MINUTES,
                "realized_pct": round(e.realized_pct, 3),
                "exit_signal": e.exit_signal,
                "entry_decision": e.entry_decision,
                "entry_reasoning_excerpt": (e.entry_reasoning or "")[:320],
                "entry_momentum_2h_pct": e.entry_momentum_2h_pct,
                "entry_rsi_5m": e.entry_rsi_5m,
                "decision_rule_params_at_entry": e.decision_rule_params,
                "suggested_config_env_review": _suggested_entry_env_keys(e),
                "note": note,
            }
        )
    return out


def _top_cases(effects: List[TradeEffect], limit: int = 8) -> Dict[str, List[Dict[str, Any]]]:
    def row(e: TradeEffect) -> Dict[str, Any]:
        return {
            "trade_id": e.trade_id,
            "ticker": e.ticker,
            "entry_ts": e.entry_ts.isoformat(),
            "exit_ts": e.exit_ts.isoformat(),
            "hold_minutes": round(e.hold_minutes, 1),
            "exit_signal": e.exit_signal,
            "realized_pct": round(e.realized_pct, 3),
            "missed_upside_pct": round(e.missed_upside_pct or 0.0, 3),
            "avoidable_loss_pct": round(e.avoidable_loss_pct or 0.0, 3),
            "entry_decision": e.entry_decision,
            "entry_reasoning": e.entry_reasoning,
            "entry_rsi_5m": e.entry_rsi_5m,
            "entry_vol_5m_pct": e.entry_vol_5m_pct,
            "entry_momentum_2h_pct": e.entry_momentum_2h_pct,
            "entry_prob_up": e.entry_prob_up,
            "entry_price_forecast_5m_summary": e.entry_price_forecast_5m_summary,
            "entry_news_impact": e.entry_news_impact,
            "entry_advice": e.entry_advice,
            "decision_rule_version": e.decision_rule_version,
            "decision_rule_params": e.decision_rule_params,
            "exit_detail": e.exit_detail,
            "position_state_v2": e.position_state_v2,
            "continuation_gate": e.continuation_gate,
        }

    by_missed = sorted(effects, key=lambda x: x.missed_upside_pct or 0.0, reverse=True)[:limit]
    by_loss = sorted(effects, key=lambda x: x.realized_pct)[:limit]
    winners = [e for e in effects if e.realized_pct > 0]
    by_win_missed = sorted(winners, key=lambda x: x.missed_upside_pct or 0.0, reverse=True)[:limit]
    return {
        "top_missed_upside": [row(e) for e in by_missed],
        "top_losses": [row(e) for e in by_loss],
        "top_profitable_missed_upside": [row(e) for e in by_win_missed],
    }


def _trade_effect_detail_dict(e: TradeEffect) -> Dict[str, Any]:
    """Полная сериализация сделки для внешнего JSON (локальный LLM, скрипты, jq)."""
    return {
        "trade_id": e.trade_id,
        "ticker": e.ticker,
        "entry_ts": e.entry_ts.isoformat(),
        "exit_ts": e.exit_ts.isoformat(),
        "hold_minutes": round(e.hold_minutes, 2),
        "qty": e.qty,
        "entry_price": e.entry_price,
        "exit_price": e.exit_price,
        "net_pnl": round(e.net_pnl, 4),
        "realized_pct": round(e.realized_pct, 4),
        "realized_log_return": round(e.realized_log_return, 6) if e.realized_log_return > -900 else None,
        "exit_signal": e.exit_signal,
        "exit_strategy": e.exit_strategy,
        "exit_detail": e.exit_detail,
        "potential_best_pct": None if e.potential_best_pct is None else round(e.potential_best_pct, 4),
        "preventable_worst_pct": None if e.preventable_worst_pct is None else round(e.preventable_worst_pct, 4),
        "missed_upside_pct": None if e.missed_upside_pct is None else round(e.missed_upside_pct, 4),
        "avoidable_loss_pct": None if e.avoidable_loss_pct is None else round(e.avoidable_loss_pct, 4),
        "likely_late_polling": e.likely_late_polling,
        "entry_decision": e.entry_decision,
        "entry_reasoning": e.entry_reasoning,
        "entry_rsi_5m": e.entry_rsi_5m,
        "entry_vol_5m_pct": e.entry_vol_5m_pct,
        "entry_momentum_2h_pct": e.entry_momentum_2h_pct,
        "entry_prob_up": e.entry_prob_up,
        "entry_prob_down": e.entry_prob_down,
        "entry_price_forecast_5m_summary": e.entry_price_forecast_5m_summary,
        "entry_news_impact": e.entry_news_impact,
        "entry_advice": e.entry_advice,
        "decision_rule_version": e.decision_rule_version,
        "decision_rule_params": e.decision_rule_params,
        "position_state_v2": e.position_state_v2,
        "continuation_gate": e.continuation_gate,
        "suggested_config_env_review_for_entry": _suggested_entry_env_keys(e),
    }


def _count_by(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        k = str(r.get(key) or "unknown")
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _build_hanger_v2_review(effects: List[TradeEffect], *, limit: int = 12) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for e in effects:
        ps = e.position_state_v2 if isinstance(e.position_state_v2, dict) else None
        if not ps:
            continue
        state = str(ps.get("state") or "unknown")
        row = {
            "trade_id": e.trade_id,
            "ticker": e.ticker,
            "exit_signal": e.exit_signal,
            "exit_detail": e.exit_detail,
            "hold_minutes": round(e.hold_minutes, 1),
            "hold_days": round(e.hold_minutes / (24 * 60), 2),
            "realized_pct": round(e.realized_pct, 3),
            "missed_upside_pct": round(e.missed_upside_pct or 0.0, 3),
            "state": state,
            "score": ps.get("score"),
            "age_minutes": ps.get("age_minutes"),
            "pnl_pct": ps.get("pnl_pct"),
            "momentum_2h_pct": ps.get("momentum_2h_pct"),
            "distance_to_take_pct": ps.get("distance_to_take_pct"),
            "components": ps.get("components") if isinstance(ps.get("components"), dict) else {},
        }
        rows.append(row)

    stale_closed = [
        r for r in rows
        if r["exit_signal"] == "TIME_EXIT_EARLY" and "stale_reversal" in str(r.get("exit_detail") or "").lower()
    ]
    stale_state_not_cut = [
        r for r in rows
        if r["state"] == "stale_reversal" and r not in stale_closed
    ]
    recoverable = [r for r in rows if r["state"] == "recoverable_hanger"]
    recoverable_near_take = [
        r for r in recoverable
        if _safe_float(r.get("distance_to_take_pct")) is not None and (_safe_float(r.get("distance_to_take_pct")) or 999) <= 0.35
    ]

    cases = sorted(
        rows,
        key=lambda r: (
            0 if r.get("state") == "stale_reversal" else 1,
            float(r.get("realized_pct") or 0.0),
            -float(r.get("hold_minutes") or 0.0),
        ),
    )[:limit]

    parameter_candidates: List[Dict[str, Any]] = []
    if stale_state_not_cut:
        parameter_candidates.append(
            {
                "env_keys": [
                    "GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES",
                    "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT",
                    "GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW",
                ],
                "direction": "review_make_stale_exit_more_consistent",
                "evidence": f"{len(stale_state_not_cut)} closed trades had position_state_v2=stale_reversal but were not closed as TIME_EXIT_EARLY/stale_reversal.",
            }
        )
    if stale_closed:
        avg_realized = float(np.mean([float(r["realized_pct"]) for r in stale_closed]))
        parameter_candidates.append(
            {
                "env_keys": [
                    "GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES",
                    "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT",
                ],
                "direction": "observe_before_changing" if len(stale_closed) < 3 else "review_age_and_loss_thresholds",
                "evidence": f"{len(stale_closed)} stale/reversal exits, avg_realized_pct={avg_realized:.2f}%.",
            }
        )
    if recoverable and not recoverable_near_take:
        parameter_candidates.append(
            {
                "env_keys": [
                    "GAME_5M_HANGER_V2_RECOVERABLE_MIN_AGE_MINUTES",
                    "GAME_5M_HANGER_V2_RECOVERABLE_MIN_PNL_PCT",
                    "GAME_5M_HANGER_V2_RECOVERABLE_MAX_PNL_PCT",
                    "GAME_5M_HANGER_V2_WEAK_MOMENTUM_BELOW",
                ],
                "direction": "review_recoverable_definition_or_take_cap",
                "evidence": f"{len(recoverable)} recoverable_hanger cases, but only {len(recoverable_near_take)} were within 0.35 p.p. of take.",
            }
        )

    return {
        "enabled": True,
        "trades_with_position_state_v2": len(rows),
        "state_counts": _count_by(rows, "state"),
        "exit_signal_counts": _count_by(rows, "exit_signal"),
        "stale_reversal_exit_count": len(stale_closed),
        "stale_state_not_cut_count": len(stale_state_not_cut),
        "recoverable_hanger_count": len(recoverable),
        "recoverable_near_take_count": len(recoverable_near_take),
        "top_cases": cases,
        "watch_today": {
            "stale_state_not_cut": stale_state_not_cut[:limit],
            "recoverable_near_take": recoverable_near_take[:limit],
            "stale_reversal_exits": stale_closed[:limit],
        },
        "parameter_candidates": parameter_candidates,
        "note": "Closed-trade review from SELL context_json.position_state_v2; live open-position monitoring comes from cron log summary.",
    }


def _build_continuation_gate_review(effects: List[TradeEffect], *, limit: int = 12) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for e in effects:
        cg = e.continuation_gate if isinstance(e.continuation_gate, dict) else None
        if not cg:
            continue
        row = {
            "trade_id": e.trade_id,
            "ticker": e.ticker,
            "exit_signal": e.exit_signal,
            "exit_detail": e.exit_detail,
            "realized_pct": round(e.realized_pct, 3),
            "missed_upside_pct": round(e.missed_upside_pct or 0.0, 3),
            "decision": cg.get("decision"),
            "would_extend_take": bool(cg.get("would_extend_take")),
            "log_only": bool(cg.get("log_only")),
            "pnl_pct": cg.get("pnl_pct"),
            "momentum_2h_pct": cg.get("momentum_2h_pct"),
            "rsi_5m": cg.get("rsi_5m"),
            "score": cg.get("score"),
            "components": cg.get("components") if isinstance(cg.get("components"), dict) else {},
        }
        rows.append(row)

    extend = [r for r in rows if r.get("would_extend_take")]
    close_now = [r for r in rows if str(r.get("decision") or "") == "close_now"]
    extend_missed = [r for r in extend if float(r.get("missed_upside_pct") or 0.0) >= 1.0]
    close_now_missed = [r for r in close_now if float(r.get("missed_upside_pct") or 0.0) >= 1.0]

    parameter_candidates: List[Dict[str, Any]] = []
    if extend_missed:
        parameter_candidates.append(
            {
                "env_keys": [
                    "GAME_5M_CONTINUATION_GATE_LOG_ONLY",
                    "GAME_5M_CONTINUATION_MIN_PNL_PCT",
                    "GAME_5M_CONTINUATION_MIN_MOMENTUM_2H_PCT",
                ],
                "direction": "candidate_for_non_log_only_after_more_samples",
                "evidence": f"{len(extend_missed)} extend_take_candidate cases still had missed_upside >= 1%.",
            }
        )
    if close_now_missed:
        parameter_candidates.append(
            {
                "env_keys": [
                    "GAME_5M_CONTINUATION_MIN_MOMENTUM_2H_PCT",
                    "GAME_5M_CONTINUATION_MAX_RSI_5M",
                    "GAME_5M_TAKE_MOMENTUM_FACTOR",
                ],
                "direction": "review_strictness_or_take_factor",
                "evidence": f"{len(close_now_missed)} close_now cases had missed_upside >= 1%, so continuation thresholds may be too strict.",
            }
        )

    top_cases = sorted(rows, key=lambda r: float(r.get("missed_upside_pct") or 0.0), reverse=True)[:limit]
    avg_missed_extend = float(np.mean([float(r.get("missed_upside_pct") or 0.0) for r in extend])) if extend else None
    avg_missed_close = float(np.mean([float(r.get("missed_upside_pct") or 0.0) for r in close_now])) if close_now else None
    return {
        "enabled": True,
        "trades_with_continuation_gate": len(rows),
        "decision_counts": _count_by(rows, "decision"),
        "would_extend_take_count": len(extend),
        "extend_candidates_with_missed_upside_ge_1pct": len(extend_missed),
        "close_now_with_missed_upside_ge_1pct": len(close_now_missed),
        "avg_missed_upside_extend_candidates": None if avg_missed_extend is None else round(avg_missed_extend, 3),
        "avg_missed_upside_close_now": None if avg_missed_close is None else round(avg_missed_close, 3),
        "top_cases": top_cases,
        "parameter_candidates": parameter_candidates,
        "note": "Closed-trade review from SELL context_json.continuation_gate; current gate is expected to be log-only until enough samples are collected.",
    }


def _effects_for_hanger_tune_json(strategy: str, effects: List[TradeEffect]) -> List[TradeEffect]:
    su = (strategy or "").strip().upper()
    if su == "ALL":
        return [e for e in effects if str(getattr(e, "exit_strategy", None) or "").strip().upper() == "GAME_5M"]
    if su == "GAME_5M":
        return list(effects)
    return []


def _game_5m_take_cap_pct_for_ticker(ticker: str) -> float:
    """Потолок тейка из config: per-ticker или общий GAME_5M_TAKE_PROFIT_PCT."""
    cfg = load_config()
    tu = str(ticker or "").strip().upper()
    if tu:
        raw = (cfg.get(f"GAME_5M_TAKE_PROFIT_PCT_{tu}") or "").strip()
        if raw:
            try:
                return float(raw.replace(",", "."))
            except (ValueError, TypeError):
                pass
    raw2 = (cfg.get("GAME_5M_TAKE_PROFIT_PCT") or "").strip()
    if raw2:
        try:
            return float(raw2.replace(",", "."))
        except (ValueError, TypeError):
            pass
    return 7.0


def _parse_hanger_tune_json_caps(data: Dict[str, Any]) -> Dict[str, float]:
    """Минимальный proposed_cap_pct на тикер (как в game_5m._hanger_tune_min_cap_pct)."""
    per: Dict[str, float] = {}
    for h in data.get("hanger_hypotheses") or []:
        if not isinstance(h, dict):
            continue
        cap_obj = h.get("remediation_take_cap")
        if not isinstance(cap_obj, dict):
            continue
        try:
            prop = float(cap_obj.get("proposed_cap_pct"))
        except (TypeError, ValueError):
            continue
        t = str(h.get("ticker") or "").strip().upper()
        if not t:
            continue
        prev = per.get(t)
        if prev is None or prop < prev:
            per[t] = prop
    return per


def _build_game5m_hanger_tune_json_review(
    effects: List[TradeEffect],
    summary: Dict[str, Any],
    *,
    strategy: str,
) -> Dict[str, Any]:
    """
    Эффективность ветки hanger JSON: файл, возраст, капы vs фактические TAKE_PROFIT / TAKE_PROFIT_SUSPEND.
    Добавляет practical_parameter_additions и parameter_candidates для game_5m_config_hints / LLM.
    """
    from collections import defaultdict

    out: Dict[str, Any] = {
        "enabled": False,
        "scope": "GAME_5M hanger JSON (remediation_take_cap) + exits TAKE_PROFIT vs TAKE_PROFIT_SUSPEND",
        "note": "Перегенерация JSON — offline (bundle); отчёт только постфактум по закрытым сделкам.",
        "practical_parameter_additions": [],
        "parameter_candidates": [],
    }
    su = (strategy or "").strip().upper()
    if su not in ("GAME_5M", "ALL"):
        out["skip_reason"] = "hanger JSON относится к выходам GAME_5M"
        return out
    out["enabled"] = True

    apply_raw = (get_config_value("GAME_5M_HANGER_TUNE_APPLY_TAKE", "false") or "false").strip().lower()
    apply_take = apply_raw in ("1", "true", "yes")
    raw_path = (get_config_value("GAME_5M_HANGER_TUNE_JSON", "") or "").strip()
    out["config"] = {
        "GAME_5M_HANGER_TUNE_APPLY_TAKE": apply_take,
        "GAME_5M_HANGER_TUNE_JSON": raw_path or None,
    }

    ge = _effects_for_hanger_tune_json(strategy, effects)
    out["trades_in_scope"] = len(ge)

    practical: List[Dict[str, Any]] = []
    candidates: List[Dict[str, Any]] = []

    take_rows = [e for e in ge if e.exit_signal in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND")]
    by_sig: Dict[str, List[TradeEffect]] = defaultdict(list)
    for e in take_rows:
        by_sig[str(e.exit_signal)].append(e)

    def _slice_stats(rows: List[TradeEffect]) -> Dict[str, Any]:
        if not rows:
            return {"count": 0}
        missed = [float(e.missed_upside_pct or 0.0) for e in rows]
        realized = [float(e.realized_pct) for e in rows]
        return {
            "count": len(rows),
            "avg_realized_pct": round(float(np.mean(realized)), 4),
            "avg_missed_upside_pct": round(float(np.mean(missed)), 4),
        }

    out["take_exit_mix"] = {
        "TAKE_PROFIT": _slice_stats(by_sig.get("TAKE_PROFIT", [])),
        "TAKE_PROFIT_SUSPEND": _slice_stats(by_sig.get("TAKE_PROFIT_SUSPEND", [])),
    }

    if not raw_path:
        out["file_status"] = "no_path"
        if apply_take:
            practical.append(
                {
                    "parameter": "hanger_tune_apply_take",
                    "current": True,
                    "proposed": False,
                    "why": "GAME_5M_HANGER_TUNE_APPLY_TAKE=true, но путь GAME_5M_HANGER_TUNE_JSON пуст — JSON не применяется.",
                    "expected_effect": "Согласованная конфигурация до появления файла с капами.",
                }
            )
            candidates.append(
                {
                    "env_keys": ["GAME_5M_HANGER_TUNE_APPLY_TAKE", "GAME_5M_HANGER_TUNE_JSON"],
                    "direction": "set_json_path_or_disable_apply",
                    "evidence": "apply_take без пути к JSON.",
                }
            )
        out["practical_parameter_additions"] = practical
        out["parameter_candidates"] = candidates
        return out

    path = Path(raw_path).expanduser()
    if not path.is_file():
        out["file_status"] = "missing"
        out["path"] = raw_path
        if apply_take:
            practical.append(
                {
                    "parameter": "hanger_tune_apply_take",
                    "current": True,
                    "proposed": False,
                    "why": f"GAME_5M_HANGER_TUNE_APPLY_TAKE=true, файл не найден: {raw_path}",
                    "expected_effect": "Не держать включённым apply без валидного JSON.",
                }
            )
            candidates.append(
                {
                    "env_keys": ["GAME_5M_HANGER_TUNE_JSON", "GAME_5M_HANGER_TUNE_APPLY_TAKE"],
                    "direction": "fix_path_or_disable_apply",
                    "evidence": "JSON path задан, файл отсутствует.",
                }
            )
        out["practical_parameter_additions"] = practical
        out["parameter_candidates"] = candidates
        return out

    try:
        mtime = float(path.stat().st_mtime)
        text = path.read_text(encoding="utf-8")
        meta = json.loads(text)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        out["file_status"] = "parse_error"
        out["path"] = str(path)
        out["error"] = str(exc)
        if apply_take:
            practical.append(
                {
                    "parameter": "hanger_tune_apply_take",
                    "current": True,
                    "proposed": False,
                    "why": "JSON hanger tune не читается или битый — отключить apply до исправления файла.",
                    "expected_effect": "Исключить непредсказуемое поведение при ошибке парсинга.",
                }
            )
        out["practical_parameter_additions"] = practical
        out["parameter_candidates"] = candidates
        return out

    if not isinstance(meta, dict):
        out["file_status"] = "invalid_root"
        out["practical_parameter_additions"] = practical
        out["parameter_candidates"] = candidates
        return out

    age_sec = max(0.0, datetime.now(timezone.utc).timestamp() - mtime)
    age_days = age_sec / 86400.0
    caps = _parse_hanger_tune_json_caps(meta)
    out["file_status"] = "ok"
    out["file"] = {
        "path": str(path.resolve()),
        "mtime_utc": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        "age_days": round(age_days, 2),
    }
    out["json_meta"] = {
        k: meta.get(k)
        for k in ("generated_at", "generated_at_utc", "source", "mode", "bundle_mode")
        if meta.get(k) is not None
    }
    out["caps_by_ticker_min_pct"] = caps

    per_ticker: Dict[str, Dict[str, Any]] = {}
    tickers_union = set(caps.keys())
    for e in by_sig.get("TAKE_PROFIT_SUSPEND", []):
        tickers_union.add(str(e.ticker or "").strip().upper())
    for t in sorted(tickers_union):
        if not t:
            continue
        cap = caps.get(t)
        cfg_take = _game_5m_take_cap_pct_for_ticker(t)
        eff = min(float(cfg_take), float(cap)) if cap is not None else float(cfg_take)
        susp = [e for e in by_sig.get("TAKE_PROFIT_SUSPEND", []) if str(e.ticker or "").strip().upper() == t]
        tpn = [e for e in by_sig.get("TAKE_PROFIT", []) if str(e.ticker or "").strip().upper() == t]
        per_ticker[t] = {
            "json_cap_pct": cap,
            "config_take_cap_pct": round(cfg_take, 4),
            "effective_cap_if_apply_pct": None if cap is None else round(eff, 4),
            "TAKE_PROFIT_SUSPEND": _slice_stats(susp),
            "TAKE_PROFIT": _slice_stats(tpn),
        }
    out["per_ticker"] = per_ticker

    policy_stale_days = 7.0
    out["staleness_policy_days"] = policy_stale_days
    out["json_stale_vs_policy"] = bool(age_days > policy_stale_days and caps)
    if out["json_stale_vs_policy"]:
        candidates.append(
            {
                "env_keys": ["GAME_5M_HANGER_TUNE_JSON"],
                "direction": "offline_regenerate_bundle",
                "evidence": (
                    f"Возраст JSON {age_days:.1f} дн. > политики {policy_stale_days:g} дн.; "
                    f"капы заданы для {len(caps)} тикер(ов). CLI: scripts/backtest_game5m_param_hypotheses.py --mode bundle"
                ),
            }
        )

    suspend = by_sig.get("TAKE_PROFIT_SUSPEND", [])
    tp_norm = by_sig.get("TAKE_PROFIT", [])
    if apply_take and len(suspend) >= 2:
        avg_r_s = float(np.mean([float(e.realized_pct) for e in suspend]))
        avg_m_s = float(np.mean([float(e.missed_upside_pct or 0.0) for e in suspend]))
        avg_m_t = (
            float(np.mean([float(e.missed_upside_pct or 0.0) for e in tp_norm]))
            if len(tp_norm) >= 2
            else 0.0
        )
        if avg_r_s < -0.25:
            practical.append(
                {
                    "parameter": "hanger_tune_apply_take",
                    "current": True,
                    "proposed": False,
                    "why": (
                        f"{len(suspend)}× TAKE_PROFIT_SUSPEND, средний realized {avg_r_s:.2f}% — "
                        "сужение/висячный тейк в окне дал слабый или отрицательный результат."
                    ),
                    "expected_effect": "Временно обычный TAKE_PROFIT до пересмотра JSON и диагностики hanger.",
                }
            )
        elif len(suspend) >= 3 and avg_m_s >= 1.2 and (len(tp_norm) < 2 or avg_m_s > avg_m_t + 0.8):
            candidates.append(
                {
                    "env_keys": [
                        "GAME_5M_HANGER_TUNE_JSON",
                        "GAME_5M_TAKE_PROFIT_PCT_<TICKER>",
                        "GAME_5M_HANGER_TUNE_APPLY_TAKE",
                    ],
                    "direction": "review_caps_or_regenerate_json",
                    "evidence": (
                        f"SUSPEND: avg missed {avg_m_s:.2f}% vs обычный TAKE_PROFIT avg missed {avg_m_t:.2f}% "
                        f"(n_susp={len(suspend)}, n_tp={len(tp_norm)})."
                    ),
                }
            )

    out["practical_parameter_additions"] = practical
    out["parameter_candidates"] = candidates
    return out


def _parse_llm_json_response(text: str) -> Any:
    """
    Парсит JSON из ответа модели: чистый JSON, ```json ... ```, или текст с JSON-объектом внутри.
    """
    if not text or not str(text).strip():
        return {"raw_text": text}
    s = str(text).strip()
    # Блок markdown ```json ... ``` или ``` ... ```
    fence = re.match(r"^```(?:json)?\s*\r?\n?", s, re.IGNORECASE)
    if fence:
        rest = s[fence.end() :]
        end = rest.rfind("```")
        if end != -1:
            s = rest[:end].strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # Первый { ... последний } (если модель добавила пояснения)
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(s[start : end + 1])
        except Exception:
            pass
    return {"raw_text": text}


def _llm_trade_analyzer_response_parsed_ok(parsed: Any) -> bool:
    """Ожидаемый контракт ответа модели: объект с priorities или in_algorithm_parameter_changes."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("priorities") is not None:
        return True
    if parsed.get("in_algorithm_parameter_changes") is not None:
        return True
    return False


def _analyzer_llm_max_output_tokens(*, game_5m_config_focus: bool) -> int:
    """Лимит completion: большие промпты + длинный JSON; 4096 режет verbose-модели (Claude) на середине объекта."""
    # Важно: get_config_value (файл config.env + merge), не только os.environ — иначе правка в config.env не подхватывается вебом.
    raw = (get_config_value("ANALYZER_LLM_MAX_COMPLETION_TOKENS") or "").strip()
    if raw:
        try:
            return max(1024, min(16384, int(float(raw))))
        except (ValueError, TypeError):
            pass
    # Дефолты выше прежних 4096/6144: полный отчёт + русскоязычные priorities/изменения часто >4k completion.
    return 8192 if game_5m_config_focus else 10240


def _analyzer_llm_temperature() -> float:
    """Ниже OPENAI_TEMPERATURE по умолчанию: меньше разброс формулировок и «уверенных» цифр."""
    raw = (get_config_value("ANALYZER_LLM_TEMPERATURE", "") or "").strip()
    if not raw:
        return 0.05
    try:
        t = float(raw.replace(",", "."))
    except (ValueError, TypeError):
        return 0.05
    return max(0.0, min(0.5, t))


def _normalize_llm_proposed_scalar(value: Any) -> str:
    """Сопоставление proposed из LLM с proposed из отчёта (TIME_EXIT и др.)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value).strip().replace(",", ".")
    sl = s.lower()
    if sl in ("true", "1", "yes"):
        return "true"
    if sl in ("false", "0", "no"):
        return "false"
    try:
        f = float(s)
        if not math.isfinite(f):
            return sl
        rf = round(f, 8)
        if abs(rf - round(rf)) < 1e-9:
            return str(int(round(rf)))
        txt = f"{rf:.8f}".rstrip("0").rstrip(".")
        return txt if txt else str(rf)
    except ValueError:
        return sl


def _time_exit_env_key_prefixes() -> Tuple[str, ...]:
    return (
        "GAME_5M_EARLY_DERISK_",
        "GAME_5M_STALE_REVERSAL_",
        "GAME_5M_EXIT_GUARD_",
    )


def _is_time_exit_early_env_key(env_key: str) -> bool:
    ek = (env_key or "").strip().upper()
    return any(ek.startswith(p) for p in _time_exit_env_key_prefixes())


def _allow_map_time_exit_from_report(report: Dict[str, Any]) -> Tuple[Dict[str, Set[str]], bool, bool]:
    """
    Множество допустимых proposed (нормализованных строк) по env_key из time_exit_early_review.
    Второе значение: insufficient_data_for_ml; третье: есть ли хотя бы одно предложение.
    """
    te = report.get("time_exit_early_review") if isinstance(report.get("time_exit_early_review"), dict) else {}
    insufficient = bool(te.get("insufficient_data_for_ml"))
    cc = te.get("config_candidates") if isinstance(te.get("config_candidates"), dict) else {}
    props = cc.get("proposals") if isinstance(cc.get("proposals"), list) else []
    allow: Dict[str, Set[str]] = {}
    for p in props:
        if not isinstance(p, dict):
            continue
        ek = str(p.get("env_key") or "").strip().upper()
        if not ek or not _is_time_exit_early_env_key(ek):
            continue
        prop = p.get("proposed")
        allow.setdefault(ek, set()).add(_normalize_llm_proposed_scalar(prop))
    return allow, insufficient, bool(allow)


def _sanitize_llm_analysis(
    parsed: Any,
    *,
    report: Dict[str, Any],
    config_env_allow_keys: Optional[Set[str]],
) -> Tuple[Dict[str, Any], List[str]]:
    """
    LLM может вернуть правдоподобный, но нестрогий JSON. Здесь:
    - убираем expected_impact (галлюцинации процентов);
    - TIME_EXIT_EARLY env: proposed только если совпадает с time_exit_early_review.config_candidates.proposals;
    - выкидываем GAME_5M_SIGNAL_CRON_MINUTES;
    - config_env_proposals: allowlist (focus) или любой GAME_5M_* + editable;
    - ограничиваем размеры списков и строк.
    """
    warnings: List[str] = []
    if not isinstance(parsed, dict):
        return ({"raw_text": str(parsed)}, ["LLM analysis is not a JSON object; returned raw_text."])

    out = dict(parsed)
    te_allow, te_insufficient, te_has_rows = _allow_map_time_exit_from_report(report)

    if "expected_impact" in out:
        warnings.append("LLM: поле expected_impact удалено (цифры влияния без строгих данных часто являются догадками).")
        out.pop("expected_impact", None)

    def _cap_list(key: str, max_n: int) -> None:
        v = out.get(key)
        if isinstance(v, list) and len(v) > max_n:
            out[key] = v[:max_n]
            warnings.append(f"LLM: поле {key} обрезано до {max_n} элементов (было {len(v)}).")

    _cap_list("priorities", 4)
    _cap_list("algorithm_change_proposals", 3)
    _cap_list("monitoring_fixes", 3)
    _cap_list("validation_plan", 6)

    mon0 = out.get("monitoring_fixes")
    if isinstance(mon0, list):
        mon_kept: List[Dict[str, Any]] = []
        for row in mon0:
            if not isinstance(row, dict):
                continue
            blob = f"{row.get('issue', '')} {row.get('proposed_fix', '')} {row.get('expected_effect', '')}"
            if "GAME_5M_SIGNAL_CRON_MINUTES" in blob.upper():
                warnings.append("LLM: удалён monitoring_fixes с упоминанием GAME_5M_SIGNAL_CRON_MINUTES.")
                continue
            mon_kept.append(row)
        out["monitoring_fixes"] = mon_kept[:3]

    # in_algorithm_parameter_changes: жёсткие фильтры
    raw_changes = out.get("in_algorithm_parameter_changes")
    kept_changes: List[Dict[str, Any]] = []
    if isinstance(raw_changes, list):
        for row in raw_changes:
            if not isinstance(row, dict):
                continue
            ek = str(row.get("env_key") or "").strip().upper()
            if ek == "GAME_5M_SIGNAL_CRON_MINUTES":
                warnings.append("LLM: удалено in_algorithm_parameter_changes для GAME_5M_SIGNAL_CRON_MINUTES (жёсткий фильтр).")
                continue
            if _is_time_exit_early_env_key(ek):
                if te_insufficient or not te_has_rows:
                    warnings.append(
                        f"LLM: удалено TE-предложение {ek}: time_exit_early_review.insufficient_data_for_ml или нет proposals."
                    )
                    continue
                if ek not in te_allow:
                    warnings.append(
                        f"LLM: удалено TE-предложение {ek}: ключа нет в time_exit_early_review.config_candidates.proposals."
                    )
                    continue
                pv = _normalize_llm_proposed_scalar(row.get("proposed"))
                if pv not in te_allow[ek]:
                    warnings.append(
                        f"LLM: удалено TE-предложение {ek}: proposed={row.get('proposed')!r} "
                        f"не из отчёта (допустимо: {sorted(te_allow[ek])})."
                    )
                    continue
            row2 = dict(row)
            for fld in ("reason_from_metrics", "expected_effect"):
                if fld in row2 and row2[fld] is not None:
                    row2[fld] = str(row2[fld])[:220]
            kept_changes.append(row2)
        out["in_algorithm_parameter_changes"] = kept_changes[:6]
        if isinstance(raw_changes, list) and len(raw_changes) > 6:
            warnings.append(f"LLM: in_algorithm_parameter_changes обрезано до 6 (было {len(raw_changes)}).")
    else:
        out["in_algorithm_parameter_changes"] = []

    cep = out.get("config_env_proposals")
    if isinstance(cep, list):
        filtered: List[Dict[str, Any]] = []
        dropped = 0
        for row in cep:
            if not isinstance(row, dict):
                dropped += 1
                continue
            env_key = str(row.get("env_key") or "").strip().upper()
            pv_raw = row.get("proposed_value")
            pv = str(pv_raw).strip() if pv_raw is not None else ""
            if not env_key or not pv:
                dropped += 1
                continue
            if env_key == "GAME_5M_SIGNAL_CRON_MINUTES":
                dropped += 1
                continue
            if config_env_allow_keys is not None:
                if env_key not in config_env_allow_keys:
                    dropped += 1
                    continue
            elif not env_key.startswith("GAME_5M_"):
                dropped += 1
                continue
            if _is_time_exit_early_env_key(env_key):
                if te_insufficient or not te_has_rows or env_key not in te_allow:
                    dropped += 1
                    continue
                if _normalize_llm_proposed_scalar(pv_raw) not in te_allow[env_key]:
                    dropped += 1
                    warnings.append(
                        f"LLM: удалено config_env_proposals {env_key}={pv!r}: proposed не из time_exit proposals "
                        f"(допустимо: {sorted(te_allow[env_key])})."
                    )
                    continue
            if not is_editable_config_env_key(env_key):
                dropped += 1
                continue
            filtered.append(
                {
                    "env_key": env_key,
                    "proposed_value": pv,
                    "reason_from_metrics": str(row.get("reason_from_metrics") or "")[:220],
                    "confidence": str(row.get("confidence") or "")[:16],
                }
            )
        out["config_env_proposals"] = filtered[:8]
        if dropped:
            warnings.append(
                f"LLM: из config_env_proposals удалено {dropped} строк (allowlist/TE-сверка/editable/GAME_5M_)."
            )

    return out, warnings


def _build_llm_recommendations(
    payload: Dict[str, Any],
    *,
    game_5m_config_focus: bool = False,
) -> Optional[Dict[str, Any]]:
    try:
        from services.llm_service import (
            get_llm_service,
            get_openai_http_timeout_prompt_entry,
            normalize_openai_sdk_proxyapi_base_model,
            parse_compare_models,
        )

        llm = get_llm_service()
        _heavy_llm_http_timeout = get_openai_http_timeout_prompt_entry()
        if not getattr(llm, "client", None):
            return {"status": "disabled", "reason": "LLM client unavailable"}

        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        current_rules = (
            meta.get("current_decision_rule_params")
            if isinstance(meta.get("current_decision_rule_params"), dict)
            else {}
        )
        algorithm_context = {
            "decision_source_expected": meta.get("decision_source_expected"),
            "current_decision_rule_params": current_rules,
            "metric_definitions": meta.get("metric_definitions") or ANALYZER_METRIC_DEFINITIONS,
            "algorithm_digest": ANALYZER_LLM_ALGORITHM_DIGEST,
            "parameter_to_env_key": dict(PARAM_TO_ENV_KEY),
            "llm_critical_notes": [
                "summary.late_polling_signals НЕ означает задержку опроса/cron. См. metric_definitions.late_polling_signals.",
                "Жёсткий запрет: не включать GAME_5M_SIGNAL_CRON_MINUTES в in_algorithm_parameter_changes, config_env_proposals и monitoring_fixes, если главное «доказательство» — late_polling_signals / exit_below_window_mfe_count / формулировки про «late polling» или «запаздывание опроса».",
                "Для недобора до MFE при TAKE_PROFIT предлагай TAKE_PROFIT_*, TAKE_MOMENTUM_FACTOR, trailing/лимиты — не cron.",
                "sum_avoidable_loss_pct на прибыльных сделках может быть велик — см. metric_definitions.sum_avoidable_loss_pct.",
                "Корреляции в отчёте (vol, prob_up, exit_signal) не доказывают причинность — указывай confidence и validation_plan.",
                "Эффективность hanger JSON и TAKE_PROFIT_SUSPEND — report.game5m_hanger_tune_json_review и metric_definitions.game5m_hanger_tune_json_review; "
                "обновление JSON — offline (algorithm_digest.game_5m_hanger_tune_json.offline_regen), не выдумывай путь к файлу.",
            ],
            "parameter_to_env_key_hint": {
                "momentum_buy_min": "GAME_5M_RTH_MOMENTUM_BUY_MIN",
                "rsi_buy_max": "GAME_5M_RSI_BUY_MAX",
                "rsi_strong_buy_max": "GAME_5M_RSI_STRONG_BUY_MAX",
                "volatility_wait_min": "GAME_5M_VOLATILITY_WAIT_MIN",
                "signal_cron_minutes": "GAME_5M_SIGNAL_CRON_MINUTES",
            },
            "scope_for_in_algorithm_changes": [
                "entry thresholds and guards from decision_rule_params",
                "config flags and numeric limits from current_decision_rule_params.config",
                "exit cadence and de-risk knobs from current_decision_rule_params.exit_strategy",
            ],
            "hard_constraints": [
                "read algorithm_digest before interpreting summary and top_cases",
                "FORBIDDEN: GAME_5M_SIGNAL_CRON_MINUTES or signal_cron_minutes in in_algorithm_parameter_changes / config_env_proposals / monitoring_fixes when the cited evidence is late_polling_signals, exit_below_window_mfe_count, or wording like 'late polling' / 'polling delay' — those metrics are exit vs 5m window MFE, not cron latency",
                "report.time_exit_early_review: числовые пороги для GAME_5M_EARLY_DERISK_* / GAME_5M_STALE_REVERSAL_* / GAME_5M_EXIT_GUARD_* бери ТОЛЬКО из report.time_exit_early_review.config_candidates.proposals (current/proposed/env_key). "
                "Не выдумывай proposed, если в proposals нет строки для этого ключа; можно описать качественно при insufficient_data_for_ml.",
                "report.time_exit_early_action_summary: используй dominant_exit_detail, tune_priority_hint, has_actionable_proposals_high_or_medium для приоритизации; не противоречь sample_confidence в proposals.",
                "tie at least one priority or parameter_change to concrete trade_id or ticker from the report when evidence exists",
                "for each in_algorithm_parameter_changes include env_key from parameter_to_env_key when the parameter maps",
                "propose changes only with concrete fields from current_decision_rule_params when tuning thresholds",
                "if there is no matching field, put proposal into algorithm_change_proposals with code_areas from algorithm_digest.code_map",
                "algorithm_change_proposals: name the function or branch (e.g. RSI sell branch), not only the file",
                "do not suggest vague ideas without target parameter, env_key, or code area",
            ],
        }
        if game_5m_config_focus:
            algorithm_context["game_5m_exit_and_tuning_env_keys"] = [
                "GAME_5M_TAKE_PROFIT_PCT",
                "GAME_5M_TAKE_PROFIT_PCT_<TICKER>",
                "GAME_5M_TAKE_PROFIT_MIN_PCT",
                "GAME_5M_TAKE_MOMENTUM_FACTOR",
                "GAME_5M_MAX_POSITION_DAYS",
                "GAME_5M_MAX_POSITION_DAYS_<TICKER>",
                "GAME_5M_MAX_POSITION_MINUTES",
                "GAME_5M_MAX_POSITION_MINUTES_<TICKER>",
                "GAME_5M_SESSION_END_EXIT_MINUTES",
                "GAME_5M_SESSION_END_MIN_PROFIT_PCT",
                "GAME_5M_STOP_LOSS_ENABLED",
                "GAME_5M_STOP_LOSS_PCT",
                "GAME_5M_STOP_TO_TAKE_RATIO",
                "GAME_5M_SIGNAL_CRON_MINUTES",
                "GAME_5M_COOLDOWN_MINUTES",
                "GAME_5M_MAX_ATR_5M_PCT",
                "GAME_5M_MIN_VOLUME_VS_AVG_PCT",
                "GAME_5M_SELL_CONFIRM_BARS",
                "GAME_5M_VOLATILITY_WAIT_MIN",
                "GAME_5M_HANGER_TUNE_JSON",
                "GAME_5M_HANGER_TUNE_APPLY_TAKE",
                "GAME_5M_EARLY_DERISK_ENABLED",
                "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES",
                "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT",
                "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW",
                "GAME_5M_STALE_REVERSAL_EXIT_ENABLED",
                "GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES",
                "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT",
                "GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW",
                "GAME_5M_EXIT_GUARD_FIRST_MINUTES",
            ]
            algorithm_context["focus_instruction"] = (
                "Это УЗКИЙ отчёт по нескольким дням и/или выбранным тикерам/сделкам. "
                "Приоритет: конкретные правки config.env из списка game_5m_exit_and_tuning_env_keys "
                "(полное имя ключа, например GAME_5M_TAKE_PROFIT_PCT_SNDK). "
                "Числа предлагай осторожно, с обоснованием по метрикам отчёта (missed_upside, exit_signal, losses)."
            )
        llm_input: Dict[str, Any] = {"algorithm_context": algorithm_context, "report": payload}
        if isinstance(payload.get("game_5m_config_hints"), list):
            llm_input["heuristic_hints"] = payload["game_5m_config_hints"]
        system_prompt = (
            "Ты senior quant и инженер по торговым системам; у тебя есть только входной JSON (отчёт + algorithm_context).\n"
            "Твоя задача: предложить дельные улучшения — в первую очередь перенастройка GAME_5M_* / порогов из "
            "current_decision_rule_params, во вторую — точечные изменения кода (ветки входа/выхода), если порогом не лечится.\n"
            "Сначала прочитай algorithm_context.algorithm_digest (как считаются метрики окна и откуда context_json), "
            "затем metric_definitions и llm_critical_notes.\n"
            "Используй только факты из отчёта; не выдумывай сделки, тикеры и значения, которых нет во входе.\n"
            "В reason_from_metrics указывай trade_id и/или ticker, если опираешься на top_cases, entry_underperformance_review "
            "или trade_effects.\n"
            "Для параметра из practical_parameter_suggestions или heuristic_hints подставь env_key из "
            "algorithm_context.parameter_to_env_key (или сам ключ GAME_5M_*).\n"
            "Никогда не связывай GAME_5M_SIGNAL_CRON_MINUTES с late_polling_signals: это разные вещи (см. llm_critical_notes). "
            "Не пиши в priorities фразы про «late polling» как про инфраструктуру — говори «выход ниже MFE окна» или «недобор после тейка».\n"
            "Для тейка/стопа опирайся на algorithm_digest.game_5m_take_exit_runtime и "
            "report.meta.current_decision_rule_params.exit_strategy (в т.ч. strategy_params_snapshot, TAKE_MOMENTUM_FACTOR).\n"
            "Пост-обработка на сервере: любые GAME_5M_EARLY_DERISK_* / GAME_5M_STALE_REVERSAL_* / GAME_5M_EXIT_GUARD_* "
            "в in_algorithm_parameter_changes или config_env_proposals будут удалены, если proposed не совпадает с "
            "report.time_exit_early_review.config_candidates.proposals (или если insufficient_data_for_ml). "
            "GAME_5M_SIGNAL_CRON_MINUTES в параметрах/config_env_proposals удаляется всегда.\n"
            "ОБЪЁМ ОТВЕТА (обязательно): priorities — не более 4 строк; in_algorithm_parameter_changes — не более 5 объектов; "
            "algorithm_change_proposals — не более 2; monitoring_fixes — не более 2; validation_plan — не более 3 строк. "
            "В каждом поле reason_from_metrics, expected_effect, change, why_current_algo_not_enough — не более 220 символов; "
            "без повторного перечисления одних и тех же trade_id. Не используй markdown (никаких ```).\n\n"
            "Верни ТОЛЬКО валидный JSON без markdown и без пояснений вне JSON со следующими ключами:\n"
            "{\n"
            "  \"priorities\": [\"...\"],\n"
            "  \"in_algorithm_parameter_changes\": [\n"
            "    {\n"
            "      \"parameter\": \"...\",\n"
            "      \"env_key\": \"GAME_5M_... или пусто если нет в parameter_to_env_key\",\n"
            "      \"current\": \"...\",\n"
            "      \"proposed\": \"число или true/false — без фраз на русском/английском; для GAME_5M_*_PCT только цифры\",\n"
            "      \"where_used\": \"services/recommend_5m.py|services/game_5m.py|config.env\",\n"
            "      \"reason_from_metrics\": \"...\",\n"
            "      \"expected_effect\": \"...\",\n"
            "      \"confidence\": \"low|medium|high\"\n"
            "    }\n"
            "  ],\n"
            "  \"algorithm_change_proposals\": [\n"
            "    {\n"
            "      \"change\": \"...\",\n"
            "      \"why_current_algo_not_enough\": \"...\",\n"
            "      \"code_areas\": [\"services/game_5m.py\", \"services/recommend_5m.py\"],\n"
            "      \"risk\": \"low|medium|high\",\n"
            "      \"expected_effect\": \"...\"\n"
            "    }\n"
            "  ],\n"
            "  \"monitoring_fixes\": [\n"
            "    {\"issue\": \"...\", \"proposed_fix\": \"...\", \"expected_effect\": \"...\"}\n"
            "  ],\n"
            "  \"validation_plan\": [\"...\"]\n"
            "}\n"
        )
        if game_5m_config_focus:
            system_prompt += (
                "\nФОКУС-РЕЖИМ: в том же корневом JSON добавь ключ \"config_env_proposals\" (массив 1–8 объектов) "
                "ПЕРЕД финальной закрывающей скобкой документа — т.е. после \"validation_plan\" поставь запятую и массив:\n"
                "\"config_env_proposals\": [\n"
                "  {\"env_key\": \"GAME_5M_TAKE_PROFIT_PCT_SNDK\", \"proposed_value\": \"6.5\", "
                "\"reason_from_metrics\": \"...\", \"confidence\": \"medium\"}\n"
                "]\n"
                "Ключи env_key только из algorithm_context.game_5m_exit_and_tuning_env_keys; для тикера — суффикс _TICKER.\n"
            )
        cfg_allow: Optional[Set[str]] = None
        if game_5m_config_focus:
            cfg_allow = {
                str(x).strip().upper()
                for x in (algorithm_context.get("game_5m_exit_and_tuning_env_keys") or [])
                if str(x).strip()
            }
        max_tok = _analyzer_llm_max_output_tokens(game_5m_config_focus=game_5m_config_focus)
        out = llm.generate_response(
            messages=[{"role": "user", "content": json.dumps(llm_input, ensure_ascii=False, indent=2)}],
            system_prompt=system_prompt,
            temperature=_analyzer_llm_temperature(),
            max_tokens=max_tok,
            http_timeout_sec=_heavy_llm_http_timeout,
        )
        text = out.get("response") or ""
        finish_reason = out.get("finish_reason")
        parsed = _parse_llm_json_response(text)
        parse_ok = _llm_trade_analyzer_response_parsed_ok(parsed)
        status = "ok" if parse_ok else "parse_failed"
        warnings: List[str] = []
        if finish_reason == "length":
            warnings.append(
                "Ответ обрезан по лимиту completion (finish_reason=length). "
                "Задайте ANALYZER_LLM_MAX_COMPLETION_TOKENS или сократите отчёт (меньше дней / без trade_effects)."
            )
        if not parse_ok:
            if not (text or "").strip():
                warnings.append("Пустой ответ модели — проверьте лимиты API и ключ.")
            elif finish_reason != "length":
                warnings.append("Ответ не распознан как JSON с priorities — см. raw_fragment в analysis.")
            frag = (text or "")[:4000]
            if not isinstance(parsed, dict):
                parsed = {"raw_text": text or ""}
            if frag:
                parsed["raw_fragment"] = frag
        if parse_ok:
            parsed, sanitize_warnings = _sanitize_llm_analysis(
                parsed,
                report=payload if isinstance(payload, dict) else {},
                config_env_allow_keys=cfg_allow,
            )
            warnings.extend(sanitize_warnings)
        ret: Dict[str, Any] = {
            "status": status,
            "parse_ok": parse_ok,
            "finish_reason": finish_reason,
            "warnings": warnings,
            "model": out.get("model"),
            "usage": out.get("usage"),
            "analysis": parsed,
        }
        compare_list = parse_compare_models(load_config())
        if compare_list:
            comp: List[Dict[str, Any]] = []
            msg_user = [{"role": "user", "content": json.dumps(llm_input, ensure_ascii=False, indent=2)}]
            for cbase, cmodel in compare_list:
                bu_n, mo_n = normalize_openai_sdk_proxyapi_base_model(cbase, cmodel)
                if bu_n.rstrip("/") == (llm.base_url or "").rstrip("/") and mo_n == llm.model:
                    continue
                try:
                    res2 = llm.generate_response_with_model(
                        bu_n,
                        mo_n,
                        msg_user,
                        system_prompt=system_prompt,
                        temperature=_analyzer_llm_temperature(),
                        max_tokens=max_tok,
                        http_timeout_sec=_heavy_llm_http_timeout,
                    )
                except Exception as ex:
                    comp.append({"base_url": bu_n, "model": mo_n, "error": str(ex)})
                    continue
                if not res2:
                    comp.append({"base_url": bu_n, "model": mo_n, "error": "no response"})
                    continue
                text2 = (res2.get("response") or "").strip()
                fr2 = res2.get("finish_reason")
                parsed2 = _parse_llm_json_response(text2)
                ok2 = _llm_trade_analyzer_response_parsed_ok(parsed2)
                w2: List[str] = []
                if fr2 == "length":
                    w2.append(
                        "Ответ compare-модели обрезан (finish_reason=length); увеличьте ANALYZER_LLM_MAX_COMPLETION_TOKENS."
                    )
                if ok2 and isinstance(parsed2, dict):
                    parsed2, sw2 = _sanitize_llm_analysis(
                        parsed2,
                        report=payload if isinstance(payload, dict) else {},
                        config_env_allow_keys=cfg_allow,
                    )
                    w2.extend(sw2)
                elif not ok2 and text2:
                    frag2 = text2[:4000]
                    if not isinstance(parsed2, dict):
                        parsed2 = {"raw_text": text2 or ""}
                    if frag2:
                        parsed2["raw_fragment"] = frag2
                comp.append(
                    {
                        "base_url": bu_n,
                        "model": mo_n,
                        "parse_ok": ok2,
                        "finish_reason": fr2,
                        "warnings": w2,
                        "usage": res2.get("usage"),
                        "analysis": parsed2,
                    }
                )
            if comp:
                ret["model_comparison"] = comp
        return ret
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def _build_practical_parameter_suggestions(
    effects: List[TradeEffect],
    summary: Dict[str, Any],
    current_rules: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Грубые, но практичные рекомендации по порогам на основе текущей выборки."""
    if not effects:
        return []
    losses = [e for e in effects if e.realized_pct <= 0]
    wins = [e for e in effects if e.realized_pct > 0]
    suggestions: List[Dict[str, Any]] = []

    # 1) Volatility gate
    loss_high_vol = [e for e in losses if e.entry_vol_5m_pct is not None and e.entry_vol_5m_pct >= 0.6]
    win_high_vol = [e for e in wins if e.entry_vol_5m_pct is not None and e.entry_vol_5m_pct >= 0.6]
    if len(loss_high_vol) >= 3 and len(loss_high_vol) >= len(win_high_vol):
        suggestions.append(
            {
                "parameter": "volatility_wait_min",
                "current": current_rules.get("volatility_wait_min"),
                "proposed": 0.7,
                "why": f"Убыточных сделок при vol>=0.6: {len(loss_high_vol)} (выигрышных: {len(win_high_vol)}).",
                "expected_effect": "Меньше входов в шуме, ниже avoidable loss.",
            }
        )

    # 2) prob_up gate (если доступно)
    losses_high_prob = [e for e in losses if e.entry_prob_up is not None and e.entry_prob_up >= 0.6]
    if len(losses_high_prob) >= 2:
        suggestions.append(
            {
                "parameter": "min_prob_up_for_entry",
                "current": "not enforced",
                "proposed": 0.65,
                "why": f"Даже при prob_up>=0.6 есть {len(losses_high_prob)} убыточных кейсов: стоит повысить порог.",
                "expected_effect": "Фильтрация ложных BUY в пограничной зоне.",
            }
        )

    # 3) Missed upside → конкретный env (иначе practical не попадает в auto_config_override)
    missed = [e.missed_upside_pct or 0.0 for e in effects]
    mean_missed = float(np.mean(missed)) if missed else 0.0
    sum_missed = float(summary.get("sum_missed_upside_pct", 0) or 0)
    if mean_missed >= 1.0 and sum_missed >= 8:
        cfg = load_config()
        try:
            cur_f = float(str(cfg.get("GAME_5M_TAKE_MOMENTUM_FACTOR") or "1.0").replace(",", "."))
        except (ValueError, TypeError):
            cur_f = 1.0
        proposed_f = round(min(cur_f + 0.05, 1.35), 3)
        if proposed_f > cur_f + 1e-9:
            suggestions.append(
                {
                    "parameter": "take_momentum_factor",
                    "current": cur_f,
                    "proposed": proposed_f,
                    "why": (
                        f"Средний missed_upside={mean_missed:.2f}%, суммарно={sum_missed:.2f}% — "
                        "поднять factor тейка от 2h-импульса (частичный недобор vs high окна)."
                    ),
                    "expected_effect": "Выше динамическая цель при сильном импульсе; согласовать с GAME_5M_TAKE_PROFIT_PCT / MIN_PCT.",
                }
            )

    # 4) Выход заметно ниже MFE окна (счётчик late_polling_signals — не про cron)
    late = int(summary.get("late_polling_signals", 0))
    if late >= 3:
        suggestions.append(
            {
                "parameter": "take_vs_window_mfe",
                "current": "фиксированный % тейка",
                "proposed": "пересмотреть тейк/трейлинг относительно intraday high (см. missed_upside)",
                "why": (
                    f"В {late} сделках цена выхода заметно ниже max High 5m-окна вход→выход при недоборе "
                    f"(late_polling_signals / exit_below_window_mfe_count; это не метрика cron)."
                ),
                "expected_effect": "Меньше «недожатого» запаса после тейка, если цель — ближе к пику окна.",
            }
        )
    return suggestions


def _build_critical_case_analysis(effects: List[TradeEffect], limit: int = 5) -> List[Dict[str, Any]]:
    """Разбор критичных кейсов: где одновременно большой убыток и/или большой missed upside."""
    if not effects:
        return []
    ranked = sorted(
        effects,
        key=lambda e: ((-e.realized_pct if e.realized_pct < 0 else 0.0) + (e.missed_upside_pct or 0.0)),
        reverse=True,
    )
    out: List[Dict[str, Any]] = []
    for e in ranked[:limit]:
        reason_parts = []
        if e.realized_pct < 0:
            reason_parts.append(f"loss {e.realized_pct:+.2f}%")
        if (e.missed_upside_pct or 0) > 0.8:
            reason_parts.append(f"missed {e.missed_upside_pct:+.2f}%")
        if e.likely_late_polling:
            reason_parts.append("exit below window MFE (not cron)")
        if e.hold_minutes >= LONG_HOLD_MINUTES:
            reason_parts.append(f"long hold {e.hold_minutes / (24 * 60):.1f}d")
        if e.entry_vol_5m_pct is not None and e.entry_vol_5m_pct >= 0.6:
            reason_parts.append(f"high vol {e.entry_vol_5m_pct:.2f}%")
        if (e.entry_news_impact or "").lower().startswith("негатив"):
            reason_parts.append("negative news at entry")
        action = "Проверить пороги входа/выхода и цель тейка относительно high окна для кейса."
        if e.exit_signal == "TAKE_PROFIT" and (e.missed_upside_pct or 0) > 1.0:
            action = (
                f"{e.ticker} #{e.trade_id}: при TAKE_PROFIT недобор {e.missed_upside_pct:.2f}% — "
                "см. GAME_5M_TAKE_MOMENTUM_FACTOR / потолок тейка (в т.ч. per-ticker GAME_5M_TAKE_PROFIT_PCT_*)."
            )
        elif e.exit_signal == "SELL" and e.realized_pct < -1.5:
            action = "Проверить условие SELL: добавить подтверждение/буфер перед выходом."
        entry_line = ""
        if e.entry_decision or e.entry_reasoning:
            ex = (e.entry_reasoning or "")[:160]
            entry_line = f"entry={e.entry_decision or '?'}" + (f" | {ex}" if ex else "")
        out.append(
            {
                "trade_id": e.trade_id,
                "ticker": e.ticker,
                "exit_signal": e.exit_signal,
                "hold_days": round(e.hold_minutes / (24 * 60), 2),
                "long_hold_ge_7d": e.hold_minutes >= LONG_HOLD_MINUTES,
                "entry_decision": e.entry_decision,
                "entry_reasoning_excerpt": (e.entry_reasoning or "")[:200],
                "entry_context_line": entry_line or None,
                "suggested_config_env_review": _suggested_entry_env_keys(e),
                "realized_pct": round(e.realized_pct, 3),
                "missed_upside_pct": round(e.missed_upside_pct or 0.0, 3),
                "diagnosis": ", ".join(reason_parts) if reason_parts else "key outlier",
                "action": action,
            }
        )
    return out


def _build_game5m_config_hints(
    effects: List[TradeEffect],
    summary: Dict[str, Any],
    hanger_tune_review: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Эвристики по выборке: какие ключи config.env разумно пересмотреть (без автоподстановки чисел)."""
    hints: List[Dict[str, Any]] = []
    if not effects and not (isinstance(hanger_tune_review, dict) and hanger_tune_review.get("enabled")):
        return hints
    from collections import defaultdict

    if effects:
        stuck_poor = [
            e for e in effects if e.hold_minutes >= LONG_HOLD_MINUTES and e.realized_pct <= 0.15
        ]
        if len(stuck_poor) >= 1:
            hints.append(
                {
                    "env_key": "GAME_5M_MAX_POSITION_DAYS",
                    "direction": "review_with_entry_thresholds",
                    "evidence": (
                        f"{len(stuck_poor)}× удержание ≥7 суток при слабом/нулевом результате (≤0.15%) "
                        f"из {len(effects)} сделок"
                    ),
                    "rationale": (
                        "Долго «висит» без ощутимого плюса — чаще всего либо слишком мягкие пороги входа, "
                        "либо слишком мягкие лимиты удержания; см. также entry_underperformance_review в JSON отчёта."
                    ),
                }
            )

        take_by_ticker: Dict[str, List[TradeEffect]] = defaultdict(list)
        for e in effects:
            if e.exit_signal == "TAKE_PROFIT":
                take_by_ticker[e.ticker].append(e)
        for ticker, lst in take_by_ticker.items():
            if len(lst) < 2:
                continue
            missed = [e.missed_upside_pct or 0.0 for e in lst]
            if float(np.mean(missed)) >= 1.0:
                tu = str(ticker).strip().upper()
                hints.append(
                    {
                        "env_key": f"GAME_5M_TAKE_PROFIT_PCT_{tu}",
                        "direction": "review_raise_cap_or_min_pct",
                        "evidence": f"{tu}: {len(lst)}× TAKE_PROFIT, средний missed_upside {float(np.mean(missed)):.2f}%",
                        "rationale": "После тейка цена часто уходит существенно выше — потолок тейка или ранняя цель (импульсная ветка) могут быть узки.",
                    }
                )

        time_exit_loss = [e for e in effects if e.exit_signal == "TIME_EXIT" and e.realized_pct <= 0]
        if len(time_exit_loss) >= 2:
            hints.append(
                {
                    "env_key": "GAME_5M_SESSION_END_MIN_PROFIT_PCT",
                    "direction": "review_with_SESSION_END_EXIT_MINUTES",
                    "evidence": f"{len(time_exit_loss)}× TIME_EXIT без плюса",
                    "rationale": "Закрытие в хвосте сессии даёт слабый или отрицательный результат — порог минимального профита или окно минут.",
                }
            )

        late = int(summary.get("late_polling_signals", 0))
        if late >= 2:
            hints.append(
                {
                    "env_key": "GAME_5M_TAKE_MOMENTUM_FACTOR",
                    "direction": "review_with_TAKE_PROFIT_PCT_and_missed_upside",
                    "evidence": (
                        f"exit_below_window_mfe_count={late} (legacy late_polling_signals) на {len(effects)} сделках"
                    ),
                    "rationale": (
                        "Счётчик — цена выхода заметно ниже max High 5m-окна при недоборе; это не доказательство «медленного cron». "
                        "Сначала тейк/потолок (TAKE_PROFIT_PCT*, TAKE_MOMENTUM_FACTOR), не интервал опроса."
                    ),
                }
            )

        sell_loss = [e for e in effects if e.exit_signal == "SELL" and e.realized_pct < -1.0]
        if len(sell_loss) >= 2:
            hints.append(
                {
                    "env_key": "GAME_5M_SELL_CONFIRM_BARS",
                    "direction": "review_raise",
                    "evidence": f"{len(sell_loss)}× SELL с результатом < -1%",
                    "rationale": "Усилить подтверждение перед выходом по перекупленности (RSI).",
                }
            )

        tp_all = [e for e in effects if e.exit_signal == "TAKE_PROFIT"]
        tp_small = [e for e in tp_all if 0 < e.realized_pct < 2.5]
        if len(tp_all) >= 3 and len(tp_small) >= max(3, int(0.6 * len(tp_all))):
            hints.append(
                {
                    "env_key": "GAME_5M_TAKE_PROFIT_MIN_PCT",
                    "direction": "review_vs_GAME_5M_TAKE_MOMENTUM_FACTOR",
                    "evidence": f"Мелкие TAKE_PROFIT (<2.5%): {len(tp_small)} из {len(tp_all)}",
                    "rationale": "Много ранних тейков — порог MIN_PCT для ветки «тейк от импульса» или factor тянут цель вниз относительно потолка.",
                }
            )

        hanger_review = _build_hanger_v2_review(effects)
        for cand in hanger_review.get("parameter_candidates", []) or []:
            keys = cand.get("env_keys") if isinstance(cand, dict) else None
            if not isinstance(keys, list) or not keys:
                continue
            hints.append(
                {
                    "env_key": keys[0],
                    "related_env_keys": keys,
                    "direction": cand.get("direction") or "review",
                    "evidence": cand.get("evidence") or "Hanger v2 review candidate.",
                    "rationale": "game5m_hanger_v2_review сопоставляет state Hanger v2 с фактическим закрытием и PnL.",
                }
            )

        cont_review = _build_continuation_gate_review(effects)
        for cand in cont_review.get("parameter_candidates", []) or []:
            keys = cand.get("env_keys") if isinstance(cand, dict) else None
            if not isinstance(keys, list) or not keys:
                continue
            hints.append(
                {
                    "env_key": keys[0],
                    "related_env_keys": keys,
                    "direction": cand.get("direction") or "review",
                    "evidence": cand.get("evidence") or "Continuation gate review candidate.",
                    "rationale": "continuation_gate_review сравнивает решение gate с missed upside после закрытия.",
                }
            )

    if isinstance(hanger_tune_review, dict) and hanger_tune_review.get("enabled"):
        for cand in hanger_tune_review.get("parameter_candidates") or []:
            keys = cand.get("env_keys") if isinstance(cand, dict) else None
            if not isinstance(keys, list) or not keys:
                continue
            hints.append(
                {
                    "env_key": keys[0],
                    "related_env_keys": keys,
                    "direction": cand.get("direction") or "review",
                    "evidence": cand.get("evidence") or "Hanger tune JSON review candidate.",
                    "rationale": "game5m_hanger_tune_json_review: капы из JSON, TAKE_PROFIT_SUSPEND vs TAKE_PROFIT, свежесть файла.",
                }
            )
    return hints


PARAM_TO_ENV_KEY: Dict[str, str] = {
    "volatility_wait_min": "GAME_5M_VOLATILITY_WAIT_MIN",
    "sell_confirm_bars": "GAME_5M_SELL_CONFIRM_BARS",
    "momentum_min_session_bars": "GAME_5M_MOMENTUM_MIN_SESSION_BARS",
    "momentum_allow_cross_day_buy": "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY",
    "premarket_momentum_buy_min": "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN",
    "premarket_momentum_block_below": "GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW",
    "rsi_strong_buy_max": "GAME_5M_RSI_STRONG_BUY_MAX",
    "momentum_for_strong_buy_min": "GAME_5M_MOMENTUM_STRONG_BUY_MIN",
    "rsi_buy_max": "GAME_5M_RSI_BUY_MAX",
    "price_to_low5d_multiplier_max": "GAME_5M_PRICE_TO_LOW5D_MULT_MAX",
    "rsi_sell_min": "GAME_5M_RSI_SELL_MIN",
    "rsi_hold_overbought_min": "GAME_5M_RSI_HOLD_OVERBOUGHT_MIN",
    "momentum_buy_min": "GAME_5M_RTH_MOMENTUM_BUY_MIN",
    "rsi_for_momentum_buy_max": "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX",
    "volatility_warn_buy_min": "GAME_5M_VOLATILITY_WARN_BUY_MIN",
    "price_polling_interval": "GAME_5M_SIGNAL_CRON_MINUTES",
    "signal_cron_minutes": "GAME_5M_SIGNAL_CRON_MINUTES",
    "take_momentum_factor": "GAME_5M_TAKE_MOMENTUM_FACTOR",
    "cfg_min_volume_vs_avg_pct": "GAME_5M_MIN_VOLUME_VS_AVG_PCT",
    "cfg_max_atr_5m_pct": "GAME_5M_MAX_ATR_5M_PCT",
    "entry_quality_guard_enabled": "GAME_5M_ENTRY_QUALITY_GUARD_ENABLED",
    "entry_quality_min_rr": "GAME_5M_ENTRY_QUALITY_MIN_RR",
    "entry_quality_min_ev_pct": "GAME_5M_ENTRY_QUALITY_MIN_EV_PCT",
    "max_position_minutes": "GAME_5M_MAX_POSITION_MINUTES",
    "max_position_days": "GAME_5M_MAX_POSITION_DAYS",
    "early_derisk_enabled": "GAME_5M_EARLY_DERISK_ENABLED",
    "early_derisk_min_age_minutes": "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES",
    "early_derisk_max_loss_pct": "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT",
    "early_derisk_momentum_below": "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW",
    "stale_reversal_min_age_minutes": "GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES",
    "stale_reversal_max_pnl_pct": "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT",
    "stale_reversal_momentum_below": "GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW",
    "exit_guard_first_minutes": "GAME_5M_EXIT_GUARD_FIRST_MINUTES",
    "stop_loss_pct_effective": "GAME_5M_STOP_LOSS_PCT",
    "take_profit_pct_effective": "GAME_5M_TAKE_PROFIT_PCT",
    "soft_take_max_pullback_from_high_pct": "GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT",
    "GAME_5M_VOLATILITY_WAIT_MIN": "GAME_5M_VOLATILITY_WAIT_MIN",
    "GAME_5M_SELL_CONFIRM_BARS": "GAME_5M_SELL_CONFIRM_BARS",
    "GAME_5M_MOMENTUM_MIN_SESSION_BARS": "GAME_5M_MOMENTUM_MIN_SESSION_BARS",
    "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY": "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY",
    "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN": "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN",
    "GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW": "GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW",
    "GAME_5M_MIN_VOLUME_VS_AVG_PCT": "GAME_5M_MIN_VOLUME_VS_AVG_PCT",
    "GAME_5M_MAX_ATR_5M_PCT": "GAME_5M_MAX_ATR_5M_PCT",
    "GAME_5M_ENTRY_QUALITY_GUARD_ENABLED": "GAME_5M_ENTRY_QUALITY_GUARD_ENABLED",
    "GAME_5M_ENTRY_QUALITY_MIN_RR": "GAME_5M_ENTRY_QUALITY_MIN_RR",
    "GAME_5M_ENTRY_QUALITY_MIN_EV_PCT": "GAME_5M_ENTRY_QUALITY_MIN_EV_PCT",
    "GAME_5M_MAX_POSITION_MINUTES": "GAME_5M_MAX_POSITION_MINUTES",
    "GAME_5M_MAX_POSITION_DAYS": "GAME_5M_MAX_POSITION_DAYS",
    "GAME_5M_EARLY_DERISK_ENABLED": "GAME_5M_EARLY_DERISK_ENABLED",
    "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES": "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES",
    "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT": "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT",
    "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW": "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW",
    "GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES": "GAME_5M_STALE_REVERSAL_MIN_AGE_MINUTES",
    "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT": "GAME_5M_STALE_REVERSAL_MAX_PNL_PCT",
    "GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW": "GAME_5M_STALE_REVERSAL_MOMENTUM_BELOW",
    "GAME_5M_EXIT_GUARD_FIRST_MINUTES": "GAME_5M_EXIT_GUARD_FIRST_MINUTES",
    "GAME_5M_STOP_LOSS_PCT": "GAME_5M_STOP_LOSS_PCT",
    "GAME_5M_TAKE_PROFIT_PCT": "GAME_5M_TAKE_PROFIT_PCT",
    "GAME_5M_RSI_STRONG_BUY_MAX": "GAME_5M_RSI_STRONG_BUY_MAX",
    "GAME_5M_MOMENTUM_STRONG_BUY_MIN": "GAME_5M_MOMENTUM_STRONG_BUY_MIN",
    "GAME_5M_RSI_BUY_MAX": "GAME_5M_RSI_BUY_MAX",
    "GAME_5M_PRICE_TO_LOW5D_MULT_MAX": "GAME_5M_PRICE_TO_LOW5D_MULT_MAX",
    "GAME_5M_RSI_SELL_MIN": "GAME_5M_RSI_SELL_MIN",
    "GAME_5M_RSI_HOLD_OVERBOUGHT_MIN": "GAME_5M_RSI_HOLD_OVERBOUGHT_MIN",
    "GAME_5M_RTH_MOMENTUM_BUY_MIN": "GAME_5M_RTH_MOMENTUM_BUY_MIN",
    "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX": "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX",
    "GAME_5M_VOLATILITY_WARN_BUY_MIN": "GAME_5M_VOLATILITY_WARN_BUY_MIN",
    "GAME_5M_SIGNAL_CRON_MINUTES": "GAME_5M_SIGNAL_CRON_MINUTES",
    "GAME_5M_TAKE_MOMENTUM_FACTOR": "GAME_5M_TAKE_MOMENTUM_FACTOR",
    "GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT": "GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT",
    "hanger_tune_apply_take": "GAME_5M_HANGER_TUNE_APPLY_TAKE",
    "GAME_5M_HANGER_TUNE_APPLY_TAKE": "GAME_5M_HANGER_TUNE_APPLY_TAKE",
    "GAME_5M_HANGER_TUNE_JSON": "GAME_5M_HANGER_TUNE_JSON",
}


def _normalize_env_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    if v is None:
        return ""
    return str(v).strip()


def _game_5m_env_key_expects_bool(env_key: str) -> bool:
    if "_ENABLED" in env_key:
        return True
    return env_key in (
        "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY",
        "GAME_5M_EARLY_USE_PREMARKET_MOMENTUM",
        "GAME_5M_HANGER_TUNE_APPLY_TAKE",
    )


def _game_5m_env_key_expects_number(env_key: str) -> bool:
    """Ключи GAME_5M с числовым значением в config.env (не bool)."""
    if not env_key.startswith("GAME_5M_") or _game_5m_env_key_expects_bool(env_key):
        return False
    if env_key == "GAME_5M_SIGNAL_CRON_MINUTES":
        return False
    markers = (
        "_PCT",
        "_MIN",
        "_MAX",
        "_FACTOR",
        "_RATIO",
        "_MINUTES",
        "_DAYS",
        "_BARS",
        "_EV_",
        "_RR",
    )
    return any(m in env_key for m in markers)


def _proposed_str_valid_for_env_key(env_key: str, proposed_str: str) -> tuple[bool, str]:
    """
    Отсекает ответы LLM вроде «слегка повысить тейк» для числовых ключей — иначе ломается config.env.
    """
    if not proposed_str or not proposed_str.strip():
        return False, "пустое значение"
    s = proposed_str.strip().replace(",", ".").rstrip("%").strip()
    if _game_5m_env_key_expects_bool(env_key):
        low = s.lower()
        if low in ("true", "false", "1", "0", "yes", "no"):
            return True, ""
        return False, "ожидалось true/false"
    if _game_5m_env_key_expects_number(env_key):
        if len(proposed_str) > 48:
            return False, "слишком длинная строка (похоже на текст, а не число)"
        if re.search(r"[\u0400-\u04FF]", proposed_str):
            return False, "в значении есть кириллица — укажите число"
        try:
            float(s)
        except ValueError:
            return False, "не число: задайте proposed числом (например 5.5), без пояснений"
        return True, ""
    return True, ""


def _cron_row_ties_mfe_exit_metric_to_polling(row: dict) -> bool:
    """
    True, если текст строки ошибочно связывает cron с late_polling_signals / «запаздыванием опроса».
    Такие предложения не применяем в auto_config_override (метрика — выход vs MFE окна, не интервал cron).
    """
    parts = [
        row.get("reason_from_metrics"),
        row.get("why"),
        row.get("expected_effect"),
        row.get("parameter"),
        row.get("proposed"),
        row.get("issue"),
        row.get("proposed_fix"),
        row.get("reason"),
    ]
    blob = " ".join(str(p) for p in parts if p is not None and str(p).strip())
    low = blob.lower()
    if "late_polling" in low or "late polling" in low:
        return True
    if "polling signals" in low or "polling signal" in low or "signal polling" in low:
        return True
    if "запаздыван" in blob.lower():
        return True
    if "exit_below_window" in low.replace(" ", "_") and ("cron" in low or "polling" in low):
        return True
    return False


def _coerce_polling_minutes(proposed: Any) -> Optional[str]:
    """LLM часто возвращает '1m near exit levels' — извлекаем целые минуты для GAME_5M_SIGNAL_CRON_MINUTES."""
    if isinstance(proposed, (int, float)) and not isinstance(proposed, bool):
        m = int(round(float(proposed)))
        return str(max(1, min(30, m)))
    s = str(proposed).strip().lower()
    if not s:
        return None
    m = re.search(r"(\d+)\s*m\b", s)
    if m:
        return str(max(1, min(30, int(m.group(1)))))
    m2 = re.search(r"\b(\d+)\b", s)
    if m2:
        return str(max(1, min(30, int(m2.group(1)))))
    return None


def _build_auto_config_override(report: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    llm = report.get("llm") if isinstance(report.get("llm"), dict) else {}
    llm_ana = llm.get("analysis") if isinstance(llm.get("analysis"), dict) else {}
    llm_changes = llm_ana.get("in_algorithm_parameter_changes")
    if not isinstance(llm_changes, list):
        llm_changes = llm_ana.get("threshold_changes")
    llm_changes = llm_changes if isinstance(llm_changes, list) else []

    updates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for row in llm_changes:
        if not isinstance(row, dict):
            continue
        parameter = str(row.get("parameter") or "").strip()
        env_key_hint = str(row.get("env_key") or "").strip()
        proposed = row.get("proposed")
        if proposed is None:
            continue
        if not parameter and not env_key_hint:
            continue
        env_key = PARAM_TO_ENV_KEY.get(parameter) or PARAM_TO_ENV_KEY.get(parameter.upper())
        # Важно: не привязываем распознавание "похоже на env key" к is_editable_config_env_key(),
        # потому что whitelist редактируемых ключей может быть закэширован до обновления config.env.example.
        # Фильтрация "можно применять" всё равно ниже, отдельной проверкой is_editable_config_env_key(env_key).
        if not env_key and parameter.startswith("GAME_5M_"):
            env_key = parameter
        if not env_key and env_key_hint.startswith("GAME_5M_"):
            env_key = env_key_hint
        if not env_key:
            skipped.append(
                {
                    "parameter": parameter,
                    "reason": "no_env_mapping",
                    "note": "Нужна доработка алгоритма/маппинга (ключ не найден).",
                }
            )
            continue
        if env_key == "GAME_5M_SIGNAL_CRON_MINUTES" and _cron_row_ties_mfe_exit_metric_to_polling(row):
            skipped.append(
                {
                    "parameter": parameter,
                    "env_key": env_key,
                    "reason": "cron_blocked_late_polling_misread",
                    "note": (
                        "late_polling_signals / «запаздывание опроса» не доказывают интервал cron; "
                        "см. metric_definitions. Предложение не применяется автоматически."
                    ),
                }
            )
            continue
        if env_key in seen:
            continue
        if not is_editable_config_env_key(env_key):
            skipped.append(
                {
                    "parameter": parameter,
                    "env_key": env_key,
                    "reason": "not_editable",
                    "note": "Ключ не разрешён для веб-редактора config.env.",
                }
            )
            continue
        current = cfg.get(env_key, "")
        if env_key == "GAME_5M_SIGNAL_CRON_MINUTES":
            proposed_str = _coerce_polling_minutes(proposed)
            if proposed_str is None:
                skipped.append(
                    {
                        "parameter": parameter,
                        "env_key": env_key,
                        "reason": "unparseable_polling",
                        "note": "Ожидались минуты (например 1 или 1m).",
                    }
                )
                continue
        else:
            proposed_str = _normalize_env_value(proposed)
        ok_pv, pv_note = _proposed_str_valid_for_env_key(env_key, proposed_str)
        if not ok_pv:
            skipped.append(
                {
                    "parameter": parameter,
                    "env_key": env_key,
                    "reason": "invalid_proposed",
                    "note": pv_note,
                }
            )
            continue
        seen.add(env_key)
        updates.append(
            {
                "env_key": env_key,
                "current": current,
                "proposed": proposed_str,
                "source_parameter": parameter,
                "reason": row.get("reason_from_metrics") or row.get("why") or row.get("expected_effect") or "",
            }
        )

    proposals = llm_ana.get("config_env_proposals")
    if isinstance(proposals, list):
        for row in proposals:
            if not isinstance(row, dict):
                continue
            env_key = str(row.get("env_key") or "").strip()
            proposed = row.get("proposed_value")
            if not env_key or proposed is None:
                continue
            if env_key in seen:
                continue
            if not is_editable_config_env_key(env_key):
                skipped.append(
                    {
                        "parameter": env_key,
                        "env_key": env_key,
                        "reason": "not_editable",
                        "note": "Ключ не в списке редактируемых.",
                    }
                )
                continue
            if env_key == "GAME_5M_SIGNAL_CRON_MINUTES" and _cron_row_ties_mfe_exit_metric_to_polling(row):
                skipped.append(
                    {
                        "parameter": env_key,
                        "env_key": env_key,
                        "reason": "cron_blocked_late_polling_misread",
                        "note": (
                            "late_polling_signals не доказывает интервал cron; предложение из config_env_proposals отклонено."
                        ),
                    }
                )
                continue
            current = cfg.get(env_key, "")
            if env_key == "GAME_5M_SIGNAL_CRON_MINUTES":
                proposed_str = _coerce_polling_minutes(proposed)
                if proposed_str is None:
                    skipped.append(
                        {
                            "parameter": env_key,
                            "env_key": env_key,
                            "reason": "unparseable_polling",
                            "note": "Ожидались минуты.",
                        }
                    )
                    continue
            else:
                proposed_str = _normalize_env_value(proposed)
            ok_pv, pv_note = _proposed_str_valid_for_env_key(env_key, proposed_str)
            if not ok_pv:
                skipped.append(
                    {
                        "parameter": env_key,
                        "env_key": env_key,
                        "reason": "invalid_proposed",
                        "note": pv_note,
                    }
                )
                continue
            seen.add(env_key)
            updates.append(
                {
                    "env_key": env_key,
                    "current": current,
                    "proposed": proposed_str,
                    "source_parameter": "config_env_proposals",
                    "reason": str(row.get("reason_from_metrics") or row.get("reason") or ""),
                }
            )

    practical = report.get("practical_parameter_suggestions")
    if isinstance(practical, list):
        for row in practical:
            if not isinstance(row, dict):
                continue
            parameter = str(row.get("parameter") or "").strip()
            proposed = row.get("proposed")
            if not parameter or proposed is None:
                continue
            env_key = PARAM_TO_ENV_KEY.get(parameter)
            if not env_key and parameter.startswith("GAME_5M_") and is_editable_config_env_key(parameter):
                env_key = parameter
            if not env_key or env_key in seen:
                continue
            if not is_editable_config_env_key(env_key):
                continue
            current = cfg.get(env_key, "")
            if env_key == "GAME_5M_SIGNAL_CRON_MINUTES":
                proposed_str = _coerce_polling_minutes(proposed)
                if proposed_str is None:
                    continue
            else:
                proposed_str = _normalize_env_value(proposed)
            ok_pv, pv_note = _proposed_str_valid_for_env_key(env_key, proposed_str)
            if not ok_pv:
                skipped.append(
                    {
                        "parameter": parameter,
                        "env_key": env_key,
                        "reason": "invalid_proposed",
                        "note": pv_note,
                    }
                )
                continue
            seen.add(env_key)
            updates.append(
                {
                    "env_key": env_key,
                    "current": current,
                    "proposed": proposed_str,
                    "source_parameter": parameter,
                    "reason": row.get("why") or row.get("expected_effect") or "",
                }
            )

    # Крон — частый шум в индексе [0]; торговые пороги оставляем первыми.
    updates.sort(
        key=lambda u: (
            u.get("env_key") == "GAME_5M_SIGNAL_CRON_MINUTES",
            u.get("env_key") or "",
        )
    )

    env_lines = [f"{u['env_key']}={u['proposed']}" for u in updates]
    manual_notes: List[str] = []
    if any(u.get("env_key") == "GAME_5M_SIGNAL_CRON_MINUTES" for u in updates):
        manual_notes.append(
            "GAME_5M_SIGNAL_CRON_MINUTES должен совпадать с crontab (см. setup_cron.sh: */N * * * * ... send_sndk_signal_cron.py). "
            "После смены N — обновите crontab вручную или запустите ./setup_cron.sh (перезапишет весь блок LSE)."
        )
    return {
        "updates": updates,
        "skipped": skipped,
        "env_block": "\n".join(env_lines),
        "can_apply": len(updates) > 0,
        "manual_notes": manual_notes,
    }


def _get_current_decision_rule_params() -> Dict[str, Any]:
    """Текущие параметры правил из кода/config (для LLM, даже если в старых сделках нет snapshot)."""
    try:
        from config_loader import get_config_value
        from services.recommend_5m import GAME_5M_RULE_VERSION, get_decision_5m_rule_thresholds
        from services.game_5m import (
            _effective_stop_loss_pct,
            _effective_take_profit_pct,
            _game_5m_stop_loss_enabled,
            _max_position_minutes,
            get_strategy_params,
        )

        def _cfg_str(key: str, default: str = "") -> Optional[str]:
            v = (get_config_value(key, default) or "").strip()
            return v or None

        def _cfg_bool(key: str, default: str = "false") -> bool:
            return (_cfg_str(key, default) or "").lower() in ("1", "true", "yes")

        def _cfg_int(key: str, default: str) -> Optional[int]:
            raw = _cfg_str(key, default)
            if raw is None:
                return None
            try:
                return int(raw)
            except Exception:
                return None

        def _cfg_float(key: str, default: str) -> Optional[float]:
            raw = _cfg_str(key, default)
            if raw is None:
                return None
            try:
                return float(raw)
            except Exception:
                return None

        th = get_decision_5m_rule_thresholds()
        try:
            cron_min = int((get_config_value("GAME_5M_SIGNAL_CRON_MINUTES", "5") or "5").strip())
        except (ValueError, TypeError):
            cron_min = 5
        cron_min = max(1, min(30, cron_min))

        sp = get_strategy_params()
        # Пороги при отсутствии momentum в снимке (= потолок тейка и стоп от него); в кроне подставляется живой momentum_2h.
        take_pct = _effective_take_profit_pct(None, ticker=None)
        stop_pct = _effective_stop_loss_pct(None, ticker=None)
        return {
            "rule_version": GAME_5M_RULE_VERSION,
            "source_fn": "services.recommend_5m.get_decision_5m",
            **th,
            "signal_cron_minutes": cron_min,
            "news_negative_min": 0.4,
            "news_very_negative_min": 0.35,
            "news_positive_min": 0.65,
            "cfg_min_volume_vs_avg_pct": _cfg_str("GAME_5M_MIN_VOLUME_VS_AVG_PCT", ""),
            "cfg_max_atr_5m_pct": _cfg_str("GAME_5M_MAX_ATR_5M_PCT", ""),
            "config": {
                "GAME_5M_SIGNAL_CRON_MINUTES": cron_min,
                "GAME_5M_RSI_STRONG_BUY_MAX": th.get("rsi_strong_buy_max"),
                "GAME_5M_MOMENTUM_STRONG_BUY_MIN": th.get("momentum_for_strong_buy_min"),
                "GAME_5M_RSI_BUY_MAX": th.get("rsi_buy_max"),
                "GAME_5M_PRICE_TO_LOW5D_MULT_MAX": th.get("price_to_low5d_multiplier_max"),
                "GAME_5M_RSI_SELL_MIN": th.get("rsi_sell_min"),
                "GAME_5M_RSI_HOLD_OVERBOUGHT_MIN": th.get("rsi_hold_overbought_min"),
                "GAME_5M_RTH_MOMENTUM_BUY_MIN": th.get("momentum_buy_min"),
                "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX": th.get("rsi_for_momentum_buy_max"),
                "GAME_5M_VOLATILITY_WARN_BUY_MIN": th.get("volatility_warn_buy_min"),
                "GAME_5M_VOLATILITY_WAIT_MIN": _cfg_float("GAME_5M_VOLATILITY_WAIT_MIN", "0.7"),
                "GAME_5M_SELL_CONFIRM_BARS": _cfg_int("GAME_5M_SELL_CONFIRM_BARS", "2"),
                "GAME_5M_MOMENTUM_MIN_SESSION_BARS": _cfg_int("GAME_5M_MOMENTUM_MIN_SESSION_BARS", "7"),
                "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY": _cfg_bool("GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY", "false"),
                "GAME_5M_EARLY_USE_PREMARKET_MOMENTUM": _cfg_bool("GAME_5M_EARLY_USE_PREMARKET_MOMENTUM", "true"),
                "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN": _cfg_float("GAME_5M_PREMARKET_MOMENTUM_BUY_MIN", "0.5"),
                "GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW": _cfg_float("GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW", "-2.0"),
                "GAME_5M_MIN_VOLUME_VS_AVG_PCT": _cfg_float("GAME_5M_MIN_VOLUME_VS_AVG_PCT", ""),
                "GAME_5M_MAX_ATR_5M_PCT": _cfg_float("GAME_5M_MAX_ATR_5M_PCT", ""),
                "GAME_5M_ENTRY_QUALITY_GUARD_ENABLED": _cfg_bool("GAME_5M_ENTRY_QUALITY_GUARD_ENABLED", "false"),
                "GAME_5M_ENTRY_QUALITY_MIN_RR": _cfg_float("GAME_5M_ENTRY_QUALITY_MIN_RR", "1.2"),
                "GAME_5M_ENTRY_QUALITY_MIN_EV_PCT": _cfg_float("GAME_5M_ENTRY_QUALITY_MIN_EV_PCT", "0.0"),
                "GAME_5M_CATBOOST_ENABLED": _cfg_bool("GAME_5M_CATBOOST_ENABLED", "false"),
                "GAME_5M_CATBOOST_MODEL_PATH": _cfg_str("GAME_5M_CATBOOST_MODEL_PATH", ""),
                "GAME_5M_CATBOOST_APPEND_REASONING": _cfg_bool("GAME_5M_CATBOOST_APPEND_REASONING", "false"),
                "GAME_5M_CATBOOST_FUSION": _cfg_str("GAME_5M_CATBOOST_FUSION", "none"),
                "GAME_5M_CATBOOST_HOLD_BELOW_P": _cfg_float("GAME_5M_CATBOOST_HOLD_BELOW_P", "0.45"),
            },
            "exit_strategy": {
                "max_position_minutes": _max_position_minutes(),
                "stop_loss_enabled": _game_5m_stop_loss_enabled(),
                "stop_loss_pct_effective": stop_pct,
                "take_profit_pct_effective": take_pct,
                "strategy_params_snapshot": {
                    "take_profit_pct_cap": sp.get("take_profit_pct"),
                    "take_profit_min_pct": sp.get("take_profit_min_pct"),
                    "stop_loss_pct_config": sp.get("stop_loss_pct"),
                    "stop_to_take_ratio": sp.get("stop_to_take_ratio"),
                    "take_profit_rule": sp.get("take_profit_rule"),
                    "stop_loss_rule": sp.get("stop_loss_rule"),
                },
                "GAME_5M_TAKE_MOMENTUM_FACTOR": _cfg_float("GAME_5M_TAKE_MOMENTUM_FACTOR", "1.0"),
                "GAME_5M_EXIT_ONLY_TAKE": _cfg_bool("GAME_5M_EXIT_ONLY_TAKE", "false"),
                "GAME_5M_SESSION_END_EXIT_MINUTES": _cfg_int("GAME_5M_SESSION_END_EXIT_MINUTES", "30"),
                "GAME_5M_SESSION_END_MIN_PROFIT_PCT": _cfg_float("GAME_5M_SESSION_END_MIN_PROFIT_PCT", "0.3"),
                "GAME_5M_EARLY_DERISK_ENABLED": _cfg_bool("GAME_5M_EARLY_DERISK_ENABLED", "false"),
                "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES": _cfg_int("GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES", "180"),
                "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT": _cfg_float("GAME_5M_EARLY_DERISK_MAX_LOSS_PCT", "-2.0"),
                "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW": _cfg_float("GAME_5M_EARLY_DERISK_MOMENTUM_BELOW", "0.0"),
                "GAME_5M_ALLOW_PYRAMID_BUY": _cfg_bool("GAME_5M_ALLOW_PYRAMID_BUY", "false"),
                "GAME_5M_SOFT_TAKE_NEAR_HIGH_ENABLED": _cfg_bool("GAME_5M_SOFT_TAKE_NEAR_HIGH_ENABLED", "true"),
                "GAME_5M_SOFT_TAKE_NEAR_HIGH_MIN_PCT": _cfg_float("GAME_5M_SOFT_TAKE_NEAR_HIGH_MIN_PCT", "2.0"),
                "GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT": _cfg_float(
                    "GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT", "0.35"
                ),
                "GAME_5M_HANGER_TUNE_JSON": (get_config_value("GAME_5M_HANGER_TUNE_JSON", "") or "").strip(),
                "GAME_5M_HANGER_TUNE_APPLY_TAKE": _cfg_bool("GAME_5M_HANGER_TUNE_APPLY_TAKE", "false"),
            },
        }
    except Exception:
        return {}


def _trade_qualifies_for_game5m_catboost(strategy: str, trade_pnl: Any) -> bool:
    """CatBoost entry-модель только для сделок, открытых в игре GAME_5M."""
    su = (strategy or "").strip().upper()
    if su == "GAME_5M":
        return True
    if su == "ALL":
        es = (getattr(trade_pnl, "entry_strategy", None) or "").strip()
        return es == "GAME_5M"
    return False


def _build_catboost_entry_backtest(strategy: str, closed: List[Any], effects: List[TradeEffect]) -> Dict[str, Any]:
    """
    Бэктест скора CatBoost на закрытых сделках: сохранённый context_json на BUY → P(благоприятный исход)
    vs фактический realized_pct (прибыль / не прибыль).

    Не применяется к портфельной игре (другой контекст входа). При strategy=ALL учитываются только
    сделки с entry_strategy GAME_5M.
    """
    from services.catboost_5m_signal import predict_entry_favorability_from_saved_context

    su = (strategy or "").strip().upper()
    if su == "PORTFOLIO":
        return {
            "mode": "skipped",
            "note": "Модель CatBoost в репозитории обучена на входах GAME_5M; для портфеля отдельный контур не подключён.",
        }
    if su not in ("GAME_5M", "ALL"):
        return {
            "mode": "skipped",
            "note": f"Стратегия {strategy!r}: бэктест CatBoost только при strategy=GAME_5M или ALL.",
        }

    by_tid: Dict[int, Any] = {}
    for t in closed:
        try:
            tid = int(getattr(t, "trade_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if tid:
            by_tid[tid] = t

    per_trade: List[Dict[str, Any]] = []
    paired: List[Tuple[float, bool]] = []  # (p_good, win) при status ok
    skipped_reasons: Dict[str, int] = {}

    for e in effects:
        tp = by_tid.get(int(e.trade_id))
        if not tp or not _trade_qualifies_for_game5m_catboost(strategy, tp):
            continue
        pred = predict_entry_favorability_from_saved_context(str(e.ticker), getattr(tp, "context_json", None))
        st = pred.get("catboost_signal_status") or ""
        row: Dict[str, Any] = {
            "trade_id": e.trade_id,
            "ticker": e.ticker,
            "catboost_signal_status": st,
            "catboost_entry_proba_good": pred.get("catboost_entry_proba_good"),
            "realized_pct": round(e.realized_pct, 4),
            "win": bool(e.realized_pct > 0),
            "estimated_upside_pct_day_at_entry": None,
            "prob_up_at_entry": None,
            "price_forecast_5m_summary_excerpt": None,
        }
        if st == "ok" and pred.get("catboost_entry_proba_good") is not None:
            try:
                pg = float(pred["catboost_entry_proba_good"])
                paired.append((pg, bool(e.realized_pct > 0)))
            except (TypeError, ValueError):
                pass
        else:
            skipped_reasons[st] = skipped_reasons.get(st, 0) + 1
        try:
            ctx = normalize_entry_context(getattr(tp, "context_json", None))
            if ctx:
                eu = ctx.get("estimated_upside_pct_day")
                if eu is not None:
                    row["estimated_upside_pct_day_at_entry"] = round(float(eu), 3)
                pu = ctx.get("prob_up")
                if pu is not None:
                    row["prob_up_at_entry"] = round(float(pu), 4)
                pfs = ctx.get("price_forecast_5m_summary")
                if isinstance(pfs, str) and pfs.strip():
                    row["price_forecast_5m_summary_excerpt"] = pfs.strip()[:120] + ("…" if len(pfs.strip()) > 120 else "")
        except Exception:
            pass
        per_trade.append(row)

    out: Dict[str, Any] = {
        "mode": "game5m_entry_context",
        "description": (
            "По каждой закрытой сделке GAME_5M: CatBoostClassifier на признаках из context_json на BUY "
            "(как train_game5m_catboost.py, без подмешивания текущей корреляции). Сравнение с фактом realized_pct."
        ),
        "trades_considered": len(per_trade),
        "trades_scored_ok": len(paired),
        "skipped_by_status": skipped_reasons,
        "per_trade": per_trade,
    }

    if len(paired) < 2:
        out["calibration"] = {
            "note": f"Мало пар «скор vs исход» (n={len(paired)}); включите GAME_5M_CATBOOST_ENABLED и наличие .cbm, либо накопите закрытия.",
        }
        return out

    wins_p = [p for p, w in paired if w]
    loss_p = [p for p, w in paired if not w]
    out["calibration"] = {
        "mean_p_given_win": round(float(np.mean(wins_p)), 4) if wins_p else None,
        "mean_p_given_loss": round(float(np.mean(loss_p)), 4) if loss_p else None,
        "win_rate_pct": round(100.0 * sum(1 for _, w in paired if w) / len(paired), 2),
        "buckets": [],
    }
    edges = [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.0001)]
    for lo, hi in edges:
        sub = [(p, w) for p, w in paired if lo <= p < hi]
        if not sub:
            out["calibration"]["buckets"].append(
                {"p_range": f"[{lo:.2f},{hi:.2f})", "n": 0, "win_rate_pct": None}
            )
            continue
        wr = 100.0 * sum(1 for _, w in sub if w) / len(sub)
        out["calibration"]["buckets"].append(
            {"p_range": f"[{lo:.2f},{hi:.2f})", "n": len(sub), "win_rate_pct": round(wr, 2)}
        )

    # Простая связь «прогноз upside на входе» vs результат (только где поле было)
    ups_pairs: List[Tuple[float, float]] = []
    for row in per_trade:
        eu = row.get("estimated_upside_pct_day_at_entry")
        if eu is None:
            continue
        try:
            ups_pairs.append((float(eu), float(row["realized_pct"])))
        except (TypeError, ValueError):
            pass
    if len(ups_pairs) >= 3:
        xs = np.array([a[0] for a in ups_pairs], dtype=float)
        ys = np.array([a[1] for a in ups_pairs], dtype=float)
        corr = float(np.corrcoef(xs, ys)[0, 1]) if np.std(xs) > 1e-9 and np.std(ys) > 1e-9 else None
        out["price_context_at_entry"] = {
            "n_with_estimated_upside_pct_day": len(ups_pairs),
            "corr_estimated_upside_vs_realized_pct": round(corr, 4) if corr is not None and math.isfinite(corr) else None,
            "note": "Корреляция справочная: estimated_upside_pct_day из входа vs итог сделки; не замена CatBoost.",
        }
    return out


def _build_game5m_catboost_fusion_entry_review(
    strategy: str,
    closed: List[Any],
    effects: List[TradeEffect],
) -> Dict[str, Any]:
    """
    Сводка по влиянию CatBoost на вход: поля из BUY context_json (после деплоя — technical_decision_*, catboost_*).

    would_hold_at_runtime_p_threshold: контрфакт при текущем GAME_5M_CATBOOST_HOLD_BELOW_P и сохранённом P
    (вход уже состоялся; для старых сделок без P в JSON поле false).
    """
    su = (strategy or "").strip().upper()
    if su == "PORTFOLIO":
        return {"mode": "skipped", "note": "Раздел только для GAME_5M и ALL (входы GAME_5M)."}
    if su not in ("GAME_5M", "ALL"):
        return {"mode": "skipped", "note": f"Стратегия {strategy!r}: обзор fusion только GAME_5M / ALL."}

    cb_on = (get_config_value("GAME_5M_CATBOOST_ENABLED", "false") or "false").strip().lower() in ("1", "true", "yes")
    try:
        p_min_rt = float((get_config_value("GAME_5M_CATBOOST_HOLD_BELOW_P", "0.45") or "0.45").strip())
    except (ValueError, TypeError):
        p_min_rt = 0.45
    fusion_rt = (get_config_value("GAME_5M_CATBOOST_FUSION", "none") or "none").strip().lower()

    by_tid: Dict[int, Any] = {}
    for t in closed:
        try:
            tid = int(getattr(t, "trade_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if tid:
            by_tid[tid] = t

    per_trade: List[Dict[str, Any]] = []
    n_considered = 0
    n_with_snapshot = 0
    n_would_hold = 0
    n_would_hold_wins = 0
    n_would_hold_losses = 0

    for e in effects:
        tp = by_tid.get(int(e.trade_id))
        if not tp or not _trade_qualifies_for_game5m_catboost(strategy, tp):
            continue
        n_considered += 1
        ctx = normalize_entry_context(getattr(tp, "context_json", None))
        core = ctx.get("technical_decision_core")
        eff = ctx.get("technical_decision_effective")
        if core is None:
            core = ctx.get("decision")
        if eff is None:
            eff = ctx.get("decision")
        branch = ctx.get("technical_entry_branch")
        st = ctx.get("catboost_signal_status")
        p_raw = ctx.get("catboost_entry_proba_good")
        p_ok: Optional[float] = None
        if p_raw is not None:
            try:
                p_ok = float(p_raw)
            except (TypeError, ValueError):
                p_ok = None
        fusion_note = ctx.get("catboost_fusion_note")
        fusion_mode_ctx = ctx.get("catboost_fusion_mode")

        has_snap = bool(
            st is not None
            or p_ok is not None
            or ctx.get("technical_decision_core") is not None
            or ctx.get("technical_decision_effective") is not None
        )
        if has_snap:
            n_with_snapshot += 1

        would_hold = False
        if fusion_rt == "hold_if_buy_below_p" and st == "ok" and p_ok is not None:
            if core in ("BUY", "STRONG_BUY") and p_ok < p_min_rt:
                would_hold = True
        if would_hold:
            n_would_hold += 1
            if e.realized_pct > 0:
                n_would_hold_wins += 1
            else:
                n_would_hold_losses += 1

        per_trade.append(
            {
                "trade_id": e.trade_id,
                "ticker": e.ticker,
                "technical_entry_branch": branch,
                "technical_decision_core": core,
                "technical_decision_effective": eff,
                "catboost_signal_status": st,
                "catboost_entry_proba_good": round(p_ok, 4) if p_ok is not None else None,
                "catboost_fusion_note_entry": fusion_note,
                "catboost_fusion_mode_entry": fusion_mode_ctx,
                "would_hold_at_runtime_p_threshold": would_hold,
                "realized_pct": round(float(e.realized_pct), 4),
                "win": bool(e.realized_pct > 0),
            }
        )

    per_trade_recent = list(reversed(per_trade))[:40]
    coverage_note: Optional[str] = None
    if n_considered and n_with_snapshot < n_considered:
        coverage_note = (
            "В части сделок в context_json ещё нет technical_decision_* / catboost_* "
            "(записи до деплоя с расширенным deal_params). После новых входов покрытие вырастет."
        )

    return {
        "mode": "game5m_entry_context",
        "description": (
            "По закрытым сделкам GAME_5M: снимок из BUY context_json (core/effective, P, статус CatBoost). "
            "would_hold_at_runtime_p_threshold — оценка при текущем пороге HOLD_BELOW_P; сделка уже открывалась в прошлом."
        ),
        "runtime": {
            "GAME_5M_CATBOOST_ENABLED": cb_on,
            "GAME_5M_CATBOOST_FUSION": fusion_rt,
            "GAME_5M_CATBOOST_HOLD_BELOW_P": p_min_rt,
        },
        "trades_considered": n_considered,
        "trades_with_catboost_snapshot": n_with_snapshot,
        "would_hold_at_runtime_threshold_count": n_would_hold,
        "would_hold_breakdown": {
            "among_hypothetical_holds_would_be_losses": n_would_hold_losses,
            "among_hypothetical_holds_would_be_wins": n_would_hold_wins,
        },
        "context_coverage_note": coverage_note,
        "per_trade_recent": per_trade_recent,
    }


def _recovery_delayed_close_after_exit(
    df: Optional[pd.DataFrame],
    exit_ts_et: pd.Timestamp,
    delay_bars: int,
) -> Optional[float]:
    """Close цены на K-м 5m баре строго после exit_ts (упрощение для D3)."""
    if df is None or getattr(df, "empty", True) or delay_bars <= 0:
        return None
    try:
        m = df["datetime"] > exit_ts_et
        fwd = df.loc[m]
        if fwd is None or fwd.empty:
            return None
        ix = min(int(delay_bars), len(fwd)) - 1
        if ix < 0:
            return None
        return float(fwd["Close"].iloc[ix])
    except Exception:
        return None


def _recovery_d4a_k_bars_candidates() -> List[int]:
    """K для офлайн-оценки пролонгации: всегда включаем LIVE_DEFER_BARS + сетку из env (или 3,6,9,12)."""
    try:
        live_k = int((get_config_value("GAME_5M_RECOVERY_LIVE_DEFER_BARS", "6") or "6").strip())
    except (TypeError, ValueError):
        live_k = 6
    live_k = max(1, min(48, live_k))
    raw = (get_config_value("GAME_5M_RECOVERY_D4A_STATS_K_BARS", "") or "").strip()
    ks: Set[int] = {live_k}
    if raw:
        for part in raw.replace(";", ",").split(","):
            p = part.strip()
            if not p:
                continue
            try:
                ks.add(int(float(p)))
            except (TypeError, ValueError):
                continue
    else:
        ks.update({3, 6, 9, 12})
    return sorted({max(1, min(48, int(x))) for x in ks})


def _recovery_d4a_tau_grid() -> List[float]:
    return [round(x, 3) for x in (0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85)]


def _recovery_d4a_tau_sweep_for_k(merged: List[Dict[str, Any]], k: int, tau_grid: List[float]) -> List[Dict[str, Any]]:
    k_s = str(k)
    base = []
    for m in merged:
        p = m.get("recovery_proba")
        db = m.get("deltas_by_k_bars")
        if p is None or not isinstance(db, dict):
            continue
        if db.get(k_s) is None:
            continue
        base.append(m)
    out: List[Dict[str, Any]] = []
    for tau in tau_grid:
        sel = [m for m in base if float(m["recovery_proba"]) >= float(tau)]
        out.append(
            {
                "tau": tau,
                "defer_count": len(sel),
                "mean_delta_delayed_minus_actual_pct": _mean_finite(
                    [float((m.get("deltas_by_k_bars") or {}).get(k_s)) for m in sel]
                ),
                "mean_post_exit_mfe_pct_1h": _mean_finite([m.get("post_exit_mfe_pct_1h") for m in sel]),
            }
        )
    return out


def _recovery_d4a_best_tau_from_sweep(sweep: List[Dict[str, Any]], *, min_defer: int) -> Optional[Dict[str, Any]]:
    """τ с максимальным средним Δ при достаточном n defer; tie-break — больший τ."""
    cand: List[Dict[str, Any]] = []
    for x in sweep:
        try:
            n = int(x.get("defer_count") or 0)
        except (TypeError, ValueError):
            n = 0
        md = x.get("mean_delta_delayed_minus_actual_pct")
        if n < min_defer or md is None:
            continue
        cand.append(x)
    if not cand:
        for x in sweep:
            try:
                n = int(x.get("defer_count") or 0)
            except (TypeError, ValueError):
                n = 0
            md = x.get("mean_delta_delayed_minus_actual_pct")
            if n >= 1 and md is not None:
                cand.append(x)
    if not cand:
        return None

    def sort_key(x: Dict[str, Any]) -> Tuple[float, float]:
        md = x.get("mean_delta_delayed_minus_actual_pct")
        tau = float(x.get("tau") or 0.0)
        return (float(md) if md is not None else float("-inf"), tau)

    best = max(cand, key=sort_key)
    return {
        "tau": best.get("tau"),
        "defer_count": best.get("defer_count"),
        "mean_delta_delayed_minus_actual_pct": best.get("mean_delta_delayed_minus_actual_pct"),
        "mean_post_exit_mfe_pct_1h": best.get("mean_post_exit_mfe_pct_1h"),
        "min_defer_met": min_defer,
    }


def _empty_recovery_ml_d4a_live_review(note: str) -> Dict[str, Any]:
    return {
        "mode": "skipped",
        "note": note,
        "policy_live_d4a": "defer iff recovery_proba >= GAME_5M_RECOVERY_LIVE_TAU_HOLD (как в send_sndk D4a телеметрия).",
        "policy_offline_d3": "recovery_scenario_backtest: delay iff P < GAME_5M_RECOVERY_SCENARIO_TAU — другая полярность, см. metric_definitions.",
        "time_exit_early_game5m_in_window": 0,
        "trades_with_recovery_gate": 0,
        "trades_gate_ok_with_proba": 0,
        "trades_with_delta_k": 0,
        "live_tau_hold_config": None,
        "defer_delay_bars_config": None,
        "k_bars_evaluated": [],
        "n_delta_by_k": {},
        "tau_sweep": [],
        "tau_sweep_by_k": {},
        "best_tau_by_k": {},
        "min_defer_for_best_tau": None,
        "shallow_gate_by_window_days": None,
        "window_suggestion": None,
        "p_buckets": [],
        "recorded_would_defer_summary": None,
        "per_trade_recent": [],
    }


def _mean_finite(xs: List[Optional[float]]) -> Optional[float]:
    vs = [float(x) for x in xs if x is not None and math.isfinite(float(x))]
    return round(float(np.mean(vs)), 4) if vs else None


def _build_recovery_ml_d4a_live_review(
    closed: List[Any],
    te_review: Dict[str, Any],
    effects: List[TradeEffect],
    ohlc_cache: Dict[str, Optional[pd.DataFrame]],
    *,
    strategy: str,
) -> Dict[str, Any]:
    """
    Сводка D4a: SELL TIME_EXIT_EARLY с recovery_ml_time_exit_early + пост-выход + контрфакт K баров (LIVE_DEFER_BARS).
    """
    su = (strategy or "").strip().upper()
    if su not in ("GAME_5M", "ALL"):
        return _empty_recovery_ml_d4a_live_review("Только для стратегии GAME_5M или ALL.")

    try:
        delay_bars = int((get_config_value("GAME_5M_RECOVERY_LIVE_DEFER_BARS", "6") or "6").strip())
    except (TypeError, ValueError):
        delay_bars = 6
    delay_bars = max(1, min(48, delay_bars))
    try:
        live_tau = float((get_config_value("GAME_5M_RECOVERY_LIVE_TAU_HOLD", "0.65") or "0.65").strip().replace(",", "."))
    except (TypeError, ValueError):
        live_tau = 0.65
    live_tau = max(0.0, min(1.0, live_tau))

    k_candidates = _recovery_d4a_k_bars_candidates()
    try:
        min_defer_best = int((get_config_value("GAME_5M_RECOVERY_D4A_STATS_MIN_DEFER", "2") or "2").strip())
    except (TypeError, ValueError):
        min_defer_best = 2
    min_defer_best = max(1, min(25, min_defer_best))

    te_rows = te_review.get("detail_rows") if isinstance(te_review.get("detail_rows"), list) else []
    te_by_tid: Dict[int, Dict[str, Any]] = {}
    for r in te_rows:
        if not isinstance(r, dict):
            continue
        try:
            tid = int(r.get("trade_id") or -1)
        except (TypeError, ValueError):
            continue
        if tid > 0:
            te_by_tid[tid] = r

    eff_by_id: Dict[int, TradeEffect] = {e.trade_id: e for e in effects}

    te_game5m_n = 0
    merged: List[Dict[str, Any]] = []
    for t in closed:
        sig = str(getattr(t, "signal_type", "") or "").strip().upper()
        if sig != "TIME_EXIT_EARLY":
            continue
        exs = str(getattr(t, "exit_strategy", "") or "").strip().upper()
        if exs != "GAME_5M":
            continue
        te_game5m_n += 1
        tid = int(getattr(t, "trade_id", 0) or 0)
        ec = _json_dict(getattr(t, "exit_context_json", None))
        gate = ec.get("recovery_ml_time_exit_early")
        if not isinstance(gate, dict):
            continue
        st = str(gate.get("status") or "")
        proba = _safe_float(gate.get("recovery_proba"))
        tau_at_close = _safe_float(gate.get("tau_hold"))
        wd_exit = gate.get("would_defer_exit")
        wd_model = gate.get("would_defer_by_model")
        rec_log_only = gate.get("log_only")
        deny = gate.get("deny_reasons")
        e = eff_by_id.get(tid)
        te_r = te_by_tid.get(tid)
        row: Dict[str, Any] = {
            "trade_id": tid,
            "ticker": str(getattr(t, "ticker", "") or "").strip().upper(),
            "gate_status": st,
            "recovery_proba": round(proba, 4) if proba is not None else None,
            "tau_hold_at_close": round(tau_at_close, 4) if tau_at_close is not None else None,
            "would_defer_exit_recorded": bool(wd_exit) if wd_exit is not None else None,
            "would_defer_by_model_recorded": bool(wd_model) if wd_model is not None else None,
            "log_only_recorded": bool(rec_log_only) if rec_log_only is not None else None,
            "deny_reasons": deny if isinstance(deny, list) else None,
            "post_exit_mfe_pct_1h": te_r.get("post_exit_mfe_pct_1h") if isinstance(te_r, dict) else None,
            "likely_premature_whipsaw_1h": bool(te_r.get("likely_premature_whipsaw_1h")) if isinstance(te_r, dict) else None,
            "deltas_by_k_bars": {},
            "delta_delayed_minus_actual_pct": None,
            "actual_close_pct_vs_entry_approx": None,
            "delayed_close_pct_vs_entry_approx": None,
        }
        if e is not None and proba is not None:
            entry_px = float(e.entry_price)
            act_px = float(e.exit_price)
            if entry_px > 0 and act_px > 0:
                actual_pct = (act_px / entry_px - 1.0) * 100.0
                row["actual_close_pct_vs_entry_approx"] = round(actual_pct, 4)
                df = ohlc_cache.get(e.ticker)
                dk: Dict[str, Optional[float]] = {}
                for kb in k_candidates:
                    del_close = _recovery_delayed_close_after_exit(df, e.exit_ts, kb)
                    if del_close is not None and entry_px > 0:
                        delayed_pct = (del_close / entry_px - 1.0) * 100.0
                        dk[str(kb)] = round(delayed_pct - actual_pct, 4)
                    else:
                        dk[str(kb)] = None
                row["deltas_by_k_bars"] = dk
                pk = str(delay_bars)
                if pk in dk and dk[pk] is not None:
                    row["delta_delayed_minus_actual_pct"] = dk[pk]
                    del_c = _recovery_delayed_close_after_exit(df, e.exit_ts, delay_bars)
                    if del_c is not None:
                        row["delayed_close_pct_vs_entry_approx"] = round((del_c / entry_px - 1.0) * 100.0, 4)
        merged.append(row)

    if te_game5m_n == 0:
        return _empty_recovery_ml_d4a_live_review("Нет закрытых TIME_EXIT_EARLY с exit_strategy=GAME_5M в окне.")

    n_gate = len(merged)
    if n_gate == 0:
        return {
            "mode": "no_live_gate",
            "note": (
                f"В окне {te_game5m_n} сделок TIME_EXIT_EARLY (GAME_5M), но в SELL context_json нет ключа "
                "`recovery_ml_time_exit_early` — типично старые сделки до D4a или recovery ML выключен на кроне."
            ),
            "policy_live_d4a": "defer iff recovery_proba >= GAME_5M_RECOVERY_LIVE_TAU_HOLD (как в send_sndk D4a телеметрия).",
            "policy_offline_d3": "recovery_scenario_backtest: delay iff P < GAME_5M_RECOVERY_SCENARIO_TAU — другая полярность, см. metric_definitions.",
            "time_exit_early_game5m_in_window": te_game5m_n,
            "trades_with_recovery_gate": 0,
            "trades_gate_ok_with_proba": 0,
            "trades_with_delta_k": 0,
            "live_tau_hold_config": live_tau,
            "defer_delay_bars_config": delay_bars,
            "k_bars_evaluated": k_candidates,
            "n_delta_by_k": {},
            "tau_sweep": [],
            "tau_sweep_by_k": {},
            "best_tau_by_k": {},
            "min_defer_for_best_tau": min_defer_best,
            "p_buckets": [],
            "recorded_would_defer_summary": None,
            "per_trade_recent": [],
        }

    with_p = [m for m in merged if m.get("recovery_proba") is not None]
    n_p = len(with_p)
    pk = str(delay_bars)
    with_delta = [m for m in merged if m.get("delta_delayed_minus_actual_pct") is not None]
    n_delta = len(with_delta)

    tau_grid = _recovery_d4a_tau_grid()
    tau_sweep = _recovery_d4a_tau_sweep_for_k(merged, delay_bars, tau_grid)

    tau_sweep_by_k: Dict[str, List[Dict[str, Any]]] = {}
    n_delta_by_k: Dict[str, int] = {}
    best_tau_by_k: Dict[str, Any] = {}
    for kb in k_candidates:
        sw = _recovery_d4a_tau_sweep_for_k(merged, kb, tau_grid)
        tau_sweep_by_k[str(kb)] = sw
        n_delta_by_k[str(kb)] = sum(
            1
            for m in merged
            if m.get("recovery_proba") is not None
            and isinstance(m.get("deltas_by_k_bars"), dict)
            and (m.get("deltas_by_k_bars") or {}).get(str(kb)) is not None
        )
        bt = _recovery_d4a_best_tau_from_sweep(sw, min_defer=min_defer_best)
        if bt is not None:
            best_tau_by_k[str(kb)] = bt

    p_buckets_spec = [
        ("0.00–0.50", 0.0, 0.5),
        ("0.50–0.60", 0.5, 0.6),
        ("0.60–0.70", 0.6, 0.7),
        ("0.70–0.80", 0.7, 0.8),
        ("0.80–1.00", 0.8, 1.0001),
    ]
    p_buckets: List[Dict[str, Any]] = []
    for label, lo, hi in p_buckets_spec:
        ms = [m for m in with_p if lo <= float(m["recovery_proba"]) < hi]
        delta_primary = []
        for m in ms:
            db = m.get("deltas_by_k_bars")
            if isinstance(db, dict) and db.get(pk) is not None:
                delta_primary.append(float(db[pk]))
        p_buckets.append(
            {
                "p_range": label,
                "count": len(ms),
                "mean_post_exit_mfe_pct_1h": _mean_finite([m.get("post_exit_mfe_pct_1h") for m in ms]),
                "premature_rate": (
                    round(
                        sum(1 for m in ms if m.get("likely_premature_whipsaw_1h")) / float(len(ms)),
                        4,
                    )
                    if ms
                    else None
                ),
                "mean_delta_primary_k_pct": _mean_finite(delta_primary),
            }
        )

    rec_wd = [
        m
        for m in merged
        if m.get("would_defer_exit_recorded") is True
        and isinstance(m.get("deltas_by_k_bars"), dict)
        and (m.get("deltas_by_k_bars") or {}).get(pk) is not None
    ]
    recorded_summary = None
    if rec_wd:
        recorded_summary = {
            "count": len(rec_wd),
            "mean_delta_delayed_minus_actual_pct": _mean_finite(
                [float((m.get("deltas_by_k_bars") or {}).get(pk)) for m in rec_wd]
            ),
            "mean_post_exit_mfe_pct_1h": _mean_finite([m.get("post_exit_mfe_pct_1h") for m in rec_wd]),
        }

    merged_sorted = sorted(merged, key=lambda m: int(m.get("trade_id") or 0), reverse=True)
    per_recent = merged_sorted[:36]
    for pr in per_recent:
        if "deltas_by_k_bars" in pr and isinstance(pr["deltas_by_k_bars"], dict):
            pr["deltas_by_k_bars"] = dict(pr["deltas_by_k_bars"])

    return {
        "mode": "ok",
        "note": (
            "Контрфакт K баров — упрощение (Close K×5m после exit_ts), без комиссий. "
            "`tau_sweep` — для LIVE_DEFER_BARS; `tau_sweep_by_k` — для всех K из k_bars_evaluated. "
            "`best_tau_by_k` — τ с max mean Δ при defer_count ≥ min_defer_for_best_tau (смягчение до n≥1 если мало данных)."
        ),
        "policy_live_d4a": "defer iff recovery_proba >= tau (как в cron; фактическое удержание — только после D4b).",
        "policy_offline_d3": "recovery_scenario_backtest использует P < SCENARIO_TAU — не смешивать с live без проверки.",
        "time_exit_early_game5m_in_window": te_game5m_n,
        "trades_with_recovery_gate": n_gate,
        "trades_gate_ok_with_proba": n_p,
        "trades_with_delta_k": n_delta,
        "live_tau_hold_config": live_tau,
        "defer_delay_bars_config": delay_bars,
        "k_bars_evaluated": k_candidates,
        "n_delta_by_k": n_delta_by_k,
        "min_defer_for_best_tau": min_defer_best,
        "tau_sweep": tau_sweep,
        "tau_sweep_by_k": tau_sweep_by_k,
        "best_tau_by_k": best_tau_by_k,
        "p_buckets": p_buckets,
        "recorded_would_defer_summary": recorded_summary,
        "per_trade_recent": per_recent,
    }


def compute_recovery_ml_d4a_live_review_for_window(
    days: int = 21,
    strategy: str = "GAME_5M",
    *,
    shallow_lookback_days: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Лёгкий вход для крона D4a: то же, что внутри analyze_trade_effectiveness для recovery_ml_d4a_live_review,
    без LLM и без полного payload анализатора.

    Если задан ``shallow_lookback_days`` (например 30), дополнительно считаются только **числа** TE/gate
    по календарным окнам 7/14/21/… дней без второго прохода OHLC — чтобы пересматривать достаточность выборки.
    """
    days = max(1, min(30, int(days)))
    closed = _load_closed_trades(days=days, strategy_name=strategy)
    if not closed:
        te0 = _build_time_exit_early_review([], {})
        out = _build_recovery_ml_d4a_live_review([], te0, [], {}, strategy=strategy)
        _maybe_fill_recovery_d4a_shallow(out, strategy=strategy, analysis_days=days, shallow_lookback_days=shallow_lookback_days)
        return out
    tickers = [str(t.ticker) for t in closed if getattr(t, "ticker", None)]
    cache = _prepare_ohlc_cache(tickers=tickers, days=days)
    effects = _estimate_trade_effects(closed, cache)
    te_review = _build_time_exit_early_review(effects, cache)
    out = _build_recovery_ml_d4a_live_review(closed, te_review, effects, cache, strategy=strategy)
    _maybe_fill_recovery_d4a_shallow(out, strategy=strategy, analysis_days=days, shallow_lookback_days=shallow_lookback_days)
    return out


def _maybe_fill_recovery_d4a_shallow(
    review: Dict[str, Any],
    *,
    strategy: str,
    analysis_days: int,
    shallow_lookback_days: Optional[int],
) -> None:
    if shallow_lookback_days is None:
        return
    try:
        lb = max(1, min(30, int(shallow_lookback_days)))
    except (TypeError, ValueError):
        lb = 30
    lb = max(lb, int(analysis_days))
    if (strategy or "").strip().upper() not in ("GAME_5M", "ALL"):
        return
    strat = "GAME_5M" if (strategy or "").strip().upper() == "ALL" else (strategy or "").strip().upper()
    closed_sh = _load_closed_trades(days=lb, strategy_name=strat)
    windows = sorted({7, 14, 21, int(analysis_days), lb})
    shallow = recovery_d4a_shallow_gate_counts_by_windows(closed_sh, windows=tuple(windows))
    review["shallow_gate_by_window_days"] = shallow
    try:
        suf = int((get_config_value("GAME_5M_RECOVERY_D4A_STATS_SUFFICIENT_GATE_MIN", "5") or "5").strip())
    except (TypeError, ValueError):
        suf = 5
    suf = max(2, min(50, suf))
    try:
        comf = int((get_config_value("GAME_5M_RECOVERY_D4A_STATS_COMFORTABLE_GATE_MIN", "10") or "10").strip())
    except (TypeError, ValueError):
        comf = 10
    comf = max(suf, min(100, comf))
    review["window_suggestion"] = recovery_d4a_window_suggestion(
        shallow,
        sufficient_gate_min=suf,
        comfortable_gate_min=comf,
        current_analysis_window_days=int(analysis_days),
    )


def _maybe_attach_recovery_d4a_shallow_for_analyzer(review: Dict[str, Any], *, days: int, strategy: str) -> None:
    """Опционально для GET /api/analyzer: второй лёгкий запрос к БД, без второго OHLC."""
    try:
        raw = (get_config_value("GAME_5M_RECOVERY_D4A_STATS_ATTACH_TO_ANALYZER", "false") or "false").strip().lower()
        on = raw in ("1", "true", "yes")
    except Exception:
        on = False
    if not on:
        return
    try:
        lb = int((get_config_value("GAME_5M_RECOVERY_D4A_STATS_SHALLOW_LOOKBACK_DAYS", "30") or "30").strip())
    except (TypeError, ValueError):
        lb = 30
    lb = max(days, min(30, lb))
    _maybe_fill_recovery_d4a_shallow(review, strategy=strategy, analysis_days=days, shallow_lookback_days=lb)


def recovery_d4a_rollup_snapshot_for_jsonl(review: Dict[str, Any], *, window_days: int) -> Dict[str, Any]:
    """Компактная строка для append-only JSONL (накопление τ×K по дням)."""
    if not isinstance(review, dict):
        return {"schema_version": "recovery_d4a_rollup_v1", "ts_utc": datetime.now(timezone.utc).isoformat(), "error": "bad_review"}
    return {
        "schema_version": "recovery_d4a_rollup_v1",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "window_days": int(window_days),
        "mode": review.get("mode"),
        "note": review.get("note"),
        "time_exit_early_game5m_in_window": review.get("time_exit_early_game5m_in_window"),
        "trades_with_recovery_gate": review.get("trades_with_recovery_gate"),
        "trades_gate_ok_with_proba": review.get("trades_gate_ok_with_proba"),
        "k_bars_evaluated": review.get("k_bars_evaluated"),
        "n_delta_by_k": review.get("n_delta_by_k"),
        "live_tau_hold_config": review.get("live_tau_hold_config"),
        "defer_delay_bars_config": review.get("defer_delay_bars_config"),
        "min_defer_for_best_tau": review.get("min_defer_for_best_tau"),
        "tau_sweep_by_k": review.get("tau_sweep_by_k"),
        "best_tau_by_k": review.get("best_tau_by_k"),
        "tau_sweep_primary_k": review.get("tau_sweep"),
        "p_buckets": review.get("p_buckets"),
        "recorded_would_defer_summary": review.get("recorded_would_defer_summary"),
        "shallow_gate_by_window_days": review.get("shallow_gate_by_window_days"),
        "window_suggestion": review.get("window_suggestion"),
    }


def recovery_d4a_shallow_gate_counts_by_windows(
    closed: List[Any],
    *,
    windows: Tuple[int, ...] = (7, 14, 21, 30),
) -> Dict[str, Any]:
    """
    Без OHLC: сколько TIME_EXIT_EARLY (GAME_5M) и сколько с recovery gate / с P в exit_context за последние W календарных дней.
    `closed` должен покрывать max(windows) дней (например загрузка на 30 дней).
    """
    now = pd.Timestamp.now(tz=timezone.utc)
    ws = sorted({max(1, min(120, int(w))) for w in windows if int(w) > 0})
    by_w: Dict[str, Dict[str, int]] = {}
    for w in ws:
        cutoff = now - pd.Timedelta(days=w)
        te_n = gate_n = proba_n = 0
        for t in closed:
            ts_raw = pd.Timestamp(getattr(t, "ts", None))
            if ts_raw.tzinfo is None:
                ts = ts_raw.tz_localize("UTC")
            else:
                ts = ts_raw.tz_convert("UTC")
            if ts < cutoff:
                continue
            sig = str(getattr(t, "signal_type", "") or "").strip().upper()
            if sig != "TIME_EXIT_EARLY":
                continue
            exs = str(getattr(t, "exit_strategy", "") or "").strip().upper()
            if exs != "GAME_5M":
                continue
            te_n += 1
            ec = _json_dict(getattr(t, "exit_context_json", None))
            gate = ec.get("recovery_ml_time_exit_early")
            if not isinstance(gate, dict):
                continue
            gate_n += 1
            if _safe_float(gate.get("recovery_proba")) is not None:
                proba_n += 1
        by_w[str(w)] = {
            "time_exit_early_game5m": te_n,
            "with_recovery_gate": gate_n,
            "with_recovery_proba": proba_n,
        }
    return {"by_window_days": by_w, "windows_requested": [str(x) for x in ws]}


def recovery_d4a_window_suggestion(
    shallow: Dict[str, Any],
    *,
    sufficient_gate_min: int,
    comfortable_gate_min: int,
    current_analysis_window_days: int,
) -> Dict[str, Any]:
    """
    Подсказка: достаточно ли короткого календарного окна для обзора τ×K (по числу SELL с gate, без OHLC).
    """
    by_w = shallow.get("by_window_days") if isinstance(shallow.get("by_window_days"), dict) else {}
    ordered = sorted(((int(k), v) for k, v in by_w.items() if str(k).isdigit()), key=lambda kv: kv[0])
    smallest_sufficient: Optional[int] = None
    smallest_comfortable: Optional[int] = None
    for w, d in ordered:
        g = int((d or {}).get("with_recovery_gate") or 0)
        if smallest_sufficient is None and g >= sufficient_gate_min:
            smallest_sufficient = w
        if smallest_comfortable is None and g >= comfortable_gate_min:
            smallest_comfortable = w
    hints: List[str] = []
    if smallest_sufficient is not None and smallest_sufficient < current_analysis_window_days:
        hints.append(
            f"За {smallest_sufficient}д уже ≥{sufficient_gate_min} SELL с recovery gate — для грубого обзора τ×K можно временно снизить "
            f"GAME_5M_RECOVERY_D4A_STATS_WINDOW_DAYS (сейчас основной расчёт на {current_analysis_window_days}д)."
        )
    if smallest_comfortable is not None:
        hints.append(
            f"Окно ≥{smallest_comfortable}д даёт ≥{comfortable_gate_min} gate — комфортнее для устойчивого best τ по K."
        )
    if not hints:
        hints.append(
            f"Мало событий с gate (пороги {sufficient_gate_min}/{comfortable_gate_min}); оставьте окно "
            f"{current_analysis_window_days}д или дождитесь новых TIME_EXIT_EARLY после D4a."
        )
    return {
        "sufficient_gate_min": sufficient_gate_min,
        "comfortable_gate_min": comfortable_gate_min,
        "smallest_window_days_gate_ge_sufficient": smallest_sufficient,
        "smallest_window_days_gate_ge_comfortable": smallest_comfortable,
        "current_analysis_window_days": current_analysis_window_days,
        "hints_ru": hints,
    }


def _build_recovery_scenario_backtest(
    effects: List[TradeEffect],
    ohlc_cache: Dict[str, Optional[pd.DataFrame]],
    *,
    strategy: str,
) -> Dict[str, Any]:
    """
    Фаза D3: для TIME_EXIT_EARLY (GAME_5M) — скор recovery на последнем баре удержания;
    при P < τ контрфакт «выход на K баров позже» по Close (без комиссий).
    """
    from services.game5m_recovery_catboost import (
        default_recovery_catboost_model_path,
        load_recovery_model_meta,
        predict_recovery_hold_proba,
        row_vector_from_hold_bar,
    )

    su = (strategy or "").strip().upper()
    if su == "PORTFOLIO":
        return {
            "mode": "skipped",
            "note": "Recovery ML — контур GAME_5M; для портфеля не применяется.",
        }
    if su not in ("GAME_5M", "ALL"):
        return {
            "mode": "skipped",
            "note": f"Стратегия {strategy!r}: сценарий только при GAME_5M или ALL.",
        }

    model_path = (get_config_value("GAME_5M_RECOVERY_CATBOOST_MODEL_PATH", "") or "").strip()
    if not model_path:
        model_path = str(default_recovery_catboost_model_path())
    meta = load_recovery_model_meta(model_path)
    mp = Path(model_path)

    try:
        tau = float((get_config_value("GAME_5M_RECOVERY_SCENARIO_TAU", "0.45") or "0.45").replace(",", "."))
    except (ValueError, TypeError):
        tau = 0.45
    try:
        delay_bars = int((get_config_value("GAME_5M_RECOVERY_SCENARIO_DELAY_BARS", "6") or "6").strip())
    except (ValueError, TypeError):
        delay_bars = 6
    delay_bars = max(1, min(delay_bars, 48))

    out: Dict[str, Any] = {
        "mode": "time_exit_early_delay",
        "tau": tau,
        "delay_bars": delay_bars,
        "model_path": model_path,
        "model_file_exists": mp.is_file(),
        "meta_summary": {
            "trained_at": (meta or {}).get("trained_at"),
            "label_column": (meta or {}).get("label_column"),
            "horizon_minutes": (meta or {}).get("horizon_minutes"),
            "auc_valid": (meta or {}).get("auc_valid"),
            "n_valid": (meta or {}).get("n_valid"),
        }
        if isinstance(meta, dict)
        else None,
        "description": (
            "Последний 5m бар с entry_ts ≤ t < exit_ts → признаки как в экспорте recovery; P = P(y_recovery=1). "
            "Если P < τ — считаем, что правило могло задержать выход на K баров после фактического exit_ts (Close, без комиссий)."
        ),
    }
    out.update(_trust_level_game5m_recovery(meta if isinstance(meta, dict) else None))

    if not mp.is_file():
        out["note"] = "Нет .cbm — обучите: python scripts/train_game5m_recovery_catboost.py --jsonl …"
        out["time_exit_early_trades"] = sum(
            1
            for e in effects
            if str(e.exit_signal or "").upper() == "TIME_EXIT_EARLY"
            and str(e.exit_strategy or "").strip().upper() == "GAME_5M"
        )
        out["scored"] = 0
        return out

    te_eff = [
        e
        for e in effects
        if str(e.exit_signal or "").upper() == "TIME_EXIT_EARLY"
        and str(e.exit_strategy or "").strip().upper() == "GAME_5M"
    ]
    per_trade: List[Dict[str, Any]] = []
    would_delay_policy_count = 0  # P < tau
    would_delay_with_delta_count = 0  # P < tau and delayed_pct exists
    improved = 0
    worse = 0
    equal = 0
    deltas: List[float] = []
    scored = 0
    delta_missing_reasons: Dict[str, int] = {}

    for e in te_eff:
        df = ohlc_cache.get(e.ticker)
        if df is None or getattr(df, "empty", True):
            continue
        entry_ts = _as_et(pd.Timestamp(e.entry_ts))
        exit_ts = _as_et(pd.Timestamp(e.exit_ts))
        if exit_ts <= entry_ts or float(e.entry_price) <= 0:
            continue
        try:
            m = (df["datetime"] >= entry_ts) & (df["datetime"] < exit_ts)
            sub = df.loc[m].reset_index(drop=True)
        except Exception:
            continue
        if len(sub) < 1:
            continue
        try:
            bar_time = pd.Timestamp(sub["datetime"].iloc[-1])
            if bar_time.tzinfo is None:
                bar_time = bar_time.tz_localize("America/New_York", ambiguous="infer")
            else:
                bar_time = bar_time.tz_convert("America/New_York")
            ref_close = float(sub["Close"].iloc[-1])
        except Exception:
            continue
        row = row_vector_from_hold_bar(
            ticker=e.ticker,
            entry_price=float(e.entry_price),
            entry_ts_et=entry_ts,
            bar_time_et=bar_time,
            ref_close=ref_close,
            entry_rsi_5m=e.entry_rsi_5m,
            entry_vol_5m_pct=e.entry_vol_5m_pct,
            entry_momentum_2h_pct=e.entry_momentum_2h_pct,
            entry_decision=e.entry_decision,
        )
        if row is None:
            continue
        pred = predict_recovery_hold_proba(str(mp), row, meta=meta if isinstance(meta, dict) else None)
        proba = pred.get("recovery_proba") if pred.get("status") == "ok" else None
        if proba is None:
            per_trade.append(
                {
                    "trade_id": e.trade_id,
                    "ticker": e.ticker,
                    "predict_status": pred.get("status"),
                    "predict_reason": pred.get("reason"),
                }
            )
            continue
        scored += 1
        entry_px = float(e.entry_price)
        act_px = float(e.exit_price)
        actual_pct = (act_px / entry_px - 1.0) * 100.0 if entry_px > 0 else 0.0
        del_close = _recovery_delayed_close_after_exit(df, exit_ts, delay_bars)
        delayed_pct: Optional[float] = None
        if del_close is not None and entry_px > 0:
            delayed_pct = (del_close / entry_px - 1.0) * 100.0
        would_delay = float(proba) < tau
        delta: Optional[float] = None
        if would_delay:
            would_delay_policy_count += 1
            if delayed_pct is None:
                delta_missing_reasons["no_delayed_close_after_exit_k_bars"] = (
                    delta_missing_reasons.get("no_delayed_close_after_exit_k_bars", 0) + 1
                )
            else:
                would_delay_with_delta_count += 1
                delta = delayed_pct - actual_pct
                deltas.append(delta)
                if delta > 1e-6:
                    improved += 1
                elif delta < -1e-6:
                    worse += 1
                else:
                    equal += 1

        per_trade.append(
            {
                "trade_id": e.trade_id,
                "ticker": e.ticker,
                "recovery_proba": round(float(proba), 4),
                "would_delay_exit": bool(would_delay),
                "exit_detail": e.exit_detail,
                "actual_close_pct_approx": round(actual_pct, 4),
                "delayed_close_pct_approx": round(delayed_pct, 4) if delayed_pct is not None else None,
                "delta_delayed_minus_actual_pct": round(delta, 4) if delta is not None else None,
            }
        )

    out["time_exit_early_trades"] = len(te_eff)
    out["scored"] = scored
    # Важно: would_delay_exit может быть true даже если delta недоступна (нет Close через K баров после exit).
    out["would_delay_policy_count"] = would_delay_policy_count
    out["would_delay_with_delta_count"] = would_delay_with_delta_count
    out["delta_missing_reasons"] = delta_missing_reasons
    # Backward compatible field name: count where we could compute delta.
    out["would_delay_count"] = would_delay_with_delta_count
    if would_delay_with_delta_count > 0:
        out["among_would_delay"] = {
            "improved": improved,
            "worse": worse,
            "about_equal": equal,
            "mean_delta_pct": round(float(np.mean(deltas)), 4) if deltas else None,
        }
    else:
        out["among_would_delay"] = {
            "improved": 0,
            "worse": 0,
            "about_equal": 0,
            "mean_delta_pct": None,
            "note": "Нет сделок с P < τ и доступной ценой через K баров после exit (delta недоступна).",
        }
    _max_rows = 120
    out["per_trade"] = per_trade[:_max_rows]
    if len(per_trade) > _max_rows:
        out["per_trade_omitted"] = len(per_trade) - _max_rows
    return out


def _attach_game5m_param_hypothesis_backtest_optional(
    payload: Dict[str, Any],
    *,
    strategy: str,
    effects: Optional[List[TradeEffect]],
    include_game5m_param_hypothesis_backtest: bool,
) -> None:
    """Офлайн-реплей: висяки (старое окно BUY) и недобор (missed upside) → mergeable_recommendations."""
    if not include_game5m_param_hypothesis_backtest or strategy.upper() != "GAME_5M":
        return
    try:
        from services.game5m_param_hypothesis_backtest import run_game5m_hypothesis_bundle

        payload["game5m_param_hypothesis_backtest"] = run_game5m_hypothesis_bundle(
            engine=get_engine(),
            effects=effects,
        )
    except Exception as exc:
        payload["game5m_param_hypothesis_backtest"] = {"status": "error", "reason": str(exc)}


def _analyzer_engine_safe():
    try:
        return get_engine()
    except Exception:
        return None


def _attach_multiday_lr_and_ml_arbiter(
    payload: Dict[str, Any],
    *,
    strategy: str,
    closed_trades: List[Any],
    effects: List[Any],
) -> None:
    eng = _analyzer_engine_safe()
    payload["multiday_lr_reality_check"] = build_multiday_lr_reality_check(
        eng, strategy, closed_trades=closed_trades, effects=effects
    )
    payload["ml_production_arbiter"] = build_ml_production_arbiter(payload)
    payload["product_ideas_arbiter"] = build_product_ideas_arbiter(
        payload, effects=effects, closed_trades=closed_trades
    )


def analyze_trade_effectiveness(
    days: int = 7,
    strategy: str = "GAME_5M",
    use_llm: bool = False,
    *,
    include_trade_details: bool = False,
    include_game5m_param_hypothesis_backtest: bool = False,
    export_recovery_ml: bool = False,
    recovery_ml_export_path: Optional[str] = None,
) -> Dict[str, Any]:
    days = max(1, min(30, int(days)))
    closed = _load_closed_trades(days=days, strategy_name=strategy)
    if not closed:
        empty_payload: Dict[str, Any] = {
            "meta": {
                "days": days,
                "strategy": strategy,
                "trades_analyzed": 0,
                "export_recovery_ml": bool(export_recovery_ml),
                "metric_definitions": ANALYZER_METRIC_DEFINITIONS,
            },
            "summary": {"total": 0},
            "catboost_entry_backtest": _build_catboost_entry_backtest(strategy, [], []),
            "game5m_catboost_fusion_entry_review": _build_game5m_catboost_fusion_entry_review(strategy, [], []),
            "portfolio_catboost_status": _build_portfolio_catboost_status(),
        }
        if (strategy or "").strip().upper() in ("GAME_5M", "ALL"):
            htr = _build_game5m_hanger_tune_json_review([], {"total": 0}, strategy=strategy)
            empty_payload["game5m_hanger_tune_json_review"] = htr
            empty_payload["game_5m_config_hints"] = _build_game5m_config_hints(
                [], {"total": 0}, hanger_tune_review=htr
            )
        te0 = _build_time_exit_early_review([], {})
        empty_payload["time_exit_early_review"] = te0
        empty_payload["time_exit_early_action_summary"] = _build_time_exit_early_action_summary(te0)
        _attach_game5m_param_hypothesis_backtest_optional(
            empty_payload,
            strategy=strategy,
            effects=[],
            include_game5m_param_hypothesis_backtest=include_game5m_param_hypothesis_backtest,
        )
        if (strategy or "").strip().upper() in ("GAME_5M", "ALL"):
            empty_payload["game5m_hold_recovery_dataset_stats"] = _empty_game5m_hold_recovery_stats(
                "no closed trades in window"
            )
        else:
            empty_payload["game5m_hold_recovery_dataset_stats"] = _empty_game5m_hold_recovery_stats(
                "recovery dataset only when strategy is GAME_5M or ALL"
            )
        if export_recovery_ml:
            sr = (empty_payload.get("game5m_hold_recovery_dataset_stats") or {}).get("skip_reason", "skipped")
            empty_payload["game5m_hold_recovery_export"] = {"status": "skipped", "reason": sr}
        else:
            empty_payload["game5m_hold_recovery_export"] = None
        empty_payload["game5m_recovery_model_status"] = _build_game5m_recovery_model_status()
        empty_payload["recovery_scenario_backtest"] = _build_recovery_scenario_backtest([], {}, strategy=strategy)
        empty_payload["recovery_ml_d4a_live_review"] = _build_recovery_ml_d4a_live_review(
            [], te0, [], {}, strategy=strategy
        )
        _attach_multiday_lr_and_ml_arbiter(empty_payload, strategy=strategy, closed_trades=[], effects=[])
        return empty_payload

    tickers = [str(t.ticker) for t in closed if getattr(t, "ticker", None)]
    cache = _prepare_ohlc_cache(tickers=tickers, days=days)
    effects = _estimate_trade_effects(closed, cache)
    summary = _aggregate(effects)
    tops = _top_cases(effects)
    current_rules = _get_current_decision_rule_params()
    prev_state = _load_analyzer_state()
    cur_snap = _extract_game5m_config_snapshot(current_rules)
    prev_snap = None
    if isinstance(prev_state.get("last_run"), dict):
        ps = prev_state["last_run"].get("game_5m_config_snapshot")
        prev_snap = ps if isinstance(ps, dict) else None
    hanger_tune_review = _build_game5m_hanger_tune_json_review(effects, summary, strategy=strategy)
    te_review = _build_time_exit_early_review(effects, cache)
    practical = _build_practical_parameter_suggestions(effects, summary, current_rules)
    add_pr = hanger_tune_review.get("practical_parameter_additions") if isinstance(hanger_tune_review, dict) else None
    if isinstance(add_pr, list):
        practical.extend([x for x in add_pr if isinstance(x, dict)])
    practical.extend(_practical_suggestions_from_time_exit_early_review(te_review))
    critical_cases = _build_critical_case_analysis(effects, limit=5)
    game_5m_config_hints = _build_game5m_config_hints(effects, summary, hanger_tune_review=hanger_tune_review)
    game_5m_config_hints.extend(_config_hints_from_time_exit_early_review(te_review))
    entry_review = _build_entry_underperformance_review(effects, limit=8)
    catboost_entry_backtest = _build_catboost_entry_backtest(strategy, closed, effects)
    catboost_fusion_entry_review = _build_game5m_catboost_fusion_entry_review(strategy, closed, effects)
    hanger_v2_review = _build_hanger_v2_review(effects)
    continuation_gate_review = _build_continuation_gate_review(effects)
    recovery_d4a_live = _build_recovery_ml_d4a_live_review(closed, te_review, effects, cache, strategy=strategy)
    _maybe_attach_recovery_d4a_shallow_for_analyzer(recovery_d4a_live, days=days, strategy=strategy)
    payload: Dict[str, Any] = {
        "meta": {
            "days": days,
            "strategy": strategy,
            "trades_analyzed": len(effects),
            "include_trade_details": bool(include_trade_details),
            "export_recovery_ml": bool(export_recovery_ml),
            "analyzer_source": "services.trade_effectiveness_analyzer.analyze_trade_effectiveness",
            "decision_source_expected": "services.recommend_5m.get_decision_5m",
            "current_decision_rule_params": current_rules,
            "previous_run_at_utc": prev_state.get("last_run", {}).get("at_utc")
            if isinstance(prev_state.get("last_run"), dict)
            else None,
            "current_game_5m_config_snapshot": cur_snap,
            "previous_game_5m_config_snapshot": prev_snap,
            "config_delta_from_previous": _diff_flat_config(prev_snap, cur_snap) if prev_snap else [],
            "long_hold_ge_7d_minutes": LONG_HOLD_MINUTES,
            "metric_definitions": ANALYZER_METRIC_DEFINITIONS,
        },
        "summary": summary,
        "top_cases": tops,
        "practical_parameter_suggestions": practical,
        "critical_case_analysis": critical_cases,
        "game_5m_config_hints": game_5m_config_hints,
        "entry_underperformance_review": entry_review,
        "catboost_entry_backtest": catboost_entry_backtest,
        "game5m_catboost_fusion_entry_review": catboost_fusion_entry_review,
        "game5m_catboost_status": _build_game5m_catboost_status(),
        "game5m_recovery_model_status": _build_game5m_recovery_model_status(),
        "recovery_scenario_backtest": _build_recovery_scenario_backtest(effects, cache, strategy=strategy),
        "portfolio_catboost_status": _build_portfolio_catboost_status(),
        "time_exit_early_review": te_review,
        "time_exit_early_action_summary": _build_time_exit_early_action_summary(te_review),
        "recovery_ml_d4a_live_review": recovery_d4a_live,
        "game5m_hanger_v2_review": hanger_v2_review,
        "continuation_gate_review": continuation_gate_review,
        "game5m_hanger_tune_json_review": hanger_tune_review,
    }
    if include_trade_details:
        payload["trade_effects"] = [_trade_effect_detail_dict(e) for e in effects]
    if use_llm:
        payload["llm"] = _build_llm_recommendations(payload)
    payload["auto_config_override"] = _build_auto_config_override(payload)
    _attach_game5m_param_hypothesis_backtest_optional(
        payload,
        strategy=strategy,
        effects=effects,
        include_game5m_param_hypothesis_backtest=include_game5m_param_hypothesis_backtest,
    )
    _attach_game5m_hold_recovery_to_payload(
        payload,
        effects,
        cache,
        strategy=strategy,
        export_recovery_ml=export_recovery_ml,
        recovery_ml_export_path=recovery_ml_export_path,
    )
    _attach_multiday_lr_and_ml_arbiter(payload, strategy=strategy, closed_trades=closed, effects=effects)
    _save_analyzer_state(
        {
            "last_run": {
                "at_utc": datetime.now(timezone.utc).isoformat(),
                "days": days,
                "strategy": strategy,
                "game_5m_config_snapshot": cur_snap,
            }
        }
    )
    return payload


def analyze_trade_effectiveness_focused(
    days: int = 4,
    strategy: str = "GAME_5M",
    *,
    tickers: Optional[List[str]] = None,
    trade_ids: Optional[List[int]] = None,
    use_llm: bool = False,
    include_trade_details: bool = False,
    include_game5m_param_hypothesis_backtest: bool = False,
    export_recovery_ml: bool = False,
    recovery_ml_export_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Узкий анализ: последние ``days`` дней, опционально только выбранные тикеры и/или trade_id выхода.
    Добавляет ``game_5m_config_hints`` и при ``use_llm`` — LLM с фокусом на ``config_env_proposals`` (GAME_5M_*).
    """
    days = max(1, min(30, int(days)))
    closed = _load_closed_trades(days=days, strategy_name=strategy)
    filtered = _filter_closed_trades_for_focus(closed, tickers=tickers, trade_ids=trade_ids)
    if not filtered:
        empty_focus: Dict[str, Any] = {
            "meta": {
                "days": days,
                "strategy": strategy,
                "focused": True,
                "trades_analyzed": 0,
                "filter": {
                    "tickers": [str(t).strip().upper() for t in (tickers or []) if str(t).strip()],
                    "trade_ids": [int(x) for x in (trade_ids or [])],
                },
                "analyzer_source": "services.trade_effectiveness_analyzer.analyze_trade_effectiveness_focused",
                "decision_source_expected": "services.recommend_5m.get_decision_5m",
                "include_trade_details": bool(include_trade_details),
                "export_recovery_ml": bool(export_recovery_ml),
                "metric_definitions": ANALYZER_METRIC_DEFINITIONS,
            },
            "summary": {"total": 0},
            "catboost_entry_backtest": _build_catboost_entry_backtest(strategy, [], []),
            "game5m_catboost_fusion_entry_review": _build_game5m_catboost_fusion_entry_review(strategy, [], []),
            "portfolio_catboost_status": _build_portfolio_catboost_status(),
        }
        if (strategy or "").strip().upper() in ("GAME_5M", "ALL"):
            htr_f = _build_game5m_hanger_tune_json_review([], {"total": 0}, strategy=strategy)
            empty_focus["game5m_hanger_tune_json_review"] = htr_f
            empty_focus["game_5m_config_hints"] = _build_game5m_config_hints(
                [], {"total": 0}, hanger_tune_review=htr_f
            )
        te_f0 = _build_time_exit_early_review([], {}, focused_trade_ids=trade_ids if trade_ids else None)
        empty_focus["time_exit_early_review"] = te_f0
        empty_focus["time_exit_early_action_summary"] = _build_time_exit_early_action_summary(te_f0)
        _attach_game5m_param_hypothesis_backtest_optional(
            empty_focus,
            strategy=strategy,
            effects=[],
            include_game5m_param_hypothesis_backtest=include_game5m_param_hypothesis_backtest,
        )
        if (strategy or "").strip().upper() in ("GAME_5M", "ALL"):
            empty_focus["game5m_hold_recovery_dataset_stats"] = _empty_game5m_hold_recovery_stats(
                "no closed trades matching focus filter"
            )
        else:
            empty_focus["game5m_hold_recovery_dataset_stats"] = _empty_game5m_hold_recovery_stats(
                "recovery dataset only when strategy is GAME_5M or ALL"
            )
        if export_recovery_ml:
            sr = (empty_focus.get("game5m_hold_recovery_dataset_stats") or {}).get("skip_reason", "skipped")
            empty_focus["game5m_hold_recovery_export"] = {"status": "skipped", "reason": sr}
        else:
            empty_focus["game5m_hold_recovery_export"] = None
        empty_focus["game5m_recovery_model_status"] = _build_game5m_recovery_model_status()
        empty_focus["recovery_scenario_backtest"] = _build_recovery_scenario_backtest([], {}, strategy=strategy)
        empty_focus["recovery_ml_d4a_live_review"] = _build_recovery_ml_d4a_live_review(
            [], te_f0, [], {}, strategy=strategy
        )
        _attach_multiday_lr_and_ml_arbiter(empty_focus, strategy=strategy, closed_trades=[], effects=[])
        return empty_focus

    tickers_list = [str(t.ticker) for t in filtered if getattr(t, "ticker", None)]
    cache = _prepare_ohlc_cache(tickers=tickers_list, days=days)
    effects = _estimate_trade_effects(filtered, cache)
    summary = _aggregate(effects)
    tops = _top_cases(effects)
    current_rules = _get_current_decision_rule_params()
    hanger_tune_review = _build_game5m_hanger_tune_json_review(effects, summary, strategy=strategy)
    te_review_f = _build_time_exit_early_review(
        effects, cache, focused_trade_ids=trade_ids if trade_ids else None
    )
    practical = _build_practical_parameter_suggestions(effects, summary, current_rules)
    add_pr_f = hanger_tune_review.get("practical_parameter_additions") if isinstance(hanger_tune_review, dict) else None
    if isinstance(add_pr_f, list):
        practical.extend([x for x in add_pr_f if isinstance(x, dict)])
    practical.extend(_practical_suggestions_from_time_exit_early_review(te_review_f))
    critical_cases = _build_critical_case_analysis(effects, limit=5)
    game_5m_config_hints = _build_game5m_config_hints(effects, summary, hanger_tune_review=hanger_tune_review)
    game_5m_config_hints.extend(_config_hints_from_time_exit_early_review(te_review_f))
    entry_review = _build_entry_underperformance_review(effects, limit=8)
    catboost_entry_backtest = _build_catboost_entry_backtest(strategy, filtered, effects)
    catboost_fusion_entry_review = _build_game5m_catboost_fusion_entry_review(strategy, filtered, effects)
    hanger_v2_review = _build_hanger_v2_review(effects)
    continuation_gate_review = _build_continuation_gate_review(effects)
    recovery_d4a_live_f = _build_recovery_ml_d4a_live_review(
        filtered, te_review_f, effects, cache, strategy=strategy
    )
    _maybe_attach_recovery_d4a_shallow_for_analyzer(recovery_d4a_live_f, days=days, strategy=strategy)

    payload: Dict[str, Any] = {
        "meta": {
            "days": days,
            "strategy": strategy,
            "focused": True,
            "trades_analyzed": len(effects),
            "filter": {
                "tickers": [str(t).strip().upper() for t in (tickers or []) if str(t).strip()],
                "trade_ids": [int(x) for x in (trade_ids or [])],
            },
            "analyzer_source": "services.trade_effectiveness_analyzer.analyze_trade_effectiveness_focused",
            "decision_source_expected": "services.recommend_5m.get_decision_5m",
            "current_decision_rule_params": current_rules,
            "include_trade_details": bool(include_trade_details),
            "export_recovery_ml": bool(export_recovery_ml),
            "long_hold_ge_7d_minutes": LONG_HOLD_MINUTES,
            "metric_definitions": ANALYZER_METRIC_DEFINITIONS,
        },
        "summary": summary,
        "top_cases": tops,
        "practical_parameter_suggestions": practical,
        "critical_case_analysis": critical_cases,
        "game_5m_config_hints": game_5m_config_hints,
        "entry_underperformance_review": entry_review,
        "catboost_entry_backtest": catboost_entry_backtest,
        "game5m_catboost_fusion_entry_review": catboost_fusion_entry_review,
        "game5m_catboost_status": _build_game5m_catboost_status(),
        "game5m_recovery_model_status": _build_game5m_recovery_model_status(),
        "recovery_scenario_backtest": _build_recovery_scenario_backtest(effects, cache, strategy=strategy),
        "portfolio_catboost_status": _build_portfolio_catboost_status(),
        "time_exit_early_review": te_review_f,
        "time_exit_early_action_summary": _build_time_exit_early_action_summary(te_review_f),
        "recovery_ml_d4a_live_review": recovery_d4a_live_f,
        "game5m_hanger_v2_review": hanger_v2_review,
        "continuation_gate_review": continuation_gate_review,
        "game5m_hanger_tune_json_review": hanger_tune_review,
    }
    if include_trade_details:
        payload["trade_effects"] = [_trade_effect_detail_dict(e) for e in effects]
    if use_llm:
        payload["llm"] = _build_llm_recommendations(payload, game_5m_config_focus=True)
    payload["auto_config_override"] = _build_auto_config_override(payload)
    _attach_game5m_param_hypothesis_backtest_optional(
        payload,
        strategy=strategy,
        effects=effects,
        include_game5m_param_hypothesis_backtest=include_game5m_param_hypothesis_backtest,
    )
    _attach_game5m_hold_recovery_to_payload(
        payload,
        effects,
        cache,
        strategy=strategy,
        export_recovery_ml=export_recovery_ml,
        recovery_ml_export_path=recovery_ml_export_path,
    )
    _attach_multiday_lr_and_ml_arbiter(payload, strategy=strategy, closed_trades=filtered, effects=effects)
    return payload


def _append_multiday_lr_and_arbiter_text_lines(lines: List[str], report: Dict[str, Any]) -> None:
    mlr = report.get("multiday_lr_reality_check") or {}
    if mlr.get("mode") == "ok":
        lines.append("")
        lines.append("Multiday ridge (walk-forward OOS, дневные quotes):")
        lines.append(
            f"• Вердикт для прод: **{mlr.get('walkforward_production_verdict')}** — {mlr.get('walkforward_verdict_rationale_ru') or ''}"
        )
        ph = mlr.get("pooled_by_horizon") or {}
        for hk, lab in (("1", "1d"), ("2", "2d"), ("3", "3d")):
            b = ph.get(hk) if isinstance(ph, dict) else None
            if not isinstance(b, dict):
                continue
            rm, sg, nsum = (
                b.get("mean_rmse_oos_log_across_tickers"),
                b.get("mean_sign_accuracy"),
                b.get("n_points_sum"),
            )
            if rm is None and sg is None:
                continue
            lines.append(f"• Пул {lab}: средн. RMSE(log)≈{rm}, доля верного знака≈{sg}, сумм. n={nsum}")
        tas = mlr.get("trade_alignment_sample") or []
        if tas:
            lines.append("• Выборка сделок (pred/actual log vs realized % — разные шкалы):")
            for row in tas[:5]:
                if not isinstance(row, dict):
                    continue
                lines.append(
                    f"  — {row.get('ticker')} #{row.get('trade_id')}: realized={row.get('realized_pct_trade')}% "
                    f"pred1d={row.get('pred_log_ret_1d')} actual1d={row.get('actual_log_ret_1d')}"
                )
    elif mlr.get("note"):
        lines.append("")
        lines.append(f"Multiday ridge: {mlr.get('note')}")
    arb = report.get("ml_production_arbiter") or {}
    concl = arb.get("conclusion_ru")
    if concl:
        lines.append("")
        lines.append("Арбитр готовности ML к продакшену:")
        lines.extend(str(concl).split("\n"))
    pia = report.get("product_ideas_arbiter") or {}
    pconcl = pia.get("conclusion_ru")
    if pconcl:
        lines.append("")
        lines.append("Арбитр продуктовых идей (песочница):")
        lines.extend(str(pconcl).split("\n"))


def format_trade_effectiveness_text(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    meta = report.get("meta") or {}
    if summary.get("total", 0) == 0:
        if meta.get("focused"):
            flt = meta.get("filter") or {}
            parts = []
            if flt.get("tickers"):
                parts.append("тикеры: " + ", ".join(str(x) for x in flt["tickers"]))
            if flt.get("trade_ids"):
                parts.append("trade_id: " + ", ".join(str(x) for x in flt["trade_ids"]))
            extra = (" (" + "; ".join(parts) + ")") if parts else ""
            head = f"За выбранный период по узкому фильтру закрытых сделок не найдено{extra}."
        else:
            head = "За выбранный период закрытых сделок не найдено."
        out0: List[str] = [head]
        _append_multiday_lr_and_arbiter_text_lines(out0, report)
        return "\n".join(out0)
    top = report.get("top_cases") or {}
    title = (
        "📊 Узкий анализ (выбранные сделки / окно)"
        if meta.get("focused")
        else "📊 Анализатор эффективности сделок"
    )
    filter_line = ""
    if meta.get("focused"):
        flt = meta.get("filter") or {}
        if flt.get("tickers") or flt.get("trade_ids"):
            filter_line = (
                f"Фильтр: тикеры={flt.get('tickers') or '—'} | trade_id={flt.get('trade_ids') or '—'}"
            )
    lines = [
        title,
        f"Период: {meta.get('days', '—')} дн. | Стратегия: {meta.get('strategy', '—')}",
    ]
    if filter_line:
        lines.append(filter_line)
    lines.extend(
        [
            f"Сделок: {summary['total']} | Win rate: {summary['win_rate_pct']:.2f}% | Net PnL: ${summary['sum_net_pnl_usd']:+.2f}",
            f"Средний результат: {summary['avg_realized_pct']:+.3f}% | Медиана: {summary['median_realized_pct']:+.3f}%",
            f"Упущенный upside: Σ {summary['sum_missed_upside_pct']:+.3f}% | Избежимый loss: Σ {summary['sum_avoidable_loss_pct']:+.3f}%",
            f"По выигрышным: Σ missed {summary.get('sum_missed_upside_pct_on_wins', 0):+.3f}% | "
            f"сделок с missed≥1%: {summary.get('wins_with_missed_upside_ge_1pct_count', 0)}",
            f"Сигналы риска: выход ниже MFE окна={summary['late_polling_signals']} (см. exit_below_window_mfe_count), "
            f"high-vol losses={summary['high_vol_losses_count']}, weak P(up) losses={summary['weak_prob_up_losses_count']}",
            f"Параметрические причины: losses@ALLOW={summary.get('losses_with_allow_entry_count', 0)}, losses@prob_up>=0.60={summary.get('losses_with_high_prob_up_count', 0)}, losses@RSI>=60={summary.get('losses_with_high_rsi_count', 0)}",
            f"Удержание ≥7 суток: {summary.get('long_hold_ge_7d_count', 0)} сделок, из них слабый результат (≤0.15%): {summary.get('long_hold_ge_7d_poor_outcome_count', 0)}; "
            f"без decision в context_json: {summary.get('trades_missing_entry_decision_count', 0)}",
            "",
            "Top losses:",
        ]
    )
    for r in (top.get("top_losses") or [])[:4]:
        ed = r.get("entry_decision") or "—"
        hm = r.get("hold_minutes")
        hm_s = f", hold {hm:.0f}m" if isinstance(hm, (int, float)) else ""
        lines.append(
            f"• {r['ticker']} #{r['trade_id']}: {r['realized_pct']:+.2f}% (exit={r['exit_signal']}, entry={ed}{hm_s})"
        )
    win_missed = top.get("top_profitable_missed_upside") or []
    if win_missed and any((row.get("missed_upside_pct") or 0) >= 0.5 for row in win_missed[:4]):
        lines.append("")
        lines.append("Выигрышные, но с крупным недобором (ранний выход vs high окна):")
        for r in win_missed[:4]:
            if (r.get("missed_upside_pct") or 0) < 0.25:
                continue
            lines.append(
                f"• {r['ticker']} #{r['trade_id']}: +{r['realized_pct']:.2f}%, missed {r.get('missed_upside_pct', 0):+.2f}% "
                f"(exit={r['exit_signal']})"
            )
    practical = report.get("practical_parameter_suggestions") or []
    if practical:
        lines.append("")
        lines.append("Практические изменения:")
        for p in practical[:4]:
            lines.append(
                f"• {p.get('parameter')}: {p.get('current')} -> {p.get('proposed')} | {p.get('why')}"
            )
    teas = report.get("time_exit_early_action_summary") or {}
    if isinstance(teas, dict) and int(teas.get("time_exit_early_trades_total") or 0) > 0:
        lines.append("")
        lines.append("TIME_EXIT_EARLY (сводка):")
        lines.append(
            f"• Сделок: {teas.get('time_exit_early_trades_total', 0)}, с OHLC: {teas.get('rows_with_ohlc', 0)}, "
            f"whipsaw 1h: {teas.get('premature_whipsaw_1h_count', 0)}"
            + (
                f" (доля {float(teas['premature_whipsaw_rate_given_ohlc']):.2%})"
                if teas.get("premature_whipsaw_rate_given_ohlc") is not None
                else ""
            )
        )
        if teas.get("tune_priority_hint"):
            lines.append(f"• {teas['tune_priority_hint']}")
        lines.append(
            f"• Предложения high/medium: {'да' if teas.get('has_actionable_proposals_high_or_medium') else 'нет'} "
            f"({teas.get('actionable_proposals_count', 0)} шт.) | готово к правке порогов: "
            f"{'да' if teas.get('ready_for_parameter_review') else 'нет'}"
        )
        if teas.get("insufficient_data_for_ml"):
            lines.append("• Для будущего ML recovery: мало строк (insufficient_data_for_ml).")
        if teas.get("focused_trade_rows_matched"):
            lines.append(f"• Focused: совпало строк отчёта: {teas.get('focused_trade_rows_matched')}.")

    # Recovery ML (офлайн): статус модели и сценарий задержки выхода для TIME_EXIT_EARLY.
    rec_st = report.get("game5m_recovery_model_status") or {}
    rec_sc = report.get("recovery_scenario_backtest") or {}
    if isinstance(rec_st, dict) and (rec_st.get("model_file_exists") or rec_st.get("meta_file_exists")):
        lines.append("")
        lines.append("Recovery ML (офлайн, не влияет на live-выходы):")
        ms = rec_st.get("meta_summary") if isinstance(rec_st.get("meta_summary"), dict) else {}
        auc = ms.get("auc_valid")
        nt = ms.get("n_total")
        hv = ms.get("horizon_minutes")
        tau = rec_sc.get("tau") if isinstance(rec_sc, dict) else None
        scored = rec_sc.get("scored") if isinstance(rec_sc, dict) else None
        te_n = rec_sc.get("time_exit_early_trades") if isinstance(rec_sc, dict) else None
        pol = rec_sc.get("would_delay_policy_count") if isinstance(rec_sc, dict) else None
        wdd = rec_sc.get("would_delay_with_delta_count") if isinstance(rec_sc, dict) else None
        missing = rec_sc.get("delta_missing_reasons") if isinstance(rec_sc, dict) else None
        mean_delta = None
        if isinstance(rec_sc, dict) and isinstance(rec_sc.get("among_would_delay"), dict):
            mean_delta = rec_sc["among_would_delay"].get("mean_delta_pct")
        lines.append(
            "• model: "
            + ("ok" if rec_st.get("model_file_exists") and rec_st.get("meta_file_exists") else "partial")
            + (f", AUC={auc}" if auc is not None else "")
            + (f", n={nt}" if nt is not None else "")
            + (f", H={hv}m" if hv is not None else "")
        )
        if tau is not None:
            lines.append(
                f"• scenario: τ={tau}, TIME_EXIT_EARLY={te_n}, scored={scored}, "
                f"P<τ={pol}, delta_ok={wdd}"
                + (f", meanΔ={mean_delta:+.3f}%" if isinstance(mean_delta, (int, float)) else "")
            )
        if isinstance(missing, dict) and missing:
            # Покажем только 1–2 причины, чтобы не раздувать текст.
            top_m = sorted(((k, int(v)) for k, v in missing.items() if isinstance(v, int)), key=lambda kv: -kv[1])[:2]
            if top_m:
                lines.append("• delta missing: " + ", ".join([f"{k}={v}" for k, v in top_m]))
    d4a = report.get("recovery_ml_d4a_live_review") or {}
    if isinstance(d4a, dict) and d4a.get("mode") == "ok":
        lines.append("")
        lines.append("Recovery ML D4a (live телеметрия в SELL context_json):")
        lines.append(
            f"• TE GAME_5M={d4a.get('time_exit_early_game5m_in_window')}, с gate={d4a.get('trades_with_recovery_gate')}, "
            f"n_P={d4a.get('trades_gate_ok_with_proba')}, n_ΔK={d4a.get('trades_with_delta_k')} "
            f"(defer-bars={d4a.get('defer_delay_bars_config')}, live_τ={d4a.get('live_tau_hold_config')})"
        )
        rw = d4a.get("recorded_would_defer_summary")
        if isinstance(rw, dict) and rw.get("count"):
            lines.append(
                f"• recorded would_defer: n={rw.get('count')}, meanΔ={rw.get('mean_delta_delayed_minus_actual_pct')}, "
                f"mean post-MFE 1h={rw.get('mean_post_exit_mfe_pct_1h')}"
            )
        sweep = d4a.get("tau_sweep") or []
        lt = d4a.get("live_tau_hold_config")
        pick = None
        if isinstance(lt, (int, float)) and sweep:
            pick = next(
                (x for x in sweep if abs(float(x.get("tau") or 0) - float(lt)) < 1e-9),
                None,
            )
        if isinstance(pick, dict):
            lines.append(
                f"• tau_sweep @ live τ: defer n={pick.get('defer_count')}, meanΔ={pick.get('mean_delta_delayed_minus_actual_pct')}"
            )
        btk = d4a.get("best_tau_by_k") or {}
        if isinstance(btk, dict) and btk:
            parts = []
            for ks, row in sorted(btk.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
                if isinstance(row, dict):
                    parts.append(
                        f"K={ks}: τ*={row.get('tau')}, meanΔ={row.get('mean_delta_delayed_minus_actual_pct')}, n={row.get('defer_count')}"
                    )
            if parts:
                lines.append("• best τ by K (max mean Δ): " + " | ".join(parts[:8]))
        wsg = d4a.get("window_suggestion") or {}
        if isinstance(wsg, dict) and wsg.get("hints_ru"):
            lines.append("• окно (shallow): " + " ".join(str(x) for x in (wsg.get("hints_ru") or [])[:2]))
    elif isinstance(d4a, dict) and d4a.get("note"):
        lines.append("")
        lines.append(f"Recovery ML D4a: {d4a.get('note')}")
    cb = report.get("catboost_entry_backtest") or {}
    if cb.get("mode") == "game5m_entry_context":
        cal = cb.get("calibration") or {}
        lines.append("")
        lines.append("CatBoost (бэктест P(благоприятный исход) на context_json входа):")
        lines.append(
            f"• Сделок со скором: {cb.get('trades_scored_ok', 0)} / учтено в отчёте: {cb.get('trades_considered', 0)}"
        )
        if cal.get("mean_p_given_win") is not None:
            mpl = cal.get("mean_p_given_loss")
            if mpl is not None:
                lines.append(f"• Средний P | win: {cal['mean_p_given_win']:.3f} | Средний P | loss: {mpl:.3f}")
            else:
                lines.append(f"• Средний P | win: {cal['mean_p_given_win']:.3f}")
        for b in cal.get("buckets") or []:
            if b.get("n"):
                lines.append(
                    f"  {b.get('p_range')}: n={b['n']}, win_rate={b.get('win_rate_pct')}%"
                )
        if cal.get("note"):
            lines.append(f"• {cal['note']}")
    elif cb.get("note"):
        lines.append("")
        lines.append(f"CatBoost: {cb.get('note')}")
    _append_multiday_lr_and_arbiter_text_lines(lines, report)
    entry_rev = report.get("entry_underperformance_review") or []
    if entry_rev:
        lines.append("")
        lines.append("Разбор входа (слабый результат / долгое удержание):")
        for row in entry_rev[:5]:
            tu = row.get("ticker")
            tid = row.get("trade_id")
            rc = row.get("realized_pct")
            ed = row.get("entry_decision") or "—"
            ex_full = str(row.get("entry_reasoning_excerpt") or "").replace("\n", " ").strip()
            ex = ex_full[:140]
            keys = row.get("suggested_config_env_review") or []
            ks = ", ".join(str(k) for k in keys[:5]) if keys else "—"
            lh = "да" if row.get("long_hold_ge_7d") else "нет"
            lines.append(f"• {tu} #{tid}: {rc:+.2f}% | entry={ed} | ≥7d={lh} | env: {ks}")
            if ex:
                suffix = "…" if len(ex_full) > 140 else ""
                lines.append(f"  reasoning: {ex}{suffix}")

    critical = report.get("critical_case_analysis") or []
    if critical:
        lines.append("")
        lines.append("Критичные кейсы:")
        for c in critical[:4]:
            lines.append(
                f"• {c.get('ticker')} #{c.get('trade_id')}: {c.get('diagnosis')} | action: {c.get('action')}"
            )
    hyp = report.get("game5m_param_hypothesis_backtest")
    if isinstance(hyp, dict) and hyp.get("hanger_hypotheses") is not None:
        lines.append("")
        lines.append("GAME_5M param backtest (висяки / недобор, офлайн):")
        if hyp.get("status") == "error":
            lines.append(f"• ошибка: {hyp.get('reason')}")
        else:
            hc = hyp.get("hanger_hypotheses") or []
            uc = hyp.get("underprofit_hypotheses") or []
            mr = hyp.get("mergeable_recommendations") or []
            lines.append(f"• висяки (строк): {len(hc)} | недобор: {len(uc)} | mergeable_hints: {len(mr)}")
    hints = report.get("game_5m_config_hints") or []
    if hints:
        lines.append("")
        lines.append("Эвристики GAME_5M (что пересмотреть в config.env):")
        for h in hints[:8]:
            ek = h.get("env_key", "")
            direction = h.get("direction", "")
            evidence = h.get("evidence", "")
            rationale = h.get("rationale", "")
            lines.append(f"• {ek} ({direction}): {evidence}")
            if rationale:
                lines.append(f"  → {rationale}")
    if report.get("llm"):
        llm = report["llm"]
        lines.append("")
        lines.append("LLM:")
        if llm.get("status") == "ok":
            ana = llm.get("analysis")
            if isinstance(ana, dict):
                lines.append(json.dumps(ana, ensure_ascii=False, indent=2)[:1800])
            else:
                lines.append(str(ana)[:1800])
        else:
            lines.append(f"{llm.get('status')}: {llm.get('reason')}")
    return "\n".join(lines)
