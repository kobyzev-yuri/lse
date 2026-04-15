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
  - external_id (если нет или это «slug» провайдера yfinance/newsapi — подменяем на SHA-256 от (exchange, symbol, url, title))
  - raw_payload (если нет — кладём исходный dict)

Вставка: INSERT ... ON CONFLICT DO NOTHING (по unique index external_id или (ticker, link), если применены миграции knowledge_pg/010).

Обновление существующих строк: по умолчанию exchange заполняется только если в KB пусто; если импортёр уже проставил NASDAQ/NYSE,
используйте --force-exchange. Для raw_payload по умолчанию мерж в raw_payload.nyse_jsonl (см. --no-merge-nyse-jsonl).
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


# Yahoo/NewsAPI кладут в «id» не статью, а имя провайдера — у всех строк одинаково → ломает UNIQUE(external_id).
_PROVIDER_SLUG_EXTERNAL_IDS = frozenset(
    {
        "yfinance",
        "yahoo",
        "newsapi",
        "news_api",
        "finnhub",
        "alphavantage",
        "alpha_vantage",
        "marketaux",
        "investing",
        "rss",
        "polygon",
    }
)


def _resolved_external_id(raw: str, exchange: str, symbol: str, link: str, title: str) -> str:
    r = (raw or "").strip()
    rl = r.lower()
    if not r or rl in _PROVIDER_SLUG_EXTERNAL_IDS or len(r) < 24:
        return _external_id(exchange, symbol, link, title)
    return r[:512]


def _raw_payload_json_str(item: Dict[str, Any], merge_nyse_jsonl: bool) -> str:
    """Один формат с UPDATE: при merge кладём статью под ключ nyse_jsonl (удобно для raw_payload ? 'nyse_jsonl')."""
    if merge_nyse_jsonl:
        return json.dumps({"nyse_jsonl": item}, ensure_ascii=False)
    return json.dumps(item, ensure_ascii=False)


def _legacy_ticker_for_kb(symbol: str) -> str:
    """Колонка knowledge_base.ticker VARCHAR(10): в фидах часто без '^', в Yahoo — '^VIX'."""
    s = _norm_symbol(symbol)
    return (s.lstrip("^")[:10] if s else "") or s[:10]


def _ticker_match_pair(symbol: str) -> tuple[str, str]:
    """Две формы для WHERE: как в JSONL и без ведущего '^'."""
    s = _norm_symbol(symbol)
    a = s[:10]
    b = s.lstrip("^")[:10]
    return (a, b if b != a else a)


