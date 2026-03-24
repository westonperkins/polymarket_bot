"""Fetch live BTC/USD price from the Chainlink oracle on Ethereum mainnet."""

import logging
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

CHAINLINK_BTC_USD_CONTRACT = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"
LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"
CHAINLINK_DECIMALS = 8

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
        "params": [{"to": contract, "data": data}, "latest"],
    }
    try:
        async with session.post(
            rpc_url, json=payload,
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
    raw = hex_result[2:]
    if len(raw) < 320:
        return None
    words = [raw[i : i + 64] for i in range(0, 320, 64)]
    answer_raw = int(words[1], 16)
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
    """Fetch the current Chainlink BTC/USD oracle price."""
    for rpc_url in PUBLIC_RPCS:
        result = await _eth_call(
            rpc_url, CHAINLINK_BTC_USD_CONTRACT, LATEST_ROUND_DATA_SELECTOR, session
        )
        if result:
            parsed = _parse_latest_round_data(result)
            if parsed and parsed["price"] > 0:
                logger.debug(f"Chainlink BTC/USD: ${parsed['price']:,.2f} (via {rpc_url})")
                return parsed["price"]
    logger.warning("All Chainlink RPC endpoints failed")
    return None
