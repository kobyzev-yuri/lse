"""
LLM сервис для работы с GPT-4o и другими моделями через proxyapi.ru.
Поддержка сравнения нескольких моделей (LLM_COMPARE_MODELS).
"""

import os
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
import re
from openai import OpenAI

logger = logging.getLogger(__name__)


def _normalize_openai_model_id_for_heuristics(model: str) -> str:
    """Убирает префиксы провайдера (openai/..., chatgpt-...) для эвристик по имени."""
    m = (model or "").strip().lower()
    if "/" in m:
        m = m.split("/")[-1]
    return m


def chat_completion_token_limit_params(
    model: str,
    *,
    max_tokens: Optional[int] = None,
    max_completion_tokens: Optional[int] = None,
    default_limit: int = 2000,
) -> Dict[str, Any]:
    """
    Часть новых моделей OpenAI (gpt-5.x и др.) в chat.completions не принимает ``max_tokens`` —
    только ``max_completion_tokens`` (ошибка 400 unsupported_parameter).

    Принудительно: ``OPENAI_CHAT_USE_MAX_COMPLETION_TOKENS=1`` — всегда max_completion_tokens;
    ``0`` — всегда max_tokens (старые модели / совместимые прокси).
    """
    if max_completion_tokens is not None:
        return {"max_completion_tokens": int(max_completion_tokens)}
    limit = int(max_tokens if max_tokens is not None else default_limit)
    raw = (os.environ.get("OPENAI_CHAT_USE_MAX_COMPLETION_TOKENS") or "").strip().lower()
    if raw in ("1", "true", "yes"):
        return {"max_completion_tokens": limit}
    if raw in ("0", "false", "no"):
        return {"max_tokens": limit}
    mid = _normalize_openai_model_id_for_heuristics(model)
    if re.match(r"^gpt-5", mid) or mid.startswith("o1") or mid.startswith("o3") or mid.startswith("o4"):
        return {"max_completion_tokens": limit}
    return {"max_tokens": limit}


def format_game5m_execution_context_for_llm(technical_data: Dict[str, Any]) -> str:
    """
    Строки для user-промпта: импульс 2ч и тейк/стоп по правилам GAME_5m,
    чтобы LLM не подменял исполнение одной ячейкой таблицы прогноза.
    """
    lines: List[str] = []
    mom = technical_data.get("momentum_2h_pct")
    if mom is not None:
        try:
            lines.append(f"- Импульс 2ч (факт за ~2ч до spot): {float(mom):+.2f}%")
        except (TypeError, ValueError):
            pass
    eff = technical_data.get("effective_take_profit_pct")
    if eff is None:
        eff = technical_data.get("estimated_upside_pct_day")
    tp = technical_data.get("take_profit_pct")
    if eff is not None:
        try:
            lines.append(
                f"- Эффективный тейк по правилам GAME_5m (исполнение; в карточке часто «Upside день»): ≈{float(eff):.2f}%"
            )
        except (TypeError, ValueError):
            pass
    elif tp is not None:
        try:
            lines.append(f"- Базовый тейк из конфига (GAME_5m): ≈{float(tp):.2f}%")
        except (TypeError, ValueError):
            pass
    sl = technical_data.get("stop_loss_pct")
    if sl is not None:
        try:
            lines.append(f"- Стоп (если включён в стратегии): ≈{float(sl):.2f}%")
        except (TypeError, ValueError):
            pass
    if not lines:
        return ""
    return "\n" + "\n".join(lines)


