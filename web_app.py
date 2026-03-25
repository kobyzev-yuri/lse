"""
Веб-интерфейс для LSE Trading System
FastAPI: портфель, отчёты 5m, база знаний, графики, параметры, сервис.
"""

import asyncio
import math
import os
import json
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
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
    load_config,
    get_config_file_path,
    update_config_key,
    get_editable_config_keys_expanded,
    is_editable_config_env_key,
)
from execution_agent import ExecutionAgent
from services.ticker_groups import get_tickers_fast
from news_importer import add_news, get_news_sources_stats
from report_generator import compute_closed_trade_pnls, compute_open_positions, load_trade_history, get_engine, get_latest_prices

app = FastAPI(title="LSE Trading System", version="1.0.0")


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
    extra = ("/usr/local/bin", "/usr/bin", "/bin", "/snap/bin")
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


def _restart_result_from_completed(result: Any, cmd: str) -> Dict[str, Any]:
    """Унифицированный ответ API для перезапуска."""
    if result.returncode == 0:
        return {"ok": True, "message": "Перезапуск выполнен"}
    err_tail = ((result.stderr or "") + (result.stdout or ""))[:500]
    hint_127 = ""
    if result.returncode == 127:
        hint_127 = (
            " Часто причина: у процесса веба нет `docker` в PATH. "
            "В config.env задайте RESTART_CMD с полным путём, например: "
            "`RESTART_CMD=/usr/bin/docker compose -f /path/to/docker-compose.yml restart lse` "
            "(или `sudo -n /usr/bin/docker ...`, если так настроено)."
        )
    return {
        "ok": False,
        "message": f"Команда вернула код {result.returncode}.{hint_127} Выполните на сервере вручную при необходимости.",
        "stderr": err_tail,
        "command": cmd[:200],
    }


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
    """Главная страница с дашбордом"""
    with engine.connect() as conn:
        cash_result = conn.execute(
            text("SELECT quantity FROM portfolio_state WHERE ticker = 'CASH'")
        ).fetchone()
        cash = float(cash_result[0]) if cash_result else 0.0

        # Открытые позиции: из trade_history (как /pending), чтобы отображались позиции game_5m и др.
        report_engine = get_engine()
        all_trades = load_trade_history(report_engine)
        open_positions_list = compute_open_positions(all_trades)
        positions = []
        for op in open_positions_list:
            ticker = op.ticker
            quantity = float(op.quantity)
            entry_price = float(op.entry_price)
            price_row = conn.execute(
                text("SELECT close FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
                {"ticker": ticker}
            ).fetchone()
            current_price = float(price_row[0]) if price_row and price_row[0] is not None else entry_price
            cost_basis = quantity * entry_price
            current_value = quantity * current_price
            pnl = current_value - cost_basis
            pnl_pct = ((current_price / entry_price) - 1) * 100 if entry_price > 0 else 0.0
            positions.append({
                "ticker": ticker,
                "quantity": quantity,
                "avg_entry_price": entry_price,
                "current_price": current_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "last_updated": _format_ts(op.entry_ts) if getattr(op, "entry_ts", None) else "—",
            })

        # Закрытые сделки — тот же источник, что и /closed: вся история, без фильтра по дате
        trade_pnls = compute_closed_trade_pnls(all_trades)
        def _safe_net_pnl(t):
            v = getattr(t, "net_pnl", None)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return 0.0
            return float(v)
        total_pnl = sum(_safe_net_pnl(t) for t in trade_pnls) if trade_pnls else 0.0
        win_rate = (sum(1 for t in trade_pnls if _safe_net_pnl(t) > 0) / len(trade_pnls) * 100) if trade_pnls else 0.0

        # Таблица закрытых позиций (как /closed): все закрытые сделки, новые сверху
        closed_positions = []
        if trade_pnls:
            def _sort_key(x):
                try:
                    ts = getattr(x, "ts", None)
                    if ts is None or (hasattr(pd, "isna") and pd.isna(ts)):
                        return pd.Timestamp.min
                    return pd.Timestamp(ts)
                except Exception:
                    return pd.Timestamp.min
            try:
                sorted_pnls = sorted(trade_pnls, key=_sort_key, reverse=True)[:50]
            except Exception:
                sorted_pnls = list(trade_pnls)[:50]
            for t in sorted_pnls:
                try:
                    pts = t.exit_price - t.entry_price
                    pips = round(pts * 10000) if "=X" in t.ticker or "USD" in t.ticker or "EUR" in t.ticker else round(pts, 2)
                    if hasattr(t, "entry_ts") and t.entry_ts is not None and hasattr(t.entry_ts, "strftime"):
                        open_msk = t.entry_ts.strftime("%d.%m.%Y %H:%M")
                    else:
                        open_msk = str(getattr(t, "entry_ts", None))[:16] if getattr(t, "entry_ts", None) else "—"
                    if hasattr(t, "ts") and t.ts is not None and hasattr(t.ts, "strftime"):
                        close_msk = t.ts.strftime("%d.%m.%Y %H:%M")
                    else:
                        close_msk = str(getattr(t, "ts", None))[:16] if getattr(t, "ts", None) else "—"
                    direction = "Long" if getattr(t, "side", "") == "SELL" else "Short"
                    raw = getattr(t, "signal_type", None)
                    if raw is None or (hasattr(pd, "isna") and pd.isna(raw)) or str(raw).strip() == "":
                        exit_reason = "—"
                    else:
                        exit_reason = str(raw).strip()
                    profit_usd = _safe_net_pnl(t)
                    ep = float(t.entry_price)
                    xp = float(t.exit_price)
                    pl_pct = ((xp / ep) - 1.0) * 100.0 if ep > 0 else 0.0
                    closed_positions.append({
                        "instrument": t.ticker,
                        "direction": direction,
                        "open": ep,
                        "close": xp,
                        "profit_pips": pips,
                        "profit_usd": profit_usd,
                        "pl_pct": pl_pct,
                        "units": int(t.quantity),
                        "open_msk": open_msk,
                        "close_msk": close_msk,
                        "exit_reason": exit_reason,
                        "exit_reason_caption": _exit_reason_caption(exit_reason if exit_reason != "—" else None),
                    })
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning("Пропуск строки закрытой позиции: %s", e)

    return HTMLResponse(render_template("index.html", {
        "request": request,
        "closed_positions": closed_positions[:20],
        "cash": cash,
        "positions": positions,
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "total_trades": len(trade_pnls) if trade_pnls else 0
    }))


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
async def get_analyzer(days: int = 7, strategy: str = "GAME_5M", use_llm: bool = False):
    """API: анализ эффективности закрытых сделок (единый код с /analyser в Telegram)."""
    try:
        from services.trade_effectiveness_analyzer import analyze_trade_effectiveness
        payload = analyze_trade_effectiveness(
            days=min(max(1, int(days)), 30),
            strategy=(strategy or "GAME_5M").strip().upper(),
            use_llm=bool(use_llm),
        )
        return _to_jsonable(payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка анализатора: {e!s}")


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
        if not is_editable_config_env_key(key):
            skipped.append({"env_key": key, "reason": "not_editable"})
            continue
        ok = update_config_key(key, proposed)
        if not ok:
            skipped.append({"env_key": key, "reason": "write_failed"})
            continue
        applied.append({"env_key": key, "proposed": proposed})

    restart_result: Dict[str, Any] = {"ok": False, "message": "Перезапуск не запрошен"}
    if do_restart and applied:
        config = load_config()
        cmd = (config.get("RESTART_CMD") or "docker compose restart lse").strip()
        if not cmd:
            restart_result = {"ok": False, "message": "Выполните на сервере: docker compose restart lse"}
        else:
            try:
                result = _run_restart_shell(cmd, Path(__file__).parent)
                if result.returncode == 0:
                    restart_result = {"ok": True, "message": "Перезапуск выполнен"}
                else:
                    restart_result = _restart_result_from_completed(result, cmd)
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
            trades_out.append({
                "ts": ts,
                "price": float(t.get("price", 0)),
                "side": t.get("side"),
                "signal_type": t.get("signal_type"),
            })
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


def _build_chart5m_data(ticker: str, days: int) -> Optional[Dict[str, Any]]:
    """Строит данные для графика 5m с пролонгацией (те же функции, что в Telegram)."""
    try:
        from services.recommend_5m import fetch_5m_ohlc, get_decision_5m
        from services.chart_prolongation import fit_and_prolong
        from services.game_5m import get_open_position, get_open_position_any, get_trades_for_chart
    except ImportError:
        return None
    try:
        df = fetch_5m_ohlc(ticker, days=days)
    except Exception:
        return None
    if df is None or df.empty or "Close" not in df.columns:
        for fallback_days in (7, 5, 2, 1):
            if fallback_days == days:
                continue
            try:
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
    try:
        from services.game_5m import trade_ts_to_et
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
            trades.append({
                "ts": ts,
                "price": float(t.get("price", 0)),
                "side": t.get("side"),
                "signal_type": t.get("signal_type"),
            })
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
                "side": t.get("side"),
                "signal_type": t.get("signal_type"),
            }
            for t in trades
        ],
        "price_forecast_5m": pf5,
    }


