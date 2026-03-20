"""Entry point — wires all components together and runs the bot."""

import asyncio
import logging
import signal
import sys

import aiohttp
from rich.live import Live

import config
from database import db
from dashboard.display import Dashboard
from models import momentum_model, reversion_model, structure_model
from models.ensemble import decide
from paper_trading.portfolio import Portfolio
from paper_trading.simulator import Simulator
from polymarket.markets import MarketInfo
from polymarket.odds import MarketOdds
from polymarket.resolver import resolve_market
from web.server import start_web_server
from signals.chainlink import fetch_chainlink_price
from signals.spot import fetch_spot_price, SpotTracker
from signals.cvd import fetch_cvd
from signals.orderbook import fetch_orderbook
from signals.liquidations import fetch_liquidations
from signals.market_structure import compute_round_number, get_time_regime, compute_streak
from timing_engine import TimingEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger(__name__)

# ── Shared state ────────────────────────────────────────────────────────
conn = db.get_connection()
portfolio = Portfolio(conn)
simulator = Simulator(conn, portfolio)
engine = TimingEngine()
spot_tracker = SpotTracker()
dashboard = Dashboard(engine, portfolio, conn)

# Track pending trades for resolution: market_slug → trade_id
pending_trades: dict[str, int] = {}


# ── Spot price polling task ─────────────────────────────────────────────
async def poll_spot_price():
    """Continuously sample spot price every 5s for momentum tracking."""
    async with aiohttp.ClientSession() as session:
        while engine.running:
            price = await fetch_spot_price(session=session)
            if price:
                spot_tracker.record(price)
            await asyncio.sleep(config.MOMENTUM_POLL_INTERVAL)


# ── Timing engine callbacks ────────────────────────────────────────────

async def on_market_discovered(market: MarketInfo):
    """Called when a new market is found."""
    spot_tracker.reset()
    dashboard.last_signals = None
    dashboard.last_decision = None
    dashboard.status_message = (
        f"Tracking: {market.title} — waiting for signal window"
    )
    logger.info(f"Market discovered: {market.title}")


async def on_skip(market: MarketInfo, reason: str):
    """Called when a market is skipped (bad odds or fetch failure)."""
    # Record skip in DB
    skip_decision = decide("ABSTAIN", "ABSTAIN", "ABSTAIN")
    simulator.enter_trade(
        market,
        engine.current_odds or MarketOdds(0.5, 0.5, 0.0, False),
        skip_decision,
        {"time_regime": get_time_regime()},
    )
    dashboard.status_message = f"SKIPPED: {reason}"
    logger.info(f"Skip recorded: {market.slug} — {reason}")


async def on_signal_window(
    market: MarketInfo,
    odds: MarketOdds,
    session: aiohttp.ClientSession,
):
    """Called at T-30s — fetch all signals, vote, and enter trade."""
    dashboard.status_message = "SIGNAL WINDOW — fetching signals..."

    # ── Fetch all signals in parallel ───────────────────────────────
    chainlink_task = fetch_chainlink_price(session=session)
    spot_task = fetch_spot_price(session=session)
    cvd_task = fetch_cvd(session=session)
    orderbook_task = fetch_orderbook(session=session)
    liq_task = fetch_liquidations(session=session)

    chainlink_price, spot_price, cvd_result, ob_result, liq_result = (
        await asyncio.gather(
            chainlink_task, spot_task, cvd_task, orderbook_task, liq_task,
            return_exceptions=True,
        )
    )

    # Convert exceptions to None
    if isinstance(chainlink_price, Exception):
        logger.warning(f"Chainlink fetch error: {chainlink_price}")
        chainlink_price = None
    if isinstance(spot_price, Exception):
        logger.warning(f"Spot fetch error: {spot_price}")
        spot_price = None
    if isinstance(cvd_result, Exception):
        logger.warning(f"CVD fetch error: {cvd_result}")
        cvd_result = None
    if isinstance(ob_result, Exception):
        logger.warning(f"Orderbook fetch error: {ob_result}")
        ob_result = None
    if isinstance(liq_result, Exception):
        logger.warning(f"Liquidation fetch error: {liq_result}")
        liq_result = None

    # Record latest spot for momentum if fresh
    if spot_price:
        spot_tracker.record(spot_price)

    # ── Compute derived signals ─────────────────────────────────────
    momentum = spot_tracker.get_momentum()

    divergence = None
    if chainlink_price and spot_price:
        divergence = spot_price - chainlink_price

    # Candle position: how far current price is from the market's opening price
    # We use chainlink since that's what Polymarket resolves on
    # Opening price isn't in the Polymarket API, so we approximate:
    # If odds are ~0.50, price is near open. Use chainlink as current reference.
    # The "price to beat" is approximated from the candle's start chainlink reading.
    # For now, use divergence from the midpoint implied by odds as a proxy.
    candle_position = None
    if chainlink_price and odds:
        # Positive candle_position means price is above open (favoring Up)
        # odds.up_price > 0.5 means market thinks Up is more likely
        # We scale by a factor to approximate dollar distance
        candle_position = (odds.up_price - 0.5) * 200  # rough approximation

    round_number = compute_round_number(chainlink_price) if chainlink_price else None
    time_regime = get_time_regime()
    outcomes = db.get_last_n_outcomes(conn)
    streak = compute_streak(outcomes)

    # ── Sub-model votes ─────────────────────────────────────────────
    dashboard.status_message = "SIGNAL WINDOW — computing votes..."

    v_momentum = momentum_model.vote(
        momentum=momentum,
        cvd=cvd_result,
        chainlink_price=chainlink_price,
        spot_price=spot_price,
    )

    v_reversion = reversion_model.vote(
        candle_position_dollars=candle_position,
        orderbook=ob_result,
        streak=streak,
    )

    v_structure = structure_model.vote(
        round_number=round_number,
        liquidations=liq_result,
        time_regime=time_regime,
        candle_position_dollars=candle_position,
    )

    # ── Ensemble decision ───────────────────────────────────────────
    decision = decide(v_momentum, v_reversion, v_structure)

    # ── Build signal data dict for DB + dashboard ───────────────────
    signal_data = {
        "chainlink_price": chainlink_price,
        "spot_price": spot_price,
        "chainlink_spot_divergence": divergence,
        "candle_position_dollars": candle_position,
        "momentum_60s": momentum.momentum_60s if momentum else None,
        "momentum_120s": momentum.momentum_120s if momentum else None,
        "cvd": cvd_result.cvd if cvd_result else None,
        "order_book_ratio": ob_result.ratio if ob_result else None,
        "liquidation_signal": liq_result.net_pressure if liq_result else None,
        "round_number_distance": round_number.distance if round_number else None,
        "time_regime": time_regime,
        "candle_streak": (
            f"{streak.streak_length}x {streak.streak_direction}"
            if streak.streak_direction
            else "none"
        ),
        "momentum_vote": v_momentum,
        "reversion_vote": v_reversion,
        "structure_vote": v_structure,
        "final_vote": decision.side or "ABSTAIN",
    }

    # Update dashboard
    dashboard.last_signals = signal_data
    dashboard.last_decision = decision

    # ── Enter trade ─────────────────────────────────────────────────
    trade_id = simulator.enter_trade(market, odds, decision, signal_data)

    if trade_id is not None:
        pending_trades[market.slug] = trade_id
        dashboard.status_message = (
            f"TRADE PLACED: {decision.side} {decision.confidence.upper()} — "
            f"waiting for resolution..."
        )
    else:
        dashboard.status_message = f"SKIPPED: {decision.reason}"


