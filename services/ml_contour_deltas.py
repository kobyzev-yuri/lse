"""DB/file deltas and readiness gates for unified ML contour refresh."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.ml_contour_refresh import MlContourSpec, default_ml_data_quality_dir, load_refresh_log


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _json_load(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _latest_ml_train_readiness(project_root: Path) -> Dict[str, Any]:
    paths = (
        Path("/app/logs/ml/logs/ml_train_readiness.jsonl"),
        project_root / "local" / "logs" / "ml_train_readiness.jsonl",
    )
    for p in paths:
        if not p.is_file():
            continue
        last = ""
        try:
            with p.open("r", encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if s:
                        last = s
        except OSError:
            continue
        if last:
            try:
                row = json.loads(last)
                return row if isinstance(row, dict) else {}
            except json.JSONDecodeError:
                pass
    return {}


def _since_ts(refresh_log: Dict[str, Any], key: str = "last_apply_at_utc") -> Optional[datetime]:
    return _parse_ts(refresh_log.get(key) or refresh_log.get("finished_at_utc"))


def count_strategy_buys_since(engine: Engine, strategy: str, since: Optional[datetime]) -> int:
    try:
        with engine.connect() as conn:
            if since is not None:
                n = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM trade_history
                        WHERE UPPER(TRIM(strategy_name)) = :s
                          AND UPPER(TRIM(side)) = 'BUY'
                          AND ts >= :since
                        """
                    ),
                    {"s": strategy.strip().upper(), "since": since},
                ).scalar()
            else:
                n = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM trade_history
                        WHERE UPPER(TRIM(strategy_name)) = :s
                          AND UPPER(TRIM(side)) = 'BUY'
                        """
                    ),
                    {"s": strategy.strip().upper()},
                ).scalar()
            return int(n or 0)
    except Exception:
        return 0


def count_strategy_closed_since(engine: Engine, strategy: str, since: Optional[datetime]) -> int:
    """
    Closed round-trips: SELL rows for strategy (exit ts = watermark for train labels).
    Matches compute_closed_trade_pnls / train_game5m_catboost sampling.
    """
    try:
        with engine.connect() as conn:
            if since is not None:
                n = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM trade_history
                        WHERE UPPER(TRIM(strategy_name)) = :s
                          AND UPPER(TRIM(side)) = 'SELL'
                          AND ts >= :since
                        """
                    ),
                    {"s": strategy.strip().upper(), "since": since},
                ).scalar()
            else:
                n = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM trade_history
                        WHERE UPPER(TRIM(strategy_name)) = :s
                          AND UPPER(TRIM(side)) = 'SELL'
                        """
                    ),
                    {"s": strategy.strip().upper()},
                ).scalar()
            return int(n or 0)
    except Exception:
        return 0


def count_earnings_pending_scenario_apply(engine: Engine, *, dataset_version: str = "v0_expanded_baseline") -> int:
    """KB events with non-empty LLM scenario_hints not yet applied to ERD (llm_scenario_v0)."""
    try:
        with engine.connect() as conn:
            n = conn.execute(
                text(
                    """
                    SELECT COUNT(DISTINCT kb.id)
                    FROM earnings_event_detail ed
                    JOIN knowledge_base kb ON kb.id = ed.knowledge_base_id
                    WHERE jsonb_array_length(
                            COALESCE(ed.guidance_summary->'scenario_hints', '[]'::jsonb)
                          ) > 0
                      AND NOT EXISTS (
                        SELECT 1
                        FROM event_reaction_dataset erd
                        WHERE erd.knowledge_base_id = kb.id
                          AND erd.dataset_version = :dv
                          AND erd.label_source = 'llm_scenario_v0'
                      )
                    """
                ),
                {"dv": dataset_version},
            ).scalar()
            return int(n or 0)
    except Exception:
        return 0


def count_earnings_extractions_since(engine: Engine, since: Optional[datetime]) -> int:
    """New/updated LLM extractions on earnings_event_detail (extraction_meta), drives apply_labels."""
    try:
        clauses = ["ed.guidance_summary ? 'extraction_meta'"]
        params: Dict[str, Any] = {}
        if since is not None:
            clauses.append("ed.updated_at >= :since")
            params["since"] = since
        sql = f"""
            SELECT COUNT(DISTINCT kb.id)
            FROM earnings_event_detail ed
            JOIN knowledge_base kb ON kb.id = ed.knowledge_base_id
            WHERE {' AND '.join(clauses)}
        """
        with engine.connect() as conn:
            n = conn.execute(text(sql), params).scalar()
            return int(n or 0)
    except Exception:
        return 0


def count_earnings_grid_apply_units(engine: Engine, since_apply: Optional[datetime]) -> int:
    """
    Units that should trigger apply-data for earnings_grid.

    Counting only new llm_scenario_v0 ERD rows misses fresh extracts (hints not applied yet).
    """
    llm_new = count_erd_rows_since(engine, since=since_apply, label_source="llm_scenario_v0")
    pending = count_earnings_pending_scenario_apply(engine)
    extracted = count_earnings_extractions_since(engine, since_apply)
    return llm_new + pending + extracted


def count_erd_rows_since(
    engine: Engine,
    *,
    since: Optional[datetime],
    label_source: Optional[str] = None,
    labeled_only: bool = False,
) -> int:
    try:
        clauses = ["1=1"]
        params: Dict[str, Any] = {}
        if since is not None:
            clauses.append("updated_at >= :since")
            params["since"] = since
        if label_source:
            clauses.append("label_source = :ls")
            params["ls"] = label_source
        if labeled_only:
            clauses.append("final_label IS NOT NULL AND TRIM(final_label) != ''")
        sql = f"SELECT COUNT(*) FROM event_reaction_dataset WHERE {' AND '.join(clauses)}"
        with engine.connect() as conn:
            n = conn.execute(text(sql), params).scalar()
            return int(n or 0)
    except Exception:
        return 0


def _multiday_lr_ticker_universe(engine: Engine) -> list[str]:
    """Same universe as multiday_lr refresh (ML_MULTIDAY_LR_TICKERS_SOURCE, default merged)."""
    try:
        from config_loader import get_config_value

        source = (get_config_value("ML_MULTIDAY_LR_TICKERS_SOURCE") or "merged").strip().lower()
    except Exception:
        source = "merged"
    try:
        if source == "game5m":
            from services.ticker_groups import get_tickers_game_5m

            return [str(t).strip().upper() for t in get_tickers_game_5m() if str(t).strip()]
        if source == "config":
            from services.ticker_groups import get_config_ticker_symbols_upper_unique

            return list(get_config_ticker_symbols_upper_unique())
        if source == "merged":
            from services.ticker_groups import get_all_ticker_groups

            from_quotes: list[str] = []
            try:
                with engine.connect() as conn:
                    rows = conn.execute(text("SELECT DISTINCT ticker FROM quotes ORDER BY ticker"))
                    from_quotes = [str(r[0]).strip().upper() for r in rows if r and r[0]]
            except Exception:
                pass
            seen: set[str] = set()
            ordered: list[str] = []
            for t in from_quotes + get_all_ticker_groups():
                u = str(t).strip().upper()
                if u and u not in seen:
                    seen.add(u)
                    ordered.append(u)
            return sorted(ordered)
    except Exception:
        pass
    return []


def count_quotes_daily_rows_since(
    engine: Engine,
    since: Optional[datetime],
    *,
    tickers: Optional[list[str]] = None,
) -> int:
    """New daily quote rows (multiday_lr data_unit)."""
    try:
        clauses = ["1=1"]
        params: Dict[str, Any] = {}
        if since is not None:
            clauses.append("date >= :since_date")
            params["since_date"] = since.date() if hasattr(since, "date") else since
        if tickers:
            upper = [str(t).strip().upper() for t in tickers if str(t).strip()]
            if upper:
                clauses.append("UPPER(TRIM(ticker)) = ANY(:tickers)")
                params["tickers"] = upper
        sql = f"SELECT COUNT(*) FROM quotes WHERE {' AND '.join(clauses)}"
        with engine.connect() as conn:
            n = conn.execute(text(sql), params).scalar()
            return int(n or 0)
    except Exception:
        return 0


def count_recovery_export_rows_since(engine: Engine, since: Optional[datetime]) -> int:
    """Proxy for new recovery ML rows: TIME_EXIT_EARLY SELL since watermark."""
    try:
        with engine.connect() as conn:
            if since is not None:
                n = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM trade_history
                        WHERE UPPER(TRIM(strategy_name)) = 'GAME_5M'
                          AND UPPER(TRIM(side)) = 'SELL'
                          AND UPPER(TRIM(COALESCE(signal_type, ''))) = 'TIME_EXIT_EARLY'
                          AND ts >= :since
                        """
                    ),
                    {"since": since},
                ).scalar()
            else:
                n = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM trade_history
                        WHERE UPPER(TRIM(strategy_name)) = 'GAME_5M'
                          AND UPPER(TRIM(side)) = 'SELL'
                          AND UPPER(TRIM(COALESCE(signal_type, ''))) = 'TIME_EXIT_EARLY'
                        """
                    ),
                ).scalar()
            return int(n or 0)
    except Exception:
        return 0


def count_gap_forecast_complete_since(engine: Engine, since: Optional[datetime]) -> int:
    """Rows with observed open gap (forecast_row) since watermark."""
    try:
        with engine.connect() as conn:
            if since is not None:
                n = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM game5m_gap_forecast_daily
                        WHERE open_gap_pct IS NOT NULL
                          AND updated_at >= :since
                        """
                    ),
                    {"since": since},
                ).scalar()
            else:
                n = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM game5m_gap_forecast_daily
                        WHERE open_gap_pct IS NOT NULL
                        """
                    ),
                ).scalar()
            return int(n or 0)
    except Exception:
        return 0


def count_open_path_labels_since(engine: Engine, since: Optional[datetime]) -> int:
    try:
        with engine.connect() as conn:
            if since is not None:
                n = conn.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM game5m_open_path_labels
                        WHERE created_at >= :since OR updated_at >= :since
                        """
                    ),
                    {"since": since},
                ).scalar()
            else:
                n = conn.execute(text("SELECT COUNT(*) FROM game5m_open_path_labels")).scalar()
            return int(n or 0)
    except Exception:
        return 0


