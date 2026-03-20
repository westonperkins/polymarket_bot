"""Ensemble decision logic — combines 3 sub-model votes into a final trade decision.

Rules:
- All 3 vote same direction → HIGH confidence, full position size
- 2 of 3 vote same direction → MEDIUM confidence, half position size
- 1 or 0 agree → NO TRADE, skip this candle
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EnsembleDecision:
    """Final trade decision from the ensemble."""
    side: Optional[str]       # "Up", "Down", or None (skip)
    confidence: str           # "high", "medium", or "skip"
    momentum_vote: str        # "Up", "Down", or "ABSTAIN"
    reversion_vote: str       # "Up", "Down", or "ABSTAIN"
    structure_vote: str       # "Up", "Down", or "ABSTAIN"
    reason: str               # human-readable explanation


def decide(
    momentum_vote: str,
    reversion_vote: str,
    structure_vote: str,
) -> EnsembleDecision:
    """Combine three sub-model votes into a final decision.

    Args:
        momentum_vote: "Up", "Down", or "ABSTAIN"
        reversion_vote: "Up", "Down", or "ABSTAIN"
        structure_vote: "Up", "Down", or "ABSTAIN"

    Returns:
        EnsembleDecision with side, confidence, and reason.
    """
    votes = [momentum_vote, reversion_vote, structure_vote]
    up_count = votes.count("Up")
    down_count = votes.count("Down")
    abstain_count = votes.count("ABSTAIN")

    labels = f"momentum={momentum_vote}, reversion={reversion_vote}, structure={structure_vote}"

    # All 3 agree → HIGH confidence
    if up_count == 3:
        decision = EnsembleDecision(
            side="Up",
            confidence="high",
            momentum_vote=momentum_vote,
            reversion_vote=reversion_vote,
            structure_vote=structure_vote,
            reason=f"3/3 vote Up ({labels})",
        )
        logger.info(f"ENSEMBLE: UP HIGH — {decision.reason}")
        return decision

    if down_count == 3:
        decision = EnsembleDecision(
            side="Down",
            confidence="high",
            momentum_vote=momentum_vote,
            reversion_vote=reversion_vote,
            structure_vote=structure_vote,
            reason=f"3/3 vote Down ({labels})",
        )
        logger.info(f"ENSEMBLE: DOWN HIGH — {decision.reason}")
        return decision

    # 2 of 3 agree → MEDIUM confidence
    if up_count == 2:
        decision = EnsembleDecision(
            side="Up",
            confidence="medium",
            momentum_vote=momentum_vote,
            reversion_vote=reversion_vote,
            structure_vote=structure_vote,
            reason=f"2/3 vote Up ({labels})",
        )
        logger.info(f"ENSEMBLE: UP MEDIUM — {decision.reason}")
        return decision

    if down_count == 2:
        decision = EnsembleDecision(
            side="Down",
            confidence="medium",
            momentum_vote=momentum_vote,
            reversion_vote=reversion_vote,
            structure_vote=structure_vote,
            reason=f"2/3 vote Down ({labels})",
        )
        logger.info(f"ENSEMBLE: DOWN MEDIUM — {decision.reason}")
        return decision

    # No consensus → SKIP
    decision = EnsembleDecision(
        side=None,
        confidence="skip",
        momentum_vote=momentum_vote,
        reversion_vote=reversion_vote,
        structure_vote=structure_vote,
        reason=f"No consensus ({labels})",
    )
    logger.info(f"ENSEMBLE: SKIP — {decision.reason}")
    return decision
