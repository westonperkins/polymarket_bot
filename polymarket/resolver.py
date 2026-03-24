"""Fetch market resolution (winning side) after a BTC 5-min market closes."""

import asyncio
import json
import logging
from typing import Callable, Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

MAX_RESOLUTION_ATTEMPTS = 60
RESOLUTION_RETRY_DELAY = 10


async def resolve_market(
    condition_id: str,
    slug: str,
    session: aiohttp.ClientSession | None = None,
    client_factory: Callable[[], aiohttp.ClientSession] | None = None,
) -> Optional[str]:
    """Wait for and return the winning side of a resolved market.

    If client_factory is provided, it's called on each attempt to get a fresh
    session — this survives session rotations that close old sessions.
    """
    close_session = session is None and client_factory is None
    if close_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config.SIGNAL_FETCH_TIMEOUT))

    try:
        for attempt in range(1, MAX_RESOLUTION_ATTEMPTS + 1):
            s = client_factory() if client_factory else session

            winner = await _resolve_via_clob(condition_id, s)
            if winner:
                logger.info(f"Market {slug} resolved: {winner} (CLOB, attempt {attempt})")
                return winner

            winner = await _resolve_via_gamma(slug, s)
            if winner:
                logger.info(f"Market {slug} resolved: {winner} (Gamma, attempt {attempt})")
                return winner

            if attempt < MAX_RESOLUTION_ATTEMPTS:
                logger.debug(
                    f"Resolution not yet available for {slug} "
                    f"(attempt {attempt}/{MAX_RESOLUTION_ATTEMPTS}), "
                    f"retrying in {RESOLUTION_RETRY_DELAY}s"
                )
                await asyncio.sleep(RESOLUTION_RETRY_DELAY)

        logger.warning(f"Could not resolve market {slug} after {MAX_RESOLUTION_ATTEMPTS} attempts")
        return None
    finally:
        if close_session and session:
            await session.close()


async def _resolve_via_clob(
    condition_id: str,
    session: aiohttp.ClientSession,
) -> Optional[str]:
    url = f"{config.POLYMARKET_CLOB_URL}/markets/{condition_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=config.SIGNAL_FETCH_TIMEOUT)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

            if not data.get("closed", False):
                return None
            tokens = data.get("tokens", [])
            for token in tokens:
                if token.get("winner") is True:
                    return token.get("outcome")
            return None
    except Exception as e:
        logger.debug(f"CLOB resolution check failed: {e}")
        return None


async def _resolve_via_gamma(
    slug: str,
    session: aiohttp.ClientSession,
) -> Optional[str]:
    url = f"{config.POLYMARKET_GAMMA_URL.replace('/markets', '/events')}?slug={slug}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=config.SIGNAL_FETCH_TIMEOUT)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

            if not data:
                return None
            event = data[0] if isinstance(data, list) else data
            markets = event.get("markets", [])
            if not markets:
                return None
            m = markets[0]

            if not m.get("closed", False):
                return None

            outcomes = m.get("outcomes", [])
            prices = m.get("outcomePrices", [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)
            if len(outcomes) < 2 or len(prices) < 2:
                return None

            if "1" in prices:
                winner_idx = prices.index("1")
                return outcomes[winner_idx]
            return None
    except Exception as e:
        logger.debug(f"Gamma resolution check failed: {e}")
        return None
