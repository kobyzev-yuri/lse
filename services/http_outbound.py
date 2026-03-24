"""
Исходящие HTTP-запросы (RSS, NewsAPI, календарь и т.д.).

По умолчанию session.trust_env=False — не читать HTTP(S)_PROXY / ALL_PROXY из окружения.
Иначе при ALL_PROXY=socks://... без PySocks получают «Missing dependencies for SOCKS support».

Локально за SOCKS: в config.env задать соответствующий *_USE_SYSTEM_PROXY=true и установить PySocks
(`pip install PySocks` в py11). На сервере без прокси — ничего не задавать (или false).
"""

from __future__ import annotations

import requests

from config_loader import get_config_value


def outbound_session(use_system_proxy_config_key: str) -> requests.Session:
    """
    Args:
        use_system_proxy_config_key: имя ключа в config.env, например RSS_USE_SYSTEM_PROXY.
            true/1/yes — учитывать переменные прокси окружения.
    """
    s = requests.Session()
    raw = (get_config_value(use_system_proxy_config_key, "false") or "false").strip().lower()
    s.trust_env = raw in ("1", "true", "yes")
    return s
