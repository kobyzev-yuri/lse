"""Predefined multi-parameter GAME_5M tuning bundles (coordinated policy changes)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Game5mTuningBundle:
    bundle_id: str
    description_ru: str
    changes: dict[str, str]
    observe_days_default: int = 5
    rationale_ru: str = ""


# Session 2026-06-16: blind EOD flat cut bullish multiday positions; early_derisk ignored hold gate.
OVERNIGHT_MULTIDAY_V1 = Game5mTuningBundle(
    bundle_id="overnight_multiday_v1",
    description_ru="Multiday-driven overnight: без blind EOD flat, selective overnight + hold gate apply",
    rationale_ru=(
        "EOD_FLATTEN_ALWAYS обнулял multiday 1–3d; hold_gate был log_only. "
        "Пакет: flat только при медвежьем multiday / premarket gap; "
        "early_derisk откладывается при 2+ бычьих горизонтах (до −4%)."
    ),
    observe_days_default=5,
    changes={
        "GAME_5M_EOD_FLATTEN_ALWAYS": "false",
        "GAME_5M_EOD_FLATTEN_ENABLED": "true",
        "GAME_5M_EOD_FLATTEN_ALLOW_STRONG_BUY_HOLD": "true",
        "GAME_5M_EOD_FLATTEN_ALLOW_HOLD_ON_BULLISH_MULTIDAY": "true",
        "GAME_5M_MULTIDAY_OVERNIGHT_GATE_MODE": "apply",
        "GAME_5M_MULTIDAY_HOLD_GATE_MODE": "apply",
        "GAME_5M_MULTIDAY_HOLD_TAU_PCT": "0.20",
        "GAME_5M_MULTIDAY_HOLD_POSITIVE_HORIZONS_MIN": "2",
        "GAME_5M_MULTIDAY_HOLD_MAX_LOSS_PCT": "-4.0",
        "GAME_5M_PREMARKET_AUTO_FLAT_ENABLED": "true",
        "GAME_5M_PREMARKET_AUTO_FLAT_USE_MULTIDAY": "true",
        "GAME_5M_PREMARKET_FLAT_ALLOW_HOLD_ON_BULLISH_MULTIDAY": "true",
        "GAME_5M_PREMARKET_FLAT_HOLD_ON_GAP_REVERSAL_REGIME": "true",
        "GAME_5M_PREMARKET_RECOVERY_ML_ENABLED": "true",
        "GAME_5M_PREMARKET_RECOVERY_ML_HOLD_ON_WOULD_DEFER": "true",
    },
)

BUNDLES: dict[str, Game5mTuningBundle] = {
    OVERNIGHT_MULTIDAY_V1.bundle_id: OVERNIGHT_MULTIDAY_V1,
}

INTRADAY_REGIME_V1 = Game5mTuningBundle(
    bundle_id="intraday_regime_v1",
    description_ru="Режимный роутер: chop vs impulse — вход/тейк/EOD",
    rationale_ru=(
        "Боковик: блок слабого buy_rth_momentum (<1.5%), ниже take cap, мягкий тейк 2%, "
        "EOD-flat при −0.35%. Импульс: factor×1.15 на тейк. fade_extended: veto входа."
    ),
    observe_days_default=5,
    changes={
        "GAME_5M_INTRADAY_REGIME_ENABLED": "true",
        "GAME_5M_INTRADAY_REGIME_GATE_MODE": "apply",
        "GAME_5M_INTRADAY_REGIME_CHOP_ENTRY_MOMENTUM_BUY_MIN": "1.5",
        "GAME_5M_INTRADAY_REGIME_CHOP_TAKE_CAP_MULT": "0.85",
        "GAME_5M_INTRADAY_REGIME_CHOP_MOMENTUM_FACTOR_MULT": "0.9",
        "GAME_5M_INTRADAY_REGIME_CHOP_SOFT_TAKE_MIN_PCT": "2.0",
        "GAME_5M_INTRADAY_REGIME_CHOP_SOFT_TAKE_REGULAR_ENABLED": "true",
        "GAME_5M_INTRADAY_REGIME_CHOP_EOD_MAX_LOSS_TO_FORCE_PCT": "-0.35",
        "GAME_5M_INTRADAY_REGIME_IMPULSE_MOMENTUM_FACTOR_MULT": "1.15",
        "GAME_5M_RTH_MOMENTUM_BUY_MIN": "1.2",
    },
)

BUNDLES["intraday_regime_v1"] = INTRADAY_REGIME_V1

# Post-mortem 2026-07-06: fusion FP at P≈0.51–0.52, weak buy_premarket_momentum (MU/TER).
ENTRY_FUSION_TIGHTEN_V1 = Game5mTuningBundle(
    bundle_id="entry_fusion_tighten_v1",
    description_ru="Ужесточение входа: CatBoost fusion + премаркет-импульс",
    rationale_ru=(
        "Rolling 14d: A=5, fusion false positive=3. Поднять HOLD_BELOW_P с 0.45→0.50; "
        "premarket momentum buy min 0.5→1.0 (слабый дрейф премаркета не даёт BUY)."
    ),
    observe_days_default=5,
    changes={
        "GAME_5M_CATBOOST_HOLD_BELOW_P": "0.50",
        "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN": "1.0",
    },
)

BUNDLES[ENTRY_FUSION_TIGHTEN_V1.bundle_id] = ENTRY_FUSION_TIGHTEN_V1

# Post-mortem 2026-07-11: gap-down regime — legacy ignored stack premarket/advice gates.
MARKET_ADAPT_V1 = Game5mTuningBundle(
    bundle_id="market_adapt_v1",
    description_ru="Адаптация к gap-down: legacy entry guards + режим + тейк",
    rationale_ru=(
        "07.07: 5 BUY при gap ≤ −2% (stack HOLD, legacy BUY). "
        "Включить apply на legacy: premarket_gap_baseline + entry_advice; "
        "intraday_regime apply; TAKE_PROFIT_MIN 4→1.5% для захвата импульса. "
        "Bar v2 обучение — без изменений (DATASET_VERSION=bar, weekly refresh)."
    ),
    observe_days_default=5,
    changes={
        "GAME_5M_PREMARKET_GAP_BASELINE_GATE_MODE": "apply",
        "GAME_5M_ENTRY_ADVICE_GATE_MODE": "apply",
        "GAME_5M_INTRADAY_REGIME_ENABLED": "true",
        "GAME_5M_INTRADAY_REGIME_GATE_MODE": "apply",
        "GAME_5M_INTRADAY_REGIME_CHOP_ENTRY_MOMENTUM_BUY_MIN": "1.5",
        "GAME_5M_INTRADAY_REGIME_CHOP_TAKE_CAP_MULT": "0.85",
        "GAME_5M_INTRADAY_REGIME_CHOP_MOMENTUM_FACTOR_MULT": "0.9",
        "GAME_5M_INTRADAY_REGIME_CHOP_SOFT_TAKE_MIN_PCT": "2.0",
        "GAME_5M_INTRADAY_REGIME_CHOP_SOFT_TAKE_REGULAR_ENABLED": "true",
        "GAME_5M_INTRADAY_REGIME_IMPULSE_MOMENTUM_FACTOR_MULT": "1.15",
        "GAME_5M_RTH_MOMENTUM_BUY_MIN": "1.2",
        "GAME_5M_TAKE_PROFIT_MIN_PCT": "1.5",
    },
)

BUNDLES[MARKET_ADAPT_V1.bundle_id] = MARKET_ADAPT_V1

# 2026-07-12: B1–B6 no-go (bar v2 fusion sweep + exit ML без PnL-edge) — freeze telemetry/train.
ML_FREEZE_B_CONTOURS_V1 = Game5mTuningBundle(
    bundle_id="ml_freeze_b_contours_v1",
    description_ru="Freeze B-контуров: hold/continuation/multiday-hold/earnings-grid/light-path",
    rationale_ru=(
        "bar_v2 fusion no-go (Spearman≈0, precision~0.45); B1–B6 без перспективы на hot path. "
        "Отключить ML-телеметрию exit/continuation, train/cron для bar v2/continuation/earnings_grid; "
        "multiday entry и market_adapt guards не трогаем."
    ),
    observe_days_default=0,
    changes={
        "GAME_5M_CATBOOST_FUSION": "none",
        "GAME_5M_HOLD_QUALITY_LOG_ENABLED": "false",
        "GAME_5M_CONTINUATION_ML_ENABLED": "false",
        "GAME_5M_CONTINUATION_ML_LOG_ONLY": "true",
        "GAME_5M_CONTINUATION_ML_GATE_MODE": "none",
        "GAME_5M_CONTINUATION_GATE_ENABLED": "false",
        "GAME_5M_MULTIDAY_HOLD_GATE_MODE": "none",
        "DAILY_ML_RUN_ENTRY_BAR_V2_APPLY": "0",
        "DAILY_ML_RUN_CONTINUATION_DATASET": "0",
        "ML_READINESS_SKIP_GAME5M": "1",
        "ML_READINESS_SKIP_EARNINGS_INTELLIGENCE": "1",
    },
)

BUNDLES[ML_FREEZE_B_CONTOURS_V1.bundle_id] = ML_FREEZE_B_CONTOURS_V1


def get_bundle(bundle_id: str) -> Game5mTuningBundle:
    bid = str(bundle_id or "").strip()
    if bid not in BUNDLES:
        raise KeyError(f"unknown bundle_id: {bid}")
    return BUNDLES[bid]


def list_bundles() -> list[dict[str, Any]]:
    return [
        {
            "bundle_id": b.bundle_id,
            "description_ru": b.description_ru,
            "rationale_ru": b.rationale_ru,
            "observe_days_default": b.observe_days_default,
            "keys": sorted(b.changes.keys()),
        }
        for b in BUNDLES.values()
    ]
