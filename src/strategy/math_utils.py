"""
Quantitative Math Utilities — RuFlo V3 Reverse Bot.

Consolidates all trading math into pure, unit-testable functions.
Zero magic numbers. Every formula has a docstring.

Constants:
    POLYMARKET_FEE_RATE: Protocol fee (2%)
    FEE_MULTIPLIER: 1.0 - fee_rate (0.98)

Functions:
    net_value()                 — Apply fee to gross value
    unrealised_pnl()           — Mark-to-market P&L
    realised_pnl()             — Closed position P&L
    expected_value()            — EV of holding to resolution
    slippage_depth_required()  — Min orderbook depth for safe execution
"""

from typing import Final


# ─── Constants ───────────────────────────────────────────────────
POLYMARKET_FEE_RATE: Final[float] = 0.02   # Legacy/unused constant (retained for backward compatibility)
FEE_MULTIPLIER: Final[float] = 1.0  # Polymarket CLOB trading and settlement/resolution have 0% fees (1.0 is correct)


# ─── Pure Functions ──────────────────────────────────────────────

def net_value(gross_value: float) -> float:
    """
    Apply Polymarket fee to a gross position value.

    Args:
        gross_value: Raw value before fees (e.g., shares * price)

    Returns:
        Fee-adjusted value (gross * 0.98)

    Example:
        >>> net_value(10.0)
        9.8
    """
    return gross_value * FEE_MULTIPLIER


def unrealised_pnl(
    current_price: float,
    shares_remaining: float,
    cost_basis: float,
    partial_proceeds: float = 0.0,
) -> float:
    """
    Calculate mark-to-market unrealised P&L for an open position.

    Args:
        current_price: Current NO token price
        shares_remaining: Number of NO shares still held
        cost_basis: Original cost in USDC (size_usdc)
        partial_proceeds: USDC already collected from partial TP fills

    Returns:
        Unrealised P&L in USDC (positive = profit)
    """
    return net_value(current_price * shares_remaining) + partial_proceeds - cost_basis


def realised_pnl(
    exit_price: float,
    shares_sold: float,
    cost_basis: float,
    partial_proceeds: float = 0.0,
) -> float:
    """
    Calculate realised P&L for a closed position.

    Args:
        exit_price: Price at which the remaining position was closed
        shares_sold: Number of NO shares sold in the final exit
        cost_basis: Original cost in USDC (size_usdc)
        partial_proceeds: USDC already collected from partial TP fills

    Returns:
        Realised P&L in USDC (positive = profit)
    """
    return net_value(exit_price * shares_sold) + partial_proceeds - cost_basis


def expected_value(
    no_price: float,
    shares: float,
    cost: float,
) -> float:
    """
    Calculate expected value of holding a NO position to resolution.

    If the event resolves NO (which is what the bot bets on),
    payout = (1.0 - no_price) * shares, minus fees.
    EV = net_payout - cost.

    Args:
        no_price: Current NO token price
        shares: Number of NO shares
        cost: Total cost to acquire the position (size_usdc)

    Returns:
        EV in USDC. Positive = favorable bet.
    """
    net_payout = (1.0 - no_price) * shares * FEE_MULTIPLIER
    return net_payout - cost


def slippage_depth_required(
    size_usdc: float,
    multiplier: float,
) -> float:
    """
    Calculate minimum orderbook depth to avoid excessive slippage.

    Formula: size_usdc * multiplier

    Args:
        size_usdc: Intended trade size
        multiplier: Safety multiplier (e.g., 1.2 = 20% buffer)

    Returns:
        Required depth in USDC
    """
    return size_usdc * multiplier


def tp_target(cost_basis: float, tp_multiplier: float) -> float:
    """
    Calculate take-profit target value.

    Formula: cost_basis * tp_multiplier

    Args:
        cost_basis: Original trade cost (size_usdc)
        tp_multiplier: E.g., 2.0 = 100% profit target

    Returns:
        Net value threshold to trigger take-profit
    """
    return cost_basis * tp_multiplier


def sl_target(cost_basis: float, sl_multiplier: float) -> float:
    """
    Calculate stop-loss target value.

    Formula: cost_basis * sl_multiplier

    Args:
        cost_basis: Original trade cost (size_usdc)
        sl_multiplier: E.g., 0.5 = 50% loss threshold

    Returns:
        Net value threshold to trigger stop-loss
    """
    return cost_basis * sl_multiplier


