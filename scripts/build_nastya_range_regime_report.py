#!/usr/bin/env python3
"""CLI для отчёта Насти (логика в services/nastya_range_regime.py). Обычно не нужен — есть вкладка UI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from services.nastya_range_regime import (
        DEFAULT_TICKERS,
        build_nastya_range_regime_report,
        default_excel_path,
        save_report_cache,
    )

    ap = argparse.ArgumentParser()
    ap.add_argument("--excel", type=Path, default=None)
    ap.add_argument("--tickers", type=str, default=",".join(DEFAULT_TICKERS))
    ap.add_argument("--out-json", type=Path, default=ROOT / "nastya" / "range_regime_prototype.json")
    ap.add_argument("--out-md", type=Path, default=ROOT / "nastya" / "range_regime_prototype_report.md")
    args = ap.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    try:
        from report_generator import get_engine

        engine = get_engine()
    except Exception:
        engine = None
    report = build_nastya_range_regime_report(
        tickers=tickers,
        excel_path=args.excel or default_excel_path(),
        engine=engine,
    )
    save_report_cache(report)
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    m = report.get("market") or {}
    lines = [
        "# Коридоры / боковик / bias",
        "",
        f"generated: `{report.get('generated_at_utc')}`",
        f"excel: `{report.get('source_excel')}` exists={report.get('excel_exists')}",
        f"ohlcv: `{report.get('ohlcv_source')}`",
        "",
        f"NDX {m.get('ndx_close')} ~6w {m.get('ndx_ret_approx_6w_pct')}% · VIX {m.get('vix_close')} ({m.get('vix_regime')})",
        "",
        "| Ticker | Close | Regime | Floor→Ceil | Pos | Bias | RVOL | Excel |",
        "|--------|------:|--------|------------|----:|------|------|-------|",
    ]
    for r in report.get("tickers") or []:
        if r.get("status") != "ok":
            lines.append(f"| {r.get('ticker')} | — | {r.get('status')} | — | — | — | — | {r.get('in_excel')} |")
            continue
        lines.append(
            f"| {r.get('ticker')} | {r.get('close')} | {r.get('regime')} | "
            f"{r.get('band_floor')}→{r.get('band_ceiling')} | {r.get('pos_in_band')} | "
            f"**{r.get('bias_exit')}** | {r.get('rvol_20')} ({r.get('rvol_flag')}) | "
            f"{'yes' if r.get('in_excel') else 'NO'} |"
        )
    args.out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "json": str(args.out_json), "md": str(args.out_md)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