@app.get("/api/chart5m/{ticker}")
async def get_chart5m(ticker: str, days: int = 5):
    """API: Данные для графика 5m с зоной пролонгации (EMA, тренд при ≥5 свечах)."""
    err_404 = "Нет 5m данных: Yahoo не вернул свечи. Обычно 5m доступны в торговые часы США. Попробуйте 5 или 7 дней."
    days = min(max(1, days), 7)
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _build_chart5m_data, ticker, days)
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
    """Страница визуализации. График 5m — по всем тикерам игры (как в карточках), фильтр — только дни."""
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
        "TIME_EXIT": "конец сессии или макс. дней",
        "TIME_EXIT_EARLY": "ранний de-risk выход",
        "SELL": "сигнал SELL (правила 5m)",
        "STOP_LOSS": "стоп-лосс",
    }.get(c, "")


def _closed_report_rows(limit: int = 50):
    """Данные для отчёта закрытых позиций (как /closed): сортировка по дате закрытия, новые сверху."""
    report_engine = get_engine()
    all_trades = load_trade_history(report_engine)
    trade_pnls = compute_closed_trade_pnls(all_trades)
    if not trade_pnls:
        return []
    sorted_pnls = sorted(trade_pnls, key=lambda t: pd.Timestamp(t.ts) if t.ts else pd.Timestamp.min, reverse=True)[:limit]
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
    for t in sorted_pnls:
        pts = t.exit_price - t.entry_price
        pips = round(pts * 10000) if ("=X" in t.ticker or "USD" in t.ticker or "EUR" in t.ticker) else round(pts, 2)
        open_msk = _to_msk_str(t.entry_ts)
        close_msk = _to_msk_str(t.ts)
        direction = "Long" if getattr(t, "side", "") == "SELL" else "Short"
        exit_reason = (t.signal_type or "—") if t.signal_type and str(t.signal_type).strip() else "—"
        entry_px = float(t.entry_price)
        pl_pct = ((float(t.exit_price) / entry_px) - 1.0) * 100.0 if entry_px > 0 else 0.0
        rows.append({
            "instrument": t.ticker,
            "direction": direction,
            "open": entry_px,
            "close": float(t.exit_price),
            "profit_pips": pips,
            "profit_usd": float(t.net_pnl),
            "pl_pct": pl_pct,
            "units": int(t.quantity),
            "entry_strategy": getattr(t, "entry_strategy", None) or "—",
            "exit_strategy": getattr(t, "exit_strategy", None) or "—",
            "open_msk": open_msk,
            "close_msk": close_msk,
            "exit_reason": exit_reason,
            "exit_reason_caption": _exit_reason_caption(exit_reason if exit_reason != "—" else None),
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
        strat = (p.strategy_name or "—").strip() or "—"
        if strat == "GAME_5M" and p.ticker not in tickers_in_game_5m:
            strat = "5m вне"
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
            "strategy": strat,
            "open_msk": open_msk,
        })
    return rows


