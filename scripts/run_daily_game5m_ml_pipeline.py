#!/usr/bin/env python3
"""
Ежедневный конвейер после закрытия US-сессии (запуск по cron на VM):

  1) Сбор stuck-risk CSV + meta
  2) Сбор continuation / underprofit CSV + meta
  3) Обучение CatBoost entry (GAME_5M) — при малом числе строк завершается кодом 2, это нормально
  4) Дописать сводку в JSONL для тренда метрик

Проверка «тестирования» в анализаторе: GET /api/analyzer?strategy=GAME_5M — блок
`catboost_entry_backtest` (модель `local/models/game5m_entry_catboost.cbm` + .meta.json на хосте веба).

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
  DAILY_ML_MIN_CATBOOST_ROWS  — порог строк для train_game5m_catboost (default: 35)
  DAILY_ML_SKIP_CATBOOST      — 1/true: не вызывать обучение
  DAILY_ML_REPORT_JSONL       — путь к JSONL-отчёту (default: local/logs/game5m_daily_ml_report.jsonl)
  DAILY_ML_DATASETS_DRY_RUN   — 1/true: только --dry-run у датасетов (без записи CSV)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily GAME_5M ML: datasets + CatBoost + JSONL report")
    parser.add_argument("--root", type=Path, default=project_root, help="Repo root (default: parent of scripts/)")
    args = parser.parse_args()
    root: Path = args.root.resolve()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    datasets_dry = _env_bool("DAILY_ML_DATASETS_DRY_RUN", False)
    skip_cb = _env_bool("DAILY_ML_SKIP_CATBOOST", False)
    try:
        min_cb_rows = int((os.environ.get("DAILY_ML_MIN_CATBOOST_ROWS") or "35").strip())
    except (ValueError, TypeError):
        min_cb_rows = 35
    min_cb_rows = max(20, min_cb_rows)

    report_path = Path(os.environ.get("DAILY_ML_REPORT_JSONL") or (root / "local" / "logs" / "game5m_daily_ml_report.jsonl"))
    report_path.parent.mkdir(parents=True, exist_ok=True)

    datasets_dir = root / "local" / "datasets"
    models_dir = root / "local" / "models"
    py = sys.executable

    stuck_out = datasets_dir / "game5m_stuck_dataset.csv"
    cont_out = datasets_dir / "game5m_continuation_dataset.csv"
    cb_out = models_dir / "game5m_entry_catboost.cbm"

    stuck_meta = stuck_out.with_suffix(stuck_out.suffix + ".meta.json")
    cont_meta = cont_out.with_suffix(cont_out.suffix + ".meta.json")
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

    rc_cont = _run(
        [py, str(root / "scripts" / "build_game5m_continuation_dataset.py"), "--out", str(cont_out)] + ds_flags,
        cwd=root,
    )
    if rc_cont != 0:
        logger.error("build_game5m_continuation_dataset завершился с кодом %s", rc_cont)
        return rc_cont

    catboost_summary: Dict[str, Any] = {"skipped": skip_cb}
    if not skip_cb:
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
        catboost_summary["note"] = "DAILY_ML_SKIP_CATBOOST"

    record = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "datasets_dry_run": datasets_dry,
        "stuck_dataset": _load_json(stuck_meta),
        "continuation_dataset": _load_json(cont_meta),
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
