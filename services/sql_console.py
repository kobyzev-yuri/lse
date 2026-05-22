"""Read-only SQL console: validate and run SELECT queries for ops/testing."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

DEFAULT_MAX_ROWS = 200
STATEMENT_TIMEOUT_MS = 30_000

_FORBIDDEN = re.compile(
    r"\b("
    r"insert|update|delete|drop|truncate|alter|create|grant|revoke|"
    r"copy|execute|call|merge|replace|attach|detach|vacuum|reindex|"
    r"refresh|comment|security|lock|notify|listen|unlisten|"
    r"pg_sleep|pg_terminate_backend|set\s+role|set\s+session"
    r")\b",
    re.IGNORECASE,
)

_SELECT_INTO = re.compile(r"\bselect\b[\s\S]*?\binto\b", re.IGNORECASE)
_LIMIT_NUM = re.compile(r"\blimit\s+(\d+)", re.IGNORECASE)


def _strip_sql_comments(sql: str) -> str:
    """Remove line comments (-- ...) for validation; keep string literals naive-safe."""
    lines = []
    for line in sql.splitlines():
        in_single = False
        out = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "'" and not in_single:
                in_single = True
                out.append(ch)
            elif ch == "'" and in_single:
                if i + 1 < len(line) and line[i + 1] == "'":
                    out.append("''")
                    i += 1
                else:
                    in_single = False
                    out.append(ch)
            elif not in_single and ch == "-" and i + 1 < len(line) and line[i + 1] == "-":
                break
            else:
                out.append(ch)
            i += 1
        lines.append("".join(out))
    return "\n".join(lines)


def validate_readonly_sql(sql: str, *, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """
    Ensure a single read-only SELECT (or WITH ... SELECT).
    Returns normalized SQL (trailing semicolon removed); may append LIMIT.
    """
    if not sql or not str(sql).strip():
        raise ValueError("Пустой запрос")

    raw = str(sql).strip()
    if ";" in raw.rstrip(";").strip():
        raise ValueError("Разрешён только один SQL-оператор (без ; в середине)")

    cleaned = _strip_sql_comments(raw).strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("Пустой запрос после удаления комментариев")

    lowered = cleaned.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("запрещены SQL-операторы кроме SELECT или WITH ... SELECT")

    if _SELECT_INTO.search(cleaned):
        raise ValueError("SELECT INTO запрещён")

    if _FORBIDDEN.search(cleaned):
        raise ValueError("Запрос содержит запрещённые ключевые слова (только чтение)")

    m = _LIMIT_NUM.search(cleaned)
    if m:
        n = int(m.group(1))
        if n > max_rows:
            raise ValueError(f"LIMIT не больше {max_rows}")
        return cleaned

    return f"{cleaned} LIMIT {max_rows}"


def run_readonly_sql(
    engine: Engine,
    sql: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    timeout_ms: int = STATEMENT_TIMEOUT_MS,
) -> Dict[str, Any]:
    """Execute validated read-only SQL; return columns, rows, timing."""
    executable = validate_readonly_sql(sql, max_rows=max_rows)
    t0 = time.perf_counter()
    with engine.connect() as conn:
        conn.execute(text(f"SET LOCAL statement_timeout = '{int(timeout_ms)}ms'"))
        df = pd.read_sql(text(executable), conn)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    if df.empty:
        columns: List[str] = list(df.columns)
        rows: List[List[Any]] = []
    else:
        columns = [str(c) for c in df.columns]
        rows = []
        for _, row in df.iterrows():
            out_row = []
            for v in row:
                if pd.isna(v):
                    out_row.append(None)
                elif hasattr(v, "isoformat"):
                    out_row.append(v.isoformat())
                else:
                    out_row.append(v)
            rows.append(out_row)

    return {
        "sql": executable,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "elapsed_ms": elapsed_ms,
        "max_rows": max_rows,
    }
