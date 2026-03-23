"""Entry point — wires all components together and runs the bot."""

import asyncio
import logging
import signal
import sys
import time as _time_module

import aiohttp
import httpx

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

logger.info("=" * 50)
logger.info(f"TRADING MODE: {config.TRADING_MODE.upper()}")
logger.info("=" * 50)

if config.TRADING_MODE == "live":
    from live_trading.executor import Executor, validate_live_credentials
    from live_trading.risk import RiskManager
    from live_trading.live_simulator import LiveSimulator

    # Refuse to start without valid credentials
    creds_ok, creds_err = validate_live_credentials()
    if not creds_ok:
        logger.error(f"Cannot start live mode: {creds_err}")
        logger.error("Set credentials in .env or switch to TRADING_MODE=paper")
        sys.exit(1)

    # Don't restore from paper snapshots — start fresh with live balance
    portfolio = Portfolio(conn, starting_balance=config.LIVE_STARTING_BALANCE, skip_restore=True)

    # Initialize executor and fetch real wallet balance
    try:
        executor = Executor()
        real_balance = executor.get_balance()
        if real_balance and real_balance > 0:
            portfolio._balance = real_balance
            logger.info(f"Live wallet balance: ${real_balance:,.2f}")
        else:
            logger.warning(f"Could not fetch wallet balance, using ${portfolio.balance:,.2f}")
    except Exception as e:
        logger.error(f"Failed to initialize CLOB executor: {e}")
        logger.error("Fix credentials in .env or switch to TRADING_MODE=paper")
        sys.exit(1)

    risk = RiskManager(conn, portfolio.balance)
    simulator = LiveSimulator(conn, portfolio, executor, risk)
    logger.info(f"*** LIVE TRADING MODE ACTIVE ***")
    logger.info(f"  Balance: ${portfolio.balance:,.2f}")
    logger.info(f"  Max position: ${portfolio.balance * config.LIVE_MAX_POSITION_SIZE_PCT / 100:,.2f} ({config.LIVE_MAX_POSITION_SIZE_PCT}%)")
    logger.info(f"  Max daily loss: ${portfolio.balance * config.LIVE_MAX_DAILY_LOSS_PCT / 100:,.2f} ({config.LIVE_MAX_DAILY_LOSS_PCT}%)")

elif config.TRADING_MODE == "paper":
    portfolio = Portfolio(conn)
    simulator = Simulator(conn, portfolio)
    logger.info(f"Paper trading mode — balance: ${portfolio.balance:,.2f}")

else:
    logger.error(f"Unknown TRADING_MODE: '{config.TRADING_MODE}' — must be 'paper' or 'live'")
    sys.exit(1)

engine = TimingEngine()
spot_tracker = SpotTracker()
dashboard = Dashboard(engine, portfolio, conn)

# Track pending trades for resolution: market_slug → trade_id
pending_trades: dict[str, int] = {}


