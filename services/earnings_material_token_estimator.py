"""Token estimates for earnings material text (cost planning, no hard dependency on tiktoken)."""
from __future__ import annotations

import math
import re
from typing import Any


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def estimate_tokens(text: str) -> dict[str, Any]:
    """
    Return char/word counts and token estimates for LLM cost planning.

    Primary estimate: chars / 3.7 (English financial prose heuristic).
    Conservative: chars / 4.0.
    If tiktoken is installed, also returns exact cl100k_base count.
    """
    body = text or ""
    chars = len(body)
    words = _word_count(body)
    est_primary = int(math.ceil(chars / 3.7)) if chars else 0
    est_conservative = int(math.ceil(chars / 4.0)) if chars else 0
    out: dict[str, Any] = {
        "chars": chars,
        "words": words,
        "tokens_est_primary": est_primary,
        "tokens_est_conservative": est_conservative,
        "tokens_exact": None,
        "tokenizer": "heuristic_chars_div_3_7",
    }
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        exact = len(enc.encode(body))
        out["tokens_exact"] = exact
        out["tokenizer"] = "tiktoken_cl100k_base"
    except Exception:
        pass
    return out


def extraction_cycle_tokens(
    material_tokens: int,
    *,
    system_prompt_tokens: int = 1800,
    output_tokens: int = 1200,
) -> dict[str, int]:
    """Rough total tokens per one LLM extraction pass (input + expected JSON output)."""
    input_tokens = int(material_tokens) + int(system_prompt_tokens)
    return {
        "input_tokens": input_tokens,
        "output_tokens_est": int(output_tokens),
        "total_tokens_est": input_tokens + int(output_tokens),
    }
