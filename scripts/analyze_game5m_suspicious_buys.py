#!/usr/bin/env python3
"""
Scan GAME_5M BUY rows for "suspicious" entries near local/session highs.

Why this exists
---------------
`scripts/send_sndk_signal_cron.py` records BUY at `record_entry(ticker, price, ...)`
where `price` comes from `get_decision_5m(...).price` (last 5m Close in the feature DF).

That can disagree with what you *see* on the chart if:
- the chart is RTH-only while Yahoo bars include extended hours spikes,
- Yahoo returns a stale/odd last bar vs what you visually interpolate,
- or the BUY timestamp is not aligned to the bar you are looking at.

This tool compares:
- recorded BUY price vs saved entry snapshot (`context_json` via `normalize_entry_context`)
- recorded BUY price vs max High on 5m bars strictly before entry time (ET)

Usage
-----
python3 scripts/analyze_game5m_suspicious_buys.py --days 14 --near-high-pct 0.35 --min-gap-to-close-pct 0.25

Optional:
  --ticker NBIS
  --open-only   # only BUY legs with no later SELL (same ticker/strategy)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import create_engine, text

# Make `import services.*` work when executed as a file.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import get_database_url  # noqa: E402
from services.deal_params_5m import normalize_entry_context  # noqa: E402
from services.recommend_5m import fetch_5m_ohlc  # noqa: E402


GAME_5M = "GAME_5M"


def _to_et(ts: Any) -> Optional[pd.Timestamp]:
    if ts is None:
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        # trade_history.ts is typically naive MSK wall time
        try:
            t = t.tz_localize("Europe/Moscow", ambiguous=True)
        except Exception:
            t = t.tz_localize("UTC", ambiguous=True)
    return t.tz_convert("America/New_York")


def _load_trades(engine, days: int, ticker: Optional[str]) -> pd.DataFrame:
    q = """
        SELECT id, ts, ticker, side, price, signal_type, context_json
        FROM public.trade_history
        WHERE strategy_name = :strategy
          -- NOTE: don't mix SQLAlchemy named binds with Postgres "::casts" inside the same token
          -- (e.g. ":days::int" breaks parsing). Cast via CAST(...) instead.
          AND ts >= (NOW() AT TIME ZONE 'UTC') - (CAST(:days AS int) * INTERVAL '1 day')
    """
    params: Dict[str, Any] = {"strategy": GAME_5M, "days": int(days)}
    if ticker:
        q += " AND UPPER(TRIM(ticker)) = UPPER(TRIM(:ticker))"
        params["ticker"] = ticker
    q += " ORDER BY ts ASC, id ASC"
    with engine.connect() as conn:
        return pd.read_sql(text(q), conn, params=params)


def _is_open_buy(df: pd.DataFrame, buy_idx: int) -> bool:
    row = df.iloc[buy_idx]
    if str(row.get("side") or "").upper() != "BUY":
        return False
    tkr = str(row.get("ticker") or "").strip().upper()
    buy_ts = row.get("ts")
    buy_id = int(row.get("id") or 0)
    later = df[(df["ticker"].astype(str).str.upper().str.strip() == tkr) & (df["ts"] >= buy_ts)]
    for _, r in later.iterrows():
        if str(r.get("side") or "").upper() != "SELL":
            continue
        rid = int(r.get("id") or 0)
        rts = r.get("ts")
        if rts > buy_ts or (rts == buy_ts and rid > buy_id):
            return False
    return True


def _fetch_ohlc_5m(ticker: str, *, entry_et: pd.Timestamp, lookback_days: int) -> Optional[pd.DataFrame]:
    # Pull a bit more history than the immediate window to stabilize Yahoo/DB edges.
    days = max(2, min(7, int(lookback_days)))
    df = fetch_5m_ohlc(ticker, days=days)
    if df is None or df.empty:
        return None
    if "datetime" not in df.columns or "High" not in df.columns or "Close" not in df.columns:
        return None
    out = df.copy()
    dtt = pd.to_datetime(out["datetime"], errors="coerce")
    if dtt.dt.tz is None:
        dtt = dtt.dt.tz_localize("America/New_York", ambiguous=True)
    else:
        dtt = dtt.dt.tz_convert("America/New_York")
    out["datetime"] = dtt
    out["High"] = pd.to_numeric(out["High"], errors="coerce")
    out["Close"] = pd.to_numeric(out["Close"], errors="coerce")
    out = out.dropna(subset=["datetime", "High", "Close"]).sort_values("datetime")
    # Keep only bars that end before/at entry (strictly before is safer for "high so far")
    out = out[out["datetime"] <= entry_et]
    return out


@dataclass
class RowOut:
    trade_id: int
    ticker: str
    ts_utc: str
    ts_et: str
    side: str
    signal_type: str
    buy_price: float
    ctx_price: Optional[float]
    ctx_session_high: Optional[float]
    ctx_last_bar_high: Optional[float]
    ctx_recent_high_max: Optional[float]
    ctx_pullback_from_high_pct: Optional[float]
    ctx_branch: Optional[str]
    ctx_session_phase: Optional[str]
    ohlc_bars: int
    ohlc_high_max_pre_entry: Optional[float]
    ohlc_close_last_pre_entry: Optional[float]
    dist_to_ohlc_high_max_pct: Optional[float]
    dist_to_ctx_session_high_pct: Optional[float]
    dist_to_ctx_price_pct: Optional[float]
    suspicious: bool
    reason: str


def _fmt_ts_et(ts_et: pd.Timestamp) -> str:
    return ts_et.strftime("%Y-%m-%d %H:%M:%S %Z")


def analyze(
    *,
    days: int,
    ticker: Optional[str],
    open_only: bool,
    near_high_pct: float,
    min_gap_to_close_pct: float,
    lookback_days: int,
) -> List[RowOut]:
    engine = create_engine(get_database_url())
    df = _load_trades(engine, days=days, ticker=ticker)
    if df.empty:
        return []

    outs: List[RowOut] = []
    for i in range(len(df)):
        row = df.iloc[i]
        if str(row.get("side") or "").upper() != "BUY":
            continue
        if open_only and not _is_open_buy(df, i):
            continue

        tkr = str(row.get("ticker") or "").strip().upper()
        buy_id = int(row.get("id") or 0)
        buy_price = float(row.get("price") or 0.0)
        if buy_price <= 0:
            continue

        ts_et = _to_et(row.get("ts"))
        if ts_et is None:
            continue

        ctx = normalize_entry_context(row.get("context_json"))
        ctx_price = ctx.get("price")
        try:
            ctx_price_f = float(ctx_price) if ctx_price is not None else None
        except (TypeError, ValueError):
            ctx_price_f = None

        def _f(name: str) -> Optional[float]:
            v = ctx.get(name)
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        sh = _f("session_high")
        lbh = _f("last_bar_high")
        rh = _f("recent_bars_high_max")
        pb = _f("pullback_from_high_pct")
        branch = ctx.get("technical_entry_branch")
        if branch is not None:
            branch = str(branch)
        sp = ctx.get("session_phase")
        if sp is None:
            ms = ctx.get("market_session")
            if isinstance(ms, dict):
                sp = ms.get("session_phase") or ms.get("phase")
        if sp is not None:
            sp = str(sp)

        ohlc = _fetch_ohlc_5m(tkr, entry_et=ts_et, lookback_days=lookback_days)
        ohlc_bars = int(len(ohlc)) if ohlc is not None else 0
        ohlc_high_max = float(ohlc["High"].max()) if ohlc_bars else None
        ohlc_close_last = float(ohlc["Close"].iloc[-1]) if ohlc_bars else None

        def _dist(a: Optional[float]) -> Optional[float]:
            if a is None or a <= 0:
                return None
            return (buy_price / a - 1.0) * 100.0

        d_high = _dist(ohlc_high_max)
        d_sh = _dist(sh)
        d_cp = _dist(ctx_price_f)

        suspicious = False
        reasons: List[str] = []

        # Near "high so far" on 5m window
        if d_high is not None and d_high >= -float(near_high_pct):
            suspicious = True
            reasons.append(f"buy_within_{near_high_pct:g}%_of_maxHigh_pre_entry({d_high:+.3f}%)")

        # Recorded buy far above last pre-entry close (often indicates bar mismatch / spike)
        if ohlc_close_last and ohlc_close_last > 0:
            gap = (buy_price / ohlc_close_last - 1.0) * 100.0
            if gap >= float(min_gap_to_close_pct):
                suspicious = True
                reasons.append(f"buy_{gap:+.3f}%_above_last_pre_entry_close")

        # Snapshot disagrees with recorded trade price
        if ctx_price_f and ctx_price_f > 0:
            snap = (buy_price / ctx_price_f - 1.0) * 100.0
            if abs(snap) >= 0.25:  # fixed small threshold: execution vs snapshot drift
                suspicious = True
                reasons.append(f"buy_vs_ctx_price_delta_{snap:+.3f}%")

        outs.append(
            RowOut(
                trade_id=buy_id,
                ticker=tkr,
                ts_utc=str(row.get("ts")),
                ts_et=_fmt_ts_et(ts_et),
                side="BUY",
                signal_type=str(row.get("signal_type") or ""),
                buy_price=buy_price,
                ctx_price=ctx_price_f,
                ctx_session_high=sh,
                ctx_last_bar_high=lbh,
                ctx_recent_high_max=rh,
                ctx_pullback_from_high_pct=pb,
                ctx_branch=branch,
                ctx_session_phase=sp,
                ohlc_bars=ohlc_bars,
                ohlc_high_max_pre_entry=ohlc_high_max,
                ohlc_close_last_pre_entry=ohlc_close_last,
                dist_to_ohlc_high_max_pct=d_high,
                dist_to_ctx_session_high_pct=d_sh,
                dist_to_ctx_price_pct=d_cp,
                suspicious=bool(suspicious),
                reason="; ".join(reasons) if reasons else "",
            )
        )

    return outs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="Look back N days in trade_history (UTC NOW anchor).")
    ap.add_argument("--ticker", type=str, default="", help="Optional ticker filter, e.g. NBIS.")
    ap.add_argument("--open-only", action="store_true", help="Only BUY legs that are still open (no later SELL).")
    ap.add_argument("--near-high-pct", type=float, default=0.35, help="Flag if buy is within this % of max pre-entry High.")
    ap.add_argument("--min-gap-to-close-pct", type=float, default=0.25, help="Flag if buy is this % above last pre-entry Close.")
    ap.add_argument("--lookback-days", type=int, default=5, help="OHLC fetch window (2..7).")
    ap.add_argument("--json-out", type=str, default="", help="Optional path to write full JSON rows.")
    args = ap.parse_args()

    rows = analyze(
        days=int(args.days),
        ticker=(args.ticker.strip().upper() if args.ticker else None),
        open_only=bool(args.open_only),
        near_high_pct=float(args.near_high_pct),
        min_gap_to_close_pct=float(args.min_gap_to_close_pct),
        lookback_days=int(args.lookback_days),
    )

    susp = [r for r in rows if r.suspicious]
    print(f"scanned_buys={len(rows)} suspicious={len(susp)}")
    for r in susp[:200]:
        print(
            f"id={r.trade_id} {r.ticker} ts_et={r.ts_et} buy={r.buy_price:.4f} "
            f"maxHigh_pre={r.ohlc_high_max_pre_entry} lastClose_pre={r.ohlc_close_last_pre_entry} "
            f"dHigh={r.dist_to_ohlc_high_max_pct} dSH={r.dist_to_ctx_session_high_pct} branch={r.ctx_branch} phase={r.ctx_session_phase} "
            f"reason={r.reason}"
        )

    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps([r.__dict__ for r in rows], ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote_json={p}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
