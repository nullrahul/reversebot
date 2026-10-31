# Comprehensive Quantitative Audit & Codebase Flaw Report

## Overview
This report details the findings from an exhaustive review of the `CHANGELOG.md` updates and the underlying strategy codebase (`decision.py`, `position_mgr.py`, `profiles.py`). The audit uncovered several critical logical flaws, silent telemetry bugs, and execution loopholes that degrade bot performance, breach capital limits, and poison analytical audits. 

---

## 1. FAK Cascade Budget Resizing Over-Allocation (Capital Breach)
### The Flaw
The v5.22 update introduced "budget-based resizing" for FAK cascading retries. However, the mechanism artificially forces over-allocation, breaking the strict flat $10.00 position sizing safety constraints.

### The Proof (Code Level)
In `src/strategy/position_mgr.py` (`open_position`), the cascade loop computes remaining shares based on `remaining_budget`:
```python
ideal_shares = remaining_budget / current_limit_price
target_shares = float(int(ideal_shares))

if target_shares < order_min_size: # order_min_size = 6.0
    if total_shares_filled > 0.0:
        if target_shares < 1.0:
            break
        else:
            remaining_shares = order_min_size # Flaw: Clamps UP to 6.0
```
**Example Scenario:**
- Original budget: $10.00. First order fills 18 shares at $0.50 (cost = $9.00). 
- `remaining_budget` = $1.00. 
- Price slips to $0.50 on the retry. `ideal_shares` = 2.0.
- Because `target_shares` (2.0) is `< 6.0` and `>= 1.0`, the logic forcefully clamps `remaining_shares` UP to 6.0.
- The bot buys 6.0 shares at $0.50, costing $3.00.
- **Total Position Cost:** $9.00 + $3.00 = **$12.00**. 
The bot routinely violates its own hard $10.00 capital limit under the guise of "bonus fills", compounding risk during high slippage.

---

## 2. REVERSAL Strategy "Deep Value Trap" (Missing Cap)
### The Flaw
The `REVERSAL` strategy mode exposes the bot to massive negative Risk:Reward drawdowns by buying deep-value opposite-side contracts at exorbitant prices (e.g., $0.80 - $0.99), completely unconstrained by safety ceilings.

### The Proof (Code Level)
In v5.17, an audit proved that fading trends at prices $\ge 0.50$ destroyed profitability. A hard 50¢ cap was explicitly added for `LOSS_FADE` entries:
```python
elif fade_entry_price is not None and fade_entry_price >= 0.50:
    logger.warning("... LOSS_FADE entry blocked: opposing price >= 0.50 ...")
```
However, the Option B1 `REVERSAL` mode (which executes the *exact same* mathematical fade strategy when rolling win rate drops below 45%) lacks this cap entirely.
Furthermore, in `src/strategy/decision.py`, both `REVERSAL` and `LOSS_FADE` are explicitly bypassed from the main bot's B3 Ceiling and Floor gates:
```python
if active_strategy not in ("LOSS_FADE", "REVERSAL"):
    # B3 gates applied here...
```
Because `REVERSAL` bypasses B3 gates and lacks the 50¢ fade cap, it is permitted to enter trades at $0.90, risking $9.00 to make $1.00 on a reversion bet. This is a severe architectural blind spot.

---

## 3. B3 Gate Telemetry Asymmetry (Corrupted Rejections DB)
### The Flaw
The empirical B3 Price Gates for `NO` side entries are recording the exact opposite of market reality into the SQLite `rejections` database. This destroys the validity of any downstream quantitative audits measuring "Friend vs. Thief" guard rates.

### The Proof (Code Level)
In `src/strategy/decision.py`, when fading an UP-UP trend (`side == "NO"`), the entry price is the `no_price`. 
- **Floor Check:** `if yes_price < 0.30:`
  If `yes_price` is `< 0.30`, then the `no_price` is $> 0.70$ (Expensive). The DOWN move is fully priced in. However, the bot logs `NO_UP_MOVE_TOO_STRONG_REJECTED`. This is factually incorrect; the YES/UP move was extremely weak, not strong.
- **Ceiling Check:** `if yes_price > 0.60:`
  If `yes_price` is $> 0.60$, then the `no_price` is $< 0.40$ (Cheap). The YES side is heavily priced. However, the bot logs `NO_ALREADY_PRICED_REJECTED`. The NO side is actually cheap, meaning it is the *opposite* of already priced.
This copy-paste error from the YES side guarantees that audits reviewing the `rejections` database will draw inverted conclusions regarding NO side momentum blocks.

---

## 4. Cheap Profile Post-Trade Starvation (Dead Code)
### The Flaw
The `cheap` virtual strategy profile is structurally starved of the very signals it was designed to test (`entry_price < 0.35`), rendering its dashboard metrics useless.

### The Proof (Code Level)
In v5.13, virtual profiles were decoupled from main bot rejection gates by moving `evaluate_and_trigger()` to the top of the decision pipeline. 
However, the `cheap` profile does not use `evaluate_and_trigger()`. It is triggered via `trigger_cheap_post_trade()`, which is called at the very end of `process_signal()`, **after** `self.position_mgr.open_position()` succeeds:
```python
if success:
    if self.profile_mgr:
        await self.profile_mgr.trigger_cheap_post_trade(...)
```
The main bot enforces `min_entry_price = 0.40` for standard TREND trades. Any signal priced below 0.40 is hard-rejected by the main bot and never reaches `open_position()`. Because `trigger_cheap_post_trade()` only runs on successful main bot executions, the `cheap` profile's evaluation logic for `< 0.35` entries is literally unreachable dead code.

---

## Recommended Remediations
1. **Fix FAK Budget Sizer:** In `position_mgr.py`, if `target_shares < order_min_size` during a retry, the bot must either break the loop immediately (saving the remaining budget) or the logic must acknowledge that clamping up violates flat sizing.
2. **Cap REVERSAL Mode:** Apply the same hard `fade_entry_price < 0.50` validation requirement to `REVERSAL` mode entries inside `decision.py`.
3. **Correct NO-Side Telemetry:** Rename the `decision.py` NO-side rejection strings to accurately reflect market conditions (e.g., `NO_DOWN_MOVE_TOO_STRONG` for high NO prices, and `NO_UP_MOVE_TOO_STRONG` for cheap NO prices).
4. **Decouple Cheap Profile:** Move `trigger_cheap_post_trade()` into the isolated `evaluate_and_trigger()` block at the top of `process_signal()` so it can independently evaluate signals before the main bot applies its 0.40¢ floor.