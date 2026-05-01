"""
Веб-интерфейс для LSE Trading System
FastAPI: портфель, отчёты 5m, база знаний, графики, параметры, сервис.
"""

import asyncio
import functools
import io
import logging
import math
import os
import json
import re
import shutil
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional, Dict, Any, Tuple

try:
    from zoneinfo import ZoneInfo
    DISPLAY_TZ = ZoneInfo("America/New_York")
except ImportError:
    DISPLAY_TZ = None  # fallback: показываем как есть, без конвертации

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
import pandas as pd
from sqlalchemy import create_engine, text

import numpy as np

from analyst_agent import AnalystAgent
from config_loader import (
    get_database_url,
    get_use_llm_for_analyst,
    get_closed_positions_report_limits,
    get_web_closed_positions_limits,
    load_config,
    get_config_file_path,
    update_config_key,
    get_editable_config_keys_expanded,
    is_editable_config_env_key,
)
from execution_agent import ExecutionAgent
from services.game5m_tuning_policy import apply_game5m_update, current_config_value, validate_game5m_update
from services.ticker_groups import get_tickers_fast
from news_importer import add_news, get_news_sources_stats
from report_generator import (
    compute_closed_trade_pnls,
    compute_open_positions,
    load_trade_history,
    get_engine,
    get_latest_prices,
    human_trade_explanation_from_exit_context,
)

app = FastAPI(title="LSE Trading System", version="1.0.0")
logger = logging.getLogger(__name__)

GAME5M_TUNING_REGLEMENT = {
    "rules": [
        "Один live-эксперимент за раз.",
        "Proposal из replay - гипотеза, не команда применять весь top-list.",
        "Минимальное окно наблюдения: 1 полный торговый день, лучше 2-3 дня или 8-20 новых закрытых сделок.",
        "Пока эксперимент pending_effect, не меняем другие GAME_5M параметры входа/выхода.",
        "Оставляем изменение только если log-return/качество выходов лучше baseline без роста риска.",
        "Откатываем old_value из ledger, если появились ранние тейки, выросли stop/stale/no-exit или результат хуже baseline.",
    ],
    "default_live_test": {
        "env_key": "GAME_5M_TAKE_PROFIT_MIN_PCT",
        "value": "2.5",
        "note": "Мягкий шаг между текущим 3.0 и replay direction 2.0.",
    },
}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Любая необработанная ошибка возвращает JSON с текстом (чтобы не показывать Internal Server Error без деталей)."""
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc!s}"},
    )


@app.middleware("http")
async def catch_all_errors(request: Request, call_next):
    """При любой ошибке в обработке запроса возвращаем JSON 500 с текстом (не HTML)."""
    try:
        return await call_next(request)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(e).__name__}: {e!s}"},
        )


def _default_ticker_5m() -> str:
    """Тикер по умолчанию для 5m API: первый из TICKERS_FAST или SNDK как fallback."""
    fast = get_tickers_fast()
    return (fast[0] if fast else "SNDK")


def _to_jsonable(obj: Any) -> Any:
    """Приводит numpy/pandas типы к нативным Python для JSON-сериализации."""
    if obj is None:
        return None
    if isinstance(obj, Decimal):
        try:
            v = float(obj)
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            return v
        except (ValueError, TypeError, OverflowError):
            return None
    # numpy/pandas скаляры (bool_, int64, float64 и т.д.) имеют .item()
    if hasattr(obj, "item") and callable(getattr(obj, "item")):
        try:
            v = obj.item()
            if isinstance(v, float) and (v != v or abs(v) == np.inf):
                return None
            return v
        except (ValueError, TypeError):
            pass
    if isinstance(obj, (bool,)):
        return bool(obj)
    if isinstance(obj, (int, float)):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        return obj
    if isinstance(obj, np.ndarray):
        return [_to_jsonable(x) for x in obj.tolist()]
    if hasattr(obj, "isoformat") and callable(getattr(obj, "isoformat")):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    return obj


def _subprocess_env_with_standard_paths() -> Dict[str, str]:
    """
    PATH для subprocess: у uvicorn/systemd часто «урезанный» PATH → `docker` не находится (код 127).
    """
    e = dict(os.environ)
    extra = ("/usr/local/bin", "/usr/bin", "/usr/sbin", "/bin", "/snap/bin")
    cur = e.get("PATH", "")
    for p in extra:
        if p and p not in cur.split(":"):
            cur = f"{cur}:{p}" if cur else p
    e["PATH"] = cur.strip(":")
    return e


def _run_restart_shell(cmd: str, cwd: Path) -> Any:
    """Запуск RESTART_CMD / docker compose с расширенным PATH."""
    import subprocess

    return subprocess.run(
        cmd,
        shell=True,
        executable="/bin/bash",
        timeout=30,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=_subprocess_env_with_standard_paths(),
    )


def _restart_shell_candidates(config: Dict[str, str]) -> List[str]:
    """
    Команды перезапуска: явный RESTART_CMD или цепочка fallback при пустом значении.
    Код 127 (command not found) часто лечится полным путём к docker / docker-compose.
    """
    raw = (config.get("RESTART_CMD") or "").strip()
    if raw:
        return [raw]
    env = _subprocess_env_with_standard_paths()
    path = env.get("PATH", "")
    candidates: List[str] = []
    seen: set[str] = set()

    def add(c: str) -> None:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            candidates.append(c)

    add("docker compose restart lse")
    for dock in ("/usr/bin/docker", "/usr/local/bin/docker", "/snap/bin/docker"):
        if os.path.isfile(dock) and os.access(dock, os.X_OK):
            add(f"{dock} compose restart lse")
    wd = shutil.which("docker", path=path)
    if wd:
        add(f"{wd} compose restart lse")
    wdc = shutil.which("docker-compose", path=path)
    if wdc:
        add(f"{wdc} restart lse")
    return candidates


def _run_restart_resolved(config: Dict[str, str]) -> Tuple[Any, str]:
    """Выполняет перезапуск; при пустом RESTART_CMD перебирает варианты, пока не исчезнет 127."""
    cwd = Path(__file__).parent
    cmds = _restart_shell_candidates(config)
    last: Any = None
    last_cmd = cmds[0] if cmds else ""
    for c in cmds:
        last_cmd = c
        last = _run_restart_shell(c, cwd)
        if last.returncode != 127:
            return last, c
    return last, last_cmd


def _restart_result_from_completed(result: Any, cmd: str) -> Dict[str, Any]:
    """Унифицированный ответ API для перезапуска."""
    if result.returncode == 0:
        return {"ok": True, "message": "Перезапуск выполнен"}
    err_tail = ((result.stderr or "") + (result.stdout or ""))[:500]
    hint_127 = ""
    if result.returncode == 127:
        hint_127 = (
            " Часто причина: нет `docker`/`docker-compose` в PATH или веб крутится в контейнере без Docker CLI. "
            "В config.env задайте RESTART_CMD с полным путём, например: "
            "`RESTART_CMD=/usr/bin/docker compose -f /path/to/docker-compose.yml restart lse` "
            "(или `sudo -n /bin/systemctl restart ваш-сервис`, если без Docker)."
        )
    return {
        "ok": False,
        "message": f"Команда вернула код {result.returncode}.{hint_127} Выполните на сервере вручную при необходимости.",
        "stderr": err_tail,
        "command": cmd[:200],
    }


def _schedule_self_restart(*, delay_sec: float = 1.0) -> None:
    """
    Fallback рестарта, когда веб крутится внутри контейнера без Docker CLI.
    Контейнер в docker-compose.yml имеет restart: unless-stopped, поэтому выход процесса
    приведёт к автоматическому перезапуску контейнера и перечитыванию config.env.
    """
    import threading
    import os

    def _exit() -> None:
        os._exit(0)

    threading.Timer(max(0.0, float(delay_sec)), _exit).start()


# Настройка шаблонов
templates_dir = Path(__file__).parent / "templates"
templates_dir.mkdir(exist_ok=True)
jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)))

def render_template(template_name: str, context: dict):
    """Рендеринг шаблона"""
    template = jinja_env.get_template(template_name)
    return template.render(**context)

# Статические файлы
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Инициализация агентов
db_url = get_database_url()
engine = create_engine(db_url)


def _now_et() -> datetime:
    """Текущее время в Eastern Time для отображения в интерфейсе."""
    if DISPLAY_TZ is not None:
        return datetime.now(DISPLAY_TZ)
    return datetime.now()


def _format_ts(ts) -> str:
    """Форматирование времени для отображения в шаблонах. Все даты в интерфейсе показываются в ET."""
    if ts is None:
        return "—"
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%d %H:%M") + " ET"
    s = str(ts)
    return (s + " ET") if s and s != "—" else s


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Главная: кэш portfolio_state + ссылки на портфельную игру (карточки/графики) и отчёты по сделкам."""
    with engine.connect() as conn:
        cash_result = conn.execute(
            text("SELECT quantity FROM portfolio_state WHERE ticker = 'CASH'")
        ).fetchone()
        cash = float(cash_result[0]) if cash_result else 0.0

    return HTMLResponse(render_template("index.html", {"request": request, "cash": cash}))


@app.get("/api/price/{ticker}", response_class=JSONResponse)
async def get_price(ticker: str):
    """API: Текущая цена тикера (аналог /price в Telegram)"""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT date, close FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
            {"ticker": ticker}
        ).fetchone()
    if not row or row[1] is None:
        raise HTTPException(status_code=404, detail=f"Котировки для {ticker} не найдены")
    return {"ticker": ticker, "price": float(row[1]), "date": _format_ts(row[0])}


def _get_recommendation_data(ticker: str) -> Optional[Dict[str, Any]]:
    """Собирает данные для рекомендации (тот же контракт, что и в Telegram). USE_LLM из config/БД."""
    try:
        agent = AnalystAgent(use_llm=get_use_llm_for_analyst(engine=engine))
        result = agent.get_decision_with_llm(ticker)
        if result.get("decision") == "NO_DATA":
            return None
        decision = result.get("decision", "HOLD")
        strategy = result.get("selected_strategy") or "—"
        technical = result.get("technical_data") or {}
        sentiment = result.get("sentiment_normalized") or result.get("sentiment") or 0.0
        if isinstance(sentiment, (int, float)) and 0 <= sentiment <= 1:
            sentiment = (sentiment - 0.5) * 2.0
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT close, rsi FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
                {"ticker": ticker},
            ).fetchone()
        price = float(row[0]) if row and row[0] is not None else None
        rsi = float(row[1]) if row and row[1] is not None else technical.get("rsi")
        try:
            from utils.risk_manager import get_risk_manager
            rm = get_risk_manager()
            stop_loss_pct = rm.get_stop_loss_percent()
            take_profit_pct = rm.get_take_profit_percent()
            max_pos_usd = rm.get_max_position_size(ticker)
            max_ticker_pct = rm.get_max_single_ticker_exposure()
        except Exception:
            stop_loss_pct = 5.0
            take_profit_pct = 10.0
            max_pos_usd = 10000.0
            max_ticker_pct = 20.0
        has_position = False
        position_info = None
        try:
            ex = ExecutionAgent()
            summary = ex.get_portfolio_summary()
            for p in summary.get("positions") or []:
                if p["ticker"] == ticker:
                    has_position = True
                    position_info = p
                    break
        except Exception:
            pass
        reasoning = (result.get("strategy_result") or {}).get("reasoning") or result.get("reasoning") or ""
        return {
            "ticker": ticker,
            "decision": decision,
            "strategy": strategy,
            "price": price,
            "rsi": rsi,
            "sentiment": sentiment,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "max_position_usd": max_pos_usd,
            "max_ticker_pct": max_ticker_pct,
            "has_position": has_position,
            "position": position_info,
            "reasoning": reasoning,
        }
    except Exception:
        return None


@app.get("/api/recommend/{ticker}", response_class=JSONResponse)
async def get_recommend(ticker: str):
    """API: Рекомендация по тикеру (аналог /recommend в Telegram)"""
    data = _get_recommendation_data(ticker)
    if not data:
        raise HTTPException(status_code=404, detail="Не удалось получить рекомендацию для тикера")
    return _to_jsonable(data)


