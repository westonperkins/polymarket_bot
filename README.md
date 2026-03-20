# Polymarket BTC 5-Minute Up/Down Paper Trading Bot

A paper trading bot that tests a signal-based strategy on Polymarket's Bitcoin 5-minute Up/Down markets. The bot waits until 30 seconds before each market closes, analyzes 9 signals across 3 sub-models, and decides whether to simulate a trade.

## Features

- **9 real-time signals**: Chainlink oracle price, Binance spot price, price momentum, CVD, order book imbalance, liquidation pressure, round number distance, time-of-day regime, candle streak
- **3 sub-model ensemble**: Momentum, Mean Reversion, and Market Structure models vote independently
- **Paper trading**: $10,000 virtual portfolio with position sizing based on confidence
- **Dual dashboard**: Terminal (rich) + Web GUI (localhost:8080)
- **SQLite database**: Full trade history with signal snapshots
- **Background resolution**: Resolves trades asynchronously so no candles are missed

## Quick Start

### macOS

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/polymarket-bot.git
cd polymarket-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the bot
python3 main.py
```

### Windows (PowerShell)

```powershell
# Clone the repo
git clone https://github.com/YOUR_USERNAME/polymarket-bot.git
cd polymarket-bot

# Create virtual environment
python -m venv venv
venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Run the bot
python main.py
```

### Windows (Command Prompt)

```cmd
git clone https://github.com/YOUR_USERNAME/polymarket-bot.git
cd polymarket-bot

python -m venv venv
venv\Scripts\activate.bat

pip install -r requirements.txt

python main.py
```

## Commands

| Command | Description |
|---------|-------------|
| `python3 main.py` | Run the bot (terminal dashboard + web GUI + trading engine) |
| `python3 status.py` | Quick P&L check in terminal (read-only, no trading) |
| `python3 status.py --web` | Web dashboard only at localhost:8080 (read-only, no trading) |

> On Windows, use `python` instead of `python3`.

## Web Dashboard

When the bot is running, open **http://localhost:8080** in your browser. The dashboard shows:

- Portfolio balance and P&L
- Current market with countdown timer
- Live signal values and model votes
- Trade history with expandable detail rows (click any trade to see all signals)

## How It Works

1. **Market Discovery** — Finds the current Polymarket BTC 5-min Up/Down market via the Gamma API
2. **Wait** — Sleeps until exactly 30 seconds before the market closes
3. **Odds Filter** — If "Up" odds are outside 0.30–0.70, skips (outcome already priced in)
4. **Signal Fetch** — Fetches all 9 signals in parallel (~0.5s)
5. **Voting** — 3 sub-models each vote Up, Down, or Abstain
6. **Decision** — 3/3 agree = high confidence (5% risk), 2/3 = medium (2.5%), else skip
7. **Resolution** — After market closes, fetches result from Polymarket and settles the trade
8. **Loop** — Immediately starts tracking the next market

## Project Structure

```
polymarket_bot/
├── main.py                  # Entry point — runs everything
├── status.py                # Read-only status viewer
├── config.py                # All constants and thresholds
├── timing_engine.py         # Market lifecycle + T-30 trigger
├── signals/
│   ├── chainlink.py         # Chainlink oracle via Ethereum RPC
│   ├── spot.py              # Binance.US spot + momentum tracker
│   ├── cvd.py               # Cumulative Volume Delta
│   ├── orderbook.py         # Order book bid/ask imbalance
│   ├── liquidations.py      # Gate.io liquidation data
│   └── market_structure.py  # Round numbers, time regime, streak
├── models/
│   ├── momentum_model.py    # Sub-model 1: momentum signals
│   ├── reversion_model.py   # Sub-model 2: mean reversion signals
│   ├── structure_model.py   # Sub-model 3: market structure signals
│   └── ensemble.py          # Combines 3 votes into final decision
├── polymarket/
│   ├── markets.py           # Market discovery via Gamma API
│   ├── odds.py              # Live odds from CLOB API
│   └── resolver.py          # Post-close resolution fetcher
├── paper_trading/
│   ├── portfolio.py         # Virtual portfolio manager
│   └── simulator.py         # Trade entry and settlement
├── database/
│   ├── db.py                # SQLite queries
│   └── schema.sql           # Table definitions
├── dashboard/
│   └── display.py           # Terminal dashboard (rich)
├── web/
│   ├── server.py            # Web server (aiohttp)
│   └── templates/
│       └── index.html       # Web dashboard UI
└── requirements.txt
```

## Configuration

All settings are in `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `STARTING_BALANCE` | $10,000 | Initial virtual USDC |
| `RISK_HIGH_CONFIDENCE` | 5% | Portfolio % risked on 3/3 consensus |
| `RISK_MEDIUM_CONFIDENCE` | 2.5% | Portfolio % risked on 2/3 consensus |
| `ODDS_LOWER_BOUND` | 0.30 | Skip if Up odds below this |
| `ODDS_UPPER_BOUND` | 0.70 | Skip if Up odds above this |
| `ENTRY_SECONDS_BEFORE_CLOSE` | 30 | Seconds before close to analyze |
| `WEB_PORT` | 8080 | Web dashboard port |

## API Sources

| Data | Source | Auth Required |
|------|--------|---------------|
| BTC oracle price | Chainlink via Ethereum RPC | No |
| BTC spot price | Binance.US (CoinGecko fallback) | No |
| Trades / Order book | Binance.US | No |
| Liquidations | Gate.io | No |
| Market data / Odds | Polymarket Gamma + CLOB API | No |

## Notes

- No API keys required — all data sources are public
- Binance.com is geo-blocked in the US, so Binance.US is used instead
- Polymarket takes 3–10 minutes to resolve markets after close; resolution runs in the background
- The database file (`polymarket_bot.db`) persists between runs — delete it to reset
- Logs are written to `bot.log`
