"""Live ML runtime probes + trade telemetry for readiness / progress blockers."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_PROBE_TICKER = "MU"

_ENTRY_CONTOURS: tuple[dict[str, Any], ...] = (
    {
        "contour_id": "catboost_entry_bar_v2",
        "surface": "GAME_5M",
        "role": "entry shadow",
        "status_field": "catboost_bar_v2_signal_status",
        "proba_field": "catboost_entry_proba_good_v2",
        "enable_config": "GAME_5M_CATBOOST_BAR_V2_LOG_ENABLED",
        "promotion_gate": "AUC≥0.545 + ≥10 BUY telemetry",
    },
    {
        "contour_id": "catboost_entry_e3",
        "surface": "GAME_5M",
        "role": "entry shadow E3",
        "status_field": "entry_e3_signal_status",
        "proba_field": "catboost_entry_proba_good_e3",
        "enable_config": "GAME_5M_ENTRY_E3_LOG_ENABLED",
        "promotion_gate": "deferred; AUC~0.61 offline",
    },
    {
        "contour_id": "catboost_entry_v1",
        "surface": "GAME_5M",
        "role": "entry fusion (trade)",
        "status_field": "catboost_signal_status",
        "proba_field": "catboost_entry_proba_good",
        "enable_config": "GAME_5M_CATBOOST_ENABLED",
        "promotion_gate": "AUC≥0.55; currently disabled",
    },
)

_EXIT_CONTOURS: tuple[dict[str, Any], ...] = (
    {
        "contour_id": "continuation_ml",
        "surface": "GAME_5M",
        "role": "TAKE shadow",
        "status_json_path": ("continuation_ml", "status"),
        "proba_json_path": ("continuation_ml", "continuation_proba"),
        "enable_config": "GAME_5M_CONTINUATION_ML_ENABLED",
        "promotion_gate": "≥50 TAKE rows + AUC gate",
    },
    {
        "contour_id": "hold_quality_h3",
        "surface": "GAME_5M",
        "role": "SELL shadow",
        "status_json_path": ("hold_quality_ml", "status"),
        "proba_json_path": ("hold_quality_ml", "hold_quality_proba"),
        "enable_config": "GAME_5M_HOLD_QUALITY_LOG_ENABLED",
        "promotion_gate": "exit bake-off",
    },
    {
        "contour_id": "recovery_ml_d4a",
        "surface": "GAME_5M",
        "role": "TIME_EXIT_EARLY telemetry",
        "status_json_path": ("recovery_ml_time_exit_early", "status"),
        "proba_json_path": ("recovery_ml_time_exit_early", "recovery_proba"),
        "enable_config": "GAME_5M_RECOVERY_ML_ENABLED",
        "promotion_gate": "D4b defer review",
    },
)

_ERROR_HINTS: dict[str, str] = {
    "feature_mismatch": (
        "Схема признаков runtime ≠ meta.json модели (типично: tech vs full). "
        "Переобучить модель или выровнять build_*_feature_row с meta.feature_names."
    ),
    "disabled": "Контур выключен в config.env (см. enable_config).",
    "no_model_file": "Нет .cbm на диске — дождитесь nightly/weekly train refresh.",
    "load_error": "Модель/meta.json не читается — проверить путь и JSON.",
    "no_package": "catboost не установлен в контейнере.",
    "predict_error": "Исключение при predict — смотреть логи cron.",
    "bad_meta": "meta.json без feature_names.",
    "skipped": "attach не вызывался или payload пустой.",
}


def _cfg_enabled(key: str, *, default: str = "true") -> bool:
    from config_loader import get_config_value

    raw = (get_config_value(key, default) or default).strip().lower()
    return raw in ("1", "true", "yes")


def _json_get(obj: Any, path: tuple[str, ...]) -> Any:
    cur = obj
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _sample_entry_payload() -> dict[str, Any]:
    return {
        "price": 100.0,
        "high_5d": 110.0,
        "low_5d": 90.0,
        "rsi_5m": 40.0,
        "momentum_2h_pct": 1.0,
        "momentum_rth_today_pct": 0.5,
        "volatility_5m_pct": 0.4,
        "pullback_from_high_pct": 2.0,
        "bars_count": 100,
        "momentum_rth_today_bars": 8,
        "prob_up": 0.55,
        "prob_down": 0.45,
        "macro_risk_level": "medium",
        "ndx_gap_pct": 0.1,
        "spy_gap_pct": 0.05,
        "premarket_gap_pct": 0.2,
        "market_session": {"now_et": "2026-06-26 10:00:00"},
        "decision_5m_bar_open_et": "2026-06-26 10:00:00",
    }


def _probe_entry_contour(contour_id: str, ticker: str, payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if contour_id == "catboost_entry_bar_v2":
        from services.catboost_5m_signal import attach_catboost_bar_v2_signal

        attach_catboost_bar_v2_signal(out, ticker)
        spec = _ENTRY_CONTOURS[0]
    elif contour_id == "catboost_entry_e3":
        from services.game5m_entry_e3_signal import attach_entry_e3_signal

        attach_entry_e3_signal(out, ticker)
        spec = _ENTRY_CONTOURS[1]
    elif contour_id == "catboost_entry_v1":
        from services.catboost_5m_signal import attach_catboost_signal

        attach_catboost_signal(out, ticker)
        spec = _ENTRY_CONTOURS[2]
    else:
        return {"contour_id": contour_id, "live_probe_status": "unknown_contour"}

    st = out.get(spec["status_field"])
    proba = out.get(spec["proba_field"])
    note = out.get(f"{spec['status_field'].replace('_status', '_note')}") or out.get("catboost_signal_note")
    row: dict[str, Any] = {
        "contour_id": contour_id,
        "live_probe_status": st,
        "live_probe_proba": proba,
        "live_probe_note": (str(note)[:240] if note else None),
        "config_enabled": _cfg_enabled(spec["enable_config"], default="false" if contour_id == "catboost_entry_v1" else "true"),
    }
    if st == "feature_mismatch":
        row["schema_diagnosis"] = _diagnose_feature_mismatch(contour_id)
    return row


def _diagnose_feature_mismatch(contour_id: str) -> dict[str, Any] | None:
    try:
        if contour_id == "catboost_entry_bar_v2":
            from services.catboost_5m_signal import (
                _default_bar_v2_model_path,
                _load_model_bundle,
                build_catboost_bar_v2_feature_row,
            )
            from services.game5m_entry_bar_dataset import resolve_bar_v2_feature_mode

            mp = _default_bar_v2_model_path()
            if not Path(mp).is_file():
                return None
            _model, meta = _load_model_bundle(mp, Path(mp).stat().st_mtime)
            mode = resolve_bar_v2_feature_mode(meta)
            colnames, _ = build_catboost_bar_v2_feature_row(_PROBE_TICKER, _sample_entry_payload(), mode=mode)
            expected = list(meta.get("feature_names") or [])
            return {
                "expected_n": len(expected),
                "runtime_n": len(colnames),
                "match": expected == colnames,
                "expected_head": expected[:6],
                "runtime_head": colnames[:6],
                "resolved_mode": mode,
            }
        if contour_id == "catboost_entry_e3":
            from services.game5m_entry_e3_signal import _default_e3_model_path, build_entry_e3_feature_row
            from services.catboost_5m_signal import _load_model_bundle

            mp = _default_e3_model_path()
            if not Path(mp).is_file():
                return None
            _model, meta = _load_model_bundle(mp, Path(mp).stat().st_mtime)
            colnames, _ = build_entry_e3_feature_row(_PROBE_TICKER, _sample_entry_payload())
            expected = list(meta.get("feature_names") or [])
            return {
                "expected_n": len(expected),
                "runtime_n": len(colnames),
                "match": expected == colnames,
            }
    except Exception as e:
        return {"error": str(e)}
    return None


def probe_all_entry_shadow_contours(*, ticker: str = _PROBE_TICKER) -> list[dict[str, Any]]:
    payload = _sample_entry_payload()
    return [_probe_entry_contour(spec["contour_id"], ticker, payload) for spec in _ENTRY_CONTOURS]


def _aggregate_entry_telemetry(
    engine: Engine,
    *,
    strategy: str,
    days: int,
) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(1, int(days)))
    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for spec in _ENTRY_CONTOURS:
            st_field = spec["status_field"]
            prob_field = spec["proba_field"]
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                      COALESCE(NULLIF(TRIM(context_json->>'{st_field}'), ''), '(missing)') AS status,
                      COUNT(*) AS n,
                      COUNT(*) FILTER (
                        WHERE context_json->>'{prob_field}' IS NOT NULL
                          AND context_json->>'{prob_field}' <> ''
                      ) AS with_proba
                    FROM trade_history
                    WHERE strategy_name = :strat
                      AND side = 'BUY'
                      AND ts >= :since
                    GROUP BY 1
                    ORDER BY n DESC
                    """
                ),
                {"strat": strategy, "since": since},
            ).mappings().all()
            total = sum(int(r["n"]) for r in rows)
            ok_n = sum(int(r["n"]) for r in rows if r["status"] == "ok")
            mismatch_n = sum(int(r["n"]) for r in rows if r["status"] == "feature_mismatch")
            missing_n = sum(int(r["n"]) for r in rows if r["status"] == "(missing)")
            with_proba = sum(int(r["with_proba"]) for r in rows)
            blockers = _telemetry_blockers(
                contour_id=spec["contour_id"],
                config_enabled=_cfg_enabled(
                    spec["enable_config"],
                    default="false" if spec["contour_id"] == "catboost_entry_v1" else "true",
                ),
                total=total,
                ok_n=ok_n,
                mismatch_n=mismatch_n,
                missing_n=missing_n,
                with_proba=with_proba,
                status_rows=[dict(r) for r in rows],
            )
            out.append(
                {
                    **spec,
                    "window_days": days,
                    "buys_total": total,
                    "status_counts": {str(r["status"]): int(r["n"]) for r in rows},
                    "with_proba": with_proba,
                    "ok_rate": round(ok_n / total, 4) if total else None,
                    "progress_blockers": blockers,
                }
            )
    return out


