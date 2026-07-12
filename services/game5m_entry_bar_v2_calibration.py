"""Bar v2 probability calibration for BUY-only deployment population."""
from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Any, Iterable, Sequence

BUY_SIGNAL_KIND = "buy_signal"
BUY_DECISIONS = frozenset({"BUY", "STRONG_BUY"})

# Gates aligned with GAME_5M bar v2 calibration playbook (2026-07).
DEFAULT_MIN_STD_P = 0.03
DEFAULT_MAX_ECE = 0.08
DEFAULT_MIN_AUC_BUY = 0.55


def _clamp_prob(p: float, eps: float = 1e-6) -> float:
    return min(max(float(p), eps), 1.0 - eps)


def logit(p: float, eps: float = 1e-6) -> float:
    p = _clamp_prob(p, eps)
    return math.log(p / (1.0 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def is_buy_bar_row(raw: dict[str, Any]) -> bool:
    kind = str(raw.get("sample_kind") or "").strip()
    if kind == BUY_SIGNAL_KIND:
        return True
    decision = str(raw.get("technical_decision") or "").strip().upper()
    return decision in BUY_DECISIONS


def fit_platt_calibrator(raw_probs: Sequence[float], labels: Sequence[int]) -> dict[str, Any]:
    """Logistic calibration on logit(raw P). Stdlib-only gradient descent."""
    if len(raw_probs) < 5 or len(set(labels)) < 2:
        return {"method": "identity", "reason": "insufficient_rows_or_single_class"}

    xs = [logit(p) for p in raw_probs]
    ys = [float(y) for y in labels]
    a, b = 1.0, 0.0
    n = len(xs)
    lr = 0.05
    for _ in range(400):
        grad_a = 0.0
        grad_b = 0.0
        for x, y in zip(xs, ys):
            z = a * x + b
            sig = sigmoid(z)
            err = sig - y
            grad_a += err * x
            grad_b += err
        a -= lr * grad_a / n
        b -= lr * grad_b / n
    return {"method": "platt", "a": round(a, 6), "b": round(b, 6)}


def apply_calibrator(p_raw: float, calibrator: dict[str, Any] | None) -> float:
    if not calibrator:
        return float(p_raw)
    method = str(calibrator.get("method") or "identity")
    if method == "identity":
        return float(p_raw)
    if method == "platt":
        z = float(calibrator.get("a", 1.0)) * logit(float(p_raw)) + float(calibrator.get("b", 0.0))
        return round(sigmoid(z), 6)
    return float(p_raw)


def apply_calibrator_batch(raw_probs: Iterable[float], calibrator: dict[str, Any] | None) -> list[float]:
    return [apply_calibrator(p, calibrator) for p in raw_probs]


def expected_calibration_error(
    probs: Sequence[float],
    labels: Sequence[int],
    *,
    n_bins: int = 10,
) -> float:
    if not probs:
        return float("nan")
    pairs = sorted(zip(probs, labels), key=lambda x: x[0])
    n = len(pairs)
    bin_size = max(1, n // n_bins)
    ece = 0.0
    for i in range(0, n, bin_size):
        chunk = pairs[i : i + bin_size]
        if not chunk:
            continue
        ps = [p for p, _ in chunk]
        ys = [y for _, y in chunk]
        conf = mean(ps)
        acc = mean(ys)
        ece += abs(acc - conf) * (len(chunk) / n)
    return round(ece, 6)


def probability_std(probs: Sequence[float]) -> float:
    if len(probs) < 2:
        return 0.0
    return round(pstdev(probs), 6)


def roc_auc_score_safe(labels: Sequence[int], probs: Sequence[float]) -> float:
    if len(set(labels)) < 2 or not probs:
        return float("nan")
    try:
        from sklearn.metrics import roc_auc_score

        return float(roc_auc_score(labels, probs))
    except Exception:
        # Mann-Whitney fallback
        pos = [p for p, y in zip(probs, labels) if y == 1]
        neg = [p for p, y in zip(probs, labels) if y == 0]
        if not pos or not neg:
            return float("nan")
        wins = sum(1 for p in pos for n in neg if p > n)
        ties = sum(1 for p in pos for n in neg if p == n)
        return (wins + 0.5 * ties) / (len(pos) * len(neg))


def calibration_gate_thresholds() -> dict[str, float]:
    from config_loader import get_config_value

    def _f(key: str, default: float) -> float:
        raw = (get_config_value(key, "") or "").strip().replace(",", ".")
        if not raw:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    return {
        "min_std_p": _f("GAME_5M_ENTRY_BAR_V2_CALIBRATION_MIN_STD_P", DEFAULT_MIN_STD_P),
        "max_ece": _f("GAME_5M_ENTRY_BAR_V2_CALIBRATION_MAX_ECE", DEFAULT_MAX_ECE),
        "min_auc_buy": _f("GAME_5M_ENTRY_BAR_V2_CALIBRATION_MIN_AUC_BUY", DEFAULT_MIN_AUC_BUY),
    }


def evaluate_calibration_gates(
    metrics: dict[str, Any],
    *,
    min_std_p: float | None = None,
    max_ece: float | None = None,
    min_auc_buy: float | None = None,
) -> dict[str, Any]:
    thr = calibration_gate_thresholds()
    min_std = DEFAULT_MIN_STD_P if min_std_p is None else min_std_p
    max_ece_v = DEFAULT_MAX_ECE if max_ece is None else max_ece
    min_auc = DEFAULT_MIN_AUC_BUY if min_auc_buy is None else min_auc_buy
    if min_std_p is None:
        min_std = thr["min_std_p"]
    if max_ece is None:
        max_ece_v = thr["max_ece"]
    if min_auc_buy is None:
        min_auc = thr["min_auc_buy"]

    std_cal = metrics.get("std_p_calibrated_valid")
    ece_cal = metrics.get("ece_calibrated_valid")
    auc_buy = metrics.get("auc_valid_buy_only")

    checks = {
        "std_p_calibrated_valid": {
            "value": std_cal,
            "min": min_std,
            "pass": std_cal is not None and float(std_cal) >= min_std,
        },
        "ece_calibrated_valid": {
            "value": ece_cal,
            "max": max_ece_v,
            "pass": ece_cal is not None and float(ece_cal) < max_ece_v,
        },
        "auc_valid_buy_only": {
            "value": auc_buy,
            "min": min_auc,
            "pass": auc_buy is not None and float(auc_buy) >= min_auc,
        },
    }
    fusion_ready = all(c["pass"] for c in checks.values())
    return {
        "fusion_calibration_ready": fusion_ready,
        "gate_checks": checks,
        "thresholds": {"min_std_p": min_std, "max_ece": max_ece_v, "min_auc_buy": min_auc},
    }


def build_calibration_block(
    *,
    raw_probs_valid: Sequence[float],
    labels_valid: Sequence[int],
    auc_valid_all: float | None = None,
) -> dict[str, Any]:
    calibrator = fit_platt_calibrator(raw_probs_valid, labels_valid)
    cal_probs = apply_calibrator_batch(raw_probs_valid, calibrator)
    auc_buy = roc_auc_score_safe(labels_valid, cal_probs)
    block: dict[str, Any] = {
        "calibrator": calibrator,
        "auc_valid_buy_only": round(auc_buy, 4) if auc_buy == auc_buy else None,
        "auc_valid_all": auc_valid_all,
        "std_p_raw_valid": probability_std(raw_probs_valid),
        "std_p_calibrated_valid": probability_std(cal_probs),
        "ece_raw_valid": expected_calibration_error(raw_probs_valid, labels_valid),
        "ece_calibrated_valid": expected_calibration_error(cal_probs, labels_valid),
        "n_valid_calibrated": len(raw_probs_valid),
        "p_raw_valid_min": round(min(raw_probs_valid), 4) if raw_probs_valid else None,
        "p_raw_valid_max": round(max(raw_probs_valid), 4) if raw_probs_valid else None,
        "p_calibrated_valid_min": round(min(cal_probs), 4) if cal_probs else None,
        "p_calibrated_valid_max": round(max(cal_probs), 4) if cal_probs else None,
    }
    gates = evaluate_calibration_gates(block)
    block.update(gates)
    return block
