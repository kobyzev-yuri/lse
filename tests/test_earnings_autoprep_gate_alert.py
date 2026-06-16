"""Tests for earnings autoprep gate Telegram alert."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from services.earnings_autoprep_gate_alert import (
    maybe_notify_autoprep_gate_ready,
    read_autoprep_gate_state,
)


def _patch_state_path(state_path: Path):
    return patch(
        "services.earnings_autoprep_gate_alert.default_gate_state_path",
        return_value=state_path,
    )


def test_no_alert_when_still_not_ready(tmp_path: Path):
    state_path = tmp_path / "gate_state.json"
    with _patch_state_path(state_path):
        out = maybe_notify_autoprep_gate_ready(
            {"overall_earnings_autoprep_ready": False, "earnings_autoprep": {}},
            project_root=tmp_path,
        )
        state = read_autoprep_gate_state(project_root=tmp_path)
    assert out["flipped_to_ready"] is False
    assert state.get("was_ready") is False


@patch("services.earnings_autoprep_gate_alert.send_telegram_message", return_value=True)
@patch("services.earnings_autoprep_gate_alert.get_signal_chat_ids", return_value=["123"])
def test_alert_on_flip_to_ready(_chats, _send, tmp_path: Path):
    state_path = tmp_path / "gate_state.json"

    def _cfg(key, default=""):
        if key == "TELEGRAM_BOT_TOKEN":
            return "bot-token"
        if key == "EARNINGS_AUTOPREP_GATE_ALERT_TELEGRAM":
            return "true"
        return default

    with _patch_state_path(state_path), patch(
        "services.earnings_autoprep_gate_alert.get_config_value", side_effect=_cfg
    ):
        gates = {
            "overall_earnings_autoprep_ready": True,
            "overall_grid_ready": True,
            "overall_peer_spillover_ready": True,
            "earnings_autoprep": {
                "llm_scenario_labels": 42,
                "shadow_n_matured": 50,
                "shadow_sign_accuracy": 0.76,
            },
        }
        out = maybe_notify_autoprep_gate_ready(gates, project_root=tmp_path)
        state = read_autoprep_gate_state(project_root=tmp_path)
    assert out["flipped_to_ready"] is True
    assert _send.called
    assert state.get("was_ready") is True
    assert state.get("notified_at_utc")


@patch("services.earnings_autoprep_gate_alert.send_telegram_message", return_value=True)
@patch("services.earnings_autoprep_gate_alert.get_signal_chat_ids", return_value=["123"])
def test_no_repeat_alert_when_already_ready(_chats, _send, tmp_path: Path):
    state_path = tmp_path / "gate_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"was_ready": true}', encoding="utf-8")
    with _patch_state_path(state_path), patch(
        "services.earnings_autoprep_gate_alert.get_config_value", return_value="bot-token"
    ):
        maybe_notify_autoprep_gate_ready(
            {"overall_earnings_autoprep_ready": True, "earnings_autoprep": {}},
            project_root=tmp_path,
        )
    assert not _send.called
