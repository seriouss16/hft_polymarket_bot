import os
import sys
import asyncio
import logging
import traceback
from datetime import datetime, timezone

# --- Форсируем вывод и отключаем мусор TF ---
os.environ['PYTHONUNBUFFERED'] = '1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'

print(">>> Инициализация HFT системы...", flush=True)

# Импорты после настройки окружения
import tensorflow as tf
from core.selector import MarketSelector
from core.executor import PnLTracker
from core.engine import HFTEngine
from core.live_engine import LiveExecutionEngine, LiveRiskManager
from data.aggregator import FastPriceAggregator
from data.providers import FastExchangeProvider
from data.poly_clob import PolyOrderBook
from ml.model import AsyncLSTMPredictor
from utils.stats import StatsCollector

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

async def main():
    # --- Конфигурация ---
    TEST_MODE = True
    LIVE_MODE = os.getenv("LIVE_MODE", "0") == "1"
    SYMBOL = "BTC"
    STATS_INTERVAL = 60  # Отчет раз в минуту
    PULSE_INTERVAL =0.5   # Пульс цен раз в 2 секунды
    
    # --- Инициализация компонентов ---
    selector = MarketSelector(asset=SYMBOL)
    aggregator = FastPriceAggregator()
    pnl = PnLTracker(initial_balance=1000.0)
    stats = StatsCollector(pnl)
    engine = HFTEngine(pnl, is_test_mode=TEST_MODE)
    lstm = AsyncLSTMPredictor(history_len=100)
    live_exec = LiveExecutionEngine(
        private_key=os.getenv("PRIVATE_KEY"),
        funder=os.getenv("FUNDER"),
        test_mode=not LIVE_MODE,
        min_order_size=float(os.getenv("LIVE_ORDER_SIZE", "10")),
        max_spread=float(os.getenv("LIVE_MAX_SPREAD", "0.03")),
    )
    live_risk = LiveRiskManager(max_daily_loss=float(os.getenv("LIVE_MAX_DAILY_LOSS", "-50")))
    
    # Отключаем GPU для предсказаний
    tf.config.set_visible_devices([], 'GPU')

    # --- Запуск провайдеров быстрых цен (Coinbase anchor + Binance lead) ---
    providers = [
        FastExchangeProvider("binance", "wss://stream.binance.com:9443", "BTC", aggregator.update),
        FastExchangeProvider("coinbase", "wss://ws-feed.exchange.coinbase.com", "BTC-USD", aggregator.update)
    ]
    for p in providers:
        asyncio.create_task(p.connect())

    current_token_id = None
    poly_book = None
    last_stats_time = asyncio.get_event_loop().time()
    last_pulse_time = 0
    last_lstm_time = 0
    last_book_pull_time = 0
    forecast = 0.0
    
    logging.info("🔥 Система запущена. Ожидание первого слота Polymarket...")

    try:
        while True:
            now = asyncio.get_event_loop().time()
            
            # 1. Авто-переключение слота (проверка раз в 10 сек)
            if int(now) % 10 == 0:
                ts = selector.get_current_slot_timestamp()
                slug = selector.format_slug(ts)
                token_id, question = await selector.fetch_token_id(slug)

                if token_id and token_id != current_token_id:
                    logging.info(f"🎯 Смена рынка: {question}")
                    current_token_id = token_id
                    # Используем RTDS (оракул) как в bot2.py для стабильности
                    poly_book = PolyOrderBook(symbol="bitcoin") 
                    asyncio.create_task(poly_book.connect())

            # 2. Получение данных
            fast_price = aggregator.get_weighted_price()
            primary_data = aggregator.get_primary_history()
            
            # 3. Инференс LSTM (раз в 1 секунду, чтобы не грузить CPU)
            if primary_data and now - last_lstm_time > 1.0:
                forecast = await lstm.predict(primary_data)
                last_lstm_time = now

            # Fast-start fallback: keep forecast on realistic price scale before warmup.
            if fast_price and (forecast <= 0 or abs(forecast - fast_price) > 0.2 * fast_price):
                forecast = float(fast_price)

            # 4. Анализ и "Пульс"
            if fast_price and poly_book and poly_book.book['mid'] > 0:
                if current_token_id and now - last_book_pull_time > 0.5:
                    try:
                        ob = await asyncio.to_thread(live_exec.get_orderbook_snapshot, current_token_id, 5)
                        best_bid = float(ob.get("best_bid", 0.0))
                        best_ask = float(ob.get("best_ask", 0.0))
                        spread = best_ask - best_bid
                        is_valid_book = (
                            best_bid > 0.0
                            and best_ask > best_bid
                            and best_ask < 1.0
                            and 0.0 < spread < 0.20
                        )
                        if is_valid_book:
                            poly_book.book["ask"] = ob["best_ask"]
                            poly_book.book["bid"] = ob["best_bid"]
                            poly_book.book["mid"] = (ob["best_bid"] + ob["best_ask"]) / 2.0
                            poly_book.book["ask_size_top"] = ob["ask_size_top"]
                            poly_book.book["bid_size_top"] = ob["bid_size_top"]
                    except Exception:
                        pass
                    last_book_pull_time = now

                aggregator.add_history(fast_price)
                zscore = aggregator.get_zscore()
                # Визуальный пульс в консоль
                if now - last_pulse_time > PULSE_INTERVAL:
                    diff = fast_price - poly_book.book['mid']
                    trend = engine.get_trend_state()
                    bid_size = float(poly_book.book.get("bid_size_top", 1.0))
                    ask_size = float(poly_book.book.get("ask_size_top", 1.0))
                    imbalance = bid_size / (bid_size + ask_size + 1e-9)
                    upnl = pnl.get_unrealized_pnl(poly_book.book["mid"])
                    print(
                        f"DEBUG: Fast: {fast_price:.2f} | Poly: {poly_book.book['mid']:.2f} | "
                        f"Diff: {diff:+.2f} | Z: {zscore:+.2f} | "
                        f"Trend: {trend['trend']} s={trend['speed']:+.2f} d={trend['depth']:.2f} a={trend['age']:.1f}s | "
                        f"Imb: {imbalance:.2f} | uPnL: {upnl:+.2f}$ | Forecast: {forecast:.2f}",
                        flush=True,
                    )
                    last_pulse_time = now
                
                # Попытка совершить сделку
                await engine.process_tick(
                    fast_price=fast_price,
                    poly_orderbook=poly_book.book,
                    price_history=list(primary_data) if primary_data else [],
                    lstm_forecast=forecast,
                    zscore=zscore,
                )
                if LIVE_MODE and current_token_id and live_risk.can_trade():
                    live_signal = engine.generate_live_signal(fast_price, poly_book.book["mid"], zscore)
                    if live_signal:
                        await live_exec.execute(live_signal, current_token_id)
            elif now - last_pulse_time > PULSE_INTERVAL:
                logging.warning("⏳ Ожидание полной синхронизации данных (Coinbase/Poly)...")
                last_pulse_time = now

            # 5. Вывод статистики
            if now - last_stats_time > STATS_INTERVAL:
                stats.show_report()
                last_stats_time = now

            # HFT частота 20Гц
            await asyncio.sleep(0.05)

    except KeyboardInterrupt:
        print("\n🛑 Остановка пользователем...")
        stats.show_report()
    except Exception as e:
        logging.error("💥 КРИТИЧЕСКАЯ ОШИБКА В ГЛАВНОМ ЦИКЛЕ")
        # Выводит подробный Traceback (стек вызовов)
        logging.error(traceback.format_exc())
        
        # Дополнительный дебаг состояния данных перед падением
        try:
            bp = aggregator.data.get("coinbase")
            pp = poly_book.book if poly_book else "None"
            logging.debug(f"DEBUG DATA AT CRASH -> Coinbase: {bp} | Poly: {pp}")
        except:
            pass
            
        stats.show_report()
        
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass