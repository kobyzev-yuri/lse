"""
Сбор единого JSON по полноте данных для ML: БД (KB, trade_history), артефакты CatBoost, CSV-датасеты.

Используется scripts/run_ml_data_quality_report.py и опционально LLM-слоем services/ml_data_quality_llm.py.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, text

logger = logging.getLogger(__name__)

REPORT_VERSION = "1.1"


def _json_load(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug("ml_data_quality: skip meta %s: %s", path, e)
        return None


def _tail_jsonl(path: Path, max_lines: int = 3) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            continue
    return out


def collect_knowledge_base_stats(engine) -> Dict[str, Any]:
    out: Dict[str, Any] = {"error": None}
    try:
        with engine.connect() as conn:
            total = conn.execute(text("SELECT COUNT(*) FROM knowledge_base")).scalar()
            out["rows_total"] = int(total or 0)
            et = conn.execute(
                text(
                    """
                    SELECT COALESCE(event_type, '') AS et, COUNT(*) AS c
                    FROM knowledge_base
                    GROUP BY COALESCE(event_type, '')
                    ORDER BY c DESC
                    """
                )
            ).mappings().all()
            out["by_event_type"] = {str(r["et"]): int(r["c"]) for r in et}
            row = conn.execute(
                text(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE outcome_json IS NULL) AS null_outcome,
                      COUNT(*) FILTER (WHERE outcome_json IS NOT NULL) AS has_outcome,
                      COUNT(*) FILTER (WHERE embedding IS NULL) AS null_embedding,
                      COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS has_embedding,
                      COUNT(*) FILTER (WHERE COALESCE(ingested_at, ts) >= NOW() - INTERVAL '30 days') AS rows_last_30d
                    FROM knowledge_base
                    """
                )
            ).mappings().first()
            if row:
                out["outcome_json_null"] = int(row["null_outcome"] or 0)
                out["outcome_json_present"] = int(row["has_outcome"] or 0)
                out["embedding_null"] = int(row["null_embedding"] or 0)
                out["embedding_present"] = int(row["has_embedding"] or 0)
                out["rows_last_30d"] = int(row["rows_last_30d"] or 0)
            out["outcome_json_fill_rate"] = (
                round(out["outcome_json_present"] / out["rows_total"], 4) if out.get("rows_total") else 0.0
            )
    except Exception as e:
        out["error"] = str(e)
        logger.warning("knowledge_base stats: %s", e)
    return out


def collect_trade_history_ml_stats(engine) -> Dict[str, Any]:
    """
    Полнота полей, нужных для ML по сделкам: топ стратегий по числу BUY.
    """
    out: Dict[str, Any] = {"error": None, "strategies": {}}
    try:
        with engine.connect() as conn:
            top = conn.execute(
                text(
                    """
                    SELECT TRIM(strategy_name) AS s, COUNT(*) AS c
                    FROM trade_history
                    WHERE UPPER(TRIM(side)) = 'BUY'
                      AND strategy_name IS NOT NULL
                      AND TRIM(strategy_name) != ''
                    GROUP BY TRIM(strategy_name)
                    ORDER BY c DESC
                    LIMIT 15
                    """
                )
            ).mappings().all()
            for r in top:
                strat = str(r["s"])
                q = text(
                    """
                    SELECT
                      COUNT(*) AS closed,
                      COUNT(*) FILTER (WHERE context_json IS NULL) AS buy_ctx_null,
                      COUNT(*) FILTER (WHERE context_json IS NOT NULL AND context_json::text != '{}'
                        AND (context_json::jsonb ? 'decision')) AS buy_ctx_has_decision
                    FROM trade_history
                    WHERE TRIM(strategy_name) = :s
                      AND UPPER(TRIM(side)) = 'BUY'
                    """
                )
                row = conn.execute(q, {"s": strat}).mappings().first()
                if not row:
                    continue
                closed = int(row["closed"] or 0)
                ctx_null = int(row["buy_ctx_null"] or 0)
                has_dec = int(row["buy_ctx_has_decision"] or 0)
                out["strategies"][strat] = {
                    "buy_rows": closed,
                    "context_json_null": ctx_null,
                    "context_json_has_decision_key": has_dec,
                    "context_json_fill_rate": round((closed - ctx_null) / closed, 4) if closed else 0.0,
                    "decision_key_rate_among_non_null": (
                        round(has_dec / max(1, closed - ctx_null), 4) if closed - ctx_null > 0 else 0.0
                    ),
                }
    except Exception as e:
        out["error"] = str(e)
        logger.warning("trade_history ml stats: %s", e)
    return out