def count_deltas_for_contour(
    engine: Engine,
    spec: MlContourSpec,
    refresh_log: Dict[str, Any],
) -> Dict[str, Optional[int]]:
    since_apply = _since_ts(refresh_log, "last_apply_at_utc")
    since_train = _since_ts(refresh_log, "last_train_at_utc")
    cid = spec.contour_id

    if cid == "game5m_entry":
        return {
            "new_units_apply": count_strategy_closed_since(engine, "GAME_5M", since_apply),
            "new_units_train": count_strategy_closed_since(engine, "GAME_5M", since_train),
        }
    if cid == "game5m_entry_bar_v2":
        tickers = _multiday_lr_ticker_universe(engine)
        return {
            "new_units_apply": count_quotes_daily_rows_since(engine, since_apply, tickers=tickers or None),
            "new_units_train": count_quotes_daily_rows_since(engine, since_train, tickers=tickers or None),
        }
    if cid == "portfolio":
        return {
            "new_units_apply": count_strategy_closed_since(engine, "PORTFOLIO", since_apply),
            "new_units_train": count_strategy_closed_since(engine, "PORTFOLIO", since_train),
        }
    if cid == "event_reaction_regression":
        return {
            "new_units_apply": count_erd_rows_since(engine, since=since_apply, labeled_only=True),
            "new_units_train": count_erd_rows_since(engine, since=since_train, labeled_only=True),
        }
    if cid == "earnings_grid":
        return {
            "new_units_apply": count_earnings_grid_apply_units(engine, since_apply),
            "new_units_train": count_erd_rows_since(
                engine, since=since_train, label_source="llm_scenario_v0"
            ),
        }
    if cid == "open_path":
        return {
            "new_units_apply": count_open_path_labels_since(engine, since_apply),
            "new_units_train": count_open_path_labels_since(engine, since_train),
        }
    if cid == "multiday_lr":
        tickers = _multiday_lr_ticker_universe(engine)
        return {
            "new_units_apply": count_quotes_daily_rows_since(engine, since_apply, tickers=tickers or None),
            "new_units_train": count_quotes_daily_rows_since(engine, since_train, tickers=tickers or None),
        }
    if cid == "recovery":
        return {
            "new_units_apply": count_recovery_export_rows_since(engine, since_apply),
            "new_units_train": count_recovery_export_rows_since(engine, since_train),
        }
    if cid == "gap_forecast":
        n_apply = count_gap_forecast_complete_since(engine, since_apply)
        n_train = count_gap_forecast_complete_since(engine, since_train)
        return {"new_units_apply": n_apply, "new_units_train": n_train}
    return {"new_units_apply": None, "new_units_train": None}


