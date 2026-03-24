"""Risk management for live trading.

Enforces daily loss limits, max position sizes, and provides a kill switch.
All limits are percentage-based relative to the starting balance.
"""

import logging

import config
from database import db

logger = logging.getLogger(__name__)


class RiskManager:
    """Checks risk limits before every trade."""

    def __init__(self, conn, starting_balance: float) -> None:
        self._conn = conn
        self._starting_balance = starting_balance
        self._killed = False

        # Convert percentages to dollar amounts based on starting balance
        self._max_daily_loss = starting_balance * (config.LIVE_MAX_DAILY_LOSS_PCT / 100)
        self._max_position_size = starting_balance * (config.LIVE_MAX_POSITION_SIZE_PCT / 100)
        self._min_balance = starting_balance * (config.LIVE_MIN_BALANCE_PCT / 100)

        logger.info(
            f"Risk limits: max daily loss=${self._max_daily_loss:,.2f} "
            f"({config.LIVE_MAX_DAILY_LOSS_PCT}%), "
            f"max position=${self._max_position_size:,.2f} "
            f"({config.LIVE_MAX_POSITION_SIZE_PCT}%), "
            f"min balance=${self._min_balance:,.2f} "
            f"({config.LIVE_MIN_BALANCE_PCT}%)"
        )

    @property
    def is_killed(self) -> bool:
        return self._killed

    def kill(self) -> None:
        """Emergency stop — no more trades until bot restarts."""
        self._killed = True
        logger.warning("KILL SWITCH ACTIVATED — no further trades will be placed")

    def check_trade_allowed(self, position_size: float) -> tuple[bool, str]:
        """Check if a trade is allowed under current risk limits.

        Returns:
            (allowed, reason) — reason is empty string if allowed.
        """
        if self._killed:
            return False, "Kill switch is active"

        # Check daily loss limit
        daily_pnl = db.get_daily_pnl(self._conn)
        if daily_pnl <= -self._max_daily_loss:
            self.kill()
            return False, (
                f"Daily loss limit reached (${daily_pnl:,.2f} / "
                f"-${self._max_daily_loss:,.2f} max)"
            )

        # Check max position size (round to cents to avoid float precision issues)
        if round(position_size, 2) > round(self._max_position_size, 2):
            return False, (
                f"Position size ${position_size:,.2f} exceeds "
                f"max ${self._max_position_size:,.2f} "
                f"({config.LIVE_MAX_POSITION_SIZE_PCT}%)"
            )

        # Check minimum balance
        snapshot = db.get_latest_portfolio(self._conn)
        if snapshot and snapshot["balance"] < self._min_balance:
            self.kill()
            return False, (
                f"Balance ${snapshot['balance']:,.2f} below minimum "
                f"${self._min_balance:,.2f} ({config.LIVE_MIN_BALANCE_PCT}%)"
            )

        return True, ""