@app.get("/api/recommend5m", response_class=JSONResponse)
async def get_recommend5m(ticker: str = None, days: int = 5):
    """API: Рекомендация по 5m данным (аналог /recommend5m в Telegram)"""
    if ticker is None or not ticker.strip():
        ticker = _default_ticker_5m()
    else:
        ticker = ticker.strip().upper()
    try:
        from services.recommend_5m import get_decision_5m, get_5m_card_payload
    except ImportError:
        raise HTTPException(status_code=501, detail="Модуль recommend_5m недоступен")
    try:
        data_5m = get_decision_5m(ticker, days=min(max(1, days), 7), use_llm_news=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка расчёта 5m: {e!s}")
    if not data_5m:
        raise HTTPException(status_code=404, detail="Нет 5m данных для тикера за указанный период")
    has_position = False
    position_info = None
    try:
        ex = ExecutionAgent()
        summary = ex.get_portfolio_summary()
        for p in summary.get("positions") or []:
            if p["ticker"] == ticker:
                has_position = True
                position_info = p
                break
    except Exception:
        pass
    alex_rule = None
    if ticker.upper() == "SNDK":
        try:
            from services.alex_rule import get_alex_rule_status
            alex_rule = get_alex_rule_status(ticker, data_5m.get("price"))
        except Exception:
            pass
    try:
        card = get_5m_card_payload(data_5m, ticker)
        out = {
            **card,
            "ticker": ticker,
            "decision": card.get("decision") or data_5m["decision"],
            "strategy": "5m (интрадей + 5д статистика)",
            "price": card.get("price", data_5m["price"]),
            "rsi_5m": card.get("rsi_5m", data_5m.get("rsi_5m")),
            "reasoning": card.get("reasoning", data_5m.get("reasoning", "")),
            "period_str": card.get("period_str", data_5m.get("period_str", "")),
            "momentum_2h_pct": card.get("momentum_2h_pct", data_5m.get("momentum_2h_pct")),
            "volatility_5m_pct": card.get("volatility_5m_pct", data_5m.get("volatility_5m_pct")),
            "stop_loss_pct": card.get("stop_loss_pct", data_5m.get("stop_loss_pct", 2.5)),
            "take_profit_pct": card.get("take_profit_pct", data_5m.get("take_profit_pct", 5.0)),
            "bars_count": card.get("bars_count", data_5m.get("bars_count")),
            "has_position": has_position,
            "position": position_info,
            "alex_rule": alex_rule,
            "llm_insight": data_5m.get("llm_insight"),
            "llm_news_content": data_5m.get("llm_news_content"),
            "curvature_5m_pct": data_5m.get("curvature_5m_pct"),
            "possible_bounce_to_high_pct": data_5m.get("possible_bounce_to_high_pct"),
            "estimated_bounce_pct": data_5m.get("estimated_bounce_pct"),
            "session_high": data_5m.get("session_high"),
            "entry_advice": data_5m.get("entry_advice"),
            "entry_advice_reason": data_5m.get("entry_advice_reason"),
            "estimated_upside_pct_day": data_5m.get("estimated_upside_pct_day"),
            "suggested_take_profit_price": data_5m.get("suggested_take_profit_price"),
            "premarket_entry_recommendation": data_5m.get("premarket_entry_recommendation"),
            "premarket_suggested_limit_price": data_5m.get("premarket_suggested_limit_price"),
            "premarket_last": data_5m.get("premarket_last"),
            "premarket_gap_pct": data_5m.get("premarket_gap_pct"),
            "minutes_until_open": data_5m.get("minutes_until_open"),
        }
        return _to_jsonable(out)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка сериализации ответа: {e!s}")


@app.post("/api/buy", response_class=JSONResponse)
async def api_buy(ticker: str = Form(...), quantity: float = Form(...)):
    """API: Ручная покупка (аналог /buy в Telegram)"""
    try:
        ex = ExecutionAgent()
        ok, msg = ex.execute_manual_buy(ticker.strip().upper(), quantity)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"status": "success", "message": msg}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sell", response_class=JSONResponse)
async def api_sell(ticker: str = Form(...), quantity: Optional[float] = Form(None)):
    """API: Ручная продажа (аналог /sell в Telegram). quantity пусто = закрыть всю позицию."""
    try:
        ex = ExecutionAgent()
        qty = quantity  # None = close all
        ok, msg = ex.execute_manual_sell(ticker.strip().upper(), qty)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"status": "success", "message": msg}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/portfolio", response_class=JSONResponse)
async def get_portfolio():
    """API: Получить состояние портфеля"""
    with engine.connect() as conn:
        cash_result = conn.execute(
            text("SELECT quantity FROM portfolio_state WHERE ticker = 'CASH'")
        ).fetchone()
        cash = float(cash_result[0]) if cash_result else 0.0
        
        positions_df = pd.read_sql(
            text("""
                SELECT ticker, quantity, avg_entry_price, last_updated
                FROM portfolio_state
                WHERE ticker != 'CASH' AND quantity > 0
            """),
            conn
        )
        
        # Получаем текущие цены
        positions = []
        for _, row in positions_df.iterrows():
            ticker = row['ticker']
            price_result = conn.execute(
                text("""
                    SELECT close FROM quotes
                    WHERE ticker = :ticker
                    ORDER BY date DESC LIMIT 1
                """),
                {"ticker": ticker}
            ).fetchone()
            
            current_price = float(price_result[0]) if price_result else 0.0
            quantity = float(row['quantity'])
            entry_price = float(row['avg_entry_price'])
            current_value = quantity * current_price
            cost_basis = quantity * entry_price
            pnl = current_value - cost_basis
            pnl_pct = ((current_price / entry_price) - 1) * 100 if entry_price > 0 else 0.0
            
            positions.append({
                "ticker": ticker,
                "quantity": quantity,
                "entry_price": entry_price,
                "current_price": current_price,
                "current_value": current_value,
                "cost_basis": cost_basis,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "last_updated": row['last_updated'].isoformat() if row['last_updated'] else None
            })
        
        total_value = sum(p['current_value'] for p in positions)
        total_pnl = sum(p['pnl'] for p in positions)
        
        return {
            "cash": cash,
            "positions": positions,
            "total_value": total_value,
            "total_portfolio_value": cash + total_value,
            "total_pnl": total_pnl
        }


@app.get("/api/quotes/{ticker}", response_class=JSONResponse)
async def get_quotes(ticker: str, days: int = 30):
    """API: Получить котировки для тикера"""
    cutoff_date = datetime.now() - timedelta(days=days)
    
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT date, ticker, close, volume, sma_5, volatility_5
                FROM quotes
                WHERE ticker = :ticker AND date >= :cutoff_date
                ORDER BY date ASC
            """),
            conn,
            params={"ticker": ticker, "cutoff_date": cutoff_date}
        )
    
    if df.empty:
        raise HTTPException(status_code=404, detail=f"Котировки для {ticker} не найдены")
    
    return {
        "ticker": ticker,
        "data": df.to_dict('records')
    }


@app.get("/api/dashboard", response_class=JSONResponse)
async def get_dashboard_api(mode: str = "all"):
    """API: Текст дашборда по тикерам (как /dashboard в Telegram). Сбор в executor — долгий."""
    if mode not in ("all", "5m", "daily"):
        mode = "all"
    try:
        from services.dashboard_builder import build_dashboard_text
    except ImportError:
        raise HTTPException(status_code=501, detail="Модуль dashboard_builder недоступен")
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, lambda: build_dashboard_text(mode))
    return {"text": text, "updated_at": _now_et().isoformat(), "timezone": "America/New_York"}


@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request):
    """Страница мониторинга: дашборд по тикерам (как /dashboard в Telegram), автообновление раз в 5 мин."""
    return HTMLResponse(render_template("monitor.html", {"request": request}))


@app.get("/analyzer", response_class=HTMLResponse)
async def analyzer_page(request: Request):
    """Страница анализатора эффективности сделок."""
    return HTMLResponse(render_template("analyzer.html", {"request": request}))


@app.get("/api/analyzer", response_class=JSONResponse)
async def get_analyzer(
    days: int = 7,
    strategy: str = "GAME_5M",
    use_llm: bool = False,
    include_trade_details: bool = False,
):
    """API: анализ эффективности закрытых сделок (единый код с /analyser в Telegram)."""
    try:
        from services.trade_effectiveness_analyzer import analyze_trade_effectiveness
        payload = analyze_trade_effectiveness(
            days=min(max(1, int(days)), 30),
            strategy=(strategy or "GAME_5M").strip().upper(),
            use_llm=bool(use_llm),
            include_trade_details=bool(include_trade_details),
        )
        return _to_jsonable(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка анализатора: {e!s}")


def _parse_csv_str(s: str) -> List[str]:
    s = (s or "").strip()
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _parse_csv_ints(s: str) -> List[int]:
    out: List[int] = []
    for x in _parse_csv_str(s):
        try:
            out.append(int(x))
        except ValueError:
            continue
    return out


@app.get("/api/analyzer/focused", response_class=JSONResponse)
async def get_analyzer_focused(
    days: int = 4,
    strategy: str = "GAME_5M",
    use_llm: bool = False,
    include_trade_details: bool = False,
    tickers: str = "",
    trade_ids: str = "",
):
    """Узкий анализ: окно в днях + опционально тикеры и/или trade_id (как query-параметры)."""
    try:
        from services.trade_effectiveness_analyzer import analyze_trade_effectiveness_focused

        t_list = _parse_csv_str(tickers)
        id_list = _parse_csv_ints(trade_ids)
        payload = analyze_trade_effectiveness_focused(
            days=min(max(1, int(days)), 30),
            strategy=(strategy or "GAME_5M").strip().upper(),
            tickers=t_list if t_list else None,
            trade_ids=id_list if id_list else None,
            use_llm=bool(use_llm),
            include_trade_details=bool(include_trade_details),
        )
        return _to_jsonable(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка узкого анализатора: {e!s}")


@app.post("/api/analyzer/apply-config", response_class=JSONResponse)
async def apply_analyzer_config(request: Request):
    """
    Применяет предложенный анализатором блок параметров в config.env.
    Формат JSON:
      {
        "updates": [{"env_key":"GAME_5M_VOLATILITY_WAIT_MIN","proposed":"0.7"}, ...],
        "restart": true
      }
    """
    import subprocess

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Ожидается JSON body")
    updates = body.get("updates") if isinstance(body, dict) else None
    do_restart = bool(body.get("restart")) if isinstance(body, dict) else False
    if not isinstance(updates, list) or not updates:
        raise HTTPException(status_code=400, detail="updates is required")

    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for row in updates:
        if not isinstance(row, dict):
            continue
        key = (row.get("env_key") or "").strip()
        proposed = str(row.get("proposed") if row.get("proposed") is not None else "").strip()
        if not key:
            continue
        validation = validate_game5m_update(key, proposed)
        if not validation.ok:
            skipped.append({"env_key": key, "reason": validation.reason})
            continue
        ok, record = apply_game5m_update(key, proposed, source="web_api_analyzer_apply_config")
        if not ok:
            skipped.append({"env_key": key, "reason": record.get("status") or "write_failed"})
            continue
        applied.append({"env_key": key, "proposed": proposed})

    restart_result: Dict[str, Any] = {"ok": False, "message": "Перезапуск не запрошен"}
    if do_restart and applied:
        config = load_config()
        try:
            result, used_cmd = _run_restart_resolved(config)
            if result.returncode == 0:
                restart_result = {
                    "ok": True,
                    "message": "Перезапуск выполнен",
                    "command": used_cmd[:200],
                }
            else:
                restart_result = _restart_result_from_completed(result, used_cmd)
        except FileNotFoundError:
            restart_result = {"ok": False, "message": "Выполните на сервере: docker compose restart lse"}
        except subprocess.TimeoutExpired:
            restart_result = {"ok": False, "message": "Таймаут. Проверьте на сервере: docker ps"}
        except Exception as e:
            restart_result = {"ok": False, "message": f"Ошибка: {e!s}. Выполните на сервере: docker compose restart lse"}

    return _to_jsonable(
        {
            "ok": len(applied) > 0,
            "applied_count": len(applied),
            "skipped_count": len(skipped),
            "applied": applied,
            "skipped": skipped,
            "restart": restart_result,
        }
    )


def _game5m_tuning_ledger_path() -> Path:
    raw = (os.environ.get("GAME5M_TUNING_LEDGER") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else Path(__file__).resolve().parent / p
    return Path(__file__).resolve().parent / "local" / "game5m_tuning_ledger.json"


def _load_game5m_tuning_ledger() -> Dict[str, Any]:
    p = _game5m_tuning_ledger_path()
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("Не удалось прочитать GAME_5M tuning ledger", exc_info=True)
        return {}


def _save_game5m_tuning_ledger(ledger: Dict[str, Any]) -> None:
    p = _game5m_tuning_ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    ledger["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps(_to_jsonable(ledger), ensure_ascii=False, indent=2), encoding="utf-8")


def _game5m_closed_summary(days: int) -> Dict[str, Any]:
    closed = compute_closed_trade_pnls(load_trade_history(get_engine(), strategy_name="GAME_5M"))
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=max(1, int(days)))
    rows = []
    for t in closed:
        ts = pd.Timestamp(getattr(t, "ts", None))
        if ts.tzinfo is None:
            ts = ts.tz_localize("Europe/Moscow", ambiguous=True).tz_convert("UTC")
        else:
            ts = ts.tz_convert("UTC")
        if ts >= cutoff:
            rows.append(t)
    wins = sum(1 for t in rows if float(getattr(t, "log_return", 0.0) or 0.0) > 0)
    total_lr = sum(float(getattr(t, "log_return", 0.0) or 0.0) for t in rows)
    total_pnl = sum(float(getattr(t, "net_pnl", 0.0) or 0.0) for t in rows)
    return {
        "days": int(days),
        "closed_trades": len(rows),
        "wins": wins,
        "losses": max(0, len(rows) - wins),
        "win_rate_pct": round((wins / len(rows) * 100.0), 2) if rows else None,
        "total_log_return": round(total_lr, 6),
        "avg_log_return": round(total_lr / len(rows), 6) if rows else None,
        "total_net_pnl": round(total_pnl, 2),
        "avg_net_pnl": round(total_pnl / len(rows), 2) if rows else None,
    }


def _find_game5m_proposal(ledger: Dict[str, Any], proposal_id: str) -> Optional[Dict[str, Any]]:
    latest = ledger.get("latest_proposals") if isinstance(ledger.get("latest_proposals"), dict) else {}
    for p in latest.get("proposals") or []:
        if isinstance(p, dict) and str(p.get("proposal_id")) == str(proposal_id):
            return p
    return None


@app.get("/api/analyzer/tuning/status", response_class=JSONResponse)
async def analyzer_tuning_status(top_n: int = 8):
    """GAME_5M tuning ledger status for Analyzer UI."""
    ledger = _load_game5m_tuning_ledger()
    latest = ledger.get("latest_proposals") if isinstance(ledger.get("latest_proposals"), dict) else {}
    return _to_jsonable(
        {
            "ok": True,
            "ledger": str(_game5m_tuning_ledger_path()),
            "reglement": GAME5M_TUNING_REGLEMENT,
            "active_experiment": ledger.get("active_experiment"),
            "latest_generated_at_utc": latest.get("generated_at_utc"),
            "latest_selection": latest.get("selection"),
            "latest_proposal_count": len(latest.get("proposals") or []),
            "top_proposals": (latest.get("proposals") or [])[: max(1, min(int(top_n), 20))],
            "current_values": {
                "GAME_5M_TAKE_PROFIT_MIN_PCT": current_config_value("GAME_5M_TAKE_PROFIT_MIN_PCT"),
                "GAME_5M_TAKE_PROFIT_PCT": current_config_value("GAME_5M_TAKE_PROFIT_PCT"),
                "GAME_5M_MAX_POSITION_DAYS": current_config_value("GAME_5M_MAX_POSITION_DAYS"),
            },
        }
    )


@app.post("/api/analyzer/tuning/apply", response_class=JSONResponse)
async def analyzer_tuning_apply(request: Request):
    """Apply one GAME_5M tuning proposal or explicit key/value through shared guardrails."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Ожидается JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON object")

    ledger = _load_game5m_tuning_ledger()
    active = ledger.get("active_experiment") if isinstance(ledger.get("active_experiment"), dict) else None
    if active and active.get("status") == "pending_effect" and not bool(body.get("force")):
        raise HTTPException(status_code=409, detail="Уже есть active experiment pending_effect. Сначала observe/review или rollback.")

    proposal = _find_game5m_proposal(ledger, str(body.get("proposal_id") or "")) if body.get("proposal_id") else None
    key = str(body.get("key") or (proposal or {}).get("env_key") or "").strip()
    value = str(body.get("value") if body.get("value") is not None else (proposal or {}).get("proposed") or "").strip()
    observe_days = max(1, min(int(body.get("observe_days") or 2), 30))
    dry_run = bool(body.get("dry_run"))

    if not key or not value:
        raise HTTPException(status_code=400, detail="Нужны key/value или proposal_id")
    validation = validate_game5m_update(key, value)
    if not validation.ok:
        raise HTTPException(status_code=400, detail=f"Guardrails rejected: {validation.reason}")

    baseline = _game5m_closed_summary(observe_days)
    ok, record = apply_game5m_update(key, value, source="web_api_analyzer_tuning_apply", dry_run=dry_run)
    experiment = {
        "experiment_id": f"{record.get('env_key')}={record.get('new_value')}@{datetime.now(timezone.utc).isoformat()}",
        "status": "dry_run" if dry_run else ("pending_effect" if ok else "failed"),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "proposal_id": (proposal or {}).get("proposal_id"),
        "applied": record,
        "proposal": proposal,
        "baseline_summary": baseline,
        "observe_days": observe_days,
        "observations": [],
    }
    ledger["active_experiment"] = experiment
    ledger.setdefault("history", []).append(experiment)
    _save_game5m_tuning_ledger(ledger)
    if not ok:
        raise HTTPException(status_code=500, detail=record.get("status") or "write_failed")
    return _to_jsonable({"ok": True, "ledger": str(_game5m_tuning_ledger_path()), "experiment": experiment})


