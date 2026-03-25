"""Entry point — wires all components together and runs the bot."""

import asyncio
import concurrent.futures
import logging
import signal
import sys
import time as _time_module

import aiohttp

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
from signals.polymarket_book import fetch_polymarket_book
from signals.fair_value import compute_fair_value
from timing_engine import TimingEngine
from notifications import notify_win, notify_loss, notify_trade_placed

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

    # Initialize executor and fetch real wallet balance
    try:
        executor = Executor()
        real_balance = executor.get_balance()
        if not real_balance or real_balance <= 0:
            logger.error("Could not fetch wallet balance — cannot start live mode")
            sys.exit(1)
        logger.info(f"Live wallet balance: ${real_balance:,.2f}")
    except Exception as e:
        logger.error(f"Failed to initialize CLOB executor: {e}")
        logger.error("Fix credentials in .env or switch to TRADING_MODE=paper")
        sys.exit(1)

    # Auto-detect starting balance on first launch, save to DB for P&L tracking
    saved_baseline = db.get_setting(conn, "live_starting_balance")
    if saved_baseline:
        starting = float(saved_baseline)
        logger.info(f"Live starting balance (from DB): ${starting:,.2f}")
    else:
        starting = real_balance
        db.set_setting(conn, "live_starting_balance", str(starting))
        logger.info(f"Live starting balance auto-detected: ${starting:,.2f} (saved to DB)")

    portfolio = Portfolio(conn, starting_balance=starting, skip_restore=True)
    portfolio._balance = real_balance

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
# Track the last resolved market outcome for ML features
last_market_outcome: str | None = None


