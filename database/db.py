"""PostgreSQL database connection and query functions (via Supabase).

Uses a ConnectionManager that auto-reconnects on dropped connections
and wraps all queries so a database hiccup never crashes the bot.
"""

import functools
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras

import config

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Errors that indicate a broken connection worth retrying
_RECOVERABLE = (
    psycopg2.OperationalError,
    psycopg2.InterfaceError,
)

_MAX_RETRIES = 3
_RETRY_DELAY = 1  # seconds


class ConnectionManager:
    """Wraps a psycopg2 connection with auto-reconnect and keepalives."""

    def __init__(self):
        self._conn = None

    def _connect(self):
        """Create a fresh connection with TCP keepalives."""
        logger.info("Connecting to database...")
        conn = psycopg2.connect(
            config.DATABASE_URL,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
            connect_timeout=10,
        )
        conn.autocommit = False
        # Create tables / run migrations — use a longer statement timeout
        with open(SCHEMA_PATH) as f:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = '60s'")
                cur.execute(f.read())
                cur.execute("SET statement_timeout = '15s'")
            conn.commit()
        self._conn = conn
        logger.info("Database connected")

    def get_conn(self):
        """Return a healthy connection, reconnecting if needed."""
        if self._conn is None or self._conn.closed:
            self._connect()
            return self._conn
        # Quick health check
        try:
            self._conn.cursor().execute("SELECT 1")
        except _RECOVERABLE:
            logger.warning("Database connection lost, reconnecting...")
            try:
                self._conn.close()
            except Exception:
                pass
            self._connect()
        return self._conn

    def cursor(self, cursor_factory=None):
        """Return a cursor from a healthy connection (used by simulator)."""
        conn = self.get_conn()
        if cursor_factory:
            return conn.cursor(cursor_factory=cursor_factory)
        return conn.cursor()

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()


def get_connection() -> ConnectionManager:
    """Return a ConnectionManager (replaces the old raw connection)."""
    mgr = ConnectionManager()
    mgr.get_conn()  # connect immediately
    return mgr


def _cursor(mgr: ConnectionManager):
    """Return a RealDictCursor from a healthy connection."""
    return mgr.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _retry(func):
    """Decorator that retries a DB function on recoverable connection errors."""
    @functools.wraps(func)
    def wrapper(conn, *args, **kwargs):
        last_err = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return func(conn, *args, **kwargs)
            except _RECOVERABLE as e:
                last_err = e
                logger.warning(
                    f"DB error in {func.__name__} (attempt {attempt}/{_MAX_RETRIES}): {e}"
                )
                # Force reconnect on next call
                if hasattr(conn, '_conn') and conn._conn and not conn._conn.closed:
                    try:
                        conn._conn.close()
                    except Exception:
                        pass
                    conn._conn = None
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY)
            except Exception as e:
                logger.error(f"Unexpected DB error in {func.__name__}: {e}")
                return _default_for(func)
        logger.error(f"DB call {func.__name__} failed after {_MAX_RETRIES} retries: {last_err}")
        return _default_for(func)
    return wrapper


def _default_for(func):
    """Return a safe default based on the function's return type hints."""
    name = func.__name__
    if name.startswith("insert_") or name == "get_last_n_outcomes":
        return None if name.startswith("insert_") else []
    if name == "get_trade_stats":
        return {"total": 0, "wins": 0, "losses": 0, "skips": 0, "win_rate": 0.0}
    if name == "get_daily_pnl":
        return 0.0
    if name == "get_best_worst_trades":
        return {"best_pnl": 0.0, "worst_pnl": 0.0}
    if name == "get_peak_balance":
        return 0.0
    if name == "get_portfolio_for_mode":
        return {"balance": 0.0, "total_pnl": 0.0, "starting_balance": 0.0, "pnl_pct": 0.0}
    if name in ("get_recent_trades", "get_pending_trades", "get_last_n_outcomes"):
        return []
    if name in ("get_latest_portfolio", "get_signals_for_trade"):
        return None
    return None


# ── Trades ─────────────────────────────────────────────────────────────