@app.post("/api/analyzer/tuning/observe", response_class=JSONResponse)
async def analyzer_tuning_observe(request: Request):
    """Attach current post-apply observation to active GAME_5M tuning experiment."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    ledger = _load_game5m_tuning_ledger()
    active = ledger.get("active_experiment") if isinstance(ledger.get("active_experiment"), dict) else None
    if not active:
        raise HTTPException(status_code=404, detail="Нет active experiment")
    days = max(1, min(int(body.get("days") or active.get("observe_days") or 2), 30))
    min_new_trades = max(1, min(int(body.get("min_new_trades") or 8), 100))
    summary = _game5m_closed_summary(days)
    obs = {"at_utc": datetime.now(timezone.utc).isoformat(), "summary": summary}
    active.setdefault("observations", []).append(obs)
    baseline_total = int((active.get("baseline_summary") or {}).get("closed_trades") or 0)
    if int(summary.get("closed_trades") or 0) >= baseline_total + min_new_trades:
        active["status"] = "ready_for_review"
        active["ready_at_utc"] = datetime.now(timezone.utc).isoformat()
    ledger["active_experiment"] = active
    ledger.setdefault("history", []).append({"type": "observation", "experiment_id": active.get("experiment_id"), **obs})
    _save_game5m_tuning_ledger(ledger)
    return _to_jsonable({"ok": True, "ledger": str(_game5m_tuning_ledger_path()), "active_experiment": active})


@app.post("/api/analyzer/tuning/rollback", response_class=JSONResponse)
async def analyzer_tuning_rollback():
    """Rollback active GAME_5M experiment to old_value recorded in ledger."""
    ledger = _load_game5m_tuning_ledger()
    active = ledger.get("active_experiment") if isinstance(ledger.get("active_experiment"), dict) else None
    if not active:
        raise HTTPException(status_code=404, detail="Нет active experiment")
    applied = active.get("applied") if isinstance(active.get("applied"), dict) else {}
    key = str(applied.get("env_key") or "").strip()
    old_value = applied.get("old_value")
    if not key or old_value is None:
        raise HTTPException(status_code=400, detail="В ledger нет old_value для rollback")
    ok, record = apply_game5m_update(key, old_value, source="web_api_analyzer_tuning_rollback")
    rollback_record = {
        "type": "rollback",
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment_id": active.get("experiment_id"),
        "rollback": record,
    }
    active["status"] = "rolled_back" if ok else "rollback_failed"
    active["rollback"] = rollback_record
    ledger["active_experiment"] = active
    ledger.setdefault("history", []).append(rollback_record)
    _save_game5m_tuning_ledger(ledger)
    if not ok:
        raise HTTPException(status_code=500, detail=record.get("status") or "rollback_failed")
    return _to_jsonable({"ok": True, "ledger": str(_game5m_tuning_ledger_path()), "active_experiment": active})


@app.get("/trading")
async def trading_page_removed():
    """Раздел «Торги» снят (дублировал портфель, отчёты и Telegram). Редирект на главную."""
    return RedirectResponse(url="/", status_code=302)


@app.post("/api/analyze", response_class=JSONResponse)
async def analyze_ticker(ticker: str = Form(...), use_llm: bool = Form(True)):
    """API: Анализ тикера (аналог /signal и /recommend в Telegram)"""
    try:
        agent = AnalystAgent(use_llm=use_llm)
        if use_llm:
            result = agent.get_decision_with_llm(ticker)
        else:
            decision = agent.get_decision(ticker)
            result = {
                "decision": decision,
                "technical_signal": "N/A",
                "sentiment": 0.0,
                "llm_analysis": None,
                "selected_strategy": None,
                "strategy_result": None,
                "technical_data": {},
            }
        if result.get("decision") == "NO_DATA":
            raise HTTPException(status_code=404, detail="Недостаточно данных для тикера")
        # Нормализуем ответ для фронта (все опциональные ключи)
        out = {
            "decision": result.get("decision", "HOLD"),
            "technical_signal": result.get("technical_signal", "N/A"),
            "sentiment": result.get("sentiment") if result.get("sentiment") is not None else 0.0,
            "llm_analysis": result.get("llm_analysis"),
            "selected_strategy": result.get("selected_strategy"),
            "strategy_result": result.get("strategy_result"),
            "llm_guidance": result.get("llm_guidance"),
            "technical_data": result.get("technical_data") or {},
            "reasoning": (result.get("strategy_result") or {}).get("reasoning"),
        }
        return _to_jsonable(out)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/execute", response_class=JSONResponse)
async def execute_trade(tickers: str = Form(..., description="Тикеры через запятую")):
    """API: Исполнить торговый цикл для тикеров (портфельная игра: сигнал → BUY при наличии, проверка стоп-лоссов)."""
    try:
        from config_loader import get_config_value
        if get_config_value("TRADING_CYCLE_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
            raise HTTPException(
                status_code=503,
                detail="Портфельная игра приостановлена (TRADING_CYCLE_ENABLED не включён в config.env). Включите для исполнения сделок.",
            )
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t and t.strip()]
        if not ticker_list:
            raise HTTPException(status_code=400, detail="Укажите хотя бы один тикер (через запятую)")
        exec_agent = ExecutionAgent()
        exec_agent.run_for_tickers(ticker_list)
        # Показать, какие сделки произошли за этот запуск (исключаем GAME_5M)
        recent = exec_agent.get_recent_trades(minutes_ago=2, exclude_strategy_name="GAME_5M")
        summary_lines = [f"Цикл выполнен для {len(ticker_list)} тикеров: {', '.join(ticker_list)}."]
        if recent:
            summary_lines.append("За цикл исполнено сделок: " + str(len(recent)))
            for r in recent[:10]:
                ts = r.get("ts")
                ts_str = ts.strftime("%H:%M") if hasattr(ts, "strftime") else str(ts)
                summary_lines.append(f"  {ts_str} {r.get('side')} {r.get('ticker')} x{r.get('quantity', 0):.0f} @ ${r.get('price', 0):.2f} ({r.get('signal_type', '')})")
        else:
            summary_lines.append("Новых сделок за цикл нет (сигналы HOLD/SELL или позиции уже открыты).")
        return {"status": "success", "message": "\n".join(summary_lines), "trades_count": len(recent)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request):
    """Страница управления базой знаний"""
    # Получаем последние новости (лимит увеличен, чтобы макро/центробанки не терялись)
    with engine.connect() as conn:
        news_df = pd.read_sql(
            text("""
                SELECT id, ts, ticker, source, content, sentiment_score
                FROM knowledge_base
                ORDER BY COALESCE(ingested_at, ts) DESC
                LIMIT 200
            """),
            conn
        )
        tickers_df = pd.read_sql(
            text("SELECT DISTINCT ticker FROM knowledge_base WHERE ticker IS NOT NULL AND ticker NOT IN ('MACRO', 'US_MACRO') ORDER BY ticker"),
            conn
        )
        tickers = tickers_df['ticker'].tolist() if not tickers_df.empty else []

    news_list = news_df.to_dict("records") if not news_df.empty else []
    for n in news_list:
        ts = n.get("ts")
        n["ts"] = _format_ts(ts)
        try:
            t = pd.Timestamp(ts)
            # Важно: в интерфейсе время помечается как ET через _format_ts без конвертации.
            # Поэтому если ts tz-naive — считаем, что он уже в ET (иначе фильтр "Сегодня" смещается).
            # Если ts tz-aware — конвертируем в America/New_York.
            if t.tzinfo is None:
                n["date_et"] = t.strftime("%Y-%m-%d")
            else:
                t_et = t.tz_convert("America/New_York")
                n["date_et"] = t_et.strftime("%Y-%m-%d")
        except Exception:
            n["date_et"] = ""
    sources_stats = get_news_sources_stats(engine, days=14)
    sources_total = sum(s["count"] for s in sources_stats)
    # Список источников по загруженным новостям — для фильтра (центробанки, Bloomberg и т.д.)
    news_sources = sorted({str(n.get("source") or "").strip() for n in news_list if n.get("source")})
    return HTMLResponse(render_template("knowledge.html", {
        "news": news_list,
        "tickers": tickers,
        "sources_stats": sources_stats,
        "sources_total": sources_total,
        "news_sources": news_sources,
    }))


@app.get("/api/news/sources", response_class=JSONResponse)
async def api_news_sources(days: int = 14):
    """API: список каналов новостей и количество записей за последние days дней."""
    days = max(1, min(365, days))
    stats = get_news_sources_stats(engine, days=days)
    return JSONResponse(content={"days": days, "sources": stats, "total": sum(s["count"] for s in stats)})


@app.post("/api/news/add", response_class=JSONResponse)
async def add_news_api(
    ticker: str = Form(...),
    source: str = Form(...),
    content: str = Form(...),
    sentiment_score: Optional[float] = Form(None)
):
    """API: Добавить новость"""
    try:
        from config_loader import get_config_value
        from services.sentiment_analyzer import calculate_sentiment
        
        # Автоматический расчет sentiment, если не указан
        if sentiment_score is None:
            auto_calculate = get_config_value('SENTIMENT_AUTO_CALCULATE', 'false').lower() == 'true'
            if auto_calculate:
                try:
                    sentiment_score, _ = calculate_sentiment(content)
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось рассчитать sentiment: {e}")
        
        add_news(engine, ticker, source, content, sentiment_score)
        return {"status": "success", "message": "Новость добавлена", "sentiment": sentiment_score}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/news/generate_llm", response_class=JSONResponse)
async def generate_news_llm_api(request: Request):
    """API: Сгенерировать группу новостей по теме через LLM (для ручного ввода в базу)."""
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        topic = (body.get("topic") or "").strip()
        if not topic:
            raise HTTPException(status_code=400, detail="topic обязателен")
        tickers = body.get("tickers")
        if tickers is not None and not isinstance(tickers, list):
            tickers = [t.strip() for t in str(tickers).split(",") if t.strip()]
        try:
            from services.llm_service import get_llm_service
            from services.ticker_groups import get_tracked_tickers_for_kb
            if tickers is None or len(tickers) == 0:
                tickers = get_tracked_tickers_for_kb()
            llm = get_llm_service()
            items = llm.generate_news_by_topic(topic, tickers=tickers)
        except Exception as e:
            logger.exception("generate_news_llm: %s", e)
            raise HTTPException(status_code=500, detail=str(e))
        return {"status": "success", "items": items}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/news/add_batch", response_class=JSONResponse)
async def add_news_batch_api(request: Request):
    """API: Ввести группу новостей в базу (массив объектов с ticker, source, content, sentiment_score, insight?, link?)."""
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        items = body.get("items")
        if not items or not isinstance(items, list):
            raise HTTPException(status_code=400, detail="items — непустой массив обязателен")
        from datetime import datetime
        inserted = 0
        with engine.begin() as conn:
            for it in items:
                ticker = (it.get("ticker") or "").strip().upper()
                source = (it.get("source") or "Manual (LLM)")[:200]
                content = (it.get("content") or "").strip()
                if not ticker or not content:
                    continue
                try:
                    sent = float(it.get("sentiment_score", 0.5))
                    sent = max(0.0, min(1.0, sent))
                except (TypeError, ValueError):
                    sent = 0.5
                insight = (it.get("insight") or "").strip()[:1000] or None
                link = (it.get("link") or "").strip()[:500] or None
                conn.execute(text("""
                    INSERT INTO knowledge_base (ts, ticker, source, content, sentiment_score, insight, event_type, link)
                    VALUES (:ts, :ticker, :source, :content, :sentiment_score, :insight, 'NEWS', :link)
                """), {
                    "ts": datetime.now(),
                    "ticker": ticker,
                    "source": source,
                    "content": content[:8000],
                    "sentiment_score": sent,
                    "insight": insight,
                    "link": link,
                })
                inserted += 1
        return {"status": "success", "message": f"Добавлено новостей: {inserted}", "inserted": inserted}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("add_news_batch: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


def _build_chart5m_trades_only(ticker: str, days: int) -> Dict[str, Any]:
    """Минимальный ответ для chart5m при отсутствии OHLC: только сделки GAME_5M за последние days дней."""
    from datetime import datetime, timedelta
    try:
        from services.game_5m import get_open_position, get_open_position_any, get_trades_for_chart, trade_ts_to_et
    except ImportError:
        return {"ticker": ticker, "days": days, "times": [], "close": [], "trades": [], "no_ohlc": True}
    now = datetime.utcnow()
    dt_end = now
    dt_start = now - timedelta(days=min(days + 2, 14))
    trades_out = []
    try:
        for t in get_trades_for_chart(ticker, dt_start, dt_end):
            ts = t.get("ts")
            stored_tz = t.get("ts_timezone")
            ts_et = trade_ts_to_et(ts, source_tz=stored_tz)
            if ts_et is not None and hasattr(ts_et, "isoformat"):
                ts = ts_et.isoformat()
            elif hasattr(ts, "isoformat"):
                ts = ts.isoformat()
            row = {
                "ts": ts,
                "price": float(t.get("price", 0)),
                "quantity": float(t.get("quantity") or 0),
                "side": t.get("side"),
                "signal_type": t.get("signal_type"),
                "id": int(t.get("id") or 0),
            }
            ct = t.get("chart_ts")
            if ct:
                row["chart_ts"] = ct
            trades_out.append(row)
    except Exception:
        pass
    entry_price = None
    try:
        pos = get_open_position_any(ticker) or get_open_position(ticker)
        if pos and pos.get("entry_price") is not None:
            entry_price = float(pos["entry_price"])
    except Exception:
        pass
    pf5 = None
    try:
        from services.recommend_5m import get_decision_5m

        d5_tr = get_decision_5m(ticker, days=days, use_llm_news=False)
        if d5_tr:
            pf5 = d5_tr.get("price_forecast_5m")
    except Exception:
        pass

    return {
        "ticker": str(ticker),
        "days": int(days),
        "times": [],
        "day_boundaries": [],
        "close": [],
        "dt_max": None,
        "dt_max_ext": None,
        "prolongation": {"times": [], "prices": [], "forecast_defined": False, "label": None},
        "entry_price": entry_price,
        "session_high": None,
        "take_level": None,
        "take_pct": None,
        "trades": trades_out,
        "no_ohlc": True,
        "price_forecast_5m": pf5,
    }


def _build_chart5m_data(ticker: str, days: int, *, source: str = "live") -> Optional[Dict[str, Any]]:
    """Строит данные для графика 5m с пролонгацией (те же функции, что в Telegram)."""
    try:
        from services.recommend_5m import fetch_5m_ohlc, get_decision_5m
        from services.chart_prolongation import fit_and_prolong
        from services.game_5m import get_open_position, get_open_position_any, get_trades_for_chart
    except ImportError:
        return None

    def _fetch_5m_from_db(symbol: str, days_back: int) -> Optional[pd.DataFrame]:
        """
        Пытаемся взять 5m из Postgres (market_bars_5m), чтобы графики были стабильными и одинаковыми
        по глубине при фильтре «Все». Fallback остаётся на Yahoo/yfinance.
        """
        sym = (symbol or "").strip().upper()
        if not sym:
            return None
        try:
            days_back = int(days_back or 1)
        except (TypeError, ValueError):
            days_back = 1
        days_back = min(max(1, days_back), 7)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back + 1)
        try:
            with engine.connect() as conn:
                df_db = pd.read_sql(
                    text(
                        """
                        SELECT bar_start_utc AS datetime,
                               open AS "Open",
                               high AS "High",
                               low  AS "Low",
                               close AS "Close",
                               volume AS "Volume"
                        FROM public.market_bars_5m
                        WHERE exchange = 'US'
                          AND symbol = :sym
                          AND bar_start_utc >= :cutoff
                        ORDER BY bar_start_utc ASC
                        """
                    ),
                    conn,
                    params={"sym": sym, "cutoff": cutoff},
                )
        except Exception:
            return None
        if df_db is None or df_db.empty or "datetime" not in df_db.columns or "Close" not in df_db.columns:
            return None
        try:
            d = pd.to_datetime(df_db["datetime"], errors="coerce")
            # В БД хранится UTC → приводим к US/Eastern для единой оси времени в UI.
            if hasattr(d, "dt") and d.dt.tz is not None:
                d = d.dt.tz_convert("America/New_York")
            df_db = df_db.copy()
            df_db["datetime"] = d
        except Exception:
            pass
        return df_db

    source = (source or "live").strip().lower()
    if source not in ("live", "db", "auto"):
        source = "live"
    try:
        if source == "db":
            df = _fetch_5m_from_db(ticker, days)
            if df is None or df.empty:
                df = fetch_5m_ohlc(ticker, days=days)
        elif source == "auto":
            df = _fetch_5m_from_db(ticker, days)
            if df is None or df.empty:
                df = fetch_5m_ohlc(ticker, days=days)
            else:
                # Если БД явно устарела — пробуем Yahoo для "живого" окна.
                try:
                    now_et = pd.Timestamp.now(tz="America/New_York")
                    dt_max_db = pd.to_datetime(df["datetime"]).max()
                    dt_max_db = pd.Timestamp(dt_max_db)
                    if dt_max_db.tzinfo is None:
                        dt_max_db = dt_max_db.tz_localize("America/New_York", ambiguous=True)
                    else:
                        dt_max_db = dt_max_db.tz_convert("America/New_York")
                    if now_et - dt_max_db > pd.Timedelta(minutes=45):
                        df_y = fetch_5m_ohlc(ticker, days=days)
                        if df_y is not None and not df_y.empty and "datetime" in df_y.columns:
                            dt_max_y = pd.Timestamp(pd.to_datetime(df_y["datetime"]).max())
                            if dt_max_y.tzinfo is None:
                                dt_max_y = dt_max_y.tz_localize("America/New_York", ambiguous=True)
                            else:
                                dt_max_y = dt_max_y.tz_convert("America/New_York")
                            if dt_max_y > dt_max_db:
                                df = df_y
                except Exception:
                    pass
        else:
            # live: сначала Yahoo (реальное время), затем БД как fallback.
            df = fetch_5m_ohlc(ticker, days=days)
            if df is None or df.empty:
                df = _fetch_5m_from_db(ticker, days)
    except Exception:
        return None
    if df is None or df.empty or "Close" not in df.columns:
        for fallback_days in (7, 5, 2, 1):
            if fallback_days == days:
                continue
            try:
                if source == "live":
                    df = fetch_5m_ohlc(ticker, days=fallback_days)
                    if df is None or df.empty:
                        df = _fetch_5m_from_db(ticker, fallback_days)
                else:
                    df = _fetch_5m_from_db(ticker, fallback_days)
                    if df is None or df.empty:
                        df = fetch_5m_ohlc(ticker, days=fallback_days)
            except Exception:
                continue
            if df is not None and not df.empty and "Close" in df.columns:
                days = fallback_days
                break
        else:
            # Нет 5m свечей (тикер вне FAST или Yahoo не отдаёт 5m) — всё равно отдаём сделки GAME_5M за период
            return _build_chart5m_trades_only(ticker, days)
    try:
        df["datetime"] = pd.to_datetime(df["datetime"])
    except Exception:
        return None
    dt_min = df["datetime"].min()
    dt_max = df["datetime"].max()
    last_close = float(df["Close"].iloc[-1])
    extend_hours = 2
    dt_max_ext = dt_max + pd.Timedelta(hours=extend_hours)

    def _ts_str(t):
        if hasattr(t, "isoformat"):
            s = t.isoformat()
            return s if isinstance(s, str) else str(s)
        return str(t)

    times = [_ts_str(t) for t in df["datetime"].tolist()]
    close = [float(x) for x in df["Close"].astype(float).tolist()]

    ohlc_block: Optional[Dict[str, list]] = None
    try:
        _ohlc_cols = ("Open", "High", "Low", "Close")
        if all(c in df.columns for c in _ohlc_cols):
            ohlc_block = {
                "open": [float(x) for x in df["Open"].astype(float).tolist()],
                "high": [float(x) for x in df["High"].astype(float).tolist()],
                "low": [float(x) for x in df["Low"].astype(float).tolist()],
                "close": [float(x) for x in df["Close"].astype(float).tolist()],
            }
    except Exception:
        ohlc_block = None

    # Индексы начала нового торгового дня (для вертикальных границ на графике)
    day_boundaries = []
    for i in range(1, len(times)):
        s0, s1 = times[i - 1], times[i]
        d0 = s0[:10] if isinstance(s0, str) and len(s0) >= 10 else str(s0)[:10]
        d1 = s1[:10] if isinstance(s1, str) and len(s1) >= 10 else str(s1)[:10]
        if d0 != d1:
            day_boundaries.append(i)

    entry_price = None
    try:
        # Как крон и /pending: сначала любая открытая позиция по тикеру, иначе только GAME_5M
        pos = get_open_position_any(ticker) or get_open_position(ticker)
        if pos and pos.get("entry_price") is not None:
            entry_price = float(pos["entry_price"])
    except Exception:
        pass

    try:
        d5_chart = get_decision_5m(ticker, days=days, use_llm_news=False)
    except Exception:
        d5_chart = None
    session_high = None
    take_level = None
    take_pct_for_chart = None
    if d5_chart and entry_price and entry_price > 0:
        sh = d5_chart.get("session_high")
        session_high = float(sh) if sh is not None and (isinstance(sh, (int, float)) or hasattr(sh, "__float__")) else None
        try:
            from services.game_5m import _effective_take_profit_pct
            mom = d5_chart.get("momentum_2h_pct")
            take_pct = _effective_take_profit_pct(mom, ticker=ticker)
            take_pct_for_chart = float(take_pct)
            take_level = float(entry_price * (1 + take_pct / 100.0))
        except Exception:
            pass

    min_bars_trend = 5
    prolong_bars = 12
    prolongation_method = "ema"
    prolongation_times = []
    prolongation_prices = []
    forecast_defined = False
    forecast_label = None
    if len(df) >= min_bars_trend and last_close > 0:
        closes_tail = df["Close"].astype(float).iloc[-min_bars_trend:].values
        res = fit_and_prolong(closes_tail, method=prolongation_method, prolong_bars=prolong_bars)
        slope_per_bar = res["slope_per_bar"]
        min_slope_pct = 0.01
        min_slope = last_close * (min_slope_pct / 100.0)
        if slope_per_bar >= min_slope or slope_per_bar <= -min_slope:
            curve_prices = res["curve_prices"]
            anchor_shift = last_close - (curve_prices[0] if curve_prices else last_close)
            curve_prices = [p + anchor_shift for p in curve_prices]
            bar_offsets = res["curve_bar_offsets"]
            prolongation_times = [
                _ts_str(dt_max + pd.Timedelta(minutes=5 * k)) for k in bar_offsets
            ]
            prolongation_prices = [float(p) for p in curve_prices]
            forecast_defined = True
            forecast_label = "Прогноз ↑" if slope_per_bar >= min_slope else "Прогноз ↓"
        else:
            prolongation_times = [_ts_str(dt_max), _ts_str(dt_max_ext)]
            prolongation_prices = [float(last_close), float(last_close)]
            forecast_label = None
    else:
        prolongation_times = [_ts_str(dt_max), _ts_str(dt_max_ext)]
        prolongation_prices = [float(last_close), float(last_close)]

    trades = []
    # Время первой покупки в ET (для честного "high so far" на момент входа).
    first_buy_ts_et = None
    try:
        from services.game_5m import trade_ts_to_et, match_trade_to_chart_bar_index, refine_bar_index_for_trade_price
        # Диапазон графика (ET); внутри get_trades_for_chart конвертируется в MSK и фильтруется по ET
        for t in get_trades_for_chart(ticker, dt_min, dt_max):
            ts = t.get("ts")
            # График в ET; используем ts_timezone из строки (в БД храним явно)
            stored_tz = t.get("ts_timezone")
            ts_et = trade_ts_to_et(ts, source_tz=stored_tz)
            if ts_et is not None and hasattr(ts_et, "isoformat"):
                ts = ts_et.isoformat()
            elif hasattr(ts, "isoformat"):
                ts = ts.isoformat()
            row = {
                "ts": ts,
                "price": float(t.get("price", 0)),
                "quantity": float(t.get("quantity") or 0),
                "side": t.get("side"),
                "signal_type": t.get("signal_type"),
                "id": int(t.get("id") or 0),
            }
            ct = t.get("chart_ts")
            if ct:
                row["chart_ts"] = ct
            time_for_bar = ct or ts
            bi = match_trade_to_chart_bar_index(times, time_for_bar)
            if (
                bi is not None
                and ohlc_block is not None
                and isinstance(ohlc_block.get("low"), list)
                and isinstance(ohlc_block.get("high"), list)
                and len(ohlc_block["low"]) == len(times)
                and len(ohlc_block["high"]) == len(times)
            ):
                bi = refine_bar_index_for_trade_price(
                    bi,
                    float(row["price"]),
                    ohlc_block["low"],
                    ohlc_block["high"],
                    times,
                    time_for_bar,
                )
            if bi is not None:
                row["bar_index"] = int(bi)
            trades.append(row)
            try:
                if first_buy_ts_et is None and (t.get("side") or "").upper() == "BUY":
                    ts_src = ct or t.get("ts")
                    ts0 = trade_ts_to_et(ts_src, source_tz="America/New_York" if ct else t.get("ts_timezone"))
                    if ts0 is not None:
                        first_buy_ts_et = pd.Timestamp(ts0).tz_convert("America/New_York") if pd.Timestamp(ts0).tzinfo else pd.Timestamp(ts0).tz_localize("America/New_York", ambiguous=True)
            except Exception:
                pass
    except Exception:
        pass

    # High so far: максимум High по барам до первой BUY-сделки (если она есть в окне).
    # Это делает визуализацию честнее: не сравниваем вход с "high дня", который мог случиться позже.
    try:
        if first_buy_ts_et is not None and "High" in df.columns and "datetime" in df.columns:
            dtt = pd.to_datetime(df["datetime"])
            if dtt.dt.tz is None:
                dtt = dtt.dt.tz_localize("America/New_York", ambiguous=True)
            else:
                dtt = dtt.dt.tz_convert("America/New_York")
            mask = dtt <= first_buy_ts_et
            if mask.any():
                hs = pd.to_numeric(df.loc[mask, "High"], errors="coerce").max()
                if hs is not None and pd.notna(hs):
                    session_high = float(hs)
    except Exception:
        pass

    def _f(x):
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        if hasattr(x, "item"):
            try:
                return float(x.item())
            except (ValueError, TypeError):
                return None
        return x

    pf5 = None
    if d5_chart:
        pf5 = d5_chart.get("price_forecast_5m")

    return {
        "ticker": str(ticker),
        "days": int(days),
        "times": list(times),
        "day_boundaries": list(day_boundaries),
        "close": [float(c) for c in close],
        "ohlc": ohlc_block,
        "dt_max": _ts_str(dt_max),
        "dt_max_ext": _ts_str(dt_max_ext),
        "prolongation": {
            "times": list(prolongation_times),
            "prices": [float(p) for p in prolongation_prices],
            "forecast_defined": bool(forecast_defined),
            "label": str(forecast_label) if forecast_label else None,
        },
        "entry_price": _f(entry_price),
        "session_high": _f(session_high),
        "take_level": _f(take_level),
        "take_pct": _f(take_pct_for_chart),
        "trades": [
            {
                "ts": t["ts"],
                "price": _f(t.get("price")),
                "quantity": float(t.get("quantity") or 0),
                "side": t.get("side"),
                "signal_type": t.get("signal_type"),
                "id": int(t.get("id") or 0),
            }
            for t in trades
        ],
        "price_forecast_5m": pf5,
    }


@app.get("/api/chart5m/{ticker}")
async def get_chart5m(ticker: str, days: int = 1, source: str = "live"):
    """API: Данные для графика 5m с зоной пролонгации (EMA, тренд при ≥5 свечах)."""
    err_404 = "Нет 5m данных: Yahoo не вернул свечи. Обычно 5m доступны в торговые часы США. Попробуйте 5 или 7 дней."
    source_norm = (source or "live").strip().lower()
    if source_norm == "live":
        # В live режиме график нужен как “текущая сессия”, иначе по оси времени
        # (в основном HH:MM) получается неочевидная каша при нескольких днях.
        days = 1
    else:
        days = min(max(1, days), 7)
    try:
        loop = asyncio.get_running_loop()
        fn = functools.partial(_build_chart5m_data, ticker, days, source=source_norm)
        data = await loop.run_in_executor(None, fn)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Ошибка загрузки 5m: {type(e).__name__}: {e!s}"},
        )
    if data is None:
        return JSONResponse(status_code=404, content={"detail": err_404})
    try:
        body = _to_jsonable(data)
        return JSONResponse(content=body)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Ошибка формирования ответа: {type(e).__name__}: {e!s}"},
        )


@app.get("/visualization", response_class=HTMLResponse)
async def visualization_page(request: Request):
    """Страница визуализации. График 5m по тикерам игры; query ``ticker=`` — один тикер (см. выпадающий список на странице)."""
    with engine.connect() as conn:
        tickers_df = pd.read_sql(
            text("SELECT DISTINCT ticker FROM quotes ORDER BY ticker"),
            conn
        )
        tickers = tickers_df['ticker'].tolist() if not tickers_df.empty else []
    try:
        from services.ticker_groups import get_tickers_game_5m
        tickers_5m = list(get_tickers_game_5m() or [])
    except Exception:
        tickers_5m = list(get_tickers_fast() or [])
    if not tickers_5m:
        tickers_5m = ["SNDK"]
    return HTMLResponse(render_template("visualization.html", {
        "tickers": tickers,
        "tickers_5m": tickers_5m,
        "tickers_5m_json": json.dumps(tickers_5m),
    }))


def _json_safe_default(obj):
    """Для json.dumps: несериализуемые типы (numpy, datetime) → None или строка."""
    if obj is None:
        return None
    try:
        if hasattr(pd, "isna") and pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except (ValueError, TypeError):
            return None
    if hasattr(obj, "item"):
        try:
            x = obj.item()
            if isinstance(x, float) and (x != x or x == float("inf") or x == float("-inf")):
                return None
            return int(x) if isinstance(x, (np.integer, int)) else x
        except (ValueError, TypeError):
            return None
    return str(obj)


def _make_json_safe(obj: Any) -> Any:
    """Рекурсивно заменяет nan/inf и несериализуемые значения на None. Результат безопасен для json.dumps."""
    if obj is None:
        return None
    if isinstance(obj, Decimal):
        try:
            v = float(obj)
            if isinstance(v, float) and (v != v or v == float("inf") or v == float("-inf")):
                return None
            return v
        except (ValueError, TypeError, OverflowError):
            return None
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, float):
        if obj != obj or obj == float("inf") or obj == float("-inf"):
            return None
        return obj
    try:
        if hasattr(pd, "isna") and pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except (ValueError, TypeError):
            return None
    if hasattr(obj, "item"):
        try:
            x = obj.item()
            if isinstance(x, float) and (x != x or x == float("inf") or x == float("-inf")):
                return None
            return int(x) if isinstance(x, (np.integer, int)) else x
        except (ValueError, TypeError):
            return None
    if isinstance(obj, (int, str)):
        return obj
    return str(obj)


def _api_json_body(obj: Any) -> Any:
    """Ответы API под JSONResponse: numpy/Decimal/nan + защита от остаточных типов (иначе 500)."""
    return _make_json_safe(_to_jsonable(obj))


@app.get("/api/trades", response_class=JSONResponse)
async def get_trades(limit: int = 100):
    """API: Получить историю сделок (аналог /history в Telegram)."""
    limit = max(1, min(500, int(limit)))
    try:
        with engine.connect() as conn:
            df = pd.read_sql(
                text("""
                    SELECT ts, ticker, side, quantity, price, commission,
                           signal_type, total_value, sentiment_at_trade, strategy_name
                    FROM public.trade_history
                    ORDER BY ts DESC
                    LIMIT :lim
                """),
                conn,
                params={"lim": limit}
            )
    except Exception as e:
        body = json.dumps({"trades": [], "error": str(e)}, default=_json_safe_default, ensure_ascii=False)
        return Response(content=body.encode("utf-8"), media_type="application/json")

    def _safe_float(x):
        if x is None:
            return None
        try:
            if pd.isna(x):
                return None
        except (TypeError, ValueError):
            pass
        try:
            f = float(x)
            if f != f or f == float("inf") or f == float("-inf"):
                return None
            return f
        except (TypeError, ValueError):
            return None

    def _to_native(val):
        if val is None:
            return None
        try:
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            pass
        if hasattr(val, "isoformat") and not (hasattr(pd, "isna") and pd.isna(val)):
            try:
                return val.isoformat()
            except (ValueError, TypeError):
                return None
        if hasattr(val, "item"):
            try:
                x = val.item()
                if pd.isna(x):
                    return None
                if isinstance(x, (np.integer,)):
                    return int(x)
                if isinstance(x, (np.floating, float)):
                    return _safe_float(x)
                if isinstance(x, (int, str)):
                    return x
                return _safe_float(x) if isinstance(x, (int, float)) else str(x)
            except (ValueError, TypeError):
                return None
        if isinstance(val, (np.integer, np.int64, np.int32)):
            return int(val)
        if isinstance(val, (np.floating, np.float64, np.float32, float)):
            return _safe_float(val)
        if isinstance(val, (int, str)):
            return val
        if isinstance(val, (int, float)):
            return _safe_float(val) if isinstance(val, float) else int(val)
        return str(val) if val is not None else None

    trades = []
    for _, row in df.iterrows():
        t = {
            "ts": _to_native(row.get("ts")),
            "ticker": _to_native(row.get("ticker")),
            "side": _to_native(row.get("side")),
            "quantity": _to_native(row.get("quantity")),
            "price": _to_native(row.get("price")),
            "commission": _to_native(row.get("commission")),
            "signal_type": _to_native(row.get("signal_type")),
            "total_value": _to_native(row.get("total_value")),
            "sentiment_at_trade": _to_native(row.get("sentiment_at_trade")),
            "strategy_name": _to_native(row.get("strategy_name")),
        }
        for k, v in list(t.items()):
            if isinstance(v, float) and (v != v or v == float("inf") or v == float("-inf")):
                t[k] = None
        trades.append(t)

    # Сериализуем сами с default=…, чтобы nan/inf и numpy-типы не ломали json.dumps у Starlette
    payload = _make_json_safe({"trades": trades})
    body = json.dumps(payload, default=_json_safe_default, ensure_ascii=False)
    return Response(content=body.encode("utf-8"), media_type="application/json")


@app.get("/api/game5m", response_class=JSONResponse)
async def get_game5m(ticker: str = None, limit: int = 20):
    """API: Мониторинг игры 5m — открытая позиция и закрытые сделки по тикеру (strategy_name=GAME_5M)."""
    if ticker is None or not ticker.strip():
        ticker = _default_ticker_5m()
    else:
        ticker = ticker.strip().upper()
    try:
        from services.game_5m import get_open_position, get_recent_results, get_strategy_params
        pos = get_open_position(ticker)
        strategy_params = get_strategy_params()
        results = get_recent_results(ticker, limit=min(50, max(5, limit)))
        closed = []
        for r in results:
            entry_ts = r.get("entry_ts")
            exit_ts = r.get("exit_ts")
            closed.append({
                "entry_ts": entry_ts.isoformat() if hasattr(entry_ts, "isoformat") else str(entry_ts),
                "exit_ts": exit_ts.isoformat() if hasattr(exit_ts, "isoformat") else str(exit_ts),
                "exit_signal_type": r.get("exit_signal_type"),
                "pnl_pct": r.get("pnl_pct"),
                "pnl_usd": r.get("pnl_usd"),
            })
        pnls = [r["pnl_pct"] for r in results if r.get("pnl_pct") is not None]
        pnls_usd = [r["pnl_usd"] for r in results if r.get("pnl_usd") is not None]
        total = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        sum_usd = sum(pnls_usd) if pnls_usd else None
        return {
            "ticker": ticker,
            "strategy_params": strategy_params,
            "open_position": {
                "entry_ts": pos["entry_ts"].isoformat() if pos and hasattr(pos.get("entry_ts"), "isoformat") else None,
                "entry_price": pos["entry_price"] if pos else None,
                "quantity": pos["quantity"] if pos else None,
                "entry_signal_type": pos.get("entry_signal_type") if pos else None,
            } if pos else None,
            "closed_trades": closed,
            "win_rate_pct": (100.0 * wins / total) if total else 0.0,
            "avg_pnl_pct": (sum(pnls) / total) if total else None,
            "total_pnl_usd": sum_usd,
            "total_closed": total,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/game5m/cards", response_class=JSONResponse)
async def get_game5m_cards(days: int = 5):
    """API: Карточки по всем тикерам игры 5m для веб-мониторинга (Telegram). Без LLM. Payload из get_5m_card_payload."""
    try:
        from services.ticker_groups import get_tickers_game_5m
        from services.recommend_5m import get_decision_5m, get_5m_card_payload
    except ImportError:
        raise HTTPException(status_code=501, detail="Модули recommend_5m / ticker_groups недоступны")
    tickers = list(get_tickers_game_5m() or [])
    if not tickers:
        return _to_jsonable({"tickers": [], "cards": [], "updated_at": None})
    days = min(max(1, days), 7)
    cards = []
    for tkr in tickers:
        try:
            d5 = get_decision_5m(tkr, days=days, use_llm_news=False)
        except Exception:
            d5 = None
        card = get_5m_card_payload(d5, tkr)
        if card.get("reasoning") and len(card["reasoning"]) > 400:
            card["reasoning"] = card["reasoning"][:400]
        session = (d5 or {}).get("market_session") or {}
        card["session_phase"] = session.get("session_phase")
        cards.append(card)
    return _to_jsonable({
        "tickers": tickers,
        "cards": cards,
        "updated_at": _now_et().isoformat() if DISPLAY_TZ else datetime.now().isoformat(),
    })


def _compute_portfolio_cards_sync(corr_days: int) -> Dict[str, Any]:
    """
    Карточки портфельной игры: дневные quotes + стратегия без LLM.
    LLM по портфелю только через GET /api/portfolio/card/{ticker}/llm (кнопка на карточке).
    """
    from services.portfolio_card import (
        get_portfolio_cluster_context,
        portfolio_card_payload,
        load_fallback_portfolio_take_pct,
    )

    ctx, trade = get_portfolio_cluster_context(days=min(max(5, corr_days), 120))
    take_fb = load_fallback_portfolio_take_pct()
    try:
        from services.portfolio_catboost_signal import predict_portfolio_expected_returns

        ml_by_ticker = predict_portfolio_expected_returns(trade)
    except Exception as e:
        logger.debug("portfolio ML batch skipped: %s", e)
        ml_by_ticker = {}
    agent = AnalystAgent(use_llm=False)
    cards: List[Dict[str, Any]] = []
    for t in trade:
        try:
            r = agent.get_decision_with_llm(t, cluster_context=ctx)
            if r.get("decision") == "NO_DATA":
                cards.append({
                    "ticker": t,
                    "decision": "NO_DATA",
                    "horizon": "daily",
                    "reasoning": "Нет данных в quotes (или недостаточно истории).",
                })
            else:
                card = portfolio_card_payload(t, r, fallback_take_pct=take_fb)
                card.update(ml_by_ticker.get(t, {}))
                cards.append(card)
        except Exception as e:
            cards.append({"ticker": t, "decision": "ERROR", "horizon": "daily", "error": str(e)})
    return {
        "tickers": trade,
        "correlation_tickers": (ctx or {}).get("tickers"),
        "cards": cards,
        "updated_at": _now_et().isoformat() if DISPLAY_TZ else datetime.now().isoformat(),
    }


def _compute_portfolio_card_llm_sync(ticker: str, corr_days: int) -> Dict[str, Any]:
    """LLM по одному тикеру портфеля: полный контекст (корреляция кластера, новости, риск-лимиты в карточке)."""
    from services.portfolio_card import (
        get_portfolio_cluster_context,
        portfolio_card_payload,
        portfolio_llm_public_payload,
        load_fallback_portfolio_take_pct,
        get_portfolio_trade_tickers,
    )

    trade = get_portfolio_trade_tickers()
    if ticker not in trade:
        return {"_error": "Тикер не в списке портфельной игры", "_status": 404}
    ctx, _ = get_portfolio_cluster_context(days=min(max(5, corr_days), 120))
    agent = AnalystAgent(use_llm=get_use_llm_for_analyst(engine=engine))
    r = agent.get_decision_with_llm(ticker, cluster_context=ctx)
    if r.get("decision") == "NO_DATA":
        return {"_error": "Нет данных quotes", "_status": 404}
    base = portfolio_card_payload(ticker, r, fallback_take_pct=load_fallback_portfolio_take_pct())
    try:
        from services.portfolio_catboost_signal import predict_portfolio_expected_return

        base.update(predict_portfolio_expected_return(ticker))
    except Exception as e:
        logger.debug("portfolio ML single skipped for %s: %s", ticker, e)
    merged = {
        **base,
        "llm_analysis": _make_json_safe(r.get("llm_analysis")),
        "technical_data": _make_json_safe(r.get("technical_data")),
        "strategy_result": _make_json_safe(r.get("strategy_result")),
        "news_count": r.get("news_count"),
        "base_decision": r.get("base_decision"),
    }
    return portfolio_llm_public_payload(merged)


def _msk_naive_datetime() -> datetime:
    """«Сейчас» в Europe/Moscow без tzinfo — как типичное хранение ts в trade_history."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def _load_portfolio_daily_chart_trades(ticker: str, cutoff: datetime, dt_hi: datetime) -> List[Dict[str, Any]]:
    """Сделки для маркеров на дневном графике портфеля: всё, кроме GAME_5M (портфельный цикл, Manual и т.д.)."""
    t = (ticker or "").strip().upper()
    if not t:
        return []
    rows: List[Any] = []
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, ts, side, price, quantity, signal_type, strategy_name
                FROM public.trade_history
                WHERE ticker = :ticker
                  AND ts >= :dt_min AND ts <= :dt_max
                  AND UPPER(COALESCE(TRIM(strategy_name), '')) <> 'GAME_5M'
                ORDER BY ts ASC, id ASC
            """),
            {"ticker": t, "dt_min": cutoff, "dt_max": dt_hi},
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        ts_raw = r[1]
        ts_s = ts_raw.isoformat() if hasattr(ts_raw, "isoformat") else str(ts_raw)
        out.append(
            {
                "id": int(r[0]),
                "ts": ts_s,
                "side": (r[2] or "").strip().upper(),
                "price": float(r[3]) if r[3] is not None else 0.0,
                "quantity": float(r[4] or 0) if r[4] is not None else 0.0,
                "signal_type": (r[5] or "") if r[5] is not None else "",
                "strategy_name": (r[6] or "") if len(r) > 6 and r[6] is not None else "",
            }
        )
    return out


def _load_portfolio_open_position_for_chart(ticker: str) -> Dict[str, Any]:
    """Open non-GAME_5M position details for portfolio daily chart reference lines."""
    t = (ticker or "").strip().upper()
    if not t:
        return {}
    report_engine = get_engine()
    with report_engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT id, ts, ticker, side, quantity, price,
                       commission, signal_type, total_value, sentiment_at_trade, strategy_name,
                       take_profit, stop_loss, mfe, mae, context_json
                FROM public.trade_history
                WHERE UPPER(TRIM(ticker)) = :ticker
                  AND UPPER(COALESCE(TRIM(strategy_name), '')) <> 'GAME_5M'
                ORDER BY ts ASC, id ASC
            """),
            conn,
            params={"ticker": t},
        )
    if df.empty:
        return {}
    open_positions = compute_open_positions(df)
    pos = next((p for p in open_positions if (p.ticker or "").strip().upper() == t), None)
    if not pos or pos.entry_price is None:
        return {}
    try:
        entry_price = float(pos.entry_price)
    except (TypeError, ValueError):
        return {}
    take_pct = None
    take_level = None
    try:
        if pos.take_profit is not None:
            take_pct = float(pos.take_profit)
            if take_pct > 0 and entry_price > 0:
                take_level = entry_price * (1.0 + take_pct / 100.0)
    except (TypeError, ValueError):
        take_pct = None
        take_level = None
    return {
        "entry_price": entry_price,
        "entry_ts": pos.entry_ts,
        "strategy_name": pos.strategy_name,
        "take_pct": take_pct,
        "take_level": take_level,
        "quantity": float(pos.quantity or 0),
    }


