#!/usr/bin/env python3
"""
Запуск анализатора эффективности сделок с LLM и (при LLM_COMPARE_MODELS) второй моделью — сравнение в одном JSON.

Пример (на сервере, в /app после git pull / копирования файлов):
  LLM_COMPARE_MODELS='https://api.proxyapi.ru/openrouter/v1|qwen/qwen3.6-27b' \\
  python3 scripts/analyzer_llm_dual_compare_run.py --days 3 --strategy GAME_5M

--days — календарное окно выборки закрытых сделок (для ~2 торговых дней NY обычно 3–5).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))


def _safe_list(x: Any) -> List[Any]:
    return x if isinstance(x, list) else []


def _llm_digest(name: str, block: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(block, dict):
        return {"name": name, "error": "no_block"}
    out: Dict[str, Any] = {
        "name": name,
        "model": block.get("model"),
        "status": block.get("status"),
        "parse_ok": block.get("parse_ok"),
        "finish_reason": block.get("finish_reason"),
    }
    usage = block.get("usage")
    if isinstance(usage, dict):
        out["prompt_tokens"] = usage.get("prompt_tokens")
        out["completion_tokens"] = usage.get("completion_tokens")
        out["total_tokens"] = usage.get("total_tokens")
    analysis = block.get("analysis")
    if isinstance(analysis, dict):
        pri = _safe_list(analysis.get("priorities"))
        iac = _safe_list(analysis.get("in_algorithm_parameter_changes"))
        cep = _safe_list(analysis.get("config_env_proposals"))
        out["priorities_n"] = len(pri)
        out["in_algorithm_parameter_changes_n"] = len(iac)
        out["config_env_proposals_n"] = len(cep)
        out["priorities_preview"] = [str(p)[:160] for p in pri[:4]]
        env_keys = [
            str(x.get("env_key") or "")
            for x in iac
            if isinstance(x, dict) and (x.get("env_key") or x.get("parameter"))
        ]
        out["iac_env_or_param_keys"] = [k for k in env_keys if k][:8]
    warns = block.get("warnings")
    if isinstance(warns, list):
        out["warnings_n"] = len(warns)
        out["warnings_head"] = [str(w)[:200] for w in warns[:3]]
    return out


def _print_compare_table(primary: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    print("\n=== Сводка качества (эвристики) ===")
    print(
        f"{'Модель':<40} {'parse':<6} {'pri':<4} {'iac':<4} {'cfg':<4} "
        f"{'prompt_tok':<11} {'compl_tok':<11} {'warnings':<9}"
    )
    all_rows = [primary] + rows
    for r in all_rows:
        if r.get("error"):
            print(f"{str(r.get('name', '?')):<40} ERR {r.get('error')}")
            continue
        print(
            f"{str(r.get('model') or r.get('name')):<40} "
            f"{str(r.get('parse_ok')):<6} "
            f"{str(r.get('priorities_n', '—')):<4} "
            f"{str(r.get('in_algorithm_parameter_changes_n', '—')):<4} "
            f"{str(r.get('config_env_proposals_n', '—')):<4} "
            f"{str(r.get('prompt_tokens', '—')):<11} "
            f"{str(r.get('completion_tokens', '—')):<11} "
            f"{str(r.get('warnings_n', '—')):<9}"
        )
    print("\n--- Приоритеты (primary) ---")
    for line in primary.get("priorities_preview") or []:
        print(f"  • {line}")
    for i, row in enumerate(rows):
        if row.get("error"):
            print(f"\n--- Compare[{i}] error: {row.get('error')} ---")
            continue
        print(f"\n--- Приоритеты (compare: {row.get('model')}) ---")
        for line in row.get("priorities_preview") or []:
            print(f"  • {line}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Анализатор сделок + сравнение LLM (primary vs LLM_COMPARE_MODELS)")
    parser.add_argument("--days", type=int, default=3, help="Календарных дней окна закрытых сделок (по умолчанию 3)")
    parser.add_argument("--strategy", type=str, default="GAME_5M", help="GAME_5M | ALL | …")
    parser.add_argument(
        "--json-out",
        type=str,
        default="",
        help="Путь для полного JSON (абсолютный или относительно корня репо)",
    )
    parser.add_argument(
        "--digest-out",
        type=str,
        default="",
        help="Путь для JSON только сводки primary + compare",
    )
    parser.add_argument(
        "--require-compare",
        action="store_true",
        help="Завершить с кодом 2, если нет model_comparison (нет LLM_COMPARE_MODELS или одна модель)",
    )
    args = parser.parse_args()

    days = max(1, min(30, int(args.days)))
    cmp_env = (os.environ.get("LLM_COMPARE_MODELS") or "").strip()
    if not cmp_env:
        print(
            "Предупреждение: LLM_COMPARE_MODELS не задан — будет только основная модель.\n"
            "Пример:\n"
            "  export LLM_COMPARE_MODELS='https://api.proxyapi.ru/openrouter/v1|qwen/qwen3.6-27b'",
            file=sys.stderr,
        )

    from services.trade_effectiveness_analyzer import analyze_trade_effectiveness

    payload = analyze_trade_effectiveness(
        days=days,
        strategy=args.strategy,
        use_llm=True,
        include_trade_details=False,
        include_game5m_param_hypothesis_backtest=False,
    )

    summary = payload.get("summary") or {}
    meta = payload.get("meta") or {}
    print(
        f"Окно: {meta.get('days', days)} дн. | стратегия: {meta.get('strategy', args.strategy)} | "
        f"сделок: {summary.get('total', 0)} | win%: {summary.get('win_rate_pct', 0):.2f}"
    )

    llm = payload.get("llm")
    if not isinstance(llm, dict):
        print("Блок llm отсутствует (use_llm выключен в конфиге или LLM недоступен).", file=sys.stderr)
        sys.exit(1)

    primary_digest = _llm_digest("primary", llm)
    compare_blocks = _safe_list(llm.get("model_comparison"))
    compare_digests = [_llm_digest(f"compare_{i}", b) for i, b in enumerate(compare_blocks)]

    if args.require_compare and not compare_blocks:
        print("Ошибка: --require-compare, но model_comparison пуст.", file=sys.stderr)
        sys.exit(2)

    _print_compare_table(primary_digest, compare_digests)

    digest_doc = {
        "meta": {"days": days, "strategy": args.strategy, "llm_compare_models_env": cmp_env or None},
        "trade_summary": {
            "total": summary.get("total"),
            "win_rate_pct": summary.get("win_rate_pct"),
            "sum_net_pnl_usd": summary.get("sum_net_pnl_usd"),
        },
        "primary": primary_digest,
        "compare": compare_digests,
    }

    if args.digest_out:
        p = Path(args.digest_out)
        if not p.is_absolute():
            p = project_root / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(digest_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nСводка JSON: {p}")

    if args.json_out:
        p = Path(args.json_out)
        if not p.is_absolute():
            p = project_root / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Полный отчёт JSON: {p}")


if __name__ == "__main__":
    main()
