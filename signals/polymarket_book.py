"""Polymarket CLOB order book bias signal.

Fetches the order book for both Up and Down tokens and compares
bid depth to determine which side has more buying pressure.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)


@dataclass
class PolymarketBookResult:
    """Order book bias from Polymarket CLOB."""
    up_bid_volume: float       # total bid size for Up token
    up_ask_volume: float       # total ask size for Up token
    down_bid_volume: float     # total bid size for Down token
    down_ask_volume: float     # total ask size for Down token
    bias: float                # positive = more Up buying, negative = more Down buying
    direction: str             # "bullish", "bearish", or "neutral"


async def fetch_polymarket_book(
    up_token_id: str,
    down_token_id: str,
    session: aiohttp.ClientSession,
) -> Optional[PolymarketBookResult]:
    """Fetch Polymarket CLOB order books for Up and Down tokens.

    Compares bid depth (buying pressure) between Up and Down sides.
    """
    if not up_token_id or not down_token_id:
        return None

    try:
        up_book = await _fetch_book(up_token_id, session)
        down_book = await _fetch_book(down_token_id, session)

        if up_book is None or down_book is None:
            return None

        up_bid_vol, up_ask_vol = up_book
        down_bid_vol, down_ask_vol = down_book

        total_bid = up_bid_vol + down_bid_vol
        if total_bid == 0:
            return PolymarketBookResult(
                up_bid_volume=0, up_ask_volume=0,
                down_bid_volume=0, down_ask_volume=0,
                bias=0, direction="neutral",
            )

        # Bias: what fraction of total bid volume is on Up vs Down
        # > 0.6 = strong Up bias, < 0.4 = strong Down bias
        up_fraction = up_bid_vol / total_bid

        # Also consider ask-side imbalance within each token
        # Heavy asks on Up = selling pressure against Up
        up_imbalance = (up_bid_vol - up_ask_vol) if (up_bid_vol + up_ask_vol) > 0 else 0
        down_imbalance = (down_bid_vol - down_ask_vol) if (down_bid_vol + down_ask_vol) > 0 else 0

        # Net bias: positive = bullish, negative = bearish
        net_bias = up_imbalance - down_imbalance

        # Classify direction
        if up_fraction > 0.6 or net_bias > 0:
            direction = "bullish"
        elif up_fraction < 0.4 or net_bias < 0:
            direction = "bearish"
        else:
            direction = "neutral"

        logger.debug(
            f"Polymarket book: Up bids={up_bid_vol:.1f} asks={up_ask_vol:.1f} | "
            f"Down bids={down_bid_vol:.1f} asks={down_ask_vol:.1f} | "
            f"up_frac={up_fraction:.2f} net_bias={net_bias:.1f} -> {direction}"
        )

        return PolymarketBookResult(
            up_bid_volume=up_bid_vol,
            up_ask_volume=up_ask_vol,
            down_bid_volume=down_bid_vol,
            down_ask_volume=down_ask_vol,
            bias=net_bias,
            direction=direction,
        )
    except Exception as e:
        logger.warning(f"Failed to fetch Polymarket book: {type(e).__name__}: {e}")
        return None


async def _fetch_book(
    token_id: str,
    session: aiohttp.ClientSession,
) -> Optional[tuple[float, float]]:
    """Fetch order book for a single token. Returns (bid_volume, ask_volume)."""
    url = f"{config.POLYMARKET_CLOB_URL}/book?token_id={token_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.debug(f"CLOB book HTTP {resp.status} for token {token_id[:16]}...")
                return None
            data = await resp.json()

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            bid_vol = sum(float(b.get("size", 0)) for b in bids)
            ask_vol = sum(float(a.get("size", 0)) for a in asks)

            return (bid_vol, ask_vol)
    except Exception as e:
        logger.debug(f"CLOB book fetch failed for token {token_id[:16]}...: {e}")
        return None
