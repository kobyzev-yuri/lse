#!/usr/bin/env python3
"""
Регулярная проверка готовности CatBoost к продакшену: dry-run (по умолчанию) или полное обучение,
запись JSON метрик и строки в ml_train_readiness.jsonl для анализатора / дашборда.

По умолчанию не перезаписывает .cbm (только --dry-run у GAME_5M; портфель — тоже dry-run).

  # Ночной cron (метрики без смены моделей)
  python scripts/run_ml_train_readiness_cron.py

  # Записать модели (осторожно на проде)
  ML_READINESS_TRAIN_MODE=full python scripts/run_ml_train_readiness_cron.py

Переменные (как в окружении процесса, так и в config.env на /app/config.env в Docker):
  ML_READINESS_JSONL       — путь JSONL (default: /app/logs/ml/logs/ml_train_readiness.jsonl или local/...)
  ML_READINESS_TRAIN_MODE  — dry_run | full
  ML_READINESS_SKIP_GAME5M — 1 — не вызывать train_game5m_catboost
  ML_READINESS_SKIP_PORTFOLIO — 1 — не вызывать train_portfolio_catboost
  ML_READINESS_SKIP_EVENT_REACTION — 1 (по умолчанию) — не вызывать train_event_reaction_catboost; 0 — включить
    (на VM ночной cron может задать 0 через `docker exec -e ML_READINESS_SKIP_EVENT_REACTION=0`, см. crontab/lse-docker.crontab)
  ML_READINESS_EVENT_REACTION_RMSE_MAX — макс. RMSE valid (default 0.12)
  ML_READINESS_EVENT_REACTION_MIN_TRAIN — мин. n_train при status=ok (default 8; поднимите для прода)
  ML_READINESS_GAME5M_AUC_MIN — порог AUC valid (default 0.52)
  ML_READINESS_GAME5M_MIN_TRAIN — мин. n_train (default 40)
  ML_READINESS_PORTFOLIO_RMSE_MAX — макс. RMSE valid в log-пространстве (default 0.08)
  ML_READINESS_SKIP_EARNINGS_INTELLIGENCE — 1 — не вызывать run_earnings_ml_refresh / гейт сетки
  ML_READINESS_EARNINGS_MIN_SCENARIO_LABELS — мин. llm_scenario labels для гейта (default 8)

См. docs/ML_DATA_QUALITY_PIPELINE.md
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from config_loader import get_config_value

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _default_log_dir(root: Path) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/logs")
    return root / "local" / "logs"


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _readiness_bool(key: str, default: bool = False) -> bool:
    v = (get_config_value(key) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _gate_game5m(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    reasons: list[str] = []
    if not data:
        return {"ready": False, "reasons": ["no_metrics_file"]}
    st = data.get("status")
    if st != "ok":
        reasons.append(f"status={st}")
    try:
        auc_min = float((get_config_value("ML_READINESS_GAME5M_AUC_MIN") or "0.52").strip())
    except (ValueError, TypeError):
        auc_min = 0.52
    auc = data.get("auc_valid")
    if auc is None or (isinstance(auc, (int, float)) and float(auc) < auc_min):
        reasons.append(f"auc_valid<{auc_min}")
    try:
        nv_min = int((get_config_value("ML_READINESS_GAME5M_MIN_VALID") or "80").strip())
    except (ValueError, TypeError):
        nv_min = 80
    nv = int(data.get("n_valid") or 0)
    if nv < nv_min:
        reasons.append(f"n_valid<{nv_min}")
    try:
        nt_min = int((get_config_value("ML_READINESS_GAME5M_MIN_TRAIN") or "40").strip())
    except (ValueError, TypeError):
        nt_min = 40
    nt = int(data.get("n_train") or 0)
    if nt < nt_min:
        reasons.append(f"n_train<{nt_min}")
    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "auc_valid": auc,
        "n_valid": nv,
        "n_train": nt,
    }


def _gate_entry_bar_v2(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    reasons: list[str] = []
    if not data:
        return {"ready": False, "reasons": ["no_metrics_file"], "shadow_only": True}
    st = data.get("status")
    if st != "ok":
        reasons.append(f"status={st}")
    try:
        auc_min = float((get_config_value("ML_READINESS_ENTRY_BAR_V2_AUC_MIN") or "0.55").strip())
    except (ValueError, TypeError):
        auc_min = 0.55
    auc = data.get("auc_valid")
    if auc is None or (isinstance(auc, (int, float)) and float(auc) < auc_min):
        reasons.append(f"auc_valid<{auc_min}")
    try:
        nv_min = int((get_config_value("ML_READINESS_ENTRY_BAR_V2_MIN_VALID") or "80").strip())
    except (ValueError, TypeError):
        nv_min = 80
    nv = int(data.get("n_valid") or 0)
    if nv < nv_min:
        reasons.append(f"n_valid<{nv_min}")
    try:
        nt_min = int((get_config_value("ML_READINESS_ENTRY_BAR_V2_MIN_ROWS") or "5000").strip())
    except (ValueError, TypeError):
        nt_min = 5000
    nt = int(data.get("n_total") or 0)
    if nt < nt_min:
        reasons.append(f"n_total<{nt_min}")
    return {
        "ready": len(reasons) == 0,
        "reasons": reasons,
        "shadow_only": True,
        "auc_valid": auc,
        "n_valid": nv,
        "n_total": nt,
    }


def _gate_event_reaction(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    reasons: list[str] = []
    if not data:
        return {"ready": False, "reasons": ["no_metrics_file"], "rmse_valid": None, "n_train": 0}
    st = data.get("status")
    mets = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    rmse = mets.get("rmse_valid")
    nt = int(data.get("n_train") or 0)

    if st != "ok":
        reasons.append(f"status={st}")
        if st == "insufficient_rows":
            ntot = int(data.get("n_total") or 0)
            need = int(data.get("min_rows_required") or data.get("min_rows_config") or 0)
            if need:
                reasons.append(f"n_total={ntot}<{need}")
            else:
                reasons.append(f"n_total={ntot}")
        return {"ready": False, "reasons": reasons, "rmse_valid": rmse, "n_train": nt}

    try:
        rmse_max = float((get_config_value("ML_READINESS_EVENT_REACTION_RMSE_MAX") or "0.12").strip())
    except (ValueError, TypeError):
        rmse_max = 0.12
    if rmse is None or (isinstance(rmse, (int, float)) and float(rmse) > rmse_max):
        reasons.append(f"rmse_valid>{rmse_max}")
    try:
        nt_min = int((get_config_value("ML_READINESS_EVENT_REACTION_MIN_TRAIN") or "8").strip())
    except (ValueError, TypeError):
        nt_min = 25
    if nt < nt_min:
        reasons.append(f"n_train<{nt_min}")
    return {"ready": len(reasons) == 0, "reasons": reasons, "rmse_valid": rmse, "n_train": nt}


def _gate_portfolio(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    reasons: list[str] = []
    if not data:
        return {"ready": False, "reasons": ["no_metrics_file"]}
    st = data.get("status")
    if st != "ok":
        reasons.append(f"status={st}")
    try:
        rmse_max = float((get_config_value("ML_READINESS_PORTFOLIO_RMSE_MAX") or "0.08").strip())
    except (ValueError, TypeError):
        rmse_max = 0.08
    mets = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    rmse = mets.get("rmse_valid")
    if rmse is None or (isinstance(rmse, (int, float)) and float(rmse) > rmse_max):
        reasons.append(f"rmse_valid>{rmse_max}")
    try:
        nt_min = int((get_config_value("ML_READINESS_PORTFOLIO_MIN_TRAIN") or "80").strip())
    except (ValueError, TypeError):
        nt_min = 80
    nt = int(data.get("n_train") or 0)
    if nt < nt_min:
        reasons.append(f"n_train<{nt_min}")
    return {"ready": len(reasons) == 0, "reasons": reasons, "rmse_valid": rmse, "n_train": nt}


def _gate_earnings_intelligence(
    readiness_bundle: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not readiness_bundle:
        return {"ready": False, "reasons": ["no_readiness_file"], "overall_grid_ready": False}
    gates = readiness_bundle.get("gates") if isinstance(readiness_bundle.get("gates"), dict) else {}
    overall = bool(gates.get("overall_grid_ready"))
    reasons: list[str] = []
    if not overall:
        for name, g in gates.items():
            if name.startswith("overall_") or not isinstance(g, dict):
                continue
            if not g.get("ready"):
                for r in g.get("reasons") or []:
                    reasons.append(f"{name}:{r}")
    return {
        "ready": overall,
        "reasons": reasons,
        "overall_grid_ready": overall,
        "overall_scenario_classifier_ready": bool(gates.get("overall_scenario_classifier_ready")),
        "overall_peer_spillover_ready": bool(gates.get("overall_peer_spillover_ready")),
        "gates": gates,
    }


def main() -> int:
    root = project_root
    py = sys.executable
    log_dir = _default_log_dir(root)
    log_dir.mkdir(parents=True, exist_ok=True)
    jraw = (get_config_value("ML_READINESS_JSONL") or "").strip()
    jsonl = Path(jraw) if jraw else (log_dir / "ml_train_readiness.jsonl")
    jsonl.parent.mkdir(parents=True, exist_ok=True)

    mode = (get_config_value("ML_READINESS_TRAIN_MODE") or "dry_run").strip().lower()
    full_train = mode in ("full", "train", "write", "prod")

    q_dir = log_dir.parent / "ml_data_quality"
    if Path("/app/logs").exists():
        q_dir = Path("/app/logs/ml/ml_data_quality")
    q_dir.mkdir(parents=True, exist_ok=True)
    g5_path = q_dir / "last_game5m_train_metrics.json"
    bar_v2_path = q_dir / "last_game5m_entry_bar_v2_train_metrics.json"
    pf_path = q_dir / "last_portfolio_train_metrics.json"
    er_path = q_dir / "last_event_reaction_train_metrics.json"
    ei_readiness_path = q_dir / "last_earnings_intelligence_readiness.json"

    skip_g5 = _readiness_bool("ML_READINESS_SKIP_GAME5M", False)
    skip_pf = _readiness_bool("ML_READINESS_SKIP_PORTFOLIO", False)
    skip_er = (get_config_value("ML_READINESS_SKIP_EVENT_REACTION", "1") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    skip_ei = _readiness_bool("ML_READINESS_SKIP_EARNINGS_INTELLIGENCE", True)

    g5_inv: Dict[str, Any] = {}
    if not skip_g5:
        cmd_g5 = [py, str(root / "scripts" / "train_game5m_catboost.py")]
        if not full_train:
            cmd_g5.append("--dry-run")
        cmd_g5 += ["--json-metrics-out", str(g5_path)]
        logger.info("GAME_5M: %s", " ".join(cmd_g5))
        p = subprocess.run(cmd_g5, cwd=str(root))
        g5_inv = {"cmd": cmd_g5, "returncode": p.returncode}
    else:
        g5_inv = {"skipped": True}

    pf_inv: Dict[str, Any] = {}
    if not skip_pf:
        cmd_pf = [py, str(root / "scripts" / "train_portfolio_catboost.py")]
        if not full_train:
            cmd_pf.append("--dry-run")
        cmd_pf += ["--json-metrics-out", str(pf_path)]
        logger.info("Portfolio: %s", " ".join(cmd_pf))
        p2 = subprocess.run(cmd_pf, cwd=str(root))
        pf_inv = {"cmd": cmd_pf, "returncode": p2.returncode}
    else:
        pf_inv = {"skipped": True}

    er_inv: Dict[str, Any] = {}
    if not skip_er:
        cmd_er = [py, str(root / "scripts" / "train_event_reaction_catboost.py")]
        er_dataset_version = (get_config_value("EVENT_REACTION_DATASET_VERSION") or "").strip()
        if er_dataset_version:
            cmd_er += ["--dataset-version", er_dataset_version]
        er_feature_builder_version = (get_config_value("EVENT_REACTION_FEATURE_BUILDER_VERSION") or "").strip()
        if er_feature_builder_version:
            cmd_er += ["--feature-builder-version", er_feature_builder_version]
        if not full_train:
            cmd_er.append("--dry-run")
        cmd_er += ["--json-metrics-out", str(er_path)]
        logger.info("Event reaction: %s", " ".join(cmd_er))
        p3 = subprocess.run(cmd_er, cwd=str(root))
        er_inv = {"cmd": cmd_er, "returncode": p3.returncode}
    else:
        er_inv = {"skipped": True}

    ei_inv: Dict[str, Any] = {}
    if not skip_ei:
        cmd_ei = [py, str(root / "scripts" / "run_earnings_ml_refresh.py")]
        if not full_train:
            cmd_ei.append("--dry-run")
        logger.info("Earnings intelligence ML refresh: %s", " ".join(cmd_ei))
        p4 = subprocess.run(cmd_ei, cwd=str(root))
        ei_inv = {"cmd": cmd_ei, "returncode": p4.returncode}
    else:
        ei_inv = {"skipped": True}

    g5_data = _load_json(g5_path)
    bar_v2_data = _load_json(bar_v2_path)
    pf_data = _load_json(pf_path)
    er_data = _load_json(er_path)
    g5_gate = _gate_game5m(g5_data) if not skip_g5 else {"ready": None, "reasons": ["skipped"]}
    bar_v2_gate = _gate_entry_bar_v2(bar_v2_data)
    pf_gate = _gate_portfolio(pf_data) if not skip_pf else {"ready": None, "reasons": ["skipped"]}
    er_gate = _gate_event_reaction(er_data) if not skip_er else {"ready": None, "reasons": ["skipped"]}
    ei_data = _load_json(ei_readiness_path)
    ei_gate = _gate_earnings_intelligence(ei_data) if not skip_ei else {"ready": None, "reasons": ["skipped"]}

    mlr_path = q_dir / "last_multiday_lr_train_metrics.json"
    mlr_rows: list = []
    if mlr_path.is_file():
        try:
            raw_mlr = json.loads(mlr_path.read_text(encoding="utf-8"))
            mlr_rows = raw_mlr if isinstance(raw_mlr, list) else []
        except Exception:
            mlr_rows = []
    mlr_gate = {
        "ready": None,
        "reasons": ["advisory_only"],
        "n_tickers_fitted": len(mlr_rows),
    }

    overall = True
    if not skip_g5:
        overall = overall and bool(g5_gate.get("ready"))
    if not skip_pf:
        overall = overall and bool(pf_gate.get("ready"))
    if not skip_er:
        overall = overall and bool(er_gate.get("ready"))
    if skip_g5 and skip_pf and skip_er:
        overall = False

    record = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "train_mode": "full" if full_train else "dry_run",
        "game5m": {"invocation": g5_inv, "metrics_path": str(g5_path), "gate": g5_gate, "metrics": g5_data},
        "entry_bar_v2": {
            "invocation": {"source": "run_game5m_entry_bar_v2_ml_refresh.py"},
            "metrics_path": str(bar_v2_path),
            "gate": bar_v2_gate,
            "metrics": bar_v2_data,
            "shadow_only": True,
        },
        "portfolio": {"invocation": pf_inv, "metrics_path": str(pf_path), "gate": pf_gate, "metrics": pf_data},
        "event_reaction": {"invocation": er_inv, "metrics_path": str(er_path), "gate": er_gate, "metrics": er_data},
        "earnings_intelligence": {
            "invocation": ei_inv,
            "readiness_path": str(ei_readiness_path),
            "gate": ei_gate,
            "readiness": ei_data,
        },
        "multiday_lr": {
            "metrics_path": str(mlr_path),
            "gate": mlr_gate,
            "metrics": {"n_tickers_fitted": len(mlr_rows), "tickers": [r.get("ticker") for r in mlr_rows[:20]]},
        },
        "overall_production_ready": overall,
        "overall_earnings_grid_ready": bool(ei_gate.get("ready")) if not skip_ei else None,
    }
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    logger.info("Дописано в %s | overall_production_ready=%s", jsonl, record["overall_production_ready"])
    try:
        from services.ml_contour_deltas import build_delta_resolver, build_readiness_resolver
        from services.ml_contour_refresh import collect_aggregate_contours_status

        collect_aggregate_contours_status(
            root,
            readiness_resolver=build_readiness_resolver(root),
            delta_resolver=build_delta_resolver(root),
        )
        logger.info("Обновлён ml_contours_status.json")
    except Exception as e:
        logger.warning("ml_contours_status aggregate skipped: %s", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
