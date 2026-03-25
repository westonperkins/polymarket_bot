"""Discord webhook notifications for trade events."""

import logging
import aiohttp

import config

logger = logging.getLogger(__name__)


async def notify_discord(message: str) -> None:
    """Send a message to the configured Discord webhook."""
    if not config.DISCORD_WEBHOOK_URL:
        return
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                config.DISCORD_WEBHOOK_URL,
                json={"content": message},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception as e:
        logger.debug(f"Discord notification failed: {e}")


async def notify_win(trade_id: int, pnl: float, balance: float) -> None:
    """Notify about a winning trade that needs claiming."""
    await notify_discord(
        f"🏆 **WIN** — Trade #{trade_id}\n"
        f"P&L: **+${pnl:.2f}**\n"
        f"Balance: ${balance:.2f}\n"
        f"→ Claim on Polymarket"
    )


async def notify_loss(trade_id: int, pnl: float, balance: float) -> None:
    """Notify about a losing trade."""
    await notify_discord(
        f"💀 **LOSS** — Trade #{trade_id}\n"
        f"P&L: **-${abs(pnl):.2f}**\n"
        f"Balance: ${balance:.2f}"
    )


async def notify_trade_placed(trade_id: int, side: str, cost: float, payout_pct: float) -> None:
    """Notify about a new trade being placed."""
    await notify_discord(
        f"💰 **TRADE** — #{trade_id} {side}\n"
        f"Cost: ${cost:.2f} | Payout: {payout_pct:.1f}%"
    )
