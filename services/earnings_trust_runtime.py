"""Runtime earnings post-mortem + trust arbiter context for decision_stack (phase D shadow)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from services.decision_stack._types import gate_mode, stack_readiness, trust_score_for_contour, weight_for_readiness
from services.earnings_event_postmortem import (
    default_postmortem_rows_path,
    default_trust_metrics_path,
    load_postmortem_rows,
    load_trust_metrics,
)
from services.unified_trust_arbiter import default_trust_arbiter_path, trust_label_from_score


def _cfg_int(key: str, default: int) -> int:
    from config_loader import get_config_value

    try:
        return int((get_config_value(key) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _load_arbiter(project_root: Path | None = None) -> dict[str, Any]:
    path = default_trust_arbiter_path(project_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _contour_trust(arbiter: dict[str, Any], contour_id: str) -> dict[str, Any]:
    for surface in (arbiter.get("surfaces") or {}).values():
        if not isinstance(surface, dict):
            continue
        for c in surface.get("contours") or []:
            if isinstance(c, dict) and c.get("contour_id") == contour_id:
                return c
    return {}


def _slice_t_hit(
    earnings_trust: dict[str, Any],
    *,
    scenario_class: str | None,
    alignment: str | None,
) -> float | None:
    by_sc = earnings_trust.get("by_scenario_class") if isinstance(earnings_trust.get("by_scenario_class"), dict) else {}
    by_al = earnings_trust.get("by_alignment") if isinstance(earnings_trust.get("by_alignment"), dict) else {}
    if scenario_class and scenario_class in by_sc:
        v = (by_sc[scenario_class] or {}).get("T_hit")
        if v is not None:
            return float(v)
    if alignment and alignment in by_al:
        v = (by_al[alignment] or {}).get("T_hit")
        if v is not None:
            return float(v)
    return None


def find_postmortem_for_ticker(
    ticker: str,
    *,
    project_root: Path | None = None,
    max_age_days: int | None = None,
) -> dict[str, Any] | None:
    """
    Latest matured post-mortem where ticker is source or spillover peer.
    Returns row augmented with runtime_role: source | peer.
    """
    sym = str(ticker or "").strip().upper()
    if not sym:
        return None
    window = max_age_days if max_age_days is not None else _cfg_int("EARNINGS_TRUST_RUNTIME_MAX_AGE_DAYS", 21)
    cutoff = date.today() - timedelta(days=max(1, window))
    rows = load_postmortem_rows(project_root)
    source_best: dict[str, Any] | None = None
    peer_best: tuple[dict[str, Any], dict[str, Any]] | None = None

    def _ev_d(row: dict[str, Any]) -> date | None:
        try:
            return date.fromisoformat(str(row.get("event_date") or "")[:10])
        except ValueError:
            return None

    for row in rows:
        ev_d = _ev_d(row)
        if ev_d is None or ev_d < cutoff:
            continue
        if str(row.get("symbol") or "").upper() == sym:
            if source_best is None or _ev_d(source_best) is None or ev_d > _ev_d(source_best):
                source_best = dict(row)
        for peer in (row.get("models") or {}).get("peer_spillover") or []:
            if not isinstance(peer, dict):
                continue
            if str(peer.get("peer") or "").upper() != sym:
                continue
            if peer_best is None or ev_d > _ev_d(peer_best[0]):
                peer_best = (dict(row), dict(peer))

    if source_best is not None:
        out = source_best
        out["runtime_role"] = "source"
        return out
    if peer_best is not None:
        row, peer = peer_best
        out = dict(row)
        out["runtime_role"] = "peer"
        out["runtime_peer_block"] = peer
        return out
    return None


def _strength_from_postmortem(
    row: dict[str, Any],
    *,
    earnings_trust: dict[str, Any],
    arbiter: dict[str, Any],
) -> tuple[float, bool, str]:
    """Return (strength, would_downgrade, detail_ru)."""
    role = str(row.get("runtime_role") or "source")
    models = row.get("models") if isinstance(row.get("models"), dict) else {}
    fusion = row.get("fusion") if isinstance(row.get("fusion"), dict) else {}
    fusion_out = row.get("fusion_outcome") if isinstance(row.get("fusion_outcome"), dict) else {}
    ctx = row.get("context") if isinstance(row.get("context"), dict) else {}

    scen_trust = _contour_trust(arbiter, "earnings_scenario")
    peer_trust = _contour_trust(arbiter, "peer_spillover")
    scen_label = str(scen_trust.get("trust_label") or "insufficient")
    peer_label = str(peer_trust.get("trust_label") or "insufficient")

    slice_hit = _slice_t_hit(
        earnings_trust,
        scenario_class=str(ctx.get("scenario_class") or "") or None,
        alignment=str(ctx.get("alignment") or fusion.get("alignment") or "") or None,
    )

    if role == "peer":
        peer_block = row.get("runtime_peer_block") if isinstance(row.get("runtime_peer_block"), dict) else {}
        pred = peer_block.get("pred")
        try:
            pred_f = float(pred) if pred is not None else 0.0
        except (TypeError, ValueError):
            pred_f = 0.0
        t_peer = float(peer_trust.get("trust_score") or 0.35)
        if peer_label in ("insufficient", "low"):
            return 0.0, False, f"peer spillover trust {peer_label} — только telemetry"
        sign = 1.0 if pred_f > 0 else (-1.0 if pred_f < 0 else 0.0)
        strength = max(-0.5, min(0.5, sign * 0.35 * t_peer))
        if slice_hit is not None and slice_hit < 0.45:
            strength *= 0.5
        src = str(row.get("symbol") or "?")
        detail = f"peer {row.get('symbol')}→{peer_block.get('peer')}: pred 5d {pred_f:+.4f}, trust {peer_label}"
        would_down = strength < -0.15 and peer_label in ("medium", "high")
        return strength, would_down, detail

    scen = models.get("scenario_sign") or {}
    reg = models.get("regression_5d") or {}
    sym = str(row.get("symbol") or "?")
    ev_d = str(row.get("event_date") or "")[:10]
    fact = reg.get("fact")
    fact_note = f" fact5d {100.0 * float(fact):+.1f}%" if fact is not None else ""

    if fusion.get("would_have_blocked") and fusion_out.get("fact_was_bad"):
        return -0.45, True, f"{sym} {ev_d}: fusion block оправдан{fact_note}"

    if scen.get("hit") is False:
        strength = -0.3
        if scen_label in ("medium", "high") and slice_hit is not None and slice_hit < 0.5:
            strength = -0.4
        return strength, True, f"{sym} {ev_d}: scenario sign промах{fact_note}"

    if scen.get("hit") is True and not fusion.get("would_have_blocked"):
        strength = 0.15 * float(scen_trust.get("trust_score") or 0.35)
        return strength, False, f"{sym} {ev_d}: scenario sign ok{fact_note}"

    if fusion.get("would_have_blocked"):
        return -0.2, True, f"{sym} {ev_d}: fusion low conv / conflict{fact_note}"

    return 0.0, False, f"{sym} {ev_d}: post-mortem без явного сигнала{fact_note}"


def build_earnings_trust_runtime(
    ticker: str,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Context block for GAME_5M decision_stack earnings_trust contribution."""
    sym = str(ticker or "").strip().upper()
    if not sym:
        return {"active": False, "reason": "empty_ticker"}

    row = find_postmortem_for_ticker(sym, project_root=project_root)
    if row is None:
        return {"active": False, "reason": "no_recent_postmortem", "ticker": sym}

    earnings_trust = load_trust_metrics(project_root)
    arbiter = _load_arbiter(project_root)
    strength, would_down, detail = _strength_from_postmortem(row, earnings_trust=earnings_trust, arbiter=arbiter)

    degradation = earnings_trust.get("degradation") if isinstance(earnings_trust.get("degradation"), dict) else {}
    if degradation.get("degrading") is True:
        strength *= 0.75
        detail += " · rolling hit деградирует 14d vs 90d"

    scen_contour = _contour_trust(arbiter, "earnings_scenario")
    peer_contour = _contour_trust(arbiter, "peer_spillover")
    role = str(row.get("runtime_role") or "source")

    return {
        "active": True,
        "ticker": sym,
        "runtime_role": role,
        "source_symbol": row.get("symbol"),
        "event_date": row.get("event_date"),
        "strength": round(strength, 4),
        "would_downgrade": bool(would_down),
        "detail_ru": detail,
        "postmortem_version": row.get("postmortem_version"),
        "context": row.get("context"),
        "fusion": row.get("fusion"),
        "fusion_outcome": row.get("fusion_outcome"),
        "trust_labels": {
            "earnings_scenario": scen_contour.get("trust_label"),
            "peer_spillover": peer_contour.get("trust_label"),
        },
        "trust_scores": {
            "earnings_scenario": scen_contour.get("trust_score"),
            "peer_spillover": peer_contour.get("trust_score"),
            "earnings_scenario_multiplier": trust_score_for_contour("earnings_scenario"),
            "peer_spillover_multiplier": trust_score_for_contour("peer_spillover"),
        },
        "rolling_degradation": degradation,
    }


def earnings_trust_gate_mode() -> str:
    return gate_mode("DECISION_STACK_EARNINGS_TRUST_GATE_MODE", "log_only")
