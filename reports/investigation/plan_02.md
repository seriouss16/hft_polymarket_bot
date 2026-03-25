### Plan 02 — Pulse entry (2nd–3rd STRONG JUMP) & trend-cross + book pullback exit

#### English (implementation reference)

**1. Observation**  
Logs (e.g. `11:10:03–11:10:09`) show repeated `AGGRESSIVE ENTRY candidate` and `STRONG JUMP` lines while the fill often lands late in the impulse. The position then rides until `PNL_SL` even though a reversal is already visible.

**2. Hypothesis**  
The first `STRONG JUMP` is often noise; entering after a long run of jumps is late. A more robust approach: require a prior aggressive context, enter only on the **2nd or 3rd** `STRONG JUMP` in the same direction within a short window, and exit early when a **trend cross** against the position combines with a **~5% adverse move** in the relevant outcome price on the book.

**3. Rules (implemented in `core/engine.py`)**  
1. **Precondition:** `AGGRESSIVE ENTRY` magnitude (`_is_aggressive_oracle_edge`).  
2. **Pulse:** Count consecutive `STRONG JUMP` ticks (`_is_strong_jump_edge` = `buy_edge * aggressive_mult * HFT_STRONG_JUMP_MULT`).  
3. **Entry:** Only when `jump_count ∈ [HFT_ENTRY_JUMP_MIN, HFT_ENTRY_JUMP_MAX]` (default 2–3) inside `HFT_ENTRY_WINDOW_MS` after the first strong jump in the sequence.  
4. **Integrity:** Reset pulse on edge sign change, trend cross, window expiry, or non-aggressive edge.  
5. **Early exit:** After a trend cross **against** the position, arm exit; if the exit-side bid has pulled back by at least `HFT_EXIT_BOOK_PULLBACK_PCT` vs entry ask for **that outcome**, for `HFT_EXIT_PULLBACK_CONFIRM_TICKS` consecutive ticks (anti-spoof), close with reason `TREND_CROSS_BOOK_PULLBACK`.  
6. **Fallback:** Existing `PNL_SL` / reaction exits unchanged.

**4. Environment variables**

| Variable | Default | Meaning |
|----------|---------|---------|
| `HFT_JUMP_SEQUENCE_ENABLED` | `1` | Enable pulse entry path. |
| `HFT_ENTRY_JUMP_MIN` | `2` | Minimum strong-jump index to allow entry. |
| `HFT_ENTRY_JUMP_MAX` | `3` | Maximum strong-jump index; beyond that pulse resets (one recursion re-starts count). |
| `HFT_ENTRY_WINDOW_MS` | `1500` | Max time from first strong jump in sequence. |
| `HFT_JUMP_DIRECTION_STRICT` | `1` | Require edge sign aligned with `trend`. |
| `HFT_STRONG_JUMP_MULT` | `1.2` | Same scale as former `aggressive * 1.2` strong jump. |
| `HFT_TREND_CROSS_EXIT_ENABLED` | `1` | Enable trend-cross + book pullback exit. |
| `HFT_EXIT_BOOK_PULLBACK_PCT` | `0.05` | 5% adverse move vs entry ask for held token. |
| `HFT_EXIT_PULLBACK_CONFIRM_TICKS` | `2` | Consecutive ticks confirming pullback. |

**5. KPI targets**  
Fewer entries after the impulse peak; lower average loss per trade via earlier exit; lower max drawdown at similar trade count.

---

#### Русский (тот же план)

**1. Наблюдение**  
В логах серия `AGGRESSIVE ENTRY` и `STRONG JUMP`, фактический вход часто ближе к затуханию импульса; выход по `PNL_SL` при уже видимом развороте.

**2. Гипотеза**  
Вход на первом сильном прыжке шумный, слишком поздний — после длинной серии. Вариант: импульс подтверждается агрессивным контекстом; вход на **2–3-м** `STRONG JUMP` в окне времени; ранний выход при **пересечении тренда** против позиции и **откате стакана** по направлению позиции на **≥5%** (с подтверждением несколькими тиками).

**3. Реализация**  
См. `hft_bot/core/engine.py`: счётчик импульса `_pulse_sequence_after_tick`, вход `_pulse_sequence_entry_side`, выход `TREND_CROSS_BOOK_PULLBACK`, сброс при кроссе/окне/смене знака `edge`. Параметры — таблица выше.

**4. Проверка**  
Прогон симуляции, сравнение доли входов на 2–3-м прыжке, доли выходов `TREND_CROSS_BOOK_PULLBACK` и влияния на просадку.
