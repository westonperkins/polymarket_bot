"""Feature engineering for ML model training."""

import numpy as np
import pandas as pd


# Features the model will use — must all be numeric
FEATURE_COLS = [
    # Market odds
    "up_odds", "down_odds", "odds_spread", "entry_odds",
    # Timing
    "seconds_before_close", "hour_of_day", "day_of_week",
    "is_us_market_hours", "is_asian_hours", "is_weekend",
    # Price context
    "btc_open_price", "btc_high", "btc_low", "btc_entry_price",
    "btc_volatility", "btc_range", "btc_position_in_range",
    # Momentum
    "momentum_60s", "momentum_120s", "momentum_acceleration",
    # CVD
    "cvd", "cvd_buy_volume", "cvd_sell_volume", "cvd_trade_count",
    "cvd_imbalance", "cvd_ratio",
    # Binance order book
    "order_book_ratio", "ob_bid_volume", "ob_ask_volume", "ob_imbalance",
    # Liquidations
    "liquidation_signal", "liq_long_usd", "liq_short_usd",
    "liq_imbalance", "liq_ratio",
    # Polymarket book
    "poly_book_up_bids", "poly_book_up_asks",
    "poly_book_down_bids", "poly_book_down_asks",
    "poly_book_bias", "poly_book_imbalance", "poly_spread",
    # Divergence
    "chainlink_spot_divergence", "candle_position_dollars",
    "round_number_distance",
    # Fair value model
    "fair_up", "fair_down", "fair_z_score",
    "edge_up_bps", "edge_down_bps", "edge_chosen",
    # Model votes (encoded numeric)
    "momentum_vote_num", "reversion_vote_num", "structure_vote_num",
    "confidence_num", "side_num",
    # Streak
    "streak_length", "streak_is_up", "prev_candle_up",
    # Execution (live only)
    "fill_price_per_share", "fill_slippage_pct", "risk_reward_ratio",
    # Missing data indicators
    "has_cvd", "has_orderbook", "has_liquidations", "has_poly_book", "has_fair_value",
]

