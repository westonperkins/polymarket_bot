"""Order execution via the Polymarket CLOB API.

Handles placing market orders and checking balances.
All methods are synchronous (py-clob-client is sync).
"""

import logging
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

import config

logger = logging.getLogger(__name__)

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


class Executor:
    """Wraps the Polymarket CLOB client for order execution."""

    def __init__(self) -> None:
        self._client = ClobClient(
            HOST,
            key=config.POLYMARKET_PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=config.POLYMARKET_SIGNATURE_TYPE,
            funder=config.POLYMARKET_FUNDER_ADDRESS,
        )
        self._client.set_api_creds(self._client.create_or_derive_api_creds())
        logger.info("Polymarket CLOB client initialized")

    def get_balance(self) -> float:
        """Return the account's USDC balance."""
        try:
            balance_wei = self._client.get_balance()
            return int(balance_wei) / 1e6
        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            return 0.0

    def place_market_order(
        self,
        token_id: str,
        amount: float,
    ) -> Optional[dict]:
        """Place a market buy order (Fill-Or-Kill).

        Args:
            token_id: the CLOB token ID for the side to buy (Up or Down)
            amount: dollar amount to spend (USDC)

        Returns:
            Order response dict on success, None on failure.
        """
        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY,
                order_type=OrderType.FOK,
            )
            signed_order = self._client.create_market_order(order_args)
            response = self._client.post_order(signed_order, OrderType.FOK)
            logger.info(f"Order placed: ${amount:.2f} on token {token_id[:16]}... -> {response}")
            return response
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return None
