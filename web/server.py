"""Lightweight web dashboard server using aiohttp.web.

Runs on the same asyncio event loop as the timing engine.
Serves a single-page dashboard at localhost and a JSON API endpoint.
"""

import logging
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

import config
from database import db
from paper_trading.portfolio import Portfolio
from timing_engine import TimingEngine
from dashboard.display import Dashboard

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def build_state_dict(
    engine: TimingEngine,
    portfolio: Portfolio,
    conn,
    dashboard: Dashboard,
) -> dict:
    """Assemble the full dashboard state as a JSON-serializable dict."""
    stats = db.get_trade_stats(conn)

    # Portfolio
    state = {
        "portfolio": {
            "balance": portfolio.balance,
            "starting_balance": config.STARTING_BALANCE,
            "pnl_pct": portfolio.pnl_pct,
            "daily_pnl": portfolio.daily_pnl,
            "total_trades": stats["total"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "skips": stats["skips"],
            "win_rate": stats["win_rate"],
        },
        "market": None,
        "signals": dashboard.last_signals,
        "decision": None,
        "trades": [],
        "status": dashboard.status_message,
    }

    # Current market
    market = engine.current_market
    odds = engine.current_odds
    if market:
        secs_close = engine.seconds_until_close()
        secs_signal = engine.seconds_until_signal_window()
        state["market"] = {
            "slug": market.slug,
            "title": market.title,
            "start_time": market.start_time.isoformat(),
            "end_time": market.end_time.isoformat(),
            "seconds_to_close": round(secs_close, 1) if secs_close else 0,
            "seconds_to_signal": round(secs_signal, 1) if secs_signal else 0,
            "up_odds": odds.up_price if odds else None,
            "down_odds": odds.down_price if odds else None,
            "tradeable": odds.tradeable if odds else None,
        }

    # Decision
    if dashboard.last_decision:
        d = dashboard.last_decision
        state["decision"] = {
            "side": d.side,
            "confidence": d.confidence,
            "momentum_vote": d.momentum_vote,
            "reversion_vote": d.reversion_vote,
            "structure_vote": d.structure_vote,
            "reason": d.reason,
        }

    # Recent trades with their signal snapshots
    trades = db.get_recent_trades(conn, limit=10)
    state["trades"] = []
    for t in trades:
        trade_dict = {
            "id": t["id"],
            "market_id": t["market_id"],
            "side": t["side"],
            "entry_odds": t["entry_odds"],
            "position_size": t["position_size"],
            "payout_rate": t["payout_rate"],
            "confidence_level": t["confidence_level"],
            "outcome": t["outcome"],
            "pnl": t["pnl"],
            "portfolio_balance_after": t["portfolio_balance_after"],
            "timestamp": t["timestamp"],
            "signals": None,
        }
        sig = db.get_signals_for_trade(conn, t["id"])
        if sig:
            trade_dict["signals"] = {
                "chainlink_price": sig["chainlink_price"],
                "spot_price": sig["spot_price"],
                "chainlink_spot_divergence": sig["chainlink_spot_divergence"],
                "candle_position_dollars": sig["candle_position_dollars"],
                "momentum_60s": sig["momentum_60s"],
                "momentum_120s": sig["momentum_120s"],
                "cvd": sig["cvd"],
                "order_book_ratio": sig["order_book_ratio"],
                "liquidation_signal": sig["liquidation_signal"],
                "round_number_distance": sig["round_number_distance"],
                "time_regime": sig["time_regime"],
                "candle_streak": sig["candle_streak"],
                "momentum_vote": sig["momentum_vote"],
                "reversion_vote": sig["reversion_vote"],
                "structure_vote": sig["structure_vote"],
                "final_vote": sig["final_vote"],
            }
        state["trades"].append(trade_dict)

    return state


async def handle_index(request):
    """Serve the dashboard HTML page."""
    return web.FileResponse(TEMPLATES_DIR / "index.html")


async def handle_api_state(request):
    """Return full dashboard state as JSON."""
    state = build_state_dict(
        request.app["engine"],
        request.app["portfolio"],
        request.app["conn"],
        request.app["dashboard"],
    )
    return web.json_response(state)


async def start_web_server(
    engine: TimingEngine,
    portfolio: Portfolio,
    conn,
    dashboard: Dashboard,
) -> web.AppRunner:
    """Create and start the web server. Returns the runner for cleanup."""
    app = web.Application()
    app["engine"] = engine
    app["portfolio"] = portfolio
    app["conn"] = conn
    app["dashboard"] = dashboard

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_api_state)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", config.WEB_PORT)
    await site.start()

    logger.info(f"Web dashboard running at http://localhost:{config.WEB_PORT}")
    return runner