def format_price_forecast_llm_block(technical_data: Dict[str, Any]) -> str:
    """
    Текст блока краткосрочного прогноза цены (price_forecast_5m) для промпта LLM.
    p50 на 120 мин согласован с той же 2ч-осью, что и импульс 2ч (см. docs/GAME_5M_PRICE_FORECAST.md).
    """
    summ = (technical_data.get("price_forecast_5m_summary") or "").strip()
    fc = technical_data.get("price_forecast_5m")
    lines: List[str] = []
    if summ:
        lines.append(summ)
    if isinstance(fc, dict) and fc.get("horizons"):
        spot = fc.get("spot")
        if spot is not None:
            try:
                lines.append(f"Реф. spot: {float(spot):.4f}")
            except (TypeError, ValueError):
                pass
        for h in (fc.get("horizons") or [])[:4]:
            m = h.get("minutes")
            if m is None:
                continue
            lo, mid, hi = h.get("p10_pct_vs_spot"), h.get("p50_pct_vs_spot"), h.get("p90_pct_vs_spot")
            ph = h.get("p_price_gt_spot")
            if lo is not None and mid is not None and hi is not None:
                try:
                    lines.append(
                        f"  {m} мин: [{float(lo):+.2f}% … {float(mid):+.2f}% … {float(hi):+.2f}%] P(>spot)≈{ph}"
                    )
                except (TypeError, ValueError):
                    lines.append(f"  {m} мин: p50≈{h.get('p50_price')}")
            else:
                lines.append(f"  {m} мин: p50≈{h.get('p50_price')}")
    if not lines:
        return ""
    return (
        "\n\nКраткосрочный прогноз цены по 5m (лог-норм. модель на лог-доходностях; горизонты 30/60/120 мин — ориентир, не гарантия). "
        "p50 на 120 мин — та же двухчасовая шкала (24×5м), что и оценка дрейфа/импульс 2ч; это согласованная математика, не две независимые метрики. "
        "Для формулировок о тейке опирайся на строки импульса и эффективного тейка в разделе «Технические данные» (если есть); по прогнозу для быстрых ориентиров смотри 30/60 мин (p50 или p90 как верх коридора), p10 — пол риска; не подменяй число тейка из стратегии ячейкой p50@120м:\n"
        + "\n".join(lines)
    )


# Базовые URL ProxyAPI по провайдерам (см. proxyapi.ru/docs)
PROXYAPI_OPENAI_BASE = "https://api.proxyapi.ru/openai/v1"
PROXYAPI_ANTHROPIC_BASE = "https://api.proxyapi.ru/anthropic/v1"
PROXYAPI_GOOGLE_BASE = "https://api.proxyapi.ru/google/v1"


def load_config():
    """Загружает конфигурацию из локального config.env или ../brats/config.env"""
    from config_loader import load_config as load_config_base
    return load_config_base()


def parse_compare_models(config: Dict[str, str]) -> List[Tuple[str, str]]:
    """
    Парсит LLM_COMPARE_MODELS в список (base_url, model).
    Формат: через запятую, каждый элемент — "model" (используется OPENAI_BASE_URL) или "provider|model".
    provider: openai, anthropic, google (базовые URL из ProxyAPI).
    Пример: gpt-4o,openai|gpt-5.2,anthropic|claude-opus-4-6,google|gemini-3.1-pro-preview
    """
    raw = config.get("LLM_COMPARE_MODELS", "").strip()
    if not raw:
        return []
    base_default = config.get("OPENAI_BASE_URL", PROXYAPI_OPENAI_BASE)
    provider_bases = {
        "openai": PROXYAPI_OPENAI_BASE,
        "anthropic": PROXYAPI_ANTHROPIC_BASE,
        "google": PROXYAPI_GOOGLE_BASE,
    }
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            left, model = part.split("|", 1)
            left, model = left.strip(), model.strip()
            base = provider_bases.get(left.lower()) or left  # если не provider, считаем полным URL
            result.append((base, model))
        else:
            result.append((base_default, part))
    return result


class LLMService:
    """
    Сервис для работы с LLM (GPT-4o через proxyapi.ru)
    """
    
    def __init__(self):
        """
        Инициализация LLM сервиса
        """
        config = load_config()
        # OPENAI_GPT_KEY — ключ для прямого доступа к OpenAI (gpt-4o); OPENAI_API_KEY — proxy или прямой
        self.api_key = (
            config.get("OPENAI_GPT_KEY") or os.getenv("OPENAI_GPT_KEY") or
            config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        )
        self.base_url = (config.get("OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.proxyapi.ru/openai/v1").strip().rstrip("/")
        self.model = config.get("OPENAI_MODEL", "gpt-4o")
        self.temperature = float(config.get("OPENAI_TEMPERATURE", "0.2"))
        self.timeout = int(config.get("OPENAI_TIMEOUT", "60"))
        
        if not self.api_key:
            logger.warning("⚠️ OPENAI_API_KEY не настроен, LLM функции будут недоступны")
            self.client = None
        else:
            # Инициализируем OpenAI клиент
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout
            )
            logger.info(f"✅ LLMService инициализирован (model={self.model}, base_url={self.base_url})")
    
    def generate_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Генерация ответа от LLM
        
        Args:
            messages: Список сообщений в формате OpenAI (role, content)
            system_prompt: Системный промпт
            **kwargs: Дополнительные параметры (temperature, max_tokens и т.д.)
            
        Returns:
            Ответ от LLM с метаданными
        """
        if not self.client:
            raise ValueError("LLMService не инициализирован (отсутствует OPENAI_API_KEY)")
        
        try:
            # Формируем список сообщений
            formatted_messages = []
            
            # Добавляем системный промпт
            if system_prompt:
                formatted_messages.append({
                    "role": "system",
                    "content": system_prompt
                })
            
            # Добавляем пользовательские сообщения
            formatted_messages.extend(messages)
            
            # Параметры запроса
            token_kw = chat_completion_token_limit_params(
                self.model,
                max_tokens=kwargs.get("max_tokens"),
                max_completion_tokens=kwargs.get("max_completion_tokens"),
                default_limit=2000,
            )
            request_params = {
                "model": self.model,
                "messages": formatted_messages,
                "temperature": kwargs.get("temperature", self.temperature),
                **token_kw,
                "timeout": self.timeout
            }
            
            # Выполняем запрос
            logger.info(f"Отправка запроса к LLM (model={self.model}, messages={len(formatted_messages)})")
            
            response = self.client.chat.completions.create(**request_params)
            
            # Извлекаем ответ
            assistant_message = response.choices[0].message.content
            
            return {
                "response": assistant_message,
                "model": response.model,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens
                },
                "finish_reason": response.choices[0].finish_reason
            }
            
        except Exception as e:
            logger.error(f"Ошибка генерации ответа от LLM: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise

    @staticmethod
    def get_entry_decision_system_prompt() -> str:
        """Системный промпт для принятия решения о входе (BUY/STRONG_BUY/HOLD). Единый источник текста."""
        return """Ты опытный финансовый аналитик, специализирующийся на техническом анализе и анализе новостей.
