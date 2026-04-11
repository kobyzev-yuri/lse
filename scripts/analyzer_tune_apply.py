#!/usr/bin/env python3
"""
Шаг настройки: применить одно (или все) предложение из auto_config_override сохранённого JSON
и записать состояние «ожидаем сравнение со следующим снимком».

«Ждать результат» в смысле cron: не блокирует процесс на дни — фиксирует applied_at и список ключей;
следующий snapshot + scripts/diff_analyzer_snapshots.py (или ручной diff) показывает эффект.

Пропуск ключей: env ANALYZER_TUNE_SKIP_KEYS=GAME_5M_SIGNAL_CRON_MINUTES,OTHER_KEY
(удобно, если крон уже поминутный и менять GAME_5M_SIGNAL_CRON_MINUTES не нужно).

Устаревший снимок (старый анализатор): по умолчанию реальная запись в config.env блокируется;
см. --force-stale-snapshot или сначала переснять JSON (snapshot_analyzer_report / API после деплоя).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

project_root = Path(__file__).resolve().parent.parent
_scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(_scripts_dir))

from analyzer_snapshot_staleness import snapshot_staleness_warnings
from config_loader import is_editable_config_env_key, update_config_key


def _skip_set() -> set[str]:
    raw = (os.environ.get("ANALYZER_TUNE_SKIP_KEYS") or "").strip()
    if not raw:
        return set()
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def _load_report(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _updates_from_report(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    auto = data.get("auto_config_override") if isinstance(data.get("auto_config_override"), dict) else None
    if not isinstance(auto, dict):
        return []
    upd = auto.get("updates")
    return upd if isinstance(upd, list) else []


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Применить предложения из analyzer JSON к config.env (по одному или пакетом)"
    )
    parser.add_argument(
        "--from-json",
        type=str,
        required=True,
        help="Путь к сохранённому отчёту analyzer (с auto_config_override.updates)",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=-1,
        help="Индекс в updates (0-based). По умолчанию -1 = применить все допустимые",
    )
    parser.add_argument("--dry-run", action="store_true", help="Только показать, что сделали бы")
    parser.add_argument(
        "--force-stale-snapshot",
        action="store_true",
        help="Разрешить запись в config.env, даже если JSON похож на снимок со старого анализатора",
    )
    parser.add_argument(
        "--state-dir",
        type=str,
        default="",
        help="Куда писать tune_state.json (по умолчанию каталог с JSON или ANALYZER_SNAPSHOT_DIR)",
    )
    args = parser.parse_args()

    src = Path(args.from_json).expanduser()
    if not src.is_absolute():
        src = Path.cwd() / src
    if not src.is_file():
        print(f"Файл не найден: {src}", file=sys.stderr)
        sys.exit(1)

    report = _load_report(src)
    updates = _updates_from_report(report)
    if not updates:
        print("Нет auto_config_override.updates в JSON", file=sys.stderr)
        sys.exit(2)

    stale = snapshot_staleness_warnings(report)
    if stale and not args.dry_run and not args.force_stale_snapshot:
        print(
            "Отказ: снимок похож на устаревший (рекомендации могут не соответствовать текущему коду анализатора).",
            file=sys.stderr,
        )
        for line in stale:
            print(f"  • {line}", file=sys.stderr)
        print(
            "Переснимите JSON, затем повторите; или явно: --force-stale-snapshot",
            file=sys.stderr,
        )
        sys.exit(5)
    if stale and args.dry_run:
        print("Предупреждение (устаревший снимок):", file=sys.stderr)
        for line in stale:
            print(f"  • {line}", file=sys.stderr)

    skip = _skip_set()
    if args.index >= 0:
        if args.index >= len(updates):
            print(f"index {args.index} вне диапазона (0..{len(updates) - 1})", file=sys.stderr)
            sys.exit(3)
        to_apply = [updates[args.index]]
    else:
        to_apply = list(updates)

    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for row in to_apply:
        if not isinstance(row, dict):
            continue
        key = str(row.get("env_key") or "").strip()
        proposed = row.get("proposed")
        if proposed is None:
            skipped.append({"env_key": key or "?", "reason": "no_proposed"})
            continue
        proposed_str = str(proposed).strip()
        if not key:
            skipped.append({"env_key": "", "reason": "empty_key"})
            continue
        ku = key.upper()
        if ku in skip:
            skipped.append({"env_key": key, "reason": "skip_list_env_ANALYZER_TUNE_SKIP_KEYS"})
            continue
        if not is_editable_config_env_key(key):
            skipped.append({"env_key": key, "reason": "not_editable"})
            continue
        if args.dry_run:
            applied.append({"env_key": key, "proposed": proposed_str, "dry_run": True})
            continue
        ok = update_config_key(key, proposed_str)
        if not ok:
            skipped.append({"env_key": key, "reason": "write_failed"})
            continue
        applied.append(
            {
                "env_key": key,
                "proposed": proposed_str,
                "reason": str(row.get("reason") or row.get("source_parameter") or ""),
            }
        )

    state_dir = (args.state_dir or os.environ.get("ANALYZER_SNAPSHOT_DIR") or "").strip()
    if state_dir:
        sd = Path(state_dir)
    else:
        sd = src.parent
    sd.mkdir(parents=True, exist_ok=True)
    state_path = sd / "tune_state.json"

    state = {
        "applied_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_report": str(src.resolve()),
        "dry_run": bool(args.dry_run),
        "applied": applied,
        "skipped": skipped,
        "note": "Сравните summary/trade_effects со следующим снимком после N дней торговли.",
    }
    if not args.dry_run:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(state, ensure_ascii=False, indent=2))
    if skipped and not applied:
        sys.exit(4)


if __name__ == "__main__":
    main()
