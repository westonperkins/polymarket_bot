"""PostgreSQL database connection and query functions (via Supabase)."""

import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection():
    """Return a connection to the PostgreSQL database, creating tables if needed."""
    conn = psycopg2.connect(config.DATABASE_URL)
    conn.autocommit = False
    with open(SCHEMA_PATH) as f:
        with conn.cursor() as cur:
            cur.execute(f.read())
        conn.commit()
    return conn


def _cursor(conn):
    """Return a RealDictCursor for dict-like row access."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ── Trades ─────────────────────────────────────────────────────────────

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
) -> int:
    """Insert a trade record and return its id."""
    with _cursor(conn) as cur:
        cur.execute(
            """INSERT INTO trades
               (timestamp, market_id, side, entry_odds, position_size,
                payout_rate, confidence_level, outcome, pnl, portfolio_balance_after)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            ),
        )
        trade_id = cur.fetchone()["id"]
    conn.commit()
    return trade_id


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
    conn.commit()


def get_recent_trades(conn, limit: int = 10) -> list[dict]:
    """Return the most recent trades, newest first."""
    with _cursor(conn) as cur:
        cur.execute("SELECT * FROM trades ORDER BY id DESC LIMIT %s", (limit,))
        return cur.fetchall()


def get_pending_trades(conn) -> list[dict]:
    """Return all trades awaiting resolution."""
    with _cursor(conn) as cur:
        cur.execute("SELECT * FROM trades WHERE outcome = 'pending'")
        return cur.fetchall()


def get_trade_stats(conn) -> dict:
    """Return aggregate trade statistics."""
    with _cursor(conn) as cur:
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


# ── Signals ────────────────────────────────────────────────────────────

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
    conn.commit()
    return signal_id


def get_signals_for_trade(conn, trade_id: int) -> Optional[dict]:
    """Return the signal snapshot for a given trade."""
    with _cursor(conn) as cur:
        cur.execute("SELECT * FROM signals WHERE trade_id = %s", (trade_id,))
        return cur.fetchone()


# ── Portfolio ──────────────────────────────────────────────────────────

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
    conn.commit()
    return snapshot_id


def get_latest_portfolio(conn) -> Optional[dict]:
    """Return the most recent portfolio snapshot."""
    with _cursor(conn) as cur:
        cur.execute("SELECT * FROM portfolio ORDER BY id DESC LIMIT 1")
        return cur.fetchone()


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
