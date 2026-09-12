"""All SQL in the project lives here.

Callers deal in domain dataclasses and plain dicts; nothing outside this module
writes a query. That keeps the schema changeable in one place and lets the rest of
the code be tested against an in-memory database with no special casing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

from ..domain.models import MemberState, MemberStateRow, WhitelistEntry
from .migrations import migrate

_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)


def _now() -> int:
    return int(time.time())


def _row_to_member(row: aiosqlite.Row) -> MemberStateRow:
    return MemberStateRow(
        guild_id=row["guild_id"],
        user_id=row["user_id"],
        state=MemberState(row["state"]),
        joined_at=row["joined_at"],
        first_seen_at=row["first_seen_at"],
        last_message_at=row["last_message_at"],
        flagged_at=row["flagged_at"],
        warned_at=row["warned_at"],
        warn_delivery=row["warn_delivery"],
        warning_channel_id=row["warning_channel_id"],
        warning_message_id=row["warning_message_id"],
        final_warned_at=row["final_warned_at"],
        verified_at=row["verified_at"],
        pardoned_until=row["pardoned_until"],
        kicked_at=row["kicked_at"],
        left_at=row["left_at"],
        rejoin_count=row["rejoin_count"],
        flagged_on_leave=bool(row["flagged_on_leave"]),
    )


class Store:
    """Async SQLite store. One connection, serialized by aiosqlite's worker thread."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store.connect() has not been awaited")
        return self._db

    async def connect(self) -> tuple[int, int]:
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        for pragma in _PRAGMAS:
            await self._db.execute(pragma)
        versions = await migrate(self._db)
        return versions

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Store:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------- activity

    async def bump_activity(
        self, rows: Iterable[tuple[int, int, int, int]]
    ) -> int:
        """Add message counts. Rows are (guild_id, user_id, day, count).

        Upsert-add, so replaying the same batch double-counts -- callers must
        deduplicate (the backfill does this by scanning strictly before the bot's
        start time, which the live listener never covers).
        """
        batch = list(rows)
        if not batch:
            return 0
        await self.db.executemany(
            """
            INSERT INTO activity_daily (guild_id, user_id, day, msg_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, day)
            DO UPDATE SET msg_count = msg_count + excluded.msg_count
            """,
            batch,
        )
        await self.db.commit()
        return len(batch)

    async def window_counts(self, guild_id: int, since_day: int) -> dict[int, int]:
        """Total messages per user at or after `since_day`, for the whole guild."""
        async with self.db.execute(
            """
            SELECT user_id, SUM(msg_count) AS total
            FROM activity_daily
            WHERE guild_id = ? AND day >= ?
            GROUP BY user_id
            """,
            (guild_id, since_day),
        ) as cur:
            return {row["user_id"]: row["total"] for row in await cur.fetchall()}

    async def user_daily(
        self, guild_id: int, user_id: int, since_day: int
    ) -> dict[int, int]:
        async with self.db.execute(
            """
            SELECT day, msg_count FROM activity_daily
            WHERE guild_id = ? AND user_id = ? AND day >= ?
            ORDER BY day
            """,
            (guild_id, user_id, since_day),
        ) as cur:
            return {row["day"]: row["msg_count"] for row in await cur.fetchall()}

    async def last_active_day(self, guild_id: int, user_id: int) -> int | None:
        """Most recent day bucket with any messages, across the whole retention."""
        async with self.db.execute(
            "SELECT MAX(day) FROM activity_daily "
            "WHERE guild_id = ? AND user_id = ? AND msg_count > 0",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row and row[0] is not None else None

    async def prune_activity(self, before_day: int) -> int:
        cur = await self.db.execute(
            "DELETE FROM activity_daily WHERE day < ?", (before_day,)
        )
        await self.db.commit()
        return cur.rowcount or 0

    async def clear_activity(self, guild_id: int, user_id: int) -> int:
        cur = await self.db.execute(
            "DELETE FROM activity_daily WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.db.commit()
        return cur.rowcount or 0

    async def clear_guild_activity(self, guild_id: int) -> int:
        """Delete every activity bucket for one guild; returns rows removed.

        Only for a forced rebuild, which immediately rescans the history these
        counts came from. Used anywhere else it would make the whole server look
        inactive.
        """
        cur = await self.db.execute(
            "DELETE FROM activity_daily WHERE guild_id = ?", (guild_id,)
        )
        await self.db.commit()
        return cur.rowcount or 0

    # --------------------------------------------------------------- member state

    async def get_member(self, guild_id: int, user_id: int) -> MemberStateRow | None:
        async with self.db.execute(
            "SELECT * FROM member_state WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_member(row) if row else None

    async def all_members(self, guild_id: int) -> dict[int, MemberStateRow]:
        async with self.db.execute(
            "SELECT * FROM member_state WHERE guild_id = ?", (guild_id,)
        ) as cur:
            return {row["user_id"]: _row_to_member(row) for row in await cur.fetchall()}

    async def ensure_member(
        self, guild_id: int, user_id: int, *, joined_at: int | None = None
    ) -> MemberStateRow:
        """Create the row if absent; never clobbers existing state."""
        await self.db.execute(
            """
            INSERT INTO member_state (guild_id, user_id, first_seen_at, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            (guild_id, user_id, _now(), joined_at),
        )
        await self.db.commit()
        row = await self.get_member(guild_id, user_id)
        assert row is not None
        return row

    async def update_member(self, guild_id: int, user_id: int, **fields: Any) -> None:
        """Patch named columns of one member row, creating it if needed."""
        if not fields:
            return
        allowed = {
            "state",
            "joined_at",
            "last_message_at",
            "flagged_at",
            "warned_at",
            "warn_delivery",
            "warning_channel_id",
            "warning_message_id",
            "final_warned_at",
            "verified_at",
            "pardoned_until",
            "kicked_at",
            "left_at",
            "rejoin_count",
            "flagged_on_leave",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update unknown member_state columns: {sorted(unknown)}")

        values: dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, MemberState):
                values[key] = value.value
            elif isinstance(value, bool):
                values[key] = int(value)
            else:
                values[key] = value

        await self.db.execute(
            """
            INSERT INTO member_state (guild_id, user_id, first_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            (guild_id, user_id, _now()),
        )
        assignments = ", ".join(f"{name} = ?" for name in values)
        await self.db.execute(
            f"UPDATE member_state SET {assignments} WHERE guild_id = ? AND user_id = ?",
            (*values.values(), guild_id, user_id),
        )
        await self.db.commit()

    async def touch_last_message(
        self, pairs: Iterable[tuple[int, int, int]]
    ) -> None:
        """Record last_message_at for (guild_id, user_id, timestamp) triples."""
        batch = list(pairs)
        if not batch:
            return
        await self.db.executemany(
            """
            INSERT INTO member_state (guild_id, user_id, first_seen_at, last_message_at)
            VALUES (?1, ?2, ?3, ?3)
            ON CONFLICT(guild_id, user_id) DO UPDATE
              SET last_message_at = MAX(COALESCE(last_message_at, 0), excluded.last_message_at)
            """,
            [(g, u, ts) for g, u, ts in batch],
        )
        await self.db.commit()

    async def count_by_state(self, guild_id: int) -> dict[str, int]:
        async with self.db.execute(
            "SELECT state, COUNT(*) AS n FROM member_state WHERE guild_id = ? GROUP BY state",
            (guild_id,),
        ) as cur:
            return {row["state"]: row["n"] for row in await cur.fetchall()}

    # ------------------------------------------------------------------ whitelist

    async def whitelist_add(
        self,
        guild_id: int,
        kind: str,
        target_id: int,
        *,
        added_by: int | None = None,
        reason: str | None = None,
    ) -> bool:
        """True if newly added, False if it was already whitelisted."""
        if kind not in ("user", "role"):
            raise ValueError(f"whitelist kind must be 'user' or 'role', got {kind!r}")
        cur = await self.db.execute(
            """
            INSERT INTO whitelist (guild_id, kind, target_id, added_by, added_at, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, kind, target_id) DO NOTHING
            """,
            (guild_id, kind, target_id, added_by, _now(), reason),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def whitelist_remove(self, guild_id: int, kind: str, target_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM whitelist WHERE guild_id = ? AND kind = ? AND target_id = ?",
            (guild_id, kind, target_id),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def whitelist_entries(self, guild_id: int) -> list[WhitelistEntry]:
        async with self.db.execute(
            "SELECT * FROM whitelist WHERE guild_id = ? ORDER BY kind, added_at",
            (guild_id,),
        ) as cur:
            return [
                WhitelistEntry(
                    guild_id=row["guild_id"],
                    kind=row["kind"],
                    target_id=row["target_id"],
                    added_by=row["added_by"],
                    added_at=row["added_at"],
                    reason=row["reason"],
                )
                for row in await cur.fetchall()
            ]

    async def whitelist_ids(self, guild_id: int) -> tuple[set[int], set[int]]:
        """Returns (user_ids, role_ids)."""
        users: set[int] = set()
        roles: set[int] = set()
        async with self.db.execute(
            "SELECT kind, target_id FROM whitelist WHERE guild_id = ?", (guild_id,)
        ) as cur:
            for row in await cur.fetchall():
                (users if row["kind"] == "user" else roles).add(row["target_id"])
        return users, roles

    async def seed_whitelist(
        self, guild_id: int, *, user_ids: Sequence[int], role_ids: Sequence[int]
    ) -> int:
        """Apply config seeds. Idempotent, so it is safe to run on every start."""
        added = 0
        for uid in user_ids:
            added += await self.whitelist_add(
                guild_id, "user", uid, reason="seeded from config.toml"
            )
        for rid in role_ids:
            added += await self.whitelist_add(
                guild_id, "role", rid, reason="seeded from config.toml"
            )
        return added

    # ----------------------------------------------------------------- audit log

    async def add_audit(
        self,
        guild_id: int,
        action: str,
        *,
        user_id: int | None = None,
        reason: str | None = None,
        actor_id: int | None = None,
        dry_run: bool = False,
        sweep_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        cur = await self.db.execute(
            """
            INSERT INTO audit_log
                (guild_id, user_id, action, reason, actor_id, dry_run, sweep_id,
                 created_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                user_id,
                action,
                reason,
                actor_id,
                int(dry_run),
                sweep_id,
                _now(),
                json.dumps(payload) if payload else None,
            ),
        )
        await self.db.commit()
        return cur.lastrowid or 0

    async def user_history(
        self, guild_id: int, user_id: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        async with self.db.execute(
            """
            SELECT * FROM audit_log
            WHERE guild_id = ? AND user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (guild_id, user_id, limit),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def recent_audit(
        self, guild_id: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        async with self.db.execute(
            "SELECT * FROM audit_log WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    # ------------------------------------------------------------------ backfill

    async def backfill_cursor(
        self, guild_id: int, channel_id: int
    ) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM backfill_state WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def save_backfill_cursor(
        self,
        guild_id: int,
        channel_id: int,
        *,
        last_message_id: int | None,
        messages_seen: int,
        window_start: int | None = None,
        completed: bool = False,
        error: str | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO backfill_state
                (guild_id, channel_id, last_message_id, messages_seen, window_start,
                 completed_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                last_message_id = excluded.last_message_id,
                messages_seen   = excluded.messages_seen,
                window_start    = COALESCE(excluded.window_start, backfill_state.window_start),
                completed_at    = excluded.completed_at,
                error           = excluded.error
            """,
            (
                guild_id,
                channel_id,
                last_message_id,
                messages_seen,
                window_start,
                _now() if completed else None,
                error,
            ),
        )
        await self.db.commit()

    async def reset_backfill(self, guild_id: int) -> None:
        await self.db.execute("DELETE FROM backfill_state WHERE guild_id = ?", (guild_id,))
        await self.db.execute(
            "UPDATE guild_meta SET backfilled_at = NULL, backfill_covers = NULL "
            "WHERE guild_id = ?",
            (guild_id,),
        )
        await self.db.commit()

    # ---------------------------------------------------------------- guild meta

    async def guild_meta(self, guild_id: int) -> dict[str, Any]:
        await self.db.execute(
            "INSERT INTO guild_meta (guild_id) VALUES (?) ON CONFLICT DO NOTHING",
            (guild_id,),
        )
        await self.db.commit()
        async with self.db.execute(
            "SELECT * FROM guild_meta WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else {"guild_id": guild_id}

    async def set_backfilled(self, guild_id: int, *, covers: int) -> None:
        await self.db.execute(
            """
            INSERT INTO guild_meta (guild_id, backfilled_at, backfill_covers)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                backfilled_at = excluded.backfilled_at,
                backfill_covers = excluded.backfill_covers
            """,
            (guild_id, _now(), covers),
        )
        await self.db.commit()

    async def set_last_sweep(self, guild_id: int, sweep_id: str) -> None:
        await self.db.execute(
            """
            INSERT INTO guild_meta (guild_id, last_sweep_at, last_sweep_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                last_sweep_at = excluded.last_sweep_at,
                last_sweep_id = excluded.last_sweep_id
            """,
            (guild_id, _now(), sweep_id),
        )
        await self.db.commit()

    # ----------------------------------------------------------- config overrides

    async def overrides(self, guild_id: int) -> dict[str, Any]:
        async with self.db.execute(
            "SELECT key, value FROM config_override WHERE guild_id = ?", (guild_id,)
        ) as cur:
            out: dict[str, Any] = {}
            for row in await cur.fetchall():
                try:
                    out[row["key"]] = json.loads(row["value"])
                except json.JSONDecodeError:
                    continue  # a corrupt row must not stop the bot from starting
            return out

    async def set_override(
        self, guild_id: int, key: str, value: Any, *, set_by: int | None = None
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO config_override (guild_id, key, value, set_by, set_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, key) DO UPDATE SET
                value = excluded.value, set_by = excluded.set_by, set_at = excluded.set_at
            """,
            (guild_id, key, json.dumps(value), set_by, _now()),
        )
        await self.db.commit()

    async def clear_override(self, guild_id: int, key: str) -> bool:
        cur = await self.db.execute(
            "DELETE FROM config_override WHERE guild_id = ? AND key = ?", (guild_id, key)
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    # --------------------------------------------------------------------- backup

    async def backup_to(self, destination: str | Path) -> Path:
        """VACUUM INTO a new file. Safe against a live WAL database."""
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(f"refusing to overwrite existing backup {target}")
        await self.db.execute("VACUUM INTO ?", (str(target),))
        await self.db.commit()
        return target
