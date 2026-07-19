"""
Отчёт «коридоры / боковик / bias» для анализа Насти (~46 стоков).

Excel — ручные якоря; OHLCV/RVOL/NDX/VIX — авто из quotes (fallback yfinance).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
from sqlalchemy import bindparam, text

from config_loader import get_config_value

logger = logging.getLogger(__name__)

DEFAULT_TICKERS = ("META", "AMKR", "ARM")
# Bump when comment/UI payload shape changes so stale JSON cache is ignored.
REPORT_SCHEMA_VERSION = 2
METHOD_RU = """
Цель (запрос Насти)
• Широкие границы движения: где ориентировочно «пол» и «потолок» (не точка входа).
• Боковик: насколько широкая зона / как долго roughly держится (Age≈, width60d).
• Bias: куда вероятнее выход из текущей зоны (up/down/neutral) — эвристика, не прогноз цены.
• RVOL: проверка гипотезы, что на РАЗВОРОТАХ тренда и на ВЫХОДАХ из боковика объём
  аномально растёт или падает. Текущий столник показывает RVOL «сегодня» как индикатор;
  сама гипотеза проверяется на истории дат разворота, а не одним спокойным снимком.

Что на экране
• Коридор floor→ceil = якоря Excel + локальный 20d channel.
• Regime: uptrend / downtrend / range / transition.
• NDX + VIX: глобальный фон (не сигнал купить/продать тикер).

Якоря Excel
• Min low (июн.2022–дек.2023) ×10 — зона ×10 к минимуму 2022 (rerating / замедление).
• UPSIDE = (Min low ×10) / Max high 52w — ×10-зона относительно годового максимума (≈1.0 → у зоны).
• Потенциал от close = (Min low ×10) / Close — та же ×10-зона относительно текущей цены (не дубль UPSIDE).
• %%margin 17%/25% = цель (+17%/+25%) / (Drop 30% от max high).
• Drop 20/25/30%, цели +17/+25%, июльский гребень — ориентиры пола/потолка.

