"""Fetch market resolution (winning side) after a BTC 5-min market closes.

Tries CLOB API first (explicit winner boolean), falls back to Gamma API
(outcomePrices positional match). Handles the delay between market close
and resolution becoming available.
"""

import asyncio
import json
import logging
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)

# Resolution may not be immediate — Polymarket can take 5-10 minutes after close
MAX_RESOLUTION_ATTEMPTS = 60
RESOLUTION_RETRY_DELAY = 10  # seconds between retries (total: ~10 min)


async def resolve_market(
    condition_id: str,
    slug: str,
    client: httpx.AsyncClient | None = None,
) -> Optional[str]:
    """Wait for and return the winning side of a resolved market.

    Retries up to MAX_RESOLUTION_ATTEMPTS times with RESOLUTION_RETRY_DELAY
    between attempts, since resolution may lag behind market close.

    Returns: "Up", "Down", or None if resolution could not be determined.
    """
    close_client = client is None
    if close_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(config.SIGNAL_FETCH_TIMEOUT))
    try:
        for attempt in range(1, MAX_RESOLUTION_ATTEMPTS + 1):
            # Try CLOB first (most explicit)
            winner = await _resolve_via_clob(condition_id, client)
            if winner:
                logger.info(f"Market {slug} resolved: {winner} (CLOB, attempt {attempt})")
                return winner

            # Fallback to Gamma
            winner = await _resolve_via_gamma(slug, client)
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
        if close_client:
            await client.aclose()


async def _resolve_via_clob(
    condition_id: str,
    client: httpx.AsyncClient,
) -> Optional[str]:
    """Check CLOB API for tokens[].winner boolean."""
    url = f"{config.POLYMARKET_CLOB_URL}/markets/{condition_id}"
    try:
        resp = await client.get(url, timeout=config.SIGNAL_FETCH_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()

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
    client: httpx.AsyncClient,
) -> Optional[str]:
    """Check Gamma API for outcomePrices resolution."""
    url = f"{config.POLYMARKET_GAMMA_URL.replace('/markets', '/events')}?slug={slug}"
    try:
        resp = await client.get(url, timeout=config.SIGNAL_FETCH_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()

        if not data:
            return None
        event = data[0] if isinstance(data, list) else data
        markets = event.get("markets", [])
        if not markets:
            return None

        m = markets[0]

        # Check if actually resolved
        if not m.get("closed", False):
            return None

        outcomes = m.get("outcomes", [])
        prices = m.get("outcomePrices", [])

        # Gamma API sometimes returns these as JSON strings instead of arrays
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)

        if len(outcomes) < 2 or len(prices) < 2:
            return None

        # Winner has price "1", loser has price "0"
        if "1" in prices:
            winner_idx = prices.index("1")
            return outcomes[winner_idx]

        return None
    except Exception as e:
        logger.debug(f"Gamma resolution check failed: {e}")
        return None
