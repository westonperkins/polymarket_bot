"""Generate human-readable insights from ML training results."""

from collections import Counter


def generate_report(
    summary: dict,
    cv_results: list[dict],
    importances: dict,
    skip_analysis: list[dict],
    backtest_results: list,
) -> str:
    """Generate a full report as a string."""
    lines = []
    lines.append("=" * 70)
    lines.append("POLYMARKET BOT — ML ANALYSIS REPORT")
    lines.append("=" * 70)

    # 1. Data Overview
    lines.append("\n--- DATA OVERVIEW ---")
    lines.append(f"Total resolved trades: {summary['total_decided']}")
    lines.append(f"Total skipped cycles: {summary['total_skipped']}")
    lines.append(f"Wins: {summary['wins']} | Losses: {summary['losses']}")
    lines.append(f"Win rate: {summary['win_rate']:.1%}")
    lines.append(f"Date range: {summary['date_range']}")

    if summary.get("null_pct"):
        high_nulls = {k: v for k, v in summary["null_pct"].items() if v > 20}
        if high_nulls:
            lines.append(f"\nSignals with >20% missing data:")
            for col, pct in sorted(high_nulls.items(), key=lambda x: x[1], reverse=True)[:10]:
                lines.append(f"  {col}: {pct}% null")

    # 2. Model Performance
    if cv_results:
        lines.append("\n--- MODEL PERFORMANCE (Cross-Validated) ---")
        avg_acc = sum(r["accuracy"] for r in cv_results) / len(cv_results)
        avg_f1 = sum(r["f1"] for r in cv_results) / len(cv_results)
        avg_brier = sum(r["brier"] for r in cv_results) / len(cv_results)

        for r in cv_results:
            lines.append(
                f"  Fold {r['fold']}: acc={r['accuracy']:.1%} prec={r['precision']:.1%} "
                f"rec={r['recall']:.1%} f1={r['f1']:.3f} brier={r['brier']:.3f}"
            )

        lines.append(f"\n  Average accuracy: {avg_acc:.1%} (baseline: {summary['win_rate']:.1%})")
        edge = avg_acc - summary['win_rate']
        if edge > 0.02:
            lines.append(f"  Model adds +{edge:.1%} edge over random")
        elif edge > 0:
            lines.append(f"  Model adds marginal +{edge:.1%} edge — needs more data")
        else:
            lines.append(f"  Model does NOT outperform baseline — signals may not be predictive yet")

    # 3. Top Predictive Signals
    if importances:
        lines.append("\n--- TOP 10 PREDICTIVE SIGNALS ---")
        top_10 = list(importances.items())[:10]
        for i, (feat, imp) in enumerate(top_10, 1):
            lines.append(f"  {i:2d}. {feat:35s} importance={imp:.4f}")

        lines.append("\n--- BOTTOM 10 (LEAST USEFUL) ---")
        bottom_10 = list(importances.items())[-10:]
        for feat, imp in bottom_10:
            lines.append(f"      {feat:35s} importance={imp:.4f}")

    # 4. Skip Verification
    if skip_analysis:
        lines.append("\n--- SKIP VERIFICATION ---")
        total_skips = len(skip_analysis)
        with_vote = [s for s in skip_analysis if s["would_have_won"] is not None]
        would_have_won = [s for s in with_vote if s["would_have_won"]]

        lines.append(f"Skips with known outcome: {total_skips}")
        if with_vote:
            lines.append(
                f"Skips where ensemble had a direction: {len(with_vote)} "
                f"(would have won {len(would_have_won)}/{len(with_vote)} = "
                f"{len(would_have_won)/len(with_vote):.1%})"
            )

        # Breakdown by skip reason
        reason_counts = Counter(s["skip_reason"] for s in skip_analysis)
        reason_wins = Counter(
            s["skip_reason"] for s in skip_analysis
            if s["would_have_won"] is True
        )
        lines.append("\nBy skip reason:")
        for reason, count in reason_counts.most_common():
            wins = reason_wins.get(reason, 0)
            wr = wins / count if count > 0 else 0
            lines.append(f"  {reason:25s}: {count:4d} skips, {wins:3d} would have won ({wr:.1%})")

        # Model confidence on skips
        avg_prob = sum(s["model_win_prob"] for s in skip_analysis) / len(skip_analysis)
        lines.append(f"\nAverage model P(win) on skips: {avg_prob:.3f}")
        high_conf_skips = [s for s in skip_analysis if s["model_win_prob"] > 0.60]
        if high_conf_skips:
            lines.append(
                f"High-confidence skips (model >60%): {len(high_conf_skips)} — "
                f"THESE ARE MISSED OPPORTUNITIES"
            )

    # 5. Backtest Results
    if backtest_results:
        lines.append("\n--- STRATEGY COMPARISON ---")
        lines.append(
            f"{'Strategy':35s} {'Trades':>7s} {'Win%':>6s} {'PnL':>10s} "
            f"{'Avg PnL':>8s} {'MaxDD':>8s} {'PF':>6s}"
        )
        lines.append("-" * 85)
        for r in backtest_results:
            if r.trades_taken > 0:
                lines.append(
                    f"{r.strategy:35s} {r.trades_taken:7d} {r.win_rate:5.1%} "
                    f"${r.total_pnl:9.2f} ${r.avg_pnl:7.2f} ${r.max_drawdown:7.2f} "
                    f"{r.profit_factor:5.2f}"
                )

    # 6. Actionable Recommendations
    lines.append("\n--- ACTIONABLE RECOMMENDATIONS ---")
    recommendations = []

    # Timing recommendation
    timing_results = [r for r in backtest_results if r.strategy.startswith("Entry T-")]
    if timing_results:
        best_timing = max(timing_results, key=lambda r: r.win_rate if r.trades_taken >= 5 else 0)
        if best_timing.win_rate > summary.get("win_rate", 0) + 0.03:
            recommendations.append(
                f"TIMING: {best_timing.strategy} has {best_timing.win_rate:.1%} win rate "
                f"vs {summary['win_rate']:.1%} baseline. Consider adjusting ENTRY_SECONDS_BEFORE_CLOSE."
            )

    # Slippage recommendation
    slip_results = [r for r in backtest_results if r.strategy.startswith("Slippage")]
    if slip_results:
        best_slip = max(slip_results, key=lambda r: r.total_pnl if r.trades_taken >= 5 else -999)
        baseline_pnl = next((r.total_pnl for r in backtest_results if r.strategy == "Baseline (actual)"), 0)
        if best_slip.total_pnl > baseline_pnl * 1.1:
            recommendations.append(
                f"SLIPPAGE: {best_slip.strategy} improves PnL to ${best_slip.total_pnl:.2f} "
                f"vs ${baseline_pnl:.2f}. Consider adjusting LIVE_MAX_SLIPPAGE_PCT."
            )

    # ML filter recommendation
    ml_results = [r for r in backtest_results if r.strategy.startswith("ML filter")]
    if ml_results:
        best_ml = max(ml_results, key=lambda r: r.total_pnl if r.trades_taken >= 5 else -999)
        if best_ml.win_rate > summary.get("win_rate", 0) + 0.05:
            recommendations.append(
                f"ML FILTER: {best_ml.strategy} achieves {best_ml.win_rate:.1%} win rate. "
                f"Consider adding model confidence gate."
            )

    # High confidence recommendation
    hc = next((r for r in backtest_results if r.strategy.startswith("High confidence")), None)
    if hc and hc.trades_taken >= 5 and hc.win_rate > summary.get("win_rate", 0) + 0.05:
        recommendations.append(
            f"CONFIDENCE: 3/3 consensus trades have {hc.win_rate:.1%} win rate. "
            f"Consider only trading on unanimous agreement."
        )

    if recommendations:
        for i, rec in enumerate(recommendations, 1):
            lines.append(f"\n{i}. {rec}")
    else:
        lines.append("\nNo strong recommendations yet — need more data for reliable patterns.")

    lines.append(f"\n{'=' * 70}")
    return "\n".join(lines)
