"""Market structure signals: round numbers, time-of-day regime, candle streak.

Signal 7 — Round number distance: BTC price proximity to $1,000 levels.
Signal 8 — Time of day regime: US market, Asian, or overnight.
Signal 9 — Previous candle streak: consecutive Up/Down outcomes.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import config

logger = logging.getLogger(__name__)


# ── Signal 7: Round Number Distance ─────────────────────────────────────

@dataclass
class RoundNumberResult:
    """Distance from the nearest round number."""
    nearest_round: float     # e.g. 84000.0
    distance: float          # absolute distance in USD
    is_near: bool            # within ROUND_NUMBER_DISTANCE_THRESHOLD
    direction: str           # "support" if price is above, "resistance" if below


def compute_round_number(price: float) -> RoundNumberResult:
    """Compute distance from nearest round number ($1,000 intervals)."""
    interval = config.ROUND_NUMBER_INTERVAL
    lower = (price // interval) * interval
    upper = lower + interval

    dist_lower = price - lower
    dist_upper = upper - price

    if dist_lower <= dist_upper:
        nearest = lower
        distance = dist_lower
        direction = "support"   # price is above the round number
    else:
        nearest = upper
        distance = dist_upper
        direction = "resistance"  # price is below the round number

    is_near = distance <= config.ROUND_NUMBER_DISTANCE_THRESHOLD

    logger.debug(
        f"Round number: ${nearest:,.0f} dist=${distance:,.0f} "
        f"near={is_near} ({direction})"
    )

    return RoundNumberResult(
        nearest_round=nearest,
        distance=distance,
        is_near=is_near,
        direction=direction,
    )


# ── Signal 8: Time of Day Regime ────────────────────────────────────────

def get_time_regime(utc_now: datetime | None = None) -> str:
    """Determine the current trading regime based on Eastern Time.

    Returns: "us_market", "asian", or "overnight"
    """
    if utc_now is None:
        utc_now = datetime.now(timezone.utc)

    # Convert to ET (UTC-5 standard, UTC-4 DST)
    # Use a simple check: DST is roughly Mar second Sun to Nov first Sun
    et_offset = _get_et_offset(utc_now)
    et_now = utc_now + timedelta(hours=et_offset)
    hour = et_now.hour + et_now.minute / 60.0

    if config.US_MARKET_OPEN_ET <= hour < config.US_MARKET_CLOSE_ET:
        regime = "us_market"
    elif hour >= config.ASIAN_OPEN_ET or hour < config.ASIAN_CLOSE_ET:
        regime = "asian"
    else:
        regime = "overnight"

    logger.debug(f"Time regime: {regime} (ET hour={hour:.1f})")
    return regime


def _get_et_offset(utc_dt: datetime) -> int:
    """Return UTC offset for US Eastern Time (-5 or -4 for DST).

    DST: second Sunday in March to first Sunday in November.
    """
    year = utc_dt.year
    # Second Sunday in March
    mar1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    # First Sunday in November
    nov1 = datetime(year, 11, 1, tzinfo=timezone.utc)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)

    # DST transitions at 2:00 AM ET (7:00 AM UTC for start, 6:00 AM UTC for end)
    dst_start_utc = dst_start.replace(hour=7)
    dst_end_utc = dst_end.replace(hour=6)

    if dst_start_utc <= utc_dt < dst_end_utc:
        return -4  # EDT
    return -5  # EST


# ── Signal 9: Candle Streak ─────────────────────────────────────────────

@dataclass
class StreakResult:
    """Previous candle streak analysis."""
    streak_direction: Optional[str]  # "Up" or "Down" or None
    streak_length: int               # consecutive same-direction candles
    mean_reversion_signal: bool      # True if streak >= threshold


def compute_streak(outcomes: list[str]) -> StreakResult:
    """Analyze a list of recent outcomes (newest first) for streaks.

    Args:
        outcomes: list of "Up"/"Down" strings, newest first.
                  Comes from db.get_last_n_outcomes().
    """
    if not outcomes:
        return StreakResult(
            streak_direction=None,
            streak_length=0,
            mean_reversion_signal=False,
        )

    direction = outcomes[0]
    length = 1
    for outcome in outcomes[1:]:
        if outcome == direction:
            length += 1
        else:
            break

    signal = length >= config.STREAK_THRESHOLD

    logger.debug(
        f"Candle streak: {length}x {direction} "
        f"mean_reversion={'YES' if signal else 'no'}"
    )

    return StreakResult(
        streak_direction=direction,
        streak_length=length,
        mean_reversion_signal=signal,
    )
