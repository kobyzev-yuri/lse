"""Digest snapshot archive / list / load (no LLM)."""

from __future__ import annotations

import json
from pathlib import Path

from services.notebook_news_digest import (
    archive_digest_snapshot,
    list_digest_snapshots,
    load_digest_snapshot,
    prune_digest_snapshots,
    _snapshot_payload,
)


def _snap(gen: str, kept: int = 1) -> dict:
    return _snapshot_payload(
        digest={
            "date": gen[:10],
            "filtered": 10,
            "kept": kept,
            "trashed": 5,
            "signals": [{"sym": "MSFT", "text": "x"}],
            "risks": [],
            "macro": [],
            "newtickers": [],
            "trashNote": "",
        },
        universe={"group3_union": ["MSFT"]},
        generated_at_utc=gen,
        pipeline={
            "lookback_hours": 72,
            "kb_source": "ALL",
            "include_macro": True,
            "include_earnings": True,
            "raw_item_count": 10,
            "deduped_drop": 2,
            "requested_ticker_count": 1,
        },
        requested_tickers=["MSFT"],
    )


def test_archive_list_load_prune(tmp_path: Path, monkeypatch):
    import services.notebook_news_digest as m

    monkeypatch.setattr(m, "DEFAULT_DIGEST_PATH", tmp_path / "digest_latest.json")
    monkeypatch.setattr(m, "get_config_value", lambda k, d=None: "14" if k == "NOTEBOOK_DIGEST_RETAIN_DAYS" else d)

    d = tmp_path / "digests"
    p1 = archive_digest_snapshot(_snap("2026-07-20T08:30:00+00:00", kept=1), directory=d)
    p2 = archive_digest_snapshot(_snap("2026-07-28T08:30:00+00:00", kept=3), directory=d)
    assert p1.is_file() and p2.is_file()

    rows = list_digest_snapshots(directory=d, limit=10)
    assert len(rows) >= 2
    assert rows[0]["id"].startswith("20260728") or rows[0]["kept"] == 3

    loaded = load_digest_snapshot(p2.stem, directory=d)
    assert loaded is not None
    assert loaded["digest"]["kept"] == 3
    assert loaded["pipeline"]["lookback_hours"] == 72

    # Force prune everything by zero retain via mtime trick: retain_days=0 not allowed (min 1);
    # create an old file and prune with retain_days=1 after touching mtime far past.
    old = d / "20200101T000000Z.json"
    old.write_text(json.dumps(_snap("2020-01-01T00:00:00+00:00")), encoding="utf-8")
    import os
    import time

    os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    removed = prune_digest_snapshots(retain_days=14, directory=d)
    assert removed >= 1
    assert not old.exists()
