# 📘 MASTER DEBUG & REFACTOR PLAN (HFT BOT, Python)

---

## 🔹 0. ЦЕЛЬ

Провести **полный аудит и рефакторинг системы**, чтобы:

* исключить скрытые баги и рассинхрон
* убрать неиспользуемый код
* гарантировать корректность логики HFT
* обеспечить соответствие:

  * SOLID
  * OOP best practices
  * PEP 8
  * PEP 257 (docstrings)

---

# 🔹 1. АРХИТЕКТУРНЫЙ АНАЛИЗ

## 1.1 Разделение системы

Проверить, что код логически разделен на модули:

* WS layer (подключение)
* Order management
* Strategy (RSI / ADX / MD)
* Orderbook processing
* Risk management
* Logging / metrics

❗ Требование:

```text
Каждый модуль = одна зона ответственности
```

---

## 1.2 SOLID

Проверить:

* S — один класс = одна задача
* O — расширяемость без переписывания
* L — корректное наследование
* I — интерфейсы не раздуты
* D — зависимости через абстракции

---

## 1.3 Dependency graph

* Найти:

  * циклические зависимости ❌
  * скрытые зависимости ❌
* Все зависимости → явные

---

# 🔹 2. АСИНХРОННОСТЬ И HFT-ПОВЕДЕНИЕ

---

## 2.1 Неблокирующая архитектура

Проверить:

* нет блокирующих операций в:

  * WS обработчиках
  * execution loop

❗ Запрещено:

```python
time.sleep()
blocking I/O
```

---

## 2.2 Разделение потоков

Разделить:

* market data (async)
* execution (strict sequential)
* logging (async)

---

## 2.3 Event Queue (ОБЯЗАТЕЛЬНО)

Ввести:

```text
ВСЕ события → очередь
обработка → строго по одному
```

---

## 2.4 FSM (state machine)

Проверить / внедрить:

```text
IDLE
PLACING_ORDER
WAITING_FILL
PARTIAL_FILL
CANCELING
FILLED
EXITING
```

❗ Запрещено:

* перескакивать состояния

---

# 🔹 3. WS И СОЕДИНЕНИЕ

---

## 3.1 Подписки

Проверить:

* ордера
* fills
* orderbook

---

## 3.2 Heartbeat

```text
каждые 4.5 сек
```

---

## 3.3 Реконнект

Если:

* нет событий
* heartbeat потерян

→ авто-реконнект
→ ресабскрайб
→ REST синхронизация

---

## 3.4 Consistency check

После реконнекта:

* сверить:

  * активные ордера
  * позиции

---

# 🔹 4. ОРДЕРБУК И ДАННЫЕ

---

## 4.1 Свежесть

```text
если stale → запрет действий
```

---

## 4.2 Timestamp контроль

* каждый тик → timestamp
* проверка задержки

---

## 4.3 Out-of-order защита

```text
если событие старее → игнор
```

---

# 🔹 5. ОРДЕРА И ИСПОЛНЕНИЕ

---

## 5.1 Статусы

* FILLED
* PARTIAL
* OPEN
* CANCELED

---

## 5.2 Контроль через WS

❗ Нельзя:

* принимать решения без подтверждения

---

## 5.3 Partial fill

Проверить:

* остаток корректно учитывается
* нет двойных входов

---

## 5.4 Анти-дублирование

* уникальные order_id
* защита от повторной отправки

---

# 🔹 6. СТРАТЕГИЯ И ИНДИКАТОРЫ

---

## 6.1 Синхронизация

Проверить:

* все индикаторы используют **одинаковый timestamp**

---

## 6.2 Задержки

* индикаторы не должны:

  * лагать
  * давать старые сигналы

---

## 6.3 Поведение при волатильности

Проверить:

* нет "залипания"
* корректный reset

---

# 🔹 7. КЭШИ

---

## 7.1 Актуальность

* TTL или timestamp

---

## 7.2 Неблокирующий доступ

* async-safe
* без гонок

---

## 7.3 Консистентность

* нет расхождения с WS

---

# 🔹 8. ЛОГИРОВАНИЕ

---

## 8.1 Обязательные события

* order placed
* canceled
* filled
* partial
* reconnect

---

## 8.2 Асинхронность

* логирование НЕ блокирует execution

---

## 8.3 Чистота

