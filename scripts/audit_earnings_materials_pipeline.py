#!/usr/bin/env python3
"""
Audit earnings materials pipeline: registry coverage, parser chain, token budget.

Steps (optional flags):
  --ensure-table   apply ml_event_analytics_schema.sql
  --seed           seed starter registry rows
  --ingest         run ingest_earnings_materials on registered/failed rows
  --probe-url URL  fetch+parse one URL without DB (connectivity smoke test)

Always prints a summary table and writes JSON report.

Examples:
  python scripts/audit_earnings_materials_pipeline.py --probe-url https://www.fool.com/earnings/call-transcripts/2026/04/30/sandisk-sndk-q3-2026-earnings-transcript/
  python scripts/audit_earnings_materials_pipeline.py --ensure-table --seed --ingest --limit 10
  python scripts/audit_earnings_materials_pipeline.py --json-out logs/earnings_materials/audit_latest.json
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from report_generator import get_engine  # noqa: E402
from services.earnings_material_parser import fetch_and_parse  # noqa: E402
from services.earnings_material_token_estimator import (  # noqa: E402
    estimate_tokens,
    extraction_cycle_tokens,
)
from services.earnings_material_extractor import (  # noqa: E402
    EXTRACTION_SYSTEM_PROMPT,
    plan_event_extraction_tokens,
    select_materials_for_event,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

MIN_USEFUL_TEXT_CHARS = 400
SYSTEM_PROMPT_TOKENS = estimate_tokens(EXTRACTION_SYSTEM_PROMPT).get("tokens_exact") or estimate_tokens(
    EXTRACTION_SYSTEM_PROMPT
)["tokens_est_primary"]
OUTPUT_TOKENS = 1200


def _ensure_table() -> None:
    rc = subprocess.call([sys.executable, str(project_root / "scripts" / "migrate_ml_event_analytics.py")])
    if rc != 0:
        raise RuntimeError("migrate_ml_event_analytics failed")


def _seed() -> None:
    rc = subprocess.call(
        [sys.executable, str(project_root / "scripts" / "seed_earnings_material_registry.py"), "--ensure-table"]
    )
    if rc != 0:
        raise RuntimeError("seed_earnings_material_registry failed")


def _ingest(*, limit: int, symbol: str, force: bool) -> None:
    cmd = [
        sys.executable,
        str(project_root / "scripts" / "ingest_earnings_materials.py"),
        "--limit",
        str(limit),
    ]
    if symbol:
        cmd.extend(["--symbol", symbol])
    if force:
        cmd.append("--force")
    else:
        cmd.extend(["--status", "registered,failed"])
    rc = subprocess.call(cmd)
    if rc != 0:
        raise RuntimeError("ingest_earnings_materials failed")


def _load_materials(engine, *, symbols: set[str] | None = None) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: dict[str, Any] = {}
    if symbols:
        where.append("UPPER(TRIM(symbol)) = ANY(:symbols)")
        params["symbols"] = sorted(symbols)
    q = text(
        f"""
        SELECT
          id, symbol, event_date, fiscal_period, material_type,
          source_name, source_url, title, parse_status, parse_error,
          content_sha256, local_path,
          LENGTH(COALESCE(content_text, '')) AS text_chars,
          content_text,
          meta
        FROM earnings_material
        WHERE {' AND '.join(where)}
        ORDER BY symbol, event_date DESC NULLS LAST, id ASC
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(q, params).mappings().all()
    return [dict(r) for r in rows]


def _probe_url(url: str, *, timeout_sec: int) -> dict[str, Any]:
    parsed = fetch_and_parse(url, timeout_sec=timeout_sec)
    tok = estimate_tokens(parsed.text)
    useful = len(parsed.text) >= MIN_USEFUL_TEXT_CHARS and parsed.parse_error is None
    return {
        "url": url,
        "final_url": parsed.final_url,
        "content_type": parsed.content_type,
        "method": parsed.method,
        "text_chars": len(parsed.text),
        "discovered_links": list(parsed.discovered_links),
        "parse_error": parsed.parse_error,
        "useful_for_llm": useful,
        "tokens": tok,
        "extraction_cycle_tokens": extraction_cycle_tokens(
            tok.get("tokens_exact") or tok["tokens_est_primary"],
            system_prompt_tokens=SYSTEM_PROMPT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
        ),
    }


