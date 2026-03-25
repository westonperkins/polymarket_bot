"""Backtest alternative strategies on historical trade data."""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from ml.features import build_features, FEATURE_COLS

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    strategy: str
    trades_taken: int
    trades_skipped: int
    win_rate: float
    total_pnl: float
    max_drawdown: float
    profit_factor: float  # gross wins / gross losses
    avg_pnl: float
    description: str


def run_all_backtests(decided: pd.DataFrame, model, feature_cols: list) -> list[BacktestResult]:
    """Run all backtest strategies and return results."""
    results = []

    # 1. Baseline — actual performance
    results.append(_baseline(decided))

    # 2. ML confidence filter at various thresholds
    df = build_features(decided.copy())
    X = df[feature_cols].values
    probs = model.predict_proba(X)[:, 1]
    df["model_prob"] = probs

    for threshold in [0.55, 0.60, 0.65, 0.70]:
        results.append(_ml_filter(df, threshold))

    # 3. Timing analysis
    results.extend(_timing_analysis(decided))

    # 4. Slippage filter
    results.extend(_slippage_filter(decided))

    # 5. Fair value edge filter
    results.extend(_edge_filter(df))

    # 6. Hour-of-day best hours
    results.append(_best_hours(decided))

    # 7. High confidence only
    results.append(_high_confidence_only(decided))

    return results


def _baseline(df: pd.DataFrame) -> BacktestResult:
    """Actual bot performance."""
    wins = df[df["outcome"] == "win"]["pnl"].sum()
    losses = abs(df[df["outcome"] == "loss"]["pnl"].sum())
    pnl = df["pnl"].sum()
    n = len(df)
    wr = (df["outcome"] == "win").mean() if n > 0 else 0
    dd = _max_drawdown(df["pnl"].values)
    pf = wins / losses if losses > 0 else float("inf")

    return BacktestResult(
        strategy="Baseline (actual)",
        trades_taken=n, trades_skipped=0,
        win_rate=round(wr, 3), total_pnl=round(pnl, 2),
        max_drawdown=round(dd, 2), profit_factor=round(pf, 2),
        avg_pnl=round(pnl / n, 2) if n > 0 else 0,
        description="Actual bot performance",
    )


def _ml_filter(df: pd.DataFrame, threshold: float) -> BacktestResult:
    """Only take trades where model confidence > threshold."""
    taken = df[df["model_prob"] >= threshold]
    skipped = df[df["model_prob"] < threshold]
    return _compute_result(
        taken, len(skipped),
        f"ML filter (>{threshold:.0%})",
        f"Only take trades with model P(win) >= {threshold}",
    )


def _timing_analysis(df: pd.DataFrame) -> list[BacktestResult]:
    """Win rate by seconds_before_close buckets."""
    if "seconds_before_close" not in df.columns:
        return []

    results = []
    buckets = [(0, 20), (20, 35), (35, 50), (50, 70), (70, 120)]
    for lo, hi in buckets:
        subset = df[(df["seconds_before_close"] >= lo) & (df["seconds_before_close"] < hi)]
        if len(subset) >= 5:
            results.append(_compute_result(
                subset, len(df) - len(subset),
                f"Entry T-{lo}s to T-{hi}s",
                f"Trades entered {lo}-{hi} seconds before close",
            ))
    return results


def _slippage_filter(df: pd.DataFrame) -> list[BacktestResult]:
    """Filter by fill slippage percentage."""
    if "fill_slippage_pct" not in df.columns:
        return []

    results = []
    for max_slip in [10, 20, 30, 50]:
        subset = df[df["fill_slippage_pct"] <= max_slip]
        if len(subset) >= 5:
            results.append(_compute_result(
                subset, len(df) - len(subset),
                f"Slippage <={max_slip}%",
                f"Only trades with slippage <= {max_slip}%",
            ))
    return results


def _edge_filter(df: pd.DataFrame) -> list[BacktestResult]:
    """Filter by fair value edge."""
    if "edge_chosen" not in df.columns:
        return []

    results = []
    for min_edge in [50, 100, 200, 500]:
        subset = df[df["edge_chosen"].abs() >= min_edge]
        if len(subset) >= 5:
            results.append(_compute_result(
                subset, len(df) - len(subset),
                f"Edge >={min_edge}bps",
                f"Only trades with fair value edge >= {min_edge} basis points",
            ))
    return results


def _best_hours(df: pd.DataFrame) -> BacktestResult:
    """Trade only during the top 3 hours by win rate."""
    if "hour_of_day" not in df.columns or len(df) < 20:
        return BacktestResult(
            strategy="Best hours", trades_taken=0, trades_skipped=0,
            win_rate=0, total_pnl=0, max_drawdown=0, profit_factor=0,
            avg_pnl=0, description="Not enough data",
        )

    hourly = df.groupby("hour_of_day").apply(
        lambda g: (g["outcome"] == "win").mean()
    ).sort_values(ascending=False)
    top_hours = hourly.head(3).index.tolist()

    subset = df[df["hour_of_day"].isin(top_hours)]
    return _compute_result(
        subset, len(df) - len(subset),
        f"Best hours ({top_hours})",
        f"Only trade during UTC hours {top_hours}",
    )


def _high_confidence_only(df: pd.DataFrame) -> BacktestResult:
    """Only 3/3 consensus trades."""
    if "confidence_level" not in df.columns:
        return BacktestResult(
            strategy="High conf only", trades_taken=0, trades_skipped=0,
            win_rate=0, total_pnl=0, max_drawdown=0, profit_factor=0,
            avg_pnl=0, description="No confidence data",
        )

    subset = df[df["confidence_level"] == "high"]
    return _compute_result(
        subset, len(df) - len(subset),
        "High confidence only (3/3)",
        "Only take trades with unanimous 3/3 model consensus",
    )


def _compute_result(
    taken: pd.DataFrame, n_skipped: int, name: str, desc: str
) -> BacktestResult:
    """Compute backtest metrics for a subset of trades."""
    n = len(taken)
    if n == 0:
        return BacktestResult(
            strategy=name, trades_taken=0, trades_skipped=n_skipped,
            win_rate=0, total_pnl=0, max_drawdown=0, profit_factor=0,
            avg_pnl=0, description=desc,
        )

    wr = (taken["outcome"] == "win").mean()
    pnl = taken["pnl"].sum()
    wins = taken[taken["pnl"] > 0]["pnl"].sum()
    losses = abs(taken[taken["pnl"] < 0]["pnl"].sum())
    dd = _max_drawdown(taken["pnl"].values)
    pf = wins / losses if losses > 0 else float("inf")

    return BacktestResult(
        strategy=name, trades_taken=n, trades_skipped=n_skipped,
        win_rate=round(wr, 3), total_pnl=round(pnl, 2),
        max_drawdown=round(dd, 2), profit_factor=round(pf, 2),
        avg_pnl=round(pnl / n, 2) if n > 0 else 0,
        description=desc,
    )


def _max_drawdown(pnl_series: np.ndarray) -> float:
    """Compute maximum drawdown from a PnL series."""
    if len(pnl_series) == 0:
        return 0.0
    cumulative = np.cumsum(pnl_series)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = running_max - cumulative
    return float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
