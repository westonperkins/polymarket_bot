"""Forensic analysis of taker-mode trades.

Pulls every trade tagged confidence_level='taker' plus recent live trades in
the taker window and joins them with signal data so we can see:

  - What the ML gate said (model P(win))
  - What each sub-model voted (momentum / reversion / structure)
  - What the fair-value model thought
  - What the actual fill was vs. quoted odds
  - What the market resolved to
  - PnL per trade

Optional: pass a Polymarket CSV export to cross-check against the DB records
and flag trades that executed on Polymarket but don't have a DB row (which
indicates a failed insert, typically from a CHECK-constraint violation).

Usage:
    python -m ml.analyze_taker
    python -m ml.analyze_taker --csv /path/to/Polymarket-History-*.csv
"""

import argparse
import csv
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]


def fetch_taker_trades(conn, since: str | None = None) -> list[dict]:
    """Pull all taker trades + their signals."""
    where = ["t.confidence_level = 'taker'"]
    params: list = []
    if since:
        where.append("t.timestamp >= %s")
        params.append(since)

    sql = f"""
        SELECT
            t.id AS trade_id,
            t.timestamp,
            t.market_id,
            t.side,
            t.entry_odds,
            t.position_size,
            t.payout_rate,
            t.outcome,
            t.pnl,
            t.market_outcome,
            t.risk_reward_ratio,
            s.up_odds,
            s.down_odds,
            s.final_vote,
            s.momentum_vote,
            s.reversion_vote,
            s.structure_vote,
            s.ml_win_prob,
            s.fill_price_per_share,
            s.fill_slippage_pct,
            s.fair_up,
            s.fair_down,
            s.edge_up_bps,
            s.edge_down_bps,
            s.seconds_before_close
        FROM trades t
        LEFT JOIN signals s ON s.trade_id = t.id
        WHERE {" AND ".join(where)}
        ORDER BY t.timestamp
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def load_polymarket_csv(path: Path) -> list[dict]:
    """Load buys from a Polymarket history CSV."""
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("action") == "Buy"]


def format_row(r: dict) -> str:
    ts = r.get("timestamp") or ""
    if len(ts) > 19:
        ts = ts[:19]
    side = r.get("side") or "?"
    entry = r.get("entry_odds") or 0
    fill = r.get("fill_price_per_share") or 0
    ml = r.get("ml_win_prob")
    ml_str = f"{ml:.1%}" if ml is not None else "    —"
    votes = (
        f"{r.get('momentum_vote', '?')[0]}/"
        f"{r.get('reversion_vote', '?')[0]}/"
        f"{r.get('structure_vote', '?')[0]}"
    )
    pnl = r.get("pnl") or 0
    outcome = r.get("outcome") or "?"
    mkt_out = r.get("market_outcome") or "?"
    slip = r.get("fill_slippage_pct")
    slip_str = f"{slip:+.1f}%" if slip is not None else "   —"
    verdict = "✅" if outcome == "win" else "❌" if outcome == "loss" else "⏳"
    return (
        f"{ts:<20} {side:<5} ml={ml_str:<6} votes={votes:<8} "
        f"entry=${entry:.3f} fill=${fill:.3f} slip={slip_str:<7} "
        f"pnl=${pnl:+.2f} {outcome:<5} mkt={mkt_out:<5} {verdict}"
    )


def main():
    parser = argparse.ArgumentParser(description="Forensic analysis of taker trades")
    parser.add_argument("--csv", help="Optional Polymarket history CSV for cross-check")
    parser.add_argument("--since", help="Only show trades from this ISO timestamp")
    args = parser.parse_args()

    conn = psycopg2.connect(DATABASE_URL)

    print("=" * 110)
    print("TAKER TRADE FORENSIC ANALYSIS")
    print("=" * 110)

    trades = fetch_taker_trades(conn, since=args.since)
    print(f"\nFound {len(trades)} taker-tagged trades in the database.\n")

    if trades:
        print(f"{'Timestamp':<20} {'Side':<5} {'MLProb':<9} {'Votes':<13} "
              f"{'Entry':<9} {'Fill':<9} {'Slip':<9} {'PnL':<9} {'Outcome':<6} {'Market':<6}")
        print("-" * 110)
        for r in trades:
            print(format_row(r))

        # Summary stats
        resolved = [r for r in trades if r["outcome"] in ("win", "loss")]
        wins = sum(1 for r in resolved if r["outcome"] == "win")
        total_pnl = sum((r["pnl"] or 0) for r in resolved)
        print()
        print(f"Resolved: {len(resolved)} | Wins: {wins} ({wins/len(resolved):.1%} if resolved else 0)")
        print(f"Total PnL: ${total_pnl:+.2f}")

        # Calibration check: did the model's P(win) match actual outcomes?
        with_prob = [r for r in resolved if r["ml_win_prob"] is not None]
        if with_prob:
            avg_pred = sum(r["ml_win_prob"] for r in with_prob) / len(with_prob)
            actual_wr = sum(1 for r in with_prob if r["outcome"] == "win") / len(with_prob)
            print(f"\nCalibration check ({len(with_prob)} trades with ML prob):")
            print(f"  Avg predicted P(win): {avg_pred:.1%}")
            print(f"  Actual win rate:      {actual_wr:.1%}")
            print(f"  Miscalibration:       {avg_pred - actual_wr:+.1%}")
            if avg_pred - actual_wr > 0.15:
                print("  → Model is OVERCONFIDENT — predicting wins that don't happen")

    # Cross-check against Polymarket CSV
    if args.csv:
        csv_buys = load_polymarket_csv(Path(args.csv))
        # Recent buys that look like taker trades (~$1-2 spent)
        recent_buys = [
            b for b in csv_buys
            if float(b.get("usdcAmount", 0)) < 5.0
            and args.since is None or _csv_ts(b) >= args.since
        ]
        print()
        print("=" * 110)
        print("POLYMARKET CSV CROSS-CHECK")
        print("=" * 110)
        print(f"Polymarket buys (small, taker-sized): {len(recent_buys)}")
        print(f"DB taker records:                     {len(trades)}")
        missing = len(recent_buys) - len(trades)
        if missing > 0:
            print(f"\n⚠ {missing} Polymarket fills have NO corresponding DB record.")
            print("  This is the signature of the CHECK-constraint violation that was")
            print("  rejecting every taker INSERT before the schema migration landed.")
            print("  These trades cannot be forensically analyzed — we have the fill")
            print("  price from Polymarket but no signal context from our side.")

    conn.close()


def _csv_ts(row: dict) -> str:
    try:
        return datetime.fromtimestamp(int(row["timestamp"]), timezone.utc).isoformat()
    except (ValueError, KeyError):
        return ""


if __name__ == "__main__":
    main()