def _row_audit(row: dict[str, Any]) -> dict[str, Any]:
    text_body = row.get("content_text") or ""
    tok = estimate_tokens(text_body)
    token_basis = tok.get("tokens_exact") or tok["tokens_est_primary"]
    cycle = extraction_cycle_tokens(
        token_basis,
        system_prompt_tokens=SYSTEM_PROMPT_TOKENS,
        output_tokens=OUTPUT_TOKENS,
    )
    useful = (
        row.get("parse_status") == "parsed"
        and len(text_body) >= MIN_USEFUL_TEXT_CHARS
        and not (row.get("parse_error") or "").startswith("short_text")
    )
    meta = row.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    discovered = meta.get("discovered_links") or []
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "event_date": str(row.get("event_date") or ""),
        "fiscal_period": row.get("fiscal_period"),
        "material_type": row["material_type"],
        "source_name": row.get("source_name"),
        "source_url": row["source_url"],
        "parse_status": row.get("parse_status"),
        "parse_error": row.get("parse_error"),
        "text_chars": int(row.get("text_chars") or 0),
        "discovered_links_count": len(discovered),
        "useful_for_llm": useful,
        "tokens": tok,
        "extraction_cycle_tokens": cycle,
        "mvp_case": (meta.get("mvp_case") if isinstance(meta, dict) else None),
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_symbol: dict[str, dict[str, int]] = {}
    useful_rows = [r for r in rows if r["useful_for_llm"]]
    token_rows = [r for r in rows if r["text_chars"] > 0]

    total_input = 0
    total_cycle = 0
    for r in token_rows:
        cyc = r["extraction_cycle_tokens"]
        total_input += int(cyc["input_tokens"])
        total_cycle += int(cyc["total_tokens_est"])

    for r in rows:
        st = str(r.get("parse_status") or "unknown")
        by_status[st] = by_status.get(st, 0) + 1
        sym = str(r.get("symbol") or "?")
        bucket = by_symbol.setdefault(sym, {"total": 0, "useful": 0, "parsed": 0, "failed": 0})
        bucket["total"] += 1
        if r["useful_for_llm"]:
            bucket["useful"] += 1
        if r.get("parse_status") == "parsed":
            bucket["parsed"] += 1
        if r.get("parse_status") == "failed":
            bucket["failed"] += 1

    return {
        "rows_total": len(rows),
        "useful_for_llm": len(useful_rows),
        "coverage_rate": round(len(useful_rows) / len(rows), 4) if rows else 0.0,
        "by_parse_status": by_status,
        "by_symbol": by_symbol,
        "token_assumptions": {
            "system_prompt_tokens": SYSTEM_PROMPT_TOKENS,
            "output_tokens_est": OUTPUT_TOKENS,
            "min_useful_text_chars": MIN_USEFUL_TEXT_CHARS,
            "material_token_basis": "tiktoken_cl100k_base if available else chars/3.7",
            "extraction_mode": "one LLM call per (symbol, event_date) with prioritized materials",
        },
        "llm_cost_planning": {
            "materials_with_text": len(token_rows),
            "sum_input_tokens_est_per_material_pass": total_input,
            "sum_total_tokens_est_per_material_pass": total_cycle,
            "avg_material_tokens_est": round(
                sum((r["tokens"].get("tokens_exact") or r["tokens"]["tokens_est_primary"]) for r in token_rows)
                / max(1, len(token_rows)),
                1,
            ),
            "avg_total_tokens_per_event_if_one_material": round(total_cycle / max(1, len(rows)), 1),
        },
    }


