#!/usr/bin/env python3
"""
Один короткий chat/completions к тому же base_url/model, что и LLMService (config.env).
Проверка ключа ProxyAPI, маршрута и достаточности таймаута.

  cd /path/to/lse && python scripts/proxyapi_llm_smoke.py
  python scripts/proxyapi_llm_smoke.py --timeout 300
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))


def main() -> int:
    p = argparse.ArgumentParser(description="Smoke: один LLM-запрос как LLMService (OPENAI_* и/или ANTHROPIC_*)")
    p.add_argument("--timeout", type=int, default=None, help="Переопределить таймаут клиента (сек)")
    args = p.parse_args()

    from services.llm_service import LLMService

    llm = LLMService()
    key = llm.api_key
    base = llm.base_url
    model = llm.model
    timeout = int(args.timeout if args.timeout is not None else llm.timeout)

    if not key:
        print("Нет ключа (OPENAI_API_KEY или ANTHROPIC_API_KEY) в config.env", file=sys.stderr)
        return 1

    print(f"provider={getattr(llm, 'llm_provider', '?')}\nbase_url={base}\nmodel={model}\ntimeout={timeout}s")
    try:
        from openai import OpenAI
    except ImportError:
        print("Установите openai: pip install openai", file=sys.stderr)
        return 1

    client = OpenAI(api_key=key, base_url=base, timeout=timeout)
    t0 = time.perf_counter()
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly one word: OK"}],
            max_tokens=24,
            temperature=0,
        )
        dt = time.perf_counter() - t0
        text = (r.choices[0].message.content or "").strip()
        print(f"OK in {dt:.1f}s: {text!r}")
        return 0
    except Exception as e:
        dt = time.perf_counter() - t0
        print(f"FAIL after {dt:.1f}s: {e}", file=sys.stderr)
        es = str(e).lower()
        if "timeout" in es or "timed out" in es:
            print(
                "Похоже на клиентский таймаут — увеличьте OPENAI_TIMEOUT / ANTHROPIC_TIMEOUT (для Opus 180–600).",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
