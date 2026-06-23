#!/usr/bin/env python3
"""Portfolio maintenance CLI — see services/portfolio_maintenance.py."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def main() -> int:
    from services.portfolio_maintenance import (
        close_indicator_legacy_positions,
        reconcile_portfolio_state,
        run_portfolio_maintenance,
    )

    p = argparse.ArgumentParser(description="Portfolio maintenance")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--close-indicators", action="store_true")
    p.add_argument("--reconcile-state", action="store_true")
    p.add_argument("--all", action="store_true")
    args = p.parse_args()
    do_all = args.all or (not args.close_indicators and not args.reconcile_state)
    if do_all:
        run_portfolio_maintenance(dry_run=args.dry_run)
        return 0
    if args.close_indicators:
        close_indicator_legacy_positions(dry_run=args.dry_run)
    if args.reconcile_state:
        reconcile_portfolio_state(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