По умолчанию: META (гиперскейлер), AMKR, ARM.
Excel — вручную при новой дате close; котировки/RVOL/NDX/VIX — авто.
""".strip()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_excel_path() -> Path:
    raw = (get_config_value("NASTYA_STOCKS_XLSX", "") or "").strip()
    if raw:
        return Path(raw)
    return _project_root() / "nastya" / "Stocks_17.07.xlsx"


def default_cache_path() -> Path:
    app = Path("/app/logs/ml/ml_data_quality/last_nastya_range_regime.json")
    if Path("/app/logs").exists():
        return app
    p = _project_root() / "local" / "logs" / "ml_data_quality" / "last_nastya_range_regime.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def method_ru() -> str:
    return METHOD_RU


def _f_num(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def load_excel_table(path: Optional[Path] = None) -> pd.DataFrame:
    p = path or default_excel_path()
    if not p.is_file():
        return pd.DataFrame()
    df = pd.read_excel(p)
    if "Ticker" not in df.columns:
        return pd.DataFrame()
    df = df.copy()
    df["Ticker"] = df["Ticker"].astype(str).str.upper().str.strip()
    return df.set_index("Ticker")


def fetch_ohlcv_from_db(engine, tickers: Sequence[str], *, bars: int = 260) -> Dict[str, pd.DataFrame]:
    wanted = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if not wanted or engine is None:
        return {}
    lim = int(bars) + 5
    sql = text(
        """
        SELECT ticker, date, open, high, low, close, volume
        FROM (
            SELECT ticker, date, open, high, low, close, volume,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM quotes
            WHERE ticker IN :tickers
        ) x
        WHERE rn <= :n
        ORDER BY ticker ASC, date ASC
        """
    ).bindparams(bindparam("tickers", expanding=True))
    try:
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn, params={"tickers": wanted, "n": lim})
    except Exception as e:
        logger.debug("nastya ohlcv db: %s", e)
        return {}
    if df is None or df.empty:
        return {}
    out: Dict[str, pd.DataFrame] = {}
    df["date"] = pd.to_datetime(df["date"])
    for t, g in df.groupby("ticker"):
        g = g.set_index("date").sort_index()
        for col in ("Open", "High", "Low", "Close", "Volume"):
            src = col.lower()
            if src in g.columns:
                g[col] = pd.to_numeric(g[src], errors="coerce")
        out[str(t).upper()] = g[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    return out


def fetch_ohlcv_yfinance(tickers: Sequence[str], *, period: str = "1y") -> Dict[str, pd.DataFrame]:
    try:
        import yfinance as yf
    except Exception as e:
        logger.debug("yfinance missing: %s", e)
        return {}
    out: Dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            d = yf.Ticker(t).history(period=period, auto_adjust=True)
            if d is None or d.empty:
                continue
            d = d.rename(columns=str.title)
            need = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in d.columns]
            out[str(t).upper()] = d[need].dropna(subset=["Close"])
        except Exception as e:
            logger.debug("yfinance %s: %s", t, e)
    return out


def _rvol(volume: pd.Series, win: int = 20) -> pd.Series:
    ma = volume.rolling(win).mean()
    return volume / ma


def _detect_local(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    *,
    lookback: int = 60,
    band_pct: float = 8.0,
) -> Dict[str, Any]:
    c = close.iloc[-lookback:]
    h = high.iloc[-lookback:]
    l = low.iloc[-lookback:]
    mid = float((h.max() + l.min()) / 2.0) if len(h) else None
    width = float((h.max() - l.min()) / mid * 100.0) if mid else None
    ret = float(c.iloc[-1] / c.iloc[0] - 1.0) * 100.0 if len(c) > 1 else None
    ch_hi = float(high.iloc[-20:].max())
    ch_lo = float(low.iloc[-20:].min())
    age = 0
    for i in range(len(close) - 1, -1, -1):
        if float(low.iloc[i]) < ch_lo * 0.98 or float(high.iloc[i]) > ch_hi * 1.02:
            break
        age += 1
    sma20 = close.rolling(20).mean().iloc[-1]
    sma50 = close.rolling(50).mean().iloc[-1]
    last = float(close.iloc[-1])
    if width is not None and width <= band_pct and abs(ret or 0.0) < band_pct * 0.6:
        regime = "range"
    elif pd.notna(sma20) and pd.notna(sma50) and last > float(sma20) > float(sma50) and (ret or 0.0) > 5:
        regime = "uptrend"
    elif pd.notna(sma20) and pd.notna(sma50) and last < float(sma20) < float(sma50) and (ret or 0.0) < -5:
        regime = "downtrend"
    else:
        regime = "transition"
    return {
        "regime": regime,
        "range_width_60d_pct": round(width, 2) if width is not None else None,
        "ret_60d_pct": round(ret, 2) if ret is not None else None,
        "approx_range_age_days": age,
        "channel_lo_20d": round(ch_lo, 2),
        "channel_hi_20d": round(ch_hi, 2),
        "sma20": round(float(sma20), 2) if pd.notna(sma20) else None,
        "sma50": round(float(sma50), 2) if pd.notna(sma50) else None,
    }


def _excel_anchors(ex: pd.DataFrame, ticker: str) -> Dict[str, Any]:
    if ex.empty or ticker not in ex.index:
        return {"in_excel": False}
    r = ex.loc[ticker]
    if isinstance(r, pd.DataFrame):
        r = r.iloc[0]

    def col(*names: str) -> Optional[float]:
        for n in names:
            if n in r.index:
                return _f_num(r[n])
        return None

    # Close column may be dated in header
    close_col = None
    for c in r.index:
        if str(c).startswith("Close"):
            close_col = _f_num(r[c])
            break

    return {
        "in_excel": True,
        "excel_close": close_col,
        "drop20_from_52w": col("Drop 20% от max high"),
        "drop25_from_52w": col("Drop 25% от max high"),
        "target_17pct": col("17% growth от close"),
        "target_25pct": col("25% growth от close"),
        "max_52w": col("Max high за 52 недели"),
        "july_crest": col("Max close до дропа 01.07.2026"),
        "drop_from_july_crest": col("Дроп от июльского гребня"),
        "upside": col("UPSIDE"),
        "potential_from_close": col("Потенциал от close"),
        "fall_from_52w": col("Падение от 52w high"),
    }


def _blend_band(last: float, local: Dict[str, Any], xa: Dict[str, Any]) -> Dict[str, Any]:
    supports = [
        v
        for v in [xa.get("drop25_from_52w"), xa.get("drop20_from_52w"), local.get("channel_lo_20d")]
        if v is not None
    ]
    resistances = [
        v
        for v in [
            xa.get("target_17pct"),
            xa.get("target_25pct"),
            xa.get("july_crest"),
            local.get("channel_hi_20d"),
        ]
        if v is not None
    ]
    below = [v for v in supports if v < last]
    above = [v for v in resistances if v > last]
    floor = max(below) if below else (min(supports) if supports else None)
    ceiling = min(above) if above else (max(resistances) if resistances else None)
    pos = None
    if floor is not None and ceiling is not None and ceiling > floor:
        pos = (last - floor) / (ceiling - floor)
    bias = "neutral"
    if pos is not None and pos < 0.35 and local.get("regime") != "downtrend":
        bias = "up"
    elif pos is not None and pos > 0.75:
        bias = "down"
    if local.get("regime") == "uptrend":
        bias = "up"
    if local.get("regime") == "downtrend":
        bias = "down"
    return {
        "band_floor": round(floor, 2) if floor is not None else None,
        "band_ceiling": round(ceiling, 2) if ceiling is not None else None,
        "pos_in_band": round(pos, 2) if pos is not None else None,
        "bias_exit": bias,
    }


def _macro_from_frames(ndx: Optional[pd.DataFrame], vix: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if ndx is None or ndx.empty or "Close" not in ndx.columns:
        return {"status": "missing_ndx"}
    weekly = ndx["Close"].resample("W-FRI").last().dropna()
    wret = weekly.pct_change()
    streak = maxstreak = 0
    for v in wret.iloc[-52:]:
        if pd.notna(v) and float(v) > 0:
            streak += 1
            maxstreak = max(maxstreak, streak)
        else:
            streak = 0
    ndx_6w = ndx["Close"].iloc[-30:]
    vix_close = None
    vix_regime = None
    if vix is not None and not vix.empty and "Close" in vix.columns:
        vix_close = round(float(vix["Close"].iloc[-1]), 2)
        vix_regime = "calm" if vix_close < 20 else ("elevated" if vix_close < 30 else "storm")
    return {
        "status": "ok",
        "ndx_close": round(float(ndx["Close"].iloc[-1]), 2),
        "ndx_ret_approx_6w_pct": round(float(ndx_6w.iloc[-1] / ndx_6w.iloc[0] - 1.0) * 100.0, 2),
        "ndx_max_consec_up_weeks_1y": int(maxstreak),
        "vix_close": vix_close,
        "vix_regime": vix_regime,
        "asof": str(pd.Timestamp(ndx.index[-1]).date()),
    }


def build_nastya_range_regime_report(
    *,
    tickers: Optional[Sequence[str]] = None,
    excel_path: Optional[Path] = None,
    engine=None,
    use_yfinance_fallback: bool = True,
) -> Dict[str, Any]:
    excel_p = excel_path or default_excel_path()
    ex = load_excel_table(excel_p)
    wanted = [str(t).strip().upper() for t in (tickers or DEFAULT_TICKERS) if str(t).strip()]
    if not wanted:
        wanted = list(DEFAULT_TICKERS)

    need = list(dict.fromkeys(wanted + ["^NDX", "^VIX"]))
    frames: Dict[str, pd.DataFrame] = {}
    source = "none"
    if engine is not None:
        frames = fetch_ohlcv_from_db(engine, need, bars=260)
        source = "quotes_db" if frames else "none"
    missing = [t for t in need if t not in frames or frames[t].empty or len(frames[t]) < 40]
    if missing and use_yfinance_fallback:
        yf_frames = fetch_ohlcv_yfinance(missing, period="1y")
        frames.update(yf_frames)
        if source == "quotes_db" and yf_frames:
            source = "quotes_db+yfinance"
        elif yf_frames:
            source = "yfinance"

    rows: List[Dict[str, Any]] = []
    for t in wanted:
        d = frames.get(t)
        if d is None or d.empty or "Close" not in d.columns:
            rows.append({"ticker": t, "status": "no_ohlcv", "in_excel": t in ex.index})
            continue
        close, high, low = d["Close"], d["High"], d["Low"]
        vol = d["Volume"] if "Volume" in d.columns else pd.Series(dtype=float)
        local = _detect_local(close, high, low)
        xa = _excel_anchors(ex, t)
        last = float(close.iloc[-1])
        rvol_now = None
        rvol_flag = "n/a"
        if len(vol) >= 20:
            rv = _rvol(vol)
            if pd.notna(rv.iloc[-1]):
                rvol_now = float(rv.iloc[-1])
                rvol_flag = "high" if rvol_now >= 1.5 else ("low" if rvol_now <= 0.7 else "normal")
        band = _blend_band(last, local, xa)
        upside = xa.get("upside")
        # Short labels for UI; not repeated in comment_ru (table/card already show UPSIDE).
        upside_note = None
        if upside is not None:
            if upside <= 1.05:
                upside_note = "у зоны ×10"
            elif upside <= 1.25:
                upside_note = "близко к ×10"
            else:
                upside_note = "запас до ×10"
        potential = xa.get("potential_from_close")
        if potential is not None:
            try:
                xa = dict(xa)
                xa["potential_from_close"] = round(float(potential), 2)
            except (TypeError, ValueError):
                pass
        # Per-ticker notes: only non-default / actionable; skip boilerplate
        # (UPSIDE note, transition, RVOL normal, floor/ceiling bias — already in columns).
        comments: List[str] = []
        if local.get("regime") == "range":
            comments.append(
                f"Боковик: ширина 60d ≈{local.get('range_width_60d_pct')}%, Age≈{local.get('approx_range_age_days')}d."
            )
        if rvol_flag == "high":
            comments.append("RVOL high — объём аномально высокий (выход/разворот?).")
        elif rvol_flag == "low":
            comments.append("RVOL low — объём слабый.")
        rows.append(
            {
                "ticker": t,
                "status": "ok",
                "asof": str(pd.Timestamp(close.index[-1]).date()),
                "close": round(last, 2),
                **local,
                "rvol_20": round(rvol_now, 2) if rvol_now is not None else None,
                "rvol_flag": rvol_flag,
                **band,
                **xa,
                "upside_note_ru": upside_note,
                "comment_ru": " ".join(comments),
            }
        )

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_excel": str(excel_p),
        "excel_exists": excel_p.is_file(),
        "ohlcv_source": source,
        "method_ru": METHOD_RU,
        "market": _macro_from_frames(frames.get("^NDX"), frames.get("^VIX")),
        "tickers": rows,
        "default_tickers": list(DEFAULT_TICKERS),
        "data_policy": {
            "excel": "manual/semi — якоря Насти; обновлять при новой дате close",
            "ohlcv_rvol_ndx_vix": "auto daily",
            "not_required_mvp": ["candlestick labels", "Investing AI news", "earnings text"],
        },
    }
    mkt = report["market"] if isinstance(report.get("market"), dict) else {}
    mnotes: List[str] = []
    if mkt.get("vix_regime") == "calm":
        mnotes.append("VIX спокойный — глобального «шторма» нет.")
    elif mkt.get("vix_regime") == "storm":
        mnotes.append("VIX storm — осторожнее с bias up.")
    try:
        ndx6 = float(mkt.get("ndx_ret_approx_6w_pct"))
        if ndx6 <= -5:
            mnotes.append(f"NDX ~6w {ndx6}% — фон рынка слабый.")
        elif ndx6 >= 5:
            mnotes.append(f"NDX ~6w +{ndx6}% — фон рынка сильный.")
    except (TypeError, ValueError):
        pass
    mnotes.append(
        "RVOL в таблице — текущий снимок; гипотеза Насти про объём проверяется на датах разворота/выхода из боковика, не этим экраном в одиночку."
    )
    report["market_comment_ru"] = " ".join(mnotes)
    return report


def save_report_cache(report: Dict[str, Any], path: Optional[Path] = None) -> Path:
    p = path or default_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def load_report_cache(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    p = path or default_cache_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def get_or_build_report(
    *,
    tickers: Optional[Sequence[str]] = None,
    refresh: bool = False,
    engine=None,
) -> Dict[str, Any]:
    if not refresh:
        cached = load_report_cache()
        if (
            cached
            and cached.get("tickers")
            and int(cached.get("schema_version") or 0) == REPORT_SCHEMA_VERSION
        ):
            # reuse if same ticker set requested
            req = [str(t).strip().upper() for t in (tickers or DEFAULT_TICKERS) if str(t).strip()]
            got = [str(r.get("ticker") or "").upper() for r in (cached.get("tickers") or [])]
            if req == got:
                cached = dict(cached)
                cached["cache_hit"] = True
                return cached
    if engine is None:
        try:
            from report_generator import get_engine

            engine = get_engine()
        except Exception:
            engine = None
    report = build_nastya_range_regime_report(tickers=tickers, engine=engine)
    save_report_cache(report)
    report["cache_hit"] = False
    return report