def _build_portfolio_daily_chart_data(ticker: str, days: int) -> Dict[str, Any]:
    """Дневной график из БД quotes (open/high/low/close), не intraday 5m; сделки портфеля (не GAME_5M) для маркеров."""
    days = min(max(10, int(days)), 730)
    cutoff = datetime.now() - timedelta(days=days)
    # Окно сделок — по MSK (как в trade_history), чуть шире по нижней границе из-за часовых поясов сервера
    now_msk = _msk_naive_datetime()
    trade_lo = now_msk - timedelta(days=days + 2)
    trade_hi = now_msk + timedelta(days=2)
    trades = _load_portfolio_daily_chart_trades(ticker, trade_lo, trade_hi)
    position = _load_portfolio_open_position_for_chart(ticker)
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT date, open, high, low, close, volume, sma_5, rsi, volatility_5
                FROM quotes
                WHERE ticker = :ticker AND date >= :cutoff
                ORDER BY date ASC
            """),
            conn,
            params={"ticker": ticker, "cutoff": cutoff},
        )
    if df.empty:
        if trades:
            return {
                "ticker": ticker,
                "interval": "1d",
                "source": "quotes",
                "bars": [],
                "trades": trades,
                "position": position,
                "no_quotes": True,
            }
        return {}
    records = []
    for _, row in df.iterrows():
        d = row["date"]
        records.append({
            "date": d.isoformat() if hasattr(d, "isoformat") else str(d),
            "open": _to_jsonable(row.get("open")),
            "high": _to_jsonable(row.get("high")),
            "low": _to_jsonable(row.get("low")),
            "close": _to_jsonable(row.get("close")),
            "volume": _to_jsonable(row.get("volume")),
            "sma_5": _to_jsonable(row.get("sma_5")),
            "rsi": _to_jsonable(row.get("rsi")),
            "volatility_5": _to_jsonable(row.get("volatility_5")),
        })
    return {"ticker": ticker, "interval": "1d", "source": "quotes", "bars": records, "trades": trades, "position": position}


def _build_portfolio_daily_charts_bulk(days: int) -> Dict[str, Any]:
    """Дневные графики по всем тикерам портфельной игры (trading list), без LLM."""
    from services.portfolio_card import get_portfolio_trade_tickers

    tickers = list(get_portfolio_trade_tickers() or [])
    charts: List[Dict[str, Any]] = []
    for t in tickers:
        one = _build_portfolio_daily_chart_data(t, days)
        bars = one.get("bars") or []
        trades = one.get("trades") or []
        entry: Dict[str, Any] = {"ticker": t, "bars": bars, "trades": trades, "interval": "1d", "source": "quotes"}
        if not bars:
            entry["error"] = "no_data"
        charts.append(entry)
    return {"days": days, "tickers": tickers, "charts": charts}


def _compute_game5m_card_llm_sync(ticker: str) -> Dict[str, Any]:
    """Синхронный расчёт вывода LLM для карточки 5m (запускается в потоке, чтобы не блокировать event loop)."""
    from services.recommend_5m import get_decision_5m
    from services.cluster_recommend import (
        load_game5m_llm_correlation,
        build_cluster_note_for_5m_llm,
        get_avg_volatility_20_pct_from_quotes,
    )
    from services.llm_service import get_llm_service
    from report_generator import get_engine

    d5 = get_decision_5m(ticker, days=5, use_llm_news=True)
    if not d5:
        return {"_error": "Нет 5m данных", "_status": 404}
    corr_matrix, corr_universe, game_5m = load_game5m_llm_correlation(days=30)
    tech_by_ticker = {ticker: {"price": d5.get("price"), "rsi": d5.get("rsi_5m")}}
    for t in corr_universe:
        if t == ticker:
            continue
        try:
            d = get_decision_5m(t, days=5, use_llm_news=False)
            if d:
                tech_by_ticker[t] = {"price": d.get("price"), "rsi": d.get("rsi_5m")}
        except Exception:
            pass
    # Второй аргумент — только тикеры игры 5m (подпись и порядок в cluster_note); матрица — по полному универсу.
    cluster_note = (
        build_cluster_note_for_5m_llm(ticker, game_5m or [ticker], corr_matrix, tech_by_ticker)
        if corr_matrix
        else None
    )
    llm_reasoning = None
    llm_key_factors = None
    avg_volatility_20 = get_avg_volatility_20_pct_from_quotes(ticker)
    if cluster_note:
        try:
            llm = get_llm_service()
            if getattr(llm, "client", None):
                technical_data = {
                    "close": d5.get("price"),
                    "rsi": d5.get("rsi_5m"),
                    "volatility_5": d5.get("volatility_5m_pct"),
                    "avg_volatility_20": avg_volatility_20,
                    "technical_signal": d5.get("technical_decision_effective") or d5.get("decision"),
                    "technical_signal_core": d5.get("technical_decision_core") or d5.get("decision"),
                    "catboost_entry_proba_good": d5.get("catboost_entry_proba_good"),
                    "catboost_signal_status": d5.get("catboost_signal_status"),
                    "catboost_fusion_note": d5.get("catboost_fusion_note"),
                    "cluster_note": cluster_note,
                    "momentum_2h_pct": d5.get("momentum_2h_pct"),
                    "take_profit_pct": d5.get("take_profit_pct"),
                    "stop_loss_pct": d5.get("stop_loss_pct"),
                    "estimated_upside_pct_day": d5.get("estimated_upside_pct_day"),
                    "price_forecast_5m": d5.get("price_forecast_5m"),
                    "price_forecast_5m_summary": d5.get("price_forecast_5m_summary"),
                }
                news_list = [{"source": "KB", "content": (d5.get("kb_news_impact") or "")[:500], "sentiment_score": 0.5}]
                sentiment = 0.35 if "негатив" in (d5.get("kb_news_impact") or "").lower() else (0.65 if "позитив" in (d5.get("kb_news_impact") or "").lower() else 0.5)
                result = llm.analyze_trading_situation(
                    ticker, technical_data, news_list, sentiment,
                    strategy_name="GAME_5M",
                    strategy_signal=d5.get("technical_decision_effective") or d5.get("decision"),
                )
                if result and result.get("llm_analysis"):
                    ana = result["llm_analysis"]
                    llm_reasoning = ana.get("reasoning") or ""
                    llm_key_factors = ana.get("key_factors")
        except Exception as e:
            llm_reasoning = f"Ошибка LLM: {e!s}"
    return {
        "ticker": ticker,
        "llm_reasoning": llm_reasoning,
        "llm_key_factors": llm_key_factors,
        "technical_signal": d5.get("technical_decision_effective") or d5.get("decision"),
    }


@app.get("/api/game5m/card/{ticker}/llm", response_class=JSONResponse)
async def get_game5m_card_llm(ticker: str):
    """API: Вывод LLM по одному тикеру 5m (по запросу, аналог prompt_entry). Для кнопки в карточке. Выполняется в потоке, чтобы не блокировать сервер."""
    ticker = ticker.strip().upper()
    try:
        from services.ticker_groups import get_tickers_game_5m
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"Модули недоступны: {e}")
    if ticker not in (get_tickers_game_5m() or []):
        raise HTTPException(status_code=404, detail="Тикер не в игре 5m")
    try:
        result = await asyncio.to_thread(_compute_game5m_card_llm_sync, ticker)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка расчёта LLM: {e!s}")
    if result.get("_error"):
        raise HTTPException(status_code=result.get("_status", 404), detail=result.get("_error", "Нет данных"))
    return _to_jsonable(result)


@app.get("/game5m/cards", response_class=HTMLResponse)
async def game5m_cards_page(request: Request):
    """Страница карточек 5m для мониторинга в Telegram (компактный скролл, LLM по кнопке)."""
    return HTMLResponse(render_template("game5m_cards.html", {"request": request}))


@app.get("/api/portfolio/cards", response_class=JSONResponse)
async def get_portfolio_cards(corr_days: int = 30):
    """API: карточки портфельной игры (дневные quotes, стратегия; LLM не вызывается — см. /api/portfolio/card/{ticker}/llm)."""
    try:
        payload = await asyncio.to_thread(_compute_portfolio_cards_sync, corr_days)
    except Exception as e:
        logger.exception("GET /api/portfolio/cards corr_days=%s: %s", corr_days, e)
        raise HTTPException(status_code=500, detail=f"Ошибка карточек портфеля: {e!s}")
    try:
        body = _api_json_body(payload)
        return JSONResponse(content=body)
    except Exception as e:
        logger.exception("GET /api/portfolio/cards JSON serialize: %s", e)
        raise HTTPException(status_code=500, detail=f"Сериализация ответа: {e!s}")


@app.get("/api/portfolio/card/{ticker}/llm", response_class=JSONResponse)
async def get_portfolio_card_llm(ticker: str, corr_days: int = 30):
    """API: LLM-разбор по тикеру портфеля (корреляции, новости, техника, риск)."""
    t = ticker.strip().upper()
    try:
        result = await asyncio.to_thread(_compute_portfolio_card_llm_sync, t, corr_days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка LLM портфеля: {e!s}")
    if result.get("_error"):
        raise HTTPException(status_code=int(result.get("_status", 404)), detail=result.get("_error", "Нет данных"))
    return JSONResponse(content=_api_json_body(result))


@app.get("/api/portfolio/charts", response_class=JSONResponse)
async def get_portfolio_charts_bulk(days: int = 180):
    """Дневные графики по всем тикерам портфельной игры (MEDIUM+LONG без индикаторов)."""
    try:
        days_clamped = min(max(10, int(days)), 730)
        payload = await asyncio.to_thread(_build_portfolio_daily_charts_bulk, days_clamped)
        return JSONResponse(content=_api_json_body(payload))
    except Exception as e:
        logger.exception("GET /api/portfolio/charts days=%s: %s", days, e)
        raise HTTPException(status_code=500, detail=f"Ошибка графиков портфеля: {e!s}")


@app.get("/api/portfolio/chart/{ticker}", response_class=JSONResponse)
async def get_portfolio_chart(ticker: str, days: int = 180):
    """API: дневные бары из quotes для графика (не 5m)."""
    t = ticker.strip().upper()
    try:
        data = await asyncio.to_thread(_build_portfolio_daily_chart_data, t, days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка графика: {e!s}")
    if not data.get("bars") and not (data.get("trades") or []):
        raise HTTPException(
            status_code=404,
            detail=f"Нет дневных котировок в quotes и сделок портфеля (не GAME_5M) для {t}",
        )
    return JSONResponse(content=_api_json_body(data))


@app.get("/portfolio/cards", response_class=HTMLResponse)
async def portfolio_cards_page(request: Request):
    """Карточки портфельной игры (MEDIUM+LONG): цели стратегии / запасной тейк, дневной контекст."""
    return HTMLResponse(render_template("portfolio_cards.html", {"request": request}))


@app.get("/portfolio/daily", response_class=HTMLResponse)
async def portfolio_daily_chart_page(request: Request):
    """Дневной график по тикеру портфеля (таблица quotes)."""
    try:
        from services.portfolio_card import get_portfolio_trade_tickers

        portfolio_trade = list(get_portfolio_trade_tickers() or [])
    except Exception:
        portfolio_trade = []
    return HTMLResponse(
        render_template(
            "portfolio_daily.html",
            {
                "request": request,
                "portfolio_trade_tickers_json": json.dumps(portfolio_trade),
            },
        )
    )


@app.get("/api/pnl", response_class=JSONResponse)
async def get_pnl():
    """API: Получить PnL по закрытым сделкам для графика на странице Визуализация."""
    try:
        report_engine = get_engine()
        all_trades = load_trade_history(report_engine)
        trade_pnls = compute_closed_trade_pnls(all_trades)

        def _safe_net_pnl(t):
            v = getattr(t, "net_pnl", None)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return 0.0
            return float(v)

        pnl_list = []
        for t in trade_pnls:
            d = {k: _to_jsonable(v) for k, v in t.__dict__.items()}
            if d.get("ts") is not None and hasattr(t, "ts") and hasattr(t.ts, "isoformat"):
                d["ts"] = t.ts.isoformat()
            if d.get("entry_ts") is not None and hasattr(t, "entry_ts") and hasattr(t.entry_ts, "isoformat"):
                d["entry_ts"] = t.entry_ts.isoformat()
            d["net_pnl"] = _safe_net_pnl(t)
            pnl_list.append(d)

        total_pnl = sum(_safe_net_pnl(t) for t in trade_pnls) if trade_pnls else 0.0
        win_rate = (sum(1 for t in trade_pnls if _safe_net_pnl(t) > 0) / len(trade_pnls) * 100) if trade_pnls else 0.0

        return _to_jsonable({
            "pnl": pnl_list,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _exit_reason_caption(code: Optional[str]) -> str:
    """Краткая расшифровка signal_type при закрытии (как в close_position)."""
    if not code or not str(code).strip():
        return ""
    c = str(code).strip().upper()
    return {
        "TAKE_PROFIT": "достигнут тейк",
        "TAKE_PROFIT_SUSPEND": "тейк по алгоритму висяка (сужение)",
        "TIME_EXIT": "конец сессии или макс. дней",
        "TIME_EXIT_EARLY": "ранний de-risk выход",
        "SELL": "сигнал SELL (правила 5m)",
        "STOP_LOSS": "стоп-лосс",
    }.get(c, "")


def _closed_ts_msk(ts) -> str:
    """Дата/время для колонок Open (MSK) / Close (MSK) — как в Telegram /closed."""
    if ts is None:
        return "—"
    try:
        t = pd.Timestamp(ts)
        if t.tzinfo is not None:
            t = t.tz_convert("Europe/Moscow")
        return t.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(ts)[:16] if ts else "—"


def _normalize_closed_game_type(game_type: Optional[str]) -> str:
    raw = (game_type or "all").strip().lower().replace("-", "_")
    aliases = {
        "": "all",
        "all": "all",
        "any": "all",
        "game_5m": "game5m",
        "game5m": "game5m",
        "5m": "game5m",
        "portfolio": "portfolio",
        "trading_cycle": "portfolio",
        "manual": "manual",
    }
    return aliases.get(raw, "all")


def _closed_trade_game_type(t: Any) -> str:
    strategy = str(getattr(t, "entry_strategy", None) or "").strip().upper()
    if strategy == "GAME_5M":
        return "game5m"
    if strategy == "MANUAL":
        return "manual"
    return "portfolio"


def _game_type_label(game_type: str) -> str:
    return {
        "game5m": "GAME_5M",
        "portfolio": "Portfolio game",
        "manual": "Manual",
    }.get(game_type, "Other")


def _strategy_display_name(strategy: Optional[str]) -> str:
    s = (strategy or "").strip()
    if not s or s == "—":
        return "—"
    if s == "Portfolio":
        return "Portfolio fallback"
    return s


def _closed_report_rows(limit: Optional[int] = None, game_type: Optional[str] = None):
    """Данные для отчёта закрытых позиций (как /closed): сортировка по дате закрытия, новые сверху."""
    default_lim, web_max = get_web_closed_positions_limits()
    if limit is None:
        limit = default_lim
    limit = max(1, min(web_max, int(limit)))
    game_type_norm = _normalize_closed_game_type(game_type)
    report_engine = get_engine()
    all_trades = load_trade_history(report_engine)
    trade_pnls = compute_closed_trade_pnls(all_trades)
    if not trade_pnls:
        return []
    if game_type_norm != "all":
        trade_pnls = [t for t in trade_pnls if _closed_trade_game_type(t) == game_type_norm]
    sorted_pnls = sorted(trade_pnls, key=lambda t: pd.Timestamp(t.ts) if t.ts else pd.Timestamp.min, reverse=True)[:limit]

    rows = []
    for t in sorted_pnls:
        pts = t.exit_price - t.entry_price
        pips = round(pts * 10000) if ("=X" in t.ticker or "USD" in t.ticker or "EUR" in t.ticker) else round(pts, 2)
        open_msk = _closed_ts_msk(t.entry_ts)
        close_msk = _closed_ts_msk(t.ts)
        direction = "Long" if getattr(t, "side", "") == "SELL" else "Short"
        exit_reason = (t.signal_type or "—") if t.signal_type and str(t.signal_type).strip() else "—"
        entry_px = float(t.entry_price)
        pl_pct = ((float(t.exit_price) / entry_px) - 1.0) * 100.0 if entry_px > 0 else 0.0
        row_game_type = _closed_trade_game_type(t)
        rows.append({
            "instrument": t.ticker,
            "game": _game_type_label(row_game_type),
            "direction": direction,
            "open": entry_px,
            "close": float(t.exit_price),
            "profit_pips": pips,
            "profit_usd": float(t.net_pnl),
            "pl_pct": pl_pct,
            "units": int(t.quantity),
            "entry_strategy": _strategy_display_name(getattr(t, "entry_strategy", None)),
            "exit_strategy": _strategy_display_name(getattr(t, "exit_strategy", None)),
            "open_msk": open_msk,
            "close_msk": close_msk,
            "exit_reason": exit_reason,
            "exit_reason_caption": _exit_reason_caption(exit_reason if exit_reason != "—" else None),
            "trade_human_note": human_trade_explanation_from_exit_context(getattr(t, "exit_context_json", None)),
        })
    return rows


def _closed_exit_diagnostics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Сводка по типам выхода (для контроля источника убытков TIME_EXIT vs TAKE_PROFIT)."""
    if not rows:
        return {"by_reason": [], "time_exit_loss_share_pct": None}
    agg: Dict[str, Dict[str, Any]] = {}
    total_loss_abs = 0.0
    time_exit_loss_abs = 0.0
    for r in rows:
        reason = str(r.get("exit_reason") or "—").strip().upper() or "—"
        pnl = float(r.get("profit_usd") or 0.0)
        rec = agg.setdefault(reason, {"reason": reason, "count": 0, "pnl_usd": 0.0, "wins": 0, "losses": 0})
        rec["count"] += 1
        rec["pnl_usd"] += pnl
        if pnl > 0:
            rec["wins"] += 1
        elif pnl < 0:
            rec["losses"] += 1
            total_loss_abs += abs(pnl)
            if reason in ("TIME_EXIT", "TIME_EXIT_EARLY"):
                time_exit_loss_abs += abs(pnl)
    by_reason = sorted(agg.values(), key=lambda x: (x["count"], abs(x["pnl_usd"])), reverse=True)
    share = (100.0 * time_exit_loss_abs / total_loss_abs) if total_loss_abs > 0 else None
    return {"by_reason": by_reason, "time_exit_loss_share_pct": share}