def _event_extraction_plans(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from collections import defaultdict
    from datetime import date as date_cls

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        ev = str(r.get("event_date") or "")
        groups[(sym, ev)].append(
            {
                "id": r.get("id"),
                "material_type": r.get("material_type"),
                "parse_status": r.get("parse_status"),
                "content_text": r.get("content_text") or "",
                "fiscal_period": r.get("fiscal_period"),
            }
        )

    plans: list[dict[str, Any]] = []
    for (sym, ev_s), materials in sorted(groups.items()):
        if not select_materials_for_event(materials):
            continue
        ev_date = None
        if ev_s:
            try:
                ev_date = date_cls.fromisoformat(ev_s[:10])
            except ValueError:
                ev_date = None
        fiscal_period = next((m.get("fiscal_period") for m in materials if m.get("fiscal_period")), None)
        plan = plan_event_extraction_tokens(
            symbol=sym,
            event_date=ev_date,
            fiscal_period=fiscal_period,
            materials=materials,
            output_tokens=OUTPUT_TOKENS,
        )
        plans.append(plan)
    return plans


def _print_table(rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    print("\n=== Earnings materials pipeline audit ===")
    print(f"rows={summary['rows_total']} useful_for_llm={summary['useful_for_llm']} coverage={summary['coverage_rate']:.1%}")
    print(f"by_status={summary['by_parse_status']}")
    plan = summary["llm_cost_planning"]
    print(
        "LLM token planning (legacy per-material pass): "
        f"avg_material≈{plan['avg_material_tokens_est']} tok, "
        f"sum_total≈{plan['sum_total_tokens_est_per_material_pass']} tok"
    )
    event_plans = summary.get("event_extraction_plans") or []
    if event_plans:
        sum_event = sum(int(p.get("total_tokens_est") or 0) for p in event_plans)
        print(
            f"Event-level extraction ({len(event_plans)} events): "
            f"system≈{SYSTEM_PROMPT_TOKENS} tok, sum_total≈{sum_event} tok"
        )
        for p in event_plans:
            print(
                f"  {p['symbol']} {p.get('event_date')}: "
                f"included={p.get('materials_included')}/{p.get('materials_available')} "
                f"input≈{p.get('input_tokens_est')} total≈{p.get('total_tokens_est')}"
            )
    print("\n| symbol | date | type | status | chars | tok(est) | useful | discovered |")
    print("|---|---|---|---|---:|---:|---|---:|")
    for r in rows:
        tok = r["tokens"].get("tokens_exact") or r["tokens"]["tokens_est_primary"]
        print(
            f"| {r['symbol']} | {r['event_date']} | {r['material_type']} | {r['parse_status']} | "
            f"{r['text_chars']} | {tok} | {'yes' if r['useful_for_llm'] else 'no'} | {r['discovered_links_count']} |"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit earnings materials parser chain and token budget")
    ap.add_argument("--ensure-table", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--force-ingest", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--symbol", default="")
    ap.add_argument("--symbols", default="", help="Comma-separated tickers filter, e.g. META,NVDA")
    ap.add_argument("--probe-url", default="", help="Fetch+parse one URL without DB")
    ap.add_argument("--timeout-sec", type=int, default=60)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "probe": None,
        "steps_run": [],
        "materials": [],
        "summary": {},
    }

    if args.probe_url:
        probe = _probe_url(args.probe_url.strip(), timeout_sec=args.timeout_sec)
        report["probe"] = probe
        report["steps_run"].append("probe_url")
        print(json.dumps(probe, ensure_ascii=False, indent=2))

    if args.ensure_table:
        _ensure_table()
        report["steps_run"].append("ensure_table")
    if args.seed:
        _seed()
        report["steps_run"].append("seed")
    if args.ingest:
        _ingest(limit=max(1, args.limit), symbol=args.symbol.strip(), force=args.force_ingest)
        report["steps_run"].append("ingest")

    sym_blob = args.symbols.strip() or args.symbol.strip()
    symbols = {s.strip().upper() for s in sym_blob.split(",") if s.strip()} or None

    engine = get_engine()
    raw_rows = _load_materials(engine, symbols=symbols)
    audited = [_row_audit(r) for r in raw_rows]
    summary = _summarize(audited)
    event_plans = _event_extraction_plans(raw_rows)
    summary["event_extraction_plans"] = event_plans
    summary["llm_cost_planning"]["sum_total_tokens_est_event_pass"] = sum(
        int(p.get("total_tokens_est") or 0) for p in event_plans
    )
    report["materials"] = audited
    report["summary"] = summary
    report["event_extraction_plans"] = event_plans

    if audited:
        _print_table(audited, summary)
    elif not args.probe_url:
        logger.warning("No earnings_material rows in DB; run with --seed --ingest")

    out_path = args.json_out.strip()
    if not out_path:
        out_dir = project_root / "logs" / "earnings_materials"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / "audit_latest.json")
    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Wrote audit report: %s", out_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
