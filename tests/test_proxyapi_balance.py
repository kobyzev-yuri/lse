from services.proxyapi_balance import (
    balance_error_payload,
    is_proxyapi_insufficient_balance,
    proxyapi_balance_user_message,
)


def test_detects_402_insufficient_balance():
    err = "Error code: 402 - Insufficient balance to run this request"
    assert is_proxyapi_insufficient_balance(err)
    payload = balance_error_payload(err)
    assert payload and payload["error_code"] == 402
    assert "proxyapi.ru" in proxyapi_balance_user_message(err)


def test_ignores_other_errors():
    assert not is_proxyapi_insufficient_balance("timeout connecting to host")
    assert balance_error_payload("timeout") is None
