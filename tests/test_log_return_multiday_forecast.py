"""Synthetic checks for ridge multiday log-return helper (no network)."""

import numpy as np

from services.log_return_multiday_forecast import _build_feature_row, _ridge_weights, _aligned_lr


def test_aligned_lr_and_feature_row():
    # Mild uptrend
    c = np.array([100.0, 101.0, 100.5, 102.0, 101.0, 103.0, 102.5, 104.0, 103.0, 105.0, 104.5], dtype=float)
    lr = _aligned_lr(c)
    assert np.isnan(lr[0])
    i = 10
    row = _build_feature_row(c, lr, i, vol_window=10, mean_window=5)
    assert row is not None
    assert row.shape[0] == 7
    assert row[0] == 1.0


def test_ridge_weights_shape():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(50, 7))
    X[:, 0] = 1.0
    y = X @ np.array([0.1, 0.2, -0.05, 0.0, 0.15, -0.3, 0.02]) + rng.normal(scale=0.01, size=50)
    w = _ridge_weights(X, y, 1.0)
    assert w.shape == (7,)
    pred = X[:3] @ w
    assert pred.shape == (3,)