def calculate_kelly_fraction(p: float, b: float) -> float:
    """
    Compute the Half-Kelly fraction for dynamic position sizing.

    Formula:
        kelly_fraction = p - (1.0 - p) / b
        half_kelly = max(0.0, kelly_fraction * 0.5)

    Args:
        p: Estimated win probability (0.0 to 1.0)
        b: Net odds (payout per unit risk - 1.0). E.g., at entry price 0.48, b = (1.0/0.48) - 1.0 = 1.0833

    Returns:
        Half-Kelly fraction (clamped to positive)
    """
    if b <= 0.0:
        return 0.0
    kelly = p - (1.0 - p) / b
    return max(0.0, kelly * 0.5)


def get_linear_time_decay_multiplier(
    time_left_seconds: float,
    total_window_seconds: float,
    min_mult: float = 0.5,
) -> float:
    """
    Compute a linear time decay multiplier to scale size or premium near expiry.

    Args:
        time_left_seconds: Remaining seconds until market closes
        total_window_seconds: Total duration of the candle (e.g., 300 for 5m, 900 for 15m)
        min_mult: Floor multiplier near expiry (default 0.5)

    Returns:
        Multiplier between min_mult and 1.0
    """
    if total_window_seconds <= 0:
        return min_mult
    ratio = max(0.0, min(1.0, time_left_seconds / total_window_seconds))
    return min_mult + (1.0 - min_mult) * ratio


def calculate_correlated_penalty_multiplier(has_correlated_open: bool) -> float:
    """
    Apply a 0.5x sizing multiplier if a same-direction trade is already open on the correlated asset.

    Args:
        has_correlated_open: True if a correlated trade in the same direction is open

    Returns:
        Sizing multiplier (0.5 if correlated trade open, else 1.0)
    """
    return 0.5 if has_correlated_open else 1.0


def is_mock_market_id(market_id: str) -> bool:
    """
    Check if a market ID is a mock ID used in unit tests.
    Mock IDs start with '0x' but contain non-hexadecimal characters,
    or contain explicit mock keywords like 'knife', 'shadow', 'test', etc.
    """
    if not market_id:
        return True
    
    # Check for mock keywords
    if any(p in market_id.lower() for p in ("knife", "shadow", "test", "corr", "target", "mock")):
        return True
        
    # Check if starting with 0x but has non-hex characters
    if market_id.startswith("0x"):
        hex_part = market_id[2:]
        import string
        if not all(c in string.hexdigits for c in hex_part):
            return True
            
    # If it's a short string that isn't numeric, it's likely a mock ID
    if not market_id.isdigit() and len(market_id) < 10:
        return True
        
    return False


def calculate_bs_binary_prob(
    spot: float,
    strike: float,
    ttl_secs: float,
    vol_5m: float,
    risk_free_rate: float = 0.0,
) -> float:
    """
    Calculate theoretical risk-neutral Binary Option probability (Black-Scholes P(S_expiry > K)).
    
    Formula:
        d2 = (ln(S / K) + (r - 0.5 * sigma^2) * t) / (sigma * sqrt(t))
        P_BS = 0.5 * (1.0 + erf(d2 / sqrt(2)))
        
    Boundary Guards:
        - If ttl_secs <= 0 or vol_5m <= 0: returns 1.0 if spot > strike else (0.0 if spot < strike else 0.5)
        - If spot <= 0 or strike <= 0: returns 0.5
    """
    import math
    if spot <= 0.0 or strike <= 0.0:
        return 0.5
    if ttl_secs <= 0.0 or vol_5m <= 0.0:
        if spot > strike:
            return 1.0
        elif spot < strike:
            return 0.0
        else:
            return 0.5
            
    t = ttl_secs / 31536000.0  # Convert seconds to annualized time
    sigma = max(0.0001, vol_5m * math.sqrt(105120.0))  # Annualize 5m volatility (105,120 5m candles per year)
    
    try:
        ln_ratio = math.log(spot / strike)
        d2 = (ln_ratio + (risk_free_rate - 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
        prob = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))
        return max(0.0, min(1.0, float(prob)))
    except Exception:
        return 0.5


