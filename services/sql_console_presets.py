"""Read-only SQL presets for the web SQL console (/sql)."""
from __future__ import annotations

from typing import Any, TypedDict


class SqlConsolePreset(TypedDict):
    id: str
    title: str
    description: str
    sql: str


class SqlConsolePresetGroup(TypedDict):
    id: str
    title: str
    description: str
    presets: list[SqlConsolePreset]


SQL_CONSOLE_PRESET_GROUPS: list[SqlConsolePresetGroup] = [
    {
        "id": "general",
        "title": "Общие",
        "description": "Базовые проверки сделок и котировок.",
        "presets": [
            {
                "id": "decision_snapshot",
                "title": "decision_snapshot (GAME_5M BUY)",
                "description": "Последние входы с decision stack snapshot: divergence legacy vs projected.",
                "sql": """SELECT id, ticker,
       context_json->'decision_snapshot'->>'resolve_divergence' AS div,
       context_json->'decision_snapshot'->>'effective_decision' AS eff,
       context_json->'decision_snapshot'->>'projected_effective_if_resolve' AS proj,
       ts
FROM trade_history
WHERE strategy_name = 'GAME_5M' AND side = 'BUY'
  AND context_json ? 'decision_snapshot'
ORDER BY id DESC
LIMIT 20""",
            },
            {
                "id": "trades_24h",
                "title": "Сделки за 24 ч",
                "description": "Все строки trade_history за сутки — быстрый sanity-check активности.",
                "sql": """SELECT id, ticker, side, signal_type, strategy_name, ts
FROM trade_history
WHERE ts >= NOW() - INTERVAL '24 hours'
ORDER BY id DESC
LIMIT 50""",
            },
            {
                "id": "quotes_last",
                "title": "Последняя дата quotes",
                "description": "Проверка свежести дневных котировок.",
                "sql": "SELECT MAX(date) AS quotes_last_date FROM quotes",
            },
        ],
    },
    {
        "id": "ml_continuation",
        "title": "Continuation ML (TAKE telemetry)",
        "description": (
            "Shadow-контур continuation_ml в SELL context_json при TAKE_PROFIT*. "
            "Нужен GAME_5M_CONTINUATION_ML_ENABLED=true. Apply не меняет выход, пока GATE_MODE=log_only."
        ),
        "presets": [
            {
                "id": "continuation_ml_wait_dashboard",
                "title": "Сводка ожидания telemetry (начать здесь)",
                "description": "Последний TAKE, дней без TAKE, покрытие continuation_ml. Пустой «последние с ml» — норма, пока with_ml=0.",
                "sql": """SELECT
  MAX(ts) FILTER (
    WHERE signal_type IN ('TAKE_PROFIT', 'TAKE_PROFIT_SUSPEND')
  ) AS last_take_ts,
  ROUND(
    EXTRACT(EPOCH FROM (
      NOW() - MAX(ts) FILTER (
        WHERE signal_type IN ('TAKE_PROFIT', 'TAKE_PROFIT_SUSPEND')
      )
    )) / 86400.0,
    1
  ) AS days_since_last_take,
  COUNT(*) FILTER (WHERE context_json ? 'continuation_ml') AS with_ml,
  COUNT(*) FILTER (WHERE NOT (context_json ? 'continuation_ml')) AS without_ml,
  COUNT(*) AS total_take_30d
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'SELL'
  AND signal_type IN ('TAKE_PROFIT', 'TAKE_PROFIT_SUSPEND')
  AND ts >= NOW() - INTERVAL '30 days'""",
            },
            {
                "id": "continuation_ml_last_take_diagnostic",
                "title": "Последние TAKE (с флагом has_ml)",
                "description": "Все TAKE за 30 д — видно, есть ли continuation_ml. Без фильтра ? continuation_ml.",
                "sql": """SELECT id, ticker, signal_type, ts,
       context_json ? 'continuation_ml' AS has_ml,
       context_json->'continuation_ml'->>'status' AS st,
       context_json->'continuation_ml'->>'continuation_proba' AS p
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'SELL'
  AND signal_type IN ('TAKE_PROFIT', 'TAKE_PROFIT_SUSPEND')
  AND ts >= NOW() - INTERVAL '30 days'
ORDER BY id DESC
LIMIT 30""",
            },
            {
                "id": "continuation_ml_recent",
                "title": "Последние TAKE с continuation_ml",
                "description": "Строки с телеметрией: P(missed upside), would_defer_take, multiday_block.",
                "sql": """SELECT id, ticker, signal_type, ts,
       context_json->'continuation_ml'->>'status' AS st,
       context_json->'continuation_ml'->>'continuation_proba' AS p,
       context_json->'continuation_ml'->>'would_defer_take' AS would_defer,
       context_json->'continuation_ml'->>'would_defer_by_model' AS model_would,
       context_json->'continuation_ml'->>'multiday_block' AS md_block,
       context_json->'continuation_ml'->>'log_only' AS log_only
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'SELL'
  AND signal_type IN ('TAKE_PROFIT', 'TAKE_PROFIT_SUSPEND')
  AND context_json ? 'continuation_ml'
ORDER BY id DESC
LIMIT 30""",
            },
            {
                "id": "continuation_ml_coverage",
                "title": "Покрытие telemetry (30 дней)",
                "description": "Сколько TAKE закрытий уже с continuation_ml vs без — цель ≥8–15 для go/no-go.",
                "sql": """SELECT
  COUNT(*) FILTER (WHERE context_json ? 'continuation_ml') AS with_ml,
  COUNT(*) FILTER (WHERE NOT (context_json ? 'continuation_ml')) AS without_ml,
  COUNT(*) AS total_take_exits
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'SELL'
  AND signal_type IN ('TAKE_PROFIT', 'TAKE_PROFIT_SUSPEND')
  AND ts >= NOW() - INTERVAL '30 days'""",
            },
            {
                "id": "continuation_ml_status_breakdown",
                "title": "Статусы predict / skip",
                "description": "Доля ok vs predict_failed vs skipped — ловит сломанную модель или конфиг.",
                "sql": """SELECT
  context_json->'continuation_ml'->>'status' AS status,
  context_json->'continuation_ml'->>'predict_status' AS predict_status,
  COUNT(*) AS n
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'SELL'
  AND context_json ? 'continuation_ml'
GROUP BY 1, 2
ORDER BY n DESC""",
            },
            {
                "id": "continuation_ml_defer_rollups",
                "title": "Сводка would_defer (модель vs финал)",
                "description": "Сколько раз модель хотела отложить TAKE и сколько прошло после multiday guard.",
                "sql": """SELECT
  COUNT(*) AS n,
  COUNT(*) FILTER (
    WHERE (context_json->'continuation_ml'->>'would_defer_by_model') = 'true'
  ) AS model_would_defer,
  COUNT(*) FILTER (
    WHERE (context_json->'continuation_ml'->>'would_defer_take') = 'true'
  ) AS final_would_defer,
  COUNT(*) FILTER (
    WHERE (context_json->'continuation_ml'->>'multiday_block') = 'true'
  ) AS multiday_blocked
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'SELL'
  AND context_json ? 'continuation_ml'""",
            },
            {
                "id": "continuation_ml_vs_gate",
                "title": "continuation_ml + rule gate на одной сделке",
                "description": "Сравнение CatBoost telemetry и rule-based continuation_gate на одном exit.",
                "sql": """SELECT id, ticker, ts,
       context_json->'continuation_ml'->>'continuation_proba' AS ml_p,
       context_json->'continuation_ml'->>'would_defer_take' AS ml_defer,
       context_json->'continuation_gate'->>'decision' AS gate_decision,
       context_json->'continuation_gate'->>'would_extend_take' AS gate_extend
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'SELL'
  AND context_json ? 'continuation_ml'
ORDER BY id DESC
LIMIT 25""",
            },
        ],
    },
    {
        "id": "ml_recovery",
        "title": "Recovery ML (TIME_EXIT_EARLY)",
        "description": "D4a telemetry recovery_ml_time_exit_early — см. docs/GAME_5M_TIME_EXIT_RECOVERY_PLAN.md.",
        "presets": [
            {
                "id": "recovery_ml_recent",
                "title": "Последние TIME_EXIT_EARLY с recovery ML",
                "description": "Скор P(recovery), would_defer_exit, log_only.",
                "sql": """SELECT id, ticker, ts,
       context_json->'recovery_ml_time_exit_early'->>'status' AS st,
       context_json->'recovery_ml_time_exit_early'->>'recovery_proba' AS p,
       context_json->'recovery_ml_time_exit_early'->>'would_defer_exit' AS would_defer,
       context_json->'recovery_ml_time_exit_early'->>'log_only' AS log_only
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'SELL'
  AND signal_type = 'TIME_EXIT_EARLY'
  AND context_json ? 'recovery_ml_time_exit_early'
ORDER BY id DESC
LIMIT 30""",
            },
            {
                "id": "recovery_ml_defer_rollups",
                "title": "Recovery: model vs guards defer",
                "description": "Агрегат would_defer_by_model vs would_defer_exit после гвардов.",
                "sql": """SELECT
  COUNT(*) AS n,
  COUNT(*) FILTER (
    WHERE (context_json->'recovery_ml_time_exit_early'->>'would_defer_by_model') = 'true'
  ) AS model_would,
  COUNT(*) FILTER (
    WHERE (context_json->'recovery_ml_time_exit_early'->>'would_defer_exit') = 'true'
  ) AS after_guards
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'SELL'
  AND signal_type = 'TIME_EXIT_EARLY'
  AND context_json ? 'recovery_ml_time_exit_early'""",
            },
        ],
    },
    {
        "id": "ml_entry_bar_v2",
        "title": "Entry bar v2 (shadow BUY)",
        "description": "Log-only catboost_entry_proba_good_v2 на входах; prod v1 fusion не меняется.",
        "presets": [
            {
                "id": "bar_v2_wait_dashboard",
                "title": "Сводка shadow BUY (bar v2)",
                "description": "Последний BUY и покрытие catboost_entry_proba_good_v2 — для go/no-go 1.8.",
                "sql": """SELECT
  MAX(ts) AS last_buy_ts,
  ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(ts))) / 86400.0, 1) AS days_since_last_buy,
  COUNT(*) FILTER (WHERE context_json ? 'catboost_entry_proba_good_v2') AS with_v2,
  COUNT(*) FILTER (WHERE NOT (context_json ? 'catboost_entry_proba_good_v2')) AS without_v2,
  COUNT(*) AS total_buy_30d
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'BUY'
  AND ts >= NOW() - INTERVAL '30 days'""",
            },
            {
                "id": "bar_v2_recent_buys",
                "title": "BUY с catboost_entry_proba_good_v2",
                "description": "Shadow P(upper barrier first) на последних входах GAME_5M.",
                "sql": """SELECT id, ticker, ts,
       context_json->>'catboost_bar_v2_signal_status' AS st,
       context_json->>'catboost_entry_proba_good_v2' AS p_good_v2
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'BUY'
  AND context_json ? 'catboost_entry_proba_good_v2'
ORDER BY id DESC
LIMIT 30""",
            },
        ],
    },
    {
        "id": "ml_entry_e3",
        "title": "Entry E3 (shadow BUY)",
        "description": "Log-only catboost_entry_proba_good_e3 (full T+N+C bar CatBoost); prod fusion не меняется.",
        "presets": [
            {
                "id": "e3_wait_dashboard",
                "title": "Сводка shadow BUY (entry E3)",
                "description": "Покрытие catboost_entry_proba_good_e3 на BUY — для shadow-окна step 4.",
                "sql": """SELECT
  MAX(ts) AS last_buy_ts,
  ROUND(EXTRACT(EPOCH FROM (NOW() - MAX(ts))) / 86400.0, 1) AS days_since_last_buy,
  COUNT(*) FILTER (WHERE context_json ? 'catboost_entry_proba_good_e3') AS with_e3,
  COUNT(*) FILTER (WHERE NOT (context_json ? 'catboost_entry_proba_good_e3')) AS without_e3,
  COUNT(*) AS total_buy_30d
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'BUY'
  AND ts >= NOW() - INTERVAL '30 days'""",
            },
            {
                "id": "e3_recent_buys",
                "title": "BUY с catboost_entry_proba_good_e3",
                "description": "Shadow P(y_entry_good) E3 на последних входах GAME_5M.",
                "sql": """SELECT id, ticker, ts,
       context_json->>'entry_e3_signal_status' AS st,
       context_json->>'catboost_entry_proba_good_e3' AS p_good_e3
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'BUY'
  AND context_json ? 'catboost_entry_proba_good_e3'
ORDER BY id DESC
LIMIT 30""",
            },
        ],
    },
    {
        "id": "ml_hold_h3",
        "title": "Hold H3 (shadow exit)",
        "description": "Log-only hold_quality_ml на SELL; defer только telemetry.",
        "presets": [
            {
                "id": "hold_h3_wait_dashboard",
                "title": "Сводка shadow SELL (hold H3)",
                "description": "Покрытие hold_quality_ml на выходах GAME_5M.",
                "sql": """SELECT
  MAX(ts) AS last_sell_ts,
  COUNT(*) FILTER (WHERE exit_context_json ? 'hold_quality_ml') AS with_h3,
  COUNT(*) FILTER (WHERE NOT (exit_context_json ? 'hold_quality_ml')) AS without_h3,
  COUNT(*) AS total_sell_30d
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'SELL'
  AND ts >= NOW() - INTERVAL '30 days'""",
            },
            {
                "id": "hold_h3_recent_sells",
                "title": "SELL с hold_quality_ml",
                "description": "Shadow P(y_hold_good) и would_defer_exit на последних выходах.",
                "sql": """SELECT id, ticker, ts, signal_type,
       exit_context_json->'hold_quality_ml'->>'status' AS st,
       exit_context_json->'hold_quality_ml'->>'hold_quality_proba' AS p_hold,
       exit_context_json->'hold_quality_ml'->>'would_defer_exit' AS would_defer
FROM trade_history
WHERE strategy_name = 'GAME_5M'
  AND side = 'SELL'
  AND exit_context_json ? 'hold_quality_ml'
ORDER BY id DESC
LIMIT 30""",
            },
        ],
    },
]


def sql_console_presets_for_ui() -> list[dict[str, Any]]:
    """JSON-serializable preset tree for sql_console.html."""
    return list(SQL_CONSOLE_PRESET_GROUPS)


def sql_console_preset_by_id(preset_id: str) -> SqlConsolePreset | None:
    for group in SQL_CONSOLE_PRESET_GROUPS:
        for preset in group["presets"]:
            if preset["id"] == preset_id:
                return preset
    return None
