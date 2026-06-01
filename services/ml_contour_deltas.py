"""DB/file deltas and readiness gates for unified ML contour refresh."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.ml_contour_refresh import MlContourSpec, load_refresh_log


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
            "new_units_apply": count_strategy_buys_since(engine, "GAME_5M", since_apply),
            "new_units_train": count_strategy_buys_since(engine, "GAME_5M", since_train),
        }
    if cid == "portfolio":
        return {
            "new_units_apply": count_strategy_buys_since(engine, "PORTFOLIO", since_apply),
            "new_units_train": count_strategy_buys_since(engine, "PORTFOLIO", since_train),
        }
    if cid == "event_reaction_regression":
        return {
            "new_units_apply": count_erd_rows_since(engine, since=since_apply, labeled_only=True),
            "new_units_train": count_erd_rows_since(engine, since=since_train, labeled_only=True),
        }
    if cid == "earnings_grid":
        return {
            "new_units_apply": count_erd_rows_since(
                engine, since=since_apply, label_source="llm_scenario_v0"
            ),
            "new_units_train": count_erd_rows_since(
                engine, since=since_train, label_source="llm_scenario_v0"
            ),
        }
    if cid == "open_path":
        return {
            "new_units_apply": count_open_path_labels_since(engine, since_apply),
            "new_units_train": count_open_path_labels_since(engine, since_train),
        }
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