def _build_closed_positions_xlsx(rows: List[Dict[str, Any]]) -> bytes:
    """Таблица закрытых позиций в .xlsx (те же поля, что на /reports/closed)."""
    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise ImportError("openpyxl required for Excel export") from e

    wb = Workbook()
    ws = wb.active
    ws.title = "closed"
    headers = [
        "instrument",
        "game",
        "direction",
        "open",
        "close",
        "pips",
        "qty",
        "profit_usd",
        "pl_pct",
        "entry_strategy",
        "exit_strategy",
        "exit_reason",
        "exit_reason_caption",
        "open_msk",
        "close_msk",
        "trade_note",
    ]
    ws.append(headers)
    for r in rows:
        note = (r.get("trade_human_note") or "") if isinstance(r.get("trade_human_note"), str) else ""
        if len(note) > 32000:
            note = note[:32000] + "…"
        ws.append(
            [
                r.get("instrument"),
                r.get("game"),
                r.get("direction"),
                r.get("open"),
                r.get("close"),
                r.get("profit_pips"),
                r.get("units"),
                r.get("profit_usd"),
                r.get("pl_pct"),
                r.get("entry_strategy"),
                r.get("exit_strategy"),
                r.get("exit_reason"),
                r.get("exit_reason_caption") or "",
                r.get("open_msk"),
                r.get("close_msk"),
                note,
            ]
        )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pending_report_rows(limit: int = 50):
    """Данные для отчёта открытых позиций (как /pending): Strategy, P/L по последней цене."""
    report_engine = get_engine()
    trades = load_trade_history(report_engine)
    pending = compute_open_positions(trades)[:limit]
    if not pending:
        return []
    try:
        from services.ticker_groups import get_tickers_game_5m
        tickers_in_game_5m = set(get_tickers_game_5m())
    except Exception:
        tickers_in_game_5m = set()
    latest_prices = get_latest_prices(report_engine, [p.ticker for p in pending])

    def _to_msk_str(ts):
        if ts is None:
            return "—"
        try:
            t = pd.Timestamp(ts)
            if t.tzinfo is not None:
                t = t.tz_convert("Europe/Moscow")
            return t.strftime("%d.%m.%Y %H:%M")
        except Exception:
            return str(ts)[:16] if ts else "—"

    rows = []
    for p in pending:
        raw_strat = (p.strategy_name or "—").strip() or "—"
        if raw_strat == "GAME_5M":
            game = "GAME_5M" if p.ticker in tickers_in_game_5m else "5m вне"
            strat = "—"
        elif raw_strat == "Manual":
            game = "Manual"
            strat = "—"
        else:
            game = "Portfolio game"
            strat = _strategy_display_name(raw_strat)
        now_price = latest_prices.get(p.ticker)
        if now_price is not None and p.entry_price and p.entry_price > 0:
            pct = (now_price - p.entry_price) / p.entry_price * 100.0
            usd = (now_price - p.entry_price) * p.quantity
            pl_str = f"{pct:+.1f}% {usd:+.0f}$"
        else:
            pl_str = "—"
            now_price = None
        open_msk = _to_msk_str(p.entry_ts)
        rows.append({
            "instrument": p.ticker,
            "direction": "Long",
            "open": float(p.entry_price),
            "now": f"{now_price:.2f}" if now_price is not None else "—",
            "units": int(p.quantity),
            "pl": pl_str,
            "game": game,
            "strategy": strat,
            "open_msk": open_msk,
            "buy_legs": int(getattr(p, "buy_leg_count", 1) or 1),
        })
    return rows