# ── Session manager ───────────────────────────────────────────────────────
class SessionManager:
    """Manages aiohttp session lifecycle with health-based recreation.

    - Recreates after SESSION_FAILURE_THRESHOLD consecutive failures
    - Logs every recreation for diagnostics
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._consecutive_failures: int = 0
        self._creation_count: int = 0

    def _create_session(self) -> aiohttp.ClientSession:
        """Create a fresh aiohttp session."""
        connector = aiohttp.TCPConnector(
            limit=20,
            limit_per_host=5,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
        )
        self._consecutive_failures = 0
        self._creation_count += 1
        logger.info(f"HTTP session created (#{self._creation_count})")
        return session

    @property
    def session(self) -> aiohttp.ClientSession:
        """Return the current session, recreating if closed."""
        if self._session is None or self._session.closed:
            self._session = self._create_session()
        return self._session

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= config.SESSION_FAILURE_THRESHOLD:
            logger.warning(
                f"Session unhealthy ({self._consecutive_failures} consecutive failures) — recreating"
            )
            old = self._session
            self._session = self._create_session()
            if old and not old.closed:
                asyncio.get_event_loop().create_task(self._close_old(old))

    async def _close_old(self, old_session: aiohttp.ClientSession) -> None:
        try:
            await old_session.close()
        except Exception:
            pass

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


session_mgr = SessionManager()


# ── Spot price polling task ─────────────────────────────────────────────
async def poll_spot_price():
    """Adaptively sample spot price: 5s active, 60s tracking, 180s between markets."""
    while engine.running:
        try:
            price = await fetch_spot_price(session=session_mgr.session)
            if price:
                spot_tracker.record(price)
                session_mgr.record_success()
            else:
                session_mgr.record_failure()
        except Exception as e:
            logger.warning(f"Spot price poll failed: {type(e).__name__}: {e}")
            session_mgr.record_failure()

        # Three-tier polling: active window → tracking → between markets
        secs = engine.seconds_until_close()
        if secs is not None and 0 < secs <= config.SPOT_ACTIVE_WINDOW:
            interval = config.SPOT_POLL_ACTIVE_INTERVAL       # 5s near close
        elif engine.current_market is None:
            interval = config.SPOT_POLL_BETWEEN_MARKETS        # 180s idle gap
        else:
            interval = config.SPOT_POLL_IDLE_INTERVAL          # 60s tracking
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
    session: aiohttp.ClientSession,
):
    """Called at T-30s — fetch all signals, vote, and enter trade."""
    try:
        dashboard.status_message = "SIGNAL WINDOW — fetching signals..."

        # ── Fetch all signals in parallel ─────────────────────────────
        sig_session = session_mgr.session

        chainlink_price, spot_price, cvd_result, ob_result, liq_result, poly_book = (
            await asyncio.gather(
                fetch_chainlink_price(session=sig_session),
                fetch_spot_price(session=sig_session),
                fetch_cvd(session=sig_session),
                fetch_orderbook(session=sig_session),
                fetch_liquidations(session=sig_session),
                fetch_polymarket_book(
                    market.clob_token_id_up, market.clob_token_id_down,
                    session=sig_session,
                ),
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
        if isinstance(poly_book, Exception):
            logger.warning(f"Polymarket book fetch error: {poly_book}")
            poly_book = None

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

        # ── Fair value model ──────────────────────────────────────────────
        fair = compute_fair_value(
            spot_price=spot_price or 0,
            open_price=spot_tracker.candle_open_price or 0,
            sigma=spot_tracker.get_volatility() or 0,
            seconds_remaining=secs_to_close,
            market_up_price=odds.up_price,
            market_down_price=odds.down_price,
        ) if spot_price and spot_tracker.candle_open_price else None

        if fair:
            logger.info(
                f"📊 FAIR VALUE: up={fair.fair_up:.3f} down={fair.fair_down:.3f} "
                f"z={fair.z_score:+.2f} | edge_up={fair.edge_up_bps:+.0f}bps edge_down={fair.edge_down_bps:+.0f}bps"
            )

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
            polymarket_book=poly_book,
            liquidations=liq_result,
            time_regime=time_regime,
            candle_position_dollars=candle_position,
        )

        # ── Ensemble decision ───────────────────────────────────────────
        decision = decide(v_momentum, v_reversion, v_structure)

        # ── Build signal data dict for DB + dashboard ───────────────────
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)
        secs_to_close = engine.seconds_until_close() or 0

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
            # ML features
            "up_odds": odds.up_price,
            "down_odds": odds.down_price,
            "seconds_before_close": secs_to_close,
            "cvd_buy_volume": cvd_result.buy_volume if cvd_result else None,
            "cvd_sell_volume": cvd_result.sell_volume if cvd_result else None,
            "cvd_trade_count": cvd_result.trade_count if cvd_result else None,
            "ob_bid_volume": ob_result.bid_volume if ob_result else None,
            "ob_ask_volume": ob_result.ask_volume if ob_result else None,
            "liq_long_usd": liq_result.long_liquidated_usd if liq_result else None,
            "liq_short_usd": liq_result.short_liquidated_usd if liq_result else None,
            "poly_book_up_bids": poly_book.up_bid_volume if poly_book else None,
            "poly_book_up_asks": poly_book.up_ask_volume if poly_book else None,
            "poly_book_down_bids": poly_book.down_bid_volume if poly_book else None,
            "poly_book_down_asks": poly_book.down_ask_volume if poly_book else None,
            "poly_book_bias": poly_book.bias if poly_book else None,
            "momentum_direction": momentum.direction if momentum else None,
            "hour_of_day": now_utc.hour,
            "day_of_week": now_utc.weekday(),
            # Price context
            "btc_open_price": spot_tracker.candle_open_price,
            "btc_high": spot_tracker.candle_high,
            "btc_low": spot_tracker.candle_low,
            "btc_entry_price": spot_price,
            "btc_volatility": spot_tracker.get_volatility(),
            "poly_spread": odds.spread if odds else None,
            "prev_candle_outcome": last_market_outcome,
            # Fair value model
            "fair_up": fair.fair_up if fair else None,
            "fair_down": fair.fair_down if fair else None,
            "fair_z_score": fair.z_score if fair else None,
            "edge_up_bps": fair.edge_up_bps if fair else None,
            "edge_down_bps": fair.edge_down_bps if fair else None,
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


async def on_market_close(market: MarketInfo, session: aiohttp.ClientSession):
    """Called after market closes — launch background resolution."""
    try:
        trade_id = pending_trades.pop(market.slug, None)

        # Always resolve in background to record market_outcome for skips too
        asyncio.create_task(
            _resolve_in_background(market, trade_id)
        )

        if trade_id:
            dashboard.status_message = (
                f"Market closed — resolving {market.slug} in background..."
            )
        else:
            dashboard.status_message = "Market closed — resolving outcome for records"
    except Exception as e:
        logger.error(f"on_market_close failed for {market.slug}: {e}", exc_info=True)
        dashboard.status_message = f"ERROR: Market close handler failed"


async def _resolve_in_background(market: MarketInfo, trade_id: int | None):
    """Background task to wait for Polymarket resolution and settle the trade.

    Also records market_outcome for all trades (including skips) in this market.
    trade_id may be None if the market was skipped entirely.
    """
    try:
        logger.info(f"Background resolution started for {market.slug} (trade {trade_id or 'skip-only'})")
        winning_side = await resolve_market(
            market.condition_id, market.slug,
            client_factory=lambda: session_mgr.session,
        )

        if winning_side:
            global last_market_outcome
            last_market_outcome = winning_side
            # Record market outcome for ALL trades in this market (including skips)
            updated = db.update_market_outcome(conn, market.slug, winning_side)
            logger.info(f"Market outcome recorded: {winning_side} for {market.slug} ({updated} trades updated)")

            if trade_id:
                simulator.settle_trade(trade_id, winning_side, market.condition_id)
                # Send Discord notification
                trade_data = db.get_trade_by_id(conn, trade_id)
                if trade_data:
                    pnl = trade_data["pnl"] or 0
                    bal = trade_data["portfolio_balance_after"] or 0
                    rr = trade_data.get("risk_reward_ratio") or 0
                    if trade_data["outcome"] == "win":
                        await notify_win(trade_id, pnl, bal, rr)
                    elif trade_data["outcome"] == "loss":
                        await notify_loss(trade_id, pnl, bal, rr)
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
    # Expand asyncio thread pool for DNS resolution — default is ~6 workers
    # on a 2-vCPU box, which gets exhausted by parallel signal fetches.
    loop = asyncio.get_event_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=20))
    logger.info("Asyncio thread pool expanded to 20 workers")

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
