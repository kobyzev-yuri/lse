#!/usr/bin/env python3
"""
Сводка по строкам HANGER_TACTIC / HANGER_V2 / CONTINUATION_GATE из cron_sndk_signal.log.

Не заменяет trade_effectiveness_analyzer (он работает по trade_history в БД).
Для текстовых рекомендаций по сделкам: scripts/analyze_trades_focused.py --llm
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

LINE_RE = re.compile(
    r"(?:\[5m\] |)HANGER_TACTIC (?P<ticker>\w+): apply_hanger_json=(?P<aj>\S+) live_hanger_kind=(?P<hk>\S+) "
    r"cap_pct=(?P<cap>[\d.]+) eff_take_pct=(?P<take>[\d.]+) thr_take_pct=(?P<thr>[\d.]+) "
    r"pnl_pct_for_take=(?P<pnl>[\d.-]+) should_close=(?P<sc>\S+) exit_type=(?P<ex>\S*)"
)
LINE_AH_RE = re.compile(
    r"AFTER_HOURS HANGER_TACTIC (?P<ticker>\w+): apply_hanger_json=(?P<aj>\S+) live_hanger_kind=(?P<hk>\S+) "
    r"cap_pct=(?P<cap>[\d.]+) eff_take_pct=(?P<take>[\d.]+) thr_take_pct=(?P<thr>[\d.]+) "
    r"pnl_pct_for_take=(?P<pnl>[\d.-]+) should_close=(?P<sc>\S+) exit_type=(?P<ex>\S*)"
)
HANGER_V2_RE = re.compile(
    r"HANGER_V2 (?P<ticker>\w+): state=(?P<state>\S+) score=(?P<score>\S+) "
    r"age_min=(?P<age>\S+) pnl=(?P<pnl>[\d.-]+)% mom=(?P<mom>\S+) distance_to_take=(?P<dist>\S+)"
)
CONTINUATION_RE = re.compile(
    r"CONTINUATION_GATE (?P<ticker>\w+): decision=(?P<decision>\S+) would_extend=(?P<extend>\S+) "
    r"log_only=(?P<log_only>\S+) pnl=(?P<pnl>\S+) mom=(?P<mom>\S+) rsi=(?P<rsi>\S+)"
)


def _parse_line(line: str) -> Optional[Dict[str, Any]]:
    m = LINE_RE.search(line) or LINE_AH_RE.search(line)
    if not m:
        return None
    d = m.groupdict()
    d["session"] = "AFTER_HOURS" if line.strip().startswith("AFTER_HOURS") else "RTH"
    return d


def _parse_hanger_v2(line: str) -> Optional[Dict[str, Any]]:
    m = HANGER_V2_RE.search(line)
    if not m:
        return None
    d = m.groupdict()
    d["session"] = "AFTER_HOURS" if "AFTER_HOURS" in line else "RTH"
    return d


def _parse_continuation(line: str) -> Optional[Dict[str, Any]]:
    m = CONTINUATION_RE.search(line)
    if not m:
        return None
    d = m.groupdict()
    d["session"] = "AFTER_HOURS" if "AFTER_HOURS" in line else "RTH"
    return d


def _count(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        val = str(r.get(key) or "unknown")
        out[val] = out.get(val, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def main() -> int:
    p = argparse.ArgumentParser(description="Сводка HANGER_TACTIC / HANGER_V2 / CONTINUATION_GATE из лога send_sndk_signal_cron")
    p.add_argument("log_file", nargs="?", default="logs/cron_sndk_signal.log", help="Путь к логу (хост или копия)")
    p.add_argument("--tail-lines", type=int, default=0, help="Если >0 — обработать только последние N строк")
    args = p.parse_args()

    path = Path(args.log_file).expanduser()
    if not path.is_file():
        print(f"Файл не найден: {path.resolve()}", file=sys.stderr)
        return 2

    raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if int(args.tail_lines) > 0:
        raw = raw[-int(args.tail_lines) :]

    rows: List[Dict[str, Any]] = []
    rows_v2: List[Dict[str, Any]] = []
    rows_cont: List[Dict[str, Any]] = []
    for line in raw:
        rec = _parse_line(line)
        if rec:
            rows.append(rec)
        rec_v2 = _parse_hanger_v2(line)
        if rec_v2:
            rows_v2.append(rec_v2)
        rec_cont = _parse_continuation(line)
        if rec_cont:
            rows_cont.append(rec_cont)

    if not rows and not rows_v2 and not rows_cont:
        print("Строк HANGER_TACTIC / HANGER_V2 / CONTINUATION_GATE не найдено.")
        print("Проверьте путь и что крон уже писал лог с новой версией send_sndk_signal_cron.py.")
        return 1

    if rows_v2:
        last_v2: Dict[str, Dict[str, Any]] = {}
        for r in rows_v2:
            last_v2[r["ticker"]] = r
        print(f"Файл: {path.resolve()} | строк HANGER_V2: {len(rows_v2)}")
        print(f"State counts: {_count(rows_v2, 'state')}")
        print("Последний HANGER_V2 по тикеру:")
        for t in sorted(last_v2.keys()):
            r = last_v2[t]
            print(
                f"  {t} [{r['session']}] state={r['state']} score={r['score']} "
                f"age={r['age']}m pnl={r['pnl']}% mom={r['mom']} dist_to_take={r['dist']}"
            )
        print("")

    if rows_cont:
        last_cont: Dict[str, Dict[str, Any]] = {}
        for r in rows_cont:
            last_cont[r["ticker"]] = r
        print(f"Строк CONTINUATION_GATE: {len(rows_cont)}")
        print(f"Decision counts: {_count(rows_cont, 'decision')}")
        print("Последний CONTINUATION_GATE по тикеру:")
        for t in sorted(last_cont.keys()):
            r = last_cont[t]
            print(
                f"  {t} [{r['session']}] decision={r['decision']} extend={r['extend']} "
                f"log_only={r['log_only']} pnl={r['pnl']} mom={r['mom']} rsi={r['rsi']}"
            )
        print("")

    if not rows:
        print("Legacy HANGER_TACTIC строк не найдено.")
        return 0

    last_by_ticker: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        last_by_ticker[r["ticker"]] = r

    near = sum(1 for r in rows if float(r["pnl"]) >= float(r["thr"]) - 0.15 and r["sc"] in ("False", "false"))
    closed = sum(1 for r in rows if r["sc"] in ("True", "true") and r.get("ex"))

    print(f"Файл: {path.resolve()} | строк HANGER_TACTIC: {len(rows)}")
    print(f"Последняя запись по каждому тикеру ({len(last_by_ticker)}):")
    for t in sorted(last_by_ticker.keys()):
        r = last_by_ticker[t]
        gap = float(r["thr"]) - float(r["pnl"])
        note = "дотянули до тейка" if gap <= 0.05 else f"до порога тейка ~{gap:.2f} п.п."
        print(
            f"  {t} [{r['session']}] apply_hanger_json={r['aj']} hanger={r['hk']} "
            f"cap={r['cap']}% eff_take={r['take']}% pnl={r['pnl']}% → {note} | last_should_close={r['sc']} exit={r['ex']!r}"
        )
    print(f"\nИтого: оценок «близко к тейку» (≤0.15 п.п. до thr, без закрытия): {near}")
    print("\nДальше — анализатор по БД (закрытия, TAKE_PROFIT_SUSPEND):")
    print("  python scripts/analyze_trades_focused.py --days 5 --tickers " + ",".join(sorted(last_by_ticker.keys())) + " --llm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
