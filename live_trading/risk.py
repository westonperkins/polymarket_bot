"""Risk management for live trading.

Enforces daily loss limits, max position sizes, and provides a kill switch.
All limits are percentage-based and recalculated from the current balance on every check.
Kill switch auto-resets at midnight PST each day.
"""

import logging
from datetime import datetime, timezone, timedelta

import config
from database import db

logger = logging.getLogger(__name__)


class RiskManager:
    """Checks risk limits before every trade. Limits scale with current balance."""

    def __init__(self, conn, starting_balance: float) -> None:
        self._conn = conn
        self._starting_balance = starting_balance
        self._killed = False
        self._killed_date = None  # date when kill switch was activated
        self._current_balance = starting_balance

        logger.info(
            f"Risk limits: max daily loss={config.LIVE_MAX_DAILY_LOSS_PCT}%, "
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
        """Emergency stop — no more trades until midnight PST reset."""
        PST = timezone(timedelta(hours=-8))
        self._killed = True
        self._killed_date = datetime.now(PST).date()
        logger.warning("KILL SWITCH ACTIVATED — no further trades until midnight PST reset")
        from notifications import notify_critical_sync
        notify_critical_sync("Kill switch activated — will auto-reset at midnight PST")

    def check_trade_allowed(self, position_size: float) -> tuple[bool, str]:
        """Check if a trade is allowed under current risk limits.

        Limits are recalculated from current balance on every call.
        Kill switch auto-resets at midnight UTC.
        """
        # Auto-reset kill switch at midnight PST
        PST = timezone(timedelta(hours=-8))
        if self._killed and self._killed_date is not None:
            today_pst = datetime.now(PST).date()
            if today_pst > self._killed_date:
                logger.info(
                    f"Kill switch auto-reset (triggered {self._killed_date}, now {today_pst} PST)"
                )
                self._killed = False
                self._killed_date = None

        if self._killed:
            return False, "Kill switch is active"

        # Recalculate limits from current balance
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

        # Check minimum balance
        if self._current_balance < min_balance:
            self.kill()
            return False, (
                f"Balance ${self._current_balance:,.2f} below minimum "
                f"${min_balance:,.2f} ({config.LIVE_MIN_BALANCE_PCT}%)"
            )

        return True, ""