@app.get("/reports/closed", response_class=HTMLResponse)
async def report_closed(request: Request, limit: Optional[int] = None, game_type: str = "all"):
    """HTML-отчёт: закрытые позиции. Веб-потолок — get_web_closed_positions_limits() (опц. WEB_CLOSED_REPORT_MAX)."""
    _def_l, _tg_max = get_closed_positions_report_limits()
    _, web_max = get_web_closed_positions_limits()
    game_type_norm = _normalize_closed_game_type(game_type)
    if limit is None:
        limit = _def_l
    else:
        limit = max(1, min(web_max, int(limit)))
    rows = _closed_report_rows(limit=limit, game_type=game_type_norm)
    total_pnl = sum(float(r["profit_usd"]) for r in rows) if rows else 0.0
    diagnostics = _closed_exit_diagnostics(rows)
    preset_candidates = (25, 50, 100, 200, 500, 1000, web_max)
    closed_limit_presets = sorted({n for n in preset_candidates if 1 <= n <= web_max})
    return HTMLResponse(
        render_template(
            "reports_closed.html",
            {
                "request": request,
                "rows": rows,
                "limit": limit,
                "closed_report_default": _def_l,
                "closed_report_web_max": web_max,
                "closed_report_telegram_max": _tg_max,
                "closed_limit_presets": closed_limit_presets,
                "game_type": game_type_norm,
                "closed_game_type_options": [
                    {"value": "all", "label": "Все игры"},
                    {"value": "game5m", "label": "GAME_5M"},
                    {"value": "portfolio", "label": "Portfolio game"},
                    {"value": "manual", "label": "Manual"},
                ],
                "total_pnl": total_pnl,
                "total_count": len(rows),
                "diagnostics": diagnostics,
            },
        )
    )


