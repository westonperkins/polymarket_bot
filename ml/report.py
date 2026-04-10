"""Generate human-readable insights from ML training results."""

from collections import Counter


def generate_report(
    summary: dict,
    cv_results: list[dict],
    importances: dict,
    skip_analysis: list[dict],
    backtest_results: list,
    taker_results: list | None = None,
    taker_holdout_results: list | None = None,
    holdout_meta: dict | None = None,
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
            f"{'Avg PnL':>8s} {'AvgW':>7s} {'AvgL':>7s} {'R:R':>6s} "
            f"{'MaxDD':>8s} {'PF':>6s}"
        )
        lines.append("-" * 110)
        for r in backtest_results:
            if r.trades_taken > 0:
                rr_str = "inf" if r.avg_rr == float("inf") else f"{r.avg_rr:5.2f}"
                lines.append(
                    f"{r.strategy:35s} {r.trades_taken:7d} {r.win_rate:5.1%} "
                    f"${r.total_pnl:9.2f} ${r.avg_pnl:7.2f} ${r.avg_winner:6.2f} "
                    f"${r.avg_loser:6.2f} {rr_str:>6s} ${r.max_drawdown:7.2f} "
                    f"{r.profit_factor:5.2f}"
                )

    # 5b. Taker Execution Counterfactual
    if taker_results:
        lines.append("\n--- TAKER EXECUTION COUNTERFACTUAL ---")
        lines.append(
            "Simulates what would have happened if every directional cycle had been"
        )
        lines.append(
            "executed as a taker at T-30 (mid + half-spread) instead of as a passive"
        )
        lines.append(
            "limit. PnL is normalized to $1 per trade. Scored with the gate model"
        )
        lines.append(
            "(no execution features) to avoid bias from adversely-selected fills."
        )
        lines.append("")
        lines.append(
            f"{'Strategy':35s} {'Trades':>7s} {'Win%':>6s} {'PnL':>10s} "
            f"{'Avg PnL':>8s} {'AvgW':>7s} {'AvgL':>7s} {'R:R':>6s} "
            f"{'MaxDD':>8s} {'PF':>6s}"
        )
        lines.append("-" * 110)
        for r in taker_results:
            if r.trades_taken > 0:
                rr_str = "inf" if r.avg_rr == float("inf") else f"{r.avg_rr:5.2f}"
                lines.append(
                    f"{r.strategy:35s} {r.trades_taken:7d} {r.win_rate:5.1%} "
                    f"${r.total_pnl:9.2f} ${r.avg_pnl:7.2f} ${r.avg_winner:6.2f} "
                    f"${r.avg_loser:6.2f} {rr_str:>6s} ${r.max_drawdown:7.2f} "
                    f"{r.profit_factor:5.2f}"
                )

        # Compare best taker bucket against actual baseline
        baseline = next(
            (r for r in backtest_results if r.strategy == "Baseline (actual)"),
            None,
        )
        ml_taker = [r for r in taker_results if "ML>" in r.strategy and r.trades_taken > 0]
        if baseline and ml_taker:
            best_taker = max(ml_taker, key=lambda r: r.total_pnl)
            lines.append("")
            lines.append(
                f"Best in-sample taker bucket: {best_taker.strategy}"
            )
            lines.append(
                f"  → {best_taker.trades_taken} trades at {best_taker.win_rate:.1%} "
                f"win rate, ${best_taker.avg_pnl:.2f} avg PnL/trade, R:R {best_taker.avg_rr:.2f}"
            )
            lines.append(
                f"  → vs. baseline: {baseline.trades_taken} trades at "
                f"{baseline.win_rate:.1%}, ${baseline.avg_pnl:.2f} avg PnL/trade"
            )
            lines.append(
                "  ⚠ In-sample only — see HOLDOUT section below for the credibility check."
            )

    # 5c. Taker Counterfactual — Temporal Holdout (step 1.5)
    if taker_holdout_results:
        lines.append("\n--- TAKER COUNTERFACTUAL — TEMPORAL HOLDOUT ---")
        if holdout_meta:
            lines.append(
                f"Trained gate model on first {int(holdout_meta['train_frac']*100)}% "
                f"of decided trades ({holdout_meta['train_decided']} rows), then scored"
            )
            lines.append(
                f"only cycles after the split timestamp "
                f"({holdout_meta['split_timestamp'][:19]}):"
            )
            lines.append(
                f"  • {holdout_meta['test_decided']} held-out decided trades"
            )
            lines.append(
                f"  • {holdout_meta['holdout_skipped']} held-out skipped cycles"
            )
            lines.append(
                f"  • {holdout_meta['holdout_directional']} directional cycles in the holdout window"
            )
            lines.append("")
        lines.append(
            f"{'Strategy':45s} {'Trades':>7s} {'Win%':>6s} {'PnL':>10s} "
            f"{'Avg PnL':>8s} {'AvgW':>7s} {'AvgL':>7s} {'R:R':>6s} "
            f"{'MaxDD':>8s} {'PF':>6s}"
        )
        lines.append("-" * 120)
        for r in taker_holdout_results:
            if r.trades_taken > 0:
                rr_str = "inf" if r.avg_rr == float("inf") else f"{r.avg_rr:5.2f}"
                lines.append(
                    f"{r.strategy:45s} {r.trades_taken:7d} {r.win_rate:5.1%} "
                    f"${r.total_pnl:9.2f} ${r.avg_pnl:7.2f} ${r.avg_winner:6.2f} "
                    f"${r.avg_loser:6.2f} {rr_str:>6s} ${r.max_drawdown:7.2f} "
                    f"{r.profit_factor:5.2f}"
                )

        # Side-by-side comparison: in-sample vs holdout at each threshold
        if taker_results:
            lines.append("")
            lines.append("In-sample vs holdout comparison (per ML threshold):")
            lines.append(
                f"  {'Threshold':12s} {'IS Win%':>9s} {'IS Avg':>9s}  "
                f"{'OOS Win%':>9s} {'OOS Avg':>9s}  {'Verdict':s}"
            )
            for thr in [55, 60, 65, 70]:
                in_row = next(
                    (r for r in taker_results if f"ML>{thr}%" in r.strategy and r.trades_taken > 0),
                    None,
                )
                oos_row = next(
                    (r for r in taker_holdout_results if f"ML>{thr}%" in r.strategy and r.trades_taken > 0),
                    None,
                )
                if in_row and oos_row:
                    win_drop = in_row.win_rate - oos_row.win_rate
                    if oos_row.avg_pnl > 0 and abs(win_drop) < 0.10:
                        verdict = "HOLDS"
                    elif oos_row.avg_pnl > 0:
                        verdict = "PARTIAL (degraded but positive)"
                    else:
                        verdict = "COLLAPSES"
                    lines.append(
                        f"  {'>'+str(thr)+'%':12s} {in_row.win_rate:8.1%} ${in_row.avg_pnl:7.2f}  "
                        f"{oos_row.win_rate:8.1%} ${oos_row.avg_pnl:7.2f}  {verdict}"
                    )

            # Overall verdict
            ml_holdout = [
                r for r in taker_holdout_results
                if "ML>" in r.strategy and r.trades_taken > 0
            ]
            if ml_holdout:
                best_oos = max(ml_holdout, key=lambda r: r.total_pnl)
                lines.append("")
                lines.append(
                    f"Best out-of-sample bucket: {best_oos.strategy}"
                )
                lines.append(
                    f"  → {best_oos.trades_taken} trades at {best_oos.win_rate:.1%} "
                    f"win rate, ${best_oos.avg_pnl:.2f} avg PnL/trade, R:R "
                    f"{best_oos.avg_rr if best_oos.avg_rr != float('inf') else 'inf'}"
                )
                if best_oos.avg_pnl > 0.5:
                    lines.append(
                        "  → HOLDOUT VALIDATES the in-sample edge — step 2 (small live test) is justified."
                    )
                elif best_oos.avg_pnl > 0:
                    lines.append(
                        "  → HOLDOUT PARTIALLY VALIDATES — edge survives but is smaller than in-sample. "
                        "Use a higher ML threshold for step 2."
                    )
                else:
                    lines.append(
                        "  → HOLDOUT FAILS — in-sample edge does not generalize. "
                        "Do NOT proceed to step 2 without further investigation."
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
