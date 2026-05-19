"""Portfolio entry guards and analyst decision payload."""

from execution_agent import _parse_analyst_decision


def test_parse_analyst_decision_string():
    d, s, sl, tp = _parse_analyst_decision("BUY")
    assert d == "BUY"
    assert s is None and tp is None


def test_parse_analyst_decision_dict():
    raw = {
        "decision": "STRONG_BUY",
        "selected_strategy": "Momentum",
        "strategy_result": {"take_profit": 10.0, "stop_loss": 3.0},
    }
    d, s, sl, tp = _parse_analyst_decision(raw)
    assert d == "STRONG_BUY"
    assert s == "Momentum"
    assert tp == 10.0
    assert sl == 3.0


def test_analyst_decision_payload():
    from analyst_agent import AnalystAgent

    agent = AnalystAgent(use_llm=False)
    payload = agent._decision_payload(
        "HOLD",
        strategy_result={"take_profit": 8.0},
        technical_signal="BUY",
    )
    assert payload["decision"] == "HOLD"
    assert payload["strategy_result"]["take_profit"] == 8.0
