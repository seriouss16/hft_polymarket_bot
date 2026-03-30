"""Position sizing, PnL line targets, and trailing TP/SL helpers for HFTEngine.

Extracted from engine.py to keep HFTEngine smaller; behavior is unchanged.
"""

from __future__ import annotations

from typing import Protocol


class _PnLBalance(Protocol):
    balance: float


def deposit_trade_notional(
    pnl: _PnLBalance,
    deposit_usd: float,
    fixed_trade_usd: float,
    trade_pct_of_deposit: float,
) -> float:
    """Return target trade USD based on current live balance and sizing mode."""
    current_balance = max(0.0, pnl.balance)
    if current_balance <= 0.0:
        return 0.0
    fixed = max(0.0, fixed_trade_usd)
    pct = trade_pct_of_deposit
    if pct <= 0.0:
        return min(fixed, current_balance)
    profit_above_start = max(0.0, current_balance - deposit_usd)
    bonus = profit_above_start * (pct / 100.0)
    size = fixed + bonus
    return max(0.0, min(size, current_balance))


def tier_dynamic_amount(
    exec_price: float,
    *,
    dynamic_min_exec_price: float,
    dynamic_floor_notional_usd: float,
    dynamic_cheap_price_below: float,
    dynamic_rich_price_above: float,
    dynamic_amount_min_usd: float,
    dynamic_amount_max_usd: float,
    dynamic_amount_cheap_usd: float,
    dynamic_amount_rich_usd: float,
    dynamic_risk_per_tick_usd: float,
) -> float:
    """Compute notional from price tier and risk-per-tick before deposit cap."""
    px = float(exec_price)
    if px < dynamic_min_exec_price:
        return dynamic_floor_notional_usd
    if px < dynamic_cheap_price_below:
        return min(
            dynamic_amount_max_usd,
            max(dynamic_amount_min_usd, dynamic_amount_cheap_usd),
        )
    if px > dynamic_rich_price_above:
        return min(
            dynamic_amount_max_usd,
            max(dynamic_amount_min_usd, dynamic_amount_rich_usd),
        )
    tick = 0.01
    shares = dynamic_risk_per_tick_usd / tick
    amount = shares * px
    return min(
        dynamic_amount_max_usd,
        max(dynamic_amount_min_usd, amount),
    )


def calc_dynamic_amount(
    exec_price: float,
    pnl: _PnLBalance,
    *,
    deposit_usd: float,
    fixed_trade_usd: float,
    trade_pct_of_deposit: float,
    dynamic_amount_max_usd: float,
    dynamic_amount_min_usd: float,
    dynamic_min_exec_price: float,
    dynamic_floor_notional_usd: float,
    dynamic_cheap_price_below: float,
    dynamic_rich_price_above: float,
    dynamic_amount_cheap_usd: float,
    dynamic_amount_rich_usd: float,
    dynamic_risk_per_tick_usd: float,
) -> float:
    """Size notional USD: tier estimate capped by deposit rules and dynamic min/max."""
    base = deposit_trade_notional(pnl, deposit_usd, fixed_trade_usd, trade_pct_of_deposit)
    tier = tier_dynamic_amount(
        exec_price,
        dynamic_min_exec_price=dynamic_min_exec_price,
        dynamic_floor_notional_usd=dynamic_floor_notional_usd,
        dynamic_cheap_price_below=dynamic_cheap_price_below,
        dynamic_rich_price_above=dynamic_rich_price_above,
        dynamic_amount_min_usd=dynamic_amount_min_usd,
        dynamic_amount_max_usd=dynamic_amount_max_usd,
        dynamic_amount_cheap_usd=dynamic_amount_cheap_usd,
        dynamic_amount_rich_usd=dynamic_amount_rich_usd,
        dynamic_risk_per_tick_usd=dynamic_risk_per_tick_usd,
    )
    amount = min(base, tier)
    amount = min(amount, dynamic_amount_max_usd)
    if base < dynamic_amount_min_usd:
        floor = base
    else:
        floor = dynamic_amount_min_usd
    return max(floor, amount)


