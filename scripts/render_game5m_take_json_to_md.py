#!/usr/bin/env python3
"""
Преобразует JSON из backtest_game5m_take_5m_vs_30m.py (--json-out) в Markdown-таблицы.

  python scripts/render_game5m_take_json_to_md.py logs/game5m_take_5m_vs_30m.json
  python scripts/render_game5m_take_json_to_md.py logs/game5m_sim30m_kb.json -o report_sim30m.md

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


def _count_signals(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        block = row.get(key)
        if not isinstance(block, dict):
            continue
        sig = block.get("signal_type")
        if sig is None or sig == "":
            sig = "—"
        else:
            sig = str(sig)
        out[sig] = out.get(sig, 0) + 1
    return dict(sorted(out.items(), key=lambda x: (-x[1], x[0])))


def render_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# GAME_5M: сверка тейка 5m vs 30m (из JSON)\n")
    lines.append(f"- **Сгенерировано (UTC):** `{payload.get('generated_at', '—')}`")
    lines.append(f"- **Параметр days:** **{payload.get('days', '—')}** (глубина BUY при обычном запуске; ширина окна ET для `--full-30m-sim`)")
    lines.append(f"- **Биржа в market_bars_*:** `{payload.get('exchange', '—')}`")
    if payload.get("sim_30m_only"):
        lines.append("- **Режим:** `--sim-30m-only` — реплей по `trade_history` **не** выполнялся (`rows` пустой).")
    if "full_30m_sim_use_kb" in payload:
        lines.append(f"- **KB+VIX в 30m-симе:** `{payload.get('full_30m_sim_use_kb')}`")
    if payload.get("full_30m_sim_kb_days_arg") is not None:
        lines.append(f"- **Аргумент глубины KB:** `{payload.get('full_30m_sim_kb_days_arg')}`")
    win = payload.get("full_30m_window_et")
    if isinstance(win, dict) and (win.get("start") or win.get("end")):
        lines.append(f"- **Окно 30m-сим (ET):** `{win.get('start', '—')}` … `{win.get('end', '—')}`")
    lines.append("")

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

    if not rows:
        lines.append("*Нет строк:* `rows` пустой (например запуск с `--sim-30m-only`).\n")
    else:
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

        pairs = [
            row
            for row in rows
            if isinstance(row.get("replay_5m"), dict) and isinstance(row.get("replay_30m"), dict)
        ]
        if pairs:
            lines.append("### 1b. Одна и та же позиция — разные стратегии выхода (реплей 5m, реплей 30m, факт из БД)\n")
            lines.append(
                "Одна строка = один фактический `BUY`. **Реплей 5m** и **реплей 30m** — как сработал бы `should_close_position` "
                "на разной сетке баров при **неизменной** цене/времени входа. **Факт** — следующий `SELL` в `trade_history`, если есть.\n"
            )
            hdr_b = (
                "| buy_id | ticker | entry_ts | entry | "
                "5m_signal | 5m_exit_ET | 5m_fill | 5m_log_ret | "
                "30m_signal | 30m_exit_ET | 30m_fill | 30m_log_ret | diff | "
                "fact_signal | fact_ts | fact_fill | fact_log_ret |\n"
            )
            sep_b = "|" + "|".join(["---"] * 17) + "|\n"
            lines.append(hdr_b + sep_b)
            for row in pairs:
                r5 = row["replay_5m"]
                r3 = row["replay_30m"]
                act = row.get("actual_sell")
                act_d = act if isinstance(act, dict) else None
                diff = _cell(row.get("log_ret_diff_5m_minus_30m"))
                lines.append(
                    f"| {_cell(row.get('buy_id'))} | {_cell(row.get('ticker'))} | {_cell(row.get('entry_ts'))} | "
                    f"{_cell(row.get('entry_price'))} | "
                    f"{_cell(r5.get('signal_type'))} | {_cell(r5.get('bar_end_et'))} | {_cell(r5.get('exit_fill_price'))} | {_cell(r5.get('log_ret'))} | "
                    f"{_cell(r3.get('signal_type'))} | {_cell(r3.get('bar_end_et'))} | {_cell(r3.get('exit_fill_price'))} | {_cell(r3.get('log_ret'))} | {diff} | "
                    f"{_cell(act_d.get('signal_type') if act_d else None)} | {_cell(act_d.get('ts') if act_d else None)} | "
                    f"{_cell(act_d.get('price') if act_d else None)} | {_cell(act_d.get('log_ret') if act_d else None)} |\n"
                )
            lines.append("")

            lines.append("### 1c. Сводка: тип сигнала выхода по «стратегии» (по всем строкам `rows`)\n")
            lines.append(
                "| Источник | Расшифровка | Количество по `signal_type` |\n"
                "|---|---|---|\n"
            )
            c5 = _count_signals(rows, "replay_5m")
            c3 = _count_signals(rows, "replay_30m")
            ca = _count_signals(rows, "actual_sell")
            lines.append(f"| Реплей **5m** | Офлайн по барам 5m | `{c5}` |\n")
            lines.append(f"| Реплей **30m** | Офлайн по барам 30m | `{c3}` |\n")
            lines.append(f"| **Факт** `SELL` | Запись в БД после `BUY` | `{ca}` |\n")
            lines.append("")

    sim = payload.get("full_30m_strategy_sim")
    if sim and isinstance(sim, dict):
        lines.append("## 2. Полная эмуляция стратегии на 30m (`--full-30m-sim`)\n")
        kb_note = ""
        if payload.get("full_30m_sim_use_kb") is True:
            kb_note = " После технического сигнала применяются **KB (sentiment) + VIX** (`apply_kb_news_to_game5m_decision`), как в `get_decision_5m` (без LLM)."
        elif payload.get("full_30m_sim_use_kb") is False:
            kb_note = " Режим **без KB** (`--no-kb-on-30m-sim`) — только `decide_game5m_technical`."
        lines.append(
            "Отдельный проход по 30m: **свои** входы и выходы по `should_close_position`; не привязан к датам BUY из п.1."
            + kb_note
            + " Окно см. `full_30m_window_et` в JSON.\n"
        )

        flat: list[dict[str, Any]] = []
        for sym, trades in sim.items():
            if not isinstance(trades, list):
                continue
            for t in trades:
                if isinstance(t, dict):
                    t2 = dict(t)
                    t2["_sym"] = sym
                    flat.append(t2)
        if flat:
            lines.append("### Сводная таблица всех сделок автономной 30m\n")
            lines.append(
                "| ticker | entry_ts | entry | branch | decision | exit_ts | exit_signal | exit_fill | log_ret | kb_in_sim |\n"
                "|---|---|---|---|---|---|---|---:|---:|---|\n"
            )
            flat.sort(key=lambda x: (str(x.get("_sym")), str(x.get("entry_ts"))))
            for t in flat:
                lines.append(
                    f"| {_cell(t.get('_sym'))} | {_cell(t.get('entry_ts'))} | {_cell(t.get('entry_price'))} | "
                    f"{_cell(t.get('entry_branch'))} | {_cell(t.get('entry_decision'))} | {_cell(t.get('exit_ts'))} | "
                    f"{_cell(t.get('exit_signal'))} | {_cell(t.get('exit_fill_price'))} | {_cell(t.get('log_ret'))} | "
                    f"{_cell(t.get('kb_in_sim'))} |\n"
                )
            lines.append("")

        lines.append("### По тикерам\n")
        for sym, trades in sorted(sim.items()):
            lines.append(f"#### `{sym}`\n")
            lines.append(
                "| entry_ts | entry | branch | decision | exit_ts | signal | detail | exit_fill | log_ret | kb_in_sim |\n"
                "|---|---:|---|---|---|---|---|---:|---:|---|\n"
            )
            for t in trades or []:
                if not isinstance(t, dict):
                    continue
                lines.append(
                    f"| {_cell(t.get('entry_ts'))} | {_cell(t.get('entry_price'))} | "
                    f"{_cell(t.get('entry_branch'))} | {_cell(t.get('entry_decision'))} | "
                    f"{_cell(t.get('exit_ts'))} | {_cell(t.get('exit_signal'))} | {_cell(t.get('exit_detail'))} | "
                    f"{_cell(t.get('exit_fill_price'))} | {_cell(t.get('log_ret'))} | {_cell(t.get('kb_in_sim'))} |\n"
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
