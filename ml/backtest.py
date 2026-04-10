"""Backtest alternative strategies on historical trade data."""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from ml.features import build_features, FEATURE_COLS, GATE_FEATURE_COLS

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
    avg_winner: float  # mean PnL of winning trades
    avg_loser: float   # mean PnL of losing trades (negative)
    avg_rr: float      # avg_winner / |avg_loser| — reward-to-risk ratio
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
    win_rows = df[df["pnl"] > 0]["pnl"]
    loss_rows = df[df["pnl"] < 0]["pnl"]
    wins_sum = win_rows.sum()
    losses_sum = abs(loss_rows.sum())
    pnl = df["pnl"].sum()
    n = len(df)
    wr = (df["outcome"] == "win").mean() if n > 0 else 0
    dd = _max_drawdown(df["pnl"].values)
    pf = wins_sum / losses_sum if losses_sum > 0 else float("inf")
    avg_w = float(win_rows.mean()) if len(win_rows) > 0 else 0.0
    avg_l = float(loss_rows.mean()) if len(loss_rows) > 0 else 0.0
    rr = (avg_w / abs(avg_l)) if avg_l < 0 else float("inf")

    return BacktestResult(
        strategy="Baseline (actual)",
        trades_taken=n, trades_skipped=0,
        win_rate=round(wr, 3), total_pnl=round(pnl, 2),
        max_drawdown=round(dd, 2), profit_factor=round(pf, 2),
        avg_pnl=round(pnl / n, 2) if n > 0 else 0,
        avg_winner=round(avg_w, 2),
        avg_loser=round(avg_l, 2),
        avg_rr=round(rr, 2) if rr != float("inf") else float("inf"),
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
            avg_pnl=0, avg_winner=0, avg_loser=0, avg_rr=0,
            description=desc,
        )

    wr = (taken["outcome"] == "win").mean()
    pnl = taken["pnl"].sum()
    win_rows = taken[taken["pnl"] > 0]["pnl"]
    loss_rows = taken[taken["pnl"] < 0]["pnl"]
    wins_sum = win_rows.sum()
    losses_sum = abs(loss_rows.sum())
    dd = _max_drawdown(taken["pnl"].values)
    pf = wins_sum / losses_sum if losses_sum > 0 else float("inf")
    avg_w = float(win_rows.mean()) if len(win_rows) > 0 else 0.0
    avg_l = float(loss_rows.mean()) if len(loss_rows) > 0 else 0.0
    rr = (avg_w / abs(avg_l)) if avg_l < 0 else float("inf")

    return BacktestResult(
        strategy=name, trades_taken=n, trades_skipped=n_skipped,
        win_rate=round(wr, 3), total_pnl=round(pnl, 2),
        max_drawdown=round(dd, 2), profit_factor=round(pf, 2),
        avg_pnl=round(pnl / n, 2) if n > 0 else 0,
        avg_winner=round(avg_w, 2),
        avg_loser=round(avg_l, 2),
        avg_rr=round(rr, 2) if rr != float("inf") else float("inf"),
        description=desc,
    )


