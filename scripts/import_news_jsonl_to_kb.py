#!/usr/bin/env python3
"""
Импорт новостей из JSONL в knowledge_base (LSE) с дедупом.

Назначение: безопасно переносить поток NYSE/tradenews в LSE постепенно, не ломая текущие импортеры.

Ожидаемый формат JSONL: одна JSON-строка = одна статья/событие, произвольная схема.
Поддерживаемые поля (если есть):
  - ts / published / published_at / datetime / time (ISO-8601 или epoch seconds)
  - ticker / symbol
  - exchange (иначе берём --exchange)
  - source / provider / site
  - title / summary / content / body / text
  - url / link
  - external_id (если нет — генерируем детерминированно из (exchange, symbol, url, title))
  - raw_payload (если нет — кладём исходный dict)

Вставка: INSERT ... ON CONFLICT DO NOTHING (по unique index external_id или (ticker, link), если применены миграции knowledge_pg/010).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text  # noqa: E402

from config_loader import get_database_url  # noqa: E402


def _coerce_ts(v: Any) -> datetime:
    if v is None or v == "":
        return datetime.now(timezone.utc)
    # epoch seconds
    if isinstance(v, (int, float)) and v > 0:
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    s = str(v).strip()
    # numeric epoch in string
    if re.fullmatch(r"\d{10}(\.\d+)?", s):
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    # ISO-8601 (handle Z)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _pick_first(d: Dict[str, Any], keys: Iterable[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _norm_symbol(v: Any) -> str:
    s = (str(v or "").strip().upper() or "").strip()
    return s


def _build_content(item: Dict[str, Any]) -> str:
    title = str(_pick_first(item, ("title", "headline")) or "").strip()
    summary = str(_pick_first(item, ("summary", "description", "snippet")) or "").strip()
    body = str(_pick_first(item, ("content", "body", "text")) or "").strip()
    link = str(_pick_first(item, ("url", "link")) or "").strip()
    parts = []
    if title:
        parts.append(title)
    if summary and summary != title:
        parts.append(summary)
    if body and body not in (title, summary):
        parts.append(body)
    if link:
        parts.append(link)
    out = "\n\n".join([p for p in parts if p])
    return out.strip()


def _sha256_hex(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="ignore")).hexdigest()


def _external_id(exchange: str, symbol: str, link: str, title: str) -> str:
    base = f"{exchange}|{symbol}|{link.strip()}|{title.strip()}"
    return _sha256_hex(base)


def main() -> None:
    ap = argparse.ArgumentParser(description="Import news JSONL into knowledge_base with dedup")
    ap.add_argument("--in", dest="in_path", required=True, help="Путь к JSONL (1 строка = 1 JSON объект)")
    ap.add_argument("--exchange", default="NYSE", help="Биржа по умолчанию (если exchange отсутствует в строке)")
    ap.add_argument("--source", default="", help="Источник по умолчанию (если source отсутствует в строке)")
    ap.add_argument("--event-type", default="NEWS", help="event_type для knowledge_base")
    ap.add_argument("--importance", default="MEDIUM", help="importance для knowledge_base")
    ap.add_argument("--dry-run", action="store_true", help="Ничего не писать в БД, только статистика")
    ap.add_argument("--limit", type=int, default=0, help="Ограничить число строк (0 = без лимита)")
    args = ap.parse_args()

    in_path = Path(args.in_path).expanduser()
    if not in_path.is_absolute():
        in_path = Path.cwd() / in_path
    if not in_path.is_file():
        print(f"File not found: {in_path}", file=sys.stderr)
        sys.exit(2)

    engine = create_engine(get_database_url())
    inserted = 0
    skipped = 0
    errors = 0
    processed = 0

    sql = text(
        """
        INSERT INTO knowledge_base
          (ts, ticker, source, content, event_type, importance, link, ingested_at,
           exchange, symbol, external_id, content_sha256, raw_payload)
        VALUES
          (:ts, :ticker, :source, :content, :event_type, :importance, :link, NOW(),
           :exchange, :symbol, :external_id, :content_sha256, :raw_payload)
        ON CONFLICT DO NOTHING
        """
    )

    with in_path.open("r", encoding="utf-8", errors="replace") as f:
        with engine.begin() as conn:
            for line in f:
                if args.limit and processed >= int(args.limit):
                    break
                s = line.strip()
                if not s:
                    continue
                processed += 1
                try:
                    item = json.loads(s)
                    if not isinstance(item, dict):
                        skipped += 1
                        continue
                except Exception:
                    errors += 1
                    continue

                ts = _coerce_ts(_pick_first(item, ("ts", "published", "published_at", "datetime", "time")))
                symbol = _norm_symbol(_pick_first(item, ("symbol", "ticker")))
                if not symbol:
                    skipped += 1
                    continue
                exchange = str(_pick_first(item, ("exchange",)) or args.exchange or "NYSE").strip().upper()[:16]
                link = str(_pick_first(item, ("url", "link")) or "").strip()[:2000]
                title = str(_pick_first(item, ("title", "headline")) or "").strip()
                ext = str(_pick_first(item, ("external_id", "id", "provider_id")) or "").strip()
                if not ext:
                    ext = _external_id(exchange, symbol, link, title)

                content = _build_content(item)[:8000]
                if not content:
                    skipped += 1
                    continue
                content_sha = _sha256_hex(content)
                source = str(_pick_first(item, ("source", "provider", "site")) or args.source or "NYSETickerNews").strip()[:120]

                params = {
                    "ts": ts,
                    "ticker": symbol[:10],  # legacy
                    "source": source,
                    "content": content,
                    "event_type": str(args.event_type or "NEWS")[:32],
                    "importance": str(args.importance or "MEDIUM")[:16],
                    "link": link,
                    "exchange": exchange,
                    "symbol": symbol,
                    "external_id": ext[:512],
                    "content_sha256": content_sha,
                    "raw_payload": json.dumps(item, ensure_ascii=False),
                }
                if args.dry_run:
                    inserted += 1
                    continue
                try:
                    res = conn.execute(sql, params)
                    # rowcount=1 если вставилось, 0 если конфликт
                    if getattr(res, "rowcount", 0) == 1:
                        inserted += 1
                    else:
                        skipped += 1
                except Exception:
                    errors += 1

    print(
        json.dumps(
            {
                "file": str(in_path),
                "processed": processed,
                "inserted": inserted,
                "skipped": skipped,
                "errors": errors,
                "dry_run": bool(args.dry_run),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

