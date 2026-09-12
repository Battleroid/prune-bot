from __future__ import annotations

import pytest

from prunebot.db.migrations import SCHEMA_VERSION, current_version
from prunebot.domain.models import MemberState

from .conftest import GUILD_ID


async def test_connect_creates_schema_at_current_version(store):
    assert await current_version(store.db) == SCHEMA_VERSION


async def test_migrate_is_idempotent(store, tmp_path):
    from prunebot.db.store import Store

    await store.close()
    reopened = Store(tmp_path / "test.db")
    start, end = await reopened.connect()
    assert (start, end) == (SCHEMA_VERSION, SCHEMA_VERSION)
    await reopened.close()


async def test_refuses_a_database_from_a_newer_build(store):
    from prunebot.db.migrations import migrate

    await store.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    with pytest.raises(RuntimeError, match="newer database"):
        await migrate(store.db)


# ------------------------------------------------------------------- activity


async def test_bump_activity_accumulates_instead_of_replacing(store):
    await store.bump_activity([(GUILD_ID, 1, 100, 3)])
    await store.bump_activity([(GUILD_ID, 1, 100, 4)])
    assert await store.window_counts(GUILD_ID, 100) == {1: 7}


async def test_window_counts_respects_the_start_day(store):
    await store.bump_activity(
        [(GUILD_ID, 1, 100, 5), (GUILD_ID, 1, 105, 2), (GUILD_ID, 2, 105, 9)]
    )
    assert await store.window_counts(GUILD_ID, 100) == {1: 7, 2: 9}
    assert await store.window_counts(GUILD_ID, 105) == {1: 2, 2: 9}
    assert await store.window_counts(GUILD_ID, 200) == {}


async def test_window_counts_are_scoped_per_guild(store):
    await store.bump_activity([(GUILD_ID, 1, 100, 5), (999, 1, 100, 50)])
    assert await store.window_counts(GUILD_ID, 100) == {1: 5}
    assert await store.window_counts(999, 100) == {1: 50}


async def test_empty_batch_is_a_no_op(store):
    assert await store.bump_activity([]) == 0


async def test_prune_activity_deletes_only_old_buckets(store):
    await store.bump_activity(
        [(GUILD_ID, 1, 10, 1), (GUILD_ID, 1, 50, 1), (GUILD_ID, 1, 99, 1)]
    )
    removed = await store.prune_activity(50)
    assert removed == 1
    assert await store.window_counts(GUILD_ID, 0) == {1: 2}


async def test_user_daily_returns_per_day_buckets(store):
    await store.bump_activity([(GUILD_ID, 1, 100, 2), (GUILD_ID, 1, 101, 3)])
    assert await store.user_daily(GUILD_ID, 1, 100) == {100: 2, 101: 3}


# --------------------------------------------------------------- member state


async def test_ensure_member_is_idempotent_and_preserves_state(store):
    await store.ensure_member(GUILD_ID, 7, joined_at=1000)
    await store.update_member(GUILD_ID, 7, state=MemberState.FLAGGED, flagged_at=555)
    again = await store.ensure_member(GUILD_ID, 7, joined_at=2000)
    assert again.state is MemberState.FLAGGED
    assert again.flagged_at == 555
    assert again.joined_at == 1000  # not clobbered


async def test_update_member_creates_the_row_when_missing(store):
    await store.update_member(GUILD_ID, 42, state=MemberState.FLAGGED)
    row = await store.get_member(GUILD_ID, 42)
    assert row is not None and row.state is MemberState.FLAGGED


async def test_update_member_rejects_unknown_columns(store):
    with pytest.raises(ValueError, match="unknown member_state columns"):
        await store.update_member(GUILD_ID, 1, not_a_column=1)


async def test_booleans_round_trip(store):
    await store.update_member(GUILD_ID, 1, flagged_on_leave=True)
    row = await store.get_member(GUILD_ID, 1)
    assert row.flagged_on_leave is True


async def test_touch_last_message_keeps_the_latest_timestamp(store):
    await store.touch_last_message([(GUILD_ID, 1, 500)])
    await store.touch_last_message([(GUILD_ID, 1, 400)])  # older, must not win
    row = await store.get_member(GUILD_ID, 1)
    assert row.last_message_at == 500


async def test_count_by_state(store):
    await store.update_member(GUILD_ID, 1, state=MemberState.FLAGGED)
    await store.update_member(GUILD_ID, 2, state=MemberState.FLAGGED)
    await store.update_member(GUILD_ID, 3, state=MemberState.ACTIVE)
    assert await store.count_by_state(GUILD_ID) == {"flagged": 2, "active": 1}


# ------------------------------------------------------------------ whitelist


async def test_whitelist_add_is_idempotent(store):
    assert await store.whitelist_add(GUILD_ID, "user", 5) is True
    assert await store.whitelist_add(GUILD_ID, "user", 5) is False
    entries = await store.whitelist_entries(GUILD_ID)
    assert len(entries) == 1