* убрать шум
* оставить только полезные события

---

# 🔹 9. ТЕСТЫ (КРИТИЧНО)

---

## 9.1 Проверка тестов

Проверить:

* не "подогнаны ли" тесты под код

---

## 9.2 Добавить:

* latency simulation
* partial fills
* reconnect
* out-of-order events

---

## 9.3 Интеграционные тесты

* полный цикл:

  * сигнал → ордер → fill → выход

---

# 🔹 10. СТАТИЧЕСКИЙ АНАЛИЗ

---

Запустить:

* pylint
* flake8
* mypy

---

Проверить:

* типы
* unused code
* dead code

---

# 🔹 11. ЧИСТКА КОДА

---

Удалить:

* неиспользуемые функции
* старые классы
* fallback-логики без смысла

---

❗ НО:

Перед удалением:

```text
проверить через usage / tests
```

---

# 🔹 12. СТИЛЬ И ДОКУМЕНТАЦИЯ

---

## PEP 8:

* имена
* отступы
* длина строк

---

## PEP 257:

* docstrings для:

  * классов
  * методов
  * модулей

---

# 🔹 13. ПРОФИЛИРОВАНИЕ

---

Проверить:

* latency:

  * сигнал → ордер
  * ордер → fill

---

Инструменты:

* cProfile
* line profiler

---

# 🔹 14. RISK LAYER

---

Добавить / проверить:

* max позиций
* max убыток
* kill-switch
* position limits per market

---

# 🔹 15. СИМУЛЯЦИЯ

---

Прогнать:

* 0.5 сек лаг
* 1 сек
* 2 сек

---

Сравнить:

* winrate
* slippage
* PnL

---

# 🔹 16. ФИНАЛЬНАЯ ПРОВЕРКА

---

Проверить:

✅ нет гонок
✅ нет рассинхрона
✅ нет stale данных
✅ нет блокировок
✅ нет дублирующих ордеров

---

# 🔹 17. ОБРАБОТКА ОШИБОК И ОТКАЗОУСТОЙЧИВОСТЬ

---

## 17.1 Исключения в asyncio

Убедиться:

* try/except в каждой корутине
* logging.exception для full traceback
* нет "тихих" падений фоновых задач
* Task.add_done_callback для отслеживания завершения

---

## 17.2 Graceful shutdown

При получении SIGTERM / SIGINT:

* cancel all pending orders
* сохранить final state в журнал
* не потерять открытые позиции
* close all connections gracefully

---

## 17.3 Dead letter queue

Для событий, которые не обработались:

* capture в отдельный queue
* retry logic с exponential backoff (1s, 2s, 4s, 8s...)
* max retries = 5
* логировать все неудачи

---

## 17.4 Circuit breaker

Для внешних API (CEX, Polymarket):

* если 5+ ошибок подряд → circuit OPEN
* остановить сигналы на 60 сек
* затем попробовать еще раз (HALF_OPEN)
* при успехе → CLOSED

---

# 🔹 18. МЕТРИКИ И TRACING

---

## 18.1 Latency tracking

Логировать для каждого ордера:

* signal_timestamp → order_send_timestamp (мс)
* order_send → order_ack (мс)
* order_ack → fill (мс)
* fill → exit_action (мс)

```python
# Пример структуры
{
  "trade_id": "uuid",
  "signal_time_ms": 1701234567890,
  "send_time_ms": 1701234567892,
  "ack_time_ms": 1701234567894,
  "fill_time_ms": 1701234567900,
  "exit_time_ms": 1701234567910,
}
```

---

## 18.2 Trade statistics

Метрики за сессию:

* trades_total
* trades_per_hour
* win_rate (%)
* sharpe_ratio
* max_drawdown (%)
* per-strategy breakdown

---

## 18.3 Data source health

Отслеживать для каждого источника:

* Coinbase: uptime %, message latency p50/p95/p99
* Binance: uptime %, message latency p50/p95/p99
* Polymarket RTDS: uptime %, price refresh rate
* Polymarket CLOB: order fill rate, rejection rate

---

## 18.4 Exposition

* открытые позиции по маркету
* текущий drawdown
* используемый размер позиции vs LIVE_MAX_SIZE

---

# 🔹 19. TRADE JOURNAL & REPLAY

---

## 19.1 Каждый ордер записать

