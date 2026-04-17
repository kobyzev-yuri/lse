#!/usr/bin/env python3
"""
Кластер «лидеры» vs остальные по портфельным тикерам (daily quotes).

Критерий (окно стресса, по умолчанию конец февраля — март 2026):
  - накопленная log-return за окно (чувствительность к движению);
  - отношение волатильности (std log-return) в окне к волатильности
    в базовом окне той же длины непосредственно до стресса.

k=2: простой Lloyd в 2D по z-оценкам признаков; кластер с большей
средней накопленной log-return в стрессе помечается как LEADERS.

«Полоса доходности» для лидеров — эмпирическое распределение
накопленных log-return за --horizon торговых дней (назад от каждой даты),
пул по всем тикерам кластера LEADERS за последние --lookback-days.

Примеры:
  python scripts/cluster_portfolio_leaders.py
  python scripts/cluster_portfolio_leaders.py --event-start 2026-02-15 --event-end 2026-03-31
  python scripts/cluster_portfolio_leaders.py --stocks-only false --horizon 63 --csv out.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine, load_quotes  # noqa: E402


def _parse_d(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def _is_stock(ticker: str) -> bool:
    t = ticker.upper().strip()
    if t.startswith("^"):
        return False
    if "=" in t:
        return False
    return True


def _zscore(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=float)
    m = np.nanmean(a)
    s = np.nanstd(a)
    if not np.isfinite(s) or s < 1e-12:
        return np.zeros_like(a, dtype=float)
    return (a - m) / s


def _kmeans2_lloyd(X: np.ndarray, max_iter: int = 50, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    """X: (n, 2), без NaN. Возвращает (labels 0/1, centroids (2,2))."""
    rng = np.random.default_rng(seed)
    n = X.shape[0]
    if n < 2:
        return np.zeros(n, dtype=int), X[:1].copy()

    # старт: две самые удалённые точки по первой координате
    i0 = int(np.argmax(X[:, 0]))
    i1 = int(np.argmin(X[:, 0]))
    if i0 == i1:
        i1 = (i0 + 1) % n
    c = np.vstack([X[i0], X[i1]]).astype(float)
    lab = np.zeros(n, dtype=int)
    for _ in range(max_iter):
        d0 = np.sum((X - c[0]) ** 2, axis=1)
        d1 = np.sum((X - c[1]) ** 2, axis=1)
        new_lab = (d1 < d0).astype(int)
        if np.array_equal(new_lab, lab):
            break
        lab = new_lab
        for k in (0, 1):
            pts = X[lab == k]
            if len(pts) == 0:
                c[k] = X[rng.integers(0, n)]
            else:
                c[k] = pts.mean(axis=0)
    return lab, c


def _window_returns(
    log_ret: pd.Series,
    cal_index: pd.DatetimeIndex,
    start: date,
    end: date,
) -> tuple[float, float, int]:
    """Сумма log-return в окне [start,end] и std; число дней."""
    mask = (cal_index.date >= start) & (cal_index.date <= end)
    sub = log_ret.loc[mask].dropna()
    if sub.empty:
        return float("nan"), float("nan"), 0
    return float(sub.sum()), float(sub.std(ddof=1)), int(sub.shape[0])


def _baseline_vol(
    log_ret: pd.Series,
    cal_index: pd.DatetimeIndex,
    event_start: date,
    n_days: int,
) -> float:
    """std log-return за n_days торговых дней сразу до event_start (не включая)."""
    before = cal_index[cal_index.date < event_start]
    if len(before) < n_days + 1:
        return float("nan")
    idx = before[-n_days:]
    sub = log_ret.reindex(idx).dropna()
    if len(sub) < max(3, n_days // 2):
        return float("nan")
    return float(sub.std(ddof=1))


def _rolling_sum_log_ret(log_ret: pd.Series, horizon: int) -> pd.Series:
    """Сумма log-return за horizon дней, окно назад (заканчивается в t)."""
    return log_ret.rolling(horizon, min_periods=horizon).sum()


def main() -> int:
    parser = argparse.ArgumentParser(description="Кластер лидеров портфеля + полоса доходности")
    parser.add_argument("--event-start", type=str, default="2026-02-15", help="Начало окна стресса (YYYY-MM-DD)")
    parser.add_argument("--event-end", type=str, default="2026-03-31", help="Конец окна стресса")
    parser.add_argument(
        "--stocks-only",
        type=str,
        default="true",
        help="Исключить forex/commodities/VIX (true/false)",
    )
    parser.add_argument("--lookback-days", type=int, default=504, help="История для полосы (торговых дней, ~2 года)")
    parser.add_argument("--horizon", type=int, default=21, help="Горизонт накопленной log-return для полосы (дн.)")
    parser.add_argument("--csv", type=str, default="", help="Сохранить таблицу по тикерам в CSV")
    args = parser.parse_args()

    stocks_only = str(args.stocks_only).strip().lower() in ("1", "true", "yes")

    from services.portfolio_card import get_portfolio_trade_tickers

    tickers = list(get_portfolio_trade_tickers() or [])
    if stocks_only:
        tickers = [t for t in tickers if _is_stock(t)]
    if len(tickers) < 2:
        print("Нужно минимум 2 тикера после фильтра. Проверьте портфельный список или --stocks-only false.")
        return 1

    ev_start = _parse_d(args.event_start)
    ev_end = _parse_d(args.event_end)
    if ev_end < ev_start:
        print("event-end < event-start")
        return 1

    engine = get_engine()
    quotes = load_quotes(engine, tickers)
    if quotes.empty:
        print("Нет данных quotes.")
        return 1

    prices = quotes.pivot_table(index="date", columns="ticker", values="close").sort_index()
    prices = prices.replace(0, np.nan).tail(max(args.lookback_days + 120, 600))
    log_ret = np.log(prices / prices.shift(1)).replace([np.inf, -np.inf], np.nan)
    cal = pd.DatetimeIndex(pd.to_datetime(prices.index))

    rows = []
    for t in tickers:
        if t not in log_ret.columns:
            continue
        lr = log_ret[t].dropna()
        if lr.empty:
            continue
        idx = pd.DatetimeIndex(pd.to_datetime(lr.index))
        s_ret, s_vol, n_e = _window_returns(lr, idx, ev_start, ev_end)
        if n_e < 3 or not np.isfinite(s_ret):
            continue
        b_vol = _baseline_vol(lr, idx, ev_start, n_e)
        if not np.isfinite(b_vol) or b_vol < 1e-9:
            vol_ratio = float("nan")
        else:
            vol_ratio = s_vol / b_vol if np.isfinite(s_vol) else float("nan")
        rows.append(
            {
                "ticker": t,
                "stress_cum_log": s_ret,
                "stress_vol": s_vol,
                "event_days": n_e,
                "baseline_vol": b_vol,
                "vol_ratio": vol_ratio,
            }
        )

    if len(rows) < 2:
        print("Мало тикеров с данными в окне стресса. Расширьте окно или отключите --stocks-only.")
        return 1

    feat = pd.DataFrame(rows).set_index("ticker")
    # признаки для кластеризации
    x0 = feat["stress_cum_log"].to_numpy(dtype=float)
    x1 = feat["vol_ratio"].to_numpy(dtype=float)
    # если vol_ratio NaN — заменим медианой
    med_vr = np.nanmedian(x1)
    x1 = np.where(np.isfinite(x1), x1, med_vr)
    X = np.column_stack([_zscore(x0), _zscore(x1)])
    mask_ok = np.isfinite(X).all(axis=1)
    Xf = X[mask_ok]
    names = feat.index.to_numpy()[mask_ok]
    if len(Xf) < 2:
        print("После очистки NaN осталось <2 тикеров.")
        return 1

    labels, _ = _kmeans2_lloyd(Xf, max_iter=50)
    # кластер с большей средней stress_cum_log = LEADERS (метка 1)
    g0 = feat.loc[names][labels == 0]["stress_cum_log"].mean()
    g1 = feat.loc[names][labels == 1]["stress_cum_log"].mean()
    leaders_label = 1 if g1 >= g0 else 0
    cluster_name = np.where(labels == leaders_label, "LEADERS", "CORE")

    feat_out = feat.loc[names].copy()
    feat_out["cluster"] = cluster_name
    feat_out["stress_cum_simple_pct"] = (np.exp(feat_out["stress_cum_log"]) - 1.0) * 100.0

    leaders = feat_out.index[feat_out["cluster"] == "LEADERS"].tolist()
    core = feat_out.index[feat_out["cluster"] == "CORE"].tolist()

    print("=== Окно стресса ===")
    print(f"  {args.event_start} … {args.event_end} ({feat_out['event_days'].iloc[0]} торговых дн. по рядам)")
    print(f"  Тикеров в кластеризации: {len(feat_out)}  (stocks_only={stocks_only})")
    print()
    print("=== Кластер LEADERS (агрессивнее отреагировали на окно) ===")
    print(" ", ", ".join(leaders) if leaders else "—")
    print()
    print("=== Кластер CORE ===")
    print(" ", ", ".join(core) if core else "—")
    print()
    print(feat_out.sort_values("stress_cum_log", ascending=False).to_string())
    print()

    # --- Полоса доходности: пул rolling horizon log-return для LEADERS ---
    h = int(args.horizon)
    pooled: list[float] = []
    for t in leaders:
        if t not in log_ret.columns:
            continue
        lr = log_ret[t].dropna().tail(int(args.lookback_days))
        rs = _rolling_sum_log_ret(lr, h).dropna()
        pooled.extend(rs.astype(float).tolist())

    if pooled:
        arr = np.array(pooled, dtype=float)
        arr = arr[np.isfinite(arr)]
        # простая доходность за горизонт (%)
        simple = (np.exp(arr) - 1.0) * 100.0
        qs = [10, 25, 50, 75, 90]
        pct = {q: float(np.percentile(simple, q)) for q in qs}
        med = pct[50]
        # грубая annualизация из медианы h-дневной простой доходности: не сложная логика
        ann_hint = (1.0 + med / 100.0) ** (252.0 / h) - 1.0
        print("=== Полоса доходности (история, пул LEADERS, назад от даты) ===")
        print(f"  Горизонт: {h} торговых дней · lookback по каждому тикеру: {args.lookback_days} дн.")
        print(f"  Простая доходность за {h} дн., процентили по пулу всех окон (%):")
        for q in qs:
            print(f"    p{q}: {pct[q]:+.2f}%")
        print(f"  Медиана за {h} дн.: {med:+.2f}%")
        print(
            f"  Ориентир annualize (медиана^{252}/{h}, не прогноз, только масштаб): "
            f"~{ann_hint * 100:+.1f}% годовых"
        )
        print()
        print("  Предупреждение: это описание прошлого распределения лидеров, не гарантия.")
    else:
        print("Нет данных для полосы (лидеры без рядов log-return).")

    sugg = {"PORTFOLIO_LEADER_CLUSTER": leaders, "PORTFOLIO_CORE_CLUSTER": core}
    print("=== JSON для конфига / ручной вставки ===")
    print(json.dumps(sugg, ensure_ascii=False, indent=2))

    if args.csv:
        feat_out.to_csv(args.csv)
        print(f"\nCSV: {args.csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
