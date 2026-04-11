#!/usr/bin/env python3
"""Сравнить два JSON отчёта анализатора (ключевые поля summary + число сделок)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict


def _summary(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    s = data.get("summary") if isinstance(data, dict) else None
    return s if isinstance(s, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Diff двух analyzer JSON по summary")
    parser.add_argument("before", type=str, help="JSON до изменений")
    parser.add_argument("after", type=str, help="JSON после")
    args = parser.parse_args()

    a = Path(args.before).expanduser()
    b = Path(args.after).expanduser()
    if not a.is_file() or not b.is_file():
        print("Оба аргумента должны быть файлами", file=sys.stderr)
        sys.exit(1)

    sa, sb = _summary(a), _summary(b)
    keys = sorted(set(sa) | set(sb))
    rows = []
    for k in keys:
        if k == "by_exit_signal":
            continue
        va, vb = sa.get(k), sb.get(k)
        if va != vb:
            rows.append((k, va, vb))
    print(f"before: {a}")
    print(f"after:  {b}")
    print()
    if not rows:
        print("summary: идентичны (по скалярным ключам)")
        return
    for k, va, vb in rows:
        print(f"  {k}:")
        print(f"    {va!r}  ->  {vb!r}")


if __name__ == "__main__":
    main()
