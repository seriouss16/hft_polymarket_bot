import csv
import logging
import time
from collections import Counter
from datetime import datetime
from pathlib import Path


class StatsCollector:
    """Aggregate PnL metrics and print session / shutdown reports."""

    def __init__(self, pnl_tracker):
        self.pnl = pnl_tracker
        self.started_ts = time.time()

    def show_report(self):
        """Print compact PnL summary to stdout (legacy block format)."""
        now_ts = time.time()
        win_rate = (self.pnl.wins / self.pnl.trades_count * 100) if self.pnl.trades_count > 0 else 0
        roi = ((self.pnl.balance - self.pnl.initial_balance) / self.pnl.initial_balance) * 100
        cooldown_until = float(getattr(self.pnl, "regime_cooldown_until", 0.0) or 0.0)
        started_at = datetime.fromtimestamp(self.started_ts).isoformat(timespec="seconds")
        report_at = datetime.fromtimestamp(now_ts).isoformat(timespec="seconds")
        uptime_min = (now_ts - self.started_ts) / 60.0

        report = [
            "\n" + "=" * 45,
            f"📊 ОТЧЕТ ПО ЭФФЕКТИВНОСТИ (HFT SIM)",
            "=" * 45,
            f"🕒 Старт сессии:      {started_at}",
            f"🧾 Время отчета:      {report_at}",
            f"⏱️ Аптайм:            {uptime_min:>10.1f} min",
            f"💰 Текущий баланс:    {self.pnl.balance:>10.2f} USD",
            f"📈 Чистая прибыль:    {self.pnl.total_pnl:>10.2f} USD ({roi:+.2f}%)",
            f"🔄 Всего сделок:      {self.pnl.trades_count:>10}",
            f"🎯 Процент побед:     {win_rate:>10.1f}%",
            f"📉 Макс. просадка:    {self.pnl.max_drawdown*100:>10.1f}%",
            f"📦 В позиции:         {'ДА' if self.pnl.inventory > 0 else 'НЕТ'}",
            f"⏸️ Regime cooldown:   {cooldown_until:>10.0f} (unix ts)",
        ]
        sp = getattr(self.pnl, "strategy_performance", None)
        if sp is not None and sp.slices:
            report.append("📊 По срезам (strategy:profile), реализовано:")
            for key in sorted(sp.slices.keys()):
                sl = sp.slices[key]
                wr = (sl.wins / sl.trades * 100.0) if sl.trades > 0 else 0.0
                report.append(
                    f"   {key:<30} n={sl.trades:>3}  WR={wr:>5.1f}%  PnL={sl.pnl_sum:>+9.2f} USD"
                )
            report.append(f"📊 Сумма по срезам:            {sp.total_pnl_all_keys():>+10.2f} USD")
        report.append("=" * 45 + "\n")

        text = "\n".join(report)
        logging.info(text)
        logging.info(
            "STATS snapshot: balance=%.2f pnl=%.2f trades=%d win=%.1f%% dd=%.1f%% inv=%s",
            self.pnl.balance,
            self.pnl.total_pnl,
            self.pnl.trades_count,
            win_rate,
            self.pnl.max_drawdown * 100.0,
            "yes" if self.pnl.inventory > 0 else "no",
        )

    def _journal_aggregates(self, journal_path: Path | None):
        """Return (rows_n, pnl_csv_sum, exit_reason_counts) from journal CSV if present."""
        if journal_path is None or not journal_path.is_file() or journal_path.stat().st_size == 0:
            return 0, 0.0, Counter()
        pnl_sum = 0.0
        n = 0
        reasons = Counter()
        try:
            with journal_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    n += 1
                    try:
                        pnl_sum += float(row.get("pnl") or 0.0)
                    except (TypeError, ValueError):
                        logging.warning(
                            "Bad pnl value in journal row %d: %r", n, row.get("pnl"),
                        )
                    r = str(row.get("exit_reason") or "").strip() or "(empty)"
                    reasons[r] += 1
        except OSError as exc:
            logging.warning("Cannot read trade journal %s: %s", journal_path, exc)
            return 0, 0.0, Counter()
        return n, pnl_sum, reasons

    def show_final_report(self, journal_path=None, shutdown_reason: str = "shutdown"):
        """Print full session summary with a tabular report and optional journal breakdown."""
        self.show_report()
        now_ts = time.time()
        win_rate = (self.pnl.wins / self.pnl.trades_count * 100) if self.pnl.trades_count > 0 else 0
        roi = ((self.pnl.balance - self.pnl.initial_balance) / self.pnl.initial_balance) * 100
        losses = self.pnl.trades_count - self.pnl.wins
        started_at = datetime.fromtimestamp(self.started_ts).isoformat(timespec="seconds")
        report_at = datetime.fromtimestamp(now_ts).isoformat(timespec="seconds")
        uptime_min = (now_ts - self.started_ts) / 60.0

        jp = Path(journal_path) if journal_path else None
        j_rows, j_pnl, j_reasons = self._journal_aggregates(jp)

        w_label = 28
        w_val = 22

        def row(label: str, val: str) -> str:
            return f"| {label:<{w_label}} | {val:>{w_val}} |"

        sep = "+" + "-" * (w_label + 2) + "+" + "-" * (w_val + 2) + "+"

        lines = [
            "",
            sep,
            row("Итоговая таблица (сессия)", ""),
            sep,
            row("Причина завершения", shutdown_reason),
            row("Старт сессии", started_at),
            row("Время отчета", report_at),
            row("Аптайм, min", f"{uptime_min:.1f}"),
            row("Начальный баланс USD", f"{self.pnl.initial_balance:.2f}"),
            row("Текущий баланс USD", f"{self.pnl.balance:.2f}"),
            row("Чистая прибыль USD", f"{self.pnl.total_pnl:+.2f}"),
            row("ROI %", f"{roi:+.2f}"),
            row("Закрытых сделок (sim)", str(self.pnl.trades_count)),
            row("Побед / поражений", f"{self.pnl.wins} / {losses}"),
            row("Win rate %", f"{win_rate:.1f}"),
            row("Макс. просадка %", f"{self.pnl.max_drawdown*100:.1f}"),
            row("Открытая позиция", "да" if self.pnl.inventory > 0 else "нет"),
            sep,
        ]

        if jp is not None:
            lines.append(row("Журнал (файл)", str(jp)))
            lines.append(row("Строк в журнале (CSV)", str(j_rows)))
            lines.append(row("Сумма pnl по журналу", f"{j_pnl:+.4f}"))
            lines.append(sep)
            lines.append(row("exit_reason (журнал)", "count"))
            lines.append(sep)
            for reason, cnt in sorted(j_reasons.items(), key=lambda x: (-x[1], x[0])):
                lines.append(row(reason[: w_label], str(cnt)))
            lines.append(sep)

        block = "\n".join(lines)
        print(block)
        logging.info("Session final report:\n%s", block)