def collect_quotes_coverage_stats(engine, tickers: Optional[List[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"error": None}
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT COUNT(DISTINCT ticker) AS tickers,
                           COUNT(*) AS rows,
                           MIN(date)::text AS min_d,
                           MAX(date)::text AS max_d
                    FROM quotes
                    """
                )
            ).mappings().first()
            if row:
                out["distinct_tickers"] = int(row["tickers"] or 0)
                out["rows"] = int(row["rows"] or 0)
                out["min_date"] = row["min_d"]
                out["max_date"] = row["max_d"]
            if tickers:
                tix = sorted({t.strip().upper() for t in tickers if str(t).strip()})
                if tix:
                    stmt = (
                        text(
                            """
                            SELECT ticker, COUNT(*) AS c, MAX(date)::text AS last_d
                            FROM quotes
                            WHERE UPPER(TRIM(ticker)) IN :tix
                            GROUP BY ticker
                            """
                        ).bindparams(bindparam("tix", expanding=True))
                    )
                    r2 = conn.execute(stmt, {"tix": tix}).mappings().all()
                    out["per_ticker_fast"] = {
                        str(x["ticker"]).upper(): {"rows": int(x["c"]), "last_date": x["last_d"]} for x in r2
                    }
    except Exception as e:
        out["error"] = str(e)
    return out


def profile_csv_light(path: Path, *, sample_rows: int = 5000) -> Dict[str, Any]:
    """Без тяжёлого полного скана: первые sample_rows для null-rate и типов."""
    p = path.expanduser()
    if not p.is_file():
        return {"path": str(p), "exists": False}
    try:
        import pandas as pd

        df = pd.read_csv(p, nrows=sample_rows)
    except Exception as e:
        return {"path": str(p), "exists": True, "error": str(e)}
    nulls = df.isna().mean().to_dict()
    null_rate = {k: round(float(v), 4) for k, v in nulls.items()}
    return {
        "path": str(p),
        "exists": True,
        "sample_rows_read": int(len(df)),
        "columns": list(df.columns),
        "dtypes": {c: str(df[c].dtype) for c in df.columns},
        "null_rate_sample": null_rate,
    }


def default_dataset_paths(project_root: Path) -> List[Path]:
    base = project_root / "local" / "datasets"
    names = [
        "game5m_stuck_dataset.csv",
        "game5m_continuation_dataset.csv",
    ]
    return [base / n for n in names]


def collect_catboost_artifact_meta(project_root: Path) -> Dict[str, Any]:
    """Читает .meta.json рядом с типовыми путями моделей (локально и /app/logs)."""
    candidates = [
        project_root / "local" / "models" / "game5m_entry_catboost.meta.json",
        Path("/app/logs/ml/models/game5m_entry_catboost.meta.json"),
        project_root / "local" / "models" / "portfolio_return_catboost.meta.json",
        Path("/app/logs/ml/models/portfolio_return_catboost.meta.json"),
        project_root / "local" / "models" / "game5m_recovery_catboost.meta.json",
        Path("/app/logs/ml/models/game5m_recovery_catboost.meta.json"),
    ]
    found: Dict[str, Any] = {}
    for p in candidates:
        data = _json_load(p)
        if data:
            key = p.name.replace(".meta.json", "")
            found[key] = {"path": str(p), "meta": data}
    return found


def _table_exists(conn, table_name: str) -> bool:
    r = conn.execute(
        text(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = :t
            """
        ),
        {"t": table_name},
    ).scalar()
    return r is not None


