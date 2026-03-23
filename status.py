"""Read-only status viewer — shows portfolio and trade history without trading.

Usage:
    python3 status.py          # print summary to terminal
    python3 status.py --web    # launch web dashboard on localhost:8080
"""

import asyncio
import sys

import config
from database import db
from paper_trading.portfolio import Portfolio


def print_status():
    """Print a quick portfolio summary to the terminal."""
    conn = db.get_connection()
    portfolio = Portfolio(conn)
    stats = db.get_trade_stats(conn)
    trades = db.get_recent_trades(conn, limit=10)

    print()
    print("  POLYMARKET PAPER TRADING BOT — STATUS")
    print("  " + "=" * 42)
    print(f"  Balance:      ${portfolio.balance:,.2f}")
    print(f"  Total P&L:    {portfolio.pnl_pct:+.2f}%")
    print(f"  Trades:       {stats['total']}")
    print(f"  Record:       {stats['wins']}W / {stats['losses']}L / {stats['skips']}S")
    print(f"  Win Rate:     {stats['win_rate']:.1f}%")
    print()

    if trades:
        print("  LAST 10 TRADES")
        print("  " + "-" * 42)
        for t in trades:
            slug = t["market_id"].replace("btc-updown-5m-", "")[-8:]
            outcome = t["outcome"].upper()
            pnl = t["pnl"] or 0
            if t["confidence_level"] == "skip":
                print(f"  #{t['id']:3d}  {slug}  SKIP")
            else:
                pnl_str = f"${pnl:+,.2f}"
                print(
                    f"  #{t['id']:3d}  {slug}  {t['side']:4s}  "
                    f"{t['confidence_level']:6s}  {outcome:7s}  {pnl_str}"
                )
    print()
    conn.close()


async def run_web():
    """Launch the web dashboard in read-only mode (no trading engine)."""
    from aiohttp import web as aiohttp_web
    from timing_engine import TimingEngine
    from dashboard.display import Dashboard
    from web.server import start_web_server

    conn = db.get_connection()
    portfolio = Portfolio(conn)
    engine = TimingEngine()  # not started — just provides empty state
    dashboard = Dashboard(engine, portfolio, conn)
    dashboard.status_message = "Read-only mode — viewing only, not trading"

    runner = await start_web_server(engine, portfolio, conn, dashboard)
    print(f"\n  Dashboard running at http://localhost:{config.WEB_PORT}")
    print("  Press Ctrl+C to stop\n")

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()
        conn.close()


if __name__ == "__main__":
    # Suppress noisy aiohttp keepalive errors on macOS + Python 3.13
    import logging as _logging
    _logging.getLogger("asyncio").setLevel(_logging.CRITICAL)

    if "--web" in sys.argv:
        asyncio.run(run_web())
    else:
        print_status()
