"""Liquidation pressure signal from Gate.io futures API."""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

GATEIO_LIQUIDATIONS_URL = (
    "https://api.gateio.ws/api/v4/futures/usdt/liq_orders?contract=BTC_USDT&limit=100"
)


@dataclass
class LiquidationResult:
    long_liquidated_usd: float
    short_liquidated_usd: float
    net_pressure: float
    event_count: int
    direction: str


async def fetch_liquidations(
    session: aiohttp.ClientSession,
) -> Optional[LiquidationResult]:
    """Fetch recent BTC liquidations from Gate.io and compute pressure."""
    try:
        async with session.get(GATEIO_LIQUIDATIONS_URL) as resp:
            if resp.status != 200:
                logger.warning(f"Gate.io liquidations API returned {resp.status}")
                return None
            orders = await resp.json()

            if not orders:
                return LiquidationResult(
                    long_liquidated_usd=0.0, short_liquidated_usd=0.0,
                    net_pressure=0.0, event_count=0, direction="neutral",
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
                    long_usd += value_usd
                elif size < 0:
                    short_usd += value_usd
                count += 1

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

            logger.debug(f"Liquidations: longs=${long_usd:,.0f} shorts=${short_usd:,.0f} net=${net:+,.0f} ({count} events) -> {direction}")

            return LiquidationResult(
                long_liquidated_usd=long_usd, short_liquidated_usd=short_usd,
                net_pressure=net, event_count=count, direction=direction,
            )
    except Exception as e:
        logger.warning(f"Failed to fetch liquidations: {type(e).__name__}: {e}")
        return None
