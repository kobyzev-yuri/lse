#!/usr/bin/env python3
"""
Снимок JSON отчёта анализатора (как GET /api/analyzer).

Два режима:
1) Локальный расчёт — импорт services.* (нужны зависимости: pip install -r requirements.txt в venv).
2) HTTP — только стандартная библиотека; удобно для cron на хосте без numpy/pandas.

  export ANALYZER_SNAPSHOT_URL=http://127.0.0.1:8080/api/analyzer
  cd ~/lse && python3 scripts/snapshot_analyzer_report.py --days 7

Если в окружении задан ANALYZER_SNAPSHOT_URL, по умолчанию снимок всегда идёт по HTTP
(ответ старого контейнера без git pull). Чтобы принудительно считать из кода на диске:
  env -u ANALYZER_SNAPSHOT_URL python3 scripts/snapshot_analyzer_report.py --days 7
  # или
  python3 scripts/snapshot_analyzer_report.py --local --days 7

Пример crontab (хост без venv, веб слушает 8080):
  30 6 * * * cd /home/USER/lse && ANALYZER_SNAPSHOT_URL=http://127.0.0.1:8080/api/analyzer \\
    python3 scripts/snapshot_analyzer_report.py --days 7 >> logs/analyzer_snapshot.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

project_root = Path(__file__).resolve().parent.parent


def _fetch_payload_via_http(
    base_url: str,
    *,
    days: int,
    strategy: str,
    use_llm: bool,
    include_trade_details: bool,
    timeout_sec: int,
) -> Dict[str, Any]:
    """base_url — полный путь к эндпоинту, например http://127.0.0.1:8080/api/analyzer"""
    q = urllib.parse.urlencode(
        {
            "days": str(days),
            "strategy": strategy,
            "use_llm": "1" if use_llm else "0",
            "include_trade_details": "1" if include_trade_details else "0",
        }
    )
    url = base_url.strip()
    if not url:
        raise ValueError("empty URL")
    sep = "&" if "?" in url else "?"
    full = url + sep + q
    req = urllib.request.Request(full, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if isinstance(data, dict) and "detail" in data and len(data) <= 3:
        raise RuntimeError(f"API error: {data!r}")
    if not isinstance(data, dict):
        raise RuntimeError("unexpected JSON root")
    return data


def _payload_local(
    *,
    days: int,
    strategy: str,
    use_llm: bool,
    include_trade_details: bool,
) -> tuple[Dict[str, Any], str]:
    sys.path.insert(0, str(project_root))
    from services import trade_effectiveness_analyzer as tea

    payload = tea.analyze_trade_effectiveness(
        days=days,
        strategy=strategy,
        use_llm=use_llm,
        include_trade_details=include_trade_details,
    )
    return payload, str(Path(tea.__file__).resolve())


def main() -> None:
    parser = argparse.ArgumentParser(description="Сохранить JSON отчёт анализатора в каталог снимков")
    parser.add_argument("--days", type=int, default=7, help="Окно сделок, дней (1–30)")
    parser.add_argument("--strategy", type=str, default="GAME_5M", help="Стратегия")
    parser.add_argument(
        "--local",
        action="store_true",
        help="Считать отчёт импортом services.* на диске; игнорировать ANALYZER_SNAPSHOT_URL в env",
    )
    parser.add_argument(
        "--url",
        type=str,
        default="",
        help="Полный URL GET /api/analyzer (перекрывает env ANALYZER_SNAPSHOT_URL); только stdlib",
    )
    parser.add_argument(
        "--http-timeout",
        type=int,
        default=180,
        help="Таймаут HTTP-запроса, сек (для --url / ANALYZER_SNAPSHOT_URL)",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default="",
        help="Каталог для файлов (по умолчанию env ANALYZER_SNAPSHOT_DIR или local/analyzer_snapshots в корне репо)",
    )
    parser.add_argument("--llm", action="store_true", help="Включить LLM (дорого для ежедневного cron)")
    parser.add_argument(
        "--no-trade-details",
        action="store_true",
        help="Не добавлять trade_effects (меньше файлы)",
    )
    parser.add_argument("--quiet", action="store_true", help="Не печатать путь в stdout")
    args = parser.parse_args()

    days = max(1, min(30, int(args.days)))
    strategy = (args.strategy or "GAME_5M").strip().upper()

    raw_dir = (args.out_dir or os.environ.get("ANALYZER_SNAPSHOT_DIR") or "").strip()
    if raw_dir:
        out_dir = Path(raw_dir)
    else:
        out_dir = project_root / "local" / "analyzer_snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"analyzer_{strategy}_{days}d_{ts}.json"
    out_path = out_dir / name

    env_url = (os.environ.get("ANALYZER_SNAPSHOT_URL") or "").strip()
    if args.local:
        http_url = ""
    elif (args.url or "").strip():
        http_url = (args.url or "").strip()
    else:
        http_url = env_url
    include_td = not bool(args.no_trade_details)

    try:
        if http_url:
            if not args.quiet:
                print(f"[snapshot] источник: HTTP {http_url}", file=sys.stderr)
            payload = _fetch_payload_via_http(
                http_url,
                days=days,
                strategy=strategy,
                use_llm=bool(args.llm),
                include_trade_details=include_td,
                timeout_sec=max(30, int(args.http_timeout)),
            )
        else:
            try:
                payload, mod_path = _payload_local(
                    days=days,
                    strategy=strategy,
                    use_llm=bool(args.llm),
                    include_trade_details=include_td,
                )
                if not args.quiet:
                    hint = f" (в env был ANALYZER_SNAPSHOT_URL — проигнорирован благодаря --local)" if args.local and env_url else ""
                    print(f"[snapshot] источник: локальный модуль {mod_path}{hint}", file=sys.stderr)
            except ImportError as e:
                vpy = project_root / ".venv" / "bin" / "python3"
                vhint = (
                    f"\n  Уже есть venv: {vpy} scripts/snapshot_analyzer_report.py --local --days {days}\n"
                    if vpy.is_file()
                    else ""
                )
                print(
                    "Локальный снимок (без ANALYZER_SNAPSHOT_URL / с --local) тянет анализатор из этого репо — "
                    "нужны зависимости как в requirements.txt (numpy, pandas, …).\n"
                    "  1) Один раз в корне репо:\n"
                    "       cd ~/lse && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt\n"
                    "     затем всегда:\n"
                    "       . .venv/bin/activate && env -u ANALYZER_SNAPSHOT_URL python3 scripts/snapshot_analyzer_report.py --days 7\n"
                    f"{vhint}"
                    "  2) Без venv на хосте: HTTP (только stdlib), но JSON = версия кода **в контейнере**:\n"
                    "       export ANALYZER_SNAPSHOT_URL=http://127.0.0.1:ПОРТ/api/analyzer\n"
                    "     После git pull на хосте пересоберите образ / перезапустите сервис, иначе отчёт останется старым.\n"
                    f"Исходная ошибка: {e}",
                    file=sys.stderr,
                )
                sys.exit(1)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        print(f"HTTP {e.code}: {body[:2000]}", file=sys.stderr)
        sys.exit(2)
    except urllib.error.URLError as e:
        print(f"URL error: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(3)

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    latest = out_dir / "latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        print(str(out_path))
        print(str(latest))


if __name__ == "__main__":
    main()