@_retry
def insert_trade(
    conn,
    market_id: str,
    side: str,
    entry_odds: Optional[float],
    position_size: Optional[float],
    payout_rate: Optional[float],
    confidence_level: str,
    outcome: str = "pending",
    pnl: float = 0.0,
    portfolio_balance_after: Optional[float] = None,
    trading_mode: Optional[str] = None,
) -> int:
    """Insert a trade record and return its id."""
    mode = trading_mode or config.TRADING_MODE
    with _cursor(conn) as cur:
        cur.execute(
            """INSERT INTO trades
               (timestamp, market_id, side, entry_odds, position_size,
                payout_rate, confidence_level, outcome, pnl, portfolio_balance_after,
                trading_mode)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                datetime.now(timezone.utc).isoformat(),
                market_id,
                side,
                entry_odds,
                position_size,
                payout_rate,
                confidence_level,
                outcome,
                pnl,
                portfolio_balance_after,
                mode,
            ),
        )
        trade_id = cur.fetchone()["id"]
    conn.get_conn().commit()
    return trade_id


@_retry
def update_trade_outcome(
    conn,
    trade_id: int,
    outcome: str,
    pnl: float,
    portfolio_balance_after: float,
) -> None:
    """Update a trade with its resolution result."""
    with _cursor(conn) as cur:
        cur.execute(
            """UPDATE trades
               SET outcome = %s, pnl = %s, portfolio_balance_after = %s
               WHERE id = %s""",
            (outcome, pnl, portfolio_balance_after, trade_id),
        )
    conn.get_conn().commit()


@_retry
def get_recent_trades(conn, limit: int = 10, mode: Optional[str] = None) -> list[dict]:
    """Return the most recent trades with a mode-specific row number, newest first."""
    with _cursor(conn) as cur:
        if mode:
            cur.execute(
                """SELECT *, ROW_NUMBER() OVER (ORDER BY id) AS trade_num
                   FROM trades WHERE trading_mode = %s
                   ORDER BY id DESC LIMIT %s""",
                (mode, limit),
            )
        else:
            cur.execute(
                """SELECT *, ROW_NUMBER() OVER (ORDER BY id) AS trade_num
                   FROM trades ORDER BY id DESC LIMIT %s""",
                (limit,),
            )
        return cur.fetchall()


@_retry
def get_pending_trades(conn, mode: Optional[str] = None) -> list[dict]:
    """Return all trades awaiting resolution."""
    with _cursor(conn) as cur:
        if mode:
            cur.execute("SELECT * FROM trades WHERE outcome = 'pending' AND trading_mode = %s", (mode,))
        else:
            cur.execute("SELECT * FROM trades WHERE outcome = 'pending'")
        return cur.fetchall()


@_retry
def get_trade_stats(conn, mode: Optional[str] = None) -> dict:
    """Return aggregate trade statistics."""
    with _cursor(conn) as cur:
        if mode:
            cur.execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
                       SUM(CASE WHEN outcome = 'skip' THEN 1 ELSE 0 END) AS skips
                   FROM trades WHERE trading_mode = %s""",
                (mode,),
            )
        else:
            cur.execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
                       SUM(CASE WHEN outcome = 'skip' THEN 1 ELSE 0 END) AS skips
                   FROM trades"""
            )
        row = cur.fetchone()
    total = row["total"]
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    decided = wins + losses
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "skips": row["skips"] or 0,
        "win_rate": (wins / decided * 100) if decided > 0 else 0.0,
    }


@_retry
def get_daily_pnl(conn, mode: Optional[str] = None) -> float:
    """Return total P&L for trades settled today (Pacific time)."""
    from zoneinfo import ZoneInfo
    pacific = ZoneInfo("America/Los_Angeles")
    today_pacific = datetime.now(pacific).strftime("%Y-%m-%d")
    day_start_pacific = datetime.strptime(today_pacific, "%Y-%m-%d").replace(tzinfo=pacific)
    day_start_utc = day_start_pacific.astimezone(timezone.utc).isoformat()
    with _cursor(conn) as cur:
        if mode:
            cur.execute(
                """SELECT COALESCE(SUM(pnl), 0) AS daily_pnl
                   FROM trades
                   WHERE outcome IN ('win', 'loss')
                   AND timestamp >= %s AND trading_mode = %s""",
                (day_start_utc, mode),
            )
        else:
            cur.execute(
                """SELECT COALESCE(SUM(pnl), 0) AS daily_pnl
                   FROM trades
                   WHERE outcome IN ('win', 'loss')
                   AND timestamp >= %s""",
                (day_start_utc,),
            )
        row = cur.fetchone()
    return float(row["daily_pnl"])


@_retry
def get_calendar_pnl(conn, year: int, month: int, mode: Optional[str] = None) -> list[dict]:
    """Return daily P&L totals for a given month, grouped by Pacific date.

    Returns a list of dicts: [{"date": "2026-03-22", "pnl": 123.45, "trades": 5}, ...]
    """
    from zoneinfo import ZoneInfo
    import calendar

    pacific = ZoneInfo("America/Los_Angeles")
    # First and last day of the month in Pacific
    first_day = datetime(year, month, 1, tzinfo=pacific)
    last_day_num = calendar.monthrange(year, month)[1]
    # Start of next month
    if month == 12:
        next_month_start = datetime(year + 1, 1, 1, tzinfo=pacific)
    else:
        next_month_start = datetime(year, month + 1, 1, tzinfo=pacific)

    start_utc = first_day.astimezone(timezone.utc).isoformat()
    end_utc = next_month_start.astimezone(timezone.utc).isoformat()

    with _cursor(conn) as cur:
        if mode:
            cur.execute(
                """SELECT timestamp, pnl
                   FROM trades
                   WHERE outcome IN ('win', 'loss')
                   AND timestamp >= %s AND timestamp < %s
                   AND trading_mode = %s
                   ORDER BY timestamp""",
                (start_utc, end_utc, mode),
            )
        else:
            cur.execute(
                """SELECT timestamp, pnl
                   FROM trades
                   WHERE outcome IN ('win', 'loss')
                   AND timestamp >= %s AND timestamp < %s
                   ORDER BY timestamp""",
                (start_utc, end_utc),
            )
        rows = cur.fetchall()

    # Group by Pacific date
    daily: dict[str, dict] = {}
    for row in rows:
        ts_str = row["timestamp"]
        ts_utc = datetime.fromisoformat(ts_str)
        ts_pacific = ts_utc.astimezone(pacific)
        date_key = ts_pacific.strftime("%Y-%m-%d")
        if date_key not in daily:
            daily[date_key] = {"date": date_key, "pnl": 0.0, "trades": 0}
        daily[date_key]["pnl"] += row["pnl"]
        daily[date_key]["trades"] += 1

    # Round pnl values
    for d in daily.values():
        d["pnl"] = round(d["pnl"], 2)

    return list(daily.values())


@_retry
def get_monthly_pnl(conn, year: int, mode: Optional[str] = None) -> list[dict]:
    """Return monthly P&L totals for a given year.

    Returns a list of dicts: [{"month": 1, "pnl": 1234.56, "trades": 42}, ...]
    """
    from zoneinfo import ZoneInfo

    pacific = ZoneInfo("America/Los_Angeles")
    start_utc = datetime(year, 1, 1, tzinfo=pacific).astimezone(timezone.utc).isoformat()
    end_utc = datetime(year + 1, 1, 1, tzinfo=pacific).astimezone(timezone.utc).isoformat()

    with _cursor(conn) as cur:
        if mode:
            cur.execute(
                """SELECT timestamp, pnl
                   FROM trades
                   WHERE outcome IN ('win', 'loss')
                   AND timestamp >= %s AND timestamp < %s
                   AND trading_mode = %s
                   ORDER BY timestamp""",
                (start_utc, end_utc, mode),
            )
        else:
            cur.execute(
                """SELECT timestamp, pnl
                   FROM trades
                   WHERE outcome IN ('win', 'loss')
                   AND timestamp >= %s AND timestamp < %s
                   ORDER BY timestamp""",
                (start_utc, end_utc),
            )
        rows = cur.fetchall()

    monthly: dict[int, dict] = {}
    for row in rows:
        ts_utc = datetime.fromisoformat(row["timestamp"])
        ts_pacific = ts_utc.astimezone(pacific)
        m = ts_pacific.month
        if m not in monthly:
            monthly[m] = {"month": m, "pnl": 0.0, "trades": 0}
        monthly[m]["pnl"] += row["pnl"]
        monthly[m]["trades"] += 1

    for d in monthly.values():
        d["pnl"] = round(d["pnl"], 2)

    return list(monthly.values())


@_retry
def get_portfolio_for_mode(conn, mode: str) -> dict:
    """Compute portfolio state from trade history for a specific mode.

    Returns {"balance": float, "total_pnl": float, "starting_balance": float}.
    """
    starting = config.LIVE_STARTING_BALANCE if mode == "live" else config.STARTING_BALANCE
    with _cursor(conn) as cur:
        # Get total P&L from all settled trades in this mode
        cur.execute(
            """SELECT COALESCE(SUM(pnl), 0) AS total_pnl
               FROM trades
               WHERE outcome IN ('win', 'loss')
               AND trading_mode = %s""",
            (mode,),
        )
        row = cur.fetchone()
    total_pnl = float(row["total_pnl"])
    balance = starting + total_pnl
    return {
        "balance": round(balance, 2),
        "total_pnl": round(total_pnl, 2),
        "starting_balance": starting,
        "pnl_pct": round((total_pnl / starting * 100), 2) if starting > 0 else 0.0,
    }


@_retry
def get_best_worst_trades(conn, mode: Optional[str] = None) -> dict:
    """Return the best and worst single trades by P&L."""
    with _cursor(conn) as cur:
        if mode:
            cur.execute(
                """SELECT MAX(pnl) AS best_pnl, MIN(pnl) AS worst_pnl
                   FROM trades WHERE outcome IN ('win', 'loss') AND trading_mode = %s""",
                (mode,),
            )
        else:
            cur.execute(
                """SELECT MAX(pnl) AS best_pnl, MIN(pnl) AS worst_pnl
                   FROM trades WHERE outcome IN ('win', 'loss')"""
            )
        row = cur.fetchone()
    return {
        "best_pnl": float(row["best_pnl"]) if row["best_pnl"] is not None else 0.0,
        "worst_pnl": float(row["worst_pnl"]) if row["worst_pnl"] is not None else 0.0,
    }


@_retry
def get_peak_balance(conn, mode: Optional[str] = None) -> float:
    """Return the highest portfolio balance ever recorded."""
    with _cursor(conn) as cur:
        if mode:
            cur.execute(
                """SELECT COALESCE(MAX(portfolio_balance_after), 0) AS peak
                   FROM trades WHERE portfolio_balance_after IS NOT NULL AND trading_mode = %s""",
                (mode,),
            )
        else:
            cur.execute(
                """SELECT COALESCE(MAX(portfolio_balance_after), 0) AS peak
                   FROM trades WHERE portfolio_balance_after IS NOT NULL"""
            )
        row = cur.fetchone()
    return float(row["peak"]) if row["peak"] else 0.0


# ── Signals ────────────────────────────────────────────────────────────

@_retry
def insert_signals(
    conn,
    trade_id: int,
    chainlink_price: Optional[float] = None,
    spot_price: Optional[float] = None,
    chainlink_spot_divergence: Optional[float] = None,
    candle_position_dollars: Optional[float] = None,
    momentum_60s: Optional[float] = None,
    momentum_120s: Optional[float] = None,
    cvd: Optional[float] = None,
    order_book_ratio: Optional[float] = None,
    liquidation_signal: Optional[float] = None,
    round_number_distance: Optional[float] = None,
    time_regime: Optional[str] = None,
    candle_streak: Optional[str] = None,
    momentum_vote: Optional[str] = None,
    reversion_vote: Optional[str] = None,
    structure_vote: Optional[str] = None,
    final_vote: Optional[str] = None,
) -> int:
    """Insert a signal snapshot for a trade and return its id."""
    with _cursor(conn) as cur:
        cur.execute(
            """INSERT INTO signals
               (trade_id, chainlink_price, spot_price, chainlink_spot_divergence,
                candle_position_dollars, momentum_60s, momentum_120s, cvd,
                order_book_ratio, liquidation_signal, round_number_distance,
                time_regime, candle_streak, momentum_vote, reversion_vote,
                structure_vote, final_vote)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                trade_id,
                chainlink_price,
                spot_price,
                chainlink_spot_divergence,
                candle_position_dollars,
                momentum_60s,
                momentum_120s,
                cvd,
                order_book_ratio,
                liquidation_signal,
                round_number_distance,
                time_regime,
                candle_streak,
                momentum_vote,
                reversion_vote,
                structure_vote,
                final_vote,
            ),
        )
        signal_id = cur.fetchone()["id"]
    conn.get_conn().commit()
    return signal_id


