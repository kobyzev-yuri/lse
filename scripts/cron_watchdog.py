#!/usr/bin/env python3
"""
Watchdog: сканирует логи cron на предмет ошибок и пишет находки в свой лог.

Просматривает последние N строк каждого лог-файла в logs/ (или переданный каталог),
ищет строки с ERROR, Exception, Traceback, failed и т.п. и записывает их в
logs/cron_watchdog.log с указанием источника и времени проверки.

В Telegram отправляются только новые ошибки (ещё не отправленные в текущей серии).
Состояние хранится в logs/.cron_watchdog_sent.json — хеши уже отправленных строк;
при следующем запуске те же ошибки не дублируются. В лог cron_watchdog.log по-прежнему
пишутся все находки при каждом запуске.

При наличии ошибок можно отправить уведомление в Telegram (TELEGRAM_BOT_TOKEN и
TELEGRAM_SIGNAL_CHAT_IDS / TELEGRAM_SIGNAL_CHAT_ID). Включение: CRON_WATCHDOG_TELEGRAM=true
в config.env или флаг --telegram.

Запуск:
  python scripts/cron_watchdog.py              # dry-run в stdout
  python scripts/cron_watchdog.py --execute   # пишет в logs/cron_watchdog.log
  python scripts/cron_watchdog.py --execute --telegram  # + отправка в Telegram (только новых)

Рекомендуется в cron каждые 15–30 мин после основных задач, например:
  45 * * * * ... scripts/cron_watchdog.py --execute >> logs/cron_watchdog.log 2>&1
(при CRON_WATCHDOG_TELEGRAM=true тот же запуск отправит алерт в Telegram только по новым ошибкам.)
"""

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime

# Максимум хешей «уже отправлено» — чтобы файл не рос бесконечно; старые выкидываются
SENT_HASHES_MAX = 2000
SENT_STATE_FILENAME = ".cron_watchdog_sent.json"

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_config_value
from services.telegram_signal import get_signal_chat_ids, send_telegram_message

# Логи cron, которые смотрит watchdog (имена файлов относительно logs/)
CRON_LOG_FILES = [
    "cron_update_prices.log",
    "update_rsi_local.log",
    "update_finviz.log",
    "cron_trading_cycle.log",
    "cron_sndk_signal.log",
    "premarket_cron.log",
    "news_fetch.log",
    "sync_vector_kb.log",
    "add_sentiment_to_news.log",
    "analyze_event_outcomes.log",
    "cleanup_calendar_noise.log",
]

# Свой лог watchdog — не сканируем его
WATCHDOG_LOG_NAME = "cron_watchdog.log"

# Паттерны ошибок (регулярки; по одной на строку лога)
ERROR_PATTERNS = [
    re.compile(r"\bERROR\b", re.IGNORECASE),
    re.compile(r"\bCRITICAL\b", re.IGNORECASE),
    re.compile(r"\bException\b"),
    re.compile(r"\bTraceback\s*\("),
    re.compile(r"^Traceback\s*$", re.MULTILINE),
    re.compile(r"\bError:\s*"),
    re.compile(r"\bfailed\b", re.IGNORECASE),
    re.compile(r"\bSyntaxError\b"),
    re.compile(r"\bImportError\b"),
    re.compile(r"\bConnectionError\b"),
    re.compile(r"exit\s+code\s+[1-9]\d*", re.IGNORECASE),
    re.compile(r"^\s*raise\s+\w+Error", re.IGNORECASE),
]


