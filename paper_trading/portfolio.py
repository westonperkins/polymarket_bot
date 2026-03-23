"""Virtual portfolio management for paper trading.

Tracks balance, computes position sizes, and records snapshots to the database.
"""

import logging
from typing import Optional

import config
from database import db

logger = logging.getLogger(__name__)


class Portfolio:
    """Manages the virtual USDC portfolio."""

    def __init__(self, conn, starting_balance: float = None, skip_restore: bool = False) -> None:
        self._conn = conn
        self._starting_balance = starting_balance or config.STARTING_BALANCE
        self._balance: float = self._starting_balance
        self._daily_pnl: float = 0.0

        # Try to restore from latest DB snapshot (skip for live mode)
        if not skip_restore:
            snapshot = db.get_latest_portfolio(conn)
            if snapshot:
                self._balance = snapshot["balance"]
                logger.info(f"Restored portfolio balance: ${self._balance:,.2f}")
            else:
                logger.info(f"New portfolio initialized: ${self._balance:,.2f}")
        else:
            logger.info(f"Portfolio initialized (no restore): ${self._balance:,.2f}")

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def pnl_pct(self) -> float:
        """Return total P&L as percentage from starting balance."""
        return ((self._balance - self._starting_balance) / self._starting_balance) * 100

    def position_size(self, confidence: str) -> float:
        """Calculate position size based on confidence level.

        Args:
            confidence: "high" or "medium"

        Returns:
            Dollar amount to risk on this trade.
        """
        if confidence == "high":
            size = self._balance * config.RISK_HIGH_CONFIDENCE
        elif confidence == "medium":
            size = self._balance * config.RISK_MEDIUM_CONFIDENCE
        else:
            return 0.0

        size = round(size, 2)
        logger.debug(f"Position size for {confidence} confidence: ${size:,.2f}")
        return size

    def settle_win(self, position_size: float, payout_rate: float) -> float:
        """Settle a winning trade. Returns the P&L amount."""
        profit = round(position_size * payout_rate, 2)
        self._balance = round(self._balance + profit, 2)
        self._daily_pnl = round(self._daily_pnl + profit, 2)
        logger.info(
            f"WIN: +${profit:,.2f} (size=${position_size:,.2f}, payout={payout_rate:.1%}) "
            f"→ balance=${self._balance:,.2f}"
        )
        return profit

    def settle_loss(self, position_size: float) -> float:
        """Settle a losing trade. Returns the P&L amount (negative)."""
        loss = -round(position_size, 2)
        self._balance = round(self._balance + loss, 2)
        self._daily_pnl = round(self._daily_pnl + loss, 2)
        logger.info(
            f"LOSS: -${position_size:,.2f} → balance=${self._balance:,.2f}"
        )
        return loss

    def save_snapshot(self) -> None:
        """Persist current portfolio state to the database."""
        stats = db.get_trade_stats(self._conn)
        db.insert_portfolio_snapshot(
            self._conn,
            balance=self._balance,
            total_trades=stats["total"],
            wins=stats["wins"],
            losses=stats["losses"],
            skips=stats["skips"],
            win_rate=stats["win_rate"],
            daily_pnl=self._daily_pnl,
        )
        logger.debug(f"Portfolio snapshot saved: ${self._balance:,.2f}")

    def reset_daily_pnl(self) -> None:
        """Reset daily P&L counter (call at start of each day)."""
        self._daily_pnl = 0.0
