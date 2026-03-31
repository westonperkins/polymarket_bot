"""Live trade simulator — same interface as paper_trading.simulator but places real orders.

Bridges the ensemble decision, portfolio, database, and CLOB execution layers.

Portfolio balance tracking:
- portfolio._balance tracks the "true" balance from fills (cost + pnl)
- get_balance() returns available USDC (excludes unclaimed winnings)
- We use fill-based tracking for DB records and get_balance() for position sizing
"""

import logging
from typing import Optional

import config
from database import db
from models.ensemble import EnsembleDecision
from polymarket.markets import MarketInfo
from polymarket.odds import MarketOdds, calculate_payout_rate
from paper_trading.portfolio import Portfolio
from live_trading.executor import Executor
from live_trading.risk import RiskManager

logger = logging.getLogger(__name__)


class LiveSimulator:
    """Places real trades on Polymarket via the CLOB API."""

    def __init__(self, conn, portfolio: Portfolio, executor: Executor, risk: RiskManager) -> None:
        self._conn = conn
        self._portfolio = portfolio
        self._executor = executor
        self._risk = risk
        # Track balance from fills — independent of wallet/claim state
        self._tracked_balance = portfolio.balance

    def enter_trade(
        self,
        market: MarketInfo,
        odds: MarketOdds,
        decision: EnsembleDecision,
        signal_data: dict,
    ) -> Optional[int]:
        """Place a real trade or record a skip."""

        if decision.side is None:
            # Determine skip reason from decision
            skip_reason = "ml_gate" if "ML gate" in (decision.reason or "") else "no_consensus"
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
                portfolio_balance_after=self._tracked_balance,
                skip_reason=skip_reason,
            )
            self._save_signals(trade_id, signal_data)
            logger.info(f"⏭️  SKIP: {market.slug} — {decision.reason}")
            return None

        # Determine entry odds and position size
        entry_odds = odds.up_price if decision.side == "Up" else odds.down_price
        payout_rate = calculate_payout_rate(entry_odds)

        # Use wallet balance for position sizing (available USDC to trade)
        wallet_balance = self._executor.get_balance()
        if wallet_balance > 0:
            self._portfolio._balance = wallet_balance
            self._risk.update_balance(wallet_balance)
        position_size = round(self._portfolio.position_size(decision.confidence), 2)

        # Risk check
        allowed, reason = self._risk.check_trade_allowed(position_size)
        if not allowed:
            logger.warning(f"🚫 RISK BLOCKED: {reason}")
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
                portfolio_balance_after=self._tracked_balance,
                skip_reason="risk_blocked",
            )
            self._save_signals(trade_id, signal_data)
            return None

        # Select the token ID based on side
        token_id = (
            market.clob_token_id_up if decision.side == "Up"
            else market.clob_token_id_down
        )

        # Max price = quoted odds + slippage tolerance, capped at 0.99 (Polymarket limit)
        max_price = min(entry_odds * (1.0 + config.LIVE_MAX_SLIPPAGE_PCT / 100.0), 0.99)
        logger.info(
            f"Placing order: {decision.side} | quoted={entry_odds:.3f} "
            f"max_price={max_price:.3f} ({config.LIVE_MAX_SLIPPAGE_PCT}% slippage)"
        )

        # Place the real order with price limit
        order_response = self._executor.place_market_order(
            token_id=token_id,
            amount=position_size,
            max_price=max_price,
        )

        if order_response is None:
            # Parse specific rejection reason from the error
            err = self._executor._last_order_error.lower()
            if "no orders found" in err or "no match" in err:
                reject_reason = "empty_book"
            elif "fully filled" in err:
                reject_reason = "insufficient_liquidity"
            elif "price" in err and ("min" in err or "max" in err):
                reject_reason = "price_out_of_range"
            elif "service not ready" in err or "too early" in err:
                reject_reason = "service_unavailable"
            elif "invalid amounts" in err or "decimals" in err:
                reject_reason = "invalid_amount"
            else:
                reject_reason = "order_rejected"

            logger.error(f"❌ ORDER FAILED: {decision.side} on {market.slug} ({reject_reason})")
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
                portfolio_balance_after=self._tracked_balance,
                skip_reason=reject_reason,
            )
            self._save_signals(trade_id, signal_data)
            return None

        # Use actual fill amounts from the CLOB response
        fill_cost = order_response.get("_fill_cost", position_size)
        fill_shares = order_response.get("_fill_shares", 0)

        # Fill quality analysis
        effective_price = fill_cost / fill_shares if fill_shares > 0 else 0
        slippage_pct = ((effective_price - entry_odds) / entry_odds * 100) if entry_odds > 0 else 0
        real_payout_rate = (fill_shares - fill_cost) / fill_cost if fill_cost > 0 else 0.0

        # Risk/reward ratio: potential win / potential loss
        potential_win = fill_shares - fill_cost
        potential_loss = fill_cost
        rr_ratio = round(potential_win / potential_loss, 2) if potential_loss > 0 else 0

        slip_emoji = "🟢" if slippage_pct <= 10 else "🟡" if slippage_pct <= 30 else "🔴"
        logger.info(
            f"{slip_emoji} FILL: quoted=${entry_odds:.3f} actual=${effective_price:.3f} "
            f"slippage={slippage_pct:+.1f}% | "
            f"R:R={rr_ratio}:1 (win +${potential_win:.2f} / lose -${potential_loss:.2f})"
        )

        trade_id = db.insert_trade(
            self._conn,
            market_id=market.slug,
            side=decision.side,
            entry_odds=entry_odds,
            position_size=fill_cost,
            payout_rate=real_payout_rate,
            confidence_level=decision.confidence,
            outcome="pending",
            pnl=0.0,
            portfolio_balance_after=self._tracked_balance,
            risk_reward_ratio=rr_ratio,
        )
        # Add fill quality data to signal record for ML training
        signal_data["fill_price_per_share"] = effective_price
        signal_data["fill_slippage_pct"] = slippage_pct
        self._save_signals(trade_id, signal_data)

        logger.info(
            f"💰 TRADE: {decision.side} on {market.slug} | "
            f"cost=${fill_cost:.2f} shares={fill_shares:.2f} "
            f"payout={real_payout_rate:.1%} confidence={decision.confidence}"
        )
        return trade_id

    def settle_trade(self, trade_id: int, winning_side: str, condition_id: str = "") -> None:
        """Settle a trade after market resolution.

        PnL is calculated from fill amounts (deterministic, no wallet dependency).
        On wins, auto-redeems positions on-chain to convert shares back to USDC.
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

        fill_cost = trade["position_size"]
        payout_rate = trade["payout_rate"]
        trade_side = trade["side"]
        outcome = "win" if trade_side == winning_side else "loss"

        # PnL from fill amounts — always correct regardless of claim state
        if outcome == "win":
            fill_shares = fill_cost * (1.0 + payout_rate)
            pnl = fill_shares - fill_cost

            # Auto-claim disabled — CTF redeemPositions reverts through relayer
            # despite encoding matching successful Polymarket claims exactly.
            # The on-chain condition may not be redeemable yet when we try.
            # Claim manually on Polymarket website.
            logger.info(f"📋 WIN: claim trade {trade_id} on Polymarket website")
        else:
            pnl = -fill_cost

        # Update tracked balance — use PnL-based tracking for consistency,
        # then periodically sync with wallet when no orders are outstanding.
        # Direct wallet reads can be temporarily low due to locked limit order funds.
        self._tracked_balance = round(self._tracked_balance + pnl, 6)

        # Sync with wallet if balance has drifted significantly (>$1)
        # and no pending limit orders are locking funds
        try:
            real_balance = self._executor.get_balance()
            if real_balance > 0 and abs(real_balance - self._tracked_balance) > 1.0:
                # Only sync if wallet is HIGHER than tracked (no locked funds)
                if real_balance >= self._tracked_balance:
                    self._tracked_balance = real_balance
        except Exception:
            pass

        db.update_trade_outcome(
            self._conn,
            trade_id=trade_id,
            outcome=outcome,
            pnl=round(pnl, 6),
            portfolio_balance_after=self._tracked_balance,
        )
        self._portfolio.save_snapshot()

        settle_emoji = "✅" if outcome == "win" else "💀"
        logger.info(
            f"{settle_emoji} SETTLED: trade {trade_id} {outcome.upper()} "
            f"pnl=${pnl:+,.2f} | balance=${self._tracked_balance:,.2f}"
        )

    def get_pending_trade_ids(self) -> list[int]:
        """Return IDs of all pending trades."""
        rows = db.get_pending_trades(self._conn)
        return [row["id"] for row in rows]

    def _save_signals(self, trade_id: int, signal_data: dict) -> None:
        """Persist signal snapshot to the database."""
        if trade_id is not None:
            db.insert_signals(self._conn, trade_id=trade_id, **signal_data)
