"""Fetch live odds for BTC 5-minute Up/Down markets from Polymarket."""

import asyncio
import json
import logging
from dataclasses import dataclass

import aiohttp

import config
from network_health import health

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_RETRY_DELAY = 2  # seconds
_CLOB_TIMEOUT = 15
_GAMMA_TIMEOUT = 15


@dataclass
class MarketOdds:
    """Current odds snapshot for a market."""
    up_price: float       # price of "Up" share (0.0–1.0)
    down_price: float     # price of "Down" share (0.0–1.0)
    spread: float         # best ask - best bid for the traded side
    tradeable: bool       # True if odds are within the tradeable window


def _is_tradeable(up_price: float) -> bool:
    """Check if the market odds are within the tradeable window."""
    return config.ODDS_LOWER_BOUND <= up_price <= config.ODDS_UPPER_BOUND


async def fetch_odds_gamma(
    slug: str,
    session: aiohttp.ClientSession | None = None,
) -> MarketOdds | None:
    """Fetch current odds from the Gamma API with retry logic.

    Uses outcomePrices from the market data. Retries 3 times with 5s delays.
    """
    url = f"{config.POLYMARKET_GAMMA_URL.replace('/markets', '/events')}?slug={slug}"

    close_session = session is None
    if close_session:
        session = aiohttp.ClientSession()
    try:
        timeout = aiohttp.ClientTimeout(total=_GAMMA_TIMEOUT)
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning(f"Gamma odds HTTP {resp.status}")
                return None
            data = await resp.json()
            if not data:
                return None
            event = data[0] if isinstance(data, list) else data
            markets = event.get("markets", [])
            if not markets:
                return None
            m = markets[0]

            outcomes = m.get("outcomes", [])
            prices = m.get("outcomePrices", [])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)
            if len(outcomes) < 2 or len(prices) < 2:
                return None

            up_idx = outcomes.index("Up") if "Up" in outcomes else 0
            down_idx = outcomes.index("Down") if "Down" in outcomes else 1

            up_price = float(prices[up_idx])
            down_price = float(prices[down_idx])
            spread = float(m.get("spread", 0.0))

            health.record("Gamma", True)
            return MarketOdds(
                up_price=up_price,
                down_price=down_price,
                spread=spread,
                tradeable=_is_tradeable(up_price),
            )
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError, ConnectionError) as e:
        health.record("Gamma", False)
        logger.warning(f"Gamma odds failed: {type(e).__name__}")
        return None
    except Exception as e:
        health.record("Gamma", False)
        logger.error(f"Gamma odds unexpected: {type(e).__name__}: {e}")
        return None
    finally:
        if close_session:
            await session.close()


async def fetch_odds_clob(
    condition_id: str,
    session: aiohttp.ClientSession | None = None,
) -> MarketOdds | None:
    """Fetch current odds from the CLOB API (more real-time).

    Uses the /markets/{conditionId} endpoint. Single attempt — retries
    are handled by the outer fetch_odds function.
    """
    url = f"{config.POLYMARKET_CLOB_URL}/markets/{condition_id}"

    close_session = session is None
    if close_session:
        session = aiohttp.ClientSession()
    try:
        timeout = aiohttp.ClientTimeout(total=_CLOB_TIMEOUT)
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning(f"CLOB odds HTTP {resp.status}")
                return None
            data = await resp.json()

            tokens = data.get("tokens", [])
            if len(tokens) < 2:
                return None

            up_token = next((t for t in tokens if t["outcome"] == "Up"), tokens[0])
            down_token = next((t for t in tokens if t["outcome"] == "Down"), tokens[1])

            up_price = float(up_token.get("price", 0.5))
            down_price = float(down_token.get("price", 0.5))

            health.record("CLOB", True)
            return MarketOdds(
                up_price=up_price,
                down_price=down_price,
                spread=abs(up_price - down_price),
                tradeable=_is_tradeable(up_price),
            )
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError, ConnectionError) as e:
        health.record("CLOB", False)
        logger.warning(f"CLOB odds failed: {type(e).__name__}")
        return None
    except Exception as e:
        health.record("CLOB", False)
        logger.error(f"CLOB odds unexpected: {type(e).__name__}: {e}")
        return None
    finally:
        if close_session:
            await session.close()


async def fetch_odds(
    condition_id: str,
    slug: str,
    session: aiohttp.ClientSession | None = None,
) -> MarketOdds | None:
    """Fetch odds with retry logic. Tries CLOB first, falls back to Gamma.

    Retries up to 2 times with 2s delay. Worst case: ~64s (2 rounds × 2 sources × 15s timeout + delays).
    Returns None if all attempts fail (the caller skips the candle gracefully).
    """
    for attempt in range(1, _MAX_RETRIES + 1):
        # Try CLOB first (more real-time)
        odds = await fetch_odds_clob(condition_id, session=session)
        if odds is not None:
            return odds

        # Fall back to Gamma
        logger.info(f"CLOB failed, trying Gamma (attempt {attempt}/{_MAX_RETRIES})")
        odds = await fetch_odds_gamma(slug, session=session)
        if odds is not None:
            return odds

        if attempt < _MAX_RETRIES:
            logger.warning(f"Both sources failed (attempt {attempt}/{_MAX_RETRIES}), retrying in {_RETRY_DELAY}s...")
            await asyncio.sleep(_RETRY_DELAY)

    logger.warning("All odds fetch attempts failed — skipping this candle")
    return None


def calculate_payout_rate(entry_odds: float) -> float:
    """Calculate the payout rate for a trade entered at the given odds.

    If you buy a share at 0.65, and it resolves correctly, you get $1.00,
    so payout_rate = (1.0 - entry_odds) / entry_odds.
    E.g. 0.65 → 0.538 (53.8% return on risk).
    """
    if entry_odds <= 0 or entry_odds >= 1:
        return 0.0
    return (1.0 - entry_odds) / entry_odds
