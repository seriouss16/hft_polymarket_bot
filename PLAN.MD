Высокочастотный арбитражный бот (Hybrid LSTM + HFT)
Этап 1: Инфраструктура и автоматизация слотов
[ ] MarketSelector (Smart Slug): Реализовать логику автоматического вычисления текущего 5-минутного рынка BTC (округление UTC времени).

[ ] Dynamic Token Fetcher: Система предзагрузки clobTokenId для следующего слота через Gamma API за 15–30 секунд до начала.

[ ] Dual-Channel Manager: Поддержка активного соединения с текущим рынком и фонового "прогрева" (warm-up) для следующего.

Этап 2: Скоростной агрегатор цен (Latency Layer)
[ ] Fast Feed Integration: Объединение binance_feeder, coinbase_feeder и kraken_feeder из bot2.py.

[ ] Weighted Mid-Price: Алгоритм расчета "истинной цены" на основе весов бирж и их текущего пинга (lag).

[ ] Fast-Path Websocket: Оптимизация обработки сообщений с использованием ujson и исключение лишних проверок в цикле.

Этап 3: Аналитика и фильтры (RSI + LSTM)
[ ] Asynchronous LSTM: Вынос тяжелого прогноза LSTM в отдельный поток, чтобы инференс не блокировал чтение цен.

[ ] Signal Smoothing: Интеграция RSI как предохранителя: запрет на покупку при RSI > 70 и на продажу при RSI < 30.

[ ] Trend Validator: Использование скользящих средних (MA) для подтверждения импульса от быстрых бирж.

Этап 4: Система исполнения и два режима работы
[ ] Режим 1: TEST_MODE (Paper Trading):

Расчет виртуального PnL в реальном времени на основе живых данных.

Учет "искусственного проскальзывания" (slippage) и виртуальной комиссии.

Ведение статистики: Win Rate, Max Drawdown, Profit Factor.

[ ] Режим 2: PROD_MODE (Live Trading):

Подпись транзакций через PRIVATE_KEY и отправка в Polymarket CLOB.

Мониторинг фактического баланса и контроль рисков (Stop-Loss на уровне счета).

Логирование ответов API для анализа скорости исполнения.

Этап 5: Анализ стакана и борьба за ликвидность
[ ] Order Book Depth: Анализ глубины стакана Polymarket перед входом, чтобы не "пробить" его своим ордером.

[ ] Front-Running Logic: Выставление лимитных ордеров на 1 тик выгоднее конкурентов при получении импульса от Binance.

[ ] Exit Strategy: Динамический выход при затухании импульса (дельта между быстрым фидом и Polymarket сокращается).

prjBJ_arb_polymarket/
├── bot_hft_main.py         # Точка входа: инициализация и запуск asyncio loop
├── config.py               # Константы, API ключи, настройки порогов (thresholds)
├── .env                    # Приватные ключи и секреты
├── core/
│   ├── engine.py           # Логика принятия решений (Decision Engine)
│   ├── executor.py         # Отправка ордеров (Prod) vs Логика симулятора (Test)
│   └── selector.py         # Авто-выбор слотов (Slot Manager)
├── data/
│   ├── aggregator.py       # Агрегатор быстрых фидов (Binance, CB, Kraken)
│   ├── providers.py        # Websocket клиенты для каждой биржи [из bot2.py]
│   └── poly_clob.py        # Чтение стакана (Order Book) Polymarket
├── ml/
│   ├── model.py            # Архитектура и инференс LSTM [из bot3.py]
│   └── indicators.py       # Быстрый расчет RSI, MA, MACD (без pandas)
└── utils/
    ├── logger.py           # Расширенное логирование с PnL
    └── stats.py            # Метрики: WinRate, Drawdown, Latency