@app.get("/reports/closed/export")
async def report_closed_export(limit: Optional[int] = None, game_type: str = "all"):
    """Выгрузка закрытых позиций в Excel (.xlsx). Потолок строк — как у /reports/closed (WEB_CLOSED_REPORT_MAX / Telegram)."""
    _def_l, web_max = get_web_closed_positions_limits()
    game_type_norm = _normalize_closed_game_type(game_type)
    if limit is None:
        limit = _def_l
    else:
        limit = max(1, min(web_max, int(limit)))
    rows = _closed_report_rows(limit=limit, game_type=game_type_norm)
    try:
        payload = _build_closed_positions_xlsx(rows)
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="Для выгрузки Excel установите пакет: pip install openpyxl",
        )
    fname = f"closed_positions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/reports/pending", response_class=HTMLResponse)
async def report_pending(request: Request, limit: int = 50):
    """HTML-отчёт: открытые позиции (аналог /pending в Telegram)."""
    limit = max(1, min(500, limit))
    rows = _pending_report_rows(limit=limit)
    return HTMLResponse(render_template("reports_pending.html", {"request": request, "rows": rows, "limit": limit}))


# Каталог логов (на хосте монтируется в контейнер как ./logs -> /app/logs)
LOGS_DIR = Path(__file__).resolve().parent / "logs"
CRON_LOG_FILES = [
    "cron_update_prices.log",
    "cron_trading_cycle.log",
    "cron_sndk_signal.log",
    "premarket_cron.log",
    "news_fetch.log",
    "sync_vector_kb.log",
    "add_sentiment_to_news.log",
    "analyze_event_outcomes.log",
    "cron_watchdog.log",
]
TAIL_LINES = 80
ERROR_RE = re.compile(r"\b(ERROR|CRITICAL|Exception|Traceback|failed|ConnectionError|ImportError)\b", re.IGNORECASE)
WARNING_RE = re.compile(r"\bWARNING\b", re.IGNORECASE)


