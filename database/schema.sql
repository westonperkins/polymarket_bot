CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('Up', 'Down')),
    entry_odds REAL,
    position_size REAL,
    payout_rate REAL,
    confidence_level TEXT NOT NULL CHECK (confidence_level IN ('high', 'medium', 'skip')),
    outcome TEXT NOT NULL DEFAULT 'pending' CHECK (outcome IN ('win', 'loss', 'skip', 'pending')),
    pnl REAL DEFAULT 0.0,
    portfolio_balance_after REAL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    chainlink_price REAL,
    spot_price REAL,
    chainlink_spot_divergence REAL,
    candle_position_dollars REAL,
    momentum_60s REAL,
    momentum_120s REAL,
    cvd REAL,
    order_book_ratio REAL,
    liquidation_signal REAL,
    round_number_distance REAL,
    time_regime TEXT,
    candle_streak TEXT,
    momentum_vote TEXT CHECK (momentum_vote IN ('Up', 'Down', 'ABSTAIN')),
    reversion_vote TEXT CHECK (reversion_vote IN ('Up', 'Down', 'ABSTAIN')),
    structure_vote TEXT CHECK (structure_vote IN ('Up', 'Down', 'ABSTAIN')),
    final_vote TEXT CHECK (final_vote IN ('Up', 'Down', 'ABSTAIN')),
    FOREIGN KEY (trade_id) REFERENCES trades (id)
);

CREATE TABLE IF NOT EXISTS portfolio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    balance REAL NOT NULL,
    total_trades INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    skips INTEGER NOT NULL DEFAULT 0,
    win_rate REAL NOT NULL DEFAULT 0.0,
    daily_pnl REAL NOT NULL DEFAULT 0.0
);
