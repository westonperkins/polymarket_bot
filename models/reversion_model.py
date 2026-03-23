"""Sub-model 2 — Mean reversion model.

Inputs: candle position (Signal 2), order book imbalance (Signal 5), candle streak (Signal 9).
Votes against the current direction if price has moved far from open and streak is long.
"""

import logging
from typing import Optional

from signals.orderbook import OrderBookResult
from signals.market_structure import StreakResult
import config

logger = logging.getLogger(__name__)

# If price has moved more than this many dollars from open, consider it extended
CANDLE_EXTENDED_THRESHOLD = 30.0


def vote(
    candle_position_dollars: Optional[float],
    orderbook: Optional[OrderBookResult],
    streak: Optional[StreakResult],
) -> str:
    """Cast a vote based on mean-reversion signals.

    candle_position_dollars: current_chainlink_price - opening_price
        Positive = candle is Up, negative = candle is Down.

    Returns: "Up", "Down", or "ABSTAIN"
    """
    votes = []

    # Signal 2 — Candle position (vote for reversion if extended)
    # Treat exactly 0.0 as missing data (odds at exactly 0.50 is unlikely)
    if candle_position_dollars is not None and candle_position_dollars != 0.0:
        if candle_position_dollars > CANDLE_EXTENDED_THRESHOLD:
            # Price has moved far Up → mean reversion says Down
            votes.append("Down")
            logger.debug(
                f"  Reversion: candle +${candle_position_dollars:.2f} extended up → Down"
            )
        elif candle_position_dollars < -CANDLE_EXTENDED_THRESHOLD:
            # Price has moved far Down → mean reversion says Up
            votes.append("Up")
            logger.debug(
                f"  Reversion: candle -${abs(candle_position_dollars):.2f} extended down → Up"
            )
        else:
            logger.debug(
                f"  Reversion: candle ${candle_position_dollars:+.2f} not extended"
            )

    # Signal 5 — Order book imbalance
    if orderbook:
        if orderbook.direction == "bullish":
            votes.append("Up")
            logger.debug(f"  Reversion: orderbook ratio {orderbook.ratio:.2f} → Up")
        elif orderbook.direction == "bearish":
            votes.append("Down")
            logger.debug(f"  Reversion: orderbook ratio {orderbook.ratio:.2f} → Down")
        else:
            logger.debug(f"  Reversion: orderbook neutral")

    # Signal 9 — Candle streak (small weight, only when streak is significant)
    if streak and streak.mean_reversion_signal and streak.streak_direction:
        # Vote against the streak direction
        if streak.streak_direction == "Up":
            votes.append("Down")
            logger.debug(
                f"  Reversion: {streak.streak_length}x Up streak → Down"
            )
        else:
            votes.append("Up")
            logger.debug(
                f"  Reversion: {streak.streak_length}x Down streak → Up"
            )

    # Majority vote
    if not votes:
        logger.info("Reversion model: ABSTAIN (no signal data)")
        return "ABSTAIN"

    up_count = votes.count("Up")
    down_count = votes.count("Down")

    if up_count > down_count:
        logger.info(f"Reversion model: UP ({up_count}/{len(votes)} signals)")
        return "Up"
    elif down_count > up_count:
        logger.info(f"Reversion model: DOWN ({down_count}/{len(votes)} signals)")
        return "Down"
    else:
        logger.info(f"Reversion model: ABSTAIN (tied {up_count}–{down_count})")
        return "ABSTAIN"
