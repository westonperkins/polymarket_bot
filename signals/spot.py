"""Binance BTC/USDT spot price fetcher and momentum tracker.

Provides:
- Live spot price from Binance.com
- Rolling price history (every 5s in the 2-min active window, every 60s idle)
- Momentum calculation over 60s and 120s windows
- Cached spot price with 90s TTL to survive transient failures
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

import config
from network_health import health

logger = logging.getLogger(__name__)


@dataclass
class MomentumResult:
    """Price momentum over two windows."""
    current_price: float
    momentum_60s: float    # rate of change over 60s ($/s)
    momentum_120s: float   # rate of change over 120s ($/s)
    direction: str         # "bullish", "bearish", or "neutral"


@dataclass
class PriceSample:
    """A timestamped price observation."""
    timestamp: float  # Unix time
    price: float


class SpotTracker:
    """Tracks Binance BTC/USDT spot price and computes momentum.

    Samples arrive every 5s during the 2-min active window before close,
    and every 60s during idle periods (cache warming only).
    Call `get_momentum()` at T-30 to get the momentum signals.
    Call `reset()` between markets.
    """

    def __init__(self, max_samples: int = 100) -> None:
        self._history: deque[PriceSample] = deque(maxlen=max_samples)

    def reset(self) -> None:
        """Clear price history for a new market."""
        self._history.clear()

    def record(self, price: float, timestamp: float | None = None) -> None:
        """Record a price sample."""
        ts = timestamp or time.time()
        self._history.append(PriceSample(timestamp=ts, price=price))

    @property
    def latest_price(self) -> Optional[float]:
        """Return the most recent recorded price."""
        if not self._history:
            return None
        return self._history[-1].price

    def get_momentum(self) -> Optional[MomentumResult]:
        """Calculate momentum over 60s and 120s windows.

        Returns None if insufficient data.
        """
        if len(self._history) < 2:
            return None

        now = self._history[-1]
        price_60s_ago = self._find_price_at(now.timestamp - config.MOMENTUM_WINDOW_SHORT)
        price_120s_ago = self._find_price_at(now.timestamp - config.MOMENTUM_WINDOW_LONG)

        m60 = 0.0
        m120 = 0.0

        if price_60s_ago is not None:
            dt = now.timestamp - (now.timestamp - config.MOMENTUM_WINDOW_SHORT)
            m60 = (now.price - price_60s_ago) / dt if dt > 0 else 0.0

        if price_120s_ago is not None:
            dt = now.timestamp - (now.timestamp - config.MOMENTUM_WINDOW_LONG)
            m120 = (now.price - price_120s_ago) / dt if dt > 0 else 0.0

        # Determine direction based on both windows
        # Use a small threshold to avoid noise (< $0.10/s ≈ $6/min)
        threshold = 0.10
        if m60 > threshold and m120 > 0:
            direction = "bullish"
        elif m60 < -threshold and m120 < 0:
            direction = "bearish"
        else:
            direction = "neutral"

        return MomentumResult(
            current_price=now.price,
            momentum_60s=m60,
            momentum_120s=m120,
            direction=direction,
        )

    def _find_price_at(self, target_ts: float) -> Optional[float]:
        """Find the price closest to the target timestamp.

        Returns None if no sample exists within 35s of the target.
        The tolerance accommodates the 60s idle polling interval — the
        worst-case gap between samples is ~60s, so 35s ensures at least
        the nearest idle sample is matched.
        """
        best = None
        best_diff = float("inf")
        for sample in self._history:
            diff = abs(sample.timestamp - target_ts)
            if diff < best_diff:
                best_diff = diff
                best = sample.price
        if best_diff > 35.0:  # no sample within 35s of target
            return None
        return best


# ── Spot price cache ──────────────────────────────────────────────────
_cached_price: Optional[float] = None
_cached_at: float = 0.0


async def fetch_spot_price(
    session: aiohttp.ClientSession | None = None,
) -> Optional[float]:
    """Fetch BTC/USDT spot price from Binance.com with cache fallback.

    On failure, returns the last successful price if it's within SPOT_CACHE_TTL.
    Retries once after SPOT_RETRY_DELAY seconds before falling back to cache.
    """
    global _cached_price, _cached_at

    close_session = session is None
    if close_session:
        connector = aiohttp.TCPConnector(limit=10)
        session = aiohttp.ClientSession(connector=connector)
    try:
        price = await _fetch_binance(session)
        if price:
            _cached_price = price
            _cached_at = time.time()
            return price

        # First attempt failed — wait and retry once
        logger.info(f"Binance fetch failed, retrying in {config.SPOT_RETRY_DELAY}s")
        await asyncio.sleep(config.SPOT_RETRY_DELAY)
        price = await _fetch_binance(session)
        if price:
            _cached_price = price
            _cached_at = time.time()
            return price

        # Both attempts failed — fall back to cache
        if _cached_price and (time.time() - _cached_at) < config.SPOT_CACHE_TTL:
            logger.warning(
                f"Using cached spot price ${_cached_price:,.2f} "
                f"(age {time.time() - _cached_at:.0f}s)"
            )
            return _cached_price

        logger.error("Spot price unavailable: Binance failed and cache expired")
        return None
    finally:
        if close_session:
            await session.close()


async def _fetch_binance(session: aiohttp.ClientSession) -> Optional[float]:
    """Fetch from Binance.com API."""
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with session.get(config.BINANCE_SPOT_URL, timeout=timeout) as resp:
            if resp.status != 200:
                logger.debug(f"Binance spot API returned {resp.status}")
                return None
            data = await resp.json()
            price = float(data.get("price", 0))
            if price > 0:
                logger.debug(f"Binance BTC/USDT spot: ${price:,.2f}")
                health.record("Binance", True)
                return price
            return None
    except Exception as e:
        health.record("Binance", False)
        logger.warning(f"Binance spot fetch failed: {type(e).__name__}: {e}")
        return None