def _dedupe_columns(d: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicate column labels left over from the trades+signals SQL join."""
    return d.loc[:, ~d.columns.duplicated()].copy()


def _build_directional_set(
    decided: pd.DataFrame, skipped: pd.DataFrame
) -> pd.DataFrame:
    """Combine decided + skipped into the set of cycles we can simulate.

    A cycle is included only if the bot or ensemble had a direction (Up/Down)
    and market_outcome is known. Adds a `chosen_side` column.
    """
    frames = []
    if len(decided) > 0:
        d = _dedupe_columns(decided)
        d["chosen_side"] = d["side"]
        frames.append(d)
    if len(skipped) > 0:
        s = _dedupe_columns(skipped)
        s["chosen_side"] = s["final_vote"] if "final_vote" in s.columns else None
        frames.append(s)
    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined = combined[combined["chosen_side"].isin(["Up", "Down"])]
    if "market_outcome" not in combined.columns:
        return pd.DataFrame()
    combined = combined[combined["market_outcome"].notna()]
    combined = combined[combined["market_outcome"].isin(["Up", "Down"])]
    return combined


def _simulate_taker_core(
    combined: pd.DataFrame,
    model,
    feature_cols: list,
    half_spread_cents: float,
    label_prefix: str,
) -> list[BacktestResult]:
    """Score the combined opportunity set with `model`, simulate taker fills,
    and return BacktestResult rows for each ML threshold bucket.
    """
    if len(combined) == 0:
        return []

    half_spread = half_spread_cents / 100.0

    df = build_features(combined)
    X = df[feature_cols].values
    df["sim_model_prob"] = model.predict_proba(X)[:, 1]

    up_odds = pd.to_numeric(df.get("up_odds"), errors="coerce")
    down_odds = pd.to_numeric(df.get("down_odds"), errors="coerce")
    is_up = df["chosen_side"] == "Up"
    mid = np.where(is_up, up_odds, down_odds)
    fill = np.clip(mid + half_spread, 0.01, 0.99)
    won = (df["market_outcome"] == df["chosen_side"]).values
    sim_pnl = np.where(won, (1.0 - fill) / fill, -1.0)

    df["sim_fill"] = fill
    df["pnl"] = sim_pnl
    df["outcome"] = np.where(won, "win", "loss")
    df = df[pd.notna(mid)]
    if len(df) == 0:
        return []

    results = []
    label_suffix = f"({half_spread_cents:.1f}c spread)"

    results.append(_compute_result(
        df, 0,
        f"{label_prefix} all dir {label_suffix}",
        f"{label_prefix}: taker fill on every directional cycle",
    ))

    for threshold in [0.55, 0.60, 0.65, 0.70]:
        subset = df[df["sim_model_prob"] >= threshold]
        n_skipped = len(df) - len(subset)
        results.append(_compute_result(
            subset, n_skipped,
            f"{label_prefix} ML>{int(threshold*100)}% {label_suffix}",
            f"{label_prefix}: taker fill gated by P(win) >= {threshold}",
        ))

    return results


def simulate_taker_execution(
    decided: pd.DataFrame,
    skipped: pd.DataFrame,
    gate_model,
    half_spread_cents: float = 1.0,
) -> list[BacktestResult]:
    """In-sample taker counterfactual: scores every directional cycle with the
    full gate model (trained on all decided trades) and reports per-threshold
    PnL. Generous baseline — see simulate_taker_holdout for the held-out version.
    """
    combined = _build_directional_set(decided, skipped)
    return _simulate_taker_core(
        combined, gate_model, GATE_FEATURE_COLS, half_spread_cents, "Taker"
    )


def simulate_taker_holdout(
    decided: pd.DataFrame,
    skipped: pd.DataFrame,
    half_spread_cents: float = 1.0,
    train_frac: float = 0.7,
) -> tuple[list[BacktestResult], dict]:
    """Out-of-sample taker counterfactual.

    Splits decided trades temporally at `train_frac`, trains a fresh gate model
    on the train slice only, and runs the taker simulation on cycles in the
    holdout window (test decided + skipped after the split timestamp).

    This is the credibility test for simulate_taker_execution: if the holdout
    PnL collapses, the in-sample numbers were leaking. If it holds, the model
    has real signal and step 2 is justified at the threshold the holdout
    confirms.

    Returns (results, meta) where meta has split timestamp, sample sizes, etc.
    """
    import xgboost as xgb

    if "timestamp" not in decided.columns or len(decided) < 50:
        return [], {"error": "not enough decided trades for a temporal holdout"}

    d = _dedupe_columns(decided).sort_values("timestamp").reset_index(drop=True)
    split_idx = int(len(d) * train_frac)
    train_decided = d.iloc[:split_idx].copy()
    test_decided = d.iloc[split_idx:].copy()
    split_ts = d.iloc[split_idx]["timestamp"]

    if len(train_decided) < 30 or len(test_decided) < 10:
        return [], {"error": "split produced too few train/test rows"}

    # Train fresh gate model on train slice only
    train_features = build_features(train_decided)
    X_train = train_features[GATE_FEATURE_COLS].values
    y_train = train_features["y"].values

    n_pos = int(y_train.sum())
    n_neg = len(y_train) - n_pos
    spw = (n_neg / n_pos) if n_pos > 0 else 1.0

    holdout_model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        min_child_weight=10,
        scale_pos_weight=spw,
        eval_metric="logloss",
        use_label_encoder=False,
        verbosity=0,
    )
    holdout_model.fit(X_train, y_train)

    # Holdout opportunity set: test decided + skipped after the split
    if len(skipped) > 0 and "timestamp" in skipped.columns:
        s = _dedupe_columns(skipped)
        holdout_skipped = s[s["timestamp"] >= split_ts]
    else:
        holdout_skipped = skipped

    holdout_set = _build_directional_set(test_decided, holdout_skipped)

    label = f"Taker HOLDOUT(train={int(train_frac*100)}%)"
    results = _simulate_taker_core(
        holdout_set, holdout_model, GATE_FEATURE_COLS, half_spread_cents, label
    )

    meta = {
        "train_decided": len(train_decided),
        "test_decided": len(test_decided),
        "holdout_skipped": int(len(holdout_skipped)) if len(holdout_skipped) > 0 else 0,
        "holdout_directional": len(holdout_set),
        "split_timestamp": str(split_ts),
        "train_frac": train_frac,
    }
    return results, meta


def _max_drawdown(pnl_series: np.ndarray) -> float:
    """Compute maximum drawdown from a PnL series."""
    if len(pnl_series) == 0:
        return 0.0
    cumulative = np.cumsum(pnl_series)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = running_max - cumulative
    return float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