def _read_tail(path: Path, n: int) -> List[str]:
    """Последние n строк файла (без загрузки всего в память для больших файлов)."""
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [s.rstrip("\n\r") for s in lines[-n:]]
    except OSError:
        return []


def _gather_service_status() -> Dict[str, Any]:
    """Собирает статус БД, новостей, хвосты логов cron и вывод watchdog для страницы /service."""
    out = {
        "db_ok": False,
        "db_error": None,
        "quotes_last_date": None,
        "trades_24h": 0,
        "news_7d": 0,
        "logs_available": False,
        "log_tails": [],
        "watchdog_lines": [],
        "hints": [],
    }
    # БД
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            out["db_ok"] = True
            row = conn.execute(
                text("SELECT MAX(date)::text FROM quotes")
            ).fetchone()
            if row and row[0]:
                out["quotes_last_date"] = str(row[0])[:10]
            since = (datetime.now(timezone.utc) - timedelta(hours=24)).replace(tzinfo=None)
            row = conn.execute(
                text("SELECT COUNT(*) FROM trade_history WHERE ts >= :since"),
                {"since": since}
            ).fetchone()
            out["trades_24h"] = int(row[0]) if row and row[0] is not None else 0
            row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM knowledge_base WHERE COALESCE(ingested_at, ts) >= CURRENT_DATE - INTERVAL '7 days'"
                )
            ).fetchone()
            out["news_7d"] = int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        out["db_error"] = str(e)
        out["hints"].append("Проверьте DATABASE_URL и доступность PostgreSQL (docker compose ps, логи lse-postgres).")

    if not out["db_ok"]:
        return out

    if out["news_7d"] == 0:
        out["hints"].append("Нет новостей за 7 дней: проверьте fetch_news_cron (logs/news_fetch.log), API ключи новостей в config.env.")
    if out["quotes_last_date"] is None:
        out["hints"].append("Нет котировок в quotes: запустите update_prices_cron (logs/cron_update_prices.log).")

    # Логи
    if LOGS_DIR.is_dir():
        out["logs_available"] = True
        for log_name in CRON_LOG_FILES:
            path = LOGS_DIR / log_name
            lines = _read_tail(path, TAIL_LINES)
            if not lines:
                continue
            classified = []
            for line in lines:
                line = line.rstrip()
                if not line.strip():
                    classified.append(("", line))
                elif ERROR_RE.search(line):
                    classified.append(("error", line))
                elif WARNING_RE.search(line):
                    classified.append(("warn", line))
                else:
                    classified.append(("info", line))
            out["log_tails"].append({
                "name": log_name,
                "lines": classified,
            })
        watchdog_path = LOGS_DIR / "cron_watchdog.log"
        out["watchdog_lines"] = _read_tail(watchdog_path, 100)
    else:
        out["hints"].append("Каталог логов не найден: смонтируйте ./logs в контейнер (volumes: ./logs:/app/logs:ro) для просмотра логов cron.")

    return out


@app.get("/api/service/status", response_class=JSONResponse)
async def get_service_status():
    """API: статус БД, новостей и хвосты логов cron для диагностики."""
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _gather_service_status)
        return _to_jsonable(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/service", response_class=HTMLResponse)
async def service_page(request: Request):
    """Страница диагностики: БД, новости, логи cron, watchdog — для поиска проблем и правки сервиса."""
    try:
        loop = asyncio.get_running_loop()
        status = await loop.run_in_executor(None, _gather_service_status)
    except Exception as e:
        status = {
            "db_ok": False,
            "db_error": str(e),
            "quotes_last_date": None,
            "trades_24h": 0,
            "news_7d": 0,
            "logs_available": False,
            "log_tails": [],
            "watchdog_lines": [],
            "hints": ["Ошибка сбора статуса: " + str(e)],
        }
    return HTMLResponse(render_template("service.html", {"request": request, "status": status}))


@app.get("/parameters", response_class=HTMLResponse)
async def parameters_page(request: Request):
    """Страница редактирования config.env"""
    return HTMLResponse(render_template("parameters.html", {"request": request}))


@app.get("/api/config/env", response_class=JSONResponse)
async def get_config_env_api():
    """API: список редактируемых ключей config.env и их текущие значения (секреты маскируются)."""
    try:
        config = load_config()
        out = []
        for key in get_editable_config_keys_expanded():
            value = config.get(key, "")
            masked = key in ("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "OPENAI_GPT_KEY", "NEWSAPI_KEY", "ALPHAVANTAGE_KEY", "DATABASE_URL")
            out.append({
                "key": key,
                "value": value if value else "",
                "masked": masked,
            })
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config/env", response_class=JSONResponse)
async def update_config_env_api(key: str = Form(...), value: str = Form(...)):
    """API: обновить ключ в config.env (файл модифицируется на диске)."""
    key = (key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    if not is_editable_config_env_key(key):
        raise HTTPException(status_code=400, detail=f"Key {key!r} is not editable")
    try:
        ok = update_config_key(key, value)
        if not ok:
            raise HTTPException(status_code=500, detail="Could not write config.env (file missing or read-only)")
        return {"status": "success", "message": "Сохранено в config.env"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config/restart", response_class=JSONResponse)
async def restart_service_api():
    """
    Пытается перезапустить сервис (docker compose restart lse или команда из RESTART_CMD в config.env).
    Если команда не задана или выполнение невозможно — возвращает подсказку для ручного перезапуска на сервере.
    """
    import subprocess
    config = load_config()
    try:
        result, used_cmd = _run_restart_resolved(config)
        if result.returncode != 0:
            # Если docker/docker-compose недоступны (127), а мы внутри контейнера — делаем self-restart.
            if result.returncode == 127:
                _schedule_self_restart(delay_sec=1.0)
                return _to_jsonable(
                    {
                        "ok": True,
                        "message": "Docker CLI недоступен — выполняю self-restart контейнера (перечитает config.env).",
                        "command": used_cmd[:200],
                        "mode": "self_restart",
                    }
                )
            return _to_jsonable(_restart_result_from_completed(result, used_cmd))
        return _to_jsonable(
            {"ok": True, "message": "Перезапуск выполнен", "command": used_cmd[:200]}
        )
    except FileNotFoundError:
        return _to_jsonable({"ok": False, "message": "Выполните на сервере: docker compose restart lse"})
    except subprocess.TimeoutExpired:
        return _to_jsonable({"ok": False, "message": "Таймаут. Проверьте на сервере: docker ps"})
    except Exception as e:
        return _to_jsonable({"ok": False, "message": f"Ошибка: {e!s}. Выполните на сервере: docker compose restart lse"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

