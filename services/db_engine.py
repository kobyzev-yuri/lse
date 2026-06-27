# -*- coding: utf-8 -*-
"""Shared SQLAlchemy engine factory with bounded pools for web vs cron subprocesses."""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from config_loader import get_config_value, get_database_url


def _engine_pool_mode() -> str:
    raw = (get_config_value("LSE_DB_ENGINE_POOL", "") or "").strip().lower()
    if raw in ("null", "none", "cron", "script"):
        return "null"
    if raw in ("pooled", "web", "small"):
        return "pooled"
    runtime = (get_config_value("LSE_RUNTIME", "") or "").strip().lower()
    if runtime == "web":
        return "pooled"
    # Short-lived docker exec cron scripts: one connection, no idle pool slots.
    return "null"


def _engine_kwargs(mode: str) -> Dict[str, Any]:
    if mode == "null":
        return {"poolclass": NullPool, "pool_pre_ping": True}
    return {
        "pool_pre_ping": True,
        "pool_size": 2,
        "max_overflow": 2,
        "pool_recycle": 3600,
    }


@lru_cache(maxsize=1)
def get_db_engine() -> Engine:
    mode = _engine_pool_mode()
    return create_engine(get_database_url(), **_engine_kwargs(mode))


def dispose_db_engine() -> None:
    try:
        eng = get_db_engine()
        eng.dispose()
    except Exception:
        pass
    get_db_engine.cache_clear()
