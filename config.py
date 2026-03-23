"""Configuration constants for the Polymarket paper trading bot."""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Trading Mode ──────────────────────────────────────────────────────
# "paper" = simulated trades (default), "live" = real orders on Polymarket
TRADING_MODE = os.environ.get("TRADING_MODE", "paper")

# ── Database ─────────────────────────────────────────────────────────
DATABASE_URL = os.environ["DATABASE_URL"]

# ── Portfolio ──────────────────────────────────────────────────────────
STARTING_BALANCE = 10_000.00  # virtual USDC for paper trading
LIVE_STARTING_BALANCE = float(os.environ.get("LIVE_STARTING_BALANCE", "9.00"))  # real USDC for live

# Risk per trade as fraction of current portfolio
RISK_HIGH_CONFIDENCE = 0.05    # 5%
RISK_MEDIUM_CONFIDENCE = 0.025 # 2.5%

# ── Tradeable Window Filter ────────────────────────────────────────────
# Skip if "Up" odds are outside this range (outcome already priced in)
ODDS_LOWER_BOUND = 0.30
ODDS_UPPER_BOUND = 0.70

# ── Timing ─────────────────────────────────────────────────────────────
CANDLE_DURATION_SECONDS = 300       # 5 minutes
ENTRY_SECONDS_BEFORE_CLOSE = 30    # trigger signal analysis at T-30s
SIGNAL_FETCH_TIMEOUT = 10          # max seconds per API call
SIGNAL_FETCH_BUDGET = 5            # total seconds allowed for all fetches
DASHBOARD_REFRESH_INTERVAL = 5     # seconds between dashboard refreshes
MOMENTUM_POLL_INTERVAL = 5         # seconds between spot price samples

# ── Signal Thresholds ──────────────────────────────────────────────────
# Chainlink vs Spot divergence — "significant" threshold in USD
CHAINLINK_SPOT_DIVERGENCE_THRESHOLD = 15.0

# Order book imbalance thresholds
ORDERBOOK_BULLISH_RATIO = 1.5
ORDERBOOK_BEARISH_RATIO = 0.67
ORDERBOOK_DEPTH_PCT = 0.001  # 0.1% of price for relevant book depth

# Round number proximity threshold in USD
ROUND_NUMBER_DISTANCE_THRESHOLD = 200.0
ROUND_NUMBER_INTERVAL = 1000  # $1,000 increments

# Candle streak — trigger mean-reversion weight after N consecutive same
STREAK_THRESHOLD = 3

# Momentum — rate-of-change windows in seconds
MOMENTUM_WINDOW_SHORT = 60
MOMENTUM_WINDOW_LONG = 120

# CVD window in seconds
CVD_WINDOW = 120

# Liquidation window in seconds
LIQUIDATION_WINDOW = 120

# ── API Endpoints ──────────────────────────────────────────────────────
CHAINLINK_BTC_URL = "https://data.chain.link/streams/btc-usd"
BINANCE_SPOT_URL = "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSDT"
BINANCE_TRADES_URL = "https://api.binance.us/api/v3/trades?symbol=BTCUSDT&limit=500"
BINANCE_DEPTH_URL = "https://api.binance.us/api/v3/depth?symbol=BTCUSDT&limit=20"
COINGECKO_BTC_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com/markets"
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"

# ── Time-of-Day Regimes ───────────────────────────────────────────────
# Hours in ET (Eastern Time)
US_MARKET_OPEN_ET = 9.5    # 9:30 AM
US_MARKET_CLOSE_ET = 16.0  # 4:00 PM
ASIAN_OPEN_ET = 22.0       # 10:00 PM
ASIAN_CLOSE_ET = 6.0       # 6:00 AM

# ── Web Dashboard ──────────────────────────────────────────────────────
WEB_PORT = 8080
WEB_REFRESH_INTERVAL = 5  # seconds between frontend polls

# ── Live Trading (only used when TRADING_MODE=live) ───────────────────
POLYMARKET_PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER_ADDRESS = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
# Signature type: 0=EOA wallet, 1=Email/Magic wallet, 2=Browser proxy
POLYMARKET_SIGNATURE_TYPE = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))

# Risk limits for live trading (as percentages of starting balance)
LIVE_MAX_DAILY_LOSS_PCT = float(os.environ.get("LIVE_MAX_DAILY_LOSS_PCT", "10"))     # stop if down X% today
LIVE_MAX_POSITION_SIZE_PCT = float(os.environ.get("LIVE_MAX_POSITION_SIZE_PCT", "5"))  # max X% per trade
LIVE_MIN_BALANCE_PCT = float(os.environ.get("LIVE_MIN_BALANCE_PCT", "20"))           # stop if balance drops below X% of starting

