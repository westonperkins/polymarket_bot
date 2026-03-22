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


def _current_candle_start(now_unix: float | None = None) -> int:
    """Return the Unix timestamp of the current candle's start time.

    Polymarket slugs use the candle START time (not close time).
    E.g. the 5:40-5:45 candle has slug btc-updown-5m-{5:40 unix}.
    """
    now = now_unix or time.time()
    return int(now // 300) * 300


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
    now_utc = datetime.now(timezone.utc)
    candle_start = _current_candle_start(now)
    for offset in range(1, 5):
        start_ts = candle_start + (offset * 300)
        slug = _build_slug(start_ts)
        market = await fetch_market_by_slug(slug, session=session)
        if market and market.end_time > now_utc:
            return market
    return None


async def fetch_current_market(session: aiohttp.ClientSession | None = None) -> MarketInfo | None:
    """Find the currently active (not yet ended) BTC 5-minute market.

    Polymarket slugs use the candle START time. So at 5:42, the current
    candle started at 5:40 and its slug is btc-updown-5m-{5:40 unix}.
    We check the current candle and the next few in case the current one
    has already ended.
    """
    now = time.time()
    now_utc = datetime.now(timezone.utc)
    candle_start = _current_candle_start(now)

    for offset in range(4):
        start_ts = candle_start + (offset * 300)
        slug = _build_slug(start_ts)
        market = await fetch_market_by_slug(slug, session=session)
        if market is None:
            logger.info(f"Market discovery: {slug} not found on API")
            continue
        if market.end_time <= now_utc:
            logger.info(f"Market discovery: {slug} already ended, skipping")
            continue
        logger.info(
            f"Market discovery: {slug} selected "
            f"(closed={market.closed}, ends={market.end_time.isoformat()})"
        )
        return market

    return None
