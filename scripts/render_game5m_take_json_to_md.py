#!/usr/bin/env python3
"""
Преобразует JSON из backtest_game5m_take_5m_vs_30m.py (--json-out) в Markdown-таблицы.

  python scripts/render_game5m_take_json_to_md.py logs/game5m_take_5m_vs_30m.json
  python scripts/render_game5m_take_json_to_md.py logs/game5m_take_5m_vs_30m.json -o report.md

Без --out — печать в stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional


def _cell(v: Any) -> str:
    if v is None:
        return "—"
    s = str(v).replace("|", "\\|").replace("\n", " ")
    return s


def _replay_cells(prefix: str, r: Optional[dict[str, Any]]) -> list[str]:
    if not r:
        return ["—"] * 6
    return [
        _cell(r.get("signal_type")),
        _cell(r.get("bar_end_et")),
        _cell(r.get("exit_fill_price")),
        _cell(r.get("momentum_2h_pct")),
        _cell(r.get("take_pct_effective")),
        _cell(r.get("log_ret")),
    ]


def render_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# GAME_5M: сверка тейка 5m vs 30m (из JSON)\n")
    lines.append(f"- **Сгенерировано (UTC):** `{payload.get('generated_at', '—')}`")
    lines.append(f"- **Окно выборки BUY:** последние **{payload.get('days', '—')}** дн.")
    lines.append(f"- **Биржа в market_bars_*:** `{payload.get('exchange', '—')}`\n")

    rows = payload.get("rows") or []
    lines.append("## 1. Реплей по фактическим BUY (один вход — два прохода по барам)\n")
    lines.append(
        "Одна строка = один `BUY` из `trade_history` (`strategy_name=GAME_5M`). "
        "Колонки **5m_*** и **30m_*** — когда сработал бы выход и log-return доходности, "
        "если пересчитывать тейк/стоп только по соответствующим свечам. "
        "**diff** = `log_ret_5m − log_ret_30m` (положительное значение → 5m-реплей «лучше» по log-return).\n"
    )

    hdr = (
        "| buy_id | ticker | entry_ts | entry | "
        "5m_signal | 5m_exit_et | 5m_fill | 5m_mom2h% | 5m_take% | 5m_log_ret | "
        "30m_signal | 30m_exit_et | 30m_fill | 30m_mom2h% | 30m_take% | 30m_log_ret | "
        "diff | actual_sell_ts | actual_log_ret |\n"
    )
    sep = "|" + "|".join(["---"] * 18) + "|\n"
    lines.append(hdr + sep)

    for row in rows:
        r5 = row.get("replay_5m")
        r3 = row.get("replay_30m")
        act = row.get("actual_sell")
        c5 = _replay_cells("5m", r5 if isinstance(r5, dict) else None)
        c3 = _replay_cells("30m", r3 if isinstance(r3, dict) else None)
        diff = _cell(row.get("log_ret_diff_5m_minus_30m"))
        act_ts = _cell(act.get("ts") if isinstance(act, dict) else None)
        act_lr = _cell(act.get("log_ret") if isinstance(act, dict) else None)
        line = (
            f"| {_cell(row.get('buy_id'))} | {_cell(row.get('ticker'))} | {_cell(row.get('entry_ts'))} | "
            f"{_cell(row.get('entry_price'))} | "
            f"{c5[0]} | {c5[1]} | {c5[2]} | {c5[3]} | {c5[4]} | {c5[5]} | "
            f"{c3[0]} | {c3[1]} | {c3[2]} | {c3[3]} | {c3[4]} | {c3[5]} | "
            f"{diff} | {act_ts} | {act_lr} |\n"
        )
        lines.append(line)

    lines.append("\n*Диагностика загрузки баров:* в JSON у каждой строки есть `bars_5m_loaded` / `bars_30m_loaded`.\n")

    sim = payload.get("full_30m_strategy_sim")
    if sim and isinstance(sim, dict):
        lines.append("## 2. Полная эмуляция стратегии на 30m (`--full-30m-sim`)\n")
        lines.append(
            "Отдельный проход по 30m: **свои** входы (те же технические правила на `compute_30m_features`) "
            "и выходы по `should_close_position`. Не привязан к датам BUY из п.1. "
            "Окно см. `full_30m_window_et` в JSON.\n"
        )
        for sym, trades in sorted(sim.items()):
            lines.append(f"### Тикер `{sym}`\n")
            lines.append(
                "| entry_ts | entry | branch | decision | exit_ts | signal | detail | exit_fill | log_ret |\n"
                "|---|---:|---|---|---|---|---|---:|---:|\n"
            )
            for t in trades or []:
                if not isinstance(t, dict):
                    continue
                lines.append(
                    f"| {_cell(t.get('entry_ts'))} | {_cell(t.get('entry_price'))} | "
                    f"{_cell(t.get('entry_branch'))} | {_cell(t.get('entry_decision'))} | "
                    f"{_cell(t.get('exit_ts'))} | {_cell(t.get('exit_signal'))} | {_cell(t.get('exit_detail'))} | "
                    f"{_cell(t.get('exit_fill_price'))} | {_cell(t.get('log_ret'))} |\n"
                )
            lines.append("")
    else:
        lines.append(
            "\n*Раздел 2 отсутствует:* в JSON нет `full_30m_strategy_sim` "
            "(скрипт бэктеста запускали без `--full-30m-sim`).\n"
        )

    return "".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="JSON → Markdown для game5m_take_5m_vs_30m")
    p.add_argument(
        "json_path",
        nargs="?",
        default="logs/game5m_take_5m_vs_30m.json",
        help="Путь к JSON (по умолчанию logs/game5m_take_5m_vs_30m.json)",
    )
    p.add_argument("--out", "-o", type=str, default="", help="Файл .md; иначе stdout")
    args = p.parse_args()

    path = Path(args.json_path)
    if not path.is_file():
        print(f"Нет файла: {path.resolve()}", file=sys.stderr)
        sys.exit(1)

    payload = json.loads(path.read_text(encoding="utf-8"))
    md = render_md(payload)

    if args.out.strip():
        out = Path(args.out.strip())
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(out.resolve())
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    main()
