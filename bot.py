import os
import sys
import asyncio
import logging
import traceback
import time
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
from core.risk_engine import RiskEngine
from data.aggregator import FastPriceAggregator
from data.providers import FastExchangeProvider
from data.poly_clob import PolyOrderBook
from ml.model import AsyncLSTMPredictor
from utils.stats import StatsCollector
from utils.trade_journal import TradeJournal

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
    USE_SMART_FAST = os.getenv("USE_SMART_FAST", "0") == "1"
    SYMBOL = "BTC"
    STATS_INTERVAL = 60  # Periodic stats report (seconds).
    PULSE_INTERVAL = float(os.getenv("PULSE_INTERVAL_SEC", "0.02"))
    MAIN_LOOP_SLEEP = float(os.getenv("HFT_LOOP_SLEEP_SEC", "0.002"))
    CLOB_PULL_INTERVAL = float(os.getenv("CLOB_BOOK_PULL_SEC", "0.15"))
    LSTM_MIN_INTERVAL = float(os.getenv("LSTM_INFERENCE_SEC", "1.0"))
    
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
    risk = RiskEngine(
        max_drawdown_pct=float(os.getenv("MAX_DRAWDOWN_PCT", "0.12")),
        max_position_pct=float(os.getenv("MAX_POSITION_PCT", "0.10")),
        loss_cooldown_sec=float(os.getenv("LOSS_COOLDOWN_SEC", "12")),
    )
    journal = TradeJournal(path=os.getenv("TRADE_JOURNAL_PATH", "reports/trade_journal.csv"))
    
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
            if USE_SMART_FAST:
                fast_price = aggregator.get_weighted_price()
            else:
                fast_price = aggregator.get_coinbase_price() or aggregator.get_weighted_price()
            primary_data = aggregator.get_primary_history()
            
            # 3. Инференс LSTM (раз в 1 секунду, чтобы не грузить CPU)
            if primary_data and now - last_lstm_time >= LSTM_MIN_INTERVAL:
                forecast = await lstm.predict(primary_data)
                last_lstm_time = now

            # Fast-start fallback: keep forecast on realistic price scale before warmup.
            if fast_price and (forecast <= 0 or abs(forecast - fast_price) > 0.2 * fast_price):
                forecast = float(fast_price)

            # 4. Анализ и "Пульс"
            poly_btc = 0.0
            if poly_book is not None:
                poly_btc = float(
                    poly_book.book.get("btc_oracle")
                    or poly_book.book.get("mid")
                    or 0.0
                )
            if fast_price and poly_book is not None and poly_btc > 0:
                if current_token_id and now - last_book_pull_time >= CLOB_PULL_INTERVAL:
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
                            poly_book.book["ask_size_top"] = ob["ask_size_top"]
                            poly_book.book["bid_size_top"] = ob["bid_size_top"]
                    except Exception:
                        pass
                    last_book_pull_time = now

                aggregator.add_history(fast_price)
                zscore = aggregator.get_zscore()
                latency_ms = aggregator.get_latency_ms(float(poly_book.book.get("ts", 0.0)))
                equity = pnl.balance + pnl.get_unrealized_pnl(poly_book.book)
                risk.update_equity(equity)
                trade_allowed = risk.can_trade(time.time(), equity)
                # Визуальный пульс в консоль
                if now - last_pulse_time > PULSE_INTERVAL:
                    diff = fast_price - poly_btc
                    trend = engine.get_trend_state()
                    bid_size = float(poly_book.book.get("bid_size_top", 1.0))
                    ask_size = float(poly_book.book.get("ask_size_top", 1.0))
                    imbalance = bid_size / (bid_size + ask_size + 1e-9)
                    upnl = pnl.get_unrealized_pnl(poly_book.book)
                    rsi_st = engine.get_rsi_v5_state()
                    cb_px = aggregator.get_coinbase_price()
                    bn_px = aggregator.get_binance_price()
                    cb_s = f"{cb_px:.2f}" if cb_px else "n/a"
                    bn_s = f"{bn_px:.2f}" if bn_px else "n/a"
                    print(
                        f"DEBUG: Fast: {fast_price:.2f} (CB {cb_s} BNC {bn_s} smart={USE_SMART_FAST}) | "
                        f"PolyRTDS: {poly_btc:.2f} | "
                        f"Diff: {diff:+.2f} | Z: {zscore:+.2f} | "
                        f"Trend: {trend['trend']} s={trend['speed']:+.2f} d={trend['depth']:.2f} a={trend['age']:.1f}s | "
                        f"RSI: {rsi_st['rsi']:.1f} [{rsi_st['lower']:.0f}-{rsi_st['upper']:.0f}] "
                        f"Δ={rsi_st['slope']:+.2f} | "
                        f"Imb: {imbalance:.2f} | uPnL: {upnl:+.2f}$ | Lat: {latency_ms:+.0f}ms | "
                        f"DD: {risk.drawdown_pct(equity)*100:.2f}% | Gate: {'ON' if trade_allowed else 'OFF'} | "
                        f"Forecast: {forecast:.2f}",
                        flush=True,
                    )
                    last_pulse_time = now
                
                # Попытка совершить сделку
                decision = await engine.process_tick(
                    fast_price=fast_price,
                    poly_orderbook=poly_book.book,
                    price_history=list(primary_data) if primary_data else [],
                    lstm_forecast=forecast,
                    zscore=zscore,
                    latency_ms=latency_ms,
                    recent_pnl=pnl.last_realized_pnl,
                    meta_enabled=trade_allowed,
                )
                if isinstance(decision, dict) and decision.get("event") == "CLOSE":
                    risk.on_trade_closed(float(decision.get("pnl", 0.0)), time.time())
                    _rs = engine.get_rsi_v5_state()
                    journal.append(
                        {
                            "ts": time.time(),
                            "side": decision.get("side"),
                            "entry_edge": decision.get("entry_edge"),
                            "exit_edge": decision.get("exit_edge"),
                            "duration_sec": decision.get("duration_sec"),
                            "entry_trend": decision.get("entry_trend"),
                            "entry_speed": decision.get("entry_speed"),
                            "entry_depth": decision.get("entry_depth"),
                            "entry_imbalance": decision.get("entry_imbalance"),
                            "latency_ms": decision.get("latency_ms"),
                            "pnl": decision.get("pnl"),
                            "exit_reason": decision.get("reason"),
                            "exit_rsi": _rs.get("rsi"),
                            "rsi_band_lower": _rs.get("lower"),
                            "rsi_band_upper": _rs.get("upper"),
                            "rsi_slope": _rs.get("slope"),
                        }
                    )
                if LIVE_MODE and current_token_id and live_risk.can_trade():
                    live_signal = engine.generate_live_signal(fast_price, poly_btc, zscore)
                    if live_signal:
                        await live_exec.execute(live_signal, current_token_id)
            elif now - last_pulse_time > PULSE_INTERVAL:
                logging.warning("⏳ Ожидание полной синхронизации данных (Coinbase/Poly)...")
                last_pulse_time = now

            # 5. Вывод статистики
            if now - last_stats_time > STATS_INTERVAL:
                stats.show_report()
                last_stats_time = now

            await asyncio.sleep(MAIN_LOOP_SLEEP)

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