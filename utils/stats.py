"""Session PnL reporting and journal aggregation for HFT bot."""

import csv
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List


def _median_avg(values: List[float]) -> float:
    """Calculate median average: for odd n take 3 central values, for even n take 4 central values.
    
    For odd count: take 3 central values and average them.
    For even count: take 4 central values and average them.
    If n < 3 (odd) or n < 4 (even), return arithmetic mean of all values.
    
    Example: [1,2,3,4,5,6,7] (n=7, odd) -> (3+4+5)/3 = 4.0
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n % 2 == 1:  # odd
        if n < 3:
            return sum(sorted_vals) / n
        mid = n // 2
        central = sorted_vals[mid-1:mid+2]  # 3 values: mid-1, mid, mid+1
    else:  # even
        if n < 4:
            return sum(sorted_vals) / n
        mid = n // 2
        # For even, take 4 central: mid-2, mid-1, mid, mid+1 (indices)
        central = sorted_vals[mid-2:mid+2]
    return sum(central) / len(central)


@dataclass
class _JournalStats:
    """Derived statistics computed from the trade journal CSV."""

    rows: int = 0
    pnl_sum: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    win_pnl_sum: float = 0.0
    loss_pnl_sum: float = 0.0
    win_pnl_values: list = field(default_factory=list)
    loss_pnl_values: list = field(default_factory=list)
    exit_reasons: Counter = field(default_factory=Counter)

    @property
    def avg_pnl(self) -> float:
        """Return mean PnL per trade."""
        return self.pnl_sum / self.rows if self.rows else 0.0

    @property
    def avg_win(self) -> float:
        """Return mean PnL for winning trades."""
        return self.win_pnl_sum / self.win_count if self.win_count else 0.0

    @property
    def avg_loss(self) -> float:
        """Return mean PnL for losing trades."""
        return self.loss_pnl_sum / self.loss_count if self.loss_count else 0.0

    @property
    def weighted_avg_pnl(self) -> float:
        """Return size-weighted average PnL across all trades."""
        return self._weighted_avg(self.win_pnl_values + self.loss_pnl_values)

    @property
    def weighted_avg_win(self) -> float:
        """Return size-weighted average PnL for winning trades."""
        return self._weighted_avg(self.win_pnl_values)

    @property
    def weighted_avg_loss(self) -> float:
        """Return size-weighted average PnL for losing trades."""
        return self._weighted_avg(self.loss_pnl_values)

    @staticmethod
    def _weighted_avg(values: list) -> float:
        """Return weighted mean where each value is weighted by its absolute magnitude."""
        if not values:
            return 0.0
        weights = [abs(v) for v in values]
        total_w = sum(weights)
        if total_w <= 0.0:
            return 0.0
        return sum(v * w for v, w in zip(values, weights)) / total_w

    @property
    def profit_factor(self) -> float:
        """Return ratio of gross profit to gross loss magnitude (>1 is profitable)."""
        if self.loss_pnl_sum == 0.0:
            return float("inf") if self.win_pnl_sum > 0.0 else 0.0
        return self.win_pnl_sum / abs(self.loss_pnl_sum)

    @property
    def win_rate_pct(self) -> float:
        """Return win rate as a percentage."""
        return self.win_count / self.rows * 100.0 if self.rows else 0.0

    @property
    def median_avg_pnl(self) -> float:
        """Return median average of all PnL values."""
        all_values = self.win_pnl_values + self.loss_pnl_values
        return _median_avg(all_values)

    @property
    def median_avg_win(self) -> float:
        """Return median average of winning trades."""
        return _median_avg(self.win_pnl_values)

    @property
    def median_avg_loss(self) -> float:
        """Return median average of losing trades."""
        return _median_avg(self.loss_pnl_values)


class StatsCollector:
    """Aggregate PnL metrics and print session / shutdown reports."""

    def __init__(self, pnl_tracker):
        """Initialize with a PnLTracker instance."""
        self.pnl = pnl_tracker
        self.started_ts = time.time()

    @staticmethod
    def _format_regime_cooldown(cooldown_until: float, now_ts: float) -> str:
        """Return human-readable regime cooldown line for reports."""
        if cooldown_until <= 0.0:
            return "none"
        if now_ts >= cooldown_until:
            return "none (expired)"
        remaining = cooldown_until - now_ts
        until_iso = datetime.fromtimestamp(cooldown_until).isoformat(timespec="seconds")
        return f"{until_iso} (in {remaining:.0f}s)"

    @staticmethod
    def _session_mode_label() -> str:
        """Return human-readable session mode string for the report header."""
        day_mode = (os.getenv("DAY_MODE") or "0").strip()
        night_mode = (os.getenv("NIGHT_MODE") or "0").strip()
        forced = (day_mode == "1") != (night_mode == "1")
        if forced:
            active = "DAY ☀️" if day_mode == "1" else "NIGHT 🌙"
            return f"{active} [принудительный]"
        from core.session_profile import current_profile_name  # noqa: PLC0415
        name = current_profile_name()
        label = "DAY ☀️" if name == "day" else "NIGHT 🌙"
        return f"{label} [авто]"

    def show_report(self):
        """Print compact PnL summary to stdout (legacy block format)."""
        now_ts = time.time()
        win_rate = (self.pnl.wins / self.pnl.trades_count * 100) if self.pnl.trades_count > 0 else 0
        roi = ((self.pnl.balance - self.pnl.initial_balance) / self.pnl.initial_balance) * 100
        cooldown_until = float(getattr(self.pnl, "regime_cooldown_until", 0.0) or 0.0)
        started_at = datetime.fromtimestamp(self.started_ts).isoformat(timespec="seconds")
        report_at = datetime.fromtimestamp(now_ts).isoformat(timespec="seconds")
        uptime_min = (now_ts - self.started_ts) / 60.0
        losses = self.pnl.trades_count - self.pnl.wins
        avg_pnl = self.pnl.total_pnl / self.pnl.trades_count if self.pnl.trades_count else 0.0

        report = [
            "\n" + "=" * 45,
            "📊 ОТЧЕТ ПО ЭФФЕКТИВНОСТИ (HFT SIM)",
            "=" * 45,
            f"🕒 Старт сессии:      {started_at}",
            f"🧾 Время отчета:      {report_at}",
            f"⏱️ Аптайм:            {uptime_min:>10.1f} min",
            f"🗂️ Режим:             {self._session_mode_label()}",
            f"💰 Текущий баланс:    {self.pnl.balance:>10.2f} USD",
            f"📈 Чистая прибыль:    {self.pnl.total_pnl:>10.2f} USD ({roi:+.2f}%)",
            f"🔄 Всего сделок:      {self.pnl.trades_count:>10}",
            f"✅ Побед / ❌ Убытков: {self.pnl.wins:>4} / {losses:<4}",
            f"🎯 Win rate:          {win_rate:>10.1f}%",
            f"📊 Средняя на сделку: {avg_pnl:>+10.4f} USD",
            f"📉 Макс. просадка:    {self.pnl.max_drawdown*100:>10.1f}%",
            f"📦 В позиции:         {'YES' if self.pnl.inventory > 0 else 'NO'}",
            f"⏸️ Regime cooldown:   {self._format_regime_cooldown(cooldown_until, now_ts)}",
        ]
        sp = getattr(self.pnl, "strategy_performance", None)
        if sp is not None and sp.slices:
            report.append("📊 По срезам (strategy:profile), реализовано:")
            for key in sorted(sp.slices.keys()):
                sl = sp.slices[key]
                wr = (sl.wins / sl.trades * 100.0) if sl.trades > 0 else 0.0
                sl_avg = sl.pnl_sum / sl.trades if sl.trades else 0.0
                report.append(
                    f"   {key:<30} n={sl.trades:>3}  WR={wr:>5.1f}%  "
                    f"PnL={sl.pnl_sum:>+9.2f} USD  avg={sl_avg:>+7.4f}"
                )
            report.append(f"📊 Сумма по срезам:            {sp.total_pnl_all_keys():>+10.2f} USD")
        
        # Add median metrics from journal if available
        journal_path_str = os.getenv("TRADE_JOURNAL_PATH", "reports/trade_journal.csv")
        journal_path = Path(journal_path_str)
        if journal_path.is_file():
            js = self._journal_aggregates(journal_path)
            if js.rows > 0:
                report.append("📊 Медианные показатели (журнал):")
                report.append(f"   Медианная средняя (все):     {js.median_avg_pnl:>+10.4f} USD")
                report.append(f"   Медианная средняя (профит):  {js.median_avg_win:>+10.4f} USD")
                report.append(f"   Медианная средняя (убыток):  {js.median_avg_loss:>10.4f} USD")
        
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

    def _journal_aggregates(self, journal_path: Path | None) -> _JournalStats:
        """Return detailed statistics parsed from journal CSV."""
        from utils.trade_journal import _FIELDNAMES as _TJ_FIELDS  # noqa: PLC0415

        js = _JournalStats()
        if journal_path is None or not journal_path.is_file() or journal_path.stat().st_size == 0:
            return js
        try:
            with journal_path.open("r", encoding="utf-8", newline="") as f:
                first_line = f.readline().strip()
                has_header = first_line.startswith("ts,")
                if not has_header:
                    logging.warning(
                        "Trade journal %s has no header — using positional fieldnames.",
                        journal_path.name,
                    )
                f.seek(0)
                reader = csv.DictReader(
                    f,
                    fieldnames=None if has_header else _TJ_FIELDS,
                )
                for js.rows, row in enumerate(reader, start=1):
                    try:
                        pnl = float(row.get("pnl") or 0.0)
                    except (TypeError, ValueError):
                        logging.warning(
                            "Bad pnl value in journal row %d: %r", js.rows, row.get("pnl"),
                        )
                        pnl = 0.0
                    js.pnl_sum += pnl
                    if pnl > 0.0:
                        js.win_count += 1
                        js.win_pnl_sum += pnl
                        js.win_pnl_values.append(pnl)
                    else:
                        js.loss_count += 1
                        js.loss_pnl_sum += pnl
                        js.loss_pnl_values.append(pnl)
                    r = str(row.get("exit_reason") or "").strip() or "(empty)"
                    js.exit_reasons[r] += 1
        except OSError as exc:
            logging.warning("Cannot read trade journal %s: %s", journal_path, exc)
            return _JournalStats()
        return js

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
        js = self._journal_aggregates(jp)

        w_label = 32
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
            row("Режим", self._session_mode_label()),
            row("Старт сессии", started_at),
            row("Время отчета", report_at),
            row("Аптайм, min", f"{uptime_min:.1f}"),
            sep,
            row("Начальный баланс USD", f"{self.pnl.initial_balance:.2f}"),
            row("Текущий баланс USD", f"{self.pnl.balance:.2f}"),
            row("Чистая прибыль USD", f"{self.pnl.total_pnl:+.2f}"),
            row("ROI %", f"{roi:+.2f}"),
            row("Макс. просадка %", f"{self.pnl.max_drawdown*100:.1f}"),
            sep,
            row("Закрытых сделок (sim)", str(self.pnl.trades_count)),
            row("Побед / Убытков", f"{self.pnl.wins} / {losses}"),
            row("Win rate %", f"{win_rate:.1f}"),
            row("Открытая позиция", "да" if self.pnl.inventory > 0 else "нет"),
            sep,
        ]

        if js.rows > 0:
            lines += [
                row("--- Journal stats ---", ""),
                sep,
                row("Строк в журнале", str(js.rows)),
                row("Побед / Убытков", f"{js.win_count} / {js.loss_count}"),
                row("Win rate % (journal)", f"{js.win_rate_pct:.1f}"),
                row("Profit factor", f"{js.profit_factor:.2f}" if js.profit_factor != float('inf') else "∞"),
                sep,
                row("Сумма PnL (журнал)", f"{js.pnl_sum:+.4f} USD"),
                row("Сумма профитов", f"{js.win_pnl_sum:+.4f} USD"),
                row("Сумма убытков", f"{js.loss_pnl_sum:+.4f} USD"),
                sep,
                row("Средняя на сделку", f"{js.avg_pnl:+.4f} USD"),
                row("Средняя прибыльная", f"{js.avg_win:+.4f} USD"),
                row("Средняя убыточная", f"{js.avg_loss:+.4f} USD"),
                sep,
                row("Средневзвешенная (все)", f"{js.weighted_avg_pnl:+.4f} USD"),
                row("Средневзвешенная (профит)", f"{js.weighted_avg_win:+.4f} USD"),
                row("Средневзвешенная (убыток)", f"{js.weighted_avg_loss:+.4f} USD"),
                sep,
                row("Медианная средняя (все)", f"{js.median_avg_pnl:+.4f} USD"),
                row("Медианная средняя (профит)", f"{js.median_avg_win:+.4f} USD"),
                row("Медианная средняя (убыток)", f"{js.median_avg_loss:+.4f} USD"),
                sep,
            ]

            if jp is not None:
                lines.append(row("Журнал (файл)", jp.name))

            lines += [
                row("exit_reason", "count"),
                sep,
            ]
            for reason, cnt in sorted(js.exit_reasons.items(), key=lambda x: (-x[1], x[0])):
                lines.append(row(reason[:w_label], str(cnt)))
            lines.append(sep)
        elif jp is not None:
            lines += [
                row("Журнал (файл)", jp.name),
                row("Строк в журнале", "0"),
                sep,
            ]

        block = "\n".join(lines)
        print(block)
        logging.info("Session final report:\n%s", block)
