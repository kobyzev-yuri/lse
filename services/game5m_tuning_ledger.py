"""Shared GAME_5M tuning ledger (controller, web API, cron)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from report_generator import compute_closed_trade_pnls, get_engine, load_trade_history


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ledger_path(raw: str = "") -> Path:
    if raw.strip():
        p = Path(raw).expanduser()
        return p if p.is_absolute() else project_root() / p
    env = (os.environ.get("GAME5M_TUNING_LEDGER") or "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else project_root() / p
    return project_root() / "local" / "game5m_tuning_ledger.json"


def load_ledger(raw: str = "") -> Dict[str, Any]:
    p = ledger_path(raw)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_ledger(ledger: Dict[str, Any], raw: str = "") -> Path:
    p = ledger_path(raw)
    p.parent.mkdir(parents=True, exist_ok=True)
    ledger["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def find_proposal(ledger: Dict[str, Any], proposal_id: str) -> Optional[Dict[str, Any]]:
    latest = ledger.get("latest_proposals") if isinstance(ledger.get("latest_proposals"), dict) else {}
    for row in latest.get("proposals") or []:
        if isinstance(row, dict) and str(row.get("proposal_id")) == str(proposal_id):
            return row
    return None


def closed_summary(days: int, *, strategy: str = "GAME_5M") -> Dict[str, Any]:
    closed = compute_closed_trade_pnls(load_trade_history(get_engine(), strategy_name=strategy))
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=max(1, int(days)))
    rows = []
    for t in closed:
        ts = pd.Timestamp(getattr(t, "ts", None))
        if ts.tzinfo is None:
            ts = ts.tz_localize("Europe/Moscow", ambiguous=True).tz_convert("UTC")
        else:
            ts = ts.tz_convert("UTC")
        if ts >= cutoff:
            rows.append(t)
    wins = sum(1 for t in rows if float(getattr(t, "log_return", 0.0) or 0.0) > 0)
    total_lr = sum(float(getattr(t, "log_return", 0.0) or 0.0) for t in rows)
    total_pnl = sum(float(getattr(t, "net_pnl", 0.0) or 0.0) for t in rows)
    return {
        "days": int(days),
        "closed_trades": len(rows),
        "wins": wins,
        "losses": max(0, len(rows) - wins),
        "win_rate_pct": round((wins / len(rows) * 100.0), 2) if rows else None,
        "total_log_return": round(total_lr, 6),
        "avg_log_return": round(total_lr / len(rows), 6) if rows else None,
        "total_net_pnl": round(total_pnl, 2),
        "avg_net_pnl": round(total_pnl / len(rows), 2) if rows else None,
    }
