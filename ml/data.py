"""Pull and clean trade + signal data from Supabase for ML training."""

import os
import sys
import logging
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras

# Add parent dir so we can import config
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]


def pull_raw_data(mode: str = "live") -> pd.DataFrame:
    """Pull all trades joined with signals from Supabase."""
    conn = psycopg2.connect(DATABASE_URL)
    query = """
        SELECT
            t.id AS trade_id,
            t.timestamp,
            t.market_id,
            t.side,
            t.entry_odds,
            t.position_size,
            t.payout_rate,
            t.confidence_level,
            t.outcome,
            t.pnl,
            t.portfolio_balance_after,
            t.trading_mode,
            t.skip_reason,
            t.risk_reward_ratio,
            t.market_outcome,
            s.*
        FROM trades t
        LEFT JOIN signals s ON s.trade_id = t.id
        WHERE t.trading_mode = %s
        ORDER BY t.id
    """
    df = pd.read_sql(query, conn, params=(mode,))
    conn.close()
    logger.info(f"Pulled {len(df)} rows for mode={mode}")
    return df


def clean_data(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split into decided (win/loss), skipped, and pending DataFrames.

    Returns (decided, skipped, pending).
    """
    # Parse streak text into numeric
    if "candle_streak" in df.columns:
        df["streak_length"] = df["candle_streak"].apply(_parse_streak_length)
        df["streak_is_up"] = df["candle_streak"].apply(
            lambda x: 1 if isinstance(x, str) and "Up" in x else 0
        )

    # Encode previous candle outcome
    if "prev_candle_outcome" in df.columns:
        df["prev_candle_up"] = (df["prev_candle_outcome"] == "Up").astype(int)

    # Encode votes as numeric
    vote_map = {"Up": 1, "Down": -1, "ABSTAIN": 0}
    for col in ["momentum_vote", "reversion_vote", "structure_vote", "final_vote"]:
        if col in df.columns:
            df[f"{col}_num"] = df[col].map(vote_map).fillna(0).astype(int)

    # Encode side
    if "side" in df.columns:
        df["side_num"] = (df["side"] == "Up").astype(int)

    # Encode confidence
    conf_map = {"high": 2, "medium": 1, "skip": 0}
    if "confidence_level" in df.columns:
        df["confidence_num"] = df["confidence_level"].map(conf_map).fillna(0).astype(int)

    # Split
    decided = df[df["outcome"].isin(["win", "loss"])].copy()
    skipped = df[df["outcome"] == "skip"].copy()
    pending = df[df["outcome"] == "pending"].copy()

    # Target variable for decided
    decided["y"] = (decided["outcome"] == "win").astype(int)

    logger.info(
        f"Data split: {len(decided)} decided, {len(skipped)} skipped, {len(pending)} pending"
    )
    return decided, skipped, pending


def get_data_summary(decided: pd.DataFrame, skipped: pd.DataFrame) -> dict:
    """Summary stats for the report."""
    summary = {
        "total_decided": len(decided),
        "total_skipped": len(skipped),
        "wins": int((decided["outcome"] == "win").sum()) if len(decided) > 0 else 0,
        "losses": int((decided["outcome"] == "loss").sum()) if len(decided) > 0 else 0,
        "win_rate": float((decided["outcome"] == "win").mean()) if len(decided) > 0 else 0,
        "date_range": (
            str(decided["timestamp"].min()) + " to " + str(decided["timestamp"].max())
        ) if len(decided) > 0 else "N/A",
        "null_pct": {},
    }

    # Null percentages for key signal columns
    signal_cols = [
        c for c in decided.columns
        if c not in ["trade_id", "id", "timestamp", "market_id", "outcome", "y",
                      "side", "trading_mode", "skip_reason", "market_outcome"]
    ]
    for col in signal_cols:
        null_pct = decided[col].isna().mean() * 100
        if null_pct > 0:
            summary["null_pct"][col] = round(null_pct, 1)

    return summary


def _parse_streak_length(val) -> int:
    """Parse '3x Up' or 'none' to integer."""
    if not isinstance(val, str) or val == "none":
        return 0
    try:
        return int(val.split("x")[0])
    except (ValueError, IndexError):
        return 0
