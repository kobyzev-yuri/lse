"""
Веб-интерфейс для LSE Trading System
FastAPI приложение с визуализацией данных и управлением торговлей
"""

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

from analyst_agent import AnalystAgent
from config_loader import get_database_url
from execution_agent import ExecutionAgent
from news_importer import add_news
from report_generator import compute_closed_trade_pnls, load_trade_history, get_engine

app = FastAPI(title="LSE Trading System", version="1.0.0")

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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Главная страница с дашбордом"""
    # Получаем статистику портфеля
    with engine.connect() as conn:
        # Текущий баланс
        cash_result = conn.execute(
            text("SELECT quantity FROM portfolio_state WHERE ticker = 'CASH'")
        ).fetchone()
        cash = float(cash_result[0]) if cash_result else 0.0
        
        # Открытые позиции
        positions_df = pd.read_sql(
            text("""
                SELECT ticker, quantity, avg_entry_price, last_updated
                FROM portfolio_state
                WHERE ticker != 'CASH' AND quantity > 0
            """),
            conn
        )
        
        # Последние сделки
        trades_df = pd.read_sql(
            text("""
                SELECT ts, ticker, side, quantity, price, signal_type
                FROM trade_history
                ORDER BY ts DESC
                LIMIT 10
            """),
            conn
        )
        
        # Статистика по PnL
        all_trades = load_trade_history(engine)
        trade_pnls = compute_closed_trade_pnls(all_trades)
        
        total_pnl = sum(t.net_pnl for t in trade_pnls) if trade_pnls else 0.0
        win_rate = (sum(1 for t in trade_pnls if t.net_pnl > 0) / len(trade_pnls) * 100) if trade_pnls else 0.0
    
    return HTMLResponse(render_template("index.html", {
        "request": request,
        "cash": cash,
        "positions": positions_df.to_dict('records') if not positions_df.empty else [],
        "trades": trades_df.to_dict('records') if not trades_df.empty else [],
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "total_trades": len(trade_pnls)
    }))


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
    """API: Анализ тикера"""
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
                "llm_analysis": None
            }
        return result
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
    
    return HTMLResponse(render_template("knowledge.html", {
        "news": news_df.to_dict('records') if not news_df.empty else [],
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
    
    return {
        "trades": df.to_dict('records') if not df.empty else []
    }


@app.get("/api/pnl", response_class=JSONResponse)
async def get_pnl():
    """API: Получить PnL по закрытым сделкам"""
    try:
        all_trades = load_trade_history(engine)
        trade_pnls = compute_closed_trade_pnls(all_trades)
        
        return {
            "pnl": [t.__dict__ for t in trade_pnls],
            "total_pnl": sum(t.net_pnl for t in trade_pnls),
            "win_rate": (sum(1 for t in trade_pnls if t.net_pnl > 0) / len(trade_pnls) * 100) if trade_pnls else 0.0
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

