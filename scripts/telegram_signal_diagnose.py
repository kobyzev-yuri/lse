#!/usr/bin/env python3
"""
Проверка рассылки сигналов в Telegram: какие файлы конфига участвуют, какие chat_id
эффективны, и (если есть сеть) getMe + getChat по Bot API.

Запуск из корня проекта (локально — тот же Python, что и крон, напр. conda py11):

  python scripts/telegram_signal_diagnose.py
  python scripts/telegram_signal_diagnose.py --skip-api   # только конфиг, без сети

На сервере в Docker (compose-сервис ``lse``, конфиг с хоста смонтирован в ``/app``):

  cd /path/to/lse && docker compose exec lse python scripts/telegram_signal_diagnose.py

Ошибка «Bad Request: chat not found» при sendMessage означает: для текущего
TELEGRAM_BOT_TOKEN чат с этим chat_id недоступен (другой бот, бот выкинут из
группы, неверный id, группа пересоздана и т.д.).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

import urllib.error
import urllib.parse
import urllib.request

from config_loader import (
    _parse_env_file,
    _secrets_overlay_path,
    _security_overlay_path,
    get_config_file_path,
    get_config_value,
)
from services.telegram_signal import get_signal_chat_ids, get_telegram_urllib_opener


def _tg_get(token: str, method: str, **params: str) -> dict:
    q = urllib.parse.urlencode(params) if params else ""
    url = f"https://api.telegram.org/bot{token}/{method}"
    if q:
        url += "?" + q
    req = urllib.request.Request(url, method="GET")
    with get_telegram_urllib_opener().open(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _mask_token(t: str) -> str:
    t = (t or "").strip()
    if len(t) < 12:
        return "(empty or short)"
    return f"{t[:6]}…{t[-4:]} (len={len(t)})"


def main() -> int:
    ap = argparse.ArgumentParser(description="Диагностика TELEGRAM_* для кронов сигналов")
    ap.add_argument(
        "--skip-api",
        action="store_true",
        help="Не вызывать api.telegram.org (офлайн: только пути и chat_id)",
    )
    args = ap.parse_args()

    primary = get_config_file_path()
    sec_path = _secrets_overlay_path()
    secu_path = _security_overlay_path()

    print("=== Конфиг (файлы) ===")
    print("primary config.env:", primary or "(не найден)")
    print("config.secrets.env:", sec_path or "(нет файла)")
    print("config.security.env:", secu_path or "(нет файла)")

    raw_primary: dict[str, str] = {}
    if primary and primary.is_file():
        raw_primary = _parse_env_file(primary)
    raw_secrets: dict[str, str] = {}
    if sec_path and sec_path.is_file():
        raw_secrets = _parse_env_file(sec_path)
    raw_security: dict[str, str] = {}
    if secu_path and secu_path.is_file():
        raw_security = _parse_env_file(secu_path)

    def line_sources(key: str) -> str:
        parts = []
        if (raw_primary.get(key) or "").strip():
            parts.append("config.env")
        if (raw_secrets.get(key) or "").strip():
            parts.append("secrets")
        if (raw_security.get(key) or "").strip():
            parts.append("security")
        return ", ".join(parts) if parts else "(нигде не задан в файлах)"

    print("\n=== Ключи Telegram (источник в файлах до merge) ===")
    for k in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_SIGNAL_CHAT_IDS",
        "TELEGRAM_SIGNAL_CHAT_ID",
        "TELEGRAM_DASHBOARD_CHAT_ID",
        "TELEGRAM_ALLOWED_USERS",
    ):
        print(f"  {k}: {line_sources(k)}")

    token = (get_config_value("TELEGRAM_BOT_TOKEN") or "").strip()
    ids = get_signal_chat_ids()

    print("\n=== Эффективные значения (get_config_value / get_signal_chat_ids) ===")
    print("TELEGRAM_BOT_TOKEN:", _mask_token(token))
    print("chat_id для рассылки (%d):" % len(ids), ", ".join(repr(x) for x in ids) if ids else "(пусто)")
    proxy = (get_config_value("TELEGRAM_PROXY_URL") or "").strip()
    print(
        "TELEGRAM_PROXY_URL:",
        proxy if proxy else "(пусто — прямое подключение, как на сервере)",
    )

    if args.skip_api:
        print("\n--skip-api: запросы к Telegram не выполнялись.")
        return 0

    if not token:
        print("\nНет токена — getMe/getChat пропущены.")
        return 2

    print("\n=== Telegram Bot API ===")
    try:
        r = _tg_get(token, "getMe")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:600]
        print("getMe HTTP", e.code, body)
        return 1
    except OSError as e:
        print("getMe: сеть/DNS недоступны:", e)
        print(
            "Запустите на сервере с прямым доступом к api.telegram.org "
            "или локально задайте в config.env TELEGRAM_PROXY_URL=http://127.0.0.1:8081 "
            "(HTTP-прокси к Telegram; SOCKS5 в urllib для кронов не используется)."
        )
        return 1
    except Exception as e:
        print("getMe:", e)
        return 1

    if not r.get("ok"):
        print("getMe ответ не ok:", r)
        return 1
    u = r.get("result") or {}
    print(
        "getMe OK: @%s (bot user id=%s)"
        % (u.get("username", "?"), u.get("id", "?"))
    )

    if not ids:
        print("Нет chat_id — getChat не вызывается.")
        return 0

    rc = 0
    for cid in ids:
        try:
            r = _tg_get(token, "getChat", chat_id=cid)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:800]
            print("getChat chat_id=%r -> HTTP %s %s" % (cid, e.code, body))
            if e.code == 400 and "chat not found" in body.lower():
                print(
                    "  -> Для ЭТОГО бота чат не найден: проверьте, что бот в группе / пользователь нажал /start,"
                    " что id супергруппы с префиксом -100, и что тот же токен, что у @BotFather для этого бота."
                )
            rc = 1
            continue
        except Exception as e:
            print("getChat chat_id=%r -> %s" % (cid, e))
            rc = 1
            continue
        if not r.get("ok"):
            print("getChat chat_id=%r ->" % cid, r)
            rc = 1
            continue
        ch = r["result"]
        print(
            "getChat OK: id=%s type=%s title=%r"
            % (ch.get("id"), ch.get("type"), ch.get("title") or ch.get("username") or "")
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
