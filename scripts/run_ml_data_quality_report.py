#!/usr/bin/env python3
"""
Единый отчёт: полнота БД для ML, профили CSV-датасетов, мета CatBoost, опционально dry-run обучения GAME_5M
и LLM-оценка применимости данных к задачам LSE.

  python scripts/run_ml_data_quality_report.py --json-out local/logs/ml_data_quality/report.json
  python scripts/run_ml_data_quality_report.py --json-out report.json --llm
  python scripts/run_ml_data_quality_report.py --game5m-train-dry-run --json-out report.json

См. docs/ML_DATA_QUALITY_PIPELINE.md
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _parse_tickers_fast() -> List[str]:
    try:
        from config_loader import get_config_value

        raw = (get_config_value("TICKERS_FAST", "") or "").strip()
        return [t.strip().upper() for t in raw.split(",") if t.strip()]
    except Exception:
        return []


def _run_game5m_train_metrics(project_root: Path, out_path: Path) -> Dict[str, Any]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(project_root / "scripts" / "train_game5m_catboost.py"),
        "--dry-run",
        "--json-metrics-out",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, cwd=str(project_root), capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "cmd": cmd}
    except Exception as e:
        return {"status": "error", "error": str(e), "cmd": cmd}
    tail_out = (proc.stdout or "")[-4000:]
    tail_err = (proc.stderr or "")[-2000:]
    return {
        "status": "ran",
        "returncode": proc.returncode,
        "metrics_file": str(out_path),
        "stdout_tail": tail_out,
        "stderr_tail": tail_err,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="ML data quality + optional LLM applicability report")
    ap.add_argument(
        "--json-out",
        type=str,
        default="",
        help="Сохранить полный JSON отчёта (рекомендуется local/logs/ml_data_quality/report.json)",
    )
    ap.add_argument(
        "--dataset",
        action="append",
        default=[],
        help="Дополнительный путь CSV для profile_csv_light (можно повторять)",
    )
    ap.add_argument("--no-default-datasets", action="store_true", help="Не профилировать local/datasets/*.csv по умолчанию")
    ap.add_argument(
        "--game5m-train-dry-run",
        action="store_true",
        help="Запустить train_game5m_catboost.py --dry-run и подмешать JSON метрик в отчёт",
    )
    ap.add_argument("--llm", action="store_true", help="Вызвать LLM-анализатор (нужен ключ в config.env)")
    ap.add_argument(
        "--print-llm-summary",
        action="store_true",
        help="Печатать summary_ru из LLM в stdout (при --llm)",
    )
    args = ap.parse_args()

    from report_generator import get_engine

    from services.ml_data_quality_llm import analyze_ml_data_quality_with_llm
    from services.ml_data_quality_report import build_ml_data_quality_report, default_dataset_paths

    engine = get_engine()
    train_paths: Dict[str, Path] = {}
    train_sidecar: Dict[str, Any] = {}

    if args.game5m_train_dry_run:
        mpath = project_root / "local" / "logs" / "ml_data_quality" / "last_game5m_train_metrics.json"
        train_sidecar["game5m_dry_run"] = _run_game5m_train_metrics(project_root, mpath)
        train_paths["game5m_entry_catboost_dry_run"] = mpath

    ds_paths: List[Path] = []
    if not args.no_default_datasets:
        ds_paths.extend(default_dataset_paths(project_root))
    for d in args.dataset:
        ds_paths.append(Path(d).expanduser())

    fast = _parse_tickers_fast()
    bundle = build_ml_data_quality_report(
        project_root=project_root,
        engine=engine,
        dataset_paths=ds_paths,
        fast_tickers=fast or None,
        train_metrics_paths=train_paths if train_paths else None,
    )
    bundle["train_invocation"] = train_sidecar

    if args.llm:
        bundle["llm_review"] = analyze_ml_data_quality_with_llm(bundle)
        if args.print_llm_summary and isinstance(bundle.get("llm_review"), dict):
            st = bundle["llm_review"].get("structured")
            if isinstance(st, dict) and st.get("summary_ru"):
                print(st["summary_ru"])

    js = json.dumps(bundle, ensure_ascii=False, indent=2, default=str)
    out_arg = (args.json_out or "").strip()
    if out_arg:
        outp = Path(out_arg).expanduser()
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(js, encoding="utf-8")
        logger.info("Wrote %s", outp)
    else:
        sys.stdout.write(js)
        if not js.endswith("\n"):
            sys.stdout.write("\n")

    kb_err = (bundle.get("knowledge_base") or {}).get("error")
    th_err = (bundle.get("trade_history_ml") or {}).get("error")
    if kb_err or th_err:
        logger.warning("Report had DB errors: kb=%s th=%s", kb_err, th_err)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
