#!/usr/bin/env python3
"""
Autotune v0 для GAME_5M на базе JSON отчёта анализатора.

Цель этапа: эволюционный цикл «снимок → 1 изменение → ждать окно → сравнить → следующее изменение».
Скрипт умеет:
  - взять свежий отчёт (HTTP /api/analyzer или локальный JSON latest.json),
  - выбрать 1 кандидат из auto_config_override.updates,
  - применить (через update_config_key) или только предложить,
  - вести состояние pending/observed в local/autotune_state.json (или ANALYZER_AUTOTUNE_STATE_PATH).

Важно:
  - не делает “ML”, не трогает код; это безопасный оптимизатор порогов с гардрейлами.
  - по умолчанию APPLY выключен (ANALYZER_AUTOTUNE_APPLY=0).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import is_editable_config_env_key, update_config_key  # noqa: E402


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path() -> Path:
    raw = (os.environ.get("ANALYZER_AUTOTUNE_STATE_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return project_root / "local" / "autotune_state.json"


def _load_state() -> Dict[str, Any]:
    p = _state_path()
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_report_http(url: str, *, days: int, strategy: str, timeout_sec: int) -> Dict[str, Any]:
    q = urllib.parse.urlencode(
        {
            "days": str(days),
            "strategy": strategy,
            "use_llm": "0",
            "include_trade_details": "0",
        }
    )
    base = url.strip()
    sep = "&" if "?" in base else "?"
    full = base + sep + q
    req = urllib.request.Request(full, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def _load_report_latest_json() -> Dict[str, Any]:
    snap_dir = (os.environ.get("ANALYZER_SNAPSHOT_DIR") or "").strip()
    if snap_dir:
        p = Path(snap_dir) / "latest.json"
    else:
        p = project_root / "local" / "analyzer_snapshots" / "latest.json"
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _summary(report: Dict[str, Any]) -> Dict[str, Any]:
    s = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    return s if isinstance(s, dict) else {}


def _auto_updates(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    auto = report.get("auto_config_override") if isinstance(report.get("auto_config_override"), dict) else {}
    upd = auto.get("updates")
    return upd if isinstance(upd, list) else []


def _coerce_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _guardrails_ok(row: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Минимальные гардрейлы v0:
      - только editable keys
      - только GAME_5M_* и только “числовые” значения или true/false
      - ограничение “шагов” для самых чувствительных ключей
    """
    key = str(row.get("env_key") or "").strip()
    proposed = str(row.get("proposed") if row.get("proposed") is not None else "").strip()
    if not key or not proposed:
        return False, "empty_key_or_value"
    if not is_editable_config_env_key(key):
        return False, "not_editable"
    if not key.startswith("GAME_5M_"):
        return False, "non_game5m_key"
    # bool allowed
    if proposed.lower() in ("true", "false", "1", "0", "yes", "no"):
        return True, "ok"
    # numeric required for other keys
    pv = _coerce_float(proposed)
    if pv is None:
        return False, "non_numeric_value"
    # Step limits (soft) — if current present
    cur = row.get("current")
    cv = _coerce_float(cur) if cur is not None else None
    if cv is not None:
        delta = pv - cv
        if key == "GAME_5M_TAKE_MOMENTUM_FACTOR" and abs(delta) > 0.1:
            return False, "delta_too_large_take_momentum_factor"
        if key == "GAME_5M_TAKE_PROFIT_PCT" and abs(delta) > 1.0:
            return False, "delta_too_large_take_profit_pct"
        if key.startswith("GAME_5M_TAKE_PROFIT_PCT_") and abs(delta) > 2.0:
            return False, "delta_too_large_take_profit_pct_ticker"
        if key in ("GAME_5M_RSI_STRONG_BUY_MAX", "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX") and abs(delta) > 5:
            return False, "delta_too_large_rsi"
        if key in ("GAME_5M_VOLATILITY_WARN_BUY_MIN", "GAME_5M_VOLATILITY_WAIT_MIN") and abs(delta) > 0.3:
            return False, "delta_too_large_volatility"
    return True, "ok"


