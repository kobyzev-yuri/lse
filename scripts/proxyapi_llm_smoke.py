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
    p = argparse.ArgumentParser(description="Smoke: один LLM-запрос по OPENAI_* из config.env")
    p.add_argument("--timeout", type=int, default=None, help="Переопределить OPENAI_TIMEOUT (сек)")
    args = p.parse_args()

    from config_loader import load_config

    cfg = load_config()
    key = (cfg.get("OPENAI_GPT_KEY") or cfg.get("OPENAI_API_KEY") or "").strip()
    base = (cfg.get("OPENAI_BASE_URL") or "https://api.proxyapi.ru/openai/v1").strip().rstrip("/")
    model = (cfg.get("OPENAI_MODEL") or "gpt-4o").strip()
    timeout = int(args.timeout if args.timeout is not None else (cfg.get("OPENAI_TIMEOUT") or "60"))

    if not key:
        print("Нет OPENAI_API_KEY / OPENAI_GPT_KEY в config.env", file=sys.stderr)
        return 1

    print(f"base_url={base}\nmodel={model}\ntimeout={timeout}s")
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
                "Похоже на клиентский таймаут — увеличьте OPENAI_TIMEOUT (для claude-opus 180–600).",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
