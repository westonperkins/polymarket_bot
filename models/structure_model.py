"""Sub-model 3 — Market structure model.

Uses Polymarket CLOB order book bias to gauge prediction market sentiment.
Also considers liquidation pressure and time-of-day regime as secondary signals.
"""

import logging
from typing import Optional

from signals.polymarket_book import PolymarketBookResult
from signals.liquidations import LiquidationResult

logger = logging.getLogger(__name__)


def vote(
    polymarket_book: Optional[PolymarketBookResult] = None,
    liquidations: Optional[LiquidationResult] = None,
    time_regime: Optional[str] = None,
    candle_position_dollars: Optional[float] = None,
) -> str:
    """Cast a vote based on market structure signals.

    Returns: "Up", "Down", or "ABSTAIN"
    """
    votes = []

    # Primary signal — Polymarket CLOB order book bias
    if polymarket_book and polymarket_book.direction != "neutral":
        if polymarket_book.direction == "bullish":
            votes.append("Up")
            logger.debug(f"  Structure: Polymarket book bullish (bias={polymarket_book.bias:+.1f}) -> Up")
        else:
            votes.append("Down")
            logger.debug(f"  Structure: Polymarket book bearish (bias={polymarket_book.bias:+.1f}) -> Down")

    # Secondary signal — Liquidation pressure
    if liquidations and liquidations.direction != "neutral":
        if liquidations.direction == "bullish":
            votes.append("Up")
            logger.debug(f"  Structure: liquidation pressure bullish -> Up")
        else:
            votes.append("Down")
            logger.debug(f"  Structure: liquidation pressure bearish -> Down")

    # Tertiary signal — Time regime
    if time_regime and candle_position_dollars is not None and candle_position_dollars != 0.0:
        if time_regime == "us_market" and abs(candle_position_dollars) > 10:
            if candle_position_dollars > 0:
                votes.append("Up")
            else:
                votes.append("Down")
        elif time_regime == "asian" and abs(candle_position_dollars) > 20:
            if candle_position_dollars > 0:
                votes.append("Down")
            else:
                votes.append("Up")

    # Majority vote
    if not votes:
        logger.info("Structure model: ABSTAIN (no signal data)")
        return "ABSTAIN"

    up_count = votes.count("Up")
    down_count = votes.count("Down")

    if up_count > down_count:
        logger.info(f"Structure model: UP ({up_count}/{len(votes)} signals)")
        return "Up"
    elif down_count > up_count:
        logger.info(f"Structure model: DOWN ({down_count}/{len(votes)} signals)")
        return "Down"
    else:
        logger.info(f"Structure model: ABSTAIN (tied {up_count}-{down_count})")
        return "ABSTAIN"