def tail_lines(path: Path, n: int) -> list[str]:
    """Читает последние n строк файла (через tail, без загрузки всего файла)."""
    if not path.is_file():
        return []
    try:
        r = subprocess.run(
            ["tail", "-n", str(n), str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if r.returncode != 0:
            return []
        return r.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return []


def line_matches_error(line: str, include_warnings: bool) -> bool:
    """True, если строка похожа на ошибку."""
    for pat in ERROR_PATTERNS:
        if pat.search(line):
            return True
    if include_warnings and re.search(r"\bWARNING\b|ПРЕДУПРЕЖДЕНИЕ", line, re.IGNORECASE):
        return True
    return False


def scan_log_dir(
    logs_dir: Path,
    tail: int,
    include_warnings: bool,
    execute: bool,
) -> list[tuple[str, str, str]]:
    """
    Сканирует логи. Возвращает список (log_name, line_number, line_text).
    Если execute — пишет находки в logs/cron_watchdog.log.
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    findings: list[tuple[str, str, str]] = []

    for log_name in CRON_LOG_FILES:
        if log_name == WATCHDOG_LOG_NAME:
            continue
        log_path = logs_dir / log_name
        lines = tail_lines(log_path, tail)
        for i, line in enumerate(lines, start=1):
            # i — номер строки в прочитанном хвосте (последние tail строк файла)
            line_stripped = line.rstrip("\n\r")
            if not line_stripped.strip():
                continue
            if line_matches_error(line_stripped, include_warnings):
                # храним: имя файла, номер строки (примерный), текст (обрезанный)
                short = line_stripped[:500] + ("..." if len(line_stripped) > 500 else "")
                findings.append((log_name, str(i), short))

    if execute and findings:
        watchdog_log = logs_dir / WATCHDOG_LOG_NAME
        try:
            with open(watchdog_log, "a", encoding="utf-8") as w:
                w.write(f"\n--- cron_watchdog {now} ---\n")
                for log_name, line_no, text in findings:
                    w.write(f"[{log_name}:~{line_no}] {text}\n")
        except OSError as e:
            print(f"Не удалось записать в {watchdog_log}: {e}", file=sys.stderr)

    return findings


# Лимит длины сообщения Telegram
TELEGRAM_MAX_LEN = 4096


def _finding_hash(log_name: str, line_text: str) -> str:
    """Уникальный хеш для строки (лог + нормализованный текст), чтобы не слать одно и то же в Telegram повторно."""
    normal = (log_name + "\n" + (line_text[:400].strip() or "")).encode("utf-8", errors="replace")
    return hashlib.sha256(normal).hexdigest()


def _load_sent_hashes(logs_dir: Path) -> tuple[set[str], list[str]]:
    """Загружает (множество для поиска, список для дополнения и сохранения последних SENT_HASHES_MAX)."""
    path = logs_dir / SENT_STATE_FILENAME
    if not path.is_file():
        return set(), []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        lst = data if isinstance(data, list) else data.get("hashes", [])
        lst = lst[-SENT_HASHES_MAX:]
        return set(lst), list(lst)
    except Exception:
        return set(), []


def _save_sent_hashes(logs_dir: Path, sent_list: list[str]) -> None:
    """Сохраняет список хешей (обрезанный до SENT_HASHES_MAX с конца)."""
    path = logs_dir / SENT_STATE_FILENAME
    lst = sent_list[-SENT_HASHES_MAX:]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(lst, f, ensure_ascii=False)
    except OSError:
        pass


def send_watchdog_to_telegram(findings: list[tuple[str, str, str]], logs_dir: Path) -> bool:
    """Отправляет в Telegram краткую сводку найденных ошибок. Возвращает True если хотя бы одному чату отправлено."""
    token = get_config_value("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = get_signal_chat_ids()
    if not token or not chat_ids:
        return False
    lines = [f"Cron watchdog: в логах найдено {len(findings)} строк с ошибками."]
    for log_name, line_no, text in findings[:8]:
        snippet = (text[:180] + "...") if len(text) > 180 else text
        lines.append(f"[{log_name}:~{line_no}] {snippet}")
    if len(findings) > 8:
        lines.append(f"... и ещё {len(findings) - 8}. Подробнее: logs/cron_watchdog.log")
    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_LEN:
        text = text[: TELEGRAM_MAX_LEN - 50] + "\n... (обрезано)"
    logger = logging.getLogger(__name__)
    sent = False
    for cid in chat_ids:
        if send_telegram_message(token, cid, text, parse_mode=None):
            sent = True
    if sent:
        logger.info("Cron watchdog: алерт отправлен в Telegram")
        print("Уведомление отправлено в Telegram.", file=sys.stderr)
    return sent


def main():
    parser = argparse.ArgumentParser(
        description="Сканирование логов cron на ошибки, запись в cron_watchdog.log"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Писать находки в logs/cron_watchdog.log (иначе только в stdout)",
    )
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=None,
        help="Каталог логов (по умолчанию <проект>/logs)",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=500,
        help="Сколько последних строк каждого лога проверять (по умолчанию 500)",
    )
    parser.add_argument(
        "--include-warnings",
        action="store_true",
        help="Учитывать WARNING и ПРЕДУПРЕЖДЕНИЕ",
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="При находках отправить уведомление в Telegram (TELEGRAM_SIGNAL_CHAT_IDS)",
    )
    args = parser.parse_args()

    logs_dir = args.logs_dir or (project_root / "logs")
    if not logs_dir.is_dir():
        print(f"Каталог логов не найден: {logs_dir}", file=sys.stderr)
        sys.exit(1)

    findings = scan_log_dir(
        logs_dir=logs_dir,
        tail=args.tail,
        include_warnings=args.include_warnings,
        execute=args.execute,
    )

    use_telegram = args.telegram or (
        get_config_value("CRON_WATCHDOG_TELEGRAM", "").strip().lower() in ("1", "true", "yes")
    )

    if findings:
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[cron_watchdog] {now} — найдено строк с ошибками: {len(findings)}")
        for log_name, line_no, text in findings[:20]:
            print(f"  [{log_name}:~{line_no}] {text[:120]}...")
        if len(findings) > 20:
            print(f"  ... и ещё {len(findings) - 20}")
        if args.execute:
            print(f"Записано в {logs_dir / WATCHDOG_LOG_NAME}")
        if use_telegram:
            sent_set, sent_list = _load_sent_hashes(logs_dir)
            findings_new = [(n, no, t) for (n, no, t) in findings if _finding_hash(n, t) not in sent_set]
            if findings_new:
                send_watchdog_to_telegram(findings_new, logs_dir)
                for item in findings_new:
                    h = _finding_hash(item[0], item[2])
                    sent_set.add(h)
                    sent_list.append(h)
                _save_sent_hashes(logs_dir, sent_list)
            elif findings and not findings_new:
                print("В Telegram не отправлено: все текущие ошибки уже были отправлены ранее.", file=sys.stderr)
    else:
        print("[cron_watchdog] Ошибок в хвостах логов не обнаружено.")

    sys.exit(0)


if __name__ == "__main__":
    main()