Твоя задача - проанализировать торговую ситуацию и дать рекомендацию: BUY, STRONG_BUY или HOLD.

Учитывай:
1. Технические индикаторы (тренд, волатильность)
2. Sentiment новостей
3. Контекст новостей
4. Риски
5. Геополитический риск: при существенных геополитических изменениях (эскалация, военные действия, удары, санкции, risk-off) — предпочтительнее HOLD и в reasoning/risks укажи рекомендацию рассмотреть превентивный выход по открытым позициям даже с небольшой потерей (история: удержание через такое событие часто ведёт к большим потерям). Одновременно учитывай прогнозы о деэскалации или стабилизации: если в новостях звучит снижение напряжённости, переговоры, «рынок учёл», адаптация — риски могут быть терпимы, не исключай вход или удержание, когда рынок уже адаптировался, чтобы не оставаться вне игры без необходимости.
6. Кластер, корреляция и геополитика: если есть блок «Кластер и корреляция» (корреляции, цены и тех. сигналы по нескольким тикерам) — обязательно учти его в key_factors и reasoning. Высокая корреляция означает совместные движения; при расхождении сигналов или слабости коррелированного актива — осторожность; не дублируй риск по двум сильно коррелированным бумагам. Если в новостях есть геополитика или sector-wide risk-off — **свяжи** её с трендами цен имен кластера: кто уже отразил стресс, кто отстаёт, куда может «дотянуться» цепочка (полупроводники, память, AI-инфра, энергия и т.д. по контексту тикеров). Не анализируй целевой тикер изолированно от кластера, когда гео-риск системный.
7. Краткосрочный прогноз (30/60/120 мин) и тейк: p10/p50/p90 и P(>spot) — ориентир направления и ширины коридора (упрощённая статистика по недавним 5m, не гарантия). Если в данных есть «Импульс 2ч» и «Эффективный тейк по правилам GAME_5m» — число тейка для исполнения берётся из правил стратегии (и связано с импульсом), а не из таблицы прогноза. p50 на 120 мин математически на той же 2ч-оси, что импульс 2ч (не считай их двумя независимыми сигналами). Для словесных оценок кратких целей уместнее горизонты 30/60 мин (p50 или p90 как верх сценария); p10 — нижний хвост риска. Не подменяй исполняемый тейк одной ячейкой таблицы.

Важно: в ответе должна быть одна чёткая итоговая рекомендация (BUY, STRONG_BUY или HOLD). В reasoning объясняй именно её: если HOLD — почему не входим сейчас (а не «есть возможность покупки, но держим»); если BUY — почему входим. Не смешивай противоположные выводы в одном тексте.

Отвечай в формате JSON:
{
    "decision": "BUY|STRONG_BUY|HOLD",
    "confidence": 0.0-1.0,
    "reasoning": "объяснение решения (один связный абзац)",
    "risks": ["список рисков"],
    "key_factors": ["короткие метки 3–7 слов каждая, не дублируй текст из reasoning"]
}
key_factors — только краткие метки для быстрого скана (например: «RSI перепродан», «слабость сектора», «нейтральный sentiment»), не повторяй в них полные фразы из reasoning.
"""

    @staticmethod
    def get_entry_decision_prompt_template() -> Dict[str, str]:
        """Шаблон промпта для решения о входе: system + user (с плейсхолдерами). Для отображения по /prompt_entry."""
        return {
            "system": LLMService.get_entry_decision_system_prompt(),
            "user_template": """Анализ для тикера {ticker}:

