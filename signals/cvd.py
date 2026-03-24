"""Cumulative Volume Delta (CVD) from Binance recent trades."""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)


@dataclass
class CVDResult:
    cvd: float
    buy_volume: float
    sell_volume: float
    trade_count: int
    direction: str


async def fetch_cvd(
    session: aiohttp.ClientSession,
) -> Optional[CVDResult]:
    """Fetch recent trades from Binance and compute CVD over the last 120s."""
    try:
        async with session.get(config.BINANCE_TRADES_URL) as resp:
            if resp.status != 200:
                logger.warning(f"Binance trades API returned {resp.status}")
                return None
            trades = await resp.json()

            if not trades:
                return None

            cutoff = time.time() * 1000 - config.CVD_WINDOW * 1000
            buy_vol = 0.0
            sell_vol = 0.0
            count = 0

            for t in trades:
                if t["time"] < cutoff:
                    continue
                qty = float(t["qty"])
                if t["isBuyerMaker"]:
                    sell_vol += qty
                else:
                    buy_vol += qty
                count += 1

            cvd = buy_vol - sell_vol
            total = buy_vol + sell_vol
            if total == 0:
                direction = "neutral"
            elif cvd / total > 0.20:
                direction = "bullish"
            elif cvd / total < -0.20:
                direction = "bearish"
            else:
                direction = "neutral"

            logger.debug(f"CVD: {cvd:+.4f} BTC ({count} trades) -> {direction}")

            return CVDResult(
                cvd=cvd, buy_volume=buy_vol, sell_volume=sell_vol,
                trade_count=count, direction=direction,
            )
    except Exception as e:
        logger.warning(f"Failed to compute CVD: {type(e).__name__}: {e}")
        return None