JSON в файл trades.jsonl (newline-delimited JSON):

```json
{
  "trade_id": "uuid",
  "timestamp_utc": "2024-01-15T10:23:45.123Z",
  "market": "BTC-25MAR",
  "side": "BUY",
  "entry_reason": "latency_arb",
  "entry_price": 65000.50,
  "entry_size": 10,
  "entry_timestamp_ms": 1705321425123,
  "exit_timestamp_ms": 1705321426234,
  "exit_price": 65001.20,
  "exit_reason": "trailing_tp",
  "pnl_points": 0.7,
  "pnl_usd": 70.0,
  "slippage_points": 0.3,
  "latency_to_fill_ms": 1111,
  "cancelled": false
}
```

---

## 19.2 Replay механизм

Для отладки конкретной сессии:

* сохранить все WS события (price ticks, order fills)
* запустить bot с теми же данными
* убедиться, что воспроизводится точно
* лог сравнить с оригиналом

```bash
# Пример
python bot.py --replay=session_2024-01-15_10-23.jsonl
```

---

## 19.3 Post-mortem анализ

При бага:

* загрузить journal трейда
* найти точку дивергенции (signal vs actual fill)
* проверить WS messages в тот момент
* воспроизвести offline

---

# 🔹 20. LIVE SAFETY GATES

---

## 20.1 Pre-flight check перед ордером

Перед отправкой в Polymarket, проверить:

* размер ≤ LIVE_MAX_SIZE (config param)
* цена в коридоре (не >2% от mid)
* нет открытых ордеров в SAME направлении (anti-doubling)
* current drawdown < MAX_DRAWDOWN_PCT
* session PnL < -LIVE_MAX_SESSION_LOSS

Если хоть одно не прошло → skip сигнал (логировать)

---

## 20.2 Order timeout

Если ордер не заполнен за N секунд:

* автоматически отменить
* логировать причину
* увеличить N на следующий раз (adaptive)

---

## 20.3 Kill-switch

Добавить endpoint:

```bash
curl -X POST http://localhost:8001/kill
```

При вызове:

* отменить ВСЕ активные ордера (REST bulk cancel)
* close WS connections
* set state to SHUTDOWN
* не принимать новые сигналы
* дождаться всех fills
* exit gracefully

---

## 20.4 Live mode indicators

В логе / stdout:

```
🔴 LIVE MODE: ON
⚠️  Max session loss: $1000
📊 Current PnL: $-45 (4.5% drawdown)
📈 Trades today: 23, WR: 65%
🔌 Coinbase: ✅ (p99: 45ms), Binance: ✅ (p99: 52ms)
```

---

# 🔹 21. КОНФИГ & HOT RELOAD

---

## 21.1 Config versioning

Сохранять версию конфига при старте:

```json
{
  "config_version": "1.2.3",
  "config_hash": "sha256:...",
  "timestamp_utc": "2024-01-15T10:00:00Z"
}
```

---

## 21.2 A/B testing

Возможность запустить две стратегии одновременно:

```
STRATEGY_A=phase_router RSI_THRESHOLD=65
STRATEGY_B=latency_pure EDGE_MIN=0.5

RUN_BOTH=true
SPLIT_CAPITAL=true (50/50)
```

---

## 21.3 Exponential backoff для реконнекта

```python
# Пример
reconnect_delays = [1, 2, 4, 8, 16, 32, 60]  # сек
attempt = 0
while attempt < len(reconnect_delays):
    try:
        connect()
        break
    except:
        await asyncio.sleep(reconnect_delays[attempt])
        attempt += 1
```

---

# 🔹 22. БЕЗОПАСНОСТЬ В LIVE

---

## 22.1 Валидация перед отправкой

Проверить ордер:

* размер > 0 и ≤ max
* цена > 0
* order_type валиден
* нет NaN / Infinity

Если invalid → reject с логированием

---

## 22.2 API key rotation

Если key скомпрометирован:

* сохранить текущие позиции в DB
* отключить старый key (blacklist)
* переключиться на backup key
* продолжить без перезапуска

---

## 22.3 Aудит логирование

Для каждого ордера (даже отклоненного) логировать:

```json
{
  "timestamp": "...",
  "action": "ORDER_SEND",
  "market": "BTC-25MAR",
  "side": "BUY",
  "size": 10,
  "price": 65000.50,
  "result": "SUCCESS",
  "server_order_id": "polymarket_123456"
}
```

