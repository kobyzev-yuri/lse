#!/usr/bin/env python3
"""
Ежедневный конвейер датасетов GAME_5M после закрытия US-сессии (cron 23:40 MSK):

  1) Сбор stuck-risk CSV + meta
  2) Сбор continuation / underprofit CSV + meta
  3) JSONL-сводка метаданных датасетов

Обучение CatBoost entry — только через ``run_game5m_entry_ml_refresh.py``
(dispatcher ``*/6`` poll или ``--slot nightly``). Legacy train: ``DAILY_ML_RUN_CATBOOST=1``.

Проверка модели в анализаторе: GET /api/analyzer?strategy=GAME_5M — ``catboost_entry_backtest``.

Запуск на сервере (как в docker-compose.yml: сервис lse, container_name lse-bot, WORKDIR /app).

  Однократно из каталога с docker-compose.yml:
    docker compose exec lse python3 scripts/run_daily_game5m_ml_pipeline.py

  С логом в примонтированный ./logs (на хосте появится logs/game5m_daily_ml_pipeline.log):
    docker compose exec lse bash -lc 'python3 scripts/run_daily_game5m_ml_pipeline.py >> /app/logs/game5m_daily_ml_pipeline.log 2>&1'

  Без compose (если известен container_name):
    docker exec lse-bot bash -lc 'cd /app && python3 scripts/run_daily_game5m_ml_pipeline.py'

  Cron на хосте (из каталога репозитория; -T — без TTY для cron):
    30 22 * * 1-5 cd /path/to/lse && docker compose exec -T lse python3 scripts/run_daily_game5m_ml_pipeline.py >> /path/to/lse/logs/game5m_daily_ml_pipeline.log 2>&1

Переменные окружения:
  DAILY_ML_RUN_CATBOOST       — 1/true: legacy train в этом скрипте (не рекомендуется)
  DAILY_ML_MIN_CATBOOST_ROWS  — порог строк для legacy train (default: 35)
  DAILY_ML_REPORT_JSONL       — путь к JSONL-отчёту (default: local/logs/game5m_daily_ml_report.jsonl)
  DAILY_ML_DATASETS_DRY_RUN   — 1/true: только --dry-run у датасетов (без записи CSV)
  DAILY_ML_RUN_ENTRY_BAR_V2_APPLY — 1/true (default): инкремент bar v2 dataset после сессии
  DAILY_ML_RUN_CONTINUATION_DATASET — 1/true (default): continuation CSV; 0 — freeze B2
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

project_root = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)


def _env_bool(key: str, default: bool = False) -> bool:
    v = (os.environ.get(key) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _run(cmd: List[str], *, cwd: Path) -> int:
    logger.info("run: %s", " ".join(cmd))
    p = subprocess.run(cmd, cwd=str(cwd))
    return int(p.returncode)


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("не прочитан %s: %s", path, e)
        return None


def _default_ml_out_dir(root: Path) -> Path:
    """
    Куда писать артефакты (CSV/CBM/meta):
    - в контейнере: /app/logs/ml (persist bind-mount)
    - локально: <repo>/local (как раньше)
    """
    if Path("/app/logs").exists():
        return Path("/app/logs/ml")
    return root / "local"


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily GAME_5M ML: datasets + CatBoost + JSONL report")
    parser.add_argument("--root", type=Path, default=project_root, help="Repo root (default: parent of scripts/)")
    args = parser.parse_args()
    root: Path = args.root.resolve()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    datasets_dry = _env_bool("DAILY_ML_DATASETS_DRY_RUN", False)
    run_cb = _env_bool("DAILY_ML_RUN_CATBOOST", False)
    if _env_bool("DAILY_ML_SKIP_CATBOOST", False):
        run_cb = False
    try:
        min_cb_rows = int((os.environ.get("DAILY_ML_MIN_CATBOOST_ROWS") or "35").strip())
    except (ValueError, TypeError):
        min_cb_rows = 35
    min_cb_rows = max(20, min_cb_rows)

    report_path = Path(
        os.environ.get("DAILY_ML_REPORT_JSONL")
        or (_default_ml_out_dir(root) / "logs" / "game5m_daily_ml_report.jsonl")
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)

    base_out = Path(os.environ.get("DAILY_ML_OUT_DIR") or _default_ml_out_dir(root))
    datasets_dir = base_out / "datasets"
    models_dir = base_out / "models"
    datasets_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    stuck_out = datasets_dir / "game5m_stuck_dataset.csv"
    cont_out = datasets_dir / "game5m_continuation_dataset.csv"
    cb_out = models_dir / "game5m_entry_catboost.cbm"

    stuck_meta_path = stuck_out.with_suffix(stuck_out.suffix + ".meta.json")
    cont_meta_path = cont_out.with_suffix(cont_out.suffix + ".meta.json")
    cb_meta = cb_out.with_suffix(".meta.json")

    ds_flags: List[str] = []
    if datasets_dry:
        ds_flags.append("--dry-run")

    rc_stuck = _run(
        [py, str(root / "scripts" / "build_game5m_stuck_dataset.py"), "--out", str(stuck_out)] + ds_flags,
        cwd=root,
    )
    if rc_stuck != 0:
        logger.error("build_game5m_stuck_dataset завершился с кодом %s", rc_stuck)
        return rc_stuck

    rc_cont = 0
    continuation_summary: Dict[str, Any] = {"skipped": True}
    run_cont = _env_bool("DAILY_ML_RUN_CONTINUATION_DATASET", True)
    if run_cont:
        continuation_summary = {"skipped": False}
        rc_cont = _run(
            [py, str(root / "scripts" / "build_game5m_continuation_dataset.py"), "--out", str(cont_out)] + ds_flags,
            cwd=root,
        )
        if rc_cont != 0:
            logger.error("build_game5m_continuation_dataset завершился с кодом %s", rc_cont)
            return rc_cont
        continuation_summary = _load_json(cont_meta_path) or {"skipped": False}
    else:
        logger.info("continuation dataset skipped (DAILY_ML_RUN_CONTINUATION_DATASET=0)")
        continuation_summary = {"skipped": True, "note": "skipped (DAILY_ML_RUN_CONTINUATION_DATASET=0)"}

    bar_v2_summary: Dict[str, Any] = {"skipped": True}
    run_bar_v2 = _env_bool("DAILY_ML_RUN_ENTRY_BAR_V2_APPLY", True)
    if run_bar_v2 and not datasets_dry:
        bar_v2_summary = {"skipped": False}
        rc_bar = _run(
            [py, str(root / "scripts" / "run_game5m_entry_bar_v2_ml_refresh.py"), "--apply-data"],
            cwd=root,
        )
        bar_v2_summary["exit_code"] = rc_bar
        if rc_bar != 0:
            logger.warning("entry bar v2 apply-data завершился с кодом %s", rc_bar)
            bar_v2_summary["note"] = "apply-data error (weekly train via dispatcher)"
        else:
            bar_v2_summary["note"] = "apply-data ok"
    elif datasets_dry:
        bar_v2_summary["note"] = "skipped (datasets dry-run)"
    else:
        bar_v2_summary["note"] = "skipped (DAILY_ML_RUN_ENTRY_BAR_V2_APPLY=0)"

    catboost_summary: Dict[str, Any] = {"skipped": not run_cb}
    if run_cb:
        rc_cb = _run(
            [
                py,
                str(root / "scripts" / "train_game5m_catboost.py"),
                "--min-rows",
                str(min_cb_rows),
                "--out",
                str(cb_out),
            ],
            cwd=root,
        )
        catboost_summary["exit_code"] = rc_cb
        if rc_cb == 2:
            catboost_summary["note"] = "мало строк для обучения — модель не перезаписана"
        elif rc_cb != 0:
            catboost_summary["note"] = "ошибка обучения CatBoost"
        else:
            catboost_summary["note"] = "ok"
        meta = _load_json(cb_meta)
        if meta:
            catboost_summary["auc_valid"] = meta.get("auc_valid")
            catboost_summary["n_train"] = meta.get("n_train")
            catboost_summary["n_valid"] = meta.get("n_valid")
            catboost_summary["trained_at"] = meta.get("trained_at")
    else:
        catboost_summary["note"] = "train via run_game5m_entry_ml_refresh (dispatcher)"

    record = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "datasets_dry_run": datasets_dry,
        "stuck_dataset": _load_json(stuck_meta_path),
        "continuation_dataset": continuation_summary,
        "entry_bar_v2": bar_v2_summary,
        "catboost_entry": catboost_summary,
        "paths": {
            "stuck_csv": str(stuck_out),
            "continuation_csv": str(cont_out),
            "catboost_cbm": str(cb_out),
        },
    }
    with report_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.info("отчёт дописан: %s", report_path)

    logger.info(
        "готово. Анализатор: /analyzer + /api/analyzer?strategy=GAME_5M — смотрите catboost_entry_backtest "
        "(нужен актуальный .cbm на том же хосте, что и веб; после обучения на боте скопируйте в окружение веба при раздельном деплое)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
