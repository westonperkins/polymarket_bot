"""Sub-model 1 — Momentum model.

Inputs: price momentum (Signal 3), CVD (Signal 4), Chainlink vs spot divergence (Signal 1).
Votes UP if majority bullish, DOWN if majority bearish, ABSTAIN if mixed.
"""

import logging
from typing import Optional

from signals.spot import MomentumResult
from signals.cvd import CVDResult
import config

logger = logging.getLogger(__name__)


def vote(
    momentum: Optional[MomentumResult],
    cvd: Optional[CVDResult],
    chainlink_price: Optional[float],
    spot_price: Optional[float],
) -> str:
    """Cast a vote based on momentum signals.

    Returns: "Up", "Down", or "ABSTAIN"
    """
    votes = []

    # Signal 1 — Chainlink vs Spot divergence (highest priority)
    if chainlink_price and spot_price:
        divergence = spot_price - chainlink_price
        if divergence > config.CHAINLINK_SPOT_DIVERGENCE_THRESHOLD:
            # Spot above chainlink → price likely to settle up
            votes.append("Up")
            logger.debug(f"  Momentum: chainlink divergence +${divergence:.2f} → Up")
        elif divergence < -config.CHAINLINK_SPOT_DIVERGENCE_THRESHOLD:
            votes.append("Down")
            logger.debug(f"  Momentum: chainlink divergence -${abs(divergence):.2f} → Down")
        else:
            logger.debug(f"  Momentum: chainlink divergence ${divergence:+.2f} → neutral")

    # Signal 3 — Price momentum
    if momentum:
        if momentum.direction == "bullish":
            votes.append("Up")
            logger.debug(f"  Momentum: price momentum {momentum.momentum_60s:+.4f}$/s → Up")
        elif momentum.direction == "bearish":
            votes.append("Down")
            logger.debug(f"  Momentum: price momentum {momentum.momentum_60s:+.4f}$/s → Down")
        else:
            logger.debug(f"  Momentum: price momentum neutral")

    # Signal 4 — CVD
    if cvd:
        if cvd.direction == "bullish":
            votes.append("Up")
            logger.debug(f"  Momentum: CVD {cvd.cvd:+.6f} → Up")
        elif cvd.direction == "bearish":
            votes.append("Down")
            logger.debug(f"  Momentum: CVD {cvd.cvd:+.6f} → Down")
        else:
            logger.debug(f"  Momentum: CVD neutral")

    # Majority vote
    if not votes:
        logger.info("Momentum model: ABSTAIN (no signal data)")
        return "ABSTAIN"

    up_count = votes.count("Up")
    down_count = votes.count("Down")

    if up_count > down_count:
        logger.info(f"Momentum model: UP ({up_count}/{len(votes)} signals)")
        return "Up"
    elif down_count > up_count:
        logger.info(f"Momentum model: DOWN ({down_count}/{len(votes)} signals)")
        return "Down"
    else:
        logger.info(f"Momentum model: ABSTAIN (tied {up_count}–{down_count})")
        return "ABSTAIN"
