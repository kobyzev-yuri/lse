"""
Веб-интерфейс для LSE Trading System
FastAPI приложение с визуализацией данных и управлением торговлей
"""

import asyncio
import os
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
import pandas as pd
from sqlalchemy import create_engine, text

import numpy as np

from analyst_agent import AnalystAgent
from config_loader import get_database_url
from execution_agent import ExecutionAgent
from news_importer import add_news
from report_generator import compute_closed_trade_pnls, load_trade_history, get_engine

app = FastAPI(title="LSE Trading System", version="1.0.0")


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


def _format_ts(ts) -> str:
    """Форматирование времени для отображения в шаблонах."""
    if ts is None:
        return "—"
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%d %H:%M")
    return str(ts)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Главная страница с дашбордом"""
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

        positions = []
        for _, row in positions_df.iterrows():
            ticker = row["ticker"]
            quantity = float(row["quantity"])
            entry_price = float(row["avg_entry_price"])
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
                "last_updated": _format_ts(row.get("last_updated")),
            })

        trades_df = pd.read_sql(
            text("""
                SELECT ts, ticker, side, quantity, price, signal_type
                FROM trade_history
                ORDER BY ts DESC
                LIMIT 10
            """),
            conn
        )
        trades = trades_df.to_dict("records") if not trades_df.empty else []
        for t in trades:
            t["ts"] = _format_ts(t.get("ts"))

        all_trades = load_trade_history(engine)
        trade_pnls = compute_closed_trade_pnls(all_trades)
        total_pnl = sum(t.net_pnl for t in trade_pnls) if trade_pnls else 0.0
        win_rate = (sum(1 for t in trade_pnls if t.net_pnl > 0) / len(trade_pnls) * 100) if trade_pnls else 0.0

    return HTMLResponse(render_template("index.html", {
        "request": request,
        "cash": cash,
        "positions": positions,
        "trades": trades,
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "total_trades": len(trade_pnls)
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
    """Собирает данные для рекомендации (тот же контракт, что и в Telegram)."""
    try:
        agent = AnalystAgent(use_llm=True)
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
async def get_recommend5m(ticker: str = "SNDK", days: int = 5):
    """API: Рекомендация по 5m данным (аналог /recommend5m в Telegram)"""
    try:
        from services.recommend_5m import get_decision_5m
    except ImportError:
        raise HTTPException(status_code=501, detail="Модуль recommend_5m недоступен")
    data_5m = get_decision_5m(ticker, days=min(max(1, days), 7), use_llm_news=True)
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
    out = {
        "ticker": ticker,
        "decision": data_5m["decision"],
        "strategy": "5m (интрадей + 5д статистика)",
        "price": data_5m["price"],
        "rsi_5m": data_5m.get("rsi_5m"),
        "reasoning": data_5m.get("reasoning", ""),
        "period_str": data_5m.get("period_str", ""),
        "momentum_2h_pct": data_5m.get("momentum_2h_pct"),
        "volatility_5m_pct": data_5m.get("volatility_5m_pct"),
        "stop_loss_pct": data_5m.get("stop_loss_pct", 2.5),
        "take_profit_pct": data_5m.get("take_profit_pct", 5.0),
        "bars_count": data_5m.get("bars_count"),
        "has_position": has_position,
        "position": position_info,
        "alex_rule": alex_rule,
        "llm_insight": data_5m.get("llm_insight"),
        "llm_news_content": data_5m.get("llm_news_content"),
    }
    return _to_jsonable(out)


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
    return {"text": text, "updated_at": datetime.now().isoformat()}


@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request):
    """Страница мониторинга: дашборд по тикерам (как /dashboard в Telegram), автообновление раз в 5 мин."""
    return HTMLResponse(render_template("monitor.html", {"request": request}))


@app.get("/trading", response_class=HTMLResponse)
async def trading_page(request: Request):
    """Страница управления торговлей"""
    # Получаем список отслеживаемых тикеров
    with engine.connect() as conn:
        tickers_df = pd.read_sql(
            text("SELECT DISTINCT ticker FROM quotes ORDER BY ticker"),
            conn
        )
        tickers = tickers_df['ticker'].tolist() if not tickers_df.empty else []
    
    return HTMLResponse(render_template("trading.html", {
        "tickers": tickers
    }))


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
async def execute_trade(tickers: str = Form(...)):
    """API: Исполнить торговый цикл для тикеров"""
    try:
        ticker_list = [t.strip() for t in tickers.split(',')]
        exec_agent = ExecutionAgent()
        exec_agent.run_for_tickers(ticker_list)
        return {"status": "success", "message": f"Торговый цикл выполнен для {len(ticker_list)} тикеров"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request):
    """Страница управления базой знаний"""
    # Получаем последние новости
    with engine.connect() as conn:
        news_df = pd.read_sql(
            text("""
                SELECT id, ts, ticker, source, content, sentiment_score
                FROM knowledge_base
                ORDER BY ts DESC
                LIMIT 50
            """),
            conn
        )
        
        tickers_df = pd.read_sql(
            text("SELECT DISTINCT ticker FROM knowledge_base WHERE ticker NOT IN ('MACRO', 'US_MACRO') ORDER BY ticker"),
            conn
        )
        tickers = tickers_df['ticker'].tolist() if not tickers_df.empty else []
    
    news_list = news_df.to_dict("records") if not news_df.empty else []
    for n in news_list:
        n["ts"] = _format_ts(n.get("ts"))
    return HTMLResponse(render_template("knowledge.html", {
        "news": news_list,
        "tickers": tickers
    }))


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
                    sentiment_score = calculate_sentiment(content)
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось рассчитать sentiment: {e}")
        
        add_news(engine, ticker, source, content, sentiment_score)
        return {"status": "success", "message": "Новость добавлена", "sentiment": sentiment_score}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/visualization", response_class=HTMLResponse)
async def visualization_page(request: Request):
    """Страница визуализации данных"""
    # Получаем список тикеров
    with engine.connect() as conn:
        tickers_df = pd.read_sql(
            text("SELECT DISTINCT ticker FROM quotes ORDER BY ticker"),
            conn
        )
        tickers = tickers_df['ticker'].tolist() if not tickers_df.empty else []
    
    return HTMLResponse(render_template("visualization.html", {
        "tickers": tickers
    }))


@app.get("/api/trades", response_class=JSONResponse)
async def get_trades(limit: int = 100):
    """API: Получить историю сделок"""
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT ts, ticker, side, quantity, price, commission, 
                       signal_type, total_value, sentiment_at_trade
                FROM trade_history
                ORDER BY ts DESC
                LIMIT :limit
            """),
            conn,
            params={"limit": limit}
        )
    
    trades = df.to_dict("records") if not df.empty else []
    for t in trades:
        if t.get("ts") is not None and hasattr(t["ts"], "isoformat"):
            t["ts"] = t["ts"].isoformat()
    return {"trades": trades}


@app.get("/api/pnl", response_class=JSONResponse)
async def get_pnl():
    """API: Получить PnL по закрытым сделкам"""
    try:
        all_trades = load_trade_history(engine)
        trade_pnls = compute_closed_trade_pnls(all_trades)
        pnl_list = []
        for t in trade_pnls:
            d = dict(t.__dict__)
            if d.get("ts") is not None and hasattr(d["ts"], "isoformat"):
                d["ts"] = d["ts"].isoformat()
            pnl_list.append(d)
        return {
            "pnl": pnl_list,
            "total_pnl": sum(t.net_pnl for t in trade_pnls),
            "win_rate": (sum(1 for t in trade_pnls if t.net_pnl > 0) / len(trade_pnls) * 100) if trade_pnls else 0.0
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

