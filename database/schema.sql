CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('Up', 'Down')),
    entry_odds DOUBLE PRECISION,
    position_size DOUBLE PRECISION,
    payout_rate DOUBLE PRECISION,
    confidence_level TEXT NOT NULL CHECK (confidence_level IN ('high', 'medium', 'skip')),
    outcome TEXT NOT NULL DEFAULT 'pending' CHECK (outcome IN ('win', 'loss', 'skip', 'pending')),
    pnl DOUBLE PRECISION DEFAULT 0.0,
    portfolio_balance_after DOUBLE PRECISION,
    trading_mode TEXT NOT NULL DEFAULT 'paper' CHECK (trading_mode IN ('paper', 'live')),
    skip_reason TEXT
);

-- Migration: add trading_mode column if it doesn't exist (for existing databases)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trades' AND column_name = 'trading_mode'
    ) THEN
        ALTER TABLE trades ADD COLUMN trading_mode TEXT DEFAULT 'paper';
        UPDATE trades SET trading_mode = 'paper' WHERE trading_mode IS NULL;
        ALTER TABLE trades ALTER COLUMN trading_mode SET NOT NULL;
        ALTER TABLE trades ADD CONSTRAINT trades_trading_mode_check
            CHECK (trading_mode IN ('paper', 'live'));
    END IF;
END $$;

-- Migration: add skip_reason column
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trades' AND column_name = 'skip_reason'
    ) THEN
        ALTER TABLE trades ADD COLUMN skip_reason TEXT;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS signals (
    id SERIAL PRIMARY KEY,
    trade_id INTEGER NOT NULL,
    chainlink_price DOUBLE PRECISION,
    spot_price DOUBLE PRECISION,
    chainlink_spot_divergence DOUBLE PRECISION,
    candle_position_dollars DOUBLE PRECISION,
    momentum_60s DOUBLE PRECISION,
    momentum_120s DOUBLE PRECISION,
    cvd DOUBLE PRECISION,
    order_book_ratio DOUBLE PRECISION,
    liquidation_signal DOUBLE PRECISION,
    round_number_distance DOUBLE PRECISION,
    time_regime TEXT,
    candle_streak TEXT,
    momentum_vote TEXT CHECK (momentum_vote IN ('Up', 'Down', 'ABSTAIN')),
    reversion_vote TEXT CHECK (reversion_vote IN ('Up', 'Down', 'ABSTAIN')),
    structure_vote TEXT CHECK (structure_vote IN ('Up', 'Down', 'ABSTAIN')),
    final_vote TEXT CHECK (final_vote IN ('Up', 'Down', 'ABSTAIN')),
    -- ML features added for future model training
    up_odds DOUBLE PRECISION,                -- Polymarket Up price at signal time
    down_odds DOUBLE PRECISION,              -- Polymarket Down price at signal time
    seconds_before_close DOUBLE PRECISION,   -- how many seconds before market close
    cvd_buy_volume DOUBLE PRECISION,         -- aggressive buy volume (BTC)
    cvd_sell_volume DOUBLE PRECISION,        -- aggressive sell volume (BTC)
    cvd_trade_count INTEGER,                 -- number of trades in CVD window
    ob_bid_volume DOUBLE PRECISION,          -- Binance bid volume near mid
    ob_ask_volume DOUBLE PRECISION,          -- Binance ask volume near mid
    liq_long_usd DOUBLE PRECISION,           -- long liquidation value (USD)
    liq_short_usd DOUBLE PRECISION,          -- short liquidation value (USD)
    poly_book_up_bids DOUBLE PRECISION,      -- Polymarket Up token bid depth
    poly_book_up_asks DOUBLE PRECISION,      -- Polymarket Up token ask depth
    poly_book_down_bids DOUBLE PRECISION,    -- Polymarket Down token bid depth
    poly_book_down_asks DOUBLE PRECISION,    -- Polymarket Down token ask depth
    poly_book_bias DOUBLE PRECISION,         -- net Polymarket book bias
    momentum_direction TEXT,                 -- "bullish", "bearish", "neutral"
    hour_of_day INTEGER,                     -- UTC hour (0-23)
    day_of_week INTEGER,                     -- 0=Mon, 6=Sun
    fill_price_per_share DOUBLE PRECISION,   -- actual fill price (live only)
    fill_slippage_pct DOUBLE PRECISION,      -- % slippage from quoted odds (live only)
    FOREIGN KEY (trade_id) REFERENCES trades (id)
);

-- Migration: add ML columns if they don't exist
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'signals' AND column_name = 'up_odds') THEN
        ALTER TABLE signals ADD COLUMN up_odds DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN down_odds DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN seconds_before_close DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN cvd_buy_volume DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN cvd_sell_volume DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN cvd_trade_count INTEGER;
        ALTER TABLE signals ADD COLUMN ob_bid_volume DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN ob_ask_volume DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN liq_long_usd DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN liq_short_usd DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN poly_book_up_bids DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN poly_book_up_asks DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN poly_book_down_bids DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN poly_book_down_asks DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN poly_book_bias DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN momentum_direction TEXT;
        ALTER TABLE signals ADD COLUMN hour_of_day INTEGER;
        ALTER TABLE signals ADD COLUMN day_of_week INTEGER;
        ALTER TABLE signals ADD COLUMN fill_price_per_share DOUBLE PRECISION;
        ALTER TABLE signals ADD COLUMN fill_slippage_pct DOUBLE PRECISION;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    balance DOUBLE PRECISION NOT NULL,
    total_trades INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    skips INTEGER NOT NULL DEFAULT 0,
    win_rate DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    daily_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0
);
