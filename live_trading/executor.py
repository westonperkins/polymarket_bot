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

        # Step 2: Build BuilderConfig with the derived creds for CLOB order settlement
        self._builder_creds = BuilderApiKeyCreds(
            key=creds.api_key,
            secret=creds.api_secret,
            passphrase=creds.api_passphrase,
        )
        builder_config = BuilderConfig(local_builder_creds=self._builder_creds)
        self._builder_config = builder_config
        logger.info("BuilderConfig created with local builder credentials")

        # Step 2b: Build separate BuilderConfig for relayer (auto-claim)
        if config.POLY_BUILDER_API_KEY and config.POLY_BUILDER_SECRET:
            self._relayer_builder_config = BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=config.POLY_BUILDER_API_KEY,
                    secret=config.POLY_BUILDER_SECRET,
                    passphrase=config.POLY_BUILDER_PASSPHRASE,
                )
            )
            logger.info("Relayer BuilderConfig created with builder API keys")
        else:
            self._relayer_builder_config = None
            logger.info("No builder API keys configured — auto-claim disabled")

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

        Follows the same flow as @polymarket/builder-relayer-client:
        1. Encode redeemPositions as proxy(calls[]) on the ProxyFactory
        2. Get relay payload (nonce + relay address) from relayer
        3. Create and sign the relay struct hash
        4. Submit signed request with builder auth headers

        Args:
            condition_id: the market's conditionId (from MarketInfo)

        Returns:
            True if redemption was submitted successfully, False otherwise.
        """
        if not condition_id:
            logger.warning("Cannot redeem: no condition_id")
            return False

        logger.info(f"Attempting to redeem via relayer for condition {condition_id[:16]}...")

        if not self._relayer_builder_config:
            logger.warning("No builder API keys configured — claim manually on Polymarket")
            return False

        try:
            import json
            import time
            import requests
            from eth_abi import encode as abi_encode

            RELAYER_BASE = "https://relayer-v2.polymarket.com"
            PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
            RELAY_HUB = "0xD216153c06E857cD7f72665E0aF1d7D82172F494"

            account = Account.from_key(config.POLYMARKET_PRIVATE_KEY)
            signer_address = account.address

            # Step 1: Encode redeemPositions calldata
            redeem_selector = Web3.keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
            condition_bytes = bytes.fromhex(condition_id.replace("0x", "")).ljust(32, b'\x00')[:32]
            redeem_calldata = redeem_selector + abi_encode(
                ['address', 'bytes32', 'bytes32', 'uint256[]'],
                [
                    Web3.to_checksum_address(USDC_ADDRESS),
                    PARENT_COLLECTION_ID,
                    condition_bytes,
                    [1, 2],
                ]
            )

            # Step 2: Encode as proxy(calls[]) — calls is tuple[] of (typeCode, to, value, data)
            # typeCode 0 = Call
            proxy_selector = Web3.keccak(text="proxy((uint8,address,uint256,bytes)[])")[:4]
            proxy_calldata = proxy_selector + abi_encode(
                ['(uint8,address,uint256,bytes)[]'],
                [[(0, Web3.to_checksum_address(CTF_ADDRESS), 0, redeem_calldata)]]
            )

            # Step 3: Get relay payload (nonce + relay address)
            builder_headers = self._relayer_builder_config.generate_builder_headers(
                "GET", "/relay-payload"
            )
            relay_headers = {**builder_headers.to_dict()} if builder_headers else {}
            relay_resp = requests.get(
                f"{RELAYER_BASE}/relay-payload",
                params={"address": signer_address, "type": "PROXY"},
                headers=relay_headers,
                timeout=10,
            )
            if relay_resp.status_code != 200:
                logger.warning(f"Relay payload failed: {relay_resp.status_code} - {relay_resp.text[:200]}")
                return False

            relay_data = relay_resp.json()
            relay_address = relay_data.get("address", "")
            nonce = relay_data.get("nonce", "0")
            logger.info(f"Relay payload: address={relay_address}, nonce={nonce}")

            # Step 4: Create struct hash for signing
            # Hash = keccak256("rlx:" + from + to + data + txFee + gasPrice + gasLimit + nonce + relayHub + relay)
            relay_hub_prefix = bytes.fromhex("726c783a")  # "rlx:"
            gas_limit = "10000000"
            gas_price = "0"
            tx_fee = "0"

            from eth_abi import encode as eth_encode
            data_to_hash = (
                relay_hub_prefix
                + bytes.fromhex(signer_address[2:].lower().zfill(40))
                + bytes.fromhex(PROXY_FACTORY[2:].lower().zfill(40))
                + proxy_calldata
                + int(tx_fee).to_bytes(32, 'big')
                + int(gas_price).to_bytes(32, 'big')
                + int(gas_limit).to_bytes(32, 'big')
                + int(nonce).to_bytes(32, 'big')
                + bytes.fromhex(RELAY_HUB[2:].lower().zfill(40))
                + bytes.fromhex(relay_address[2:].lower().zfill(40))
            )
            struct_hash = Web3.keccak(data_to_hash)

            # Step 5: Sign the hash (personal_sign — \x19Ethereum Signed Message:\nNN prefix)
            # The JS signMessage treats the hex hash as raw bytes
            from eth_account.messages import encode_defunct
            message = encode_defunct(primitive=struct_hash)
            signed = account.sign_message(message)
            signature = "0x" + signed.signature.hex()
            logger.debug(f"Struct hash: 0x{struct_hash.hex()}, sig: {signature[:20]}...")

            # Step 6: Derive proxy wallet address
            # proxy = getPolyProxyWalletAddress(signer) — we already know this
            proxy_wallet = config.POLYMARKET_FUNDER_ADDRESS

            # Step 7: Build request
            request = {
                "from": signer_address,
                "to": PROXY_FACTORY,
                "proxyWallet": proxy_wallet,
                "data": "0x" + proxy_calldata.hex(),
                "nonce": str(nonce),
                "signature": signature,
                "signatureParams": {
                    "gasPrice": gas_price,
                    "gasLimit": gas_limit,
                    "relayerFee": tx_fee,
                    "relayHub": RELAY_HUB,
                    "relay": relay_address,
                },
                "type": "PROXY",
                "metadata": f"Redeem {condition_id[:16]}",
            }

            request_body = json.dumps(request, separators=(",", ":"))

            # Step 8: Submit with builder auth
            submit_headers_payload = self._relayer_builder_config.generate_builder_headers(
                "POST", "/submit", request_body
            )
            submit_headers = {
                "Content-Type": "application/json",
                **submit_headers_payload.to_dict(),
            }

            # Log what we're sending for debugging
            logger.info(f"Relayer submit body keys: {list(request.keys())}")
            logger.info(f"Relayer submit headers: {list(submit_headers.keys())}")

            response = requests.post(
                f"{RELAYER_BASE}/submit",
                data=request_body,
                headers=submit_headers,
                timeout=30,
            )
            logger.info(f"Relayer submit: {response.status_code} - {response.text[:300]}")

            if response.status_code in (200, 201):
                result = response.json()
                tx_id = result.get("transactionID") or result.get("transactionId") or result.get("id", "")
                logger.info(f"Relayer redemption submitted: {tx_id or result}")

                # Poll for confirmation
                if tx_id:
                    for attempt in range(10):
                        time.sleep(3)
                        try:
                            poll_resp = requests.get(
                                f"{RELAYER_BASE}/transaction",
                                params={"id": tx_id},
                                timeout=10,
                            )
                            if poll_resp.status_code == 200:
                                status_data = poll_resp.json()
                                txns = status_data if isinstance(status_data, list) else [status_data]
                                for txn in txns:
                                    state = txn.get("state", "").upper()
                                    tx_hash = txn.get("transactionHash", "")
                                    if state in ("CONFIRMED", "MINED", "SUCCESS"):
                                        logger.info(f"Relayer redemption confirmed: tx={tx_hash}")
                                        return True
                                    elif state in ("FAILED", "REVERTED"):
                                        logger.warning(f"Relayer redemption failed: {txn}")
                                        return False
                        except Exception:
                            pass
                    logger.info("Relayer redemption submitted, confirmation pending")
                    return True
                return True
            else:
                logger.warning(f"Relayer submit failed: {response.status_code} - {response.text[:200]}")
                return False

        except Exception as e:
            logger.warning(f"Relayer redemption failed: {type(e).__name__}: {e}")
            import traceback
            logger.warning(traceback.format_exc())
            return False
