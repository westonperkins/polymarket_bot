"""Order book imbalance from Binance depth data.

Compares bid volume vs ask volume within 0.1% of current price.
Ratio > 1.5 = strong buy wall (bullish).
Ratio < 0.67 = strong sell wall (bearish).
"""

import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)


@dataclass
class OrderBookResult:
    """Order book imbalance snapshot."""
    bid_volume: float       # total bid volume within depth window (BTC)
    ask_volume: float       # total ask volume within depth window (BTC)
    ratio: float            # bid_volume / ask_volume
    mid_price: float        # midpoint of best bid/ask
    direction: str          # "bullish", "bearish", or "neutral"


async def fetch_orderbook(
    session: aiohttp.ClientSession | None = None,
) -> Optional[OrderBookResult]:
    """Fetch order book from Binance and compute bid/ask imbalance.

    Only counts volume within config.ORDERBOOK_DEPTH_PCT (0.1%) of mid price.
    Returns None on API failure.
    """
    timeout = aiohttp.ClientTimeout(total=config.SIGNAL_FETCH_TIMEOUT)
    close_session = session is None
    if close_session:
        session = aiohttp.ClientSession()
    try:
        async with session.get(config.BINANCE_DEPTH_URL, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning(f"Binance depth API returned {resp.status}")
                return None
            data = await resp.json()

        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return None

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid_price = (best_bid + best_ask) / 2.0
        depth_range = mid_price * config.ORDERBOOK_DEPTH_PCT

        lower_bound = mid_price - depth_range
        upper_bound = mid_price + depth_range

        bid_vol = sum(
            float(qty) for price, qty in bids
            if float(price) >= lower_bound
        )
        ask_vol = sum(
            float(qty) for price, qty in asks
            if float(price) <= upper_bound
        )

        ratio = bid_vol / ask_vol if ask_vol > 0 else float("inf")

        if ratio >= config.ORDERBOOK_BULLISH_RATIO:
            direction = "bullish"
        elif ratio <= config.ORDERBOOK_BEARISH_RATIO:
            direction = "bearish"
        else:
            direction = "neutral"

        logger.debug(
            f"Order book: bid={bid_vol:.4f} ask={ask_vol:.4f} ratio={ratio:.2f} → {direction}"
        )

        return OrderBookResult(
            bid_volume=bid_vol,
            ask_volume=ask_vol,
            ratio=ratio,
            mid_price=mid_price,
            direction=direction,
        )
    except Exception as e:
        logger.warning(f"Failed to fetch order book: {e}")
        return None
    finally:
        if close_session:
            await session.close()
