"""Risk management for live trading.

Enforces daily loss limits, max position sizes, and provides a kill switch.
All limits are percentage-based and recalculated from the current balance on every check.
"""

import logging

import config
from database import db

logger = logging.getLogger(__name__)


class RiskManager:
    """Checks risk limits before every trade. Limits scale with current balance."""

    def __init__(self, conn, starting_balance: float) -> None:
        self._conn = conn
        self._starting_balance = starting_balance
        self._killed = False
        self._current_balance = starting_balance

        logger.info(
            f"Risk limits: max daily loss={config.LIVE_MAX_DAILY_LOSS_PCT}%, "
            f"max position={config.LIVE_MAX_POSITION_SIZE_PCT}%, "
            f"min balance={config.LIVE_MIN_BALANCE_PCT}%"
        )

    def update_balance(self, balance: float) -> None:
        """Update the current balance for dynamic risk calculations."""
        if balance > 0:
            self._current_balance = balance

    @property
    def is_killed(self) -> bool:
        return self._killed

    def kill(self) -> None:
        """Emergency stop — no more trades until bot restarts."""
        self._killed = True
        logger.warning("KILL SWITCH ACTIVATED — no further trades will be placed")

    def check_trade_allowed(self, position_size: float) -> tuple[bool, str]:
        """Check if a trade is allowed under current risk limits.

        Limits are recalculated from current balance on every call.
        """
        if self._killed:
            return False, "Kill switch is active"

        # Recalculate limits from current balance
        max_position = self._current_balance * (config.LIVE_MAX_POSITION_SIZE_PCT / 100)
        max_daily_loss = self._current_balance * (config.LIVE_MAX_DAILY_LOSS_PCT / 100)
        min_balance = self._starting_balance * (config.LIVE_MIN_BALANCE_PCT / 100)

        # Check daily loss limit
        daily_pnl = db.get_daily_pnl(self._conn)
        if daily_pnl <= -max_daily_loss:
            self.kill()
            return False, (
                f"Daily loss limit reached (${daily_pnl:,.2f} / "
                f"-${max_daily_loss:,.2f} max)"
            )

        # Check max position size
        if round(position_size, 2) > round(max_position, 2):
            return False, (
                f"Position size ${position_size:,.2f} exceeds "
                f"max ${max_position:,.2f} "
                f"({config.LIVE_MAX_POSITION_SIZE_PCT}%)"
            )

        # Check minimum balance
        if self._current_balance < min_balance:
            self.kill()
            return False, (
                f"Balance ${self._current_balance:,.2f} below minimum "
                f"${min_balance:,.2f} ({config.LIVE_MIN_BALANCE_PCT}%)"
            )

        return True, ""
