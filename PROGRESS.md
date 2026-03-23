# Polymarket BTC 5-Minute Up/Down Paper Trading Bot

> **If this is a new Claude Code session, read this entire file first before doing anything. This is the full project context.**

---

## Current Status

**All 14 build steps complete. Bot deployed to VPS and running in production.**

Paper trading performance (as of 2026-03-23):
- Starting balance: $10,000
- Current balance: ~$99,598
- Win rate: ~71%
- Over 520 trades

---

## Infrastructure

- **VPS**: Hetzner Helsinki (65.109.132.210), Ubuntu 24.04
- **Access**: `ssh hetzner-bot`
- **Process**: Runs in a `screen` session on the VPS
- **Dashboard**: http://65.109.132.210:8080
- **Logs**: `bot.log` on VPS
- **Linux TCP keepalive settings** applied to VPS for connection stability

To run the bot:
```
cd polymarket_bot
python3 main.py
```
Ctrl+C for graceful shutdown.

---

## Environment Variables

```
DATABASE_URL=<supabase postgres connection string>
POLY_PRIVATE_KEY=<polymarket wallet private key>
POLY_FUNDER=<polymarket funder address>
POLY_SIGNATURE_TYPE=1
TRADING_MODE=paper          # or "live"
BINANCE_REGION=global       # "us" for local Mac, "global" for VPS
```

---

## Major Code Changes (post-initial build)

### HTTP Client Migration: aiohttp → httpx
- All signal modules (`signals/spot.py`, `signals/orderbook.py`, `signals/cvd.py`, `signals/chainlink.py`, `signals/liquidations.py`) now use `httpx.AsyncClient`
- All polymarket modules (`polymarket/odds.py`, `polymarket/resolver.py`, `polymarket/markets.py`) now use `httpx.AsyncClient`
- `timing_engine.py` uses its own `httpx.AsyncClient` for market discovery and odds
- `aiohttp` retained only for the web dashboard server (`web/server.py`)
- Reason: httpx handles SSL connections more reliably on Linux

### SessionManager (main.py)
- Rotating `httpx.AsyncClient` factory with 2-minute max age (`SESSION_MAX_AGE = 120`)
- Health-based recreation: 2 consecutive failures trigger immediate client recreation
- Logs every client creation for diagnostics (`HTTP client created (#N)`)
- All signal and polymarket modules receive the managed client — no module creates its own

### Adaptive Spot Polling
- Three-tier polling strategy to reduce Binance API load:
  - **5s** (active): within 2 minutes of market close (`SPOT_ACTIVE_WINDOW = 120`)
  - **60s** (tracking): while tracking a market but far from close
  - **180s** (between markets): idle gap after market close, before next discovery
- Reduced from ~780 calls/hour to ~60 calls/hour during idle periods
- Momentum calculation tolerance widened to 35s to accommodate sparser samples

### Post-Close Request Staggering
- 5-second delay (`POST_CLOSE_DELAY`) after market close before next market discovery
- Prevents resolution, discovery, and spot polling from all firing HTTP requests simultaneously
- Solves ConnectTimeout cascade that occurred right after market close

### Binance Region Support
- `BINANCE_REGION` env variable: `"us"` → `api.binance.us`, `"global"` → `api.binance.com`
- Local Mac development uses `us`, VPS uses `global`

### Live Trading Mode
- `TRADING_MODE=live` activates real order execution via `py-clob-client`
- `live_trading/executor.py`: CLOB order placement with Fill-Or-Kill orders
- `live_trading/risk.py`: risk limits (max daily loss, max position size, min balance)
- `live_trading/live_simulator.py`: wraps real execution in the same interface as paper
- Live/paper portfolio separation in dashboard with toggle
- P&L calendar view added to web dashboard

### Error Handling & Resilience
- All HTTP calls include `{type(e).__name__}: {e}` in error logs for precise diagnosis
- Retry logic with exponential backoff throughout signal and polymarket modules
- Cache fallback for spot price (`SPOT_CACHE_TTL = 90s`)
- Background resolution: 60 attempts x 10s (up to ~10 min wait for Polymarket to resolve)
- Auto-restart on crash in `main.py` with 10s cooldown

---

## Current Known Issues