def _pick_one_update(
    updates: List[Dict[str, Any]],
    *,
    prefer_prefixes: List[str],
    deny_keys: List[str],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    skipped: List[Dict[str, Any]] = []
    deny = {k.strip().upper() for k in deny_keys if k.strip()}
    # 1) filter/guard
    candidates: List[Dict[str, Any]] = []
    for row in updates:
        if not isinstance(row, dict):
            continue
        k = str(row.get("env_key") or "").strip()
        if k.upper() in deny:
            skipped.append({"env_key": k, "reason": "deny_list"})
            continue
        ok, reason = _guardrails_ok(row)
        if not ok:
            skipped.append({"env_key": k, "reason": reason})
            continue
        candidates.append(row)
    if not candidates:
        return None, skipped
    # 2) preference ordering: first match any prefix (in order), else first
    for pref in prefer_prefixes:
        for row in candidates:
            if str(row.get("env_key") or "").startswith(pref):
                return row, skipped
    return candidates[0], skipped


def _apply_update(key: str, value: str) -> Tuple[bool, str]:
    ok = update_config_key(key, value)
    return (ok, "applied" if ok else "write_failed")


def main() -> None:
    ap = argparse.ArgumentParser(description="Autotune v0: propose/apply one analyzer update with guardrails")
    ap.add_argument("--days", type=int, default=5, help="Окно анализатора (1–30)")
    ap.add_argument("--strategy", type=str, default="GAME_5M", help="Стратегия (по умолчанию GAME_5M)")
    ap.add_argument("--url", type=str, default="", help="URL GET /api/analyzer (если задан — брать отчёт по HTTP)")
    ap.add_argument("--timeout", type=int, default=180, help="HTTP таймаут, сек")
    ap.add_argument("--dry-run", action="store_true", help="Не применять; только вывести решение и обновить state")
    args = ap.parse_args()

    days = max(1, min(30, int(args.days)))
    strategy = (args.strategy or "GAME_5M").strip().upper()

    state = _load_state()
    pending = state.get("pending") if isinstance(state.get("pending"), dict) else None

    # Если есть pending изменение — фиксируем “наблюдение” по summary и не применяем новое.
    report = _fetch_report_http(args.url, days=days, strategy=strategy, timeout_sec=max(30, int(args.timeout))) if args.url.strip() else _load_report_latest_json()
    if not report:
        print(json.dumps({"ok": False, "error": "no_report"}, ensure_ascii=False, indent=2))
        sys.exit(2)
    summ = _summary(report)
    total = int(summ.get("total") or 0) if isinstance(summ.get("total"), (int, float, str)) else 0

    if pending:
        pending.setdefault("observations", [])
        pending["observations"].append({"at_utc": _utc_now(), "summary": summ})
        # закрываем pending только когда накопилось достаточно новых сделок или прошло N часов
        min_trades = int((os.environ.get("ANALYZER_AUTOTUNE_MIN_TRADES", "8") or "8").strip() or "8")
        if total >= int(pending.get("baseline_total", 0)) + max(1, min_trades):
            pending["status"] = "ready_for_review"
            pending["ready_at_utc"] = _utc_now()
            state["pending"] = pending
            _save_state(state)
        print(json.dumps({"ok": True, "mode": "observe_pending", "pending": pending}, ensure_ascii=False, indent=2))
        return

    updates = _auto_updates(report)
    deny = (os.environ.get("ANALYZER_AUTOTUNE_DENY_KEYS") or "GAME_5M_SIGNAL_CRON_MINUTES").split(",")
    prefer = (os.environ.get("ANALYZER_AUTOTUNE_PREFER_PREFIXES") or "GAME_5M_TAKE_PROFIT_PCT_,GAME_5M_TAKE_MOMENTUM_FACTOR").split(",")
    picked, skipped = _pick_one_update(updates, prefer_prefixes=[p.strip() for p in prefer if p.strip()], deny_keys=deny)

    apply_on = (os.environ.get("ANALYZER_AUTOTUNE_APPLY", "0") or "0").strip().lower() in ("1", "true", "yes")
    do_apply = apply_on and (not args.dry_run)

    result: Dict[str, Any] = {
        "ok": True,
        "mode": "pick_and_apply" if do_apply else "pick_only",
        "picked": picked,
        "skipped": skipped,
        "summary": summ,
        "applied": None,
        "state_path": str(_state_path()),
    }
    if not picked:
        result["ok"] = False
        result["error"] = "no_eligible_updates"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    key = str(picked.get("env_key") or "").strip()
    val = str(picked.get("proposed") if picked.get("proposed") is not None else "").strip()
    applied = {"env_key": key, "proposed": val, "status": "not_applied", "reason": "apply_disabled_or_dry_run"}
    if do_apply:
        ok, reason = _apply_update(key, val)
        applied = {"env_key": key, "proposed": val, "status": "applied" if ok else "failed", "reason": reason}

    result["applied"] = applied

    # Записать pending для следующего наблюдения
    state["pending"] = {
        "status": "pending_effect",
        "created_at_utc": _utc_now(),
        "baseline_total": total,
        "baseline_summary": summ,
        "applied": applied,
        "source": {
            "days": days,
            "strategy": strategy,
            "report_meta": report.get("meta"),
        },
        "observations": [],
    }
    _save_state(state)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

