"""Trade simulator — handles entry at T-30 and settlement at market close.

Bridges the ensemble decision, portfolio, and database layers.
"""

import logging
from typing import Optional

from database import db
from models.ensemble import EnsembleDecision
from polymarket.markets import MarketInfo
from polymarket.odds import MarketOdds, calculate_payout_rate
from paper_trading.portfolio import Portfolio

logger = logging.getLogger(__name__)


class Simulator:
    """Simulates paper trades on Polymarket BTC 5-min Up/Down markets."""

    def __init__(self, conn, portfolio: Portfolio) -> None:
        self._conn = conn
        self._portfolio = portfolio

    def enter_trade(
        self,
        market: MarketInfo,
        odds: MarketOdds,
        decision: EnsembleDecision,
        signal_data: dict,
    ) -> Optional[int]:
        """Record a simulated trade entry (or skip) at T-30.

        Args:
            market: current market metadata
            odds: current market odds
            decision: ensemble vote result
            signal_data: dict of all signal values for the signals table

        Returns:
            trade_id if a trade was placed, None if skipped.
        """
        if decision.side is None:
            # Skip — log it in the database
            trade_id = db.insert_trade(
                self._conn,
                market_id=market.slug,
                side="Up",  # placeholder for skips
                entry_odds=odds.up_price,
                position_size=0.0,
                payout_rate=0.0,
                confidence_level="skip",
                outcome="skip",
                pnl=0.0,
                portfolio_balance_after=self._portfolio.balance,
            )
            self._save_signals(trade_id, signal_data)
            logger.info(f"SKIP: {market.slug} — {decision.reason}")
            return None

        # Determine entry odds based on side
        entry_odds = odds.up_price if decision.side == "Up" else odds.down_price
        payout_rate = calculate_payout_rate(entry_odds)
        position_size = self._portfolio.position_size(decision.confidence)

        trade_id = db.insert_trade(
            self._conn,
            market_id=market.slug,
            side=decision.side,
            entry_odds=entry_odds,
            position_size=position_size,
            payout_rate=payout_rate,
            confidence_level=decision.confidence,
            outcome="pending",
            pnl=0.0,
            portfolio_balance_after=self._portfolio.balance,
        )
        self._save_signals(trade_id, signal_data)

        logger.info(
            f"TRADE: {decision.side} on {market.slug} | "
            f"odds={entry_odds:.3f} size=${position_size:,.2f} "
            f"payout={payout_rate:.1%} confidence={decision.confidence}"
        )
        return trade_id

    def settle_trade(self, trade_id: int, winning_side: str) -> None:
        """Settle a pending trade after market resolution.

        Args:
            trade_id: the trade to settle
            winning_side: "Up" or "Down" — the resolved market outcome
        """
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchall()
        if not rows:
            logger.error(f"Trade {trade_id} not found for settlement")
            return

        trade = rows[0]

        if trade["outcome"] != "pending":
            logger.warning(f"Trade {trade_id} already settled as {trade['outcome']}")
            return

        position_size = trade["position_size"]
        payout_rate = trade["payout_rate"]
        trade_side = trade["side"]

        if trade_side == winning_side:
            pnl = self._portfolio.settle_win(position_size, payout_rate)
            outcome = "win"
        else:
            pnl = self._portfolio.settle_loss(position_size)
            outcome = "loss"

        db.update_trade_outcome(
            self._conn,
            trade_id=trade_id,
            outcome=outcome,
            pnl=pnl,
            portfolio_balance_after=self._portfolio.balance,
        )
        self._portfolio.save_snapshot()

        logger.info(
            f"SETTLED: trade {trade_id} {outcome.upper()} "
            f"pnl=${pnl:+,.2f} → balance=${self._portfolio.balance:,.2f}"
        )

    def get_pending_trade_ids(self) -> list[int]:
        """Return IDs of all pending trades."""
        rows = db.get_pending_trades(self._conn)
        return [row["id"] for row in rows]

    def _save_signals(self, trade_id: int, signal_data: dict) -> None:
        """Persist signal snapshot to the database."""
        db.insert_signals(self._conn, trade_id=trade_id, **signal_data)