def resolve_readiness_gates(
    project_root: Path,
    spec: MlContourSpec,
    engine: Optional[Engine] = None,
) -> Dict[str, bool]:
    cid = spec.contour_id
    out = {"product_ready": False, "dataset_ready": False, "train_ready": False}

    if cid == "open_path":
        from services.open_path_readiness import default_readiness_metrics_path

        raw = _json_load(default_readiness_metrics_path(project_root)) or {}
        gates = raw.get("gates") if isinstance(raw.get("gates"), dict) else {}
        out["product_ready"] = bool(gates.get("overall_open_path_classifier_ready"))
        out["dataset_ready"] = bool((gates.get("open_path_classifier_dataset") or {}).get("ready"))
        out["train_ready"] = bool(gates.get("overall_open_path_classifier_model_ready"))
        return out

    if cid == "earnings_grid":
        from services.earnings_intelligence_readiness import default_readiness_metrics_path

        raw = _json_load(default_readiness_metrics_path(project_root)) or {}
        gates = raw.get("gates") if isinstance(raw.get("gates"), dict) else {}
        out["product_ready"] = bool(gates.get("overall_earnings_autoprep_ready"))
        out["dataset_ready"] = bool((gates.get("scenario_classifier_dataset") or {}).get("ready"))
        out["train_ready"] = bool(gates.get("overall_scenario_classifier_ready"))
        return out

    row = _latest_ml_train_readiness(project_root)
    block_key = {
        "game5m_entry": "game5m",
        "game5m_entry_bar_v2": "entry_bar_v2",
        "portfolio": "portfolio",
        "event_reaction_regression": "event_reaction",
    }.get(cid)
    if block_key:
        block = row.get(block_key) if isinstance(row.get(block_key), dict) else {}
        gate = block.get("gate") if isinstance(block.get("gate"), dict) else {}
        metrics = block.get("metrics") if isinstance(block.get("metrics"), dict) else {}
        ready = bool(gate.get("ready"))
        out["train_ready"] = ready
        out["product_ready"] = ready
        n_train = int(metrics.get("n_train") or 0)
        st = metrics.get("status")
        out["dataset_ready"] = n_train > 0 or st == "ok"
        return out

    q_dir = default_ml_data_quality_dir(project_root)
    if cid == "multiday_lr":
        raw = _json_load(q_dir / spec.train_metrics_relpath)
        rows = raw if isinstance(raw, list) else []
        n_fitted = len(rows)
        out["dataset_ready"] = n_fitted > 0
        out["train_ready"] = n_fitted >= 3
        return out

    if cid == "recovery":
        raw = _json_load(q_dir / spec.train_metrics_relpath) or {}
        n_train = int(raw.get("n_train") or raw.get("n_total") or 0)
        out["dataset_ready"] = n_train > 0
        auc = raw.get("auc_valid")
        out["train_ready"] = n_train >= 50 and auc is not None
        return out

    if cid == "gap_forecast":
        raw = _json_load(q_dir / spec.train_metrics_relpath) or {}
        n_complete = int(raw.get("n_complete") or 0)
        out["dataset_ready"] = n_complete >= 12
        out["train_ready"] = n_complete >= 30
        return out

    if cid == "game5m_entry_bar_v2":
        raw = _json_load(q_dir / spec.train_metrics_relpath) or {}
        n_valid = int(raw.get("n_valid") or 0)
        n_total = int(raw.get("n_total") or 0)
        auc = raw.get("auc_valid")
        out["dataset_ready"] = n_total >= 5000
        out["train_ready"] = n_valid >= 80 and auc is not None
        out["product_ready"] = False
        return out

    return out


def build_delta_resolver(project_root: Path):
    def _resolver(spec: MlContourSpec) -> Dict[str, Optional[int]]:
        from report_generator import get_engine

        prev = load_refresh_log(project_root, spec)
        try:
            engine = get_engine()
        except Exception:
            return {"new_units_apply": None, "new_units_train": None}
        return count_deltas_for_contour(engine, spec, prev)

    return _resolver


def build_readiness_resolver(project_root: Path):
    def _resolver(spec: MlContourSpec) -> Dict[str, bool]:
        return resolve_readiness_gates(project_root, spec)

    return _resolver
