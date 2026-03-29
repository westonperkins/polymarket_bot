"""Fair value model for BTC 5-minute Up/Down markets.

Uses log-normal (GBM) price dynamics to compute the theoretical probability
that BTC will finish above the opening price at candle close.

    P(Up) = Phi(z)
    z = ln(S_now / ref_px) / (sigma * sqrt(tau_norm))

Where:
    S_now    = current BTC spot price
    ref_px   = candle opening price (Polymarket reference)
    sigma    = volatility scaled to 5-minute window
    tau_norm = fraction of candle time remaining (0 to 1)

Adapted from txbabaxyz/mlmodelpoly fair_model.py for 5-minute windows.
"""

import math
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

WINDOW_SEC = 300.0  # 5-minute candles
MIN_SIGMA = 0.000001
MIN_TAU_SEC = 1.0


def _phi(x: float) -> float:
    """Standard normal CDF via error function."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


@dataclass
class FairValue:
    """Fair value estimate for a 5-minute Up/Down market."""
    fair_up: float          # theoretical P(Up)
    fair_down: float        # theoretical P(Down)
    z_score: float          # z-score (positive = bullish)
    edge_up_bps: float      # edge on Up in basis points vs market
    edge_down_bps: float    # edge on Down in basis points vs market
    sigma: float            # volatility used


def compute_fair_value(
    spot_price: float,
    open_price: float,
    sigma: float,
    seconds_remaining: float,
    market_up_price: float = 0.5,
    market_down_price: float = 0.5,
) -> Optional[FairValue]:
    """Compute fair value for a BTC 5-minute Up/Down market.

    Args:
        spot_price: current BTC price
        open_price: BTC price at candle open (reference/PTB)
        sigma: volatility scaled to 5-minute window (stdev of log returns)
        seconds_remaining: seconds until candle close
        market_up_price: current Polymarket Up price (0-1)
        market_down_price: current Polymarket Down price (0-1)

    Returns:
        FairValue with probabilities, z-score, and edge in bps.
        None if inputs are invalid.
    """
    if not spot_price or not open_price or spot_price <= 0 or open_price <= 0:
        return None

    if sigma is None or sigma < MIN_SIGMA:
        # No volatility estimate — fall back to simple comparison
        if spot_price > open_price:
            fair_up, fair_down, z = 0.6, 0.4, 0.0
        elif spot_price < open_price:
            fair_up, fair_down, z = 0.4, 0.6, 0.0
        else:
            fair_up, fair_down, z = 0.5, 0.5, 0.0
        return FairValue(
            fair_up=fair_up, fair_down=fair_down, z_score=z,
            edge_up_bps=(fair_up - market_up_price) * 10000,
            edge_down_bps=(fair_down - market_down_price) * 10000,
            sigma=sigma or 0,
        )

    if seconds_remaining < MIN_TAU_SEC:
        # Almost no time left — price stays where it is
        if spot_price > open_price:
            fair_up, fair_down = 0.95, 0.05
        elif spot_price < open_price:
            fair_up, fair_down = 0.05, 0.95
        else:
            fair_up, fair_down = 0.5, 0.5
        return FairValue(
            fair_up=fair_up, fair_down=fair_down, z_score=0.0,
            edge_up_bps=(fair_up - market_up_price) * 10000,
            edge_down_bps=(fair_down - market_down_price) * 10000,
            sigma=sigma,
        )

    # Normalize time remaining to window
    tau_norm = seconds_remaining / WINDOW_SEC

    # Log price ratio
    log_ratio = math.log(spot_price / open_price)

    # Scale sigma to remaining time
    sigma_scaled = sigma * math.sqrt(tau_norm)
    if sigma_scaled < MIN_SIGMA:
        sigma_scaled = MIN_SIGMA

    # Z-score: how many sigma above/below the reference
    z = log_ratio / sigma_scaled

    # Fair probabilities
    fair_up = _phi(z)
    fair_down = 1.0 - fair_up

    # Edge vs market in basis points
    edge_up = (fair_up - market_up_price) * 10000
    edge_down = (fair_down - market_down_price) * 10000

    logger.debug(
        f"Fair value: up={fair_up:.3f} down={fair_down:.3f} z={z:.2f} "
        f"edge_up={edge_up:+.0f}bps edge_down={edge_down:+.0f}bps "
        f"(spot=${spot_price:.2f} open=${open_price:.2f} sigma={sigma:.6f} tau={seconds_remaining:.0f}s)"
    )

    return FairValue(
        fair_up=round(fair_up, 4),
        fair_down=round(fair_down, 4),
        z_score=round(z, 3),
        edge_up_bps=round(edge_up, 1),
        edge_down_bps=round(edge_down, 1),
        sigma=sigma,
    )