@_retry
def get_signals_for_trade(conn, trade_id: int) -> Optional[dict]:
    """Return the signal snapshot for a given trade."""
    with _cursor(conn) as cur:
        cur.execute("SELECT * FROM signals WHERE trade_id = %s", (trade_id,))
        return cur.fetchone()


# ── Portfolio ──────────────────────────────────────────────────────────

@_retry
def insert_portfolio_snapshot(
    conn,
    balance: float,
    total_trades: int,
    wins: int,
    losses: int,
    skips: int,
    win_rate: float,
    daily_pnl: float,
) -> int:
    """Insert a portfolio snapshot and return its id."""
    with _cursor(conn) as cur:
        cur.execute(
            """INSERT INTO portfolio
               (timestamp, balance, total_trades, wins, losses, skips, win_rate, daily_pnl)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                datetime.now(timezone.utc).isoformat(),
                balance,
                total_trades,
                wins,
                losses,
                skips,
                win_rate,
                daily_pnl,
            ),
        )
        snapshot_id = cur.fetchone()["id"]
    conn.get_conn().commit()
    return snapshot_id


@_retry
def get_latest_portfolio(conn) -> Optional[dict]:
    """Return the most recent portfolio snapshot."""
    with _cursor(conn) as cur:
        cur.execute("SELECT * FROM portfolio ORDER BY id DESC LIMIT 1")
        return cur.fetchone()


@_retry
def get_last_n_outcomes(conn, n: int = 5) -> list[str]:
    """Return the last N trade outcomes (for candle streak tracking).
    Only includes resolved trades (win/loss), newest first."""
    with _cursor(conn) as cur:
        cur.execute(
            """SELECT side FROM trades
               WHERE outcome IN ('win', 'loss')
               ORDER BY id DESC LIMIT %s""",
            (n,),
        )
        rows = cur.fetchall()
    return [row["side"] for row in rows]
