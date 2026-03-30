"""Professional trading terminal — connects directly to Supabase, no VPS needed.

Run: python3 local_dashboard.py
View: http://localhost:3000
"""

import asyncio
import json
import os
from functools import partial
from pathlib import Path

import psycopg2
import psycopg2.extras
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
PORT = 3000

# ── Wallet balance via CLOB ───────────────────────────────────────
_wallet_balance = None
_wallet_balance_ts = 0

def get_wallet_balance() -> float:
    """Fetch real wallet balance from Polymarket CLOB. Cached for 30s."""
    global _wallet_balance, _wallet_balance_ts
    import time
    now = time.time()
    if _wallet_balance is not None and now - _wallet_balance_ts < 30:
        return _wallet_balance
    try:
        from py_clob_client.client import ClobClient
        pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
        sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
        if not pk or not funder:
            return _wallet_balance or 0.0
        client = ClobClient(
            "https://clob.polymarket.com",
            key=pk, chain_id=137,
            signature_type=sig_type, funder=funder,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
        )
        balance_raw = result.get("balance", "0") if isinstance(result, dict) else getattr(result, "balance", "0")
        _wallet_balance = int(balance_raw) / 1e6
        _wallet_balance_ts = now
        from datetime import datetime
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Wallet balance: ${_wallet_balance:.2f}")
        return _wallet_balance
    except Exception as e:
        print(f"Wallet balance fetch failed: {e}")
        return _wallet_balance or 0.0


def get_conn():
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=15)
    conn.autocommit = True
    return conn


