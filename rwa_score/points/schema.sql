-- Append-only off-chain points. Unpaid activity counters.
-- There is no token. Rows are insert-only: UPDATE and DELETE abort.
--
-- user_version 2. R1 files (no utc_day) are rebuilt by init_db. See ledger.py.
--
-- tx_hash is the row key. Off-chain rows store synthetic_tx_key():
-- 'rat-points-v1|' plus SHA-256 hex of wallet|action|issuer|YYYY-MM-DD (UTC).
-- Attest rows store a hash that matches ^0x[0-9a-f]{64}$ exactly. That is a
-- format check. Comparing the hash to an on-chain receipt is R3, before routes.
-- One attest hash can be stored once, across every wallet.
--
-- utc_day is set by the server. UNIQUE(wallet, action, utc_day) is the daily
-- cap: one row per wallet and action on that UTC day.

CREATE TABLE IF NOT EXISTS points_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL CHECK (
        length(wallet) = 42
        AND substr(wallet, 1, 2) = '0x'
        AND wallet = lower(wallet)
        AND wallet GLOB '0x[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'
    ),
    action TEXT NOT NULL CHECK (
        action IN ('score', 'attest', 'share', 'integration', 'bug')
    ),
    -- 500 is the per-row cap. Award amounts in rubric.py are placeholders pending Spencer.
    points INTEGER NOT NULL CHECK (
        typeof(points) = 'integer' AND points > 0 AND points <= 500
    ),
    tx_hash TEXT NOT NULL CHECK (length(trim(tx_hash)) BETWEEN 1 AND 128),
    metadata TEXT,
    created_at TEXT NOT NULL,
    utc_day TEXT NOT NULL CHECK (
        length(utc_day) = 10
        AND utc_day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
    ),
    CONSTRAINT points_ledger_wallet_action_tx UNIQUE (wallet, action, tx_hash),
    CONSTRAINT points_ledger_wallet_action_day UNIQUE (wallet, action, utc_day)
);

CREATE INDEX IF NOT EXISTS idx_points_ledger_wallet ON points_ledger (wallet);

-- One attest hash for the whole table, not one per wallet.
CREATE UNIQUE INDEX IF NOT EXISTS idx_points_ledger_attest_tx
ON points_ledger (tx_hash)
WHERE action = 'attest';

-- Keys already inserted. SQLite's REPLACE path deletes the old row without
-- firing DELETE triggers unless recursive_triggers is on, and that pragma is
-- per connection. A BEFORE INSERT check against this table aborts the rewrite
-- and rolls the delete back. UPDATE and DELETE on this table abort too.
CREATE TABLE IF NOT EXISTS points_ledger_keys (
    wallet TEXT NOT NULL,
    action TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    PRIMARY KEY (wallet, action, tx_hash)
);

CREATE TRIGGER IF NOT EXISTS points_ledger_block_update
BEFORE UPDATE ON points_ledger
BEGIN
    SELECT RAISE(ABORT, 'points ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS points_ledger_block_delete
BEFORE DELETE ON points_ledger
BEGIN
    SELECT RAISE(ABORT, 'points ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS points_ledger_block_reinsert
BEFORE INSERT ON points_ledger
WHEN EXISTS (
    SELECT 1 FROM points_ledger_keys
    WHERE wallet = NEW.wallet AND action = NEW.action AND tx_hash = NEW.tx_hash
)
BEGIN
    SELECT RAISE(ABORT, 'points ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS points_ledger_remember_key
AFTER INSERT ON points_ledger
BEGIN
    INSERT INTO points_ledger_keys (wallet, action, tx_hash)
    VALUES (NEW.wallet, NEW.action, NEW.tx_hash);
END;

CREATE TRIGGER IF NOT EXISTS points_ledger_keys_block_update
BEFORE UPDATE ON points_ledger_keys
BEGIN
    SELECT RAISE(ABORT, 'points ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS points_ledger_keys_block_delete
BEFORE DELETE ON points_ledger_keys
BEGIN
    SELECT RAISE(ABORT, 'points ledger is append-only');
END;
