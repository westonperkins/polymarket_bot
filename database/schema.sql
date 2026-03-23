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
    trading_mode TEXT NOT NULL DEFAULT 'paper' CHECK (trading_mode IN ('paper', 'live'))
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
    FOREIGN KEY (trade_id) REFERENCES trades (id)
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
