import os
import logging

class StatsCollector:
    def __init__(self, pnl_tracker):
        self.pnl = pnl_tracker

    def show_report(self):
        total_days = 1 # Для масштабирования в будущем
        win_rate = (self.pnl.wins / self.pnl.trades_count * 100) if self.pnl.trades_count > 0 else 0
        roi = ((self.pnl.balance - self.pnl.initial_balance) / self.pnl.initial_balance) * 100

        report = [
            "\n" + "="*45,
            f"📊 ОТЧЕТ ПО ЭФФЕКТИВНОСТИ (HFT SIM)",
            "="*45,
            f"💰 Текущий баланс:    {self.pnl.balance:>10.2f} USD",
            f"📈 Чистая прибыль:    {self.pnl.total_pnl:>10.2f} USD ({roi:+.2f}%)",
            f"🔄 Всего сделок:      {self.pnl.trades_count:>10}",
            f"🎯 Процент побед:     {win_rate:>10.1f}%",
            f"📉 Макс. просадка:    {self.pnl.max_drawdown*100:>10.1f}%",
            f"📦 В позиции:         {'ДА' if self.pnl.inventory > 0 else 'НЕТ'}",
            "="*45 + "\n"
        ]
        
        # Печатаем в консоль и дублируем в лог
        print("\n".join(report))