#!/usr/bin/env python3
"""
Преобразует JSON из backtest_game5m_take_5m_vs_30m.py (--json-out) в Markdown-таблицы.

  python scripts/render_game5m_take_json_to_md.py logs/game5m_take_5m_vs_30m.json
  python scripts/render_game5m_take_json_to_md.py logs/game5m_sim30m_kb.json -o report_sim30m.md

Без --out — печать в stdout.

  Только «шеф-таблица» (20 сделок, русские заголовки):
  python scripts/render_game5m_take_json_to_md.py logs/game5m_take_5m_vs_30m.json --mode chef -o docs/GAME5M_20_TRADES_TABLE.md
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


def _take_price_diff_usd(r5: dict[str, Any], r3: dict[str, Any]) -> Optional[float]:
    try:
        a = float(r5.get("exit_fill_price"))
        b = float(r3.get("exit_fill_price"))
        return round(a - b, 4)
    except (TypeError, ValueError):
        return None


def _pairs_both_replays(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [
        row
        for row in rows
        if isinstance(row.get("replay_5m"), dict) and isinstance(row.get("replay_30m"), dict)
    ]
    def _bid(r: dict[str, Any]) -> int:
        try:
            return int(r.get("buy_id"))
        except (TypeError, ValueError):
            return 0

    out.sort(key=lambda r: (str(r.get("ticker") or ""), _bid(r)))
    return out


def render_chef_pairs_table_md(
    pairs: list[dict[str, Any]],
    *,
    heading: str,
    intro: str,
) -> str:
    """
    Одна строка = один BUY, по которому посчитаны оба реплея выхода.
    Заголовки колонок на русском (для отчёта руководству).
    """
    lines: list[str] = []
    lines.append(f"{heading}\n\n")
    lines.append(intro + "\n\n")
    hdr = (
        "| Тикер | № BUY в БД | Дата и время входа | Цена входа (факт BUY) | "
        "№ SELL в БД (если уже был) | "
        "Дата и время тейка (реплей **5m**) | Цена тейка (реплей 5m) | "
        "Дата и время тейка (реплей **30m**) | Цена тейка (реплей 30m) | "
        "Разница цен тейка (5m−30m), USD | Разница log_ret (5m−30m) | "
        "Тип выхода 5m | Тип выхода 30m |\n"
    )
    sep = "|" + "|".join(["---"] * 12) + "|\n"
    lines.append(hdr + sep)
    for row in pairs:
        r5 = row["replay_5m"]
        r3 = row["replay_30m"]
        act = row.get("actual_sell")
        act_d = act if isinstance(act, dict) else None
        pdiff = _take_price_diff_usd(r5, r3)
        lines.append(
            f"| {_cell(row.get('ticker'))} | {_cell(row.get('buy_id'))} | {_cell(row.get('entry_ts'))} | "
            f"{_cell(row.get('entry_price'))} | "
            f"{_cell(act_d.get('id') if act_d else None)} | "
            f"{_cell(r5.get('bar_end_et'))} | {_cell(r5.get('exit_fill_price'))} | "
            f"{_cell(r3.get('bar_end_et'))} | {_cell(r3.get('exit_fill_price'))} | "
            f"{_cell(pdiff)} | {_cell(row.get('log_ret_diff_5m_minus_30m'))} | "
            f"{_cell(r5.get('signal_type'))} | {_cell(r3.get('signal_type'))} |\n"
        )
    lines.append("")
    return "".join(lines)


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
    lines.append(f"- **Сгенерировано (UTC):** `{payload.get('generated_at', '—')}`\n")
    lines.append(
        f"- **Параметр days:** **{payload.get('days', '—')}** "
        "(глубина BUY при обычном запуске; ширина окна ET для `--full-30m-sim`)\n"
    )
    lines.append(f"- **Биржа в market_bars_*:** `{payload.get('exchange', '—')}`\n")
    if payload.get("sim_30m_only"):
        lines.append(
            "- **Режим:** `--sim-30m-only` — реплей по `trade_history` **не** выполнялся (`rows` пустой).\n"
        )
    if "full_30m_sim_use_kb" in payload:
        lines.append(f"- **KB+VIX в 30m-симе:** `{payload.get('full_30m_sim_use_kb')}`\n")
    if payload.get("full_30m_sim_kb_days_arg") is not None:
        lines.append(f"- **Аргумент глубины KB:** `{payload.get('full_30m_sim_kb_days_arg')}`\n")
    win = payload.get("full_30m_window_et")
    if isinstance(win, dict) and (win.get("start") or win.get("end")):
        lines.append(f"- **Окно 30m-сим (ET):** `{win.get('start', '—')}` … `{win.get('end', '—')}`\n")
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

        pairs = _pairs_both_replays(rows)
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

            lines.append(
                render_chef_pairs_table_md(
                    pairs,
                    heading="### 1d. Полная таблица сделок с двумя реплеями тейка (для руководства)",
                    intro=(
                        "Здесь только строки, где **оба** реплея нашли выход (это **20** сделок при типичном недельном прогоне). "
                        "**Цена входа** — из фактического `BUY` в БД (одна на оба реплея). **Цены тейка** — модельные цены выхода из реплея "
                        "(для `TAKE_PROFIT` по правилам игры используется уровень по **high** бара). "
                        "**№ SELL** — идентификатор следующей записи продажи в `trade_history`, если она уже есть; даты тейка в реплее с ней не обязаны совпадать."
                    ),
                )
            )

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


def render_chef_only_md(payload: dict[str, Any]) -> str:
    """Только русская таблица §1d + краткий заголовок (для вставки в отчёт шефу)."""
    rows = payload.get("rows") or []
    pairs = _pairs_both_replays(rows)
    lines: list[str] = []
    lines.append("# GAME_5M: таблица сделок — тейк по реплею 5m и 30m при одном входе\n\n")
    lines.append(f"- **Источник JSON:** `generated_at` = `{payload.get('generated_at', '—')}`\n")
    lines.append(f"- **Строк в таблице:** **{len(pairs)}** (только `BUY`, у которых есть и `replay_5m`, и `replay_30m`).\n\n")
    if not pairs:
        lines.append("*Нет ни одной строки с обоими реплеями.* Проверьте JSON или запускайте бэктест без `--sim-30m-only`.\n")
        return "".join(lines)
    lines.append(
        render_chef_pairs_table_md(
            pairs,
            heading="## Полная таблица",
            intro=(
                "Одна строка = один фактический `BUY`. Колонки — то, что обычно просят в сводке по сделкам."
            ),
        )
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
    p.add_argument(
        "--mode",
        choices=("full", "chef"),
        default="full",
        help="full — весь отчёт; chef — только русская таблица 20 сделок (5m/30m тейк при одном входе)",
    )
    args = p.parse_args()

    path = Path(args.json_path)
    if not path.is_file():
        print(f"Нет файла: {path.resolve()}", file=sys.stderr)
        sys.exit(1)

    payload = json.loads(path.read_text(encoding="utf-8"))
    md = render_chef_only_md(payload) if args.mode == "chef" else render_md(payload)

    if args.out.strip():
        out = Path(args.out.strip())
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(out.resolve())
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    main()
