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
    """Monitors Polymarket BTC 5-min markets and fires callbacks at key moments."""

    def __init__(self) -> None:
        self.current_market: Optional[MarketInfo] = None
        self.current_odds: Optional[MarketOdds] = None
        self.running: bool = False
        self._session: Optional[aiohttp.ClientSession] = None

        self.on_signal_window: Optional[
            Callable[[MarketInfo, MarketOdds, aiohttp.ClientSession], Awaitable[None]]
        ] = None
        self.on_limit_entry_window: Optional[
            Callable[[MarketInfo, MarketOdds, aiohttp.ClientSession], Awaitable[None]]
        ] = None
        self.on_cancel_window: Optional[
            Callable[[MarketInfo], Awaitable[None]]
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

    def _create_session(self) -> aiohttp.ClientSession:
        """Create a fresh aiohttp session."""
        connector = aiohttp.TCPConnector(
            limit=20,
            limit_per_host=10,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        return aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
        )

    async def run(self) -> None:
        """Main loop — runs indefinitely, processing one market at a time."""
        self.running = True
        self._session = self._create_session()
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
                    if consecutive_failures >= 3:
                        logger.warning("Multiple consecutive failures — recreating HTTP session")
                        try:
                            await self._session.close()
                        except Exception:
                            pass
                        self._session = self._create_session()
                        consecutive_failures = 0
                    await asyncio.sleep(10)
        except asyncio.CancelledError:
            logger.info("Timing engine cancelled")
        finally:
            await self._session.close()
            self._session = None
            self.running = False

    async def stop(self) -> None:
        self.running = False

    async def _process_one_market(self) -> None:
        market = await self._discover_market()
        if market is None:
            logger.warning("No market found, retrying in 30s")
            await asyncio.sleep(30)
            return

        self.current_market = market
        logger.info(f"Tracking market: {market.title} (closes {market.end_time})")

        if self.on_market_discovered:
            await self.on_market_discovered(market)

        # ── Step 2a: Limit entry window (T-120) if enabled ─────────────
        if config.LIMIT_ORDER_ENABLED and self.on_limit_entry_window:
            await self._wait_until_limit_entry(market)
            if not self.running:
                return

            odds_early = await fetch_odds(market.condition_id, market.slug, session=self._session)
            if odds_early and odds_early.tradeable:
                logger.info(
                    f"LIMIT ENTRY WINDOW for {market.slug} | "
                    f"Up={odds_early.up_price:.3f} Down={odds_early.down_price:.3f}"
                )
                await self.on_limit_entry_window(market, odds_early, self._session)

        # ── Step 2b: Signal window (T-30) ──────────────────────────────
        await self._wait_until_signal_window(market)
        if not self.running:
            return

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

        logger.info(f"SIGNAL WINDOW for {market.slug} | Up={odds.up_price:.3f} Down={odds.down_price:.3f}")
        if self.on_signal_window:
            await self.on_signal_window(market, odds, self._session)

        # ── Step 2c: Cancel window (T-15) if limit orders enabled ──────
        if config.LIMIT_ORDER_ENABLED and self.on_cancel_window:
            await self._wait_until_cancel(market)
            if not self.running:
                return
            await self.on_cancel_window(market)

        await self._wait_until_close(market)
        if not self.running:
            return

        logger.info(f"Market closed: {market.slug}")
        if self.on_market_close:
            await self.on_market_close(market, self._session)

        self.current_market = None
        self.current_odds = None

        logger.debug(f"Post-close cooldown: {config.POST_CLOSE_DELAY}s before next discovery")
        await asyncio.sleep(config.POST_CLOSE_DELAY)

    async def _discover_market(self) -> Optional[MarketInfo]:
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

    async def _wait_until_limit_entry(self, market: MarketInfo) -> None:
        target = market.end_time.timestamp() - config.LIMIT_ENTRY_SECONDS_BEFORE_CLOSE
        await self._sleep_until(target, label="limit entry")

    async def _wait_until_signal_window(self, market: MarketInfo) -> None:
        target = market.end_time.timestamp() - config.ENTRY_SECONDS_BEFORE_CLOSE
        await self._sleep_until(target, label="signal window")

    async def _wait_until_cancel(self, market: MarketInfo) -> None:
        target = market.end_time.timestamp() - config.LIMIT_CANCEL_SECONDS_BEFORE_CLOSE
        await self._sleep_until(target, label="cancel window")

    async def _wait_until_close(self, market: MarketInfo) -> None:
        target = market.end_time.timestamp() + 5
        await self._sleep_until(target, label="market close")

    async def _sleep_until(self, target_unix: float, label: str = "") -> None:
        while self.running:
            remaining = target_unix - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                return
            sleep_time = min(remaining, 1.0)
            await asyncio.sleep(sleep_time)

    def seconds_until_close(self) -> Optional[float]:
        if self.current_market is None:
            return None
        return self.current_market.end_time.timestamp() - datetime.now(timezone.utc).timestamp()

    def seconds_until_signal_window(self) -> Optional[float]:
        if self.current_market is None:
            return None
        target = self.current_market.end_time.timestamp() - config.ENTRY_SECONDS_BEFORE_CLOSE
        return target - datetime.now(timezone.utc).timestamp()
