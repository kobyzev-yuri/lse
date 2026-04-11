#!/usr/bin/env python3
"""
Список кандидатов для ручного цикла «снимок → выбрать один параметр → применить → ждать эффект».

Читает JSON анализатора (по умолчанию local/analyzer_snapshots/latest.json) и печатает:
  1) строки из auto_config_override.updates с индексом для analyzer_tune_apply.py --index N;
  2) подсказки без готового env (practical_parameter_suggestions, game_5m_config_hints, critical_case_analysis).

Не пишет config.env — только вывод для выбора человеком.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import is_editable_config_env_key


def _skip_set() -> set[str]:
    raw = (os.environ.get("ANALYZER_TUNE_SKIP_KEYS") or "").strip()
    if not raw:
        return set()
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def _load(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Кандидаты из снимка анализатора для ручного тюнинга")
    ap.add_argument(
        "--from-json",
        type=Path,
        default=project_root / "local" / "analyzer_snapshots" / "latest.json",
        help="Путь к JSON снимку (по умолчанию local/analyzer_snapshots/latest.json)",
    )
    ap.add_argument("--json", action="store_true", help="Вывести структурированный JSON в stdout")
    args = ap.parse_args()
    path = args.from_json
    if not path.is_file():
        print(f"Файл не найден: {path}", file=sys.stderr)
        return 1

    data = _load(path)
    skip = _skip_set()

    auto = data.get("auto_config_override") if isinstance(data.get("auto_config_override"), dict) else {}
    updates = auto.get("updates") if isinstance(auto.get("updates"), list) else []
    apply_rows: List[Dict[str, Any]] = []
    for i, u in enumerate(updates):
        if not isinstance(u, dict):
            continue
        key = (u.get("env_key") or "").strip().upper()
        proposed = u.get("proposed")
        reason = u.get("reason") or ""
        editable = bool(key and is_editable_config_env_key(key))
        skipped = key in skip
        apply_rows.append(
            {
                "tune_apply_index": i,
                "env_key": key or None,
                "proposed": proposed,
                "reason": reason,
                "editable": editable,
                "skipped_by_env": skipped,
            }
        )

    practical = data.get("practical_parameter_suggestions")
    if not isinstance(practical, list):
        practical = []

    hints = data.get("game_5m_config_hints")
    if not isinstance(hints, list):
        hints = []

    critical = data.get("critical_case_analysis")
    if not isinstance(critical, list):
        critical = []

    if args.json:
        out = {
            "source": str(path),
            "auto_config_updates": apply_rows,
            "practical_parameter_suggestions": practical,
            "game_5m_config_hints": hints,
            "critical_case_analysis": critical,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"Источник: {path}\n")
    print("=== Применение через analyzer_tune_apply.py (один шаг за раз) ===\n")
    if not apply_rows:
        print("  (нет auto_config_override.updates в этом снимке)\n")
    else:
        for r in apply_rows:
            i = r["tune_apply_index"]
            key = r["env_key"] or "?"
            prop = r["proposed"]
            reason = (r["reason"] or "").replace("\n", " ").strip()[:200]
            flags: List[str] = []
            if not r["editable"]:
                flags.append("не в белом списке редактируемых — tune_apply пропустит")
            if r["skipped_by_env"]:
                flags.append("ANALYZER_TUNE_SKIP_KEYS")
            suffix = f"  [{'; '.join(flags)}]" if flags else ""
            print(f"  [{i}] {key}  →  {prop!r}")
            if reason:
                print(f"      {reason}{suffix}")
            else:
                print(f"      {suffix}".rstrip())
            print(
                f"      python3 scripts/analyzer_tune_apply.py --from-json {path} --index {i} --dry-run"
            )
            print()

    print("=== Подсказки без готового env в updates (ручная правка / следующий отчёт) ===\n")
    if practical:
        print("  practical_parameter_suggestions:")
        for p in practical[:20]:
            if isinstance(p, dict):
                par = p.get("parameter") or p.get("name") or "?"
                prop = p.get("proposed")
                why = (p.get("why") or p.get("rationale") or "")[:160]
                print(f"    - {par}: {prop!r} — {why}")
            else:
                print(f"    - {p}")
        if len(practical) > 20:
            print(f"    ... ещё {len(practical) - 20}")
        print()
    else:
        print("  (нет practical_parameter_suggestions)\n")

    if hints:
        print("  game_5m_config_hints:")
        for h in hints[:15]:
            if isinstance(h, dict):
                print(f"    - {h}")
            else:
                print(f"    - {h}")
        if len(hints) > 15:
            print(f"    ... ещё {len(hints) - 15}")
        print()
    else:
        print("  (нет game_5m_config_hints в этом снимке)\n")

    if critical:
        print("  critical_case_analysis (выборочно):")
        for c in critical[:8]:
            if isinstance(c, dict):
                tid = c.get("trade_id") or c.get("ticker")
                act = c.get("action") or c.get("suggested_action") or ""
                print(f"    - {tid}: {str(act)[:120]}")
            else:
                print(f"    - {str(c)[:120]}")
        if len(critical) > 8:
            print(f"    ... ещё {len(critical) - 8}")
        print()

    print(
        "Дальше: убрать --dry-run при применении, перезапуск по вашему RESTART_CMD, "
        "через период — новый snapshot + diff_analyzer_snapshots.py."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
