"""Real-time terminal dashboard using the rich library.

Shows portfolio status, current market, recent trades, and live signals.
Refreshes every DASHBOARD_REFRESH_INTERVAL seconds.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import config
from database import db
from models.ensemble import EnsembleDecision
from paper_trading.portfolio import Portfolio
from polymarket.markets import MarketInfo
from polymarket.odds import MarketOdds
from timing_engine import TimingEngine

logger = logging.getLogger(__name__)


class Dashboard:
    """Terminal dashboard that reads state from the engine, portfolio, and DB."""

    def __init__(
        self,
        engine: TimingEngine,
        portfolio: Portfolio,
        conn,
    ) -> None:
        self._engine = engine
        self._portfolio = portfolio
        self._conn = conn
        self._console = Console()
        self._live: Optional[Live] = None

        # Signal snapshot — updated by main.py during signal window
        self.last_signals: Optional[dict] = None
        self.last_decision: Optional[EnsembleDecision] = None
        self.status_message: str = "Starting up..."

    def build_display(self) -> Layout:
        """Build the full dashboard layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1),
        )
        layout["left"].split_column(
            Layout(name="portfolio", size=10),
            Layout(name="market", size=9),
            Layout(name="signals", ratio=1),
        )
        layout["right"].split_column(
            Layout(name="trades", ratio=1),
        )

        layout["header"].update(self._build_header())
        layout["portfolio"].update(self._build_portfolio_panel())
        layout["market"].update(self._build_market_panel())
        layout["signals"].update(self._build_signals_panel())
        layout["trades"].update(self._build_trades_panel())
        layout["footer"].update(self._build_footer())

        return layout

    def _build_header(self) -> Panel:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        title = Text("POLYMARKET BTC 5-MIN PAPER TRADING BOT", style="bold cyan")
        title.append(f"  |  {now}", style="dim")
        return Panel(title, style="cyan")

    def _build_portfolio_panel(self) -> Panel:
        table = Table(show_header=False, expand=True, padding=(0, 1))
        table.add_column("Label", style="bold")
        table.add_column("Value", justify="right")

        balance = self._portfolio.balance
        pnl_pct = self._portfolio.pnl_pct
        daily_pnl = self._portfolio.daily_pnl
        stats = db.get_trade_stats(self._conn)

        pnl_style = "green" if pnl_pct >= 0 else "red"
        daily_style = "green" if daily_pnl >= 0 else "red"

        table.add_row("Balance", f"[bold]${balance:,.2f}[/bold]")
        table.add_row("Total P&L", f"[{pnl_style}]{pnl_pct:+.2f}%[/{pnl_style}]")
        table.add_row("Daily P&L", f"[{daily_style}]${daily_pnl:+,.2f}[/{daily_style}]")
        table.add_row("", "")
        table.add_row("Trades", str(stats["total"]))
        table.add_row(
            "W / L / S",
            f"[green]{stats['wins']}[/green] / [red]{stats['losses']}[/red] / {stats['skips']}",
        )
        table.add_row("Win Rate", f"{stats['win_rate']:.1f}%")

        return Panel(table, title="Portfolio", border_style="green")

    def _build_market_panel(self) -> Panel:
        market = self._engine.current_market
        odds = self._engine.current_odds

        if market is None:
            return Panel(
                Text("Waiting for next market...", style="dim"),
                title="Current Market",
                border_style="yellow",
            )

        table = Table(show_header=False, expand=True, padding=(0, 1))
        table.add_column("Label", style="bold")
        table.add_column("Value", justify="right")

        secs_close = self._engine.seconds_until_close()
        secs_signal = self._engine.seconds_until_signal_window()

        time_str = f"{secs_close:.0f}s" if secs_close and secs_close > 0 else "CLOSED"
        signal_str = (
            f"{secs_signal:.0f}s"
            if secs_signal and secs_signal > 0
            else "[bold yellow]NOW[/bold yellow]"
        )

        table.add_row("Market", market.slug.replace("btc-updown-5m-", ""))
        table.add_row("Window", market.title.split(" - ")[-1] if " - " in market.title else "")
        table.add_row("Time to Close", f"[bold]{time_str}[/bold]")
        table.add_row("Signal Window", signal_str)

        if odds:
            tradeable_style = "green" if odds.tradeable else "red"
            table.add_row(
                "Odds (Up/Down)",
                f"{odds.up_price:.3f} / {odds.down_price:.3f}",
            )
            table.add_row(
                "Tradeable",
                f"[{tradeable_style}]{'YES' if odds.tradeable else 'NO'}[/{tradeable_style}]",
            )

        return Panel(table, title="Current Market", border_style="yellow")

    def _build_signals_panel(self) -> Panel:
        if self.last_signals is None:
            return Panel(
                Text("No signals yet — waiting for signal window", style="dim"),
                title="Signals & Votes",
                border_style="magenta",
            )

        table = Table(show_header=False, expand=True, padding=(0, 0))
        table.add_column("Signal", style="bold", width=20)
        table.add_column("Value", justify="right")

        s = self.last_signals

        if s.get("chainlink_price"):
            table.add_row("Chainlink", f"${s['chainlink_price']:,.2f}")
        if s.get("spot_price"):
            table.add_row("Spot", f"${s['spot_price']:,.2f}")
        if s.get("chainlink_spot_divergence") is not None:
            table.add_row("Divergence", f"${s['chainlink_spot_divergence']:+,.2f}")
        if s.get("candle_position_dollars") is not None:
            table.add_row("Candle Pos", f"${s['candle_position_dollars']:+,.2f}")
        if s.get("momentum_60s") is not None:
            table.add_row("Momentum 60s", f"${s['momentum_60s']:+.4f}/s")
        if s.get("momentum_120s") is not None:
            table.add_row("Momentum 120s", f"${s['momentum_120s']:+.4f}/s")
        if s.get("cvd") is not None:
            table.add_row("CVD", f"{s['cvd']:+.6f} BTC")
        if s.get("order_book_ratio") is not None:
            table.add_row("OB Ratio", f"{s['order_book_ratio']:.3f}")
        if s.get("liquidation_signal") is not None:
            table.add_row("Liq Net", f"${s['liquidation_signal']:+,.0f}")
        if s.get("round_number_distance") is not None:
            table.add_row("Round # Dist", f"${s['round_number_distance']:,.0f}")
        if s.get("time_regime"):
            table.add_row("Regime", s["time_regime"])
        if s.get("candle_streak"):
            table.add_row("Streak", s["candle_streak"])

        table.add_row("", "")

        # Votes
        if self.last_decision:
            d = self.last_decision
            table.add_row("Momentum Vote", _vote_styled(d.momentum_vote))
            table.add_row("Reversion Vote", _vote_styled(d.reversion_vote))
            table.add_row("Structure Vote", _vote_styled(d.structure_vote))
            table.add_row("", "")

            if d.side:
                conf_style = "bold green" if d.confidence == "high" else "yellow"
                table.add_row(
                    "DECISION",
                    f"[{conf_style}]{d.side} ({d.confidence.upper()})[/{conf_style}]",
                )
            else:
                table.add_row("DECISION", "[dim]SKIP[/dim]")

        return Panel(table, title="Signals & Votes", border_style="magenta")

    def _build_trades_panel(self) -> Panel:
        trades = db.get_recent_trades(self._conn, limit=10)

        table = Table(expand=True, padding=(0, 1))
        table.add_column("#", style="dim", width=4)
        table.add_column("Market", width=12)
        table.add_column("Side", width=5)
        table.add_column("Conf", width=6)
        table.add_column("Odds", justify="right", width=5)
        table.add_column("Size", justify="right", width=8)
        table.add_column("Result", width=6)
        table.add_column("P&L", justify="right", width=9)

        for t in trades:
            outcome = t["outcome"]
            if outcome == "win":
                result_str = "[green]WIN[/green]"
            elif outcome == "loss":
                result_str = "[red]LOSS[/red]"
            elif outcome == "skip":
                result_str = "[dim]SKIP[/dim]"
            else:
                result_str = "[yellow]...[/yellow]"

            pnl = t["pnl"] or 0
            pnl_style = "green" if pnl > 0 else "red" if pnl < 0 else "dim"
            slug_short = t["market_id"].replace("btc-updown-5m-", "")[-8:]

            table.add_row(
                str(t["id"]),
                slug_short,
                t["side"] if t["outcome"] != "skip" else "-",
                t["confidence_level"][:3].upper() if t["confidence_level"] != "skip" else "-",
                f"{t['entry_odds']:.2f}" if t["entry_odds"] else "-",
                f"${t['position_size']:,.0f}" if t["position_size"] else "-",
                result_str,
                f"[{pnl_style}]${pnl:+,.2f}[/{pnl_style}]" if outcome != "skip" else "-",
            )

        if not trades:
            table.add_row("", "", "", "", "", "", "[dim]No trades yet[/dim]", "")

        return Panel(table, title="Last 10 Trades", border_style="blue")

    def _build_footer(self) -> Panel:
        return Panel(
            Text(self.status_message, style="dim"),
            style="dim",
        )

    def get_renderable(self):
        """Return the current dashboard layout for use with rich.Live."""
        return self.build_display()


def _vote_styled(vote: str) -> str:
    """Return a styled vote string."""
    if vote == "Up":
        return "[green]UP[/green]"
    elif vote == "Down":
        return "[red]DOWN[/red]"
    return "[dim]ABSTAIN[/dim]"
