#!/usr/bin/env python3
"""
LLM extraction from parsed earnings_material rows → earnings_event_detail.

Examples:
  python scripts/extract_earnings_material_facts.py --dry-run --symbols META,NVDA
  python scripts/extract_earnings_material_facts.py --symbol META --event-date 2026-04-29
  python scripts/extract_earnings_material_facts.py --symbols META,NVDA --limit 5
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.earnings_intelligence_universe import get_earnings_intelligence_universe  # noqa: E402
from services.earnings_material_extractor import (  # noqa: E402
    extract_event_facts_with_llm,
    map_extraction_to_event_detail,
    plan_event_extraction_tokens,
    select_materials_for_event,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()


def _load_materials(
    engine,
    *,
    symbols: set[str] | None,
    event_date: date | None,
    since: date | None,
) -> list[dict[str, Any]]:
    where = ["parse_status IN ('parsed', 'extracted')"]
    params: dict[str, Any] = {}
    if symbols:
        where.append("UPPER(TRIM(symbol)) = ANY(:symbols)")
        params["symbols"] = sorted(symbols)
    if event_date:
        where.append("event_date = :event_date")
        params["event_date"] = event_date
    if since:
        where.append("event_date >= :since")
        params["since"] = since
    q = text(
        f"""
        SELECT
          id, knowledge_base_id, symbol, event_date, fiscal_period,
          material_type, source_name, source_url, title,
          parse_status, content_text, meta
        FROM earnings_material
        WHERE {' AND '.join(where)}
        ORDER BY symbol, event_date DESC NULLS LAST, id ASC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, params).mappings().all()
    return [dict(r) for r in rows]


