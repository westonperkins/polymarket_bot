# Polymarket BTC 5-Minute Up/Down Paper Trading Bot

> **If this is a new Claude Code session, read this entire file first before doing anything. This is the full project context.**

---

## Current Status

**Step 14 COMPLETE — End-to-end test with paper trading running live**

All 14 steps are done. The bot is fully built and tested live.

Live test results (2 full 5-minute cycles):
- Cycle 1: Discovered "3:20-3:25 AM ET", signal window at T-30, all models voted → SKIP (1/3 consensus)
- Cycle 2: Discovered "3:30-3:35 AM ET", signal window fired, momentum=Down, reversion=Up, structure=Up → MEDIUM UP trade at odds 0.475, $250 size
- Background resolution: Polymarket resolved "Up" after ~3 min (attempt 20). Trade settled as WIN: +$276.32
- Trade 3 entered automatically on next cycle (pending resolution)
- Final balance: $10,276.32 (+2.76%), 1W/0L/1S

Fixes applied during live testing:
- Background resolution: Polymarket takes 3-10 min to resolve markets. Resolution now runs as async background task so the engine doesn't miss the next candle
- Resolution retry: increased to 60 attempts × 10s (up to ~10 min wait)
- Post-close buffer: 5s (was 2s), enough to trigger close callback
- Dashboard screen mode: auto-detects TTY, falls back to non-screen mode when piped
- Race condition fix: added 0.1s delay before dashboard loop for engine.running to initialize

To run the bot:
```
cd polymarket_bot
python3 main.py
```
Ctrl+C for graceful shutdown. Logs go to bot.log.

Previously completed:
- All 14 steps

---

## Full Project Brief

### Strategy Overview

Polymarket runs 5-minute Bitcoin Up/Down markets. Each market resolves "Up" if BTC price at the end of the 5-minute window is >= the opening price, "Down" otherwise. Resolution uses the Chainlink BTC/USD oracle, not spot price. Shares pay out $1 each if correct.

The bot waits until exactly 30 seconds before each market closes, then analyzes signals and decides whether to place a simulated trade, or skip entirely.

### Edge Case Filter — Tradeable Window

At the 30-second mark, check the current Polymarket odds for "Up":

- If odds are between 0.30 and 0.70 → market is tradeable, proceed to signal analysis
- If odds are below 0.30 or above 0.70 → outcome already priced in, skip this candle entirely and wait for the next 5-minute market to open
- Log every skip with reason

This handles situations where price has moved so far in one direction that the payout is essentially zero.

### Signal Inputs

At the 30-second mark, compute and analyze all of the following:

**Signal 1 — Chainlink vs Spot Divergence (highest priority)**

- Fetch live Chainlink BTC/USD oracle price from: https://data.chain.link/streams/btc-usd
- Fetch live spot BTC/USD from Binance public API: https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT
- Calculate divergence: spot_price - chainlink_price
- If spot is significantly above chainlink AND current candle is "Up" → strong Up signal
- If spot is significantly below chainlink AND current candle is "Down" → strong Down signal
- This is the most important signal because Polymarket resolves on Chainlink, not spot

**Signal 2 — Current candle position**

- opening_price = the "price to beat" for this Polymarket market (fetch from Polymarket API)
- current_chainlink_price vs opening_price → how far up or down is the candle right now?
- Express as a dollar amount and direction

**Signal 3 — Price momentum (last 60-120 seconds)**

- Track BTC spot price every 5 seconds
- Calculate rate of change over last 60 seconds and last 120 seconds
- Is price accelerating or decelerating toward/away from the opening price?
- Momentum direction: bullish, bearish, or neutral

**Signal 4 — CVD (Cumulative Volume Delta)**

- Fetch Binance trade stream or recent trades endpoint: https://api.binance.com/api/v3/trades?symbol=BTCUSDT&limit=500
- CVD = sum of (buy volume - sell volume) over last 120 seconds
- Positive CVD = aggressive buying pressure
- Negative CVD = aggressive selling pressure

**Signal 5 — Order book imbalance**

- Fetch Binance order book: https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=20
- Calculate bid_volume vs ask_volume within 0.1% of current price
- Ratio > 1.5 = strong buy wall (bullish)
- Ratio < 0.67 = strong sell wall (bearish)

**Signal 6 — Liquidation pressure**

- Fetch from Coinglass public API or Binance futures liquidation stream if available
- Recent large liquidations in last 2 minutes
- Long liquidations = bearish pressure
- Short liquidations = bullish pressure

**Signal 7 — Distance from round number**

- Is current BTC price within $200 of a round number ($83,000, $84,000, etc.)?
- Round numbers act as strong support/resistance at short timeframes
- Factor into confidence score

**Signal 8 — Time of day regime**

- US market hours (9:30am-4pm ET): high volume, more momentum-driven
- Asian hours (10pm-6am ET): low volume, more mean reversion
- Overnight US (4pm-9:30am ET): medium
- Weight signals differently based on regime

**Signal 9 — Previous candle streak**

- Track last 5 candle outcomes (Up/Down)
- 3+ consecutive same direction = mean reversion probability increases
- Use as a small weight modifier, not a primary signal

### Decision Logic — Ensemble Voting

