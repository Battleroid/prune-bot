-- prunebot schema, version 1.
-- Activity is stored as daily per-user counters rather than one row per message:
-- storage is bounded by (population x retention), independent of message volume.

CREATE TABLE IF NOT EXISTS activity_daily (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    day       INTEGER NOT NULL,            -- days since Unix epoch, UTC
    msg_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, day)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_activity_guild_day
    ON activity_daily(guild_id, day);

CREATE TABLE IF NOT EXISTS member_state (
    guild_id           INTEGER NOT NULL,
    user_id            INTEGER NOT NULL,
    state              TEXT    NOT NULL DEFAULT 'active',
                       -- active | flagged | pardoned | kicked | left
    joined_at          INTEGER,            -- current guild join, unix seconds
    first_seen_at      INTEGER NOT NULL,
    last_message_at    INTEGER,            -- derived, for display only
    flagged_at         INTEGER,            -- when the role went on
    warned_at          INTEGER,            -- when the warning was DELIVERED
    warn_delivery      TEXT,               -- 'dm' | 'channel' | NULL (undelivered)
    warning_channel_id INTEGER,
    warning_message_id INTEGER,
    final_warned_at    INTEGER,
    verified_at        INTEGER,            -- last /verify or button press
    pardoned_until     INTEGER,
    kicked_at          INTEGER,
    left_at            INTEGER,
    rejoin_count       INTEGER NOT NULL DEFAULT 0,
    flagged_on_leave   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_state_by_state
    ON member_state(guild_id, state);

CREATE TABLE IF NOT EXISTS whitelist (
    guild_id  INTEGER NOT NULL,
    kind      TEXT    NOT NULL,            -- 'user' | 'role'
    target_id INTEGER NOT NULL,
    added_by  INTEGER,
    added_at  INTEGER NOT NULL,
    reason    TEXT,
    PRIMARY KEY (guild_id, kind, target_id)
);

-- The durable record of everything the bot did. Survives channel deletion and
-- message purges, which is why it exists alongside the audit channel.
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER,
    action     TEXT    NOT NULL,
    reason     TEXT,
    actor_id   INTEGER,                    -- NULL = automatic
    dry_run    INTEGER NOT NULL DEFAULT 0,
    sweep_id   TEXT,
    created_at INTEGER NOT NULL,
    payload    TEXT                        -- JSON
);

CREATE INDEX IF NOT EXISTS ix_audit_guild_time
    ON audit_log(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_audit_user
    ON audit_log(guild_id, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS backfill_state (
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    last_message_id INTEGER,               -- resume cursor
    messages_seen   INTEGER NOT NULL DEFAULT 0,
    window_start    INTEGER,
    completed_at    INTEGER,
    error           TEXT,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS guild_meta (
    guild_id        INTEGER PRIMARY KEY,
    backfilled_at   INTEGER,
    backfill_covers INTEGER,               -- earliest ts the backfill actually reached
    last_sweep_at   INTEGER,
    last_sweep_id   TEXT
);

CREATE TABLE IF NOT EXISTS config_override (
    guild_id INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    value    TEXT    NOT NULL,             -- JSON-encoded scalar
    set_by   INTEGER,
    set_at   INTEGER NOT NULL,
    PRIMARY KEY (guild_id, key)
);
