"""Fetch live odds for BTC 5-minute Up/Down markets from Polymarket."""

import json
from dataclasses import dataclass

import aiohttp

import config


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
    """Fetch current odds from the Gamma API (event endpoint).

    This is the simpler approach — uses outcomePrices from the market data.
    """
    url = f"{config.POLYMARKET_GAMMA_URL.replace('/markets', '/events')}?slug={slug}"
    timeout = aiohttp.ClientTimeout(total=config.SIGNAL_FETCH_TIMEOUT)

    close_session = session is None
    if close_session:
        session = aiohttp.ClientSession()
    try:
        async with session.get(url, timeout=timeout) as resp:
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

            return MarketOdds(
                up_price=up_price,
                down_price=down_price,
                spread=spread,
                tradeable=_is_tradeable(up_price),
            )
    finally:
        if close_session:
            await session.close()


async def fetch_odds_clob(
    condition_id: str,
    session: aiohttp.ClientSession | None = None,
) -> MarketOdds | None:
    """Fetch current odds from the CLOB API (more real-time).

    Uses the /markets/{conditionId} endpoint.
    """
    url = f"{config.POLYMARKET_CLOB_URL}/markets/{condition_id}"
    timeout = aiohttp.ClientTimeout(total=config.SIGNAL_FETCH_TIMEOUT)

    close_session = session is None
    if close_session:
        session = aiohttp.ClientSession()
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

            tokens = data.get("tokens", [])
            if len(tokens) < 2:
                return None

            up_token = next((t for t in tokens if t["outcome"] == "Up"), tokens[0])
            down_token = next((t for t in tokens if t["outcome"] == "Down"), tokens[1])

            up_price = float(up_token.get("price", 0.5))
            down_price = float(down_token.get("price", 0.5))

            return MarketOdds(
                up_price=up_price,
                down_price=down_price,
                spread=abs(up_price - down_price),
                tradeable=_is_tradeable(up_price),
            )
    finally:
        if close_session:
            await session.close()


async def fetch_odds(
    condition_id: str,
    slug: str,
    session: aiohttp.ClientSession | None = None,
) -> MarketOdds | None:
    """Fetch odds, preferring CLOB (more real-time) with Gamma as fallback."""
    odds = await fetch_odds_clob(condition_id, session=session)
    if odds is not None:
        return odds
    return await fetch_odds_gamma(slug, session=session)


def calculate_payout_rate(entry_odds: float) -> float:
    """Calculate the payout rate for a trade entered at the given odds.

    If you buy a share at 0.65, and it resolves correctly, you get $1.00,
    so payout_rate = (1.0 - entry_odds) / entry_odds.
    E.g. 0.65 → 0.538 (53.8% return on risk).
    """
    if entry_odds <= 0 or entry_odds >= 1:
        return 0.0
    return (1.0 - entry_odds) / entry_odds