def _aggregate_exit_telemetry(
    engine: Engine,
    *,
    strategy: str,
    days: int,
) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(1, int(days)))
    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        sell_total = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM trade_history
                WHERE strategy_name = :strat AND side = 'SELL' AND ts >= :since
                """
            ),
            {"strat": strategy, "since": since},
        ).scalar()
        for spec in _EXIT_CONTOURS:
            root = spec["status_json_path"][0]
            status_key = spec["status_json_path"][1]
            prob_key = spec["proba_json_path"][1]
            rows = conn.execute(
                text(
                    f"""
                    SELECT
                      COALESCE(NULLIF(TRIM(context_json->'{root}'->>'{status_key}'), ''), '(missing)') AS status,
                      COUNT(*) AS n
                    FROM trade_history
                    WHERE strategy_name = :strat
                      AND side = 'SELL'
                      AND ts >= :since
                      AND context_json ? '{root}'
                    GROUP BY 1
                    ORDER BY n DESC
                    """
                ),
                {"strat": strategy, "since": since},
            ).mappings().all()
            present = sum(int(r["n"]) for r in rows)
            ok_n = sum(int(r["n"]) for r in rows if r["status"] == "ok")
            mismatch_n = sum(int(r["n"]) for r in rows if r["status"] == "feature_mismatch")
            with_proba = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*) FROM trade_history
                    WHERE strategy_name = :strat AND side = 'SELL' AND ts >= :since
                      AND context_json->'{root}'->>'{prob_key}' IS NOT NULL
                      AND context_json->'{root}'->>'{prob_key}' <> ''
                    """
                ),
                {"strat": strategy, "since": since},
            ).scalar()
            blockers = _telemetry_blockers(
                contour_id=spec["contour_id"],
                config_enabled=_cfg_enabled(spec["enable_config"], default="true"),
                total=int(sell_total or 0),
                ok_n=ok_n,
                mismatch_n=mismatch_n,
                missing_n=int(sell_total or 0) - present,
                with_proba=int(with_proba or 0),
                status_rows=[dict(r) for r in rows],
                present_n=present,
            )
            out.append(
                {
                    **spec,
                    "window_days": days,
                    "sells_total": int(sell_total or 0),
                    "rows_with_block": present,
                    "status_counts": {str(r["status"]): int(r["n"]) for r in rows},
                    "with_proba": int(with_proba or 0),
                    "ok_rate": round(ok_n / present, 4) if present else None,
                    "progress_blockers": blockers,
                }
            )
    return out