async def test_whitelist_separates_users_from_roles(store):
    await store.whitelist_add(GUILD_ID, "user", 5)
    await store.whitelist_add(GUILD_ID, "role", 5)  # same id, different kind
    users, roles = await store.whitelist_ids(GUILD_ID)
    assert users == {5} and roles == {5}


async def test_whitelist_remove_reports_whether_it_did_anything(store):
    await store.whitelist_add(GUILD_ID, "user", 5)
    assert await store.whitelist_remove(GUILD_ID, "user", 5) is True
    assert await store.whitelist_remove(GUILD_ID, "user", 5) is False


async def test_whitelist_rejects_a_bogus_kind(store):
    with pytest.raises(ValueError, match="must be 'user' or 'role'"):
        await store.whitelist_add(GUILD_ID, "channel", 5)


async def test_seed_whitelist_only_adds_what_is_missing(store):
    assert await store.seed_whitelist(GUILD_ID, user_ids=[1, 2], role_ids=[3]) == 3
    assert await store.seed_whitelist(GUILD_ID, user_ids=[1, 2], role_ids=[3]) == 0


async def test_seeding_never_removes_a_command_added_entry(store):
    """The database is the source of truth; config seeds must not prune it."""
    await store.whitelist_add(GUILD_ID, "user", 99, reason="added via command")
    await store.seed_whitelist(GUILD_ID, user_ids=[1], role_ids=[])
    users, _ = await store.whitelist_ids(GUILD_ID)
    assert 99 in users


# ------------------------------------------------------------------ audit log


async def test_audit_rows_record_dry_run_and_actor(store):
    await store.add_audit(
        GUILD_ID, "kick", user_id=7, actor_id=3, dry_run=True, payload={"k": "v"}
    )
    rows = await store.user_history(GUILD_ID, 7)
    assert len(rows) == 1
    assert rows[0]["action"] == "kick"
    assert rows[0]["dry_run"] == 1
    assert rows[0]["actor_id"] == 3


async def test_user_history_is_newest_first(store):
    for i in range(3):
        await store.add_audit(GUILD_ID, f"action{i}", user_id=7)
    rows = await store.user_history(GUILD_ID, 7)
    assert [r["action"] for r in rows] == ["action2", "action1", "action0"]


# ------------------------------------------------------- guild meta & overrides


async def test_guild_meta_autocreates(store):
    meta = await store.guild_meta(GUILD_ID)
    assert meta["guild_id"] == GUILD_ID
    assert meta["backfilled_at"] is None


async def test_set_backfilled_then_reset(store):
    await store.set_backfilled(GUILD_ID, covers=12345)
    assert (await store.guild_meta(GUILD_ID))["backfill_covers"] == 12345
    await store.reset_backfill(GUILD_ID)
    assert (await store.guild_meta(GUILD_ID))["backfilled_at"] is None


async def test_overrides_round_trip_json_scalars(store):
    await store.set_override(GUILD_ID, "safety.dry_run", False)
    await store.set_override(GUILD_ID, "activity.window_days", 45)
    assert await store.overrides(GUILD_ID) == {
        "safety.dry_run": False,
        "activity.window_days": 45,
    }


async def test_a_corrupt_override_row_is_skipped_not_fatal(store):
    await store.set_override(GUILD_ID, "safety.dry_run", True)
    await store.db.execute(
        "UPDATE config_override SET value = 'not json' WHERE key = 'safety.dry_run'"
    )
    await store.db.commit()
    assert await store.overrides(GUILD_ID) == {}


async def test_backfill_cursor_round_trip(store):
    await store.save_backfill_cursor(
        GUILD_ID, 55, last_message_id=900, messages_seen=100, window_start=1
    )
    cursor = await store.backfill_cursor(GUILD_ID, 55)
    assert cursor["last_message_id"] == 900
    assert cursor["completed_at"] is None

    await store.save_backfill_cursor(
        GUILD_ID, 55, last_message_id=950, messages_seen=150, completed=True
    )
    cursor = await store.backfill_cursor(GUILD_ID, 55)
    assert cursor["completed_at"] is not None
    assert cursor["window_start"] == 1  # preserved across updates


async def test_backup_writes_a_readable_copy(store, tmp_path):
    await store.bump_activity([(GUILD_ID, 1, 100, 5)])
    target = await store.backup_to(tmp_path / "backups" / "copy.db")
    assert target.exists() and target.stat().st_size > 0

    with pytest.raises(FileExistsError):
        await store.backup_to(target)



async def test_last_active_day_is_the_latest_bucket_with_messages(store):
    await store.bump_activity(
        [(GUILD_ID, 1, 100, 2), (GUILD_ID, 1, 140, 1), (GUILD_ID, 2, 150, 1)]
    )
    assert await store.last_active_day(GUILD_ID, 1) == 140
    assert await store.last_active_day(GUILD_ID, 3) is None
