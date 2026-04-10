"""In-memory safety monitor for the taker execution path (step 2 validation).

Tracks recent taker trade outcomes and auto-halts the path if the rolling win
rate or per-trade PnL drops below the floors configured in config.py. State is
process-local — a bot restart resets the monitor.

Usage:
    register_trade(trade_id)        # right after placing a taker order
    record_resolution(trade_id, pnl, won)  # from the resolve hook
    halted, reason = is_halted()    # check before placing the next taker order
"""

import logging
from collections import deque
from typing import Optional

import config

logger = logging.getLogger(__name__)

_taker_trade_ids: set[int] = set()
_recent: deque = deque(maxlen=200)
_halted: bool = False
_halt_reason: Optional[str] = None


def register_trade(trade_id: int) -> None:
    """Mark a freshly-placed trade as having come from the taker path."""
    if trade_id is not None:
        _taker_trade_ids.add(int(trade_id))


def is_taker_trade(trade_id: int) -> bool:
    return int(trade_id) in _taker_trade_ids


def record_resolution(trade_id: int, pnl: float, won: bool) -> None:
    """Called from the resolve hook when a taker trade settles."""
    tid = int(trade_id)
    if tid not in _taker_trade_ids:
        return
    _recent.append({"trade_id": tid, "pnl": float(pnl), "won": bool(won)})
    _taker_trade_ids.discard(tid)
    _check_halt()


def _check_halt() -> None:
    global _halted, _halt_reason
    if _halted:
        return
    n_required = config.TAKER_MODE_HALT_AFTER_N
    if len(_recent) < n_required:
        return

    window = list(_recent)[-n_required:]
    win_rate = sum(1 for r in window if r["won"]) / len(window)
    avg_pnl = sum(r["pnl"] for r in window) / len(window)

    if win_rate < config.TAKER_MODE_MIN_WIN_RATE:
        _halted = True
        _halt_reason = (
            f"win rate {win_rate:.1%} below floor "
            f"{config.TAKER_MODE_MIN_WIN_RATE:.1%} over last {len(window)} taker trades"
        )
    elif avg_pnl < config.TAKER_MODE_MIN_AVG_PNL:
        _halted = True
        _halt_reason = (
            f"avg PnL ${avg_pnl:.2f} below floor "
            f"${config.TAKER_MODE_MIN_AVG_PNL:.2f} over last {len(window)} taker trades"
        )

    if _halted:
        logger.warning(
            f"🛑 TAKER MODE HALTED: {_halt_reason}. "
            f"Future ML-gate trades will fall back to the limit path until the bot "
            f"is restarted or taker_monitor.reset() is called."
        )


def is_halted() -> tuple[bool, Optional[str]]:
    return _halted, _halt_reason


def stats() -> dict:
    """For dashboards and logging."""
    if not _recent:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "halted": _halted,
            "halt_reason": _halt_reason,
            "open": len(_taker_trade_ids),
        }
    n = len(_recent)
    wr = sum(1 for r in _recent if r["won"]) / n
    avg = sum(r["pnl"] for r in _recent) / n
    return {
        "trades": n,
        "win_rate": round(wr, 3),
        "avg_pnl": round(avg, 3),
        "halted": _halted,
        "halt_reason": _halt_reason,
        "open": len(_taker_trade_ids),
    }


def reset() -> None:
    """Clear all monitor state. Use after investigating a halt."""
    global _halted, _halt_reason
    _halted = False
    _halt_reason = None
    _recent.clear()
    _taker_trade_ids.clear()