def query_state(conn):
    """Build full terminal state from Supabase."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── Portfolio ────────────────────────────────────────────────────
    cur.execute("SELECT value FROM settings WHERE key = 'live_starting_balance'")
    row = cur.fetchone()
    live_starting = float(row["value"]) if row else 9.11

    # Use real wallet balance, fall back to DB calculation
    wallet_bal = get_wallet_balance()
    if wallet_bal and wallet_bal > 0:
        live_balance = wallet_bal
        live_pnl = live_balance - live_starting
    else:
        cur.execute("""
            SELECT COALESCE(SUM(pnl), 0) AS total_pnl
            FROM trades WHERE outcome IN ('win', 'loss') AND trading_mode = 'live'
        """)
        live_pnl = float(cur.fetchone()["total_pnl"])
        live_balance = live_starting + live_pnl

    # ── Trade Stats ──────────────────────────────────────────────────
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE outcome IN ('win','loss')) AS total,
            COUNT(*) FILTER (WHERE outcome = 'win') AS wins,
            COUNT(*) FILTER (WHERE outcome = 'loss') AS losses,
            COUNT(*) FILTER (WHERE outcome = 'skip') AS skips,
            COALESCE(AVG(pnl) FILTER (WHERE outcome = 'win'), 0) AS avg_win,
            COALESCE(AVG(pnl) FILTER (WHERE outcome = 'loss'), 0) AS avg_loss,
            COALESCE(SUM(pnl) FILTER (WHERE outcome = 'win'), 0) AS sum_wins,
            COALESCE(ABS(SUM(pnl) FILTER (WHERE outcome = 'loss')), 0) AS sum_losses,
            MAX(pnl) AS best_trade,
            MIN(pnl) AS worst_trade
        FROM trades WHERE trading_mode = 'live'
    """)
    s = cur.fetchone()
    total = s["total"] or 0
    wins = s["wins"] or 0
    losses = s["losses"] or 0
    sum_wins = float(s["sum_wins"] or 0)
    sum_losses = float(s["sum_losses"] or 0)
    profit_factor = round(sum_wins / sum_losses, 2) if sum_losses > 0 else 0

    # ── Skip Breakdown ───────────────────────────────────────────────
    cur.execute("""
        SELECT
            COALESCE(t.skip_reason, CASE WHEN s.final_vote = 'ABSTAIN' THEN 'no_consensus' ELSE 'order_rejected' END) AS reason,
            COUNT(*) AS cnt
        FROM trades t
        LEFT JOIN signals s ON s.trade_id = t.id
        WHERE t.trading_mode = 'live' AND t.outcome = 'skip'
        GROUP BY reason ORDER BY cnt DESC
    """)
    skip_detail = {}
    for row in cur.fetchall():
        skip_detail[row["reason"]] = row["cnt"]

    # ── Peak Balance ─────────────────────────────────────────────────
    cur.execute("""
        SELECT COALESCE(MAX(portfolio_balance_after), 0) AS peak
        FROM trades WHERE portfolio_balance_after IS NOT NULL AND trading_mode = 'live'
    """)
    peak = float(cur.fetchone()["peak"])

    stats = {
        "total": total, "wins": wins, "losses": losses,
        "skips": s["skips"] or 0, "skip_detail": skip_detail,
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
        "avg_win": round(float(s["avg_win"] or 0), 2),
        "avg_loss": round(float(s["avg_loss"] or 0), 2),
        "profit_factor": profit_factor,
        "best_trade": float(s["best_trade"]) if s["best_trade"] is not None else 0,
        "worst_trade": float(s["worst_trade"]) if s["worst_trade"] is not None else 0,
        "peak_balance": peak,
    }

    # ── Recent Trades + Signals (single JOIN) ────────────────────────
    cur.execute("""
        SELECT t.id, t.timestamp, t.market_id, t.side, t.entry_odds, t.position_size,
               t.payout_rate, t.confidence_level, t.outcome, t.pnl,
               t.portfolio_balance_after, t.trading_mode, t.risk_reward_ratio,
               t.skip_reason, t.market_outcome,
               s.chainlink_price, s.spot_price, s.chainlink_spot_divergence,
               s.candle_position_dollars, s.momentum_60s, s.momentum_120s,
               s.cvd, s.order_book_ratio, s.liquidation_signal,
               s.round_number_distance, s.time_regime, s.candle_streak,
               s.momentum_vote, s.reversion_vote, s.structure_vote, s.final_vote,
               s.up_odds, s.down_odds, s.seconds_before_close,
               s.cvd_buy_volume, s.cvd_sell_volume, s.cvd_trade_count,
               s.ob_bid_volume, s.ob_ask_volume,
               s.liq_long_usd, s.liq_short_usd,
               s.poly_book_up_bids, s.poly_book_up_asks,
               s.poly_book_down_bids, s.poly_book_down_asks, s.poly_book_bias,
               s.momentum_direction, s.hour_of_day, s.day_of_week,
               s.fill_price_per_share, s.fill_slippage_pct,
               s.btc_open_price, s.btc_high, s.btc_low, s.btc_entry_price,
               s.btc_volatility, s.poly_spread, s.odds_velocity,
               s.prev_candle_outcome, s.fair_up, s.fair_down,
               s.fair_z_score, s.edge_up_bps, s.edge_down_bps, s.ml_win_prob
        FROM trades t
        LEFT JOIN signals s ON s.trade_id = t.id
        WHERE t.trading_mode = 'live'
        ORDER BY t.id DESC LIMIT 500
    """)
    trade_cols = ['id','timestamp','market_id','side','entry_odds','position_size',
                  'payout_rate','confidence_level','outcome','pnl',
                  'portfolio_balance_after','trading_mode','risk_reward_ratio',
                  'skip_reason','market_outcome']
    trades = []
    for r in cur.fetchall():
        row = dict(r)
        trade = {k: row[k] for k in trade_cols}
        sig_data = {k: row[k] for k in row if k not in trade_cols and k != 'id'}
        # Check if any signal data exists
        has_signal = any(v is not None for k, v in sig_data.items() if k != 'trade_id')
        trade["signals"] = sig_data if has_signal else None
        trades.append(trade)

    # ── Equity Curve + Drawdown ──────────────────────────────────────
    cur.execute("""
        SELECT id, timestamp, portfolio_balance_after, outcome, pnl
        FROM trades
        WHERE trading_mode = 'live' AND outcome IN ('win', 'loss')
          AND portfolio_balance_after IS NOT NULL
        ORDER BY id ASC
    """)
    equity_raw = [dict(r) for r in cur.fetchall()]
    running_peak = 0
    max_dd = 0
    equity_curve = []
    for e in equity_raw:
        bal = e["portfolio_balance_after"]
        if bal > running_peak:
            running_peak = bal
        dd_pct = round((running_peak - bal) / running_peak * 100, 2) if running_peak > 0 else 0
        if dd_pct > max_dd:
            max_dd = dd_pct
        equity_curve.append({**e, "drawdown_pct": dd_pct, "running_peak": running_peak})

    # ── P&L Calendar ─────────────────────────────────────────────────
    cur.execute("""
        SELECT
            TO_CHAR(DATE(timestamp::timestamptz AT TIME ZONE 'America/Los_Angeles'), 'YYYY-MM-DD') AS day,
            SUM(pnl) AS daily_pnl,
            COUNT(*) FILTER (WHERE outcome = 'win') AS wins,
            COUNT(*) FILTER (WHERE outcome = 'loss') AS losses,
            COUNT(*) AS total
        FROM trades
        WHERE trading_mode = 'live' AND outcome IN ('win', 'loss')
        GROUP BY DATE(timestamp::timestamptz AT TIME ZONE 'America/Los_Angeles')
        ORDER BY DATE(timestamp::timestamptz AT TIME ZONE 'America/Los_Angeles')
    """)
    calendar = []
    for r in cur.fetchall():
        calendar.append({
            "day": r["day"],
            "daily_pnl": float(r["daily_pnl"] or 0),
            "wins": int(r["wins"] or 0),
            "losses": int(r["losses"] or 0),
            "total": int(r["total"] or 0),
        })

    # ── Hourly Heatmap ───────────────────────────────────────────────
    cur.execute("""
        SELECT
            EXTRACT(DOW FROM timestamp::timestamptz AT TIME ZONE 'America/Los_Angeles')::int AS dow,
            EXTRACT(HOUR FROM timestamp::timestamptz AT TIME ZONE 'America/Los_Angeles')::int AS hour,
            COUNT(*) AS trades,
            COUNT(*) FILTER (WHERE outcome = 'win') AS wins,
            COALESCE(SUM(pnl), 0) AS total_pnl
        FROM trades
        WHERE trading_mode = 'live' AND outcome IN ('win','loss')
        GROUP BY dow, hour ORDER BY dow, hour
    """)
    hourly_heatmap = [dict(r) for r in cur.fetchall()]

    # ── Vote Combination Win Rates ───────────────────────────────────
    cur.execute("""
        SELECT
            COALESCE(s.momentum_vote, '-') AS mv,
            COALESCE(s.reversion_vote, '-') AS rv,
            COALESCE(s.structure_vote, '-') AS sv,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE t.outcome = 'win') AS wins,
            COALESCE(SUM(t.pnl), 0) AS total_pnl
        FROM trades t
        JOIN signals s ON s.trade_id = t.id
        WHERE t.trading_mode = 'live' AND t.outcome IN ('win','loss')
        GROUP BY mv, rv, sv ORDER BY total DESC
    """)
    vote_combos = [dict(r) for r in cur.fetchall()]

    # ── ML Gate Accuracy ─────────────────────────────────────────────
    cur.execute("""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE t.side = t.market_outcome) AS would_have_won,
            COUNT(*) FILTER (WHERE t.side != t.market_outcome) AS would_have_lost
        FROM trades t
        WHERE t.trading_mode = 'live' AND t.skip_reason = 'ml_gate'
          AND t.market_outcome IS NOT NULL AND t.side IS NOT NULL
    """)
    ml_gate = dict(cur.fetchone())

    # ── Slippage Distribution ────────────────────────────────────────
    cur.execute("""
        SELECT s.fill_slippage_pct, t.outcome
        FROM signals s JOIN trades t ON s.trade_id = t.id
        WHERE t.trading_mode = 'live' AND s.fill_slippage_pct IS NOT NULL
          AND t.outcome IN ('win','loss')
        ORDER BY t.id
    """)
    slippage_data = [dict(r) for r in cur.fetchall()]

    # ── Edge vs Outcome ──────────────────────────────────────────────
    cur.execute("""
        SELECT
            CASE WHEN t.side = 'Up' THEN s.edge_up_bps ELSE s.edge_down_bps END AS edge_bps,
            t.outcome, t.pnl
        FROM trades t JOIN signals s ON s.trade_id = t.id
        WHERE t.trading_mode = 'live' AND t.outcome IN ('win','loss')
          AND s.edge_up_bps IS NOT NULL
        ORDER BY t.id
    """)
    edge_data = [dict(r) for r in cur.fetchall()]

    # ── Sub-Model Accuracy ───────────────────────────────────────────
    cur.execute("""
        SELECT s.momentum_vote, s.reversion_vote, s.structure_vote,
               t.market_outcome
        FROM trades t JOIN signals s ON s.trade_id = t.id
        WHERE t.trading_mode = 'live' AND t.outcome IN ('win','loss')
          AND t.market_outcome IS NOT NULL
    """)
    model_rows = cur.fetchall()
    model_accuracy = {"momentum": {"correct": 0, "total": 0},
                      "reversion": {"correct": 0, "total": 0},
                      "structure": {"correct": 0, "total": 0}}
    for r in model_rows:
        mo = r["market_outcome"]
        for model, vote_col in [("momentum", "momentum_vote"), ("reversion", "reversion_vote"), ("structure", "structure_vote")]:
            vote = r[vote_col]
            if vote and vote != "ABSTAIN":
                model_accuracy[model]["total"] += 1
                if vote == mo:
                    model_accuracy[model]["correct"] += 1

    # ── Current Streak ───────────────────────────────────────────────
    cur.execute("""
        SELECT outcome FROM trades
        WHERE trading_mode = 'live' AND outcome IN ('win','loss')
        ORDER BY id DESC LIMIT 50
    """)
    outcomes_list = [r["outcome"] for r in cur.fetchall()]
    streak_type = outcomes_list[0] if outcomes_list else None
    streak_count = 0
    for o in outcomes_list:
        if o == streak_type:
            streak_count += 1
        else:
            break
    # Last 30 outcomes for dot display
    recent_outcomes = outcomes_list[:30]

    # ── Today Stats ──────────────────────────────────────────────────
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE outcome IN ('win','loss')) AS trades_today,
            COALESCE(SUM(pnl) FILTER (WHERE outcome IN ('win','loss')), 0) AS pnl_today,
            COUNT(*) FILTER (WHERE outcome = 'win') AS wins_today,
            COUNT(*) FILTER (WHERE outcome = 'loss') AS losses_today
        FROM trades
        WHERE trading_mode = 'live'
          AND DATE(timestamp::timestamptz AT TIME ZONE 'America/Los_Angeles') =
              DATE(NOW() AT TIME ZONE 'America/Los_Angeles')
    """)
    today = dict(cur.fetchone())

    cur.close()

    return {
        "portfolio": {
            "balance": round(live_balance, 2),
            "starting": live_starting,
            "pnl": round(live_pnl, 2),
            "pnl_pct": round(live_pnl / live_starting * 100, 2) if live_starting > 0 else 0,
            **stats,
        },
        "trades": trades,
        "equity_curve": equity_curve,
        "calendar": calendar,
        "hourly_heatmap": [dict(r) for r in hourly_heatmap],
        "vote_combos": [dict(r) for r in vote_combos],
        "ml_gate": ml_gate,
        "slippage_data": slippage_data,
        "edge_data": edge_data,
        "model_accuracy": model_accuracy,
        "streak": {"type": streak_type, "count": streak_count, "recent": recent_outcomes},
        "today": today,
        "max_drawdown": max_dd,
    }


# ═══════════════════════════════════════════════════════════════════════
# FRONTEND
# ═══════════════════════════════════════════════════════════════════════

HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>POLYMARKET TERMINAL</title>
<style>
:root {
  --bg: #080b10;
  --bg-card: #0d1117;
  --bg-card-hover: #111820;
  --border: #1b2028;
  --border-bright: #2a3140;
  --text: #c9d1d9;
  --text-dim: #6e7681;
  --text-bright: #e6edf3;
  --green: #3fb950;
  --green-dim: rgba(63,185,80,0.15);
  --red: #f85149;
  --red-dim: rgba(248,81,73,0.15);
  --cyan: #58d1c9;
  --cyan-dim: rgba(88,209,201,0.08);
  --yellow: #d29922;
  --yellow-dim: rgba(210,153,34,0.15);
  --purple: #bc8cff;
  --purple-dim: rgba(188,140,255,0.15);
  --blue: #58a6ff;
  --orange: #f0883e;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: 'SF Mono', 'Cascadia Code', 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
  background: var(--bg);
  color: var(--text);
  font-size: 12px;
  line-height: 1.5;
  overflow-x: hidden;
}

/* ── Ticker Strip ──────────────────────────────────────── */
.ticker {
  display: flex;
  background: #060810;
  border-bottom: 1px solid var(--border);
  padding: 0;
  overflow-x: auto;
  position: sticky;
  top: 0;
  z-index: 100;
}
.ticker-item {
  flex: 1;
  min-width: 120px;
  padding: 8px 16px;
  border-right: 1px solid var(--border);
  text-align: center;
  white-space: nowrap;
}
.ticker-item:last-child { border-right: none; }
.ticker-label { font-size: 9px; text-transform: uppercase; letter-spacing: 1.5px; color: var(--text-dim); }
.ticker-value { font-size: 16px; font-weight: 700; margin-top: 2px; }
.ticker-sub { font-size: 9px; color: var(--text-dim); margin-top: 1px; }

/* ── Layout ────────────────────────────────────────────── */
.terminal {
  display: grid;
  grid-template-columns: 1fr;
  gap: 12px;
  padding: 12px 16px;
  max-width: 1800px;
  margin: 0 auto;
}
.row-2 { display: grid; grid-template-columns: 3fr 2fr; gap: 12px; }
.row-2-equal { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.row-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }

/* ── Card ──────────────────────────────────────────────── */
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px 16px;
  position: relative;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}
.card-title {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 1.5px;
  font-weight: 700;
}
.card-title::before {
  content: '';
  display: inline-block;
  width: 3px;
  height: 10px;
  border-radius: 1px;
  margin-right: 8px;
  vertical-align: middle;
}
.ct-cyan::before { background: var(--cyan); }
.ct-green::before { background: var(--green); }
.ct-yellow::before { background: var(--yellow); }
.ct-red::before { background: var(--red); }
.ct-purple::before { background: var(--purple); }
.ct-blue::before { background: var(--blue); }
.ct-orange::before { background: var(--orange); }

/* ── Stat Grid ─────────────────────────────────────────── */
.stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 20px; }
.stat-row { display: flex; justify-content: space-between; padding: 3px 0; }
.stat-label { color: var(--text-dim); font-size: 11px; }
.stat-value { font-weight: 600; font-size: 11px; }

/* ── Colors ────────────────────────────────────────────── */
.pos { color: var(--green); }
.neg { color: var(--red); }
.dim { color: var(--text-dim); }
.cyan { color: var(--cyan); }
.yellow { color: var(--yellow); }
.purple { color: var(--purple); }

/* ── Badges ────────────────────────────────────────────── */
.badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 9px; font-weight: 700; letter-spacing: 0.5px; }
.b-win { background: var(--green-dim); color: var(--green); }
.b-loss { background: var(--red-dim); color: var(--red); }
.b-skip { background: rgba(110,118,129,0.15); color: var(--text-dim); }
.b-pending { background: var(--yellow-dim); color: var(--yellow); }
.b-failed { background: rgba(248,81,73,0.1); color: var(--orange); }
.b-mlgate { background: var(--purple-dim); color: var(--purple); }

/* ── Streak Dots ───────────────────────────────────────── */
.streak-dots { display: flex; gap: 3px; flex-wrap: wrap; margin-top: 8px; }
.streak-dot { width: 10px; height: 10px; border-radius: 50%; }
.streak-dot.win { background: var(--green); }
.streak-dot.loss { background: var(--red); }

/* ── Heatmap ───────────────────────────────────────────── */
.heatmap { display: grid; grid-template-columns: 40px repeat(24, 1fr); gap: 2px; font-size: 9px; }
.heatmap-label { display: flex; align-items: center; justify-content: flex-end; padding-right: 6px; color: var(--text-dim); font-weight: 600; }
.heatmap-header { text-align: center; color: var(--text-dim); font-weight: 600; padding: 2px 0; }
.heatmap-cell {
  text-align: center; padding: 4px 1px; border-radius: 2px;
  font-size: 8px; font-weight: 600; min-height: 24px;
  display: flex; align-items: center; justify-content: center;
}

/* ── Trade Table ───────────────────────────────────────── */
.table-controls {
  display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap;
}
.table-controls input, .table-controls select {
  background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
  padding: 4px 8px; color: var(--text); font-family: inherit; font-size: 11px;
}
.table-controls input:focus, .table-controls select:focus {
  outline: none; border-color: var(--cyan);
}
.table-controls select { cursor: pointer; }
.table-controls .filter-label { color: var(--text-dim); font-size: 10px; text-transform: uppercase; letter-spacing: 1px; }

table { width: 100%; border-collapse: collapse; font-size: 11px; }
th {
  text-align: left; padding: 6px 8px; color: var(--text-dim);
  border-bottom: 1px solid var(--border-bright); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.5px; cursor: pointer;
  user-select: none; white-space: nowrap;
}
th:hover { color: var(--cyan); }
th .sort-arrow { font-size: 8px; margin-left: 2px; }
td { padding: 5px 8px; border-bottom: 1px solid var(--border); }
.r { text-align: right; }

.trade-row { cursor: pointer; transition: background 0.1s; }
.trade-row:hover { background: var(--bg-card-hover); }
.trade-row td:first-child::before {
  content: '\25B6'; font-size: 7px; margin-right: 4px;
  color: var(--text-dim); display: inline-block; transition: transform 0.15s;
}
.trade-row.expanded td:first-child::before { transform: rotate(90deg); }
.trade-detail { display: none; }
.trade-detail.open { display: table-row; }
.detail-inner {
  padding: 12px 16px; background: rgba(8,11,16,0.6);
  border-left: 3px solid var(--cyan);
}
.detail-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 3px 24px; font-size: 11px; }
.detail-section {
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 1px; color: var(--cyan); margin: 10px 0 4px; grid-column: 1/-1;
}
.detail-section:first-child { margin-top: 0; }
.detail-row { display: flex; justify-content: space-between; padding: 1px 0; }
.detail-label { color: var(--text-dim); }
.detail-value { font-weight: 600; }

/* ── Pagination ────────────────────────────────────────── */
.pagination { display: flex; justify-content: center; gap: 8px; margin-top: 10px; align-items: center; }
.pagination button {
  background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
  padding: 4px 12px; color: var(--text); cursor: pointer; font-family: inherit; font-size: 11px;
}
.pagination button:hover { border-color: var(--cyan); }
.pagination button:disabled { opacity: 0.3; cursor: default; }
.pagination span { color: var(--text-dim); font-size: 11px; }

/* ── Calendar ──────────────────────────────────────────── */
.cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; font-size: 10px; }
.cal-header { text-align: center; color: var(--text-dim); font-weight: 600; padding: 4px; font-size: 9px; }
.cal-day {
  text-align: center; padding: 6px 2px; border-radius: 4px;
  border: 1px solid var(--border);
}
.cal-day.win { background: var(--green-dim); border-color: rgba(63,185,80,0.3); }
.cal-day.loss { background: var(--red-dim); border-color: rgba(248,81,73,0.3); }
.cal-day.empty { border-color: transparent; }
.cal-pnl { font-weight: 700; font-size: 10px; }
.cal-record { font-size: 8px; color: var(--text-dim); }

/* ── Toggle Button ─────────────────────────────────────── */
.toggle-btn {
  background: var(--bg); color: var(--text-dim); border: 1px solid var(--border);
  border-radius: 3px; padding: 2px 8px; cursor: pointer; font-family: inherit;
  font-size: 9px; letter-spacing: 1px; font-weight: 600;
}
.toggle-btn:hover { border-color: var(--cyan); color: var(--text); }
.toggle-btn.active { border-color: var(--cyan); color: var(--cyan); background: var(--cyan-dim); }

/* ── Hidden Mode ───────────────────────────────────────── */
.hidden-mode .sensitive { visibility: hidden !important; }
.hidden-mode .sensitive::after { content: '••••'; visibility: visible; color: var(--text-dim); }

/* ── Vote badges ───────────────────────────────────────── */
.vote-up { color: var(--green); font-weight: 700; }
.vote-down { color: var(--red); font-weight: 700; }
.vote-abstain { color: var(--text-dim); }

/* ── ML Gate Bar ───────────────────────────────────────── */
.gate-bar { height: 20px; border-radius: 3px; display: flex; overflow: hidden; margin: 8px 0; }
.gate-correct { background: var(--green); }
.gate-incorrect { background: var(--red); }

/* ── Footer ────────────────────────────────────────────── */
.footer { text-align: center; font-size: 10px; color: var(--text-dim); padding: 12px 0; }

/* ── Scrollbar ─────────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border-bright); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-dim); }

@media (max-width: 1200px) {
  .row-2 { grid-template-columns: 1fr; }
  .row-2-equal { grid-template-columns: 1fr; }
  .row-3 { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<!-- ══ TICKER STRIP ═══════════════════════════════════════ -->
<div class="ticker" id="ticker">
  <div class="ticker-item"><div class="ticker-label">Balance</div><div class="ticker-value sensitive" id="tk-balance">--</div></div>
  <div class="ticker-item"><div class="ticker-label">P&L</div><div class="ticker-value sensitive" id="tk-pnl">--</div><div class="ticker-sub" id="tk-pnl-pct">--</div></div>
  <div class="ticker-item"><div class="ticker-label">Win Rate</div><div class="ticker-value" id="tk-winrate">--</div><div class="ticker-sub" id="tk-record">--</div></div>
  <div class="ticker-item"><div class="ticker-label">Streak</div><div class="ticker-value" id="tk-streak">--</div></div>
  <div class="ticker-item"><div class="ticker-label">Today</div><div class="ticker-value" id="tk-today-pnl">--</div><div class="ticker-sub" id="tk-today-record">--</div></div>
  <div class="ticker-item"><div class="ticker-label">Max Drawdown</div><div class="ticker-value" id="tk-drawdown">--</div></div>
  <div class="ticker-item"><div class="ticker-label">Profit Factor</div><div class="ticker-value" id="tk-pf">--</div></div>
  <div class="ticker-item"><div class="ticker-label">Avg Win / Loss</div><div class="ticker-value" id="tk-avg">--</div></div>
  <div class="ticker-item">
    <div class="ticker-label">Controls</div>
    <div style="display:flex; gap:4px; justify-content:center; margin-top:4px;">
      <button class="toggle-btn" id="btn-hide" onclick="toggleHide()">HIDE $</button>
    </div>
  </div>
</div>

<div class="terminal">

<!-- ══ ROW 1: Equity + Stats ══════════════════════════════ -->
<div class="row-2">
  <div class="card">
    <div class="card-header">
      <span class="card-title ct-cyan">Equity Curve & Drawdown</span>
    </div>
    <canvas id="equityChart" height="200"></canvas>
  </div>
  <div>
    <div class="card" style="margin-bottom:12px">
      <div class="card-header">
        <span class="card-title ct-green">Portfolio</span>
      </div>
      <div id="portfolio-stats"></div>
    </div>
    <div class="card" style="margin-bottom:12px">
      <div class="card-header">
        <span class="card-title ct-yellow">Streak</span>
      </div>
      <div id="streak-display"></div>
    </div>
    <div class="card">
      <div class="card-header">
        <span class="card-title ct-purple">ML Gate Accuracy</span>
      </div>
      <div id="ml-gate-display"></div>
    </div>
  </div>
</div>

<!-- ══ ROW 2: Daily P&L + Win Rate ════════════════════════ -->
<div class="row-3">
  <div class="card">
    <div class="card-header">
      <span class="card-title ct-green">Daily P&L</span>
    </div>
    <canvas id="dailyPnlChart" height="160"></canvas>
  </div>
  <div class="card">
    <div class="card-header">
      <span class="card-title ct-cyan">Win Rate Over Time</span>
    </div>
    <canvas id="winrateChart" height="160"></canvas>
  </div>
  <div class="card">
    <div class="card-header">
      <span class="card-title ct-yellow">Profit Factor Over Time</span>
    </div>
    <canvas id="pfChart" height="160"></canvas>
  </div>
</div>

<!-- ══ ROW 3: Heatmap ═════════════════════════════════════ -->
<div class="card">
  <div class="card-header">
    <span class="card-title ct-orange">Hourly Performance Heatmap</span>
    <span class="dim" style="font-size:10px">PST &middot; Win Rate %</span>
  </div>
  <div id="heatmap"></div>
</div>

<!-- ══ ROW 4: Analytics ═══════════════════════════════════ -->
<div class="row-3">
  <div class="card">
    <div class="card-header">
      <span class="card-title ct-blue">Model Accuracy</span>
    </div>
    <canvas id="modelChart" height="200"></canvas>
  </div>
  <div class="card">
    <div class="card-header">
      <span class="card-title ct-yellow">Slippage Distribution</span>
    </div>
    <canvas id="slippageChart" height="200"></canvas>
  </div>
  <div class="card">
    <div class="card-header">
      <span class="card-title ct-purple">Edge vs Outcome</span>
    </div>
    <canvas id="edgeChart" height="200"></canvas>
  </div>
</div>

<!-- ══ ROW 5: Vote Combos + Skip Breakdown ════════════════ -->
<div class="row-2-equal">
  <div class="card">
    <div class="card-header">
      <span class="card-title ct-cyan">Vote Combination Win Rates</span>
    </div>
    <canvas id="voteChart" height="200"></canvas>
  </div>
  <div class="card" style="display:flex; gap:20px; align-items:flex-start; flex-wrap:wrap;">
    <div style="flex:1; min-width:140px;">
      <div class="card-header" style="margin-bottom:8px;">
        <span class="card-title ct-red">Skip Breakdown</span>
      </div>
      <canvas id="skipChart" height="180" width="180"></canvas>
    </div>
    <div style="flex:1; min-width:140px;">
      <div id="skipLegend" style="margin-top:40px;"></div>
    </div>
  </div>
</div>

<!-- ══ ROW 6: Calendar ════════════════════════════════════ -->
<div class="card">
  <div class="card-header">
    <span class="card-title ct-yellow">P&L Calendar</span>
    <span class="dim" style="font-size:10px">PST timezone</span>
  </div>
  <div id="calendar"></div>
</div>

<!-- ══ ROW 7: Trade Table ═════════════════════════════════ -->
<div class="card">
  <div class="card-header">
    <span class="card-title ct-cyan">Trade Log</span>
    <span class="dim" style="font-size:10px" id="trade-count"></span>
  </div>
  <div class="table-controls">
    <input type="text" id="search" placeholder="Search market..." style="width:160px;">
    <span class="filter-label">Side</span>
    <select id="filter-side"><option value="">All</option><option value="Up">Up</option><option value="Down">Down</option></select>
    <span class="filter-label">Result</span>
    <select id="filter-outcome"><option value="">All</option><option value="win">Win</option><option value="loss">Loss</option><option value="skip">Skip</option><option value="pending">Pending</option></select>
    <span class="filter-label">Confidence</span>
    <select id="filter-conf"><option value="">All</option><option value="high">High</option><option value="medium">Medium</option></select>
    <button class="toggle-btn" id="btn-hide-skips" onclick="toggleSkips()" style="margin-left:auto;">HIDE SKIPS</button>
  </div>
  <table>
    <thead><tr>
      <th onclick="sortBy('id')"># <span class="sort-arrow" id="sort-id"></span></th>
      <th onclick="sortBy('time')">Time <span class="sort-arrow" id="sort-time"></span></th>
      <th onclick="sortBy('side')">Side <span class="sort-arrow" id="sort-side"></span></th>
      <th class="r" onclick="sortBy('cost')">Cost <span class="sort-arrow" id="sort-cost"></span></th>
      <th class="r" onclick="sortBy('rr')">R:R <span class="sort-arrow" id="sort-rr"></span></th>
      <th class="r" onclick="sortBy('ml')">ML% <span class="sort-arrow" id="sort-ml"></span></th>
      <th onclick="sortBy('result')">Result <span class="sort-arrow" id="sort-result"></span></th>
      <th class="r" onclick="sortBy('pnl')">P&L <span class="sort-arrow" id="sort-pnl"></span></th>
      <th class="r" onclick="sortBy('balance')">Balance <span class="sort-arrow" id="sort-balance"></span></th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <div class="pagination" id="pagination"></div>
</div>

</div><!-- end .terminal -->

<div class="footer" id="footer">Connecting...</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script>
// ═══════════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════════
let DATA = null;
let equityChart=null, dailyPnlChart=null, winrateChart=null, skipChart=null, pfChart=null;
let modelChart=null, slippageChart=null, edgeChart=null, voteChart=null;
let hideSkips = localStorage.getItem('hideSkips') !== 'false';
let portfolioHidden = localStorage.getItem('hidePortfolio') === 'true';
let sortCol = 'id', sortDir = 'desc';
let currentPage = 0;
const PAGE_SIZE = 50;

const fmt = (n, d=2) => n != null ? n.toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d}) : '-';
const pnlClass = n => n >= 0 ? 'pos' : 'neg';
const pnlStr = (n, d=2) => n != null ? `<span class="${pnlClass(n)}">${n>=0?'+':''}$${fmt(Math.abs(n), d)}</span>` : '-';

Chart.defaults.color = '#6e7681';
Chart.defaults.borderColor = 'rgba(27,32,40,0.8)';
Chart.defaults.font.family = "'SF Mono','Consolas',monospace";
Chart.defaults.font.size = 10;

// ═══════════════════════════════════════════════════════════════
// TOGGLES
// ═══════════════════════════════════════════════════════════════
function toggleHide() {
  portfolioHidden = !portfolioHidden;
  localStorage.setItem('hidePortfolio', portfolioHidden);
  document.body.classList.toggle('hidden-mode', portfolioHidden);
  const btn = document.getElementById('btn-hide');
  btn.textContent = portfolioHidden ? 'SHOW $' : 'HIDE $';
  btn.classList.toggle('active', portfolioHidden);
}
if (portfolioHidden) {
  document.body.classList.add('hidden-mode');
  document.getElementById('btn-hide').textContent = 'SHOW $';
  document.getElementById('btn-hide').classList.add('active');
}

function toggleSkips() {
  hideSkips = !hideSkips;
  localStorage.setItem('hideSkips', hideSkips);
  const btn = document.getElementById('btn-hide-skips');
  btn.textContent = hideSkips ? 'HIDE SKIPS' : 'SHOW ALL';
  btn.className = 'toggle-btn' + (hideSkips ? ' active' : '');
  renderTrades();
}
if (!hideSkips) {
  document.getElementById('btn-hide-skips').textContent = 'SHOW ALL';
  document.getElementById('btn-hide-skips').className = 'toggle-btn';
}

// Filter change handlers
['search','filter-side','filter-outcome','filter-conf'].forEach(id => {
  document.getElementById(id).addEventListener(id === 'search' ? 'input' : 'change', () => { currentPage = 0; renderTrades(); });
});

function sortBy(col) {
  if (sortCol === col) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
  else { sortCol = col; sortDir = 'desc'; }
  document.querySelectorAll('.sort-arrow').forEach(s => s.textContent = '');
  const el = document.getElementById('sort-' + col);
  if (el) el.textContent = sortDir === 'asc' ? '\u25B2' : '\u25BC';
  renderTrades();
}

// ═══════════════════════════════════════════════════════════════
// RENDER: TICKER
// ═══════════════════════════════════════════════════════════════
function renderTicker(d) {
  const p = d.portfolio;
  const s = d.streak;
  const t = d.today;

  document.getElementById('tk-balance').innerHTML = `$${fmt(p.balance)}`;
  document.getElementById('tk-balance').className = 'ticker-value sensitive';

  const pnlEl = document.getElementById('tk-pnl');
  pnlEl.innerHTML = `${p.pnl>=0?'+':''}$${fmt(p.pnl)}`;
  pnlEl.className = `ticker-value sensitive ${pnlClass(p.pnl)}`;
  document.getElementById('tk-pnl-pct').innerHTML = `${p.pnl_pct>=0?'+':''}${fmt(p.pnl_pct,1)}%`;

  document.getElementById('tk-winrate').innerHTML = `${fmt(p.win_rate,1)}%`;
  document.getElementById('tk-record').innerHTML = `${p.wins}W / ${p.losses}L`;

  const streakEl = document.getElementById('tk-streak');
  if (s.type) {
    streakEl.innerHTML = `${s.count}${s.type === 'win' ? 'W' : 'L'}`;
    streakEl.className = `ticker-value ${s.type === 'win' ? 'pos' : 'neg'}`;
  } else { streakEl.innerHTML = '-'; streakEl.className = 'ticker-value dim'; }

  const todayPnl = parseFloat(t.pnl_today) || 0;
  const todayPct = p.balance > 0 ? (todayPnl / (p.balance - todayPnl) * 100) : 0;
  const todayEl = document.getElementById('tk-today-pnl');
  todayEl.innerHTML = `${todayPnl>=0?'+':''}$${fmt(todayPnl)}`;
  todayEl.className = `ticker-value ${pnlClass(todayPnl)}`;
  document.getElementById('tk-today-record').innerHTML = `${todayPct>=0?'+':''}${fmt(todayPct,1)}% | ${t.wins_today||0}W / ${t.losses_today||0}L`;

  document.getElementById('tk-drawdown').innerHTML = `${fmt(d.max_drawdown,1)}%`;
  document.getElementById('tk-drawdown').className = `ticker-value ${d.max_drawdown > 10 ? 'neg' : 'dim'}`;

  document.getElementById('tk-pf').innerHTML = `${fmt(p.profit_factor,2)}`;
  document.getElementById('tk-pf').className = `ticker-value ${p.profit_factor >= 1.5 ? 'pos' : p.profit_factor >= 1 ? 'yellow' : 'neg'}`;

  document.getElementById('tk-avg').innerHTML = `<span class="pos">+$${fmt(p.avg_win)}</span> / <span class="neg">$${fmt(Math.abs(p.avg_loss))}</span>`;
}

// ═══════════════════════════════════════════════════════════════
// RENDER: PORTFOLIO STATS
// ═══════════════════════════════════════════════════════════════
function renderPortfolio(d) {
  const p = d.portfolio;
  document.getElementById('portfolio-stats').innerHTML = `
    <div class="stat-grid">
      <div class="stat-row"><span class="stat-label">Starting</span><span class="stat-value sensitive">$${fmt(p.starting)}</span></div>
      <div class="stat-row"><span class="stat-label">Peak</span><span class="stat-value">$${fmt(p.peak_balance)}</span></div>
      <div class="stat-row"><span class="stat-label">Trades</span><span class="stat-value">${p.total}</span></div>
      <div class="stat-row"><span class="stat-label">Skips</span><span class="stat-value">${p.skips}</span></div>
      <div class="stat-row"><span class="stat-label">Best</span><span class="stat-value sensitive pos">+$${fmt(p.best_trade)}</span></div>
      <div class="stat-row"><span class="stat-label">Worst</span><span class="stat-value sensitive neg">$${fmt(p.worst_trade)}</span></div>
    </div>`;
}

// ═══════════════════════════════════════════════════════════════
// RENDER: STREAK
// ═══════════════════════════════════════════════════════════════
function renderStreak(d) {
  const s = d.streak;
  let big = '-';
  let cls = '';
  if (s.type) { big = s.count; cls = s.type === 'win' ? 'pos' : 'neg'; }
  let dots = s.recent.map(o => `<div class="streak-dot ${o}"></div>`).join('');
  document.getElementById('streak-display').innerHTML = `
    <div style="display:flex; align-items:center; gap:16px;">
      <div style="text-align:center;">
        <div style="font-size:32px; font-weight:800;" class="${cls}">${big}</div>
        <div style="font-size:10px; color:var(--text-dim);">${s.type ? s.type.toUpperCase() + ' STREAK' : 'NO TRADES'}</div>
      </div>
      <div style="flex:1;"><div class="streak-dots">${dots}</div></div>
    </div>`;
}

// ═══════════════════════════════════════════════════════════════
// RENDER: ML GATE
// ═══════════════════════════════════════════════════════════════
function renderMLGate(d) {
  const g = d.ml_gate;
  const total = g.total || 0;
  const won = g.would_have_won || 0;
  const lost = g.would_have_lost || 0;
  const accuracy = total > 0 ? Math.round(lost / total * 100) : 0;
  const wonPct = total > 0 ? Math.round(won / total * 100) : 0;
  const lostPct = total > 0 ? Math.round(lost / total * 100) : 0;

  document.getElementById('ml-gate-display').innerHTML = `
    <div class="stat-grid">
      <div class="stat-row"><span class="stat-label">Total Blocked</span><span class="stat-value">${total}</span></div>
      <div class="stat-row"><span class="stat-label">Gate Accuracy</span><span class="stat-value ${accuracy >= 50 ? 'pos' : 'neg'}">${accuracy}%</span></div>
      <div class="stat-row"><span class="stat-label">Correctly Blocked</span><span class="stat-value pos">${lost} (${lostPct}%)</span></div>
      <div class="stat-row"><span class="stat-label">Missed Winners</span><span class="stat-value neg">${won} (${wonPct}%)</span></div>
    </div>
    <div class="gate-bar">
      ${lost > 0 ? `<div class="gate-correct" style="width:${lostPct}%"></div>` : ''}
      ${won > 0 ? `<div class="gate-incorrect" style="width:${wonPct}%"></div>` : ''}
    </div>
    <div style="display:flex; justify-content:space-between; font-size:9px;">
      <span class="pos">Correct blocks</span><span class="neg">Missed winners</span>
    </div>`;
}

// ═══════════════════════════════════════════════════════════════
// RENDER: EQUITY + DRAWDOWN
// ═══════════════════════════════════════════════════════════════
function renderEquity(d) {
  const ec = d.equity_curve;
  if (!ec || ec.length < 1) return;
  const labels = ec.map(e => '#' + e.id);
  const balances = ec.map(e => e.portfolio_balance_after);
  const drawdowns = ec.map(e => e.drawdown_pct);
  const colors = ec.map(e => e.outcome === 'win' ? '#3fb950' : '#f85149');

  const ctx = document.getElementById('equityChart').getContext('2d');
  if (equityChart) {
    equityChart.data.labels = labels;
    equityChart.data.datasets[0].data = balances;
    equityChart.data.datasets[0].pointBackgroundColor = colors;
    equityChart.data.datasets[1].data = drawdowns;
    equityChart.update('none');
  } else {
    equityChart = new Chart(ctx, {
      data: {
        labels,
        datasets: [{
          type: 'line', label: 'Balance', data: balances, yAxisID: 'y',
          borderColor: '#58d1c9', borderWidth: 2, pointBackgroundColor: colors,
          pointRadius: 3, pointHoverRadius: 5,
          fill: { target: 'origin', above: 'rgba(88,209,201,0.05)' }, tension: 0.3,
        }, {
          type: 'line', label: 'Drawdown %', data: drawdowns, yAxisID: 'y1',
          borderColor: 'rgba(248,81,73,0.4)', borderWidth: 1, pointRadius: 0,
          fill: { target: 'origin', above: 'rgba(248,81,73,0.08)' }, tension: 0.3,
        }]
      },
      options: {
        responsive: true, interaction: { mode: 'index', intersect: false },
        plugins: { legend: { display: true, labels: { boxWidth: 12, font: { size: 9 } } },
          tooltip: { callbacks: { label: function(ctx) {
            if (ctx.datasetIndex === 0) {
              const e = ec[ctx.dataIndex];
              const pnl = e.pnl >= 0 ? '+$'+e.pnl.toFixed(2) : '-$'+Math.abs(e.pnl).toFixed(2);
              return 'Balance: $'+ctx.parsed.y.toFixed(2)+' ('+e.outcome.toUpperCase()+' '+pnl+')';
            }
            return 'Drawdown: '+ctx.parsed.y.toFixed(1)+'%';
          }}}
        },
        scales: {
          x: { ticks: { maxTicksLimit: 20, font: { size: 8 } } },
          y: { position: 'left', ticks: { callback: v => '$'+v.toFixed(0) } },
          y1: { position: 'right', reverse: true, min: 0, ticks: { callback: v => v.toFixed(0)+'%' },
            grid: { drawOnChartArea: false } },
        }
      }
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// RENDER: DAILY P&L
// ═══════════════════════════════════════════════════════════════
function renderDailyPnl(d) {
  const cal = d.calendar;
  if (!cal || cal.length < 1) return;
  const labels = cal.map(c => c.day.slice(5));
  const data = cal.map(c => c.daily_pnl);
  const colors = data.map(v => v >= 0 ? '#3fb950' : '#f85149');

  const ctx = document.getElementById('dailyPnlChart').getContext('2d');
  if (dailyPnlChart) {
    dailyPnlChart.data.labels = labels;
    dailyPnlChart.data.datasets[0].data = data;
    dailyPnlChart.data.datasets[0].backgroundColor = colors;
    dailyPnlChart.update('none');
  } else {
    dailyPnlChart = new Chart(ctx, {
      type: 'bar',
      data: { labels, datasets: [{ label: 'Daily P&L', data, backgroundColor: colors, borderRadius: 3 }] },
      options: {
        responsive: true,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: ctx => {
            const c = cal[ctx.dataIndex];
            return `$${ctx.parsed.y>=0?'+':''}${ctx.parsed.y.toFixed(2)} (${c.wins}W/${c.losses}L)`;
          }}}
        },
        scales: {
          x: { ticks: { font: { size: 9 } } },
          y: { ticks: { callback: v => '$'+v.toFixed(0) } },
        }
      }
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// RENDER: WIN RATE OVER TIME
// ═══════════════════════════════════════════════════════════════
function renderWinRate(d) {
  const ec = d.equity_curve;
  if (!ec || ec.length < 2) return;
  let cumWins = 0;
  const labels = [], data = [];
  for (let i = 0; i < ec.length; i++) {
    if (ec[i].outcome === 'win') cumWins++;
    labels.push('#' + ec[i].id);
    data.push(Math.round(cumWins / (i+1) * 1000) / 10);
  }
  const ctx = document.getElementById('winrateChart').getContext('2d');
  if (winrateChart) {
    winrateChart.data.labels = labels;
    winrateChart.data.datasets[0].data = data;
    winrateChart.update('none');
  } else {
    winrateChart = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets: [{
        label: 'Win Rate %', data, borderColor: '#3fb950', borderWidth: 2,
        pointRadius: 1, pointHoverRadius: 4,
        fill: { target: 'origin', above: 'rgba(63,185,80,0.06)' }, tension: 0.3,
      }, {
        label: '50%', data: data.map(() => 50),
        borderColor: 'rgba(110,118,129,0.3)', borderWidth: 1, borderDash: [5,5],
        pointRadius: 0, fill: false,
      }]},
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { maxTicksLimit: 20, font: { size: 8 } } },
          y: { min: 0, max: 100, ticks: { callback: v => v+'%' } },
        }
      }
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// RENDER: PROFIT FACTOR OVER TIME
// ═══════════════════════════════════════════════════════════════
function renderProfitFactor(d) {
  const ec = d.equity_curve;
  if (!ec || ec.length < 5) return;
  let cumWins = 0, cumLosses = 0;
  const labels = [], data = [];
  for (let i = 0; i < ec.length; i++) {
    const pnl = ec[i].pnl || 0;
    if (pnl > 0) cumWins += pnl;
    else cumLosses += Math.abs(pnl);
    labels.push('#' + ec[i].id);
    data.push(cumLosses > 0 ? Math.round(cumWins / cumLosses * 100) / 100 : 0);
  }
  const ctx = document.getElementById('pfChart').getContext('2d');
  if (pfChart) {
    pfChart.data.labels = labels;
    pfChart.data.datasets[0].data = data;
    pfChart.update('none');
  } else {
    pfChart = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets: [{
        label: 'Profit Factor', data, borderColor: '#d29922', borderWidth: 2,
        pointRadius: 1, pointHoverRadius: 4,
        fill: { target: 'origin', above: 'rgba(210,153,34,0.06)' }, tension: 0.3,
      }, {
        label: '1.0', data: data.map(() => 1.0),
        borderColor: 'rgba(110,118,129,0.3)', borderWidth: 1, borderDash: [5,5],
        pointRadius: 0, fill: false,
      }]},
      options: {
        responsive: true,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: ctx => ctx.datasetIndex === 0 ? 'PF: ' + ctx.parsed.y.toFixed(2) : null } }
        },
        scales: {
          x: { ticks: { maxTicksLimit: 20, font: { size: 8 } } },
          y: { min: 0, ticks: { callback: v => v.toFixed(1) } },
        }
      }
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// RENDER: HEATMAP
// ═══════════════════════════════════════════════════════════════
function renderHeatmap(d) {
  const hm = d.hourly_heatmap;
  const dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  // Build lookup: heatData[dow][hour] = {trades, wins, pnl}
  const heatData = {};
  hm.forEach(h => {
    const key = h.dow + '-' + h.hour;
    heatData[key] = h;
  });

  let html = '<div class="heatmap">';
  // Header row
  html += '<div class="heatmap-label"></div>';
  for (let h = 0; h < 24; h++) {
    html += `<div class="heatmap-header">${h}</div>`;
  }
  // Data rows
  for (let dow = 1; dow <= 6; dow++) {
    html += `<div class="heatmap-label">${dayNames[dow]}</div>`;
    for (let h = 0; h < 24; h++) {
      const cell = heatData[dow + '-' + h];
      if (cell && cell.trades > 0) {
        const wr = Math.round(cell.wins / cell.trades * 100);
        const intensity = Math.min(Math.abs(wr - 50) / 50, 1);
        const bg = wr >= 50
          ? `rgba(63,185,80,${0.1 + intensity * 0.5})`
          : `rgba(248,81,73,${0.1 + intensity * 0.5})`;
        html += `<div class="heatmap-cell" style="background:${bg};" title="${cell.trades} trades, ${wr}% win, $${parseFloat(cell.total_pnl).toFixed(2)}">${wr}%</div>`;
      } else {
        html += `<div class="heatmap-cell" style="background:rgba(27,32,40,0.3);"></div>`;
      }
    }
  }
  // Sunday
  html += `<div class="heatmap-label">${dayNames[0]}</div>`;
  for (let h = 0; h < 24; h++) {
    const cell = heatData['0-' + h];
    if (cell && cell.trades > 0) {
      const wr = Math.round(cell.wins / cell.trades * 100);
      const intensity = Math.min(Math.abs(wr - 50) / 50, 1);
      const bg = wr >= 50 ? `rgba(63,185,80,${0.1+intensity*0.5})` : `rgba(248,81,73,${0.1+intensity*0.5})`;
      html += `<div class="heatmap-cell" style="background:${bg};" title="${cell.trades} trades">${wr}%</div>`;
    } else {
      html += `<div class="heatmap-cell" style="background:rgba(27,32,40,0.3);"></div>`;
    }
  }
  html += '</div>';
  document.getElementById('heatmap').innerHTML = html;
}

// ═══════════════════════════════════════════════════════════════
// RENDER: MODEL ACCURACY
// ═══════════════════════════════════════════════════════════════
function renderModelAccuracy(d) {
  const ma = d.model_accuracy;
  const labels = ['Momentum', 'Reversion', 'Structure'];
  const accs = labels.map(l => {
    const k = l.toLowerCase();
    return ma[k].total > 0 ? Math.round(ma[k].correct / ma[k].total * 100) : 0;
  });
  const totals = labels.map(l => ma[l.toLowerCase()].total);
  const colors = accs.map(a => a >= 55 ? '#3fb950' : a >= 45 ? '#d29922' : '#f85149');

  const ctx = document.getElementById('modelChart').getContext('2d');
  if (modelChart) {
    modelChart.data.datasets[0].data = accs;
    modelChart.data.datasets[0].backgroundColor = colors;
    modelChart.update('none');
  } else {
    modelChart = new Chart(ctx, {
      type: 'bar',
      data: { labels, datasets: [{ label: 'Accuracy %', data: accs, backgroundColor: colors, borderRadius: 4, barPercentage: 0.6 }] },
      options: {
        responsive: true, indexAxis: 'y',
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: ctx => `${ctx.parsed.x}% (${totals[ctx.dataIndex]} votes)` } }
        },
        scales: {
          x: { min: 0, max: 100, ticks: { callback: v => v+'%' } },
          y: { ticks: { font: { size: 11, weight: 'bold' } } },
        }
      }
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// RENDER: SLIPPAGE HISTOGRAM
// ═══════════════════════════════════════════════════════════════
function renderSlippage(d) {
  const sd = d.slippage_data;
  if (!sd || sd.length < 1) return;
  const buckets = [
    { label: '<-50%', min: -Infinity, max: -50 },
    { label: '-50 to -20', min: -50, max: -20 },
    { label: '-20 to 0', min: -20, max: 0 },
    { label: '0 to 10', min: 0, max: 10 },
    { label: '10 to 30', min: 10, max: 30 },
    { label: '>30%', min: 30, max: Infinity },
  ];
  const counts = buckets.map(() => ({ total: 0, wins: 0 }));
  sd.forEach(s => {
    const v = s.fill_slippage_pct;
    for (let i = 0; i < buckets.length; i++) {
      if (v >= buckets[i].min && v < buckets[i].max) {
        counts[i].total++;
        if (s.outcome === 'win') counts[i].wins++;
        break;
      }
    }
  });

  const ctx = document.getElementById('slippageChart').getContext('2d');
  if (slippageChart) {
    slippageChart.data.datasets[0].data = counts.map(c => c.total);
    slippageChart.data.datasets[1].data = counts.map(c => c.wins);
    slippageChart.update('none');
  } else {
    slippageChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: buckets.map(b => b.label),
        datasets: [
          { label: 'Total', data: counts.map(c => c.total), backgroundColor: 'rgba(88,166,255,0.3)', borderRadius: 3 },
          { label: 'Wins', data: counts.map(c => c.wins), backgroundColor: 'rgba(63,185,80,0.5)', borderRadius: 3 },
        ]
      },
      options: {
        responsive: true,
        plugins: { legend: { labels: { boxWidth: 10, font: { size: 9 } } } },
        scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } }
      }
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// RENDER: EDGE VS OUTCOME
// ═══════════════════════════════════════════════════════════════
function renderEdge(d) {
  const ed = d.edge_data;
  if (!ed || ed.length < 1) return;
  const wins = ed.filter(e => e.outcome === 'win').map(e => ({ x: e.edge_bps, y: e.pnl }));
  const losses = ed.filter(e => e.outcome === 'loss').map(e => ({ x: e.edge_bps, y: e.pnl }));

  const ctx = document.getElementById('edgeChart').getContext('2d');
  if (edgeChart) {
    edgeChart.data.datasets[0].data = wins;
    edgeChart.data.datasets[1].data = losses;
    edgeChart.update('none');
  } else {
    edgeChart = new Chart(ctx, {
      type: 'scatter',
      data: {
        datasets: [
          { label: 'Win', data: wins, backgroundColor: 'rgba(63,185,80,0.6)', pointRadius: 4 },
          { label: 'Loss', data: losses, backgroundColor: 'rgba(248,81,73,0.6)', pointRadius: 4 },
        ]
      },
      options: {
        responsive: true,
        plugins: {
          legend: { labels: { boxWidth: 10, font: { size: 9 } } },
          tooltip: { callbacks: { label: ctx => `Edge: ${ctx.parsed.x.toFixed(0)}bps, P&L: $${ctx.parsed.y.toFixed(2)}` } }
        },
        scales: {
          x: { title: { display: true, text: 'Edge (bps)', font: { size: 9 } } },
          y: { title: { display: true, text: 'P&L ($)', font: { size: 9 } }, ticks: { callback: v => '$'+v.toFixed(0) } },
        }
      }
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// RENDER: VOTE COMBOS
// ═══════════════════════════════════════════════════════════════
function renderVoteCombos(d) {
  const vc = d.vote_combos;
  if (!vc || vc.length < 1) return;
  const items = vc.slice(0, 12).map(v => ({
    label: `${v.mv[0]}/${v.rv[0]}/${v.sv[0]}`,
    wr: v.total > 0 ? Math.round(v.wins / v.total * 100) : 0,
    total: v.total,
    pnl: parseFloat(v.total_pnl),
  }));

  const ctx = document.getElementById('voteChart').getContext('2d');
  const colors = items.map(i => i.wr >= 55 ? '#3fb950' : i.wr >= 45 ? '#d29922' : '#f85149');

  if (voteChart) {
    voteChart.data.labels = items.map(i => i.label);
    voteChart.data.datasets[0].data = items.map(i => i.wr);
    voteChart.data.datasets[0].backgroundColor = colors;
    voteChart.update('none');
  } else {
    voteChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: items.map(i => i.label),
        datasets: [{ label: 'Win Rate %', data: items.map(i => i.wr), backgroundColor: colors, borderRadius: 3, barPercentage: 0.7 }]
      },
      options: {
        responsive: true, indexAxis: 'y',
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: ctx => {
            const i = items[ctx.dataIndex];
            return `${i.wr}% win (${i.total} trades, $${i.pnl>=0?'+':''}${i.pnl.toFixed(2)})`;
          }}}
        },
        scales: {
          x: { min: 0, max: 100, ticks: { callback: v => v+'%' } },
          y: { ticks: { font: { size: 9 } } },
        }
      }
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// RENDER: SKIP BREAKDOWN
// ═══════════════════════════════════════════════════════════════
function renderSkips(d) {
  const sd = d.portfolio.skip_detail;
  const colorMap = {
    'no_consensus':'#6e7681', 'risk_blocked':'#f85149', 'order_rejected':'#f0883e',
    'empty_book':'#d29922', 'insufficient_liquidity':'#e3b341', 'ml_gate':'#bc8cff',
    'price_out_of_range':'#58a6ff', 'service_unavailable':'#58a6ff', 'invalid_amount':'#ff7b72',
  };
  const nameMap = {
    'no_consensus':'No Consensus', 'risk_blocked':'Risk Blocked', 'order_rejected':'Order Rejected',
    'empty_book':'Empty Book', 'insufficient_liquidity':'Low Liquidity', 'ml_gate':'ML Gate',
    'price_out_of_range':'Price Range', 'service_unavailable':'Service Down', 'invalid_amount':'Invalid Amt',
  };
  const labels=[], data=[], colors=[];
  for (const [k,v] of Object.entries(sd)) {
    if (v > 0) { labels.push(nameMap[k]||k); data.push(v); colors.push(colorMap[k]||'#6e7681'); }
  }
  const ctx = document.getElementById('skipChart').getContext('2d');
  if (skipChart) {
    skipChart.data.labels = labels; skipChart.data.datasets[0].data = data;
    skipChart.data.datasets[0].backgroundColor = colors; skipChart.update('none');
  } else if (data.length > 0) {
    skipChart = new Chart(ctx, {
      type: 'doughnut',
      data: { labels, datasets: [{ data, backgroundColor: colors, borderWidth: 0 }] },
      options: { responsive: false, plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => {
          const t = ctx.dataset.data.reduce((a,b)=>a+b,0);
          return ctx.label+': '+ctx.raw+' ('+Math.round(ctx.raw/t*100)+'%)';
        }}}
      }}
    });
  }
  const total = data.reduce((a,b)=>a+b,0);
  let legend = '';
  for (let i=0; i<labels.length; i++) {
    const pct = total > 0 ? Math.round(data[i]/total*100) : 0;
    legend += `<div class="stat-row"><span class="stat-label"><span style="color:${colors[i]}">&#9679;</span> ${labels[i]}</span><span class="stat-value">${data[i]} (${pct}%)</span></div>`;
  }
  document.getElementById('skipLegend').innerHTML = legend;
}

// ═══════════════════════════════════════════════════════════════
// RENDER: CALENDAR
// ═══════════════════════════════════════════════════════════════
function renderCalendar(d) {
  const cal = d.calendar;
  if (!cal || cal.length < 1) { document.getElementById('calendar').innerHTML = '<div class="dim">No data</div>'; return; }
  let html = '<div class="cal-grid">';
  ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].forEach(d => html += `<div class="cal-header">${d}</div>`);
  const calData = {}; cal.forEach(c => calData[c.day] = c);
  const dates = cal.map(c => new Date(c.day+'T00:00:00'));
  const first = new Date(Math.min(...dates)), last = new Date(Math.max(...dates));
  const start = new Date(first); start.setDate(start.getDate() - ((start.getDay()+6)%7));
  const end = new Date(last); end.setDate(end.getDate() + (7-end.getDay())%7);
  for (let dt = new Date(start); dt <= end; dt.setDate(dt.getDate()+1)) {
    const key = dt.toISOString().split('T')[0];
    const c = calData[key];
    if (c) {
      const cls = c.daily_pnl >= 0 ? 'win' : 'loss';
      html += `<div class="cal-day ${cls}"><div>${dt.getDate()}</div><div class="cal-pnl ${pnlClass(c.daily_pnl)}">${c.daily_pnl>=0?'+':''}$${Math.abs(c.daily_pnl).toFixed(0)}</div><div class="cal-record">${c.wins}W/${c.losses}L</div></div>`;
    } else {
      html += `<div class="cal-day empty"><div class="dim">${dt.getDate()}</div></div>`;
    }
  }
  html += '</div>';
  document.getElementById('calendar').innerHTML = html;
}

// ═══════════════════════════════════════════════════════════════
// RENDER: TRADES TABLE
// ═══════════════════════════════════════════════════════════════
function badge(outcome, skipReason, signals) {
  if (outcome === 'skip') {
    if (skipReason === 'ml_gate') return '<span class="badge b-mlgate">ML GATE</span>';
    if (signals && signals.final_vote !== 'ABSTAIN') return '<span class="badge b-failed">FAILED</span>';
    return '<span class="badge b-skip">SKIP</span>';
  }
  const m = {win:'b-win',loss:'b-loss',pending:'b-pending'};
  return `<span class="badge ${m[outcome]||''}">${outcome.toUpperCase()}</span>`;
}

function voteHtml(v) {
  if (v==='Up') return '<span class="vote-up">UP</span>';
  if (v==='Down') return '<span class="vote-down">DOWN</span>';
  return '<span class="vote-abstain">ABS</span>';
}

function detailRow(label, value) {
  if (value == null || value === '') return '';
  return `<div class="detail-row"><span class="detail-label">${label}</span><span class="detail-value">${value}</span></div>`;
}

function buildDetail(t) {
  let h = '<div class="detail-inner"><div class="detail-grid">';
  h += '<div class="detail-section">Trade</div>';
  h += detailRow('Market', t.market_id);
  h += detailRow('Time', t.timestamp);
  h += detailRow('Side', t.side);
  h += detailRow('Entry Odds', t.entry_odds ? fmt(t.entry_odds,3) : null);
  h += detailRow('Cost', t.position_size ? '$'+fmt(t.position_size) : null);
  h += detailRow('Payout Rate', t.payout_rate ? (t.payout_rate*100).toFixed(1)+'%' : null);
  h += detailRow('R:R', t.risk_reward_ratio ? t.risk_reward_ratio.toFixed(2)+':1' : null);
  h += detailRow('Confidence', t.confidence_level ? t.confidence_level.toUpperCase() : null);
  h += detailRow('Market Outcome', t.market_outcome || null);
  if (t.skip_reason) {
    const reasonMap = {
      'no_consensus': 'No model consensus (need 2/3 agreement)',
      'ml_gate': 'ML gate blocked — P(win) below threshold',
      'empty_book': 'No liquidity on order book (FAK found no match)',
      'risk_blocked': 'Position size exceeded risk limits',
      'invalid_amount': 'Order amount too small or invalid',
      'insufficient_liquidity': 'Not enough liquidity to fill order',
      'price_out_of_range': 'Odds moved outside tradeable window (0.30-0.70)',
      'service_unavailable': 'API or signal fetch failed',
      'order_rejected': 'CLOB rejected the order',
    };
    const reason = reasonMap[t.skip_reason] || t.skip_reason;
    h += detailRow('Skip Reason', '<span style="color:var(--orange)">' + reason + '</span>');
  }
  if (t.outcome === 'loss' && t.market_outcome && t.side) {
    h += detailRow('Why Lost', '<span class="neg">Picked ' + t.side + ' but market went ' + t.market_outcome + '</span>');
  }
  const s = t.signals;
  if (s) {
    h += '<div class="detail-section">Price</div>';
    h += detailRow('Chainlink', s.chainlink_price ? '$'+fmt(s.chainlink_price) : null);
    h += detailRow('Spot', s.spot_price ? '$'+fmt(s.spot_price) : null);
    h += detailRow('Divergence', s.chainlink_spot_divergence!=null ? '$'+(s.chainlink_spot_divergence>=0?'+':'')+fmt(s.chainlink_spot_divergence) : null);
    h += detailRow('Candle Pos', s.candle_position_dollars!=null ? '$'+(s.candle_position_dollars>=0?'+':'')+fmt(s.candle_position_dollars) : null);
    h += detailRow('BTC Vol', s.btc_volatility!=null ? '$'+fmt(s.btc_volatility) : null);
    h += '<div class="detail-section">Momentum & Volume</div>';
    h += detailRow('Mom 60s', s.momentum_60s!=null ? '$'+(s.momentum_60s>=0?'+':'')+fmt(s.momentum_60s,4)+'/s' : null);
    h += detailRow('Mom 120s', s.momentum_120s!=null ? '$'+(s.momentum_120s>=0?'+':'')+fmt(s.momentum_120s,4)+'/s' : null);
    h += detailRow('CVD', s.cvd!=null ? (s.cvd>=0?'+':'')+s.cvd.toFixed(6)+' BTC' : null);
    h += detailRow('OB Ratio', s.order_book_ratio!=null ? fmt(s.order_book_ratio,3) : null);
    h += detailRow('OB Imb', s.ob_imbalance!=null ? fmt(s.ob_imbalance,3) : null);
    h += '<div class="detail-section">Structure</div>';
    h += detailRow('Liq Net', s.liquidation_signal!=null ? '$'+(s.liquidation_signal>=0?'+':'')+fmt(s.liquidation_signal,0) : null);
    h += detailRow('Round # Dist', s.round_number_distance!=null ? '$'+fmt(s.round_number_distance,0) : null);
    h += detailRow('Regime', s.time_regime);
    h += detailRow('Streak', s.candle_streak);
    h += detailRow('Poly Bias', s.poly_book_bias!=null ? fmt(s.poly_book_bias,3) : null);
    h += '<div class="detail-section">Fair Value</div>';
    h += detailRow('Fair Up', s.fair_up!=null ? fmt(s.fair_up,3) : null);
    h += detailRow('Fair Down', s.fair_down!=null ? fmt(s.fair_down,3) : null);
    h += detailRow('Z-Score', s.fair_z_score!=null ? fmt(s.fair_z_score,3) : null);
    h += detailRow('Edge Up', s.edge_up_bps!=null ? s.edge_up_bps.toFixed(0)+'bps' : null);
    h += detailRow('Edge Down', s.edge_down_bps!=null ? s.edge_down_bps.toFixed(0)+'bps' : null);
    h += '<div class="detail-section">Votes</div>';
    h += detailRow('Momentum', voteHtml(s.momentum_vote));
    h += detailRow('Reversion', voteHtml(s.reversion_vote));
    h += detailRow('Structure', voteHtml(s.structure_vote));
    h += detailRow('Final', voteHtml(s.final_vote));
    h += '<div class="detail-section">Execution</div>';
    h += detailRow('Fill Price', s.fill_price_per_share!=null ? '$'+fmt(s.fill_price_per_share,4) : null);
    h += detailRow('Slippage', s.fill_slippage_pct!=null ? fmt(s.fill_slippage_pct,1)+'%' : null);
    h += detailRow('ML P(win)', s.ml_win_prob!=null ? (s.ml_win_prob*100).toFixed(1)+'%' : null);
  }
  h += '</div></div>';
  return h;
}

function toggleDetail(row) {
  const id = row.dataset.id;
  const detail = document.querySelector('.trade-detail[data-id="'+id+'"]');
  row.classList.toggle('expanded');
  detail.classList.toggle('open');
}

function getFilteredTrades() {
  if (!DATA) return [];
  let trades = DATA.trades;
  const search = document.getElementById('search').value.toLowerCase();
  const side = document.getElementById('filter-side').value;
  const outcome = document.getElementById('filter-outcome').value;
  const conf = document.getElementById('filter-conf').value;

  return trades.filter(t => {
    if (hideSkips && t.confidence_level === 'skip') return false;
    if (search && !t.market_id.toLowerCase().includes(search)) return false;
    if (side && t.side !== side) return false;
    if (outcome && t.outcome !== outcome) return false;
    if (conf && t.confidence_level !== conf) return false;
    return true;
  });
}

function sortTrades(trades) {
  const dir = sortDir === 'asc' ? 1 : -1;
  return trades.sort((a, b) => {
    let va, vb;
    switch(sortCol) {
      case 'id': va=a.id; vb=b.id; break;
      case 'time': va=a.timestamp; vb=b.timestamp; break;
      case 'side': va=a.side||''; vb=b.side||''; break;
      case 'cost': va=a.position_size||0; vb=b.position_size||0; break;
      case 'rr': va=a.risk_reward_ratio||0; vb=b.risk_reward_ratio||0; break;
      case 'ml': va=(a.signals&&a.signals.ml_win_prob)||0; vb=(b.signals&&b.signals.ml_win_prob)||0; break;
      case 'result': va=a.outcome; vb=b.outcome; break;
      case 'pnl': va=a.pnl||0; vb=b.pnl||0; break;
      case 'balance': va=a.portfolio_balance_after||0; vb=b.portfolio_balance_after||0; break;
      default: va=a.id; vb=b.id;
    }
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return 0;
  });
}

function renderTrades() {
  if (!DATA) return;
  const wasExpanded = new Set();
  document.querySelectorAll('.trade-row.expanded').forEach(r => wasExpanded.add(r.dataset.id));

  let filtered = getFilteredTrades();
  filtered = sortTrades(filtered);
  const totalFiltered = filtered.length;
  const totalPages = Math.ceil(totalFiltered / PAGE_SIZE);
  if (currentPage >= totalPages) currentPage = Math.max(0, totalPages - 1);
  const pageItems = filtered.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

  let html = '';
  for (const t of pageItems) {
    const isSkip = t.confidence_level === 'skip';
    const pnl = t.pnl || 0;
    const pnlH = isSkip ? '<span class="dim">-</span>' : `<span class="${pnlClass(pnl)}">${pnl>=0?'+':''}$${fmt(Math.abs(pnl))}</span>`;
    const isOpen = wasExpanded.has(String(t.id));
    const rrStr = t.risk_reward_ratio ? t.risk_reward_ratio.toFixed(1)+':1' : '<span class="dim">-</span>';
    const mlProb = t.signals && t.signals.ml_win_prob != null ? (t.signals.ml_win_prob * 100).toFixed(0)+'%' : '<span class="dim">-</span>';
    const mlClass = t.signals && t.signals.ml_win_prob != null ? (t.signals.ml_win_prob >= 0.6 ? 'pos' : t.signals.ml_win_prob < 0.4 ? 'neg' : '') : '';
    let timeStr = '';
    if (t.timestamp) {
      try {
        const dt = new Date(t.timestamp);
        timeStr = dt.toLocaleTimeString('en-US', { hour12: false, timeZone: 'America/New_York' });
      } catch(e) { timeStr = t.timestamp.slice(11, 19); }
    }

    html += `<tr class="trade-row${isOpen?' expanded':''}" data-id="${t.id}" onclick="toggleDetail(this)">`
      +`<td>${t.id}</td><td>${timeStr}</td><td>${isSkip?'<span class="dim">-</span>':t.side}</td>`
      +`<td class="r">${t.position_size?'$'+fmt(t.position_size):'<span class="dim">-</span>'}</td>`
      +`<td class="r">${rrStr}</td><td class="r ${mlClass}">${mlProb}</td>`
      +`<td>${badge(t.outcome, t.skip_reason, t.signals)}</td><td class="r">${pnlH}</td>`
      +`<td class="r">${t.portfolio_balance_after?'$'+fmt(t.portfolio_balance_after):'<span class="dim">-</span>'}</td></tr>`;
    html += `<tr class="trade-detail${isOpen?' open':''}" data-id="${t.id}"><td colspan="9">${buildDetail(t)}</td></tr>`;
  }
  document.getElementById('tbody').innerHTML = html || '<tr><td colspan="9" class="dim" style="text-align:center">No trades</td></tr>';
  document.getElementById('trade-count').textContent = `${totalFiltered} trades`;

  // Pagination
  let pag = '';
  if (totalPages > 1) {
    pag += `<button onclick="currentPage=0;renderTrades()" ${currentPage===0?'disabled':''}>&#171;</button>`;
    pag += `<button onclick="currentPage--;renderTrades()" ${currentPage===0?'disabled':''}>&#8249;</button>`;
    pag += `<span>${currentPage+1} / ${totalPages}</span>`;
    pag += `<button onclick="currentPage++;renderTrades()" ${currentPage>=totalPages-1?'disabled':''}>&#8250;</button>`;
    pag += `<button onclick="currentPage=${totalPages-1};renderTrades()" ${currentPage>=totalPages-1?'disabled':''}>&#187;</button>`;
  }
  document.getElementById('pagination').innerHTML = pag;
}

// ═══════════════════════════════════════════════════════════════
// POLL
// ═══════════════════════════════════════════════════════════════
async function poll() {
  try {
    const r = await fetch('/api/state');
    DATA = await r.json();
    if (DATA.error) throw new Error(DATA.error);

    renderTicker(DATA);
    renderPortfolio(DATA);
    renderStreak(DATA);
    renderMLGate(DATA);
    renderEquity(DATA);
    renderDailyPnl(DATA);
    renderWinRate(DATA);
    renderProfitFactor(DATA);
    renderHeatmap(DATA);
    renderModelAccuracy(DATA);
    renderSlippage(DATA);
    renderEdge(DATA);
    renderVoteCombos(DATA);
    renderSkips(DATA);
    renderCalendar(DATA);
    renderTrades();

    document.getElementById('footer').textContent = 'Last updated: ' + new Date().toLocaleTimeString() + ' \u00B7 Auto-refreshes every 5s';
  } catch(e) {
    document.getElementById('footer').textContent = 'Error: ' + e.message;
  }
}

poll();
setInterval(poll, 5000);
</script>
</body></html>"""


async def handle_index(request):
    return web.Response(text=HTML, content_type="text/html")


async def handle_api(request):
    loop = asyncio.get_event_loop()
    conn = request.app["conn"]
    try:
        state = await loop.run_in_executor(None, partial(query_state, conn))
        return web.json_response(state)
    except Exception as e:
        try:
            request.app["conn"] = get_conn()
        except Exception:
            pass
        return web.json_response({"error": str(e)}, status=500)


async def main():
    conn = get_conn()
    app = web.Application()
    app["conn"] = conn
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_api)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", PORT)
    await site.start()
    print(f"Trading terminal running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        conn.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
