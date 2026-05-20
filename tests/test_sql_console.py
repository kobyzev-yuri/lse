import pytest

from services.sql_console import DEFAULT_MAX_ROWS, validate_readonly_sql


def test_select_ok():
    sql = validate_readonly_sql("SELECT 1")
    assert sql.endswith(f"LIMIT {DEFAULT_MAX_ROWS}")


def test_with_cte_ok():
    sql = validate_readonly_sql("WITH x AS (SELECT 1 AS n) SELECT n FROM x LIMIT 5")
    assert "LIMIT 5" in sql
    assert "LIMIT 200" not in sql


def test_rejects_insert():
    with pytest.raises(ValueError, match="запрещ"):
        validate_readonly_sql("INSERT INTO t VALUES (1)")


def test_rejects_multiple_statements():
    with pytest.raises(ValueError, match="один"):
        validate_readonly_sql("SELECT 1; SELECT 2")


def test_rejects_delete():
    with pytest.raises(ValueError, match="запрещ"):
        validate_readonly_sql("DELETE FROM trade_history WHERE id = 1")


def test_limit_cap():
    with pytest.raises(ValueError, match="LIMIT"):
        validate_readonly_sql("SELECT 1 LIMIT 9999", max_rows=100)


def test_select_into_forbidden():
    with pytest.raises(ValueError, match="INTO"):
        validate_readonly_sql("SELECT * INTO tmp FROM quotes LIMIT 1")
