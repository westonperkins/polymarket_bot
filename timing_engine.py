"""Timing engine — manages the market lifecycle and triggers signal analysis at T-30s."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional

import aiohttp

import config
from polymarket.markets import MarketInfo, fetch_current_market, fetch_next_market
from polymarket.odds import MarketOdds, fetch_odds

logger = logging.getLogger(__name__)


class TimingEngine:
    """Monitors Polymarket BTC 5-min markets and fires callbacks at key moments.

    Lifecycle for each market:
        1. Discover the current/next market
        2. Sleep until T-30s before close
        3. Fetch odds → check tradeable window
        4. If tradeable, call on_signal_window (signal fetch + vote + trade decision)
        5. Sleep until market close
        6. Call on_market_close (fetch resolution, settle P&L)
        7. Loop to next market
    """

    def __init__(self) -> None:
        self.current_market: Optional[MarketInfo] = None
        self.current_odds: Optional[MarketOdds] = None
        self.running: bool = False
        self._session: Optional[aiohttp.ClientSession] = None

        # Callbacks — set by main.py before calling run()
        self.on_signal_window: Optional[
            Callable[[MarketInfo, MarketOdds, aiohttp.ClientSession], Awaitable[None]]
        ] = None
        self.on_market_close: Optional[
            Callable[[MarketInfo, aiohttp.ClientSession], Awaitable[None]]
        ] = None
        self.on_market_discovered: Optional[
            Callable[[MarketInfo], Awaitable[None]]
        ] = None
        self.on_skip: Optional[
            Callable[[MarketInfo, str], Awaitable[None]]
        ] = None

    async def run(self) -> None:
        """Main loop — runs indefinitely, processing one market at a time."""
        self.running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300),
        )
        logger.info("Timing engine started")
        consecutive_failures = 0

        try:
            while self.running:
                try:
                    await self._process_one_market()
                    consecutive_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    consecutive_failures += 1
                    logger.error(f"Error in market cycle ({consecutive_failures} consecutive): {e}", exc_info=True)
                    # If session might be broken, recreate it
                    if consecutive_failures >= 3:
                        logger.warning("Multiple consecutive failures — recreating HTTP session")
                        try:
                            await self._session.close()
                        except Exception:
                            pass
                        self._session = aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(total=15),
                            connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300),
                        )
                        consecutive_failures = 0
                    await asyncio.sleep(10)
        except asyncio.CancelledError:
            logger.info("Timing engine cancelled")
        finally:
            await self._session.close()
            self._session = None
            self.running = False

    async def stop(self) -> None:
        """Signal the engine to stop after the current cycle."""
        self.running = False

    async def _process_one_market(self) -> None:
        """Discover a market, wait for T-30, analyze, wait for close, resolve."""
        # ── Step 1: Discover market ──────────────────────────────────
        market = await self._discover_market()
        if market is None:
            logger.warning("No market found, retrying in 30s")
            await asyncio.sleep(30)
            return

        self.current_market = market
        logger.info(f"Tracking market: {market.title} (closes {market.end_time})")

        if self.on_market_discovered:
            await self.on_market_discovered(market)

        # ── Step 2: Wait until T-30s before close ────────────────────
        await self._wait_until_signal_window(market)
        if not self.running:
            return

        # ── Step 3: Fetch odds and check tradeable window ────────────
        odds = await fetch_odds(market.condition_id, market.slug, session=self._session)
        self.current_odds = odds

        if odds is None:
            reason = "Failed to fetch odds"
            logger.warning(f"SKIP {market.slug}: {reason}")
            if self.on_skip:
                await self.on_skip(market, reason)
            await self._wait_until_close(market)
            return

        if not odds.tradeable:
            reason = f"Odds outside tradeable window (Up={odds.up_price:.3f})"
            logger.info(f"SKIP {market.slug}: {reason}")
            if self.on_skip:
                await self.on_skip(market, reason)
            await self._wait_until_close(market)
            return

        # ── Step 4: Signal window — trigger analysis + trade decision ─
        logger.info(
            f"SIGNAL WINDOW for {market.slug} | Up={odds.up_price:.3f} Down={odds.down_price:.3f}"
        )
        if self.on_signal_window:
            await self.on_signal_window(market, odds, self._session)

        # ── Step 5: Wait for market close ────────────────────────────
        await self._wait_until_close(market)
        if not self.running:
            return

        # ── Step 6: Market closed — resolve ──────────────────────────
        logger.info(f"Market closed: {market.slug}")
        if self.on_market_close:
            await self.on_market_close(market, self._session)

        self.current_market = None
        self.current_odds = None

    async def _discover_market(self) -> Optional[MarketInfo]:
        """Try to find the current or next market, with retries.

        Retries 5 times with 10s delays. On any exception, catches it,
        logs it, waits, and retries — never propagates up.
        """
        for attempt in range(1, 6):
            try:
                market = await fetch_current_market(session=self._session)
                if market:
                    return market
                logger.info(f"Market discovery attempt {attempt}/5: no market found, retrying in 10s")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Market discovery attempt {attempt}/5 error: {type(e).__name__}: {e}")
            if not self.running:
                return None
            await asyncio.sleep(10)
        logger.warning("Market discovery failed after 5 attempts")
        return None

    async def _wait_until_signal_window(self, market: MarketInfo) -> None:
        """Sleep until T-30s before market close."""
        target = market.end_time.timestamp() - config.ENTRY_SECONDS_BEFORE_CLOSE
        await self._sleep_until(target, label="signal window")

    async def _wait_until_close(self, market: MarketInfo) -> None:
        """Sleep until market close time (+ 5s buffer before triggering close callback)."""
        target = market.end_time.timestamp() + 5
        await self._sleep_until(target, label="market close")

    async def _sleep_until(self, target_unix: float, label: str = "") -> None:
        """Sleep until a target time, checking self.running periodically."""
        while self.running:
            remaining = target_unix - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                return
            sleep_time = min(remaining, 1.0)  # wake every 1s to check self.running
            await asyncio.sleep(sleep_time)

    def seconds_until_close(self) -> Optional[float]:
        """Return seconds until the current market closes, or None."""
        if self.current_market is None:
            return None
        return self.current_market.end_time.timestamp() - datetime.now(timezone.utc).timestamp()

    def seconds_until_signal_window(self) -> Optional[float]:
        """Return seconds until the T-30s signal window, or None."""
        if self.current_market is None:
            return None
        target = self.current_market.end_time.timestamp() - config.ENTRY_SECONDS_BEFORE_CLOSE
        return target - datetime.now(timezone.utc).timestamp()
