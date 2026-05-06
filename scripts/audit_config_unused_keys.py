#!/usr/bin/env python3
"""
Config/code audit (two directions):

1) config -> code: find keys present in config.env(.example) that have no string hits in the repo.
2) code -> config: find keys read by code (get_config_value/os.getenv) that are missing from config.env.example.

The goal is to help remove abandoned config keys AND eventually delete dead code paths behind them.

Notes:
- This is static and conservative. "No hits" is a strong signal; "hit" does not guarantee runtime use.
- Some keys may be used externally (docker-compose, cron, infra); keep that in mind before deletion.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple


def _iter_config_keys(path: Path, *, include_commented: bool = False) -> list[str]:
    keys: list[str] = []
    if not path.is_file():
        return keys
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if not include_commented:
                continue
            # Allow commented example lines like: "# FOO=bar" or "#FOO=bar"
            line = line.lstrip("#").strip()
            if not line:
                continue
        if "=" not in line:
            continue
        k = line.split("=", 1)[0].strip()
        if not k or any(ch.isspace() for ch in k):
            continue
        keys.append(k)
    # keep stable order, unique
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _extract_code_keys(text: str) -> Set[str]:
    """
    Extract env key literals used by code.
    We intentionally focus on the dominant patterns:
      - get_config_value("KEY", ...)
      - os.getenv("KEY", ...) / os.environ.get("KEY", ...)
    """
    keys: Set[str] = set()
    # get_config_value("FOO", ...) and get_config_value('FOO', ...)
    for m in re.finditer(r"get_config_value\(\s*([\"'])([A-Z0-9_]+)\1", text):
        keys.add(m.group(2))
    # os.getenv("FOO") / os.environ.get("FOO")
    for m in re.finditer(r"os\.(?:getenv|environ\.get)\(\s*([\"'])([A-Z0-9_]+)\1", text):
        keys.add(m.group(2))
    return keys


def _iter_files(root: Path, globs: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for g in globs:
        files.extend(root.glob(g))
    # filter out huge/irrelevant
    out = []
    for p in files:
        if not p.is_file():
            continue
        if "/.git/" in str(p):
            continue
        out.append(p)
    return sorted(set(out))


def _scan_repo_for_config_usage(root: Path) -> Tuple[Set[str], Dict[str, List[str]]]:
    """
    Returns:
      - all keys referenced in code (best-effort)
      - per key list of files where it was found (first few)
    """
    files = _iter_files(root, ["**/*.py"])
    found: Set[str] = set()
    files_by_key: Dict[str, List[str]] = {}
    ignore_files = {
        "scripts/audit_config_unused_keys.py",
    }
    ignore_keys = {
        # Common placeholders / not a config contract
        "FOO",
        "KEY",
        # Generic web-app env used outside this repo’s config convention
        "HOST",
        "PORT",
    }
    for p in files:
        try:
            rel = str(p.relative_to(root))
        except Exception:
            rel = str(p)
        if rel in ignore_files:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        ks = _extract_code_keys(text)
        if not ks:
            continue
        for k in ks:
            if k in ignore_keys:
                continue
            found.add(k)
            files_by_key.setdefault(k, [])
            if len(files_by_key[k]) < 10:
                files_by_key[k].append(rel)
    return found, files_by_key


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str, default="config.env", help="Path to config.env (or config.env.example)")
    ap.add_argument("--root", type=str, default=".", help="Repo root to scan")
    ap.add_argument("--prefix", type=str, default="", help="Optional key prefix filter (e.g. GAME_5M_)")
    ap.add_argument(
        "--mode",
        type=str,
        default="config_to_code",
        choices=["config_to_code", "code_to_example"],
        help="config_to_code: keys in config with no repo hits; code_to_example: keys read by code missing in config.env.example",
    )
    args = ap.parse_args()

    root = Path(args.root).resolve()
    cfg_path = (root / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)

    if args.mode == "config_to_code":
        keys = _iter_config_keys(cfg_path)
        if args.prefix:
            keys = [k for k in keys if k.startswith(args.prefix)]
        if not keys:
            print(f"No keys found in {cfg_path}")
            return 2

        # Scan across repo (code+docs+scripts) for literal key mentions.
        files = _iter_files(root, ["**/*.py", "**/*.md", "**/*.html", "**/*.sh", "**/*.env*"])
        hits: dict[str, int] = {k: 0 for k in keys}
        pat_cache: dict[str, re.Pattern[str]] = {}
        for p in files:
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for k in keys:
                if hits[k] > 0:
                    continue
                pat = pat_cache.get(k)
                if pat is None:
                    pat = re.compile(rf"(?<![A-Z0-9_]){re.escape(k)}(?![A-Z0-9_])")
                    pat_cache[k] = pat
                if pat.search(text):
                    hits[k] = 1
        no_hits = [k for k, v in hits.items() if v == 0]
        used = [k for k, v in hits.items() if v > 0]
        print(f"[config_to_code] Config: {cfg_path}")
        print(f"Scanned files: {len(files)}")
        print(f"Keys total: {len(keys)}")
        print(f"Used (string-hit): {len(used)}")
        print(f"No-hits candidates: {len(no_hits)}")
        for k in no_hits:
            print(k)
        return 0

    # code_to_example
    code_keys, files_by_key = _scan_repo_for_config_usage(root)
    example_path = root / "config.env.example"
    # In example file many keys are intentionally commented-out; include them.
    example_keys = set(_iter_config_keys(example_path, include_commented=True))
    missing = sorted([k for k in code_keys if k not in example_keys and (not args.prefix or k.startswith(args.prefix))])
    print(f"[code_to_example] Repo root: {root}")
    print(f"Example: {example_path}")
    print(f"Keys read by code: {len(code_keys)}")
    print(f"Keys in config.env.example: {len(example_keys)}")
    print(f"Missing from example: {len(missing)}")
    for k in missing:
        locs = ", ".join(files_by_key.get(k, [])[:3])
        print(f"{k}  # {locs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