# ── Session manager ───────────────────────────────────────────────────────
class SessionManager:
    """Manages httpx async client lifecycle with periodic rotation and health checks.

    - Recreates client every SESSION_MAX_AGE seconds (2 min)
    - Recreates immediately after SESSION_FAILURE_THRESHOLD consecutive failures
    - Logs every recreation for diagnostics
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._created_at: float = 0.0
        self._consecutive_failures: int = 0
        self._creation_count: int = 0

    def _create_client(self) -> httpx.AsyncClient:
        """Create a fresh httpx async client."""
        client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=5,
            ),
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        self._created_at = _time_module.time()
        self._consecutive_failures = 0
        self._creation_count += 1
        logger.info(f"HTTP client created (#{self._creation_count})")
        return client

    @property
    def client(self) -> httpx.AsyncClient:
        """Return the current client, rotating if stale or closed."""
        now = _time_module.time()
        needs_rotation = (
            self._client is None
            or self._client.is_closed
            or (now - self._created_at) >= config.SESSION_MAX_AGE
        )
        if needs_rotation:
            old = self._client
            self._client = self._create_client()
            if old and not old.is_closed:
                asyncio.get_event_loop().create_task(self._close_old(old))
        return self._client

    def record_success(self) -> None:
        """Reset failure counter on a successful request."""
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """Track a failed request; recreate client after threshold."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= config.SESSION_FAILURE_THRESHOLD:
            logger.warning(
                f"Client unhealthy ({self._consecutive_failures} consecutive failures) — recreating"
            )
            old = self._client
            self._client = self._create_client()
            if old and not old.is_closed:
                asyncio.get_event_loop().create_task(self._close_old(old))

    async def _close_old(self, old_client: httpx.AsyncClient) -> None:
        """Close a retired client in the background."""
        try:
            await old_client.aclose()
        except Exception:
            pass

    async def close(self) -> None:
        """Shut down the current client (called on bot exit)."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


session_mgr = SessionManager()


# ── Spot price polling task ─────────────────────────────────────────────
async def poll_spot_price():
    """Adaptively sample spot price: every 5s in the 2-min active window, every 60s otherwise."""
    while engine.running:
        try:
            price = await fetch_spot_price(client=session_mgr.client)
            if price:
                spot_tracker.record(price)
                session_mgr.record_success()
            else:
                session_mgr.record_failure()
        except Exception as e:
            logger.warning(f"Spot price poll failed: {type(e).__name__}: {e}")
            session_mgr.record_failure()

        # Use active (5s) polling when within 2 minutes of market close
        secs = engine.seconds_until_close()
        if secs is not None and 0 < secs <= config.SPOT_ACTIVE_WINDOW:
            interval = config.SPOT_POLL_ACTIVE_INTERVAL
        else:
            interval = config.SPOT_POLL_IDLE_INTERVAL
        await asyncio.sleep(interval)


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
    try:
        skip_decision = decide("ABSTAIN", "ABSTAIN", "ABSTAIN")
        simulator.enter_trade(
            market,
            engine.current_odds or MarketOdds(0.5, 0.5, 0.0, False),
            skip_decision,
            {"time_regime": get_time_regime()},
        )
        dashboard.status_message = f"SKIPPED: {reason}"
        logger.info(f"Skip recorded: {market.slug} — {reason}")
    except Exception as e:
        logger.error(f"Failed to record skip for {market.slug}: {e}")
        dashboard.status_message = f"SKIPPED: {reason}"


async def on_signal_window(
    market: MarketInfo,
    odds: MarketOdds,
    client: httpx.AsyncClient,
):
    """Called at T-30s — fetch all signals, vote, and enter trade."""
    try:
        dashboard.status_message = "SIGNAL WINDOW — fetching signals..."

        # ── Fetch all signals in parallel (use managed httpx client) ──
        sig_client = session_mgr.client
        chainlink_task = fetch_chainlink_price(client=sig_client)
        spot_task = fetch_spot_price(client=sig_client)
        cvd_task = fetch_cvd(client=sig_client)
        orderbook_task = fetch_orderbook(client=sig_client)
        liq_task = fetch_liquidations(client=sig_client)

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

        # Validate momentum — if both windows are zero, the tracker has no real data
        if momentum and momentum.momentum_60s == 0.0 and momentum.momentum_120s == 0.0:
            logger.warning("Momentum data is all zeros — treating as missing")
            momentum = None

        divergence = None
        if chainlink_price and spot_price:
            divergence = spot_price - chainlink_price

        candle_position = None
        if chainlink_price and odds:
            candle_position = (odds.up_price - 0.5) * 200

        # Validate CVD — if cvd is 0.0 with 0 trades, the fetch failed
        if cvd_result and cvd_result.cvd == 0.0 and cvd_result.trade_count == 0:
            logger.warning("CVD data is zero with no trades — treating as missing")
            cvd_result = None

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

    except Exception as e:
        logger.error(f"Signal window failed for {market.slug}: {e}", exc_info=True)
        dashboard.status_message = f"ERROR: Signal window failed — {e}"


async def on_market_close(market: MarketInfo, client: httpx.AsyncClient):
    """Called after market closes — launch background resolution."""
    try:
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
    except Exception as e:
        logger.error(f"on_market_close failed for {market.slug}: {e}", exc_info=True)
        dashboard.status_message = f"ERROR: Market close handler failed"


async def _resolve_in_background(market: MarketInfo, trade_id: int):
    """Background task to wait for Polymarket resolution and settle the trade."""
    try:
        logger.info(f"Background resolution started for {market.slug} (trade {trade_id})")
        winning_side = await resolve_market(
            market.condition_id, market.slug, client=session_mgr.client
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
    except Exception as e:
        logger.error(f"Background resolution crashed for {market.slug} (trade {trade_id}): {e}", exc_info=True)
        dashboard.status_message = f"ERROR: Resolution error for {market.slug}"


# ── Main ────────────────────────────────────────────────────────────────

async def run():
    """Start all tasks: timing engine, spot poller, and web server."""
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

    logger.info(f"Web dashboard running at http://localhost:{config.WEB_PORT}")

    # Keep running until engine stops
    try:
        while engine.running:
            await asyncio.sleep(1)
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
        await session_mgr.close()
        conn.close()


def main():
    """Entry point with auto-restart. The bot should never stay dead."""
    logger.info("Polymarket Trading Bot starting...")

    # Handle Ctrl+C gracefully
    shutdown_requested = False

    def shutdown(sig, frame):
        nonlocal shutdown_requested
        shutdown_requested = True
        logger.info("Shutdown signal received")
        engine.running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while not shutdown_requested:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(run())
            break  # clean exit (Ctrl+C or engine stopped)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt — shutting down")
            break
        except Exception as e:
            logger.error(f"Bot crashed: {type(e).__name__}: {e}", exc_info=True)
            logger.info("Auto-restarting in 10 seconds...")
        finally:
            # Reset engine state for potential restart
            engine.running = False
            engine.current_market = None
            engine.current_odds = None
            try:
                loop.close()
            except Exception:
                pass

        if shutdown_requested:
            break

        # Wait before restart
        import time as _time
        _time.sleep(10)
        logger.info("Restarting bot...")

    logger.info("Bot stopped. Final balance: ${:,.2f}".format(portfolio.balance))


if __name__ == "__main__":
    main()
