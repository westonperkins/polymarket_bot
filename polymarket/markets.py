"""Fetch BTC 5-minute Up/Down market metadata from Polymarket Gamma API."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

import config
from network_health import health

logger = logging.getLogger(__name__)


@dataclass
class MarketInfo:
    event_id: str
    market_id: str
    condition_id: str
    slug: str
    title: str
    start_time: datetime
    end_time: datetime
    clob_token_id_up: str
    clob_token_id_down: str
    active: bool
    closed: bool


def _current_candle_start(now_unix: float | None = None) -> int:
    now = now_unix or time.time()
    return int(now // 300) * 300


def _build_slug(close_unix: int) -> str:
    return f"btc-updown-5m-{close_unix}"


def _parse_market(event: dict) -> MarketInfo | None:
    markets = event.get("markets", [])
    if not markets:
        return None
    m = markets[0]

    outcomes = m.get("outcomes", [])
    clob_ids = m.get("clobTokenIds", [])
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(clob_ids, str):
        clob_ids = json.loads(clob_ids)
    if len(outcomes) < 2 or len(clob_ids) < 2:
        return None

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
    """Fetch a specific market by its slug with retry logic."""
    url = f"{config.POLYMARKET_GAMMA_URL.replace('/markets', '/events')}?slug={slug}"
    close_session = session is None
    if close_session:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        for attempt in range(1, 3):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.warning(f"fetch_market_by_slug({slug}) HTTP {resp.status} (attempt {attempt}/2)")
                        if attempt < 2:
                            await asyncio.sleep(3)
                            continue
                        return None
                    data = await resp.json()
                    if not data:
                        return None
                    event = data[0] if isinstance(data, list) else data
                    health.record("Gamma", True)
                    return _parse_market(event)
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError, ConnectionError) as e:
                health.record("Gamma", False)
                logger.warning(f"fetch_market_by_slug({slug}) attempt {attempt}/2 failed: {type(e).__name__}")
                if attempt < 2:
                    await asyncio.sleep(3)
                    continue
                return None
            except Exception as e:
                logger.error(f"fetch_market_by_slug({slug}) unexpected: {type(e).__name__}: {e}")
                return None
        return None
    finally:
        if close_session:
            await session.close()


async def fetch_next_market(session: aiohttp.ClientSession | None = None) -> MarketInfo | None:
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
    """Find the currently active BTC 5-minute market."""
    now = time.time()
    now_utc = datetime.now(timezone.utc)
    candle_start = _current_candle_start(now)

    for offset in range(4):
        start_ts = candle_start + (offset * 300)
        end_ts = start_ts + 300
        slug = _build_slug(start_ts)

        end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
        if end_dt <= now_utc:
            logger.info(f"Market discovery: {slug} already ended, skipping")
            continue

        market = await fetch_market_by_slug(slug, session=session)
        if market is not None:
            if market.end_time <= now_utc:
                logger.info(f"Market discovery: {slug} already ended (API confirmed), skipping")
                continue
            logger.info(
                f"Market discovery: {slug} selected "
                f"(closed={market.closed}, ends={market.end_time.isoformat()})"
            )
            return market

        if offset == 0:
            logger.warning(f"Market discovery: {slug} API failed — using fallback for current candle")
            return MarketInfo(
                event_id="", market_id="", condition_id="",
                slug=slug,
                title=f"BTC 5-min (fallback) — ends {end_dt.strftime('%H:%M UTC')}",
                start_time=datetime.fromtimestamp(start_ts, tz=timezone.utc),
                end_time=end_dt,
                clob_token_id_up="", clob_token_id_down="",
                active=True, closed=False,
            )

        logger.info(f"Market discovery: {slug} not found on API")

    return None