def collect_event_analytics_stats(engine) -> Dict[str, Any]:
    """
    Таблицы миграции migrate_ml_event_analytics + статистика event_reaction_dataset.
    """
    out: Dict[str, Any] = {"error": None, "tables": {}, "event_reaction_dataset": None}
    expected = (
        "earnings_event_detail",
        "earnings_material",
        "peer_graph_edge",
        "market_regime_daily",
        "event_reaction_dataset",
    )
    try:
        with engine.connect() as conn:
            present = {t: _table_exists(conn, t) for t in expected}
            out["tables"] = present
            out["tables_present_all"] = all(present.values())
            if not present.get("event_reaction_dataset"):
                out["event_reaction_dataset"] = {
                    "note": "Таблица отсутствует — выполните scripts/migrate_ml_event_analytics.py",
                }
                return out
            row = conn.execute(text("SELECT COUNT(*) FROM event_reaction_dataset")).scalar()
            n = int(row or 0)
            row = conn.execute(
                text(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE final_label IS NULL OR TRIM(final_label) = '') AS unlabeled,
                      COUNT(*) FILTER (WHERE final_label IS NOT NULL AND TRIM(final_label) != '') AS labeled,
                      COUNT(*) FILTER (WHERE features_before IS NOT NULL AND features_before <> '{}'::jsonb) AS with_features,
                      COUNT(*) FILTER (WHERE outcomes_after IS NOT NULL AND outcomes_after <> '{}'::jsonb) AS with_outcomes,
                      MIN(event_time_et)::text AS min_event_et,
                      MAX(event_time_et)::text AS max_event_et
                    FROM event_reaction_dataset
                    """
                )
            ).mappings().first()
            ver = conn.execute(
                text(
                    """
                    SELECT dataset_version AS v, COUNT(*) AS c
                    FROM event_reaction_dataset
                    GROUP BY dataset_version
                    ORDER BY c DESC
                    """
                )
            ).mappings().all()
            sym = conn.execute(
                text("SELECT COUNT(DISTINCT symbol) FROM event_reaction_dataset")
            ).scalar()
            out["event_reaction_dataset"] = {
                "rows_total": n,
                "distinct_symbols": int(sym or 0),
                "by_dataset_version": {str(x["v"]): int(x["c"]) for x in ver},
                "unlabeled": int((row or {}).get("unlabeled") or 0) if row else 0,
                "labeled": int((row or {}).get("labeled") or 0) if row else 0,
                "with_features_before": int((row or {}).get("with_features") or 0) if row else 0,
                "with_outcomes_after": int((row or {}).get("with_outcomes") or 0) if row else 0,
                "min_event_time_et": (row or {}).get("min_event_et"),
                "max_event_time_et": (row or {}).get("max_event_et"),
                "label_coverage_rate": round((row or {}).get("labeled", 0) / n, 4) if n and row else 0.0,
            }
            if present.get("earnings_event_detail"):
                ed = conn.execute(text("SELECT COUNT(*) FROM earnings_event_detail")).scalar()
                out["earnings_event_detail_rows"] = int(ed or 0)
            if present.get("earnings_material"):
                em = conn.execute(text("SELECT COUNT(*) FROM earnings_material")).scalar()
                em_status = conn.execute(
                    text(
                        """
                        SELECT parse_status, COUNT(*) AS c
                        FROM earnings_material
                        GROUP BY parse_status
                        ORDER BY c DESC
                        """
                    )
                ).mappings().all()
                out["earnings_material_rows"] = int(em or 0)
                out["earnings_material_by_parse_status"] = {
                    str(x["parse_status"]): int(x["c"]) for x in em_status
                }
            if present.get("peer_graph_edge"):
                pe = conn.execute(text("SELECT COUNT(*) FROM peer_graph_edge")).scalar()
                out["peer_graph_edge_rows"] = int(pe or 0)
            if present.get("market_regime_daily"):
                mr = conn.execute(text("SELECT COUNT(*) FROM market_regime_daily")).scalar()
                out["market_regime_daily_rows"] = int(mr or 0)
    except Exception as e:
        out["error"] = str(e)
        logger.warning("event analytics stats: %s", e)
    return out


def collect_ml_train_readiness_tail(project_root: Path, *, max_lines: int = 3) -> Dict[str, Any]:
    paths = [
        Path("/app/logs/ml/logs/ml_train_readiness.jsonl"),
        project_root / "local" / "logs" / "ml_train_readiness.jsonl",
    ]
    for p in paths:
        tail = _tail_jsonl(p, max_lines=max_lines)
        if tail:
            return {"path": str(p), "last_records": tail}
    return {"path": None, "last_records": []}


def collect_game5m_daily_ml_tail(project_root: Path, *, max_lines: int = 2) -> Dict[str, Any]:
    paths = [
        Path("/app/logs/ml/logs/game5m_daily_ml_report.jsonl"),
        project_root / "local" / "logs" / "ml" / "game5m_daily_ml_report.jsonl",
    ]
    for p in paths:
        tail = _tail_jsonl(p, max_lines=max_lines)
        if tail:
            return {"path": str(p), "last_records": tail}
    return {"path": None, "last_records": []}


def collect_portfolio_ml_report_tail(project_root: Path, *, max_lines: int = 2) -> Dict[str, Any]:
    paths = [
        Path("/app/logs/ml/logs/portfolio_daily_ml_report.jsonl"),
        project_root / "local" / "logs" / "portfolio_daily_ml_report.jsonl",
    ]
    for p in paths:
        tail = _tail_jsonl(p, max_lines=max_lines)
        if tail:
            return {"path": str(p), "last_records": tail}
    return {"path": None, "last_records": []}


def build_ml_data_quality_report(
    *,
    project_root: Path,
    engine,
    dataset_paths: Optional[List[Path]] = None,
    fast_tickers: Optional[List[str]] = None,
    train_metrics_paths: Optional[Dict[str, Path]] = None,
) -> Dict[str, Any]:
    """
    engine — SQLAlchemy engine (get_engine()).
    train_metrics_paths — опционально пути к JSON от последнего dry-run (например game5m из --json-metrics-out).
    """
    paths = dataset_paths if dataset_paths is not None else default_dataset_paths(project_root)
    datasets = {str(p): profile_csv_light(p) for p in paths}

    bundle: Dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "services.ml_data_quality_report.build_ml_data_quality_report",
        "knowledge_base": collect_knowledge_base_stats(engine),
        "trade_history_ml": collect_trade_history_ml_stats(engine),
        "quotes": collect_quotes_coverage_stats(engine, tickers=fast_tickers),
        "dataset_files": datasets,
        "catboost_meta_artifacts": collect_catboost_artifact_meta(project_root),
        "portfolio_ml_report_jsonl": collect_portfolio_ml_report_tail(project_root),
        "game5m_daily_ml_report_jsonl": collect_game5m_daily_ml_tail(project_root),
        "ml_train_readiness_jsonl": collect_ml_train_readiness_tail(project_root),
        "event_analytics": collect_event_analytics_stats(engine),
        "external_train_metrics": {},
    }
    if train_metrics_paths:
        ext: Dict[str, Any] = {}
        for name, p in train_metrics_paths.items():
            if p is None:
                continue
            pp = Path(p).expanduser()
            data = _json_load(pp)
            if data:
                ext[name] = {"path": str(pp), "data": data}
            else:
                ext[name] = {"path": str(pp), "data": None, "note": "missing_or_invalid"}
        bundle["external_train_metrics"] = ext
    return bundle


def enrich_ml_data_quality_for_strategy(bundle: Dict[str, Any], strategy: str) -> Dict[str, Any]:
    """Срез readiness/метрик под выбранную стратегию в анализаторе (GAME_5M | PORTFOLIO | ALL)."""
    from config_loader import get_config_value

    su = (strategy or "GAME_5M").strip().upper()
    bundle["strategy_focus"] = su
    try:
        bundle["readiness_thresholds"] = {
            "portfolio_rmse_max": float((get_config_value("ML_READINESS_PORTFOLIO_RMSE_MAX", "0.08") or "0.08").strip()),
            "game5m_auc_min": float((get_config_value("ML_READINESS_GAME5M_AUC_MIN", "0.52") or "0.52").strip()),
            "event_reaction_rmse_max": float(
                (get_config_value("ML_READINESS_EVENT_REACTION_RMSE_MAX", "0.12") or "0.12").strip()
            ),
        }
    except (ValueError, TypeError):
        bundle["readiness_thresholds"] = {
            "portfolio_rmse_max": 0.08,
            "game5m_auc_min": 0.52,
            "event_reaction_rmse_max": 0.12,
        }
    recs = (bundle.get("ml_train_readiness_jsonl") or {}).get("last_records") or []
    latest = recs[-1] if recs else {}
    bundle["readiness_latest"] = {
        "ts_utc": latest.get("ts_utc"),
        "train_mode": latest.get("train_mode"),
        "overall_production_ready": latest.get("overall_production_ready"),
        "portfolio": latest.get("portfolio"),
        "game5m": latest.get("game5m"),
        "event_reaction": latest.get("event_reaction"),
    }
    ext = bundle.get("external_train_metrics") if isinstance(bundle.get("external_train_metrics"), dict) else {}
    if su == "PORTFOLIO":
        bundle["external_train_metrics_focus"] = {
            k: v for k, v in ext.items() if "portfolio" in k.lower()
        }
    elif su == "GAME_5M":
        bundle["external_train_metrics_focus"] = {
            k: v for k, v in ext.items() if "game5m" in k.lower() or "game5m" in k
        }
    else:
        bundle["external_train_metrics_focus"] = dict(ext)
    return bundle