class _PnLPosition(Protocol):
    inventory: float
    entry_price: float


def position_notional_usd(pnl: _PnLPosition) -> float:
    """Return absolute position notional in USD for percent-based TP/SL."""
    inv = float(pnl.inventory or 0.0)
    ep = float(pnl.entry_price or 0.0)
    return abs(inv * ep)


def pnl_target_and_stop_lines(
    pnl: _PnLPosition,
    *,
    pnl_tp_pct: float,
    target_profit_usd: float,
    pnl_sl_pct: float,
    stop_loss_usd: float,
) -> tuple[float, float]:
    """Return (take_profit_usd, stop_loss_usd) thresholds from percent or fixed env."""
    n = position_notional_usd(pnl)
    if pnl_tp_pct > 0.0:
        tp = n * pnl_tp_pct
    else:
        tp = target_profit_usd
    if pnl_sl_pct > 0.0:
        sl = n * pnl_sl_pct
    else:
        sl = stop_loss_usd
    return tp, sl


def hold_met(min_hold_sec: float, hold_sec: float) -> bool:
    """Return True when min-hold delay does not apply or is satisfied."""
    return min_hold_sec <= 0.0 or hold_sec >= min_hold_sec


class _TrailingHost(Protocol):
    _peak_unrealized: float
    _trailing_sl_floor: float | None
    min_hold_sec: float
    trailing_tp_enabled: bool
    trailing_tp_activate_usd: float
    trailing_tp_pullback_pct: float
    trailing_tp_min_pullback_usd: float
    trailing_sl_enabled: bool
    trailing_sl_breakeven_at_usd: float
    trailing_sl_step_usd: float
    trailing_sl_step_lock_pct: float


def update_trailing_state(eng: _TrailingHost, unrealized: float) -> None:
    """Track peak unrealized PnL and ratchet the trailing SL floor upward."""
    if unrealized > eng._peak_unrealized:
        eng._peak_unrealized = unrealized
    if not eng.trailing_sl_enabled:
        return
    if eng._peak_unrealized >= eng.trailing_sl_breakeven_at_usd:
        new_floor = 0.0
        if eng.trailing_sl_step_usd > 0.0:
            steps_above = (
                eng._peak_unrealized - eng.trailing_sl_breakeven_at_usd
            ) / eng.trailing_sl_step_usd
            new_floor = int(steps_above) * eng.trailing_sl_step_usd * eng.trailing_sl_step_lock_pct
        if eng._trailing_sl_floor is None or new_floor > eng._trailing_sl_floor:
            eng._trailing_sl_floor = new_floor


def trailing_tp_triggered(eng: _TrailingHost, unrealized: float, hold_sec: float) -> bool:
    """Return True when profit has pulled back from peak beyond the trailing threshold."""
    if not eng.trailing_tp_enabled:
        return False
    if not hold_met(eng.min_hold_sec, hold_sec):
        return False
    if eng._peak_unrealized < eng.trailing_tp_activate_usd:
        return False
    pullback = eng._peak_unrealized - unrealized
    threshold = max(
        eng._peak_unrealized * eng.trailing_tp_pullback_pct,
        eng.trailing_tp_min_pullback_usd,
    )
    return pullback >= threshold


def trailing_sl_triggered(eng: _TrailingHost, unrealized: float, hold_sec: float) -> bool:
    """Return True when unrealized PnL drops below the ratcheted trailing SL floor."""
    if not eng.trailing_sl_enabled:
        return False
    if not hold_met(eng.min_hold_sec, hold_sec):
        return False
    if eng._trailing_sl_floor is None:
        return False
    return unrealized < eng._trailing_sl_floor


def reset_trailing_state(eng: _TrailingHost) -> None:
    """Clear trailing tracking on position close or new entry."""
    eng._peak_unrealized = 0.0
    eng._trailing_sl_floor = None
