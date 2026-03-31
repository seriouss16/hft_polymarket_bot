# benchmark_feed_latency.py

Замеряет не только сетевой отклик, а именно **запаздывание `rtds_polymarket` по цене** относительно более быстрых источников (Binance/Coinbase) в реальном времени.

Скрипт полезен для подбора VPN/VPS с минимальным lag по кривой цены.

## Что измеряется

1. `Polymarket signal staleness`  
   Возраст последнего тика Poly (мс) в момент прихода нового тика Binance/Coinbase.

2. `Price gap`  
   Разница цены `Poly - CEX` (USD), чтобы видеть постоянный сдвиг и разброс.

3. `Catch-up`  
   После движения Binance на `--move-threshold` USD измеряется время до следующего тика Poly.

4. `Curve lag`  
   Основная метрика: лаг по форме кривой.
   - Ресемплинг в 1 Hz (1 точка/сек)
   - Окно `--lag-window-sec` (например 20 сек)
   - Поиск оптимального сдвига Poly в диапазоне `0..--lag-max-sec`
   - Выбор лага по максимальной корреляции изменений (first diff)

5. `Supplement`  
   Дополнительные recv-метрики (порядок прихода и inter-arrival gaps).

## Соответствие обычному режиму hft_bot

По умолчанию используются те же WSS-подключения, что и в рабочем цикле бота:

- Binance: `wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker`
- Coinbase: `wss://ws-feed.exchange.coinbase.com` (ticker `BTC-USD`)
- Polymarket RTDS: `wss://ws-live-data.polymarket.com` (`crypto_prices_chainlink`, `btc/usd`)

Kraken в основном цикле `hft_bot` не используется, поэтому он только опционален (`--kraken`).

## Быстрый запуск

```bash
uv run python hft_bot/scripts/benchmark_feed_latency.py --duration 60
```

После запуска скрипт автоматически сохраняет **3 файла** в `hft_bot/reports/banch_lag`:

- `... .csv` — выровненные ряды (1Hz)
- `... .png` — график кривых
- `... .md` — итоговый отчет с метриками + встроенный график

Имена файлов имеют общий basename с меткой:
`timestamp + country + city` (по текущему публичному IP).

Рекомендуемый запуск для lag-анализа (20 точек по 1 сек):

```bash
uv run python hft_bot/scripts/benchmark_feed_latency.py \
  --duration 60 \
  --lag-window-sec 20 \
  --lag-max-sec 15 \
  --move-threshold 5
```

Первичный отбор для быстрого теста:
```bash
uv run python hft_bot/scripts/benchmark_feed_latency.py \
  --duration 35 \
  --lag-window-sec 12 \
  --lag-max-sec 8 \
  --move-threshold 3
```

```bash
uv run python hft_bot/scripts/benchmark_feed_latency.py \
  --duration 60 \
  --lag-window-sec 20 \
  --lag-max-sec 15 \
  --move-threshold 5 \
  --export-csv hft_bot/reports/banch_lag/
```

## Аргументы

- `--duration`  
  Длительность замера в секундах. Для стабильного `Curve lag` обычно >= 45-60.

- `--move-threshold`  
  Порог движения Binance (USD) для метрики `Catch-up`.  
  Практично: `3..6`. При `10+` часто мало сэмплов.

- `--lag-window-sec`  
  Размер окна в секундах для оценки лага по кривым (обычно `20`).

- `--lag-max-sec`  
  Максимальный лаг Poly (в секундах), который ищем в окне.

- `--export-csv`  
  Базовый путь экспорта. По умолчанию: `hft_bot/reports/banch_lag/feed_lag_alignment.csv`.  
  На основе этого пути скрипт создает `csv/png/md` с timestamp+geo тегом.  
  Можно передать директорию: `hft_bot/reports/banch_lag/`.

- `--kraken`  
  Добавляет Kraken как дополнительный источник (не часть стандартного `hft_bot` цикла).

- `--http-clob`  
  Дополнительно делает 3 HTTPS запроса в `https://clob.polymarket.com/` (не влияет на curve lag).

## Как читать `Curve lag`

Пример:

```text
Binance -> Poly lag(sec): 1.0 / 2.7 / 14.0; median=2.0; windows=26; corr(mean/median)=0.584/0.606
```

- `min / mean / max` — оценка лага в секундах по окнам.
- `median` — более устойчивое значение (ориентир для практики).
- `corr` — качество совпадения формы кривых после сдвига.

Если `no samples`:
- увеличьте `--duration` (например до 90-120),
- уменьшите `--lag-window-sec` (например 15),
- проверьте стабильность потока RTDS.

## CSV для графика

При `--export-csv` создается файл с колонками:

- `sec_idx`
- `binance_mid`
- `coinbase_mid`
- `poly_mid`
- `poly_shifted_for_binance`
- `poly_shifted_for_coinbase`

`poly_shifted_*` уже сдвинуты на медианный лаг, их удобно накладывать на график для визуальной проверки догоняния.

### Отдельный рендер (опционально)

```bash
uv run python hft_bot/scripts/plot_feed_lag_alignment.py \
  --input hft_bot/reports/banch_lag/feed_lag_alignment.csv \
  --output hft_bot/reports/banch_lag/feed_lag_alignment.png
```

Обычно этот шаг не нужен, потому что `benchmark_feed_latency.py` уже строит PNG автоматически.
Отдельный скрипт полезен, если нужно перерисовать график из уже сохраненного CSV.

PNG содержит 2 панели:

1. `Raw curves` — Binance / Coinbase / Poly RTDS как есть.
2. `Aligned curves` — Binance / Coinbase + `poly_shifted_for_binance` и `poly_shifted_for_coinbase`.

Так видно, насколько после сдвига Poly визуально совпадает с ведущими кривыми.

## Рекомендации для сравнения VPN/VPS

Для каждого маршрута прогоните одинаковый сценарий (например 2-3 раза по 60 сек) и сравнивайте:

1. `Curve lag median` (Binance->Poly и Coinbase->Poly) — меньше лучше.
2. `Polymarket signal staleness median/mean` — меньше лучше.
3. `Catch-up` median/mean — меньше лучше.
4. `corr` в `Curve lag` — выше стабильнее.

## Примечание

Абсолютные цены Binance/Coinbase и RTDS могут отличаться по уровню. Для оценки задержки это нормально: ключевая метрика — **временной сдвиг формы кривых**, а не нулевой ценовой спред.