# Pre-trade features only — used by the ML gate (no execution data available yet)
GATE_FEATURE_COLS = [c for c in FEATURE_COLS if c not in [
    "fill_price_per_share", "fill_slippage_pct", "risk_reward_ratio",
]]


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Engineer features from raw signal data. Returns DataFrame with FEATURE_COLS."""
    df = df.copy()

    # Derived features
    df["odds_spread"] = df.get("up_odds", 0.5) - df.get("down_odds", 0.5)

    df["cvd_imbalance"] = df.get("cvd_buy_volume", 0) - df.get("cvd_sell_volume", 0)
    cvd_total = df.get("cvd_buy_volume", 0) + df.get("cvd_sell_volume", 0)
    df["cvd_ratio"] = np.where(cvd_total > 0, df.get("cvd_buy_volume", 0) / cvd_total, 0.5)

    df["ob_imbalance"] = df.get("ob_bid_volume", 0) - df.get("ob_ask_volume", 0)

    df["liq_imbalance"] = df.get("liq_long_usd", 0) - df.get("liq_short_usd", 0)
    liq_total = df.get("liq_long_usd", 0) + df.get("liq_short_usd", 0)
    df["liq_ratio"] = np.where(liq_total > 0, df.get("liq_long_usd", 0) / liq_total, 0.5)

    df["poly_book_imbalance"] = (
        (df.get("poly_book_up_bids", 0) - df.get("poly_book_up_asks", 0))
        - (df.get("poly_book_down_bids", 0) - df.get("poly_book_down_asks", 0))
    )

    df["btc_range"] = df.get("btc_high", 0) - df.get("btc_low", 0)
    btc_range_safe = df["btc_range"].replace(0, np.nan)
    df["btc_position_in_range"] = (
        (df.get("btc_entry_price", 0) - df.get("btc_low", 0)) / btc_range_safe
    ).fillna(0.5)

    df["momentum_acceleration"] = df.get("momentum_60s", 0) - df.get("momentum_120s", 0)

    # Edge on the chosen side
    df["edge_chosen"] = np.where(
        df.get("side_num", 0) == 1,
        df.get("edge_up_bps", 0),
        df.get("edge_down_bps", 0),
    )

    # Time regime flags
    # UTC hours: US market 13:30-20:00, Asian 02:00-10:00
    df["is_us_market_hours"] = df.get("hour_of_day", 12).apply(
        lambda h: 1 if 13 <= h <= 20 else 0
    )
    df["is_asian_hours"] = df.get("hour_of_day", 12).apply(
        lambda h: 1 if 2 <= h <= 10 else 0
    )
    df["is_weekend"] = df.get("day_of_week", 0).apply(
        lambda d: 1 if d >= 5 else 0
    )

    # Missing data indicators (model can learn that missing data is informative)
    df["has_cvd"] = df.get("cvd", np.nan).notna().astype(int)
    df["has_orderbook"] = df.get("order_book_ratio", np.nan).notna().astype(int)
    df["has_liquidations"] = df.get("liquidation_signal", np.nan).notna().astype(int)
    df["has_poly_book"] = df.get("poly_book_bias", np.nan).notna().astype(int)
    df["has_fair_value"] = df.get("fair_up", np.nan).notna().astype(int)

    # Fill NaN with 0 for numeric features
    available_cols = [c for c in FEATURE_COLS if c in df.columns]
    missing_cols = [c for c in FEATURE_COLS if c not in df.columns]
    if missing_cols:
        for col in missing_cols:
            df[col] = 0

    df[FEATURE_COLS] = df[FEATURE_COLS].fillna(0)

    return df


def build_features_from_signal_data(
    signal_data: dict,
    side: str,
    confidence: str,
    entry_odds: float,
) -> np.ndarray:
    """Build a feature vector from live signal_data for the ML gate.

    Converts raw signal_data dict (as built in main.py on_signal_window)
    into the same feature format the model was trained on.

    Returns numpy array of shape (1, len(GATE_FEATURE_COLS)).
    """
    # Start with raw signal values
    row = dict(signal_data)

    # Encode votes as numeric (same as ml/data.py clean_data)
    vote_map = {"Up": 1, "Down": -1, "ABSTAIN": 0}
    row["momentum_vote_num"] = vote_map.get(row.get("momentum_vote", "ABSTAIN"), 0)
    row["reversion_vote_num"] = vote_map.get(row.get("reversion_vote", "ABSTAIN"), 0)
    row["structure_vote_num"] = vote_map.get(row.get("structure_vote", "ABSTAIN"), 0)
    row["final_vote_num"] = vote_map.get(row.get("final_vote", "ABSTAIN"), 0)

    # Encode side and confidence
    row["side_num"] = 1 if side == "Up" else 0
    conf_map = {"high": 2, "medium": 1, "skip": 0}
    row["confidence_num"] = conf_map.get(confidence, 0)

    # Entry odds
    row["entry_odds"] = entry_odds

    # Parse streak
    streak = row.get("candle_streak", "none")
    if isinstance(streak, str) and streak != "none" and "x" in streak:
        try:
            row["streak_length"] = int(streak.split("x")[0])
        except (ValueError, IndexError):
            row["streak_length"] = 0
        row["streak_is_up"] = 1 if "Up" in streak else 0
    else:
        row["streak_length"] = 0
        row["streak_is_up"] = 0

    # Previous candle outcome
    row["prev_candle_up"] = 1 if row.get("prev_candle_outcome") == "Up" else 0

    # Execution features not available pre-trade
    row["fill_price_per_share"] = 0
    row["fill_slippage_pct"] = 0
    row["risk_reward_ratio"] = 0

    # Build DataFrame and apply feature engineering
    df = pd.DataFrame([row])
    df = build_features(df)

    # Return gate features only
    return df[GATE_FEATURE_COLS].values