def _telemetry_blockers(
    *,
    contour_id: str,
    config_enabled: bool,
    total: int,
    ok_n: int,
    mismatch_n: int,
    missing_n: int,
    with_proba: int,
    status_rows: list[dict[str, Any]],
    present_n: int | None = None,
) -> list[str]:
    blockers: list[str] = []
    if not config_enabled:
        blockers.append(f"{contour_id}: выключен ({'enable_config'}) — telemetry не ожидается.")
        return blockers
    if total == 0:
        blockers.append(f"{contour_id}: нет сделок в окне — прогресс telemetry невозможен.")
        return blockers
    if mismatch_n > 0:
        hint = _ERROR_HINTS.get("feature_mismatch", "")
        blockers.append(
            f"{contour_id}: feature_mismatch на {mismatch_n} сделках — telemetry бесполезна. {hint}"
        )
    if present_n is not None and present_n == 0:
        blockers.append(
            f"{contour_id}: блок JSON ни разу не записан в SELL context ({present_n}/{total}) — "
            "проверить cron path / log_only gate."
        )
    elif missing_n > 0 and ok_n == 0 and mismatch_n == 0:
        blockers.append(
            f"{contour_id}: status отсутствует на {missing_n} BUY — код до attach не доходит или старые сделки."
        )
    if ok_n == 0 and mismatch_n == 0 and with_proba == 0 and (present_n is None or present_n > 0):
        top = status_rows[0]["status"] if status_rows else "?"
        blockers.append(
            f"{contour_id}: нет ни одного ok/proba (доминирует status={top!r}) — см. live_probe."
        )
    elif ok_n > 0 and with_proba < ok_n:
        blockers.append(f"{contour_id}: ok={ok_n}, но proba заполнена только {with_proba} раз.")
    if not blockers and ok_n > 0:
        if contour_id == "catboost_entry_bar_v2" and ok_n < 10:
            blockers.append(
                f"{contour_id}: telemetry работает, но мало BUY с ok ({ok_n}<10) для promotion review."
            )
        elif contour_id == "continuation_ml" and ok_n < 8:
            blockers.append(
                f"{contour_id}: telemetry ok, но мало TAKE rows ({ok_n}<8) для apply gate."
            )
    return blockers