1. **ConnectTimeout during idle period** — Occasional timeouts on VPS between markets, mitigated by 180s between-markets polling and post-close staggering but not fully eliminated
2. **eth.llamarpc.com returns 403 on VPS** — Chainlink oracle falls back to ankr and publicnode RPCs (works fine)
3. **Live/paper portfolio separation bug** — Dashboard doesn't fully separate live vs paper portfolios yet
4. **get_balance bug** — `live_trading/executor.py` balance fetch not working correctly

---

## Architecture

### Project Structure

```
polymarket_bot/
├── main.py                  # Entry point, SessionManager, callbacks, polling
├── config.py                # All constants and env vars
├── timing_engine.py         # Market lifecycle: discover → signal → close → resolve
├── network_health.py        # API success rate tracking
├── signals/
│   ├── chainlink.py         # Chainlink BTC/USD oracle via Ethereum RPC (httpx)
│   ├── spot.py              # Binance spot price + SpotTracker momentum (httpx)
│   ├── cvd.py               # Cumulative volume delta from Binance trades (httpx)
│   ├── orderbook.py         # Order book imbalance from Binance depth (httpx)
│   ├── liquidations.py      # Gate.io futures liquidation pressure (httpx)
│   └── market_structure.py  # Round numbers, time regime, streak
├── models/
│   ├── momentum_model.py    # Sub-model 1: momentum + CVD + divergence
│   ├── reversion_model.py   # Sub-model 2: candle position + orderbook + streak
│   ├── structure_model.py   # Sub-model 3: round numbers + liquidations + time
│   └── ensemble.py          # 3-vote ensemble → UP/DOWN/SKIP + confidence
├── polymarket/
│   ├── markets.py           # Gamma API market discovery with fallback (httpx)
│   ├── odds.py              # CLOB + Gamma odds with retry (httpx)
│   └── resolver.py          # Post-close resolution polling (httpx)
├── paper_trading/
│   ├── portfolio.py         # Virtual portfolio with Supabase persistence
│   └── simulator.py         # Paper trade entry/settlement
├── live_trading/
│   ├── executor.py          # py-clob-client order execution
│   ├── risk.py              # Daily loss / position size limits
│   └── live_simulator.py    # Real execution wrapped in simulator interface
├── database/
│   └── db.py                # Supabase PostgreSQL connection and queries
├── dashboard/
│   └── display.py           # Terminal dashboard state
├── web/
│   └── server.py            # aiohttp web dashboard (port 8080)
└── requirements.txt         # aiohttp, httpx, rich, psycopg2-binary, py-clob-client, etc.
```

### Key APIs

| Service | Base URL | Used For |
|---------|----------|----------|
| Binance | `api.binance.{com,us}` | Spot price, trades (CVD), order book depth |
| Polymarket Gamma | `gamma-api.polymarket.com` | Market metadata, event discovery, resolution |
| Polymarket CLOB | `clob.polymarket.com` | Live odds, order placement (live mode) |
| Gate.io Futures | `api.gateio.ws/api/v4` | BTC liquidation pressure |
| Ethereum RPCs | llamarpc, ankr, publicnode | Chainlink BTC/USD oracle price |
| Supabase | pooler endpoint | PostgreSQL database |

### Decision Flow

1. Timing engine discovers next BTC 5-min market via Gamma API
2. Waits until T-30s before close
3. Fetches odds → checks tradeable window (0.30-0.70)
4. If tradeable, fetches all 9 signals in parallel via httpx
5. Three sub-models vote: momentum, reversion, structure
6. Ensemble: 3/3 agree → HIGH confidence (5% risk), 2/3 → MEDIUM (2.5%), else SKIP
7. Records trade in Supabase, waits for resolution
8. Background resolution polls CLOB/Gamma for winner, settles P&L

### Build Order (all complete)

1. Project structure and config.py ✅
2. Database schema and db.py ✅
3. Polymarket market fetcher (markets.py, odds.py) ✅
4. Timing engine skeleton ✅
5. Each signal module one at a time (chainlink.py and spot.py) ✅
6. CVD and order book signals ✅
7. Liquidations and market structure signals ✅
8. All three sub-models ✅
9. Ensemble decision logic ✅
10. Portfolio and simulator ✅
11. Resolver ✅
12. Dashboard ✅
13. Wire everything together in main.py ✅
14. End-to-end test with paper trading running live ✅
