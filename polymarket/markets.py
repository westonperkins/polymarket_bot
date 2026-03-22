"""Fetch BTC 5-minute Up/Down market metadata from Polymarket Gamma API."""

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

import config

logger = logging.getLogger(__name__)


@dataclass
class MarketInfo:
    """Metadata for a single BTC 5-minute Up/Down market."""
    event_id: str
    market_id: str
    condition_id: str
    slug: str
    title: str
    start_time: datetime      # opening price timestamp (UTC)
    end_time: datetime         # close/resolution timestamp (UTC)
    clob_token_id_up: str
    clob_token_id_down: str
    active: bool
    closed: bool


def _next_5min_close_timestamp(now_unix: float | None = None) -> int:
    """Return the Unix timestamp of the next 5-minute boundary (close time)."""
    now = now_unix or time.time()
    return int(math.ceil(now / 300) * 300)


def _build_slug(close_unix: int) -> str:
    return f"btc-updown-5m-{close_unix}"


def _parse_market(event: dict) -> MarketInfo | None:
    """Parse a Gamma API event response into a MarketInfo."""
    markets = event.get("markets", [])
    if not markets:
        return None
    m = markets[0]

    outcomes = m.get("outcomes", [])
    clob_ids = m.get("clobTokenIds", [])
    # Gamma API sometimes returns these as JSON strings instead of arrays
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(clob_ids, str):
        clob_ids = json.loads(clob_ids)
    if len(outcomes) < 2 or len(clob_ids) < 2:
        return None

    # Map token IDs to Up/Down
    up_idx = outcomes.index("Up") if "Up" in outcomes else 0
    down_idx = outcomes.index("Down") if "Down" in outcomes else 1

    start_str = m.get("eventStartTime") or event.get("startTime")
    end_str = m.get("endDate") or event.get("endDate")

    return MarketInfo(
        event_id=str(event.get("id", "")),
        market_id=str(m.get("id", "")),
        condition_id=m.get("conditionId", ""),
        slug=event.get("slug", ""),
        title=event.get("title", ""),
        start_time=datetime.fromisoformat(start_str.replace("Z", "+00:00")),
        end_time=datetime.fromisoformat(end_str.replace("Z", "+00:00")),
        clob_token_id_up=clob_ids[up_idx],
        clob_token_id_down=clob_ids[down_idx],
        active=m.get("active", False) if isinstance(m.get("active"), bool) else event.get("active", False),
        closed=m.get("closed", False) if isinstance(m.get("closed"), bool) else event.get("closed", False),
    )


async def fetch_market_by_slug(slug: str, session: aiohttp.ClientSession | None = None) -> MarketInfo | None:
    """Fetch a specific market by its slug."""
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
            return _parse_market(event)
    finally:
        if close_session:
            await session.close()


async def fetch_next_market(session: aiohttp.ClientSession | None = None) -> MarketInfo | None:
    """Find the next upcoming BTC 5-minute Up/Down market.

    Tries the next few 5-minute boundaries in case the immediate next one
    isn't listed yet.
    """
    now = time.time()
    for offset in range(0, 4):
        close_ts = _next_5min_close_timestamp(now) + (offset * 300)
        slug = _build_slug(close_ts)
        market = await fetch_market_by_slug(slug, session=session)
        if market and not market.closed:
            return market
    return None


async def fetch_current_market(session: aiohttp.ClientSession | None = None) -> MarketInfo | None:
    """Find the currently active (not yet closed) BTC 5-minute market.

    Checks the current 5-minute window and adjacent windows to ensure
    we never skip a candle. Markets close every 5 minutes, so we check
    the current boundary and the next few.
    """
    now = time.time()
    current_close = _next_5min_close_timestamp(now)

    # Check current and next boundaries to find the earliest open market
    for offset in range(4):
        close_ts = current_close + (offset * 300)
        slug = _build_slug(close_ts)
        market = await fetch_market_by_slug(slug, session=session)
        if market is None:
            logger.info(f"Market discovery: {slug} not found on API")
            continue
        if market.closed:
            logger.info(f"Market discovery: {slug} already closed, skipping")
            continue
        logger.info(f"Market discovery: {slug} is open — selecting")
        return market

    return None
