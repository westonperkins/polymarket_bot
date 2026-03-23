"""Fetch live BTC/USD price from the Chainlink oracle on Ethereum mainnet.

Calls latestRoundData() on the Chainlink BTC/USD aggregator proxy contract.
This is the same oracle network Polymarket uses for resolution.
"""

import struct
import logging
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

# Chainlink BTC/USD aggregator proxy on Ethereum mainnet
CHAINLINK_BTC_USD_CONTRACT = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"
# Function selector for latestRoundData() → (uint80, int256, uint256, uint256, uint80)
LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"
# Price has 8 decimals
CHAINLINK_DECIMALS = 8

# Public RPC endpoints (no API key needed), tried in order
PUBLIC_RPCS = [
    "https://eth.llamarpc.com",
    "https://rpc.ankr.com/eth",
    "https://ethereum-rpc.publicnode.com",
]


async def _eth_call(
    rpc_url: str,
    contract: str,
    data: str,
    session: aiohttp.ClientSession,
    timeout: float = config.SIGNAL_FETCH_TIMEOUT,
) -> Optional[str]:
    """Make an eth_call JSON-RPC request and return the hex result."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": contract, "data": data},
            "latest",
        ],
    }
    try:
        async with session.post(
            rpc_url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                return None
            body = await resp.json()
            result = body.get("result")
            if not result or result == "0x":
                return None
            return result
    except Exception as e:
        logger.debug(f"RPC call to {rpc_url} failed: {e}")
        return None


def _parse_latest_round_data(hex_result: str) -> Optional[dict]:
    """Parse the latestRoundData() return value.

    Returns: {round_id, price, started_at, updated_at, answered_in_round}
    """
    # Remove 0x prefix and decode 5 x 32-byte words
    raw = hex_result[2:]
    if len(raw) < 320:  # 5 * 64 hex chars
        return None

    words = [raw[i : i + 64] for i in range(0, 320, 64)]
    # answer is a signed int256 (word index 1)
    answer_raw = int(words[1], 16)
    # Handle two's complement for negative (shouldn't happen for BTC price)
    if answer_raw >= 2**255:
        answer_raw -= 2**256

    price = answer_raw / (10**CHAINLINK_DECIMALS)

    return {
        "round_id": int(words[0], 16),
        "price": price,
        "started_at": int(words[2], 16),
        "updated_at": int(words[3], 16),
        "answered_in_round": int(words[4], 16),
    }


async def fetch_chainlink_price(
    session: aiohttp.ClientSession,
) -> Optional[float]:
    """Fetch the current Chainlink BTC/USD oracle price.

    Tries multiple public RPC endpoints. Returns the price in USD or None on failure.
    Expects the shared aiohttp session from main.py.
    """
    for rpc_url in PUBLIC_RPCS:
        result = await _eth_call(
            rpc_url, CHAINLINK_BTC_USD_CONTRACT, LATEST_ROUND_DATA_SELECTOR, session
        )
        if result:
            parsed = _parse_latest_round_data(result)
            if parsed and parsed["price"] > 0:
                logger.debug(
                    f"Chainlink BTC/USD: ${parsed['price']:,.2f} (via {rpc_url})"
                )
                return parsed["price"]
    logger.warning("All Chainlink RPC endpoints failed")
    return None