Технические данные:
- Текущая цена: {close}
- SMA_5: {sma_5}
- Волатильность (5 дней): {volatility_5}
- Средняя волатильность (20 дней): {avg_volatility_20}
- RSI: {rsi} ({rsi_status})
- Технический сигнал: {technical_signal}

Sentiment анализ:
- Взвешенный sentiment: {sentiment_score:.3f}
- Количество новостей: {len_news}

Новости:
{news_summary}

(Опционально: Контекст сессии: {premarket_note} — учти при рекомендации входа, в премаркете ликвидность ниже.)
(Опционально: Контекст стратегии: выбранная стратегия — {strategy_name}, её сигнал — {strategy_signal}. Учти при итоговой рекомендации.)

Дай рекомендацию на основе этих данных.""",
        }

    @staticmethod
    def build_entry_prompt(
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float,
        strategy_name: Optional[str] = None,
        strategy_signal: Optional[str] = None,
        strategy_outcome_stats: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Собирает промпт (system + user) для решения о входе без вызова LLM.
        Те же входы, что у analyze_trading_situation. Для /prompt_entry: всегда заполнять user_prompt в JSON.
        """
        system_prompt = LLMService.get_entry_decision_system_prompt()
        from services.geopolitical_prompt import (
            build_geopolitical_block,
            geo_cluster_bridge_hint,
            geopolitical_followup_hint,
        )

        news_summary = "\n".join([
            f"- {news.get('source', 'Unknown')}: {news.get('content', '')[:200]}... (sentiment: {news.get('sentiment_score', 0)})"
            for news in (news_data or [])[:5]
        ])
        geo_block, geo_blob = build_geopolitical_block(news_data or [])
        _geo_nl_block = ("\n\n" + geo_block) if geo_block else ""
        rsi_value = technical_data.get('rsi')
        rsi_text = ""
        if rsi_value is not None:
            if rsi_value >= 70:
                rsi_status = "перекупленность"
            elif rsi_value <= 30:
                rsi_status = "перепроданность"
            elif rsi_value >= 60:
                rsi_status = "близко к перекупленности"
            elif rsi_value <= 40:
                rsi_status = "близко к перепроданности"
            else:
                rsi_status = "нейтральная зона"
            rsi_text = f"\n- RSI: {rsi_value:.1f} ({rsi_status})"
        exec_ctx = format_game5m_execution_context_for_llm(technical_data)
        pf_block = format_price_forecast_llm_block(technical_data)
        user_message = f"""Анализ для тикера {ticker}:

Технические данные:
- Текущая цена: {technical_data.get('close', 'N/A')}
- SMA_5: {technical_data.get('sma_5', 'N/A')}
- Волатильность (5 дней): {technical_data.get('volatility_5', 'N/A')}
- Средняя волатильность (20 дней): {technical_data.get('avg_volatility_20', 'N/A')}{rsi_text}{exec_ctx}
- Технический сигнал: {technical_data.get('technical_signal', 'N/A')}{pf_block}

Sentiment анализ:
- Взвешенный sentiment: {sentiment_score:.3f}
- Количество новостей: {len(news_data or [])}

Новости:
{news_summary if news_summary else "Новостей не найдено"}{_geo_nl_block}
"""
        cluster_note = technical_data.get("cluster_note")
        if not cluster_note:
            corr_val = technical_data.get("corr_with_benchmark")
            corr_label = technical_data.get("corr_label")
            benchmark_signal = technical_data.get("benchmark_signal")
            if corr_val is not None and corr_label:
                user_message += f"\n\nКорреляция с бенчмарком (MU, 14 дн.): {corr_val:.2f} ({corr_label})."
                if benchmark_signal:
                    user_message += f" Текущий сигнал по MU: {benchmark_signal}. При высокой корреляции (In-Sync) движение бенчмарка может поддерживать или давить на тикер — учти при рекомендации."
                else:
                    user_message += " Учти при рекомендации: при In-Sync бумага движется с сектором; при Independent — на собственных драйверах."
        premarket_note = technical_data.get("premarket_note")
        if premarket_note:
            user_message += f"\n\nКонтекст сессии:\n{premarket_note}\n\nУчти это при рекомендации входа (в премаркете ликвидность ниже)."
        if cluster_note:
            user_message += f"\n\nКластер и корреляция (обязательно учти в key_factors и reasoning):\n{cluster_note}"
        if strategy_name and strategy_signal:
            user_message += f"\n\nКонтекст стратегии: выбранная стратегия — {strategy_name}, её сигнал — {strategy_signal}. Дай одну итоговую рекомендацию (согласись или скорректируй). Если итог HOLD — в reasoning объясни, почему не входим, а не «есть возможность покупки, но держим»."
        if strategy_outcome_stats:
            user_message += f"\n\n{strategy_outcome_stats} Учти исходы в похожих ситуациях при рекомендации."
        _geo_combined = ((news_summary or "").lower() + "\n" + geo_blob).strip()
        user_message += geopolitical_followup_hint(_geo_combined)
        user_message += geo_cluster_bridge_hint(cluster_note, _geo_combined)
        user_message += "\n\nДай рекомендацию на основе этих данных."
        return {"prompt_system": system_prompt, "prompt_user": user_message}

    def generate_response_with_model(
        self,
        base_url: str,
        model: str,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        Один запрос к указанному (base_url, model). Для сравнения моделей.
        Возвращает тот же формат что generate_response или None при ошибке.
        """
        if not self.api_key:
            return None
        try:
            client = OpenAI(
                api_key=self.api_key,
                base_url=base_url,
                timeout=kwargs.get("timeout", self.timeout),
            )
            formatted_messages = []
            if system_prompt:
                formatted_messages.append({"role": "system", "content": system_prompt})
            formatted_messages.extend(messages)
            _token_kw = chat_completion_token_limit_params(
                model,
                max_tokens=kwargs.get("max_tokens"),
                max_completion_tokens=kwargs.get("max_completion_tokens"),
                default_limit=2000,
            )
            response = client.chat.completions.create(
                model=model,
                messages=formatted_messages,
                temperature=kwargs.get("temperature", self.temperature),
                **_token_kw,
            )
            msg = response.choices[0].message.content
            return {
                "response": msg,
                "model": getattr(response, "model", model),
                "usage": getattr(response, "usage", None) and {
                    "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                    "total_tokens": getattr(response.usage, "total_tokens", 0),
                } or {},
                "finish_reason": getattr(response.choices[0], "finish_reason", None),
            }
        except Exception as e:
            logger.warning("Сравнение моделей: ошибка для %s: %s", model, e)
            return None

    def analyze_trading_situation(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float,
        strategy_name: Optional[str] = None,
        strategy_signal: Optional[str] = None,
        strategy_outcome_stats: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Анализ торговой ситуации с помощью LLM
        
        Args:
            ticker: Тикер инструмента
            technical_data: Технические данные (цена, SMA, волатильность)
            news_data: Список новостей
            sentiment_score: Взвешенный sentiment score
            strategy_name: Имя выбранной стратегии (Momentum, Mean Reversion и т.д.), если есть
            strategy_signal: Сигнал стратегии (BUY/HOLD/SELL), если есть
            strategy_outcome_stats: Текст со статистикой исходов по стратегиям (закрытые сделки), если есть
            
        Returns:
            Анализ от LLM с рекомендацией
        """
        system_prompt = self.get_entry_decision_system_prompt()
        from services.geopolitical_prompt import (
            build_geopolitical_block,
            geo_cluster_bridge_hint,
            geopolitical_followup_hint,
        )

        # Формируем контекст
        news_summary = "\n".join([
            f"- {news.get('source', 'Unknown')}: {news.get('content', '')[:200]}... (sentiment: {news.get('sentiment_score', 0)})"
            for news in news_data[:5]  # Берем топ-5 новостей
        ])
        geo_block, geo_blob = build_geopolitical_block(news_data or [])
        _geo_nl_block = ("\n\n" + geo_block) if geo_block else ""

        # Форматируем RSI с интерпретацией
        rsi_value = technical_data.get('rsi')
        rsi_text = ""
        if rsi_value is not None:
            if rsi_value >= 70:
                rsi_status = "перекупленность"
            elif rsi_value <= 30:
                rsi_status = "перепроданность"
            elif rsi_value >= 60:
                rsi_status = "близко к перекупленности"
            elif rsi_value <= 40:
                rsi_status = "близко к перепроданности"
            else:
                rsi_status = "нейтральная зона"
            rsi_text = f"\n- RSI: {rsi_value:.1f} ({rsi_status})"

        tech_sig = technical_data.get("technical_signal", "N/A")
        tech_core = technical_data.get("technical_signal_core")
        sig_extra = ""
        if tech_core is not None and str(tech_core) != str(tech_sig):
            sig_extra = f"\n- Базовый сигнал правил (до слоя CatBoost): {tech_core}"
        cb_p = technical_data.get("catboost_entry_proba_good")
        cb_st = technical_data.get("catboost_signal_status")
        cb_fusion = technical_data.get("catboost_fusion_note")
        cb_lines = ""
        if cb_st and cb_st != "disabled":
            cb_lines = f"\n- CatBoost: статус={cb_st}"
            if cb_p is not None:
                cb_lines += f", P(благоприятный исход по истории)≈{cb_p}"
            if cb_fusion:
                cb_lines += f"\n  Слияние с тех. сигналом: {cb_fusion}"

        exec_ctx = format_game5m_execution_context_for_llm(technical_data)
        pf_block = format_price_forecast_llm_block(technical_data)
        user_message = f"""Анализ для тикера {ticker}:

Технические данные:
- Текущая цена: {technical_data.get('close', 'N/A')}
- SMA_5: {technical_data.get('sma_5', 'N/A')}
- Волатильность (5 дней): {technical_data.get('volatility_5', 'N/A')}
- Средняя волатильность (20 дней): {technical_data.get('avg_volatility_20', 'N/A')}{rsi_text}{exec_ctx}
- Итоговый технический сигнал (для стратегии входа): {tech_sig}{sig_extra}{cb_lines}{pf_block}

Sentiment анализ:
- Взвешенный sentiment: {sentiment_score:.3f}
- Количество новостей: {len(news_data)}

Новости:
{news_summary if news_summary else "Новостей не найдено"}{_geo_nl_block}
"""
        cluster_note = technical_data.get("cluster_note")
        if not cluster_note:
            corr_val = technical_data.get("corr_with_benchmark")
            corr_label = technical_data.get("corr_label")
            benchmark_signal = technical_data.get("benchmark_signal")
            if corr_val is not None and corr_label:
                user_message += f"\n\nКорреляция с бенчмарком (MU, 14 дн.): {corr_val:.2f} ({corr_label})."
                if benchmark_signal:
                    user_message += f" Текущий сигнал по MU: {benchmark_signal}. При высокой корреляции (In-Sync) движение бенчмарка может поддерживать или давить на тикер — учти при рекомендации."
                else:
                    user_message += " Учти при рекомендации: при In-Sync бумага движется с сектором; при Independent — на собственных драйверах."
        premarket_note = technical_data.get("premarket_note")
        if premarket_note:
            user_message += f"\n\nКонтекст сессии:\n{premarket_note}\n\nУчти это при рекомендации входа (в премаркете ликвидность ниже)."
        if cluster_note:
            user_message += f"\n\nКластер и корреляция (обязательно учти в key_factors и reasoning):\n{cluster_note}"
        if strategy_name and strategy_signal:
            user_message += f"\n\nКонтекст стратегии: выбранная стратегия — {strategy_name}, её сигнал — {strategy_signal}. Дай одну итоговую рекомендацию (согласись или скорректируй). Если итог HOLD — в reasoning объясни, почему не входим, а не «есть возможность покупки, но держим»."
        if strategy_outcome_stats:
            user_message += f"\n\n{strategy_outcome_stats} Учти исходы в похожих ситуациях при рекомендации."
        _geo_combined = ((news_summary or "").lower() + "\n" + geo_blob).strip()
        user_message += geopolitical_followup_hint(_geo_combined)
        user_message += geo_cluster_bridge_hint(cluster_note, _geo_combined)
        user_message += "\n\nДай рекомендацию на основе этих данных."
        
        messages = [{"role": "user", "content": user_message}]
        
        try:
            result = self.generate_response(messages, system_prompt=system_prompt)
            response_text = result["response"]
            
            # Пытаемся распарсить JSON из ответа
            try:
                # Ищем JSON в ответе
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    analysis = json.loads(json_match.group())
                else:
                    # Если JSON не найден, создаем структуру из текста
                    analysis = {
                        "decision": "HOLD",
                        "confidence": 0.5,
                        "reasoning": response_text,
                        "risks": [],
                        "key_factors": []
                    }
            except json.JSONDecodeError:
                # Если не удалось распарсить, используем текст как reasoning
                analysis = {
                    "decision": "HOLD",
                    "confidence": 0.5,
                    "reasoning": response_text,
                    "risks": [],
                    "key_factors": []
                }
            # Отслеживание: учитывает ли LLM контекст кластера/корреляции (для проверки промпта)
            if technical_data.get("cluster_note"):
                reasoning_text = (analysis.get("reasoning") or "") + " " + " ".join(analysis.get("key_factors") or [])
                reasoning_lower = reasoning_text.lower()
                mentions_correlation = any(
                    k in reasoning_lower for k in (
                        "коррел", "кластер", "correlation", "cluster",
                        "синхрон", "in-sync", "дублир", "согласован", "другим тикер",
                    )
                )
                logger.info(
                    "LLM entry (ticker=%s): cluster_note was in prompt; reasoning/key_factors mention correlation: %s; reasoning: %.200s",
                    ticker, mentions_correlation, (analysis.get("reasoning") or "")[:200],
                )
            return {
                "llm_analysis": analysis,
                "usage": result.get("usage", {}),
                "prompt_system": system_prompt,
                "prompt_user": user_message,
                "llm_response_raw": response_text,
            }
        except Exception as e:
            logger.error(f"Ошибка анализа через LLM: {e}")
            err_str = str(e).lower()
            if "401" in err_str or "invalid api key" in err_str or "invalid_api_key" in err_str or "authentication" in err_str:
                reasoning = "Ошибка анализа: неверный или отсутствующий API-ключ. Проверьте OPENAI_API_KEY (или прокси) в Параметрах → config.env."
            else:
                reasoning = f"Ошибка анализа: {str(e)}"
            return {
                "llm_analysis": {
                    "decision": "HOLD",
                    "confidence": 0.0,
                    "reasoning": reasoning,
                    "risks": ["Ошибка LLM анализа"],
                    "key_factors": []
                },
                "usage": {},
                "prompt_system": system_prompt,
                "prompt_user": user_message,
                "llm_response_raw": "",
            }

    def fetch_news_for_ticker(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Прямой запрос к LLM: какие новости/события могут влиять на тикер (например SNDK).
        Используется как один из источников новостей наряду с RSS, NewsAPI, KB.

        Важно: LLM не выполняет поиск в интернете в реальном времени — только знание из обучения.
        Свежие breaking news (например шорт от Citron в день публикации) могут не попасть в ответ.
        Чтобы агент реагировал на такие новости, они должны быть уже в knowledge_base:
        cron fetch_news_cron (Investing.com News, NewsAPI, Alpha Vantage и т.д.) и/или
        разовая вставка через scripts/add_manual_news.py.

        Returns:
            dict с ключами content, sentiment_score (0–1 или None), insight, source_label
            или None при ошибке / отсутствии API key.
        """
        if not self.client:
            logger.warning("LLM не инициализирован, пропуск fetch_news_for_ticker")
            return None

        ticker_upper = ticker.upper()
        name_hint = {"SNDK": "Western Digital / SanDisk, память, полупроводники"}.get(
            ticker_upper, ticker_upper
        )

        system_prompt = """Ты финансовый обзор. Задача: кратко перечисли последние новости и события, которые могут влиять на цену указанной бумаги в ближайшие дни.
Формат ответа:
1. Краткий список фактов (дата/источник по возможности, 2–5 пунктов).
2. Строка "SENTIMENT: положительный|негативный|нейтральный" — общая тональность для цены.
3. Строка "INSIGHT: один короткий вывод в одну фразу."
Пиши по-русски, без лишнего вступления."""

        user_message = f"""Какие последние новости и события могут влиять на {ticker_upper} ({name_hint})? Укажи только релевантные факты за последние 1–2 недели."""

        def _parse_llm_news_text(text: str) -> tuple:
            sentiment_score = None
            insight = None
            for line in (text or "").split("\n"):
                line = line.strip()
                if line.upper().startswith("SENTIMENT:"):
                    part = line.split(":", 1)[-1].strip().lower()
                    if "положительн" in part or "позитив" in part:
                        sentiment_score = 0.65
                    elif "негатив" in part or "негативн" in part:
                        sentiment_score = 0.35
                    else:
                        sentiment_score = 0.5
                elif line.upper().startswith("INSIGHT:"):
                    insight = line.split(":", 1)[-1].strip()[:500]
            return sentiment_score, insight

        try:
            result = self.generate_response(
                [{"role": "user", "content": user_message}],
                system_prompt=system_prompt,
                max_tokens=800,
            )
            text = result.get("response", "").strip()
            if not text:
                return None

            sentiment_score, insight = _parse_llm_news_text(text)
            content = text
            source_label = f"LLM ({self.model})"
            out = {
                "content": content[:8000],
                "sentiment_score": sentiment_score,
                "insight": insight,
                "source_label": source_label,
            }

            # Сравнение с другими моделями (LLM_COMPARE_MODELS)
            config = load_config()
            compare_list = parse_compare_models(config)
            if compare_list:
                llm_comparison = []
                for base_url, model in compare_list:
                    if base_url == self.base_url and model == self.model:
                        llm_comparison.append({
                            "model": model,
                            "content": content[:2000],
                            "sentiment_score": sentiment_score,
                            "insight": insight,
                            "source": "primary",
                        })
                        continue
                    res = self.generate_response_with_model(
                        base_url, model,
                        [{"role": "user", "content": user_message}],
                        system_prompt=system_prompt,
                        max_tokens=800,
                    )
                    if not res:
                        llm_comparison.append({"model": model, "error": "no response"})
                        continue
                    text_other = (res.get("response") or "").strip()
                    sent_other, insight_other = _parse_llm_news_text(text_other)
                    llm_comparison.append({
                        "model": model,
                        "content": text_other[:2000],
                        "sentiment_score": sent_other,
                        "insight": insight_other,
                    })
                out["llm_comparison"] = llm_comparison

            return out
        except Exception as e:
            logger.exception("Ошибка fetch_news_for_ticker %s: %s", ticker, e)
            return None

    def generate_news_by_topic(
        self, topic: str, tickers: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Генерация группы новостей по заданной теме для ручного ввода в базу знаний.
        LLM возвращает JSON-массив записей с полями ticker, content, sentiment_score, insight, source.
        tickers: список тикеров, по которым нужно сформировать записи (например GC=F, MSFT, AMD, MU, SNDK).
        """
        if not self.client:
            logger.warning("LLM не инициализирован, пропуск generate_news_by_topic")
            return []

        default_tickers = ["GC=F", "MSFT", "AMD", "MU", "SNDK"]
        ticker_list = tickers or default_tickers
        ticker_list_upper = [t.strip().upper() for t in ticker_list if t]
        if not ticker_list_upper:
            ticker_list_upper = [t.upper() for t in default_tickers]
        tickers_str = ", ".join(ticker_list)

        system_prompt = """Ты финансовый обзор. Задача: по заданной теме сформировать группу коротких новостных записей для базы знаний (по одной или несколько на тикер).
Отвечай строго в формате JSON — один массив объектов. Каждый объект:
{
  "ticker": "один из указанных тикеров",
  "content": "2-4 предложения: суть события/ожидания для рынка в связи с темой, релевантно тикеру",
  "sentiment_score": число от 0.0 до 1.0 (0.5 нейтрально, выше — позитив для цены, ниже — негатив),
  "insight": "одна короткая фраза-вывод для трейдера",
  "source": "краткое имя источника, например LLM (тема)"
}
Тикеры для использования: """ + tickers_str + """. Распредели тему по тикерам уместно (золото — GC=F, тех/полупроводники — MSFT, AMD, MU, SNDK). От 3 до 8 записей. Только JSON, без markdown и пояснений до/после."""

        user_message = f"""Тема: {topic}\n\nСформируй группу новостных записей в формате JSON (массив объектов с полями ticker, content, sentiment_score, insight, source)."""

        try:
            result = self.generate_response(
                [{"role": "user", "content": user_message}],
                system_prompt=system_prompt,
                max_tokens=2000,
            )
            text = (result.get("response") or "").strip()
            if not text:
                return []
            # Убрать markdown-обёртку если есть
            if "```json" in text:
                text = text.split("```json")[-1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            data = json.loads(text)
            if not isinstance(data, list):
                data = [data]
            out = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                ticker = (item.get("ticker") or "").strip().upper()
                if not ticker or ticker not in ticker_list_upper:
                    ticker = ticker_list_upper[0]
                content = (item.get("content") or "")[:8000]
                if not content:
                    continue
                try:
                    sent = float(item.get("sentiment_score", 0.5))
                    sent = max(0.0, min(1.0, sent))
                except (TypeError, ValueError):
                    sent = 0.5
                insight = (item.get("insight") or "")[:1000]
                source = (item.get("source") or f"LLM ({self.model})")[:200]
                out.append({
                    "ticker": ticker,
                    "content": content,
                    "sentiment_score": round(sent, 2),
                    "insight": insight,
                    "source": source,
                    "link": (item.get("link") or "")[:500],
                })
            return out
        except json.JSONDecodeError as e:
            logger.warning("generate_news_by_topic: не удалось распарсить JSON: %s", e)
            return []
        except Exception as e:
            logger.exception("Ошибка generate_news_by_topic: %s", e)
            return []


# Глобальный экземпляр сервиса
_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    """
    Получить глобальный экземпляр LLM сервиса
    """
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service

