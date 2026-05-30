"""Scenario classifier dataset quality snapshot for readiness / analyzer."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.event_reaction_labeling import (
    FEATURE_BUILDER_VERSION_EARNINGS,
    FEATURE_BUILDER_VERSION_QUOTES,
    event_reaction_numeric_feature_keys,
)

LABEL_SOURCE = "llm_scenario_v0"
RULE_LABELS = frozenset({"UP", "DOWN", "FLAT"})


def _json_obj(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            o = json.loads(raw)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def load_scenario_training_frame(
    engine: Engine,
    *,
    dataset_version: str,
    feature_builder_version: str,
) -> pd.DataFrame:
    """Labeled rows with valid quotes_regime_earnings_v1 features — trainable classifier sample."""
    numeric_keys = event_reaction_numeric_feature_keys(feature_builder_version)
    quote_keys = event_reaction_numeric_feature_keys(FEATURE_BUILDER_VERSION_QUOTES)
    extra_keys = tuple(k for k in numeric_keys if k not in quote_keys)

    q = text(
        """
        SELECT id, symbol, event_time_et, final_label, label_source, features_before
        FROM event_reaction_dataset
        WHERE dataset_version = :dv
          AND final_label IS NOT NULL
          AND TRIM(final_label) <> ''
          AND label_source = :label_source
          AND features_before IS NOT NULL AND features_before <> '{}'::jsonb
          AND (features_before->>'feature_builder_version') = :fbv
        ORDER BY event_time_et NULLS LAST, id
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(
            q,
            conn,
            params={"dv": dataset_version, "fbv": feature_builder_version, "label_source": LABEL_SOURCE},
        )
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        label = str(r.get("final_label") or "").strip()
        if not label or label in RULE_LABELS:
            continue
        fb = _json_obj(r.get("features_before"))
        rec: dict[str, Any] = {
            "id": int(r["id"]),
            "symbol": str(r["symbol"]).strip().upper(),
            "event_time_et": r.get("event_time_et"),
            "target_scenario": label,
        }
        skip = False
        for k in quote_keys:
            try:
                fv = float(fb.get(k))
            except (TypeError, ValueError):
                skip = True
                break
            if not math.isfinite(fv):
                skip = True
                break
            rec[k] = fv
        if skip:
            continue
        for k in extra_keys:
            try:
                fv = float(fb.get(k)) if fb.get(k) is not None else 0.0
            except (TypeError, ValueError):
                fv = 0.0
            if not math.isfinite(fv):
                fv = 0.0
            rec[k] = fv
        rows.append(rec)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def collect_scenario_classifier_coverage(
    engine: Engine,
    *,
    dataset_version: str = "v0_expanded_baseline",
    feature_builder_version: str = FEATURE_BUILDER_VERSION_EARNINGS,
    since: str = "2026-01-01",
    universe: list[str] | None = None,
    min_class_samples: int = 2,
) -> dict[str, Any]:
    """
    Training dataset control for scenario CatBoost classifier.

    Surfaces gaps: hints not applied, labels without earnings_v1 features,
    universe tickers never labeled, sparse scenario classes.
    """
    from services.earnings_intelligence_universe import get_earnings_intelligence_universe

    sym_set = sorted({s.strip().upper() for s in (universe or get_earnings_intelligence_universe()) if s.strip()})
    out: dict[str, Any] = {
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "label_source": LABEL_SOURCE,
        "feature_builder_version": feature_builder_version,
    }

    try:
        train_frame = load_scenario_training_frame(
            engine,
            dataset_version=dataset_version,
            feature_builder_version=feature_builder_version,
        )
    except Exception as e:
        train_frame = None
        out["train_frame_error"] = str(e)

    n_trainable = int(len(train_frame)) if train_frame is not None else 0
    labels_by_symbol: dict[str, int] = {}
    labels_by_class: dict[str, int] = {}
    if train_frame is not None and not train_frame.empty:
        labels_by_symbol = dict(Counter(train_frame["symbol"].astype(str).str.upper()))
        labels_by_class = dict(Counter(train_frame["target_scenario"].astype(str)))

    sparse_classes = sorted(c for c, n in labels_by_class.items() if n < max(1, int(min_class_samples)))
    symbols_with_labels = set(labels_by_symbol.keys())
    symbols_without_labels = sorted(s for s in sym_set if s not in symbols_with_labels)

    hints_pending: list[dict[str, Any]] = []
    extract_missing: list[dict[str, Any]] = []
    label_no_features = 0
    n_llm_labels = 0
    n_events_with_hints = 0
    try:
        with engine.connect() as conn:
            n_llm_labels = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM event_reaction_dataset
                        WHERE dataset_version = :dv
                          AND label_source = :ls
                          AND final_label IS NOT NULL
                          AND TRIM(final_label) <> ''
                          AND final_label NOT IN ('UP','DOWN','FLAT')
                        """
                    ),
                    {"dv": dataset_version, "ls": LABEL_SOURCE},
                ).scalar()
                or 0
            )
            n_events_with_hints = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(DISTINCT ed.knowledge_base_id)
                        FROM earnings_event_detail ed
                        JOIN knowledge_base kb ON kb.id = ed.knowledge_base_id
                        WHERE ed.guidance_summary ? 'scenario_hints'
                          AND kb.ts::date >= CAST(:since AS date)
                          AND UPPER(TRIM(kb.ticker)) = ANY(:symbols)
                        """
                    ),
                    {"since": str(since)[:10], "symbols": sym_set},
                ).scalar()
                or 0
            )
            label_no_features = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM event_reaction_dataset erd
                        JOIN knowledge_base kb ON kb.id = erd.knowledge_base_id
                        WHERE erd.dataset_version = :dv
                          AND erd.label_source = :ls
                          AND kb.ts::date >= CAST(:since AS date)
                          AND UPPER(TRIM(erd.symbol)) = ANY(:symbols)
                          AND (
                            erd.features_before IS NULL
                            OR erd.features_before = '{}'::jsonb
                            OR (erd.features_before->>'feature_builder_version') IS DISTINCT FROM :fbv
                          )
                        """
                    ),
                    {
                        "dv": dataset_version,
                        "ls": LABEL_SOURCE,
                        "since": str(since)[:10],
                        "symbols": sym_set,
                        "fbv": feature_builder_version,
                    },
                ).scalar()
                or 0
            )
            pending_rows = conn.execute(
                text(
                    """
                    SELECT UPPER(TRIM(kb.ticker)) AS symbol, kb.ts::date AS event_date, erd.label_source,
                           jsonb_array_length(COALESCE(ed.guidance_summary->'scenario_hints', '[]'::jsonb)) AS n_hints
                    FROM earnings_event_detail ed
                    JOIN knowledge_base kb ON kb.id = ed.knowledge_base_id
                    JOIN event_reaction_dataset erd ON erd.knowledge_base_id = ed.knowledge_base_id
                        AND erd.dataset_version = :dv
                    WHERE ed.guidance_summary ? 'scenario_hints'
                      AND kb.ts::date >= CAST(:since AS date)
                      AND UPPER(TRIM(kb.ticker)) = ANY(:symbols)
                      AND (
                        erd.label_source IS DISTINCT FROM :ls
                        OR erd.final_label IS NULL
                        OR TRIM(erd.final_label) = ''
                        OR erd.final_label IN ('UP','DOWN','FLAT')
                      )
                    ORDER BY kb.ts DESC
                    LIMIT 48
                    """
                ),
                {
                    "dv": dataset_version,
                    "ls": LABEL_SOURCE,
                    "since": str(since)[:10],
                    "symbols": sym_set,
                },
            ).mappings()
            hints_pending: list[dict[str, Any]] = []
            extract_missing: list[dict[str, Any]] = []
            for r in pending_rows:
                item = {
                    "symbol": str(r["symbol"]),
                    "event_date": r["event_date"].isoformat() if r.get("event_date") else None,
                    "label_source": r.get("label_source"),
                    "n_hints": int(r.get("n_hints") or 0),
                }
                if int(r.get("n_hints") or 0) > 0:
                    hints_pending.append(item)
                else:
                    extract_missing.append(item)
    except Exception as e:
        out["coverage_error"] = str(e)

    symbols_with_hints_no_label = sorted({str(p["symbol"]) for p in hints_pending if p.get("symbol")})
    symbols_needing_extract = sorted({str(p["symbol"]) for p in extract_missing if p.get("symbol")})
    features_gap_rows = max(0, n_llm_labels - n_trainable)

    out.update(
        {
            "n_llm_labels": n_llm_labels,
            "n_trainable_rows": n_trainable,
            "n_events_with_hints": n_events_with_hints,
            "n_classes_distinct": len(labels_by_class),
            "labels_by_class": dict(sorted(labels_by_class.items(), key=lambda x: -x[1])[:12]),
            "labels_by_symbol_top": dict(sorted(labels_by_symbol.items(), key=lambda x: -x[1])[:12]),
            "sparse_classes_below_min_samples": sparse_classes,
            "min_class_samples_threshold": max(1, int(min_class_samples)),
            "symbols_without_labels": symbols_without_labels,
            "symbols_with_hints_pending_apply": symbols_with_hints_no_label,
            "hints_pending_apply": hints_pending[:12],
            "events_missing_llm_extract": extract_missing[:12],
            "symbols_needing_llm_extract": symbols_needing_extract[:12],
            "n_events_missing_llm_extract": len(extract_missing),
            "labels_without_earnings_v1_features": label_no_features,
            "features_gap_rows": features_gap_rows,
            "symbols_never_in_train": symbols_without_labels,
        }
    )
    return out
