#!/usr/bin/env python3
"""Apply a predefined GAME_5M tuning bundle (coordinated policy pack)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply GAME_5M tuning bundle via controller")
    parser.add_argument("--bundle-id", default="overnight_multiday_v1")
    parser.add_argument("--observe-days", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--relaxed", action="store_true", default=True)
    parser.add_argument("--ledger", default="")
    args = parser.parse_args()

    cmd = [
        sys.executable,
        str(project_root / "scripts" / "game5m_tuning_controller.py"),
        "apply-bundle",
        "--bundle-id",
        args.bundle_id,
    ]
    if args.observe_days:
        cmd.extend(["--observe-days", str(args.observe_days)])
    if args.dry_run:
        cmd.append("--dry-run")
    if args.force:
        cmd.append("--force")
    if args.relaxed:
        cmd.append("--relaxed")
    if args.ledger:
        cmd.extend(["--ledger", args.ledger])

    raise SystemExit(subprocess.call(cmd, cwd=str(project_root)))


if __name__ == "__main__":
    main()