@app.get("/reports/closed", response_class=HTMLResponse)
async def report_closed(request: Request, limit: int = 50):
    """HTML-отчёт: закрытые позиции (аналог /closed в Telegram)."""
    limit = max(1, min(500, limit))
    rows = _closed_report_rows(limit=limit)
    total_pnl = sum(float(r["profit_usd"]) for r in rows) if rows else 0.0
    diagnostics = _closed_exit_diagnostics(rows)
    return HTMLResponse(
        render_template(
            "reports_closed.html",
            {
                "request": request,
                "rows": rows,
                "limit": limit,
                "total_pnl": total_pnl,
                "total_count": len(rows),
                "diagnostics": diagnostics,
            },
        )
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
    cmd = (config.get("RESTART_CMD") or "docker compose restart lse").strip()
    if not cmd:
        return _to_jsonable({"ok": False, "message": "Выполните на сервере: docker compose restart lse"})
    try:
        result = _run_restart_shell(cmd, Path(__file__).parent)
        if result.returncode != 0:
            return _to_jsonable(_restart_result_from_completed(result, cmd))
        return _to_jsonable({"ok": True, "message": "Перезапуск выполнен"})
    except FileNotFoundError:
        return _to_jsonable({"ok": False, "message": "Выполните на сервере: docker compose restart lse"})
    except subprocess.TimeoutExpired:
        return _to_jsonable({"ok": False, "message": "Таймаут. Проверьте на сервере: docker ps"})
    except Exception as e:
        return _to_jsonable({"ok": False, "message": f"Ошибка: {e!s}. Выполните на сервере: docker compose restart lse"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

