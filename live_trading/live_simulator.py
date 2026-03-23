"""Live trade simulator — same interface as paper_trading.simulator but places real orders.

Bridges the ensemble decision, portfolio, database, and CLOB execution layers.
"""

import logging
from typing import Optional

from database import db
from models.ensemble import EnsembleDecision
from polymarket.markets import MarketInfo
from polymarket.odds import MarketOdds, calculate_payout_rate
from paper_trading.portfolio import Portfolio
from live_trading.executor import Executor
from live_trading.risk import RiskManager

logger = logging.getLogger(__name__)


class LiveSimulator:
    """Places real trades on Polymarket via the CLOB API.

    Has the same interface as paper_trading.simulator.Simulator so main.py
    can swap between them based on TRADING_MODE.
    """

    def __init__(self, conn, portfolio: Portfolio, executor: Executor, risk: RiskManager) -> None:
        self._conn = conn
        self._portfolio = portfolio
        self._executor = executor
        self._risk = risk

    def enter_trade(
        self,
        market: MarketInfo,
        odds: MarketOdds,
        decision: EnsembleDecision,
        signal_data: dict,
    ) -> Optional[int]:
        """Place a real trade or record a skip.

        Returns:
            trade_id if a trade was placed, None if skipped.
        """
        if decision.side is None:
            # Skip — log it in the database (same as paper)
            trade_id = db.insert_trade(
                self._conn,
                market_id=market.slug,
                side="Up",
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

        # Determine entry odds and position size
        entry_odds = odds.up_price if decision.side == "Up" else odds.down_price
        payout_rate = calculate_payout_rate(entry_odds)
        position_size = self._portfolio.position_size(decision.confidence)

        # Risk check
        allowed, reason = self._risk.check_trade_allowed(position_size)
        if not allowed:
            logger.warning(f"RISK BLOCKED: {reason}")
            trade_id = db.insert_trade(
                self._conn,
                market_id=market.slug,
                side=decision.side,
                entry_odds=entry_odds,
                position_size=0.0,
                payout_rate=0.0,
                confidence_level="skip",
                outcome="skip",
                pnl=0.0,
                portfolio_balance_after=self._portfolio.balance,
            )
            self._save_signals(trade_id, signal_data)
            return None

        # Select the token ID based on side
        token_id = (
            market.clob_token_id_up if decision.side == "Up"
            else market.clob_token_id_down
        )

        # Place the real order
        order_response = self._executor.place_market_order(
            token_id=token_id,
            amount=position_size,
        )

        if order_response is None:
            logger.error(f"ORDER FAILED: {decision.side} on {market.slug}")
            # Record as skip since the order didn't go through
            trade_id = db.insert_trade(
                self._conn,
                market_id=market.slug,
                side=decision.side,
                entry_odds=entry_odds,
                position_size=0.0,
                payout_rate=0.0,
                confidence_level="skip",
                outcome="skip",
                pnl=0.0,
                portfolio_balance_after=self._portfolio.balance,
            )
            self._save_signals(trade_id, signal_data)
            return None

        # Order placed successfully — record in DB
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
            f"LIVE TRADE: {decision.side} on {market.slug} | "
            f"odds={entry_odds:.3f} size=${position_size:,.2f} "
            f"payout={payout_rate:.1%} confidence={decision.confidence}"
        )
        return trade_id

    def settle_trade(self, trade_id: int, winning_side: str) -> None:
        """Settle a trade after market resolution.

        For live trading, the actual USDC settlement happens on-chain
        automatically. This updates the DB records and portfolio tracker
        to match.
        """
        import psycopg2.extras
        try:
            with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM trades WHERE id = %s", (trade_id,))
                rows = cur.fetchall()
        except Exception as e:
            logger.error(f"DB error fetching trade {trade_id}: {e}")
            return

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

        # Sync balance from wallet after settlement
        real_balance = self._executor.get_balance()
        if real_balance > 0:
            logger.info(
                f"SETTLED: trade {trade_id} {outcome.upper()} "
                f"pnl=${pnl:+,.2f} | wallet=${real_balance:,.2f}"
            )
        else:
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
        if trade_id is not None:
            db.insert_signals(self._conn, trade_id=trade_id, **signal_data)