def _resolve_kb_id(engine, *, symbol: str, event_date: date | None, fallback: int | None) -> int | None:
    if fallback:
        return int(fallback)
    if not event_date:
        return None
    q = text(
        """
        SELECT id
        FROM knowledge_base
        WHERE UPPER(TRIM(ticker)) = :symbol
          AND ts::date = :event_date
          AND UPPER(COALESCE(event_type, '')) LIKE '%EARNING%'
        ORDER BY id DESC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(
            q,
            {"symbol": symbol.strip().upper(), "event_date": event_date},
        ).first()
    return int(row[0]) if row else None


def _detail_has_llm_extraction(engine, kb_id: int) -> bool:
    q = text(
        """
        SELECT guidance_summary
        FROM earnings_event_detail
        WHERE knowledge_base_id = :kb_id
        """
    )
    with engine.connect() as conn:
        row = conn.execute(q, {"kb_id": kb_id}).first()
    if not row or not row[0]:
        return False
    gs = row[0]
    if isinstance(gs, str):
        try:
            gs = json.loads(gs)
        except Exception:
            return False
    return bool(isinstance(gs, dict) and gs.get("extraction_meta"))
    guidance = dict(payload.get("guidance_summary") or {})
    guidance["extraction_meta"] = extraction_meta
    q = text(
        """
        INSERT INTO earnings_event_detail (
          knowledge_base_id, fiscal_period,
          revenue_actual, revenue_estimate, eps_actual, eps_estimate,
          guidance_summary, affected_tickers, updated_at
        )
        VALUES (
          :kb_id, :fiscal_period,
          :revenue_actual, :revenue_estimate, :eps_actual, :eps_estimate,
          CAST(:guidance_summary AS jsonb), CAST(:affected_tickers AS jsonb), NOW()
        )
        ON CONFLICT (knowledge_base_id) DO UPDATE SET
          fiscal_period = COALESCE(EXCLUDED.fiscal_period, earnings_event_detail.fiscal_period),
          revenue_actual = COALESCE(EXCLUDED.revenue_actual, earnings_event_detail.revenue_actual),
          revenue_estimate = COALESCE(EXCLUDED.revenue_estimate, earnings_event_detail.revenue_estimate),
          eps_actual = COALESCE(EXCLUDED.eps_actual, earnings_event_detail.eps_actual),
          eps_estimate = COALESCE(EXCLUDED.eps_estimate, earnings_event_detail.eps_estimate),
          guidance_summary = earnings_event_detail.guidance_summary || EXCLUDED.guidance_summary,
          affected_tickers = EXCLUDED.affected_tickers,
          updated_at = NOW()
        """
    )
    with engine.begin() as conn:
        conn.execute(
            q,
            {
                "kb_id": kb_id,
                "fiscal_period": (str(payload.get("fiscal_period") or "")[:128] or None),
                "revenue_actual": payload.get("revenue_actual"),
                "revenue_estimate": payload.get("revenue_estimate"),
                "eps_actual": payload.get("eps_actual"),
                "eps_estimate": payload.get("eps_estimate"),
                "guidance_summary": json.dumps(guidance, ensure_ascii=False),
                "affected_tickers": json.dumps(payload.get("affected_tickers") or [], ensure_ascii=False),
            },
        )


def _mark_materials_extracted(engine, material_ids: list[int], extraction_meta: dict[str, Any]) -> None:
    if not material_ids:
        return
    q = text(
        """
        UPDATE earnings_material
        SET parse_status = 'extracted',
            meta = COALESCE(meta, '{}'::jsonb) || CAST(:meta_patch AS jsonb),
            updated_at = NOW()
        WHERE id = ANY(:ids)
        """
    )
    patch = {"llm_extraction": extraction_meta}
    with engine.begin() as conn:
        conn.execute(q, {"ids": material_ids, "meta_patch": json.dumps(patch, ensure_ascii=False)})


def _group_events(rows: list[dict[str, Any]]) -> dict[tuple[str, date | None], list[dict[str, Any]]]:
    groups: dict[tuple[str, date | None], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        sym = str(row["symbol"]).upper()
        ev = row.get("event_date")
        if ev is not None and not isinstance(ev, date):
            ev = _parse_date(str(ev))
        groups[(sym, ev)].append(row)
    return groups


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract structured earnings facts via LLM")
    ap.add_argument("--dry-run", action="store_true", help="Token plan only, no LLM/DB writes")
    ap.add_argument("--symbols", default="", help="Comma-separated tickers; default = earnings intelligence universe")
    ap.add_argument("--universe", action="store_true", default=True)
    ap.add_argument("--no-universe", action="store_false", dest="universe")
    ap.add_argument("--symbol", default="", help="Single ticker alias")
    ap.add_argument("--event-date", default="", help="YYYY-MM-DD")
    ap.add_argument("--since", default="2026-01-01", help="Only events on/after date")
    ap.add_argument("--limit", type=int, default=10, help="Max events to process")
    ap.add_argument("--model", default="", help="Override LLM model (EARNINGS_EXTRACT_MODEL env)")
    ap.add_argument("--json-out", default="", help="Write dry-run / results JSON")
    args = ap.parse_args()

    symbols: set[str] | None = None
    sym_blob = args.symbols.strip() or args.symbol.strip()
    if sym_blob:
        symbols = {s.strip().upper() for s in sym_blob.split(",") if s.strip()}
    elif args.universe:
        symbols = set(get_earnings_intelligence_universe())
        logger.info("Universe symbols for extract: %s", len(symbols))

    engine = get_engine()
    rows = _load_materials(
        engine,
        symbols=symbols,
        event_date=_parse_date(args.event_date),
        since=_parse_date(args.since),
    )
    groups = _group_events(rows)
    if not groups:
        logger.warning("No parsed earnings_material rows found")
        return 0

    results: list[dict[str, Any]] = []
    processed = 0
    for (symbol, event_date), materials in sorted(groups.items(), key=lambda x: (x[0][0], x[0][1] or date.min)):
        if processed >= max(1, args.limit):
            break
        fiscal_period = next((m.get("fiscal_period") for m in materials if m.get("fiscal_period")), None)
        selected = select_materials_for_event(materials)
        if not selected:
            continue

        kb_id_precheck = _resolve_kb_id(
            engine,
            symbol=symbol,
            event_date=event_date,
            fallback=next((m.get("knowledge_base_id") for m in materials if m.get("knowledge_base_id")), None),
        )
        if kb_id_precheck and _detail_has_llm_extraction(engine, kb_id_precheck):
            logger.info("%s %s skip: LLM extraction already in earnings_event_detail", symbol, event_date)
            results.append(
                {
                    "symbol": symbol,
                    "event_date": event_date.isoformat() if event_date else None,
                    "status": "skipped",
                    "reason": "llm_extraction_exists",
                    "knowledge_base_id": kb_id_precheck,
                }
            )
            processed += 1
            continue

        if args.dry_run:
            plan = plan_event_extraction_tokens(
                symbol=symbol,
                event_date=event_date,
                fiscal_period=fiscal_period,
                materials=materials,
            )
            logger.info(
                "dry-run %s %s: materials=%s included=%s total_tok≈%s",
                symbol,
                event_date,
                plan["materials_available"],
                plan["materials_included"],
                plan["total_tokens_est"],
            )
            results.append({"status": "dry_run", "token_plan": plan})
            processed += 1
            continue

        out = extract_event_facts_with_llm(
            symbol=symbol,
            event_date=event_date,
            fiscal_period=fiscal_period,
            materials=materials,
            dry_run=False,
            model=args.model.strip() or None,
        )
        structured = out.get("structured")
        usage = out.get("usage") or {}
        logger.info(
            "%s %s extract status=%s model=%s usage=%s",
            symbol,
            event_date,
            out.get("status"),
            out.get("model"),
            usage,
        )
        result_row = {
            "symbol": symbol,
            "event_date": event_date.isoformat() if event_date else None,
            "status": out.get("status"),
            "model": out.get("model"),
            "usage": usage,
            "token_plan": out.get("token_plan"),
        }
        if structured and out.get("status") in ("ok", "parse_warning"):
            kb_id = _resolve_kb_id(
                engine,
                symbol=symbol,
                event_date=event_date,
                fallback=next((m.get("knowledge_base_id") for m in materials if m.get("knowledge_base_id")), None),
            )
            if not kb_id:
                result_row["status"] = "skipped"
                result_row["reason"] = "knowledge_base_id not found"
                results.append(result_row)
                processed += 1
                continue
            detail = map_extraction_to_event_detail(structured)
            extraction_meta = {
                "model": out.get("model"),
                "usage": usage,
                "included_material_ids": out.get("included_material_ids") or [],
                "extracted_at_utc": datetime.utcnow().isoformat() + "Z",
            }
            _upsert_event_detail(engine, kb_id=kb_id, payload=detail, extraction_meta=extraction_meta)
            _mark_materials_extracted(engine, out.get("included_material_ids") or [], extraction_meta)
            result_row["knowledge_base_id"] = kb_id
            result_row["affected_tickers"] = detail.get("affected_tickers")
        results.append(result_row)
        processed += 1

    total_tok = sum(int((r.get("token_plan") or {}).get("total_tokens_est") or 0) for r in results)
    logger.info("Processed %s events; sum_total_tokens_est≈%s", processed, total_tok)

    out_path = args.json_out.strip()
    if not out_path and args.dry_run:
        out_dir = project_root / "logs" / "earnings_materials"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / "extract_token_plan.json")
    if out_path:
        Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        logger.info("Wrote %s", out_path)

    failed = [r for r in results if r.get("status") not in ("ok", "dry_run", "parse_warning")]
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
