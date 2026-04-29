#!/usr/bin/env python3
"""
Обучение CatBoostClassifier по закрытым сделкам GAME_5M и context_json на входе.

Признаки совпадают с services.catboost_5m_signal (корреляция только из JSON, без текущей матрицы).

  pip install -r requirements-catboost.txt
  python scripts/train_game5m_catboost.py [--dry-run] [--min-rows 80]

См. docs/ML_GAME5M_CATBOOST.md
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

GAME_5M = "GAME_5M"
FALSE_TAKE_TOLERANCE_PCT = 0.05


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _target_take_pct_from_exit_context(ctx: dict[str, Any]) -> float | None:
    text = str(ctx.get("exit_condition") or "")
    match = re.search(r"цель\s*~\s*([0-9]+(?:\.[0-9]+)?)%", text)
    if match:
        return _safe_float(match.group(1))
    return None


def _is_false_take_profit_by_session_high(trade_pnl: Any) -> bool:
    """
    Skip historical rows where TAKE_PROFIT was triggered by a session_high lift
    rather than by recent executable 5m highs. We leave trade_history untouched,
    but avoid teaching the model that these premature exits were clean outcomes.
    """
    ctx = _json_dict(getattr(trade_pnl, "exit_context_json", None))
    if not ctx.get("bar_high_session_lifted"):
        return False
    exit_signal = str(ctx.get("exit_signal") or getattr(trade_pnl, "signal_type", "") or "").upper()
    if exit_signal not in ("TAKE_PROFIT", "TAKE_PROFIT_SUSPEND"):
        return False
    target_pct = _target_take_pct_from_exit_context(ctx)
    entry_price = _safe_float(getattr(trade_pnl, "entry_price", None))
    recent_high = _safe_float(ctx.get("bar_high_recent_max") or ctx.get("recent_bars_high_max"))
    if target_pct is None or entry_price is None or entry_price <= 0 or recent_high is None or recent_high <= 0:
        return False
    recent_high_pct = (recent_high / entry_price - 1.0) * 100.0
    return recent_high_pct < (target_pct - FALSE_TAKE_TOLERANCE_PCT)


def main() -> int:
    parser = argparse.ArgumentParser(description="Train CatBoost on GAME_5M closed trades")
    parser.add_argument("--min-rows", type=int, default=None, help="Override GAME_5M_CATBOOST_MIN_TRAIN_ROWS")
    parser.add_argument("--valid-ratio", type=float, default=0.2, help="Last fraction of trades for validation (time-ordered)")
    parser.add_argument(
        "--out",
        type=str,
        default=str(project_root / "local" / "models" / "game5m_entry_catboost.cbm"),
        help="Output .cbm path (meta written alongside as .meta.json)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only print stats, no model file")
    parser.add_argument(
        "--label",
        choices=("net_pnl_pos", "log_return_pos"),
        default="net_pnl_pos",
        help="Binary label: net_pnl>0 or log_return>0",
    )
    args = parser.parse_args()

    try:
        from catboost import CatBoostClassifier, Pool
    except ImportError:
        logger.error("Установите catboost: pip install -r requirements-catboost.txt")
        return 1

    from config_loader import get_config_value
    from report_generator import compute_closed_trade_pnls, get_engine, load_trade_history
    from services.deal_params_5m import normalize_entry_context
    from services.catboost_5m_signal import get_catboost_feature_schema, row_from_entry_context_dict

    min_rows = args.min_rows
    if min_rows is None:
        try:
            min_rows = int((get_config_value("GAME_5M_CATBOOST_MIN_TRAIN_ROWS", "60") or "60").strip())
        except (ValueError, TypeError):
            min_rows = 60
    min_rows = max(20, min_rows)

    engine = get_engine()
    raw = load_trade_history(engine, strategy_name=GAME_5M)
    closed = compute_closed_trade_pnls(raw)

    rows: list[list] = []
    labels: list[int] = []
    meta_rows: list[tuple] = []  # (entry_ts, ticker) for sorting
    skipped_false_take = 0

    for t in closed:
        if (t.entry_strategy or "").strip().upper() != GAME_5M:
            continue
        if _is_false_take_profit_by_session_high(t):
            skipped_false_take += 1
            logger.info(
                "skip trade_id=%s %s: false_take_profit_by_session_high",
                getattr(t, "trade_id", None),
                getattr(t, "ticker", "?"),
            )
            continue
        ctx_raw = t.context_json
        if not ctx_raw:
            continue
        n = normalize_entry_context(ctx_raw)
        if not n:
            continue
        try:
            colnames, row = row_from_entry_context_dict(n, t.ticker)
        except Exception as e:
            logger.debug("skip trade_id=%s %s: %s", t.trade_id, t.ticker, e)
            continue
        if args.label == "net_pnl_pos":
            y = 1 if float(t.net_pnl) > 0 else 0
        else:
            y = 1 if float(t.log_return) > 0 else 0
        rows.append(row)
        labels.append(y)
        meta_rows.append((t.entry_ts or t.ts, t.ticker, t.trade_id))

    n_total = len(rows)
    pos = sum(labels)
    logger.info(
        "Закрытых сделок GAME_5M с context_json и валидной строкой признаков: %s (y=1: %s, y=0: %s); "
        "исключено false_take_profit_by_session_high=%s",
        n_total,
        pos,
        n_total - pos,
        skipped_false_take,
    )

    if n_total < min_rows:
        logger.warning(
            "Строк меньше порога %s — модель не пишем. Накопите историю или снизьте GAME_5M_CATBOOST_MIN_TRAIN_ROWS.",
            min_rows,
        )
        return 2

    # Time-ordered split by entry_ts (fallback exit ts)
    order = sorted(range(n_total), key=lambda i: (meta_rows[i][0] is not None, meta_rows[i][0]))
    rows = [rows[i] for i in order]
    labels = [labels[i] for i in order]
    meta_rows = [meta_rows[i] for i in order]

    n_valid = max(1, int(n_total * float(args.valid_ratio)))
    n_train = n_total - n_valid
    if n_train < 10:
        n_train = max(10, n_total // 2)
        n_valid = n_total - n_train

    train_X = rows[:n_train]
    train_y = labels[:n_train]
    valid_X = rows[n_train:]
    valid_y = labels[n_train:]

    feature_names, cat_features = get_catboost_feature_schema()
    train_pool = Pool(train_X, label=train_y, cat_features=cat_features, feature_names=feature_names)
    valid_pool = Pool(valid_X, label=valid_y, cat_features=cat_features, feature_names=feature_names)

    model = CatBoostClassifier(
        iterations=300,
        learning_rate=0.05,
        depth=6,
        loss_function="Logloss",
        eval_metric="AUC",
        random_seed=42,
        verbose=False,
        early_stopping_rounds=40,
    )
    model.fit(train_pool, eval_set=valid_pool, use_best_model=True)

    try:
        from sklearn.metrics import roc_auc_score

        proba = model.predict_proba(valid_X)[:, 1]
        auc = roc_auc_score(valid_y, proba) if len(set(valid_y)) > 1 else float("nan")
    except Exception:
        auc = float("nan")
    logger.info("Train=%s Valid=%s AUC(valid)≈%s", n_train, n_valid, auc if auc == auc else "n/a")

    out_path = Path(args.out)
    meta_path = out_path.with_suffix(".meta.json")

    if args.dry_run:
        logger.info("Dry-run: не записываем %s", out_path)
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_path))

    meta = {
        "feature_names": feature_names,
        "cat_feature_indices": cat_features,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_train": n_train,
        "n_valid": n_valid,
        "n_total": n_total,
        "excluded_false_take_profit_by_session_high": skipped_false_take,
        "label": args.label,
        "min_train_rows_config": min_rows,
        "auc_valid": round(auc, 4) if auc == auc else None,
        "game_5m_rule_version_note": "Переобучите после смены признаков или GAME_5M_RULE_VERSION",
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    logger.info("Сохранено: %s и %s", out_path, meta_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