Use 3 sub-models that each cast a vote: UP, DOWN, or ABSTAIN

**Sub-model 1 — Momentum model:**

- Inputs: price momentum (Signal 3), CVD (Signal 4), Chainlink vs spot (Signal 1)
- Votes UP if majority bullish, DOWN if majority bearish, ABSTAIN if mixed

**Sub-model 2 — Mean reversion model:**

- Inputs: candle position (Signal 2), order book imbalance (Signal 5), candle streak (Signal 9)
- Votes against the current direction if price has moved far from open and streak is long

**Sub-model 3 — Market structure model:**

- Inputs: round number distance (Signal 7), liquidation pressure (Signal 6), time of day (Signal 8)
- Votes based on structural factors

**Final decision rules:**

- All 3 vote same direction → HIGH CONFIDENCE trade, full position size
- 2 of 3 vote same direction → MEDIUM CONFIDENCE trade, half position size
- 1 or 0 agree → NO TRADE, skip this candle
- Log confidence level with every trade and skip

### Paper Trading System

**Virtual portfolio:**

- Starting balance: $10,000 virtual USDC
- Risk per trade (high confidence): 5% of current portfolio
- Risk per trade (medium confidence): 2.5% of current portfolio
- Payout rate: determined by actual Polymarket odds at time of trade (e.g. buying at 65¢ = 35% payout)
- Never risk more than defined % regardless of confidence

**Position simulation:**

- At 30-second mark, if trade signal triggered and odds in tradeable window:
  - Record: entry odds, side (Up/Down), simulated position size, all signal values
- At market resolution (fetch result from Polymarket API):
  - If correct: portfolio += position_size * payout_rate
  - If incorrect: portfolio -= position_size
  - Log result

### Database — SQLite

**Table: trades**

id, timestamp, market_id, side (Up/Down), entry_odds, position_size, payout_rate, confidence_level (high/medium/skip), outcome (win/loss/skip/pending), pnl, portfolio_balance_after

**Table: signals**

id, trade_id, chainlink_price, spot_price, chainlink_spot_divergence, candle_position_dollars, momentum_60s, momentum_120s, cvd, order_book_ratio, liquidation_signal, round_number_distance, time_regime, candle_streak, momentum_vote, reversion_vote, structure_vote, final_vote

**Table: portfolio**

id, timestamp, balance, total_trades, wins, losses, skips, win_rate, daily_pnl

### Timing Engine

This is critical. The bot must:

1. Know the exact open timestamp of each Polymarket 5-minute market
2. Calculate T-30 seconds from close
3. At exactly T-30, begin fetching all signals simultaneously (parallel async requests)
4. Complete all signal fetching within 5 seconds
5. Compute votes and decision within 2 seconds
6. Log simulated trade within 1 second
7. At market close, fetch resolution and log outcome
8. Immediately begin monitoring next market

Use asyncio for parallel signal fetching to stay within the time budget. All API calls must have a 3-second timeout — if a signal fails to fetch, that sub-model abstains rather than crashing.

### Polymarket API Integration

Use the Polymarket CLOB API and Gamma API (no authentication needed for reading market data):

- Gamma API for market metadata (opening price, market ID, close time): https://gamma-api.polymarket.com/markets
- CLOB API for live odds/orderbook: https://clob.polymarket.com
- Filter for BTC 5-minute Up/Down markets specifically
- Poll for the next upcoming market and queue it in the timing engine

### Dashboard

Build a simple terminal dashboard using the rich Python library that shows in real time:

- Current portfolio balance and % change from start
- Win rate, total trades, wins, losses, skips
- Current market: time remaining, current odds, "price to beat"
- Last 10 trades with outcome and P&L
- Current signal values and votes when at the 30-second window
- Refresh every 5 seconds

### Project Structure

```
polymarket_bot/
├── main.py
├── config.py
├── timing_engine.py
├── signals/
│   ├── chainlink.py
│   ├── spot.py
│   ├── cvd.py
│   ├── orderbook.py
│   ├── liquidations.py
│   ├── market_structure.py
├── models/
│   ├── momentum_model.py
│   ├── reversion_model.py
│   ├── structure_model.py
│   ├── ensemble.py
├── polymarket/
│   ├── markets.py
│   ├── odds.py
│   ├── resolver.py
├── paper_trading/
│   ├── portfolio.py
│   ├── simulator.py
├── database/
│   ├── db.py
│   ├── schema.sql
├── dashboard/
│   ├── display.py
└── requirements.txt
```

### Requirements

aiohttp, asyncio, rich, sqlite3, python-dotenv, pandas, numpy

### Build Order

1. Project structure and config.py ✅
2. Database schema and db.py ✅
3. Polymarket market fetcher (markets.py, odds.py) ✅
4. Timing engine skeleton ✅
5. Each signal module one at a time (start with chainlink.py and spot.py) ✅
6. CVD and order book signals ✅
7. Liquidations and market structure signals ✅
8. All three sub-models ✅
9. Ensemble decision logic ✅
10. Portfolio and simulator ✅
11. Resolver ✅
12. Dashboard ✅
13. Wire everything together in main.py ✅
14. End-to-end test with paper trading running live ✅
