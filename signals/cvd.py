"""Cumulative Volume Delta (CVD) from Binance recent trades.

CVD = sum of (buy volume - sell volume) over a time window.
Positive CVD = aggressive buying pressure.
Negative CVD = aggressive selling pressure.

Binance marks each trade with isBuyerMaker:
  - isBuyerMaker=False → buyer is taker (aggressive buy)
  - isBuyerMaker=True  → seller is taker (aggressive sell)
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)


@dataclass
class CVDResult:
    """Cumulative Volume Delta over the configured window."""
    cvd: float              # net buy - sell volume in BTC
    buy_volume: float       # total aggressive buy volume in BTC
    sell_volume: float      # total aggressive sell volume in BTC
    trade_count: int        # number of trades in the window
    direction: str          # "bullish", "bearish", or "neutral"


async def fetch_cvd(
    session: aiohttp.ClientSession | None = None,
) -> Optional[CVDResult]:
    """Fetch recent trades from Binance and compute CVD over the last 120s.

    Returns None on API failure.
    """
    timeout = aiohttp.ClientTimeout(total=15)
    close_session = session is None
    if close_session:
        connector = aiohttp.TCPConnector(limit=10)
        session = aiohttp.ClientSession(connector=connector)
    try:
        async with session.get(config.BINANCE_TRADES_URL, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning(f"Binance trades API returned {resp.status}")
                return None
            trades = await resp.json()

        if not trades:
            return None

        cutoff = time.time() * 1000 - config.CVD_WINDOW * 1000  # ms
        buy_vol = 0.0
        sell_vol = 0.0
        count = 0

        for t in trades:
            if t["time"] < cutoff:
                continue
            qty = float(t["qty"])
            if t["isBuyerMaker"]:
                sell_vol += qty   # seller is taker → aggressive sell
            else:
                buy_vol += qty    # buyer is taker → aggressive buy
            count += 1

        cvd = buy_vol - sell_vol

        # Classify direction — require meaningful imbalance (>20% of total)
        total = buy_vol + sell_vol
        if total == 0:
            direction = "neutral"
        elif cvd / total > 0.20:
            direction = "bullish"
        elif cvd / total < -0.20:
            direction = "bearish"
        else:
            direction = "neutral"

        logger.debug(
            f"CVD: {cvd:+.4f} BTC ({count} trades, buy={buy_vol:.4f}, sell={sell_vol:.4f}) → {direction}"
        )

        return CVDResult(
            cvd=cvd,
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            trade_count=count,
            direction=direction,
        )
    except Exception as e:
        logger.warning(f"Failed to compute CVD: {type(e).__name__}: {e}")
        return None
    finally:
        if close_session:
            await session.close()