def main() -> None:
    ap = argparse.ArgumentParser(description="Import news JSONL into knowledge_base with dedup")
    ap.add_argument("--in", dest="in_path", required=True, help="Путь к JSONL (1 строка = 1 JSON объект)")
    ap.add_argument("--exchange", default="NYSE", help="Биржа по умолчанию (если exchange отсутствует в строке)")
    ap.add_argument("--source", default="", help="Источник по умолчанию (если source отсутствует в строке)")
    ap.add_argument("--event-type", default="NEWS", help="event_type для knowledge_base")
    ap.add_argument("--importance", default="MEDIUM", help="importance для knowledge_base")
    ap.add_argument("--dry-run", action="store_true", help="Ничего не писать в БД, только статистика")
    ap.add_argument("--limit", type=int, default=0, help="Ограничить число строк (0 = без лимита)")
    ap.add_argument(
        "--force-exchange",
        action="store_true",
        help=(
            "При UPDATE всегда выставлять exchange из строки/--exchange. Иначе COALESCE: только если "
            "в KB пусто (часто в KB уже NASDAQ/NYSE из импортёра — тогда без этого флага count(* WHERE exchange='NYSE') почти не растёт)."
        ),
    )
    ap.add_argument(
        "--no-merge-nyse-jsonl",
        action="store_true",
        help="Не мержить JSON строки в raw_payload.nyse_jsonl (по умолчанию мерж включён, чтобы не терять NYSE-данные при уже заполненном raw_payload).",
    )
    args = ap.parse_args()
    merge_nyse_jsonl = not bool(args.no_merge_nyse_jsonl)

    in_path = Path(args.in_path).expanduser()
    if not in_path.is_absolute():
        in_path = Path.cwd() / in_path
    if not in_path.is_file():
        print(f"File not found: {in_path}", file=sys.stderr)
        sys.exit(2)

    engine = create_engine(get_database_url())
    inserted = 0
    enriched = 0
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
    if args.force_exchange:
        _ex_set = "exchange = :exchange"
    else:
        _ex_set = "exchange = COALESCE(NULLIF(BTRIM(exchange), ''), :exchange)"
    if merge_nyse_jsonl:
        _raw_set = (
            "raw_payload = COALESCE(raw_payload, '{}'::jsonb) "
            "|| jsonb_build_object('nyse_jsonl', CAST(:raw_payload AS jsonb))"
        )
    else:
        _raw_set = "raw_payload = COALESCE(raw_payload, CAST(:raw_payload AS jsonb))"

    sql_enrich = text(
        f"""
        UPDATE knowledge_base
        SET
          {_ex_set},
          symbol = COALESCE(NULLIF(BTRIM(symbol), ''), :symbol),
          external_id = COALESCE(NULLIF(BTRIM(external_id), ''), :external_id),
          content_sha256 = COALESCE(NULLIF(BTRIM(content_sha256), ''), :content_sha256),
          {_raw_set}
        WHERE
          :link IS NOT NULL
          AND length(trim(:link)) > 0
          AND link = :link
          AND (
            ticker = :ticker_a
            OR ticker = :ticker_b
            OR (symbol IS NOT NULL AND BTRIM(symbol) = BTRIM(:symbol))
          )
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
                raw_ext = str(_pick_first(item, ("external_id", "id", "provider_id")) or "").strip()
                ext = _resolved_external_id(raw_ext, exchange, symbol, link, title)

                content = _build_content(item)[:8000]
                if not content:
                    skipped += 1
                    continue
                content_sha = _sha256_hex(content)
                source = str(_pick_first(item, ("source", "provider", "site")) or args.source or "NYSETickerNews").strip()[:120]

                legacy_ticker = _legacy_ticker_for_kb(symbol) or symbol[:10]
                ta, tb = _ticker_match_pair(symbol)

                params = {
                    "ts": ts,
                    "ticker": legacy_ticker,
                    "source": source,
                    "content": content,
                    "event_type": str(args.event_type or "NEWS")[:32],
                    "importance": str(args.importance or "MEDIUM")[:16],
                    "link": link,
                    "exchange": exchange,
                    "symbol": symbol,
                    "external_id": ext[:512],
                    "content_sha256": content_sha,
                    "raw_payload": _raw_payload_json_str(item, merge_nyse_jsonl),
                    "ticker_a": ta,
                    "ticker_b": tb,
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
                        # Дубликат по unique (external_id или ticker+link). Обогащаем существующую строку
                        # новыми полями exchange/symbol/raw_payload, чтобы “NYSE-поток” не терялся.
                        try:
                            up = conn.execute(sql_enrich, params)
                            if getattr(up, "rowcount", 0) > 0:
                                enriched += int(getattr(up, "rowcount", 0) or 0)
                            else:
                                skipped += 1
                        except Exception:
                            skipped += 1
                except Exception:
                    errors += 1

    print(
        json.dumps(
            {
                "file": str(in_path),
                "processed": processed,
                "inserted": inserted,
                "enriched": enriched,
                "skipped": skipped,
                "errors": errors,
                "dry_run": bool(args.dry_run),
                "force_exchange": bool(args.force_exchange),
                "merge_nyse_jsonl_into_raw_payload": merge_nyse_jsonl,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

