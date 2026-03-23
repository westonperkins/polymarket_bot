"""Order execution via the Polymarket CLOB API.

Handles placing market orders and checking balances.
All methods are synchronous (py-clob-client is sync).
"""

import logging
import sys
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

import config

logger = logging.getLogger(__name__)

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


def validate_live_credentials() -> tuple[bool, str]:
    """Check that all required live trading credentials are present.

    Returns (ok, error_message).
    """
    if not config.POLYMARKET_PRIVATE_KEY:
        return False, "POLYMARKET_PRIVATE_KEY is not set in .env"
    if not config.POLYMARKET_PRIVATE_KEY.startswith("0x"):
        return False, "POLYMARKET_PRIVATE_KEY must start with 0x"
    if len(config.POLYMARKET_PRIVATE_KEY) != 66:
        return False, f"POLYMARKET_PRIVATE_KEY wrong length ({len(config.POLYMARKET_PRIVATE_KEY)}, expected 66)"
    if not config.POLYMARKET_FUNDER_ADDRESS:
        return False, "POLYMARKET_FUNDER_ADDRESS is not set in .env"
    if not config.POLYMARKET_FUNDER_ADDRESS.startswith("0x"):
        return False, "POLYMARKET_FUNDER_ADDRESS must start with 0x"
    return True, ""


class Executor:
    """Wraps the Polymarket CLOB client for order execution."""

    def __init__(self) -> None:
        # Validate credentials before attempting connection
        ok, err = validate_live_credentials()
        if not ok:
            raise RuntimeError(f"Live trading credentials invalid: {err}")

        logger.info(f"Initializing CLOB client...")
        logger.info(f"  Host: {HOST}")
        logger.info(f"  Chain ID: {CHAIN_ID}")
        logger.info(f"  Funder: {config.POLYMARKET_FUNDER_ADDRESS}")
        logger.info(f"  Signature type: {config.POLYMARKET_SIGNATURE_TYPE} "
                     f"({'EOA' if config.POLYMARKET_SIGNATURE_TYPE == 0 else 'Magic/Email' if config.POLYMARKET_SIGNATURE_TYPE == 1 else 'Browser proxy'})")
        logger.info(f"  Private key: {config.POLYMARKET_PRIVATE_KEY[:6]}...{config.POLYMARKET_PRIVATE_KEY[-4:]}")

        try:
            self._client = ClobClient(
                HOST,
                key=config.POLYMARKET_PRIVATE_KEY,
                chain_id=CHAIN_ID,
                signature_type=config.POLYMARKET_SIGNATURE_TYPE,
                funder=config.POLYMARKET_FUNDER_ADDRESS,
            )
        except Exception as e:
            logger.error(f"ClobClient initialization failed: {type(e).__name__}: {e}")
            raise

        # Derive API credentials (this hits the CLOB auth endpoint)
        try:
            logger.info("Deriving API credentials from wallet signature...")
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            logger.info("API credentials set successfully")
        except Exception as e:
            logger.error(f"API credential derivation failed: {type(e).__name__}: {e}")
            logger.error(
                "This usually means:\n"
                "  1. The private key doesn't match the funder address\n"
                "  2. The funder address is wrong — for Magic/email accounts,\n"
                "     use your Polymarket PROXY wallet address (found in\n"
                "     Polymarket → Settings → Wallet), not your deposit address\n"
                "  3. The signature type is wrong — email accounts should use 1"
            )
            raise

        # Quick test — try to fetch balance to confirm auth works
        try:
            balance = self.get_balance()
            logger.info(f"Auth verified — wallet balance: ${balance:,.2f} USDC")
        except Exception as e:
            logger.warning(f"Could not verify balance (auth may still be ok): {e}")

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
            logger.error(f"Order placement failed: {type(e).__name__}: {e}")
            return None
