#!/usr/bin/env python3
"""
Проверка ключа OPENAI_GPT_KEY (или OPENAI_API_KEY) с прямым доступом к OpenAI API (gpt-4o).
Запуск: из корня репо  python scripts/check_openai_gpt_key.py

Если ключ рабочий — можно перейти на прямую работу с OpenAI:
  OPENAI_API_KEY=<тот же ключ>
  OPENAI_BASE_URL=https://api.openai.com/v1
  OPENAI_MODEL=gpt-4o
"""

import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

def main():
    from config_loader import get_config_value, load_config
    config = load_config()
    # Сначала OPENAI_GPT_KEY (для прямого OpenAI), затем OPENAI_API_KEY
    key = (config.get("OPENAI_GPT_KEY") or os.getenv("OPENAI_GPT_KEY") or
           config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        print("❌ Не найден OPENAI_GPT_KEY ни OPENAI_API_KEY в config.env или окружении.")
        sys.exit(1)
    # Скрываем ключ в выводе
    key_preview = key[:8] + "…" + key[-4:] if len(key) > 12 else "…"
    print(f"Ключ: {key_preview} (длина {len(key)})")
    print("Проверка запроса к OpenAI API (https://api.openai.com/v1), модель gpt-4o…")

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=key,
            base_url="https://api.openai.com/v1",
        )
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Ответь одним словом: ок"}],
            max_tokens=10,
        )
        text = (resp.choices[0].message.content or "").strip()
        print(f"✅ Ответ API: {text!r}")
        print("✅ Ключ работает с OpenAI gpt-4o напрямую.")
        print("")
        print("Чтобы заменить proxy на прямой OpenAI, в config.env задайте:")
        print("  OPENAI_API_KEY=<ваш ключ>")
        print("  OPENAI_BASE_URL=https://api.openai.com/v1")
        print("  OPENAI_MODEL=gpt-4o")
        return 0
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        err = str(e).lower()
        if "invalid_api_key" in err or "401" in err or "authentication" in err:
            print("   Похоже, ключ невалидный или не имеет доступа к OpenAI API.")
        elif "403" in str(e) or "unsupported_country" in err or "request_forbidden" in err:
            print("   Регион не поддерживается OpenAI (ожидаемо локально). На сервере (GCP и т.д. в разрешённом регионе) этот ключ должен работать — проверьте там.")
        elif "connection" in err or "timeout" in err:
            print("   Проблема сети или доступ к api.openai.com заблокирован.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
