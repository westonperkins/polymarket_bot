"""Sub-model 3 — Market structure model.

Inputs: round number distance (Signal 7), liquidation pressure (Signal 6), time of day (Signal 8).
Votes based on structural factors.
"""

import logging
from typing import Optional

from signals.market_structure import RoundNumberResult
from signals.liquidations import LiquidationResult

logger = logging.getLogger(__name__)


def vote(
    round_number: Optional[RoundNumberResult],
    liquidations: Optional[LiquidationResult],
    time_regime: Optional[str],
    candle_position_dollars: Optional[float],
) -> str:
    """Cast a vote based on market structure signals.

    candle_position_dollars is needed to interpret round number support/resistance
    in context (is price approaching or bouncing off the level?).

    Returns: "Up", "Down", or "ABSTAIN"
    """
    votes = []

    # Signal 7 — Round number proximity
    if round_number and round_number.is_near:
        if round_number.direction == "support":
            # Price is just above a round number → support likely holds → Up
            votes.append("Up")
            logger.debug(
                f"  Structure: near ${round_number.nearest_round:,.0f} support "
                f"(dist=${round_number.distance:,.0f}) → Up"
            )
        else:
            # Price is just below a round number → resistance likely holds → Down
            votes.append("Down")
            logger.debug(
                f"  Structure: near ${round_number.nearest_round:,.0f} resistance "
                f"(dist=${round_number.distance:,.0f}) → Down"
            )

    # Signal 6 — Liquidation pressure
    if liquidations:
        if liquidations.direction == "bullish":
            votes.append("Up")
            logger.debug(
                f"  Structure: liquidation pressure bullish "
                f"(shorts=${liquidations.short_liquidated_usd:,.0f}) → Up"
            )
        elif liquidations.direction == "bearish":
            votes.append("Down")
            logger.debug(
                f"  Structure: liquidation pressure bearish "
                f"(longs=${liquidations.long_liquidated_usd:,.0f}) → Down"
            )
        else:
            logger.debug("  Structure: liquidation pressure neutral")

    # Signal 8 — Time of day regime (modifies interpretation, not direct vote)
    # During US market hours, momentum is more reliable → lean with current direction
    # During Asian hours, mean reversion is more likely → lean against current direction
    if time_regime and candle_position_dollars is not None:
        if time_regime == "us_market" and abs(candle_position_dollars) > 10:
            # US hours: momentum-driven, lean with current direction
            if candle_position_dollars > 0:
                votes.append("Up")
                logger.debug("  Structure: US market hours + candle up → Up (momentum regime)")
            else:
                votes.append("Down")
                logger.debug("  Structure: US market hours + candle down → Down (momentum regime)")
        elif time_regime == "asian" and abs(candle_position_dollars) > 20:
            # Asian hours: mean-reversion, lean against current direction
            if candle_position_dollars > 0:
                votes.append("Down")
                logger.debug("  Structure: Asian hours + candle up → Down (reversion regime)")
            else:
                votes.append("Up")
                logger.debug("  Structure: Asian hours + candle down → Up (reversion regime)")
        else:
            logger.debug(f"  Structure: time regime={time_regime}, no strong lean")

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
        logger.info(f"Structure model: ABSTAIN (tied {up_count}–{down_count})")
        return "ABSTAIN"
