"""GAME_5M trade post-mortem: per-session tags + rolling tactics for tuning decisions."""

from __future__ import annotations

import json
import time as time_mod
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.game5m_tuning_ledger import load_ledger, save_ledger

POSTMORTEM_VERSION = "game5m_trade_postmortem_v1"

TAG_LABELS = {
    "A": "слабый вход",
    "B": "плохой выход",
    "C": "долгое удержание",
    "D": "данные / цена",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ml_data_quality_dir(project_root: Path | None = None) -> Path:
    root = project_root or _project_root()
    app = Path("/app/logs/ml/ml_data_quality")
    if app.parent.exists():
        return app
    return root / "local" / "logs" / "ml_data_quality"


def last_session_snapshot_path(project_root: Path | None = None) -> Path:
    return ml_data_quality_dir(project_root) / "last_game5m_trade_postmortem.json"


def sessions_jsonl_path(project_root: Path | None = None) -> Path:
    return ml_data_quality_dir(project_root) / "game5m_trade_postmortem_sessions.jsonl"


def tactics_aggregate_path(project_root: Path | None = None) -> Path:
    return ml_data_quality_dir(project_root) / "last_game5m_postmortem_tactics.json"


def _parse_ctx(raw: Any) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _f(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _branch_label(ctx: dict) -> str:
    return str(ctx.get("technical_entry_branch") or ctx.get("entry_branch") or "").strip()


def mfe_mae_from_bars(
    eng,
    ticker: str,
    opened_ts: Any,
    closed_ts: Optional[Any],
    entry_price: float,
) -> Tuple[Optional[float], Optional[float]]:
    if not entry_price or entry_price <= 0:
        return None, None
    try:
        import pandas as pd
        from sqlalchemy import text

        t0 = pd.Timestamp(opened_ts).to_pydatetime()
        t1 = pd.Timestamp(closed_ts).to_pydatetime() if closed_ts is not None else t0 + timedelta(hours=8)
        df = pd.read_sql(
            text(
                """
                SELECT high, low FROM market_bars_5m
                WHERE symbol = :sym AND bar_start_utc >= :t0 AND bar_start_utc <= :t1
                ORDER BY bar_start_utc
                """
            ),
            eng,
            params={"sym": ticker.upper(), "t0": t0, "t1": t1},
        )
        if df.empty:
            return None, None
        hi = float(df["high"].max())
        lo = float(df["low"].min())
        return (
            round((hi / entry_price - 1.0) * 100.0, 2),
            round((lo / entry_price - 1.0) * 100.0, 2),
        )
    except Exception:
        return None, None


def classify_trade(
    *,
    entry_ctx: dict,
    exit_ctx: dict,
    pnl_pct: Optional[float],
    mfe_pct: Optional[float],
    mae_pct: Optional[float],
    exit_type: Optional[str],
    exit_detail: str,
    is_open: bool,
    hold_minutes: Optional[float],
) -> Tuple[List[str], str]:
    tags: List[str] = []
    branch = _branch_label(entry_ctx)
    p_good = _f(entry_ctx.get("catboost_entry_proba_good"))
    mom = _f(entry_ctx.get("momentum_2h_pct"))
    mfe = mfe_pct if mfe_pct is not None else _f(entry_ctx.get("mfe"))
    mae = mae_pct if mae_pct is not None else _f(entry_ctx.get("mae"))
    stored_mfe = _f(entry_ctx.get("mfe"))

    if mfe is not None and stored_mfe is not None and abs(mfe - stored_mfe) > 1.5:
        tags.append("D")

    weak_branch = branch in ("buy_rth_momentum", "buy_news_support") and "strong_buy" not in branch
    if mfe is not None and mfe < 1.0 and (pnl_pct is None or pnl_pct < 0.5):
        tags.append("A")
    if p_good is not None and p_good >= 0.45 and pnl_pct is not None and pnl_pct < 0:
        tags.append("A")
    if weak_branch and mom is not None and mom < 2.0 and (mfe is None or mfe < 1.5):
        tags.append("A")

    if mfe is not None and pnl_pct is not None and mfe >= 1.5 and pnl_pct < mfe - 1.0:
        tags.append("B")

    if exit_detail.startswith("overnight_eod_flat") and pnl_pct is not None and pnl_pct < 0:
        tags.append("C")
    if mae is not None and mae <= -1.5 and (exit_type or "").upper() in ("TIME_EXIT", "TIME_EXIT_EARLY"):
        tags.append("C")
    if is_open and mae is not None and mae < -1.0:
        tags.append("C")

    seen: set[str] = set()
    tags = [t for t in tags if not (t in seen or seen.add(t))]

    if not tags:
        if pnl_pct is not None and pnl_pct >= 0:
            return tags, "ok"
        return tags, "review"

    if "A" in tags and "C" not in tags:
        rec = "train_entry"
    elif "C" in tags or "B" in tags:
        rec = "train_exit"
    elif "D" in tags:
        rec = "fix_data"
    elif "A" in tags:
        rec = "train_entry"
    else:
        rec = "review"
    return tags, rec


def _hold_minutes(opened_ts: Any, closed_ts: Any) -> Optional[float]:
    try:
        import pandas as pd

        t0 = pd.Timestamp(opened_ts)
        t1 = pd.Timestamp(closed_ts) if closed_ts is not None else pd.Timestamp.now()
        return round((t1 - t0).total_seconds() / 60.0, 1)
    except Exception:
        return None


def _build_notes(
    tags: List[str],
    p_good: Optional[float],
    mfe: Optional[float],
    mae: Optional[float],
    pnl: Optional[float],
    exit_detail: str,
    is_open: bool,
) -> List[str]:
    notes: List[str] = []
    if "A" in tags:
        if p_good is not None and p_good >= 0.45:
            notes.append(f"fusion пропустил вход (P={p_good:.3f})")
        if mfe is not None and mfe < 1.0:
            notes.append(f"низкий MFE по барам ({mfe:+.2f}%)")
    if "B" in tags and mfe is not None and pnl is not None:
        notes.append(f"отдал прибыль: MFE {mfe:+.2f}% → итог {pnl:+.2f}%")
    if "C" in tags:
        if exit_detail.startswith("overnight_eod_flat"):
            notes.append("закрыт EOD-flat убытка после просадки")
        elif is_open and mae is not None:
            notes.append(f"открыт в просадке MAE {mae:+.2f}%")
        elif mae is not None:
            notes.append(f"глубокий MAE {mae:+.2f}% перед выходом")
    if not tags and pnl is not None and pnl >= 0:
        notes.append("контуры согласованы, результат положительный")
    return notes


def analyze_buy_row(eng, buy_row: Any, sell_row: Optional[Any], session_day: date) -> Dict[str, Any]:
    bctx = _parse_ctx(buy_row.context_json)
    ep = float(buy_row.price)
    ticker = str(buy_row.ticker).strip().upper()
    opened_ts = buy_row.ts

    is_open = sell_row is None
    exit_type = exit_detail = None
    pnl_pct = None
    closed_ts = None
    xctx: dict = {}

    if sell_row is not None:
        xctx = _parse_ctx(sell_row.context_json)
        xp = float(sell_row.price)
        pnl_pct = round((xp - ep) / ep * 100.0, 2) if ep else None
        exit_type = str(sell_row.signal_type or "").strip().upper()
        exit_detail = str(xctx.get("exit_detail") or "")
        closed_ts = sell_row.ts

    mfe_bar, mae_bar = mfe_mae_from_bars(eng, ticker, opened_ts, closed_ts, ep)
    hold_min = _hold_minutes(opened_ts, closed_ts)
    tags, rec = classify_trade(
        entry_ctx=bctx,
        exit_ctx=xctx,
        pnl_pct=pnl_pct,
        mfe_pct=mfe_bar,
        mae_pct=mae_bar,
        exit_type=exit_type,
        exit_detail=exit_detail or "",
        is_open=is_open,
        hold_minutes=hold_min,
    )

    p_good = _f(bctx.get("catboost_entry_proba_good"))
    regime = bctx.get("intraday_regime")
    regime_name = regime.get("regime") if isinstance(regime, dict) else None

    return {
        "buy_id": int(buy_row.id),
        "sell_id": int(sell_row.id) if sell_row is not None else None,
        "ticker": ticker,
        "opened_at": str(opened_ts),
        "closed_at": str(closed_ts) if closed_ts else None,
        "status": "open" if is_open else "closed",
        "entry_price": round(ep, 2),
        "exit_price": round(float(sell_row.price), 2) if sell_row is not None else None,
        "pnl_pct": pnl_pct,
        "mfe_pct": mfe_bar,
        "mae_pct": mae_bar,
        "hold_minutes": hold_min,
        "exit_type": exit_type,
        "exit_detail": exit_detail or None,
        "entry_branch": _branch_label(bctx) or None,
        "entry_decision": bctx.get("technical_decision_effective") or bctx.get("decision"),
        "catboost_p_good": p_good,
        "catboost_dataset_version": bctx.get("catboost_dataset_version"),
        "momentum_2h_pct": _f(bctx.get("momentum_2h_pct")),
        "pullback_from_high_pct": _f((regime.get("metrics") or {}).get("pullback_from_high_pct"))
        if isinstance(regime, dict)
        else None,
        "intraday_regime": regime_name,
        "tags": tags,
        "tag_labels": [TAG_LABELS[t] for t in tags],
        "training_recommendation": rec,
        "notes": _build_notes(tags, p_good, mfe_bar, mae_bar, pnl_pct, exit_detail or "", is_open),
    }


def pair_buys_on_session(day_df, wide_df=None) -> List[Tuple[Any, Optional[Any]]]:
    import pandas as pd

    if wide_df is None:
        wide_df = day_df
    buys = day_df[day_df["side"].astype(str).str.upper() == "BUY"].copy()
    pairs: List[Tuple[Any, Optional[Any]]] = []
    for _, b in buys.iterrows():
        tkr = str(b.ticker).strip().upper()
        sell_q = wide_df[
            (wide_df["ticker"].astype(str).str.upper() == tkr)
            & (wide_df["side"].astype(str).str.upper() == "SELL")
            & (pd.to_datetime(wide_df["ts"]) > pd.to_datetime(b.ts))
        ]
        sell_row = sell_q.iloc[0] if not sell_q.empty else None
        pairs.append((b, sell_row))
    return pairs


def _training_priority(rec_counts: Dict[str, int], tag_counts: Dict[str, int]) -> str:
    exit_n = rec_counts.get("train_exit", 0)
    entry_n = rec_counts.get("train_entry", 0)
    if exit_n > entry_n:
        return "exit/hold"
    if entry_n > exit_n:
        return "entry/fusion"
    if tag_counts.get("C", 0) + tag_counts.get("B", 0) > tag_counts.get("A", 0):
        return "exit/hold"
    if tag_counts.get("A", 0) > 0:
        return "entry/fusion"
    return "observe"


def build_session_postmortem(session_day: date) -> Dict[str, Any]:
    import pandas as pd
    from report_generator import get_engine
    from sqlalchemy import text

    t0 = time_mod.time()
    day_s = session_day.isoformat()
    eng = get_engine()
    d0 = f"{day_s} 00:00:00"
    d1 = f"{(session_day + timedelta(days=1)).isoformat()} 00:00:00"

    day_df = pd.read_sql(
        text(
            """
            SELECT id, ts, ticker, side, price, signal_type, context_json
            FROM trade_history
            WHERE strategy_name = 'GAME_5M' AND ts >= :d0 AND ts < :d1
            ORDER BY ts ASC, id ASC
            """
        ),
        eng,
        params={"d0": d0, "d1": d1},
    )
    wide_df = pd.read_sql(
        text(
            """
            SELECT id, ts, ticker, side, price, signal_type, context_json
            FROM trade_history
            WHERE strategy_name = 'GAME_5M' AND ts >= :d0_wide AND ts < :d1
            ORDER BY ts ASC, id ASC
            """
        ),
        eng,
        params={"d0_wide": f"{(session_day - timedelta(days=5)).isoformat()} 00:00:00", "d1": d1},
    )

    pairs = pair_buys_on_session(day_df, wide_df)
    trades = [analyze_buy_row(eng, b, s, session_day) for b, s in pairs]

    tag_counts: Dict[str, int] = {k: 0 for k in TAG_LABELS}
    rec_counts: Dict[str, int] = {}
    for t in trades:
        for tag in t["tags"]:
            tag_counts[tag] += 1
        rec = t["training_recommendation"]
        rec_counts[rec] = rec_counts.get(rec, 0) + 1

    session_buys = int((day_df["side"].astype(str).str.upper() == "BUY").sum()) if not day_df.empty else 0
    focus = _training_priority(rec_counts, tag_counts)

    return {
        "postmortem_version": POSTMORTEM_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session_date_msk": day_s,
        "elapsed_sec": round(time_mod.time() - t0, 1),
        "summary": {
            "session_buys": session_buys,
            "analyzed_trades": len(trades),
            "closed": sum(1 for t in trades if t["status"] == "closed"),
            "open": sum(1 for t in trades if t["status"] == "open"),
            "tag_counts": tag_counts,
            "training_recommendation_counts": rec_counts,
            "training_focus": focus,
        },
        "trades": trades,
        "training_priority": focus,
    }


def load_postmortem_sessions(
    project_root: Path | None = None,
    *,
    max_lines: int = 120,
) -> List[Dict[str, Any]]:
    path = sessions_jsonl_path(project_root)
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except json.JSONDecodeError:
            continue
    return rows[-max_lines:]


def _upsert_session_row(rows: List[Dict[str, Any]], session: Dict[str, Any]) -> List[Dict[str, Any]]:
    day = str(session.get("session_date_msk") or "")
    out = [r for r in rows if str(r.get("session_date_msk") or "") != day]
    out.append(session)
    out.sort(key=lambda r: str(r.get("session_date_msk") or ""))
    return out


def write_sessions_jsonl(rows: List[Dict[str, Any]], project_root: Path | None = None) -> str:
    path = sessions_jsonl_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return str(path)


def aggregate_tactics_state(
    sessions: List[Dict[str, Any]],
    *,
    window_days: int = 14,
) -> Dict[str, Any]:
    window_days = max(1, min(int(window_days), 90))
    cutoff = (datetime.now().date() - timedelta(days=window_days)).isoformat()
    in_window = [s for s in sessions if str(s.get("session_date_msk") or "") >= cutoff]

    tag_counts = {k: 0 for k in TAG_LABELS}
    rec_counts: Dict[str, int] = {}
    trades_n = 0
    fusion_fp = 0
    eod_loss_n = 0
    low_mfe_n = 0

    for sess in in_window:
        for t in sess.get("trades") or []:
            if not isinstance(t, dict):
                continue
            trades_n += 1
            for tag in t.get("tags") or []:
                if tag in tag_counts:
                    tag_counts[tag] += 1
            rec = str(t.get("training_recommendation") or "")
            rec_counts[rec] = rec_counts.get(rec, 0) + 1
            p = _f(t.get("catboost_p_good"))
            pnl = _f(t.get("pnl_pct"))
            mfe = _f(t.get("mfe_pct"))
            if p is not None and p >= 0.45 and pnl is not None and pnl < 0:
                fusion_fp += 1
            if mfe is not None and mfe < 1.0:
                low_mfe_n += 1
            if str(t.get("exit_detail") or "").startswith("overnight_eod_flat_loss"):
                eod_loss_n += 1

    entry_signals = tag_counts.get("A", 0) + rec_counts.get("train_entry", 0)
    exit_signals = tag_counts.get("B", 0) + tag_counts.get("C", 0) + rec_counts.get("train_exit", 0)
    total_sig = max(1, entry_signals + exit_signals)
    focus = _training_priority(rec_counts, tag_counts)

    return {
        "postmortem_version": POSTMORTEM_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days,
        "sessions_in_window": len(in_window),
        "trades_in_window": trades_n,
        "tag_counts_rolling": tag_counts,
        "recommendation_counts_rolling": rec_counts,
        "signals": {
            "fusion_false_positive_trades": fusion_fp,
            "low_mfe_trades": low_mfe_n,
            "eod_flat_loss_trades": eod_loss_n,
        },
        "training_focus": focus,
        "training_focus_pct": {
            "entry": round(100.0 * entry_signals / total_sig, 1),
            "exit": round(100.0 * exit_signals / total_sig, 1),
        },
        "recent_sessions": [
            {
                "session_date_msk": s.get("session_date_msk"),
                "summary": s.get("summary"),
                "training_priority": s.get("training_priority"),
            }
            for s in in_window[-8:]
        ],
    }


def build_tactic_recommendations(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Structured recommendations for tuning controller / Analyzer (no auto-apply)."""
    recs: List[Dict[str, Any]] = []
    tags = state.get("tag_counts_rolling") if isinstance(state.get("tag_counts_rolling"), dict) else {}
    sig = state.get("signals") if isinstance(state.get("signals"), dict) else {}
    focus = str(state.get("training_focus") or "observe")
    trades_n = int(state.get("trades_in_window") or 0)

    if trades_n < 3:
        recs.append(
            {
                "id": "accumulate_sessions",
                "priority": "low",
                "contour": "observe",
                "rationale_ru": f"Мало сделок в окне ({trades_n}) — накопить ещё 1–2 сессии post-mortem.",
                "suggested_actions": ["продолжить cron post-mortem", "не менять bundle"],
            }
        )
        return recs

    if int(sig.get("fusion_false_positive_trades") or 0) >= 2 or (
        focus == "entry/fusion" and int(tags.get("A") or 0) >= 2
    ):
        recs.append(
            {
                "id": "entry_fusion_tighten",
                "priority": "high",
                "contour": "entry",
                "rationale_ru": (
                    f"В окне {state.get('window_days')}d: тег A={tags.get('A', 0)}, "
                    f"ложные fusion={sig.get('fusion_false_positive_trades', 0)}."
                ),
                "suggested_actions": [
                    "рассмотреть GAME_5M_CATBOOST_HOLD_BELOW_P +0.03…0.05",
                    "ужесточить buy_rth_momentum (min momentum_2h)",
                    "поставить в очередь train entry_bar",
                ],
            }
        )

    if int(tags.get("B") or 0) >= 1 or int(tags.get("C") or 0) >= 2 or focus == "exit/hold":
        recs.append(
            {
                "id": "exit_hold_improve",
                "priority": "high" if int(tags.get("C") or 0) >= 2 else "medium",
                "contour": "exit",
                "rationale_ru": (
                    f"Теги B={tags.get('B', 0)} C={tags.get('C', 0)}; "
                    f"EOD-flat loss={sig.get('eod_flat_loss_trades', 0)}."
                ),
                "suggested_actions": [
                    "ранний loss-cut при core=SELL и pnl<-1%",
                    "soft-take / continuation apply",
                    "train hold/recovery",
                ],
            }
        )

    if int(tags.get("D") or 0) > 0:
        recs.append(
            {
                "id": "price_feed_quality",
                "priority": "medium",
                "contour": "data",
                "rationale_ru": "Расхождение bar MFE vs сохранённого mfe — проверить price_5m vs quote.",
                "suggested_actions": ["аудит record_entry price source"],
            }
        )

    if not recs:
        recs.append(
            {
                "id": "continue_observe",
                "priority": "low",
                "contour": "observe",
                "rationale_ru": "Явных перекосов entry/exit в rolling post-mortem нет.",
                "suggested_actions": ["продолжить observe текущего bundle"],
            }
        )
    return recs


def build_tactics_payload(
    sessions: List[Dict[str, Any]],
    *,
    window_days: int = 14,
) -> Dict[str, Any]:
    state = aggregate_tactics_state(sessions, window_days=window_days)
    state["tactic_recommendations"] = build_tactic_recommendations(state)
    return state


def load_tactics_aggregate(project_root: Path | None = None) -> Dict[str, Any]:
    path = tactics_aggregate_path(project_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def sync_postmortem_to_ledger(
    tactics: Dict[str, Any],
    *,
    last_session: Dict[str, Any],
    ledger_raw: str = "",
) -> Dict[str, Any]:
    ledger = load_ledger(ledger_raw)
    ledger["postmortem_tactics"] = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "last_session_date_msk": last_session.get("session_date_msk"),
        "training_focus": tactics.get("training_focus"),
        "training_focus_pct": tactics.get("training_focus_pct"),
        "tag_counts_rolling": tactics.get("tag_counts_rolling"),
        "signals": tactics.get("signals"),
        "tactic_recommendations": tactics.get("tactic_recommendations"),
        "sessions_jsonl": str(sessions_jsonl_path()),
        "tactics_path": str(tactics_aggregate_path()),
    }
    hist = ledger.setdefault("postmortem_history", [])
    if isinstance(hist, list):
        hist.append(
            {
                "session_date_msk": last_session.get("session_date_msk"),
                "summary": last_session.get("summary"),
                "training_focus": last_session.get("training_priority"),
                "at_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
        ledger["postmortem_history"] = hist[-60:]
    save_ledger(ledger, ledger_raw)
    return ledger.get("postmortem_tactics") or {}


def refresh_game5m_trade_postmortem(
    session_day: date | None = None,
    *,
    window_days: int = 14,
    project_root: Path | None = None,
    ledger_raw: str = "",
    sync_ledger: bool = True,
) -> Dict[str, Any]:
    """Nightly pipeline: session report → JSONL → tactics aggregate → tuning ledger."""
    session_day = session_day or datetime.now().date()
    session = build_session_postmortem(session_day)

    rows = load_postmortem_sessions(project_root)
    rows = _upsert_session_row(rows, session)
    jsonl_path = write_sessions_jsonl(rows, project_root)

    snap_path = last_session_snapshot_path(project_root)
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    tactics = build_tactics_payload(rows, window_days=window_days)
    tact_path = tactics_aggregate_path(project_root)
    tact_path.write_text(json.dumps(tactics, ensure_ascii=False, indent=2), encoding="utf-8")

    ledger_sync = None
    if sync_ledger:
        try:
            ledger_sync = sync_postmortem_to_ledger(tactics, last_session=session, ledger_raw=ledger_raw)
        except Exception:
            ledger_sync = {"error": "ledger_sync_failed"}

    return {
        "ok": True,
        "session": session,
        "paths": {
            "session_snapshot": str(snap_path),
            "sessions_jsonl": jsonl_path,
            "tactics_aggregate": str(tact_path),
        },
        "tactics": tactics,
        "ledger_postmortem_tactics": ledger_sync,
    }


def format_session_markdown(report: Dict[str, Any]) -> str:
    s = report.get("summary") or {}
    lines = [
        f"# GAME_5M post-mortem — {report.get('session_date_msk')}",
        "",
        f"- BUY: **{s.get('session_buys', 0)}** | разобрано: **{s.get('analyzed_trades', 0)}**",
        f"- Теги: A={s.get('tag_counts', {}).get('A', 0)} "
        f"B={s.get('tag_counts', {}).get('B', 0)} "
        f"C={s.get('tag_counts', {}).get('C', 0)} "
        f"D={s.get('tag_counts', {}).get('D', 0)}",
        f"- Фокус: **{report.get('training_priority')}**",
        "",
    ]
    for t in report.get("trades") or []:
        tags = ",".join(t.get("tags") or []) or "—"
        pnl = f"{t.get('pnl_pct'):+.2f}%" if t.get("pnl_pct") is not None else "open"
        lines.append(f"### {t.get('ticker')} [{tags}] | PnL {pnl}")
        if t.get("notes"):
            lines.append(f"- {'; '.join(t['notes'])}")
        lines.append("")
    return "\n".join(lines)


def recommendations_ru_from_tactics(tactics: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for row in tactics.get("tactic_recommendations") or []:
        if not isinstance(row, dict):
            continue
        out.append(str(row.get("rationale_ru") or ""))
    focus = tactics.get("training_focus")
    if focus:
        out.insert(0, f"Post-mortem rolling focus ({tactics.get('window_days')}d): {focus}.")
    return [x for x in out if x]