---

## 22.4 Логирование без exposure

При логировании НЕ писать:

* полные API keys (only last 4 chars)
* wallet addresses (only last 4 chars)
* сенситивные данные в stdout

```python
# ❌ ПЛОХО
logger.info(f"API key: {api_key}")

# ✅ ХОРОШО
logger.info(f"API key: ...{api_key[-4:]}")
```

---

# 🔹 23. ВЕРСИОНИРОВАНИЕ & ROLLBACK

---

## 23.1 Strategy versioning

Каждая стратегия должна иметь версию:

```python
class LatencyArbitrageV2:
    VERSION = "2.1.3"
    AUTHOR = "..."
    DEPLOYED_AT = "2024-01-10"
```

---

## 23.2 Live strategy switch

Если текущая версия теряет деньги:

* сохранить текущие позиции
* switch на предыдущую версию
* не потерять PnL

```bash
curl -X POST http://localhost:8001/switch-strategy \
  -d '{"strategy": "latency_arb", "version": "2.0.5"}'
```

---

## 23.3 Config rollback

Если новый конфиг сломал bot:

* откатить на версию N-1
* сравнить метрики
* логировать что изменилось

---

# 🔹 24. ЗАВИСИМОСТИ & БЕЗОПАСНОСТЬ

---

## 24.1 Уязвимости

Регулярно проверять:

```bash
pip-audit
```

Критичные (CVSS ≥7.0) → обновить сразу

---

## 24.2 Version pinning

Зафиксировать версии библиотек:

```
websockets==12.0  # точная версия
numpy==1.24.0
```

НЕ использовать `>=` для latency-critical пакетов

---

## 24.3 Batch testing

Перед обновлением зависимости:

* тесты на sim mode
* сравнить latency
* А/B тест в live (если нужно)

---

# 🔹 25. KPI & ФИНАЛЬНЫЙ ЧЕКЛИСТ

---

## 25.1 Success criteria

После рефакторинга:

✅ 100% тест coverage для critical path
✅ Latency signal→fill < 200мс (p99)
✅ Zero unhandled exceptions за 100+ trades
✅ Win rate ≥ 60% в sim mode
✅ Zero crashed sessions за месяц
✅ Code review passed (static analysis + human)

---

## 25.2 Performance targets

| Метрика | Target | Current |
|---------|--------|---------|
| Signal → Order (мс) | < 50 | ? |
| Order → Fill (мс) | < 100 | ? |
| Reconnect time (сек) | < 5 | ? |
| Trades/hour | 10-30 | ? |
| Win rate (%) | ≥ 60 | ? |
| Sharpe ratio | ≥ 1.5 | ? |

---

## 25.3 Deployment checklist

Перед live:

- [ ] Все тесты зеленые
- [ ] Static analysis: pylint/flake8/mypy ✓
- [ ] Risk parameters в консервативных пределах
- [ ] Kill-switch включен и тестирован
- [ ] Alerting настроен (Slack/email)
- [ ] Backup strategy готов
- [ ] Trade journal пишется
- [ ] Metrics собираются
- [ ] API key в safe storage (не в .env)
- [ ] Логирование чистое (no secrets)

---

# 🧠 КАК ИСПОЛЬЗОВАТЬ ЭТОТ ПЛАН

Для каждого раздела:

1. **Найти проблемы** в текущем коде
2. **Объяснить** почему это проблема
3. **Написать фикс** с примером
4. **Создать тест** который проверяет фикс
5. **Коммитить** атомарно (один раздел = один или несколько PR)

Пример цикла:

```
15 раздел (Симуляция) →
  - Найти текущие latency lag scenarios
  - Добавить 1s/2s lag sim tests
  - Посмотреть win rate
  - Коммитить в отдельный PR
  - Code review
  - Merge

Затем: 16 раздел → 17 раздел → ...
```

---

# 🚀 ИТОГ

Если пройти все 25 разделов:

👉 У тебя будет:

* **Production-grade HFT бот**
* **Без скрытых багов и рассинхрона**
* **Устойчивый к рынку и сбоям**
* **Auditible trade history**
* **Safety gates для live**
* **Полная observability**
* **Easy rollback & recovery**

**Estimate:** 3-4 недели интенсивной работы с AI помощью.
