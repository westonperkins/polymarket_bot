"""Train XGBoost model on historical trade data.

Usage: python3 ml/train.py [--mode live|paper] [--min-trades 100]
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    log_loss, brier_score_loss,
)

try:
    import xgboost as xgb
except ImportError:
    print("Install xgboost: pip install xgboost")
    sys.exit(1)

from ml.data import pull_raw_data, clean_data, get_data_summary
from ml.features import build_features, FEATURE_COLS, GATE_FEATURE_COLS
from ml.backtest import run_all_backtests
from ml.report import generate_report

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


def train_model(decided: pd.DataFrame, feature_cols: list = None) -> tuple:
    """Train XGBoost with time-series cross-validation.

    Returns (model, cv_results, feature_importances).
    """
    if feature_cols is None:
        feature_cols = FEATURE_COLS
    df = build_features(decided)
    X = df[feature_cols].values
    y = df["y"].values
    feature_names = feature_cols

    # Class balance
    n_pos = y.sum()
    n_neg = len(y) - n_pos
    scale_pos_weight = n_neg / n_pos if n_pos > 0 else 1.0

    logger.info(f"Training on {len(y)} trades ({n_pos} wins, {n_neg} losses)")
    logger.info(f"Features: {len(feature_names)}")

    # Cross-validation with time-series split
    n_splits = min(5, max(2, len(y) // 50))
    tscv = TimeSeriesSplit(n_splits=n_splits)
    cv_results = []

    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]

        model = xgb.XGBClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            min_child_weight=10,
            scale_pos_weight=scale_pos_weight,
            eval_metric="logloss",
            use_label_encoder=False,
            verbosity=0,
        )
        model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

        y_pred = model.predict(X_test)
        y_prob = model.predict_proba(X_test)[:, 1]

        fold_result = {
            "fold": fold + 1,
            "train_size": len(y_train),
            "test_size": len(y_test),
            "accuracy": round(accuracy_score(y_test, y_pred), 4),
            "precision": round(precision_score(y_test, y_pred, zero_division=0), 4),
            "recall": round(recall_score(y_test, y_pred, zero_division=0), 4),
            "f1": round(f1_score(y_test, y_pred, zero_division=0), 4),
            "log_loss": round(log_loss(y_test, y_prob), 4),
            "brier": round(brier_score_loss(y_test, y_prob), 4),
        }
        cv_results.append(fold_result)
        logger.info(f"  Fold {fold+1}: acc={fold_result['accuracy']} f1={fold_result['f1']}")

    # Train final model on all data
    final_model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        min_child_weight=10,
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss",
        use_label_encoder=False,
        verbosity=0,
    )
    final_model.fit(X, y)

    # Feature importance
    importances = dict(zip(feature_names, final_model.feature_importances_))
    importances = dict(sorted(importances.items(), key=lambda x: x[1], reverse=True))

    return final_model, cv_results, importances


def analyze_skips(model, skipped: pd.DataFrame) -> list[dict]:
    """Analyze skipped trades: would they have won?"""
    if len(skipped) == 0 or "market_outcome" not in skipped.columns:
        return []

    has_outcome = skipped[skipped["market_outcome"].notna()].copy()
    if len(has_outcome) == 0:
        return []

    df = build_features(has_outcome)
    X = df[FEATURE_COLS].values
    probabilities = model.predict_proba(X)[:, 1]

    results = []
    for i, (_, row) in enumerate(has_outcome.iterrows()):
        final_vote = row.get("final_vote", "ABSTAIN")
        market_won = row.get("market_outcome", "")
        skip_reason = row.get("skip_reason", "unknown")

        # Would we have won if we bet the ensemble's direction?
        if final_vote in ("Up", "Down"):
            would_have_won = final_vote == market_won
        else:
            would_have_won = None  # Can't determine for ABSTAIN

        results.append({
            "trade_id": row.get("trade_id"),
            "skip_reason": skip_reason,
            "final_vote": final_vote,
            "market_outcome": market_won,
            "would_have_won": would_have_won,
            "model_win_prob": round(float(probabilities[i]), 3),
            "up_odds": row.get("up_odds"),
            "hour_of_day": row.get("hour_of_day"),
        })

    return results


def main():
    parser = argparse.ArgumentParser(description="Train ML model on trade data")
    parser.add_argument("--mode", default="live", help="Trading mode (live or paper)")
    parser.add_argument("--min-trades", type=int, default=50, help="Minimum trades to train")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("POLYMARKET ML TRAINING PIPELINE")
    logger.info("=" * 60)

    # Pull data
    logger.info("\n1. Pulling data from Supabase...")
    raw = pull_raw_data(mode=args.mode)
    decided, skipped, pending = clean_data(raw)
    summary = get_data_summary(decided, skipped)

    logger.info(f"   Decided: {summary['total_decided']} ({summary['wins']}W / {summary['losses']}L)")
    logger.info(f"   Skipped: {summary['total_skipped']}")
    logger.info(f"   Win rate: {summary['win_rate']:.1%}")
    logger.info(f"   Date range: {summary['date_range']}")

    if summary["total_decided"] < args.min_trades:
        logger.warning(
            f"\n   Not enough data to train ({summary['total_decided']} < {args.min_trades})."
            f"\n   Keep the bot running to accumulate more trades."
            f"\n   Run again when you have {args.min_trades}+ resolved trades."
        )
        # Still save summary
        with open(OUTPUT_DIR / "summary.json", "w") as f:
            json.dump(summary, f, indent=2, default=str)
        return

    # Train full model (all features including execution)
    logger.info("\n2. Training full XGBoost model...")
    model, cv_results, importances = train_model(decided)
    model.save_model(str(OUTPUT_DIR / "model.json"))
    logger.info(f"   Full model saved to {OUTPUT_DIR / 'model.json'}")

    # Train gate model (pre-trade features only — no execution data)
    logger.info("\n2b. Training gate model (pre-trade features only)...")
    gate_model, gate_cv, gate_imp = train_model(decided, feature_cols=GATE_FEATURE_COLS)
    gate_model.save_model(str(OUTPUT_DIR / "gate_model.json"))
    logger.info(f"   Gate model saved to {OUTPUT_DIR / 'gate_model.json'}")

    avg_gate_acc = sum(r["accuracy"] for r in gate_cv) / len(gate_cv)
    logger.info(f"   Gate model accuracy: {avg_gate_acc:.1%} (baseline: {summary['win_rate']:.1%})")

    # Save feature importance
    imp_df = pd.DataFrame(
        [{"feature": k, "importance": v} for k, v in importances.items()]
    )
    imp_df.to_csv(OUTPUT_DIR / "feature_importance.csv", index=False)

    gate_imp_df = pd.DataFrame(
        [{"feature": k, "importance": v} for k, v in gate_imp.items()]
    )
    gate_imp_df.to_csv(OUTPUT_DIR / "gate_feature_importance.csv", index=False)

    # Save CV results
    cv_df = pd.DataFrame(cv_results)
    cv_df.to_csv(OUTPUT_DIR / "cv_results.csv", index=False)

    # Analyze skips
    logger.info("\n3. Analyzing skipped trades...")
    skip_analysis = analyze_skips(model, skipped)
    if skip_analysis:
        skip_df = pd.DataFrame(skip_analysis)
        skip_df.to_csv(OUTPUT_DIR / "skip_analysis.csv", index=False)
        logger.info(f"   Analyzed {len(skip_analysis)} skips with known outcomes")
    else:
        skip_df = pd.DataFrame()
        logger.info("   No skips with market_outcome data yet")

    # Run backtests
    logger.info("\n4. Running backtests...")
    backtest_results = run_all_backtests(decided, model, FEATURE_COLS)

    # Generate report
    logger.info("\n5. Generating report...")
    report = generate_report(summary, cv_results, importances, skip_analysis, backtest_results)

    with open(OUTPUT_DIR / "report.txt", "w") as f:
        f.write(report)

    logger.info(f"\n{'=' * 60}")
    logger.info("REPORT:")
    logger.info(f"{'=' * 60}")
    print(report)

    logger.info(f"\nAll outputs saved to {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
