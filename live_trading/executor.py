"""Order execution via the Polymarket CLOB API.

Handles placing market orders, checking balances, and redeeming winning positions.
All methods are synchronous (py-clob-client is sync).
"""

import logging
import sys
from typing import Optional

from eth_account import Account
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

import config

logger = logging.getLogger(__name__)

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# Polygon RPC endpoints (free, no API key needed)
POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
]

# Polymarket Conditional Tokens Framework contract on Polygon
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_REDEEM_ABI = [{
    "inputs": [
        {"name": "collateralToken", "type": "address"},
        {"name": "parentCollectionId", "type": "bytes32"},
        {"name": "conditionId", "type": "bytes32"},
        {"name": "indexSets", "type": "uint256[]"},
    ],
    "name": "redeemPositions",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function",
}]

# USDC on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Parent collection ID for top-level positions (no parent)
PARENT_COLLECTION_ID = bytes(32)


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
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=config.POLYMARKET_SIGNATURE_TYPE,
            )
            result = self._client.get_balance_allowance(params)
            # result has a 'balance' field in wei (USDC has 6 decimals)
            balance_raw = result.get("balance", "0") if isinstance(result, dict) else getattr(result, "balance", "0")
            return int(balance_raw) / 1e6
        except Exception as e:
            logger.error(f"Failed to fetch balance: {type(e).__name__}: {e}")
            return 0.0

    def place_market_order(
        self,
        token_id: str,
        amount: float,
        max_price: float = 0,
    ) -> Optional[dict]:
        """Place a market buy order (Fill-Or-Kill) with optional price limit.

        Args:
            token_id: the CLOB token ID for the side to buy (Up or Down)
            amount: dollar amount to spend (USDC)
            max_price: maximum price per share (0 = no limit)

        Returns:
            Order response dict on success, None on failure.
        """
        try:
            # CLOB requires maker amount to have max 2 decimal places
            amount = round(amount, 2)
            # Price must be rounded to 2 decimal places for the CLOB
            max_price = round(max_price, 2) if max_price > 0 else 0
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY,
                price=max_price,
                order_type=OrderType.FAK,
            )
            signed_order = self._client.create_market_order(order_args)
            response = self._client.post_order(signed_order, OrderType.FAK)

            # Extract actual fill amounts from CLOB response
            taking = float(response.get("takingAmount", 0)) if isinstance(response, dict) else 0
            making = float(response.get("makingAmount", 0)) if isinstance(response, dict) else 0

            # FAK may fill zero if book is completely empty
            if making == 0 or taking == 0:
                logger.warning(f"Order got zero fill (empty book): ${amount:.2f} on token {token_id[:16]}...")
                return None

            fill_pct = round(making / amount * 100, 1) if amount > 0 else 0
            logger.info(
                f"Order filled: ${making:.6f} of ${amount:.2f} ({fill_pct}%) | "
                f"shares={taking:.6f} -> {response}"
            )

            # Attach parsed fill amounts for live_simulator
            if isinstance(response, dict):
                response["_fill_cost"] = making      # USDC spent
                response["_fill_shares"] = taking     # shares received (payout if win)

            return response
        except Exception as e:
            logger.error(f"Order placement failed: {type(e).__name__}: {e}")
            return None

    def redeem_positions(self, condition_id: str) -> bool:
        """Redeem winning positions for a resolved market, converting shares back to USDC.

        Calls redeemPositions on the Conditional Tokens Framework contract on Polygon.
        This is the on-chain equivalent of clicking "Claim" on the Polymarket website.

        Args:
            condition_id: the market's conditionId (from MarketInfo)

        Returns:
            True if redemption tx was sent successfully, False otherwise.
        """
        if not condition_id:
            logger.warning("Cannot redeem: no condition_id")
            return False

        logger.info(f"Attempting to redeem positions for condition {condition_id[:16]}...")

        for rpc_url in POLYGON_RPCS:
            try:
                logger.info(f"Trying redemption via {rpc_url}...")
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
                if not w3.is_connected():
                    logger.warning(f"RPC {rpc_url} not connected, skipping")
                    continue

                account = Account.from_key(config.POLYMARKET_PRIVATE_KEY)
                logger.info(f"Redeeming from address: {account.address}")
                ctf = w3.eth.contract(
                    address=Web3.to_checksum_address(CTF_ADDRESS),
                    abi=CTF_REDEEM_ABI,
                )

                # Convert conditionId to bytes32
                condition_bytes = bytes.fromhex(condition_id.replace("0x", ""))

                # indexSets [1, 2] = redeem both outcomes (only winning one has value)
                tx = ctf.functions.redeemPositions(
                    Web3.to_checksum_address(USDC_ADDRESS),
                    PARENT_COLLECTION_ID,
                    condition_bytes,
                    [1, 2],
                ).build_transaction({
                    "from": account.address,
                    "nonce": w3.eth.get_transaction_count(account.address),
                    "gas": 300000,
                    "gasPrice": w3.eth.gas_price,
                    "chainId": CHAIN_ID,
                })

                signed = account.sign_transaction(tx)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

                if receipt["status"] == 1:
                    logger.info(f"Redemption successful: tx={tx_hash.hex()}")
                    return True
                else:
                    logger.warning(f"Redemption tx reverted: tx={tx_hash.hex()}")
                    return False

            except Exception as e:
                logger.warning(f"Redemption via {rpc_url} failed: {type(e).__name__}: {e}")
                continue

        logger.warning(f"Redemption failed for condition {condition_id[:16]}... (all RPCs failed)")
        return False
