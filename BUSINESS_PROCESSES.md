# Бизнес-процессы и потоки данных LSE Trading System

> **Актуальная карта архитектуры и потоков (читать первым):** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).  
> Ниже — развёрнутые пошаговые диаграммы (Mermaid) по инициализации, котировкам, новостям, исполнению, Telegram и деплою.

Данный документ описывает основные бизнес-процессы и потоки данных системы автоматической торговли на Лондонской фондовой бирже в стандартной нотации [Mermaid](https://mermaid.js.org/), которая поддерживается в Markdown и GitHub.

## Содержание

1. [Инициализация системы и загрузка данных](#1-инициализация-системы-и-загрузка-данных)
2. [Обновление цен котировок](#2-обновление-цен-котировок)
3. [Импорт и обработка новостей](#3-импорт-и-обработка-новостей)
4. [Анализ торговых сигналов](#4-анализ-торговых-сигналов)
5. [Исполнение сделок](#5-исполнение-сделок)
6. [Управление рисками (стоп-лоссы)](#6-управление-рисками-стоп-лоссы)
7. [Генерация отчетов](#7-генерация-отчетов)
8. [Векторная база знаний](#8-векторная-база-знаний)
9. [Диаграммы взаимодействия компонентов](#9-диаграммы-взаимодействия-компонентов)
10. [Telegram бот-агент и вебхук](#10-telegram-бот-агент-и-вебхук)
11. [Развёртывание (Cloud Run и сервер БД/КБ)](#11-развёртывание-cloud-run-и-сервер-бдкб)

---

## 1. Инициализация системы и загрузка данных

### 1.1. Процесс инициализации базы данных

```mermaid
flowchart TD
    A[Запуск init_db.py] --> B[Загрузка конфигурации из config.env]
    B --> C[Подключение к PostgreSQL]
    C --> D{База lse_trading существует?}
    D -->|Нет| E[Создание базы данных]
    D -->|Да| F[Проверка расширений]
    E --> F
    F --> G{Расширение pgvector установлено?}
    G -->|Нет| H[CREATE EXTENSION vector]
    G -->|Да| I[Создание таблиц]
    H --> I
    
    I --> J[quotes: котировки с метриками]
    I --> L[knowledge_base: новости, sentiment, embedding, outcome_json]
    I --> M[portfolio_state: состояние портфеля]
    I --> N[trade_history: история сделок]
    
    J --> O[Инициализация стартового капитала]
    L --> O
    M --> O
    N --> O
    
    O --> P[Загрузка исторических данных через yfinance]
    P --> Q[Расчет SMA_5 и volatility_5]
    Q --> R[Сохранение в quotes]
    R --> S[✅ Система готова к работе]
    
    style A fill:#e1f5ff
    style S fill:#c8e6c9
    style I fill:#fff9c4
```

**Комментарий**: Процесс инициализации создает структуру базы данных и загружает начальные исторические данные. Расширение `pgvector` необходимо для работы с векторными embeddings в будущем. Стартовый капитал устанавливается в 100,000 USD.

### 1.2. Загрузка исторических данных

```mermaid
sequenceDiagram
    participant Script as init_db.py
    participant YFinance as yfinance API
    participant Pandas as pandas
    participant DB as PostgreSQL
    
    Script->>YFinance: Запрос данных (period="2y", interval="1d")
    YFinance-->>Script: DataFrame с котировками
    
    Script->>Pandas: Расчет SMA_5 и volatility_5
    Pandas-->>Script: DataFrame с метриками
    
    Script->>DB: INSERT INTO quotes (date, ticker, close, volume, sma_5, volatility_5)
    DB-->>Script: Подтверждение вставки
    
    Note over Script,DB: Для каждого тикера: MSFT, SNDK, GBPUSD=X
```

**Комментарий**: Используется библиотека `yfinance` для получения исторических данных. Метрики SMA (Simple Moving Average) и волатильность рассчитываются через pandas и сохраняются вместе с котировками для последующего технического анализа.

---

## 2. Обновление цен котировок

### 2.1. Процесс обновления цен

```mermaid
flowchart TD
    A[Запуск update_prices.py] --> B[Получение списка отслеживаемых тикеров]
    B --> C{Тикеры указаны в аргументах?}
    C -->|Да| D[Использовать указанные тикеры]
    C -->|Нет| E[Загрузить все тикеры из quotes]
    D --> F[Для каждого тикера]
    E --> F
    
    F --> G[Получить дату последнего обновления]
    G --> H{Данные есть в БД?}
    H -->|Да| I[Загрузить данные с последней даты + 1 день]
    H -->|Нет| J[Загрузить данные за последние 30 дней]
    
    I --> K[yfinance.download]
    J --> K
    K --> L{Данные получены?}
    L -->|Нет| M[⚠️ Пропустить тикер]
    L -->|Да| N[Расчет SMA_5 и volatility_5]
    
    N --> O[Фильтрация новых данных]
    O --> P{Есть новые данные?}
    P -->|Нет| Q[✅ Данные актуальны]
    P -->|Да| R[Вставка в quotes с ON CONFLICT DO NOTHING]
    
    R --> S[✅ Обновлено N записей]
    M --> T[Следующий тикер]
    Q --> T
    S --> T
    T --> U{Есть еще тикеры?}
    U -->|Да| F
    U -->|Нет| V[✅ Обновление завершено]
    
    style A fill:#e1f5ff
    style V fill:#c8e6c9
    style K fill:#fff9c4
```

**Комментарий**: Процесс обновления цен можно запускать вручную или через cron для автоматического обновления. Скрипт умно определяет, какие данные нужно загрузить, чтобы не дублировать записи. Используется `ON CONFLICT DO NOTHING` для идемпотентности.

### 2.2. Автоматическое обновление через cron

```mermaid
flowchart LR
    A[Cron: ежедневно в 18:00] --> B[Запуск update_prices.py]
    B --> C[Обновление всех тикеров]
    C --> D[Логирование результатов]
    D --> E{Ошибки?}
    E -->|Да| F[Отправка уведомления]
    E -->|Нет| G[✅ Успешное обновление]
    
    style A fill:#e1f5ff
    style G fill:#c8e6c9
```

**Комментарий**: Рекомендуется настроить cron для автоматического обновления цен после закрытия торговой сессии. Это обеспечивает актуальность данных для следующего торгового дня.

---

## 3. Импорт и обработка новостей

### 3.1. Процесс добавления новостей

```mermaid
flowchart TD
    A[Источник новости] --> B{Тип источника}
    B -->|Интерактивный ввод| C[news_importer.py add]
    B -->|CSV файл| D[news_importer.py import file.csv]
    B -->|JSON файл| E[news_importer.py import file.json]
    B -->|API интеграция| F[Будущая интеграция]
    
    C --> G[Ввод данных через CLI]
    D --> H[Парсинг CSV]
    E --> I[Парсинг JSON]
    
    G --> J[Валидация данных]
    H --> J
    I --> J
    
    J --> K{Данные валидны?}
    K -->|Нет| L[❌ Ошибка валидации]
    K -->|Да| M[Определение тикера]
    
    M --> N{Тип тикера}
    N -->|Обычный тикер| O[MSFT, SNDK и т.д.]
    N -->|Макро-событие| P[MACRO или US_MACRO]
    
    O --> Q[INSERT INTO knowledge_base]
    P --> Q
    
    Q --> R[Сохранение: ticker, source, content, sentiment_score, ts]
    R --> S[✅ Новость добавлена]
    
    style A fill:#e1f5ff
    style S fill:#c8e6c9
    style Q fill:#fff9c4
```

**Комментарий**: Новости добавляются в таблицу `knowledge_base` с sentiment score. Макро-события (MACRO, US_MACRO) имеют больший временной лаг влияния (72 часа) по сравнению с обычными новостями (24 часа). Sentiment score может быть указан вручную или рассчитан автоматически в будущем.

### 3.2. Временной лаг для разных типов новостей

```mermaid
stateDiagram-v2
    [*] --> Новость_поступила: Новость добавлена
    Новость_поступила --> Определение_типа: Анализ тикера
    
    Определение_типа --> Макро_новость: ticker = MACRO/US_MACRO
    Определение_типа --> Обычная_новость: ticker = конкретный тикер
    
    Макро_новость --> Активна_72ч: Влияние 72 часа
    Обычная_новость --> Активна_24ч: Влияние 24 часа
    
    Активна_72ч --> Устарела: Прошло 72 часа
    Активна_24ч --> Устарела: Прошло 24 часа
    
    Устарела --> [*]: Не используется в анализе
```

**Комментарий**: Макро-события (например, данные по инфляции, процентным ставкам) имеют более длительное влияние на рынок, поэтому учитываются в течение 72 часов. Новости по конкретным компаниям обычно влияют быстрее, поэтому временной лаг составляет 24 часа.

### 3.3. Новостной сигнал, горизонты и политика (целевой контур)

Агрегированный **новостной сигнал** (этап A), затем **fusion / арбитраж** с техникой и корреляциями (этап B), опциональные LLM, кэш по бэтчам и горизонты 1D / 3D / 5D — описаны в отдельном документе, чтобы не дублировать здесь устаревающие детали: [docs/NEWS_SIGNAL_ARCHITECTURE.md](docs/NEWS_SIGNAL_ARCHITECTURE.md).

---

## 4. Анализ торговых сигналов

### 4.1. Процесс принятия решения AnalystAgent

```mermaid
flowchart TD
    A[AnalystAgent.get_decision] --> B[ШАГ 1: Технический анализ]
    B --> C[Загрузка последних 5 дней котировок]
    C --> D[Расчет средней волатильности за 20 дней]
    D --> E{Условия технического сигнала}
    
    E -->|close > sma_5 AND volatility_5 < avg_vol_20| F[Сигнал: BUY]
    E -->|Иначе| G[Сигнал: HOLD]
    
    F --> H[ШАГ 2: Анализ новостей]
    G --> H
    
    H --> I[Поиск новостей за период]
    I --> J{Тип новостей}
    J -->|Макро-события| K[Период: 72 часа]
    J -->|Обычные новости| L[Период: 24 часа]
    
    K --> M[Расчет взвешенного sentiment]
    L --> M
    
    M --> N{Новости найдены?}
    N -->|Да| O[Взвешивание: новости с упоминанием тикера = 2.0, макро = 1.0]
    N -->|Нет| P[Sentiment = 0.0]
    
    O --> Q[Нормализация sentiment: 0.0-1.0 -> -1.0 до 1.0]
    P --> Q
    
    Q --> R[ШАГ 3: Выбор стратегии через Strategy Manager]
    
    R --> S{Анализ режима рынка}
    S -->|Высокая волатильность + гэп/sentiment| T[VolatileGapStrategy]
    S -->|Низкая волатильность + позитив| U[MomentumStrategy]
    S -->|Высокая волатильность + нейтрал| V[MeanReversionStrategy]
    S -->|Не подходит| W[Fallback: базовая логика]
    
    T --> X[Расчет сигнала стратегии]
    U --> X
    V --> X
    W --> X
    
    X --> Y[ШАГ 4: LLM анализ опционально]
    Y --> Z{LLM доступен?}
    Z -->|Да| AA[LLM guidance и детальный анализ]
    Z -->|Нет| AB[Использование сигнала стратегии]
    
    AA --> AC{Финальное решение}
    AB --> AC
    
    AC -->|BUY + Положительный sentiment| AD[STRONG_BUY]
    AC -->|BUY + Нейтральный sentiment| AE[BUY]
    AC -->|HOLD| AF[HOLD]
    
    AD --> AG[✅ Решение принято]
    AE --> AG
    AF --> AG
    
    style A fill:#e1f5ff
    style Y fill:#c8e6c9
    style M fill:#fff9c4
```

**Комментарий**: Процесс принятия решения комбинирует технический анализ (тренд и волатильность) с анализом новостей (sentiment). Взвешенный sentiment нормализуется в центрированную шкалу (-1.0 до 1.0) для удобства математических операций. Strategy Manager автоматически выбирает оптимальную стратегию на основе режима рынка (волатильность, sentiment, гэпы). LLM анализ (опционально) предоставляет дополнительное обоснование и рекомендации.

### 4.2. Расчет взвешенного sentiment

```mermaid
flowchart LR
    A[Новости для тикера] --> B{Проверка каждой новости}
    B --> C{Тикер упоминается в контенте?}
    C -->|Да| D[Weight = 2.0]
    C -->|Нет| E[Weight = 1.0]
    
    D --> F[Взвешенная сумма]
    E --> F
    F --> G[weighted_sum = сумма sentiment умножить weight]
    F --> H[total_weight = сумма weight]
    
    G --> I[weighted_sentiment = weighted_sum разделить total_weight]
    H --> I
    I --> J[✅ Взвешенный sentiment]
    
    style A fill:#e1f5ff
    style J fill:#c8e6c9
    style I fill:#fff9c4
```

**Комментарий**: Взвешивание позволяет учитывать, что новости, напрямую упоминающие тикер, более релевантны, чем общие макро-новости. Это улучшает качество sentiment анализа.

---

## 5. Исполнение сделок

### 5.1. Процесс исполнения сделок ExecutionAgent

```mermaid
flowchart TD
    A[ExecutionAgent.run_for_tickers] --> B[Для каждого тикера]
    B --> C[Получение сигнала от AnalystAgent]
    C --> D{Тип сигнала}
    
    D -->|BUY или STRONG_BUY| E{Позиция уже открыта?}
    D -->|HOLD| F[Пропуск покупки]
    
    E -->|Да| G[Пропуск: позиция уже есть]
    E -->|Нет| H[Проверка доступного кэша]
    
    H --> I{Кэш достаточен?}
    I -->|Нет| J[⚠️ Недостаточно средств]
    I -->|Да| K[Расчет размера позиции: 10% от кэша]
    
    K --> L[Расчет количества: floor allocation разделить price]
    L --> M[Расчет комиссии: notional умножить 0.1 процента]
    M --> N[Проверка: total_cost <= cash]
    
    N -->|Нет| O[⚠️ Недостаточно средств с учетом комиссии]
    N -->|Да| P[Получение sentiment для записи]
    
    P --> Q[Обновление кэша: cash - total_cost]
    Q --> R[Добавление/обновление позиции в portfolio_state]
    R --> S[Запись сделки в trade_history]
    
    S --> T[✅ Покупка выполнена]
    F --> U[Проверка стоп-лоссов]
    G --> U
    J --> U
    O --> U
    T --> U
    
    U --> V{Есть еще тикеры?}
    V -->|Да| B
    V -->|Нет| W[✅ Исполнение завершено]
    
    style A fill:#e1f5ff
    style W fill:#c8e6c9
    style S fill:#fff9c4
```

**Комментарий**: ExecutionAgent использует консервативный подход: размер позиции ограничен 10% от доступного кэша, что позволяет диверсифицировать портфель. Комиссия учитывается при расчете стоимости сделки (0.1% от номинала). Все сделки записываются в `trade_history` с сохранением sentiment на момент сделки для последующего анализа.

### 5.2. Управление позициями

```mermaid
sequenceDiagram
    participant EA as ExecutionAgent
    participant DB as PostgreSQL
    participant Portfolio as portfolio_state
    
    EA->>DB: Проверка открытых позиций
    DB-->>EA: Список позиций (ticker, quantity, avg_entry_price)
    
    EA->>EA: Расчет текущей стоимости позиций
    EA->>EA: Проверка лимитов диверсификации
    
    Note over EA,Portfolio: При покупке: обновление avg_entry_price через средневзвешенную формулу
    EA->>Portfolio: INSERT/UPDATE позиции
    Portfolio-->>EA: Подтверждение
    
    Note over EA,Portfolio: avg_entry_price = old_qty умножить old_price плюс new_qty умножить new_price разделить на old_qty плюс new_qty
```

**Комментарий**: Система использует средневзвешенную цену входа для расчета средней стоимости позиции при добавлении новых лотов. Это важно для правильного расчета PnL при частичном закрытии позиции.

---

## 6. Управление рисками (стоп-лоссы)

### 6.1. Процесс проверки стоп-лоссов

```mermaid
flowchart TD
    A[check_stop_losses] --> B[Загрузка всех открытых позиций]
    B --> C{Есть открытые позиции?}
    C -->|Нет| D[✅ Нет позиций для проверки]
    C -->|Да| E[Для каждой позиции]
    
    E --> F[Получение текущей цены]
    F --> G[Расчет лог-доходности: log current_price разделить entry_price]
    G --> H[Порог стоп-лосса: log 0.95 примерно -0.0513]
    
    H --> I{log_return <= threshold?}
    I -->|Да| J[🛑 Стоп-лосс сработал]
    I -->|Нет| K[✅ Позиция в безопасности]
    
    J --> L[Расчет параметров продажи]
    L --> M[Получение sentiment]
    M --> N[Обновление кэша: cash + proceeds - commission]
    N --> O[Удаление позиции из portfolio_state]
    O --> P[Запись SELL в trade_history]
    P --> Q[✅ Позиция закрыта по стоп-лоссу]
    
    K --> R{Есть еще позиции?}
    R -->|Да| E
    R -->|Нет| S[✅ Проверка завершена]
    
    style A fill:#e1f5ff
    style Q fill:#c8e6c9
    style J fill:#ffcccc
```

**Комментарий**: Стоп-лосс установлен на уровне 5% падения от цены входа (используется лог-доходность для корректного расчета). Это защищает от больших потерь при неблагоприятном движении цены. Лог-доходность используется вместо простого процента для более точного расчета, особенно при больших изменениях цены.

### 6.2. Расчет лог-доходности

```mermaid
flowchart LR
    A[entry_price] --> B[Текущая цена]
    B --> C[Расчет: log current разделить entry]
    C --> D{log_return меньше или равно -0.0513?}
    D -->|Да| E[🛑 Стоп-лосс]
    D -->|Нет| F[✅ Позиция держится]
    
    style E fill:#ffcccc
    style F fill:#c8e6c9
```

**Комментарий**: Использование лог-доходности (log-returns) является стандартной практикой в финансовых расчетах, так как она обладает свойством аддитивности и лучше отражает реальную доходность при больших изменениях цены.

---

## 7. Генерация отчетов

### 7.1. Процесс генерации отчетов

```mermaid
flowchart TD
    A[report_generator.py] --> B[Загрузка истории сделок из trade_history]
    B --> C{Есть сделки?}
    C -->|Нет| D[ℹ️ Нет данных для отчета]
    C -->|Да| E[Построение PnL по закрытым сделкам]
    
    E --> F[Для каждой SELL сделки]
    F --> G[Поиск соответствующей BUY сделки]
    G --> H[Расчет средней цены входа]
    H --> I[Расчет gross_pnl и net_pnl]
    I --> J[Расчет лог-доходности]
    
    J --> K[Агрегация результатов]
    K --> L[Расчет Win Rate]
    L --> M[Анализ корреляций с GBPUSD=X]
    M --> N[Анализ влияния sentiment на PnL]
    
    N --> O[Вывод отчета]
    O --> P[PnL по каждой сделке]
    O --> Q[Общий Win Rate]
    O --> R[Корреляции с валютными парами]
    O --> S[Влияние sentiment на результаты]
    
    style A fill:#e1f5ff
    style O fill:#c8e6c9
    style E fill:#fff9c4
```

**Комментарий**: Отчеты позволяют анализировать эффективность торговой стратегии. Win Rate показывает процент прибыльных сделок, корреляции помогают понять влияние валютных факторов, а анализ sentiment показывает, насколько хорошо sentiment анализ предсказывает результаты сделок.

### 7.2. Расчет PnL по закрытым сделкам

```mermaid
sequenceDiagram
    participant RG as report_generator
    participant DB as PostgreSQL
    participant Calc as Расчет PnL
    
    RG->>DB: SELECT * FROM trade_history ORDER BY ts
    DB-->>RG: Все сделки
    
    RG->>Calc: Построение позиций по тикерам
    Note over Calc: Для каждого тикера: отслеживание quantity и cost_basis
    
    RG->>Calc: Обработка BUY сделок
    Note over Calc: position_qty += qty<br/>position_cost += qty × price + commission
    
    RG->>Calc: Обработка SELL сделок
    Note over Calc: avg_entry = position_cost / position_qty<br/>gross_pnl = qty × (price - avg_entry)<br/>net_pnl = proceeds - cost_for_sold<br/>log_return = log(price / avg_entry)
    
    Calc-->>RG: Список TradePnL объектов
    RG->>RG: Расчет Win Rate и метрик
```

**Комментарий**: Расчет PnL использует модель средневзвешенной цены входа (FIFO-подобный подход). Это позволяет корректно рассчитывать прибыль/убыток даже при частичном закрытии позиций.

### 7.3. Анализатор эффективности сделок и автотюнинг (dataflow по времени)

Ниже — бизнес-процесс анализатора как **временной цикл**: торговля → пост‑анализ → (опционально) применение параметров → ожидание эффекта → повтор.

```mermaid
sequenceDiagram
    autonumber
    participant Cron5m as send_sndk_signal_cron (*/N min)
    participant DB as PostgreSQL (trade_history)
    participant Analyzer as /api/analyzer (trade_effectiveness_analyzer)
    participant Snapshot as snapshot_analyzer_report.py (опц.)
    participant Tune as apply-config / analyzer_tune_apply.py
    participant Autotune as analyzer_autotune.py (v0, опц.)
    participant Restart as Restart (RESTART_CMD / manual)

    Note over Cron5m,DB: Шаг 0 (в течение дня): сделки + context_json на входе/выходе
    Cron5m->>DB: INSERT BUY/SELL (strategy_name=GAME_5M)\n+ context_json (entry/exit snapshot)

    Note over Analyzer,DB: Шаг 1 (раз в день / по запросу): пост‑анализ окна 1–30 дней
    Analyzer->>DB: load closed trades (days, strategy)
    Analyzer->>Analyzer: fetch 5m OHLC per trade window\ncompute missed_upside/avoidable_loss\naggregate summary + top_cases
    Analyzer-->>Analyzer: auto_config_override (whitelisted GAME_5M_*)\n(optional llm)

    alt Снимок отчёта (рекомендуется)
        Snapshot->>Analyzer: GET /api/analyzer (HTTP)\nили локальный импорт
        Snapshot-->>Snapshot: write analyzer_*.json + latest.json
    end

    alt Ручной тюнинг (1 шаг)
        Tune->>DB: (опц.) ничего, только читает JSON
        Tune->>Tune: выбрать 1 update из auto_config_override
        Tune->>Tune: update_config_key(key=value) → config.env
        Tune->>Restart: restart service
    end

    alt Автотюнинг v0 (эволюционный, опц.)
        Autotune->>Analyzer: GET /api/analyzer (HTTP) или latest.json
        Autotune->>Autotune: pick 1 candidate by guardrails\npersist pending baseline in autotune_state.json
        Autotune->>Tune: (если ANALYZER_AUTOTUNE_APPLY=1) update_config_key → config.env
        Autotune->>Restart: restart service (v0: вручную/RESTART_CMD)
        Note over Autotune: Следующие запуски: наблюдение пока\nнаберётся ANALYZER_AUTOTUNE_MIN_TRADES новых сделок
    end

    Note over Analyzer: Шаг 6–7: повторный прогон → сравнение с прошлым snapshot\n(meta.previous_game_5m_config_snapshot + config_delta)
```

**Комментарий**:
- Анализатор — **постфактум** контур: он не “торгует”, а делает диагностику и предлагает изменения `GAME_5M_*`.
- Применять изменения рекомендуется **по одному** (чтобы понимать причинность) и сравнивать эффект на следующем окне.
- Для воспроизводимости анализатор возвращает текущие параметры в `meta.current_decision_rule_params` и “память” о прошлом прогоне (если включено).

---

## 8. Векторная база знаний

### 8.1. Процесс использования векторной БЗ (планируется)

```mermaid
flowchart TD
    A[Новость добавлена в knowledge_base] --> B[Автоматическая синхронизация]
    B --> C[Генерация embedding (sentence-transformers или API)]
    C --> D[Сохранение в knowledge_base: колонка embedding]
    
    E[AnalystAgent.get_decision] --> F[Формирование запроса]
    F --> G[Генерация embedding для запроса]
    G --> H[Векторный поиск в knowledge_base WHERE embedding IS NOT NULL]
    
    H --> I[Поиск топ-5 похожих событий]
    I --> J[Анализ исходов похожих событий]
    J --> K[Улучшение торгового решения]
    
    style A fill:#e1f5ff
    style K fill:#c8e6c9
    style C fill:#fff9c4
```

**Комментарий**: Векторный поиск реализован в той же таблице **knowledge_base** (колонка embedding). Семантический поиск и анализ исходов (outcome_json) — см. [docs/NEWS.md](docs/NEWS.md), [docs/VECTOR_KB_USAGE.md](docs/VECTOR_KB_USAGE.md).

### 8.2. Интеграция векторного поиска в анализ

```mermaid
sequenceDiagram
    participant AA as AnalystAgent
    participant VKB as VectorKB
    participant OpenAI as OpenAI API
    participant DB as PostgreSQL (knowledge_base)
    
    AA->>AA: Формирование контекста текущей ситуации
    Note over AA: "MSFT: close=350, sentiment=0.7, новость о продукте"
    
    AA->>VKB: search_similar(query, ticker="MSFT", limit=5)
    VKB->>OpenAI: Генерация embedding для query
    OpenAI-->>VKB: embedding[1536]
    
    VKB->>DB: Векторный поиск через pgvector
    Note over DB: SELECT * FROM knowledge_base<br/>WHERE embedding IS NOT NULL<br/>ORDER BY embedding <=> :query LIMIT 5
    
    DB-->>VKB: Топ-5 похожих событий
    VKB->>VKB: Анализ исходов событий
    Note over VKB: Проверка, как рынок реагировал<br/>на похожие новости в прошлом
    
    VKB-->>AA: Исторический контекст
    AA->>AA: Улучшение решения с учетом контекста
```

**Комментарий**: Векторный поиск позволяет находить похожие события по смыслу, а не только по ключевым словам. Это особенно полезно для анализа ситуаций, которые похожи по сути, но выражены разными словами. **Статус**: Планируется.

---

## 9. Диаграммы взаимодействия компонентов

### 9.1. Общая архитектура системы

```mermaid
graph TB
    subgraph "Внешние источники данных"
        A[yfinance API]
        B[Новостные источники]
        C[OpenAI API - будущее]
    end
    
    subgraph "Торговые агенты"
        D[AnalystAgent]
        E[ExecutionAgent]
    end
    
    subgraph "Утилиты"
        F[update_prices.py]
        G[news_importer.py]
        H[report_generator.py]
    end
    
    subgraph "База данных PostgreSQL"
        I[quotes]
        J[knowledge_base: новости, embedding, outcome_json]
        L[portfolio_state]
        M[trade_history]
    end
    
    A --> F
    F --> I
    B --> G
    G --> J
    C -.->|Backfill embeddings| J
    
    I --> D
    J --> D
    J -.->|Векторный поиск| D
    
    D --> E
    E --> L
    E --> M
    
    M --> H
    I --> H
    L --> H
    
    style D fill:#e1f5ff
    style E fill:#e1f5ff
    style I fill:#c8e6c9
    style J fill:#c8e6c9
    style L fill:#c8e6c9
    style M fill:#c8e6c9
```

**Комментарий**: Система построена на модульной архитектуре. Каждый компонент имеет четкую ответственность: AnalystAgent анализирует, ExecutionAgent исполняет, утилиты обновляют данные и генерируют отчеты. Векторная БЗ интегрируется в процесс анализа для улучшения качества решений.

### 9.2. Поток данных: от обновления цен до исполнения сделки

```mermaid
sequenceDiagram
    participant Cron as Cron/Scheduler
    participant Update as update_prices.py
    participant YF as yfinance
    participant DB as PostgreSQL
    participant Analyst as AnalystAgent
    participant Exec as ExecutionAgent
    participant Portfolio as portfolio_state
    
    Cron->>Update: Ежедневное обновление
    Update->>YF: Запрос котировок
    YF-->>Update: Данные
    Update->>DB: Сохранение в quotes
    
    Note over Analyst,Exec: Запуск торгового цикла
    Exec->>Analyst: get_decision(ticker)
    Analyst->>DB: Загрузка котировок (последние 5 дней)
    Analyst->>DB: Загрузка новостей (knowledge_base)
    Analyst->>Analyst: Технический анализ
    Analyst->>Analyst: Sentiment анализ
    Analyst-->>Exec: Решение (BUY/STRONG_BUY/HOLD)
    
    alt Решение = BUY/STRONG_BUY
        Exec->>DB: Проверка открытых позиций
        Exec->>DB: Получение текущей цены
        Exec->>Exec: Расчет размера позиции
        Exec->>Portfolio: Обновление portfolio_state
        Exec->>DB: Запись в trade_history
    else Решение = HOLD
        Exec->>Exec: Пропуск покупки
    end
    
    Exec->>Exec: Проверка стоп-лоссов
    Exec->>DB: Закрытие позиций при срабатывании стоп-лосса
```

**Комментарий**: Полный цикл от обновления данных до исполнения сделок. Система работает автономно после настройки cron для обновления цен. Торговый цикл можно запускать вручную или автоматически.

### 9.3. Управление портфелем и рисками

```mermaid
flowchart TD
    A[Портфель: portfolio_state] --> B[Расчет текущей стоимости]
    B --> C[Проверка диверсификации]
    C --> D{Доля актива > 20%?}
    
    D -->|Да| E[Ребалансировка: частичная продажа]
    D -->|Нет| F[Проверка стоп-лоссов]
    
    E --> G[Фиксация прибыли]
    G --> H[Обновление portfolio_state]
    H --> F
    
    F --> I[Для каждой позиции]
    I --> J{log_return <= -5%?}
    J -->|Да| K[Закрытие позиции]
    J -->|Нет| L[Позиция держится]
    
    K --> M[Запись в trade_history]
    L --> N[Следующая позиция]
    M --> N
    
    N --> O{Есть еще позиции?}
    O -->|Да| I
    O -->|Нет| P[✅ Управление рисками завершено]
    
    style A fill:#e1f5ff
    style P fill:#c8e6c9
    style E fill:#fff9c4
    style K fill:#ffcccc
```

**Комментарий**: Система управления рисками включает два уровня защиты: ребалансировку портфеля (ограничение доли одного актива) и стоп-лоссы (защита от больших потерь). Ребалансировка пока не реализована, но запланирована в ROADMAP.

---

## 10. Telegram бот-агент и вебхук

### 10.1. Общий поток: пользователь → бот → сервисы LSE

```mermaid
flowchart TD
    A[Пользователь в Telegram] --> B[Сообщение / команда]
    B --> C[Telegram API → webhook URL]
    C --> D[Cloud Run: LSE Bot Service]
    
    D --> E{Тип запроса}
    E -->|Команда /signal| H[Анализ сигналов / Analyst Agent]
    E -->|Команда /news| G[Запрос к knowledge_base]
    E -->|Команда /price| P[Текущая цена из quotes]
    E -->|Команда /chart| C[График цены (matplotlib)]
    E -->|Команда /ask| A[Вопросы (LLM + анализ)]
    E -->|Команда /tickers| T[Список тикеров]
    E -->|/portfolio, /buy, /sell, /history| EX[ExecutionAgent: портфель и сделки]
    E -->|Команда /recommend| REC[Рекомендация: сигнал + параметры управления]
    
    G --> L[PostgreSQL: knowledge_base]
    H --> M[Analyst Agent + quotes, sentiment]
    P --> Q[PostgreSQL: quotes]
    C --> Q
    A --> L
    A --> LLM[LLM Service]
    A --> M
    T --> Q
    EX --> Q
    EX --> PS[portfolio_state + trade_history]
    REC --> M
    REC --> RM[risk_limits: стоп-лосс, размер позиции]
    
    L --> P[Форматирование ответа]
    M --> P
    Q --> P
    LLM --> P
    PS --> P
    RM --> P
    
    P --> Q[Ответ в Telegram]
    Q --> A
    
    style A fill:#e1f5ff
    style D fill:#fff9c4
    style Q fill:#c8e6c9
```

**Комментарий**: Бот принимает входящие обновления через webhook (HTTPS), обрабатывает команды и свободный текст, обращается к БД и агентам LSE, возвращает ответ пользователю. 

**Доступные команды:**
- `/signal <ticker>` - полный анализ (решение, цена, RSI, sentiment, стратегия)
- `/news <ticker> [N]` — новости из `knowledge_base` (окно `KB_NEWS_LOOKBACK_HOURS`, по умолч. ~14 дней): HTML в чат + файл; **draft_bias**, **news.bias**, режим Gate (как nyse GAME_5M); см. `services/kb_news_report.py`
- `/price <ticker>` - текущая цена инструмента
- `/chart <ticker> [days]` - график цены за период (дневные данные)
- `/ask <вопрос>` - задать вопрос боту (LLM для понимания естественного языка)
- `/tickers` - список отслеживаемых инструментов
- **Песочница (виртуальные сделки):** `/portfolio` - портфель и P&L; `/buy <ticker> <кол-во>` - покупка по последней цене из `quotes`; `/sell <ticker> [кол-во]` - продажа (полная или частичная); `/history [N]` - последние сделки; `/recommend <ticker>` - рекомендация по входу и параметрам управления (стоп-лосс, размер позиции).

**Где хранятся сделки:**
- `portfolio_state` - текущий кэш (CASH) и открытые позиции (ticker, quantity, avg_entry_price).
- `trade_history` - каждая сделка: ts, ticker, side (BUY/SELL), quantity, price, commission, signal_type (MANUAL / STOP_LOSS / BUY / SELL), total_value, sentiment_at_trade, strategy_name. Актуальная схема таблиц: `docs/DATABASE_SCHEMA.md`; логика портфельной игры: `docs/PORTFOLIO_GAME.md`.

**Особенности:**
- Автоматическая нормализация тикеров (GC-F → GC=F)
- Распознавание естественных названий (золото → GC=F, фунт → GBPUSD=X)
- Поддержка множественных тикеров в одном запросе
- LLM используется для `/ask` и для ответов на вопросы вида «когда открыть позицию и какие параметры советуешь»
- Работа в группах через команду `/ask`

При деплое «Cloud Run + VM» бот и API — на Cloud Run, БД и cron — на VM; возможен вариант «всё на одной VM» (см. раздел 11).

### 10.2. Маршрутизация webhook и обработчики

```mermaid
flowchart LR
    subgraph Telegram[Telegram]
        A[Update]
    end
    
    A -->|POST /webhook| B[Cloud Run: Bot API]
    
    subgraph BotAPI[LSE Bot Service]
        B --> C{Тип update}
        C -->|message| D[Router сообщений]
        C -->|callback_query| E[Обработчик кнопок]
        C -->|не message| F[Игнор / лог]
        
        D --> G{Команда или текст}
        G -->|/start, /help| H[Приветствие и справка]
        G -->|/signal| K[Handler: сигналы/анализ]
        G -->|/news| J[Handler: новости/календарь]
        G -->|/price| P[Handler: текущая цена]
        G -->|/chart| C[Handler: график цены]
        G -->|/ask| A[Handler: вопросы (LLM)]
        G -->|/tickers| T[Handler: список тикеров]
        G -->|/portfolio, /buy, /sell, /history| EX[Handler: ExecutionAgent]
        G -->|/recommend| REC[Handler: рекомендация по тикеру]
        G -->|Текст (в группах игнорируется)| M[Используйте /ask]
    end
    
    K --> N[(PostgreSQL)]
    J --> N
    P --> N
    C --> N
    A --> N
    A --> LLM[LLM Service]
    T --> N
    EX --> N
    EX --> PS[portfolio_state, trade_history]
    REC --> N
    
    N --> O[Ответ в Telegram]
    LLM --> O
    H --> O
    E --> O
    F --> O
    
    style B fill:#fff9c4
    style N fill:#e1f5ff
```

**Комментарий**: Webhook получает все типы update; основная логика — в `message`. Команды маппятся на отдельные handler'ы; произвольный текст идёт в агента с доступом к knowledge_base (новости и векторный поиск по embedding).

### 10.3. Размещение компонентов (Cloud Run + VM)

```mermaid
flowchart TB
    subgraph External[Внешний мир]
        TG[Telegram]
        CRON[Cron / Scheduler]
    end
    
    subgraph GCP[Cloud Run]
        BOT[LSE Bot Service<br/>webhook + handlers]
        API[LSE API Service<br/>отчёты, статус, данные]
    end
    
    subgraph Server[Отдельный сервер<br/>когда будет готов]
        PG[(PostgreSQL<br/>lse_trading)]
        KB[(knowledge_base)]
    end
    
    TG -->|HTTPS webhook| BOT
    CRON -->|Вызов API или скрипт| API
    
    BOT --> PG
    BOT --> KB
    API --> PG
    API --> KB
    
    style BOT fill:#fff9c4
    style API fill:#fff9c4
    style PG fill:#c8e6c9
    style KB fill:#c8e6c9
```

**Комментарий**: При варианте «Cloud Run + VM» Telegram и API работают на Cloud Run, Postgres и knowledge_base — на VM; подключение по `DATABASE_URL`. Альтернатива — всё на одной VM (см. раздел 11, `docs/DEPLOY.md` и `docs/DEPLOY_GCP.md`).

---

## 11. Развёртывание

Два варианта: **одна VM** (Postgres + cron + бот) или **Cloud Run** (бот/API) + **VM** (БД + cron). Команды, переменные окружения и сравнение стоимости — в **docs/DEPLOY.md** и **docs/DEPLOY_GCP.md**.

```mermaid
flowchart LR
    subgraph Deploy[Деплой]
        GIT[GitHub] --> BUILD[Cloud Build]
        BUILD --> RUN[Cloud Run: Bot + API]
        RUN --> SERVER[VM: Postgres + cron]
    end
    style RUN fill:#fff9c4
    style SERVER fill:#c8e6c9
```

---

## Примечания

### Использование диаграмм

1. **Mermaid поддерживается в:**
   - ✅ GitHub/GitLab (автоматически рендерится)
   - ✅ VS Code (с расширением Mermaid Preview)
   - ✅ Онлайн-редакторы: [mermaid.live](https://mermaid.live)

2. **Редактирование:**
   - Диаграммы можно редактировать прямо в Markdown
   - Для сложных диаграмм используйте [mermaid.live](https://mermaid.live) для визуализации

### Связь с кодом

Эти диаграммы соответствуют:
- `analyst_agent.py` — анализ торговых сигналов
- `execution_agent.py` — исполнение сделок
- `init_db.py` — инициализация БД
- `update_prices.py` — обновление цен
- `news_importer.py` — импорт новостей
- `report_generator.py` — генерация отчетов
- `vector_kb` / sync cron — векторный поиск по `knowledge_base.embedding` (см. [docs/VECTOR_KB_USAGE.md](docs/VECTOR_KB_USAGE.md))
- Telegram бот: webhook, handlers, команды `/signal`, `/news`, `/price`, `/chart`, `/ask`, `/tickers`, песочница: `/portfolio`, `/buy`, `/sell`, `/history`, `/recommend` (см. раздел 10)
- Документация по сделкам: `docs/DATABASE_SCHEMA.md` и `docs/PORTFOLIO_GAME.md`
- Деплой: варианты «одна VM» или «Cloud Run + VM» (см. раздел 11, `docs/DEPLOY.md` и `docs/DEPLOY_GCP.md`)
- Премаркет и игры: `setup_cron.sh` — расписание (5m каждые 5 мин, портфельная 9/13/17, премаркет 16:30 MSK). Премаркет: `scripts/premarket_cron.py`, `services/premarket.py`; актуальное описание — [docs/GAME_5M_PREMARKET_AND_IMPULSE.md](docs/GAME_5M_PREMARKET_AND_IMPULSE.md); старое резюме — [docs/archive/RESUME_PREMARKET_AND_RECENT.md](docs/archive/RESUME_PREMARKET_AND_RECENT.md)
- Уведомления в Telegram: `services/telegram_signal.py` — общая рассылка (get_signal_chat_ids, send_telegram_message). Сигналы 5m — `send_sndk_signal_cron.py`; сделки портфельной игры — `trading_cycle_cron.py` после run_for_tickers (те же TELEGRAM_SIGNAL_CHAT_IDS). В боте `/history [тикер] [N]` — фильтр по тикеру, в ответе стратегия (GAME_5M / Portfolio / Manual).

### Обновление диаграмм

При изменении бизнес-логики обновляйте соответствующие диаграммы в этом файле и краткую схему в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Диаграммы должны быть согласованы с кодом.

---

**Последнее обновление**: 2026-03-27  
**Версия системы**: 1.4.0

### Изменения в версии 1.4.0:
- ✅ Песочница в Telegram: `/portfolio`, `/buy`, `/sell`, `/history [тикер] [N]` — виртуальные сделки через ExecutionAgent; в `/history` — фильтр по тикеру и отображение стратегии
- ✅ Уведомления о сделках портфельной игры в те же чаты (trading_cycle_cron → telegram_signal); общий модуль `services/telegram_signal.py`
- ✅ `/recommend <ticker>` — рекомендация по входу и параметрам управления (стоп-лосс, размер позиции)
- ✅ В `/ask` — ответы на вопросы «когда открыть позицию», «какие параметры советуешь» с учётом сигнала и risk_limits
- ✅ Документация: `docs/DATABASE_SCHEMA.md` и `docs/PORTFOLIO_GAME.md` — схема БД и актуальная логика сделок
- ✅ Обновлены диаграммы раздела 10 (поток запросов и маршрутизация)

### Изменения в версии 1.3.0:
- ✅ Добавлена команда `/chart` - график цены за период (дневные данные)
- ✅ Добавлена команда `/ask` - вопросы на естественном языке с поддержкой LLM
- ✅ Улучшена нормализация тикеров (GC-F → GC=F, GBPUSD-X → GBPUSD=X)
- ✅ Поддержка множественных тикеров в одном запросе
- ✅ Распознавание естественных названий (золото → GC=F, фунт → GBPUSD=X)
- ✅ LLM используется только для команды `/ask` (понимание вопросов)
- ✅ Добавлена стратегия Neutral для неопределённых рыночных режимов
- ✅ Улучшена обработка новостей: фильтрация шума, сортировка по важности
- ✅ Обновлены диаграммы бизнес-процессов для Telegram бота

### Изменения в версии 1.2.0:
- ✅ Добавлен раздел 10: Telegram бот-агент с webhook и маршрутизацией команд
- ✅ Добавлен раздел 11: развёртывание на Cloud Run и отдельном сервере (Postgres + КБ), по образцу sc
- ✅ Ссылки на `docs/DEPLOY.md` и `docs/DEPLOY_GCP.md` для деплоя

### Изменения в версии 1.1.0:
- ✅ Добавлен Strategy Manager для автоматического выбора стратегий
- ✅ Реализована центрированная шкала sentiment (-1.0 до 1.0)
- ✅ Добавлено извлечение insight из новостей
- ✅ Интеграция strategy_name в trade_history
- ✅ Улучшено логирование выбора стратегий

