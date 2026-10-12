-- Append-only off-chain points. Unpaid activity counters.
-- There is no token. Rows are insert-only: UPDATE and DELETE abort.
--
-- tx_hash is caller-supplied text. On-chain rows store the transaction hash.
-- Off-chain rows store synthetic_tx_key() from rwa_score/points/ledger.py:
-- SHA-256 hex of wallet|action|issuer|YYYY-MM-DD (UTC).

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
    points INTEGER NOT NULL CHECK (typeof(points) = 'integer' AND points > 0),
    tx_hash TEXT NOT NULL CHECK (length(trim(tx_hash)) BETWEEN 1 AND 128),
    metadata TEXT,
    created_at TEXT NOT NULL,
    CONSTRAINT points_ledger_wallet_action_tx UNIQUE (wallet, action, tx_hash)
);

CREATE INDEX IF NOT EXISTS idx_points_ledger_wallet ON points_ledger (wallet);

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