async def on_market_close(market: MarketInfo, session: aiohttp.ClientSession):
    """Called after market closes — launch background resolution."""
    trade_id = pending_trades.pop(market.slug, None)
    if trade_id is None:
        dashboard.status_message = "Market closed — no pending trade"
        return

    # Resolve in background so the engine can move on to the next market
    asyncio.create_task(
        _resolve_in_background(market, trade_id)
    )
    dashboard.status_message = (
        f"Market closed — resolving {market.slug} in background..."
    )


async def _resolve_in_background(market: MarketInfo, trade_id: int):
    """Background task to wait for Polymarket resolution and settle the trade."""
    logger.info(f"Background resolution started for {market.slug} (trade {trade_id})")
    async with aiohttp.ClientSession() as session:
        winning_side = await resolve_market(
            market.condition_id, market.slug, session=session
        )

    if winning_side:
        simulator.settle_trade(trade_id, winning_side)
        dashboard.status_message = (
            f"RESOLVED: {winning_side} won — "
            f"balance=${portfolio.balance:,.2f} ({portfolio.pnl_pct:+.2f}%)"
        )
    else:
        logger.error(f"Could not resolve market {market.slug} after all retries")
        dashboard.status_message = f"ERROR: Resolution failed for {market.slug}"


# ── Main ────────────────────────────────────────────────────────────────

async def run():
    """Start all tasks: timing engine, spot poller, web server, and dashboard."""
    # Wire callbacks
    engine.on_market_discovered = on_market_discovered
    engine.on_signal_window = on_signal_window
    engine.on_market_close = on_market_close
    engine.on_skip = on_skip

    # Start engine and spot poller as concurrent tasks
    engine_task = asyncio.create_task(engine.run())
    poller_task = asyncio.create_task(poll_spot_price())

    # Start web dashboard server
    web_runner = await start_web_server(engine, portfolio, conn, dashboard)

    # Give the engine a moment to start before entering the dashboard loop
    await asyncio.sleep(0.1)

    # Run terminal dashboard — use screen mode only if we have a real terminal
    use_screen = dashboard._console.is_terminal
    try:
        with Live(
            dashboard.get_renderable(),
            console=dashboard._console,
            refresh_per_second=1 / config.DASHBOARD_REFRESH_INTERVAL,
            screen=use_screen,
        ) as live:
            while engine.running:
                live.update(dashboard.get_renderable())
                await asyncio.sleep(config.DASHBOARD_REFRESH_INTERVAL)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await web_runner.cleanup()
        await engine.stop()
        engine_task.cancel()
        poller_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass
        try:
            await poller_task
        except asyncio.CancelledError:
            pass
        conn.close()


def main():
    """Entry point with graceful shutdown."""
    logger.info("Polymarket Paper Trading Bot starting...")
    logger.info(f"Starting balance: ${config.STARTING_BALANCE:,.2f}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Handle Ctrl+C gracefully
    def shutdown(sig, frame):
        logger.info("Shutdown signal received")
        engine.running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        loop.run_until_complete(run())
    finally:
        loop.close()
        logger.info("Bot stopped. Final balance: ${:,.2f}".format(portfolio.balance))


if __name__ == "__main__":
    main()
