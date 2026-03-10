import logging
import numpy as np
import pandas as pd
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

class ClusterManager:
    """
    Кластеризация активов на основе исторической корреляции.
    Позволяет динамически находить режимы рынка и группы "зависимых" активов.
    """

    def __init__(self, engine=None):
        if engine is None:
            try:
                from report_generator import get_engine
                self.engine = get_engine()
            except ImportError:
                self.engine = None
        else:
            self.engine = engine

    def get_price_data(
        self,
        tickers: List[str],
        max_days: int = 100,
        min_tickers_per_row: Optional[int] = None,
    ) -> Optional[pd.DataFrame]:
        """
        Загружает матрицу цен (close) для списка тикеров из БД.
        min_tickers_per_row: если задан, оставляем строки, где заполнено не меньше этого числа тикеров
        (для смешанных кластеров акции+forex+товары даёт больше общих дней). None = требовать все тикеры (dropna(how="any")).
        """
        if not self.engine:
            logger.error("Нет подключения к БД (engine is None)")
            return None
            
        try:
            from report_generator import load_quotes
            quotes = load_quotes(self.engine, tickers)
            if quotes.empty:
                logger.warning("Нет котировок для запрошенных тикеров.")
                return None

            pt = quotes.pivot_table(index="date", columns="ticker", values="close").sort_index()
            pt = pt.tail(max(max_days, 252))
            pt = pt.replace(0, np.nan)
            if min_tickers_per_row is not None and min_tickers_per_row >= 2:
                pt = pt.dropna(thresh=min_tickers_per_row)
            else:
                pt = pt.dropna(how="any")

            if pt.shape[0] < 5:
                logger.warning("Меньше 5 общих дней с данными по котировкам, расчет невозможен.")
                return None

            return pt
        except Exception as e:
            logger.error(f"Ошибка получения цен для кластеризации: {e}")
            return None

    def get_price_data_with_fallback(
        self,
        tickers: List[str],
        max_days: int = 100,
        min_tickers_per_row: Optional[int] = None,
    ) -> Optional[pd.DataFrame]:
        """БД → при недостатке строк fallback на yfinance. Для большого кластера можно задать min_tickers_per_row."""
        if min_tickers_per_row is None and len(tickers) > 4:
            # Смешанный кластер (акции + forex + товары): оставляем строки, где есть данные хотя бы по n-2 тикерам
            min_tickers_per_row = max(2, len(tickers) - 2)
        prices = self.get_price_data(tickers, max_days, min_tickers_per_row=min_tickers_per_row)
        if prices is None or prices.shape[0] < 5:
            logger.info("Используем fallback yfinance для загрузки данных...")
            try:
                import yfinance as yf
                data = {}
                for t in tickers:
                    try:
                        hist = yf.Ticker(t).history(period=f"{max_days}d", interval="1d", auto_adjust=False)
                        if hist is not None and not hist.empty and "Close" in hist.columns:
                            data[t] = hist["Close"]
                    except Exception:
                        continue
                if len(data) >= 2:
                    df = pd.DataFrame(data).sort_index()
                    df = df.replace(0, np.nan)
                    if min_tickers_per_row is not None and min_tickers_per_row >= 2:
                        df = df.dropna(thresh=min_tickers_per_row)
                    else:
                        df = df.dropna(how="any")
                    if df.shape[0] >= 5:
                        return df
                return None
            except Exception as e:
                logger.warning(f"Ошибка fallback yfinance: {e}")
                return None
        return prices

    def calculate_log_returns(self, prices: pd.DataFrame) -> pd.DataFrame:
        """Считает лог-доходности для матрицы цен."""
        log_returns = np.log(prices / prices.shift(1)).replace([np.inf, -np.inf], np.nan).dropna(how="any")
        return log_returns

    def get_correlation_and_beta_matrix(self, tickers: List[str], days: int = 30) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Возвращает (corr_matrix, beta_matrix) за последние `days` дней.
        """
        # Запрашиваем данные с небольшим запасом для shift(1)
        prices = self.get_price_data_with_fallback(tickers, max_days=days + 10)
        
        if prices is None or prices.shape[0] < 5:
            return pd.DataFrame(), pd.DataFrame()

        log_returns = self.calculate_log_returns(prices)
        
        # Ограничиваем окно нужным количеством дней
        log_returns = log_returns.tail(days)

        if log_returns.shape[0] < 5:
            return pd.DataFrame(), pd.DataFrame()

        # Матрица корреляции
        corr_matrix = log_returns.corr()

        # Матрица Beta: Beta_i,j = Cov(i, j) / Var(j)
        # beta_matrix[j][i] будет значить Beta тикера i по отношению к бенчмарку j
        cov_matrix = log_returns.cov()
        var_series = log_returns.var()

        beta_matrix = pd.DataFrame(index=corr_matrix.index, columns=corr_matrix.columns)
        for i in beta_matrix.index:
            for j in beta_matrix.columns:
                if var_series[j] != 0:
                    beta_matrix.loc[i, j] = cov_matrix.loc[i, j] / var_series[j]
                else:
                    beta_matrix.loc[i, j] = np.nan

        return corr_matrix, beta_matrix

    def find_clusters(self, corr_matrix: pd.DataFrame, threshold: float = 0.65) -> List[Dict]:
        """
        Находит кластеры сильно скоррелированных активов.
        Алгоритм:
        1. Находит пару с максимальной корреляцией > threshold.
        2. Формирует кластер.
        3. Добавляет в кластер другие активы, у которых средняя корреляция с кластером > threshold.
        4. Повторяет, пока есть активы не в кластерах.
        Возвращает список кластеров: [{'tickers': ['A', 'B', 'C'], 'type': 'positive', 'mean_corr': ...}, ...]
        """
        clusters = []
        unassigned = set(corr_matrix.columns)
        
        # Пока есть хотя бы 2 нераспределенных тикера
        while len(unassigned) >= 2:
            # Ищем максимальную корреляцию среди unassigned
            sub_corr = corr_matrix.loc[list(unassigned), list(unassigned)]
            
            # Заменяем диагональ на NaN, чтобы не находить корреляцию 1.0 с самим собой
            np.fill_diagonal(sub_corr.values, np.nan)
            
            max_val = sub_corr.max().max()
            if np.isnan(max_val) or max_val < threshold:
                break # Нет пар, удовлетворяющих порогу
                
            # Находим тикеры этой пары
            t1, t2 = None, None
            for col in sub_corr.columns:
                for row in sub_corr.index:
                    if sub_corr.loc[row, col] == max_val:
                        t1, t2 = row, col
                        break
                if t1 is not None:
                    break
                    
            if t1 is None or t2 is None:
                break
                
            cluster_tickers = {t1, t2}
            unassigned.remove(t1)
            if t2 in unassigned:
                unassigned.remove(t2)
                
            # Пытаемся добавить другие активы в этот кластер
            added_new = True
            while added_new and unassigned:
                added_new = False
                for t in list(unassigned):
                    # Проверяем среднюю корреляцию t со всеми тикерами в кластере
                    avg_corr = corr_matrix.loc[t, list(cluster_tickers)].mean()
                    if avg_corr >= threshold:
                        cluster_tickers.add(t)
                        unassigned.remove(t)
                        added_new = True
            
            clusters.append({
                'tickers': list(cluster_tickers),
                'type': 'positive',
                'mean_corr': float(max_val) # примерная сила связи в ядре кластера
            })
            
        # Поиск негативных кластеров (парный трейдинг) -> для будущих версий
        # Можно добавить логику поиска минимальной корреляции < -threshold
        
        return clusters

    def get_market_regimes(self, days: int = 30, threshold: float = 0.65) -> Dict:
        """
        Комплексный анализ: собирает все тикеры, считает матрицы и возвращает кластеры.
        Это основная точка входа для AnalystAgent (Этап 3.2).
        """
        try:
            from services.ticker_groups import get_all_ticker_groups
            all_tickers = list(get_all_ticker_groups())
            
            if not all_tickers:
                return {"status": "error", "message": "Нет доступных тикеров."}
                
            corr, beta = self.get_correlation_and_beta_matrix(all_tickers, days=days)
            if corr.empty:
                return {"status": "error", "message": "Недостаточно данных для расчета корреляции."}
                
            clusters = self.find_clusters(corr, threshold=threshold)
            
            # Одиночные тикеры (не вошли ни в один кластер)
            clustered_tickers = set()
            for c in clusters:
                clustered_tickers.update(c['tickers'])
                
            independent = [t for t in all_tickers if t in corr.columns and t not in clustered_tickers]
            
            return {
                "status": "ok",
                "days": days,
                "clusters": clusters,
                "independent_tickers": independent,
            }
        except Exception as e:
            logger.error(f"Ошибка в get_market_regimes: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    # Простой тест при запуске напрямую
    logging.basicConfig(level=logging.INFO)
    import sys
    import os
    # Добавляем корневую директорию проекта в sys.path
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
        
    manager = ClusterManager()
    res = manager.get_market_regimes(days=30, threshold=0.5)
    print(f"Clusters: {res.get('clusters')}")
    print(f"Independent: {res.get('independent_tickers')}")
