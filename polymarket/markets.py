"""Fetch BTC 5-minute Up/Down market metadata from Polymarket Gamma API."""

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

import config
from network_health import health

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


async def fetch_market_by_slug(slug: str, client: httpx.AsyncClient | None = None) -> MarketInfo | None:
    """Fetch a specific market by its slug with retry logic. Returns None on failure."""
    import asyncio as _asyncio

    url = f"{config.POLYMARKET_GAMMA_URL.replace('/markets', '/events')}?slug={slug}"

    close_client = client is None
    if close_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(15))

    try:
        for attempt in range(1, 3):
            try:
                resp = await client.get(url, timeout=15)
                if resp.status_code != 200:
                    logger.warning(f"fetch_market_by_slug({slug}) HTTP {resp.status_code} (attempt {attempt}/2)")
                    if attempt < 2:
                        await _asyncio.sleep(3)
                        continue
                    return None
                data = resp.json()
                if not data:
                    return None
                event = data[0] if isinstance(data, list) else data
                health.record("Gamma", True)
                return _parse_market(event)
            except (httpx.HTTPError, _asyncio.TimeoutError, TimeoutError, OSError, ConnectionError) as e:
                health.record("Gamma", False)
                logger.warning(f"fetch_market_by_slug({slug}) attempt {attempt}/2 failed: {type(e).__name__}")
                if attempt < 2:
                    await _asyncio.sleep(3)
                    continue
                return None
            except Exception as e:
                logger.error(f"fetch_market_by_slug({slug}) unexpected: {type(e).__name__}: {e}")
                return None
        return None
    finally:
        if close_client:
            await client.aclose()


async def fetch_next_market(client: httpx.AsyncClient | None = None) -> MarketInfo | None:
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
        market = await fetch_market_by_slug(slug, client=client)
        if market and market.end_time > now_utc:
            return market
    return None


async def fetch_current_market(client: httpx.AsyncClient | None = None) -> MarketInfo | None:
    """Find the currently active (not yet ended) BTC 5-minute market.

    Polymarket slugs use the candle START time. So at 5:42, the current
    candle started at 5:40 and its slug is btc-updown-5m-{5:40 unix}.

    If the API times out for the current candle, we construct a minimal
    MarketInfo from the slug rather than skipping ahead to a future candle.
    """
    now = time.time()
    now_utc = datetime.now(timezone.utc)
    candle_start = _current_candle_start(now)

    for offset in range(4):
        start_ts = candle_start + (offset * 300)
        end_ts = start_ts + 300
        slug = _build_slug(start_ts)

        # Skip if this candle has already ended
        end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
        if end_dt <= now_utc:
            logger.info(f"Market discovery: {slug} already ended, skipping")
            continue

        market = await fetch_market_by_slug(slug, client=client)
        if market is not None:
            if market.end_time <= now_utc:
                logger.info(f"Market discovery: {slug} already ended (API confirmed), skipping")
                continue
            logger.info(
                f"Market discovery: {slug} selected "
                f"(closed={market.closed}, ends={market.end_time.isoformat()})"
            )
            return market

        # API returned None — could be a timeout or genuinely not found.
        # For the current candle (offset 0), construct a fallback MarketInfo
        # so we don't skip ahead to a future candle due to network issues.
        if offset == 0:
            logger.warning(
                f"Market discovery: {slug} API failed — using fallback for current candle"
            )
            return MarketInfo(
                event_id="",
                market_id="",
                condition_id="",
                slug=slug,
                title=f"BTC 5-min (fallback) — ends {end_dt.strftime('%H:%M UTC')}",
                start_time=datetime.fromtimestamp(start_ts, tz=timezone.utc),
                end_time=end_dt,
                clob_token_id_up="",
                clob_token_id_down="",
                active=True,
                closed=False,
            )

        logger.info(f"Market discovery: {slug} not found on API")

    return None
