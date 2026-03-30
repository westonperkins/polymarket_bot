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
from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from py_builder_signing_sdk.config import BuilderConfig
from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

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
        self._last_order_error = ""

        # Step 1: Create a temporary client to derive API credentials
        try:
            tmp_client = ClobClient(
                HOST,
                key=config.POLYMARKET_PRIVATE_KEY,
                chain_id=CHAIN_ID,
                signature_type=config.POLYMARKET_SIGNATURE_TYPE,
                funder=config.POLYMARKET_FUNDER_ADDRESS,
            )
        except Exception as e:
            logger.error(f"ClobClient initialization failed: {type(e).__name__}: {e}")
            raise

        try:
            logger.info("Deriving API credentials from wallet signature...")
            creds = tmp_client.create_or_derive_api_creds()
            logger.info("API credentials derived successfully")
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

        # Step 2: Build BuilderConfig with the derived creds for proxy wallet settlement
        self._builder_creds = BuilderApiKeyCreds(
            key=creds.api_key,
            secret=creds.api_secret,
            passphrase=creds.api_passphrase,
        )
        builder_config = BuilderConfig(local_builder_creds=self._builder_creds)
        self._builder_config = builder_config
        logger.info("BuilderConfig created with local builder credentials")

        # Step 3: Create the real client with builder_config for proper on-chain settlement
        try:
            self._client = ClobClient(
                HOST,
                key=config.POLYMARKET_PRIVATE_KEY,
                chain_id=CHAIN_ID,
                signature_type=config.POLYMARKET_SIGNATURE_TYPE,
                funder=config.POLYMARKET_FUNDER_ADDRESS,
                builder_config=builder_config,
            )
            self._client.set_api_creds(creds)
            logger.info(f"ClobClient initialized with BuilderConfig (builder auth enabled: {self._client.can_builder_auth()})")
        except Exception as e:
            logger.error(f"ClobClient re-init with BuilderConfig failed: {type(e).__name__}: {e}")
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

            # Log warning if fill is below minimum price (but still record it — order already executed)
            price_per_share = making / taking if taking > 0 else 0
            if config.LIVE_MIN_FILL_PRICE > 0 and price_per_share < config.LIVE_MIN_FILL_PRICE:
                logger.warning(
                    f"⚠️  CHEAP FILL: ${price_per_share:.3f}/share below min ${config.LIVE_MIN_FILL_PRICE:.2f} | "
                    f"cost=${making:.2f} shares={taking:.2f} — trade still recorded (already executed on CLOB)"
                )

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
            # Return error details so caller can record the specific reason
            self._last_order_error = str(e)
            return None

    def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
    ) -> Optional[str]:
        """Place a GTC limit buy order.

        Args:
            token_id: the CLOB token ID for the side to buy
            price: limit price per share (e.g. 0.55)
            size: number of shares to buy

        Returns:
            Order ID string on success, None on failure.
        """
        try:
            price = round(price, 2)
            size = round(size, 2)
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY,
            )
            signed_order = self._client.create_order(order_args)
            response = self._client.post_order(signed_order, OrderType.GTC)

            order_id = None
            if isinstance(response, dict):
                order_id = response.get("orderID") or response.get("order_id") or response.get("id")

            logger.info(
                f"📋 Limit order placed: {size} shares @ ${price:.2f} on token {token_id[:16]}... "
                f"→ order_id={order_id}"
            )
            return order_id
        except Exception as e:
            logger.error(f"Limit order placement failed: {type(e).__name__}: {e}")
            self._last_order_error = str(e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order by ID."""
        try:
            self._client.cancel(order_id)
            logger.info(f"🗑️ Order cancelled: {order_id}")
            return True
        except Exception as e:
            logger.warning(f"Cancel failed for {order_id}: {type(e).__name__}: {e}")
            return False

    def get_order_status(self, order_id: str) -> Optional[dict]:
        """Get the current status of an order."""
        try:
            return self._client.get_order(order_id)
        except Exception as e:
            logger.debug(f"Get order failed for {order_id}: {e}")
            return None

    def get_trade_details(self, trade_id: str) -> Optional[dict]:
        """Look up a specific trade by ID to get actual fill amounts."""
        try:
            from py_clob_client.clob_types import TradeParams
            result = self._client.get_trades(TradeParams(id=trade_id))
            trades = result if isinstance(result, list) else result.get("data", []) if isinstance(result, dict) else []
            for t in trades:
                if t.get("id") == trade_id:
                    return t
            return trades[0] if trades else None
        except Exception as e:
            logger.debug(f"Get trade failed for {trade_id}: {e}")
            return None

    def redeem_positions(self, condition_id: str) -> bool:
        """Redeem winning positions via Polymarket's Builder Relayer.

        Submits a redeemPositions call through the relayer, which executes it
        through the proxy wallet that actually holds the conditional tokens.

        Args:
            condition_id: the market's conditionId (from MarketInfo)

        Returns:
            True if redemption was submitted successfully, False otherwise.
        """
        if not condition_id:
            logger.warning("Cannot redeem: no condition_id")
            return False

        logger.info(f"Attempting to redeem via relayer for condition {condition_id[:16]}...")

        try:
            import json
            import time
            import requests
            from eth_abi import encode as abi_encode

            # Encode redeemPositions calldata
            selector = Web3.keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
            condition_bytes = bytes.fromhex(condition_id.replace("0x", "")).ljust(32, b'\x00')[:32]
            calldata = selector + abi_encode(
                ['address', 'bytes32', 'bytes32', 'uint256[]'],
                [
                    Web3.to_checksum_address(USDC_ADDRESS),
                    PARENT_COLLECTION_ID,
                    condition_bytes,
                    [1, 2],
                ]
            )

            # Build the transaction payload for the relayer
            tx_payload = {
                "to": CTF_ADDRESS,
                "data": "0x" + calldata.hex(),
                "value": "0",
            }

            # Get signing key address
            signer_address = Account.from_key(config.POLYMARKET_PRIVATE_KEY).address

            body = json.dumps({
                "chainId": CHAIN_ID,
                "txType": "PROXY",
                "signerAddress": signer_address,
                "transactions": [tx_payload],
                "description": f"Redeem positions for {condition_id[:16]}",
            }, separators=(",", ":"))

            # Generate builder auth headers — try multiple path formats
            method = "POST"
            path = "/submit"
            headers_payload = self._builder_config.generate_builder_headers(method, path, body)
            if not headers_payload:
                logger.warning("Failed to generate builder headers for relayer")
                return False

            headers = {
                "Content-Type": "application/json",
                **headers_payload.to_dict(),
            }

            # Submit to relayer — try multiple auth approaches
            relayer_base = "https://relayer-v2.polymarket.com"
            response = None
            builder_headers = self._builder_config.generate_builder_headers(method, "/submit", body)

            # Approach 1: Builder headers only
            auth_approaches = [
                {
                    "Content-Type": "application/json",
                    **builder_headers.to_dict(),
                },
                # Approach 2: Builder headers + signer address
                {
                    "Content-Type": "application/json",
                    "POLY_ADDRESS": signer_address,
                    **builder_headers.to_dict(),
                },
                # Approach 3: Builder headers with lowercase
                {
                    "Content-Type": "application/json",
                    "poly-builder-api-key": self._builder_creds.key,
                    "poly-builder-passphrase": self._builder_creds.passphrase,
                    "poly-builder-signature": builder_headers.to_dict()["POLY_BUILDER_SIGNATURE"],
                    "poly-builder-timestamp": builder_headers.to_dict()["POLY_BUILDER_TIMESTAMP"],
                },
            ]

            for i, try_headers in enumerate(auth_approaches):
                response = requests.post(f"{relayer_base}/submit", data=body, headers=try_headers, timeout=30)
                logger.info(f"Relayer auth approach {i+1}: {response.status_code} - {response.text[:100]}")
                if response.status_code != 401:
                    break

            if response and (response.status_code == 200 or response.status_code == 201):
                result = response.json()
                tx_id = result.get("transactionId") or result.get("id") or result.get("txHash", "")
                logger.info(f"Relayer redemption submitted: {tx_id or result}")

                # Poll for completion if we got a transaction ID
                if tx_id:
                    for attempt in range(10):
                        time.sleep(3)
                        try:
                            poll_headers_payload = self._builder_config.generate_builder_headers(
                                "GET", f"/transactions/{tx_id}"
                            )
                            poll_headers = {**poll_headers_payload.to_dict()} if poll_headers_payload else {}
                            poll_resp = requests.get(
                                f"https://relayer-v2.polymarket.com/transactions/{tx_id}",
                                headers=poll_headers, timeout=10,
                            )
                            if poll_resp.status_code == 200:
                                status_data = poll_resp.json()
                                status = status_data.get("status", "").upper()
                                tx_hash = status_data.get("transactionHash", "")
                                if status in ("CONFIRMED", "SUCCESS", "MINED"):
                                    logger.info(f"Relayer redemption confirmed: tx={tx_hash}")
                                    return True
                                elif status in ("FAILED", "REVERTED"):
                                    logger.warning(f"Relayer redemption failed: {status_data}")
                                    return False
                                # else still pending, continue polling
                        except Exception:
                            pass
                    logger.info(f"Relayer redemption submitted but not confirmed after polling")
                    return True  # submitted successfully even if we couldn't confirm
                return True
            else:
                logger.warning(f"Relayer submit failed: {response.status_code} - {response.text[:200]}")
                return False

        except Exception as e:
            logger.warning(f"Relayer redemption failed: {type(e).__name__}: {e}")
            return False
