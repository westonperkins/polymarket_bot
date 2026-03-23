"""Liquidation pressure signal from Gate.io futures API.

Fetches recent BTC liquidation orders and classifies pressure:
- Long liquidations = bearish pressure (longs forced out -> sell pressure)
- Short liquidations = bullish pressure (shorts forced out -> buy pressure)

Gate.io size field: positive = long liquidated, negative = short liquidated.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)

GATEIO_LIQUIDATIONS_URL = (
    "https://api.gateio.ws/api/v4/futures/usdt/liq_orders?contract=BTC_USDT&limit=100"
)


@dataclass
class LiquidationResult:
    """Liquidation pressure over the configured window."""
    long_liquidated_usd: float    # total long liquidation value in USD
    short_liquidated_usd: float   # total short liquidation value in USD
    net_pressure: float           # positive = bullish (shorts liquidated), negative = bearish
    event_count: int              # number of liquidation events in window
    direction: str                # "bullish", "bearish", or "neutral"


async def fetch_liquidations(
    client: httpx.AsyncClient,
) -> Optional[LiquidationResult]:
    """Fetch recent BTC liquidations from Gate.io and compute pressure.

    Looks at liquidations in the last LIQUIDATION_WINDOW seconds (default 120s).
    Returns None on API failure. Expects the shared httpx client from main.py.
    """
    try:
        resp = await client.get(GATEIO_LIQUIDATIONS_URL)
        if resp.status_code != 200:
            logger.warning(f"Gate.io liquidations API returned {resp.status_code}")
            return None
        orders = resp.json()

        if not orders:
            return LiquidationResult(
                long_liquidated_usd=0.0,
                short_liquidated_usd=0.0,
                net_pressure=0.0,
                event_count=0,
                direction="neutral",
            )

        cutoff = time.time() - config.LIQUIDATION_WINDOW
        long_usd = 0.0
        short_usd = 0.0
        count = 0

        for order in orders:
            if order.get("time", 0) < cutoff:
                continue

            size = int(order.get("size", 0))
            fill_price = float(order.get("fill_price", 0))
            value_usd = abs(size) * fill_price

            if size > 0:
                # Positive size = long position liquidated
                long_usd += value_usd
            elif size < 0:
                # Negative size = short position liquidated
                short_usd += value_usd
            count += 1

        # Net pressure: shorts liquidated (bullish) minus longs liquidated (bearish)
        net = short_usd - long_usd
        total = long_usd + short_usd

        if total == 0:
            direction = "neutral"
        elif net / total > 0.3:
            direction = "bullish"
        elif net / total < -0.3:
            direction = "bearish"
        else:
            direction = "neutral"

        logger.debug(
            f"Liquidations: longs=${long_usd:,.0f} shorts=${short_usd:,.0f} "
            f"net=${net:+,.0f} ({count} events) -> {direction}"
        )

        return LiquidationResult(
            long_liquidated_usd=long_usd,
            short_liquidated_usd=short_usd,
            net_pressure=net,
            event_count=count,
            direction=direction,
        )
    except Exception as e:
        logger.warning(f"Failed to fetch liquidations: {type(e).__name__}: {e}")
        return None