def _scan_ml_quality_artifacts(project_root: Path | None = None) -> list[dict[str, Any]]:
    root = project_root or Path(__file__).resolve().parents[1]
    q = Path("/app/logs/ml/ml_data_quality") if Path("/app/logs").exists() else root / "local" / "logs" / "ml_data_quality"
    issues: list[dict[str, Any]] = []
    if not q.is_dir():
        return [{"kind": "missing_dir", "path": str(q)}]
    for p in sorted(q.glob("last_*_train_metrics.json")):
        data = json.loads(p.read_text(encoding="utf-8")) if p.stat().st_size < 2_000_000 else {}
        if not isinstance(data, dict):
            continue
        auc = data.get("auc_valid")
        gate_ready = data.get("gate_ready")
        if auc is not None and isinstance(auc, (int, float)) and float(auc) < 0.52:
            issues.append(
                {
                    "artifact": p.name,
                    "issue": "low_auc_valid",
                    "auc_valid": auc,
                    "hint": "Модель слабее монетки на valid — apply не рекомендуется.",
                }
            )
        if gate_ready is False:
            issues.append({"artifact": p.name, "issue": "gate_not_ready", "hint": data.get("gate_reasons") or data.get("reasons")})
    return issues


def build_ml_runtime_readiness_diagnostics(
    engine: Engine | None = None,
    *,
    strategy: str = "GAME_5M",
    days: int = 21,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """
    Live probes + trade_history telemetry + artifact scan.
    Explains why shadow/apply progress stalled (feature_mismatch, missing rows, low n).
    """
    live_probes = probe_all_entry_shadow_contours()
    entry_telemetry: list[dict[str, Any]] = []
    exit_telemetry: list[dict[str, Any]] = []
    if engine is not None:
        try:
            entry_telemetry = _aggregate_entry_telemetry(engine, strategy=strategy, days=days)
            exit_telemetry = _aggregate_exit_telemetry(engine, strategy=strategy, days=days)
        except Exception as e:
            logger.warning("ml_runtime_readiness telemetry: %s", e)
            entry_telemetry = [{"error": str(e)}]
    artifact_issues = _scan_ml_quality_artifacts(project_root)

    priority_blockers: list[str] = []
    for block in entry_telemetry + exit_telemetry:
        if isinstance(block, dict):
            priority_blockers.extend(block.get("progress_blockers") or [])

    for probe in live_probes:
        st = probe.get("live_probe_status")
        if st and st != "ok":
            hint = _ERROR_HINTS.get(str(st), "")
            priority_blockers.append(
                f"live_probe {probe.get('contour_id')}: {st}" + (f" — {hint}" if hint else "")
            )

    # dedupe preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for b in priority_blockers:
        if b not in seen:
            seen.add(b)
            deduped.append(b)

    overall = "healthy"
    if any("feature_mismatch" in b for b in deduped):
        overall = "blocked_schema"
    elif any("нет ни одного ok" in b or "ни разу не записан" in b for b in deduped):
        overall = "blocked_telemetry"
    elif deduped:
        overall = "collecting"

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "window_days": days,
        "overall_runtime_health": overall,
        "priority_blockers": deduped[:12],
        "live_entry_probes": live_probes,
        "entry_telemetry": entry_telemetry,
        "exit_telemetry": exit_telemetry,
        "train_metrics_artifact_flags": artifact_issues,
        "error_hints": _ERROR_HINTS,
        "ops_note_ru": (
            "Сравните live_probe (сейчас) с entry_telemetry (история BUY). "
            "Если live ok, а в истории feature_mismatch — фикс уже на новых сделках; старые не пересчитываются."
        ),
    }


__all__ = [
    "build_ml_runtime_readiness_diagnostics",
    "probe_all_entry_shadow_contours",
]
