"""End-to-end sweeps against FakeGateway and a real SQLite database.

No Discord connection, but every layer above the wire is exercised: reconcile,
snapshot building, planning, caps, the breaker, and the executor.
"""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from prunebot.config import Config
from prunebot.domain.models import Action, MemberState
from prunebot.services.sweep import (
    build_sweep_plan,
    clear_flag,
    execute_plan,
    run_sweep,
    status_for,
)

from .conftest import GUILD_ID, NOW, FakeGateway, days_ago, make_info

BASE_CONFIG: dict = {
    "bot": {"guild_ids": [GUILD_ID], "timezone": "UTC"},
    "safety": {"dry_run": False, "action_delay_seconds": 0.0},
    "flagging": {"inactive_role_id": 456},
    "audit": {"channel_id": 789},
}


def make_config(**sections) -> Config:
    data = copy.deepcopy(BASE_CONFIG)
    for name, values in sections.items():
        data.setdefault(name, {}).update(values)
    return Config.model_validate(data)


async def seed_backfilled(store, *, days_covered: int = 400):
    """Mark the guild as backfilled far enough back to satisfy the gate."""
    await store.set_backfilled(
        GUILD_ID, covers=int((NOW - timedelta(days=days_covered)).timestamp())
    )


async def with_activity(store, user_id: int, count: int, *, day_offset: int = 0):
    from prunebot.domain.windows import day_of

    await store.bump_activity([(GUILD_ID, user_id, day_of(NOW) - day_offset, count)])


@pytest.fixture
async def env(store):
    await seed_backfilled(store)
    return store


# --------------------------------------------------------------------- happy path


async def test_inactive_member_is_flagged_warned_and_recorded(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])

    result = await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert result.plan.count(Action.FLAG) == 1
    assert gw.calls_of("add_role") == [member.user_id]
    assert gw.calls_of("warn:initial") == [member.user_id]

    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.FLAGGED
    assert row.flagged_at is not None
    assert row.warn_delivery == "dm"
    assert row.warned_at is not None


async def test_active_member_is_untouched(env):
    store = env
    member = make_info()
    await with_activity(store, member.user_id, 5)
    gw = FakeGateway([member])

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert gw.mutations == []
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row is None or row.state is MemberState.ACTIVE


async def test_overdue_member_is_kicked_and_recorded(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])
    await store.update_member(
        GUILD_ID,
        member.user_id,
        state=MemberState.FLAGGED,
        flagged_at=int(days_ago(120).timestamp()),
        warned_at=int(days_ago(120).timestamp()),
        warn_delivery="dm",
    )
    gw.set_members([make_info(user_id=member.user_id, has_inactive_role=True)])

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert gw.calls_of("kick") == [member.user_id]
    assert gw.calls_of("warn:pre_kick") == [member.user_id]
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.KICKED
    assert row.kicked_at is not None


# ------------------------------------------------------------------------ dry run


async def test_dry_run_changes_nothing_in_discord_or_the_database(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])

    result = await run_sweep(
        gateway=gw, store=store, config=make_config(), now=NOW, dry_run=True
    )

    assert result.plan.count(Action.FLAG) == 1  # it still says what it would do
    assert gw.mutations == []
    assert gw.calls_of("warn:initial") == []
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row is None or row.state is MemberState.ACTIVE


async def test_dry_run_is_repeatable_and_does_not_drift(env):
    store = env
    gw = FakeGateway([make_info() for _ in range(3)])
    cfg = make_config()

    first = await run_sweep(gateway=gw, store=store, config=cfg, now=NOW, dry_run=True)
    second = await run_sweep(gateway=gw, store=store, config=cfg, now=NOW, dry_run=True)

    assert first.plan.count(Action.FLAG) == second.plan.count(Action.FLAG) == 3
    assert gw.mutations == []


async def test_dry_run_still_writes_audit_rows_marked_as_such(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW, dry_run=True)

    rows = await store.user_history(GUILD_ID, member.user_id)
    assert rows and all(r["dry_run"] == 1 for r in rows)


async def test_preview_matches_what_a_real_run_executes(env):
    """The property that makes /prune preview trustworthy."""
    store = env
    members = [make_info() for _ in range(6)]
    for m in members[:3]:
        await with_activity(store, m.user_id, 4)
    gw = FakeGateway(members)
    cfg = make_config()

    preview = await build_sweep_plan(gateway=gw, store=store, config=cfg, now=NOW)
    predicted = sorted(p.user_id for p in preview.plan.of(Action.FLAG))

    result = await run_sweep(gateway=gw, store=store, config=cfg, now=NOW)
    actually_flagged = sorted(gw.calls_of("add_role"))

    assert predicted == actually_flagged
    assert result.plan.count(Action.FLAG) == len(predicted) == 3


# ------------------------------------------------------------- warning delivery


async def test_blocked_dm_does_not_crash_the_sweep(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])
    gw.dm_blocked.add(member.user_id)

    result = await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert gw.calls_of("add_role") == [member.user_id]
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.FLAGGED
    assert row.warn_delivery is None
    assert row.warned_at is None
    assert result.execution.warnings_undelivered == 1


async def test_undelivered_warning_pauses_the_kick_when_required(env):
    """require_warning_before_kick = true: closed DMs are flagged but never kicked."""
    store = env
    member = make_info(has_inactive_role=True)
    gw = FakeGateway([member])
    gw.dm_blocked.add(member.user_id)
    await store.update_member(
        GUILD_ID,
        member.user_id,
        state=MemberState.FLAGGED,
        flagged_at=int(days_ago(300).timestamp()),
        warned_at=None,
        warn_delivery=None,
    )
    cfg = make_config(safety={"require_warning_before_kick": True})

    await run_sweep(gateway=gw, store=store, config=cfg, now=NOW)

    assert gw.calls_of("kick") == []
    assert gw.calls_of("warn:initial") == [member.user_id]  # retried instead


async def test_undelivered_warning_still_kicks_by_default(env):
    """The shipped default, matching the DM-only choice."""
    store = env
    member = make_info(has_inactive_role=True)
    gw = FakeGateway([member])
    gw.dm_blocked.add(member.user_id)
    await store.update_member(
        GUILD_ID,
        member.user_id,
        state=MemberState.FLAGGED,
        flagged_at=int(days_ago(300).timestamp()),
        warned_at=None,
        warn_delivery=None,
    )
    cfg = make_config(safety={"require_warning_before_kick": False})

    await run_sweep(gateway=gw, store=store, config=cfg, now=NOW)

    assert gw.calls_of("kick") == [member.user_id]


async def test_retried_warning_that_lands_starts_the_clock(env):
    store = env
    member = make_info(has_inactive_role=True)
    gw = FakeGateway([member])
    await store.update_member(
        GUILD_ID,
        member.user_id,
        state=MemberState.FLAGGED,
        flagged_at=int(days_ago(300).timestamp()),
        warned_at=None,
        warn_delivery=None,
    )
    cfg = make_config(safety={"require_warning_before_kick": True})

    await run_sweep(gateway=gw, store=store, config=cfg, now=NOW)

    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.warn_delivery == "dm"
    assert row.warned_at is not None


# ------------------------------------------------------------ safety mechanisms


async def test_breaker_abort_executes_nothing_at_all(env):
    store = env
    members = [make_info() for _ in range(40)]
    for m in members[:10]:
        await with_activity(store, m.user_id, 5)
    gw = FakeGateway(members)

    result = await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert result.plan.aborted
    assert gw.mutations == []
    assert result.execution is None
    assert any("aborted" in line.lower() for line in gw.audit)


async def test_missing_backfill_downgrades_a_live_run_to_a_simulation(store):
    # Deliberately not seeded: guild_meta.backfilled_at is NULL.
    member = make_info()
    gw = FakeGateway([member])

    result = await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert result.dry_run is True
    assert gw.mutations == []
    assert "backfill" in result.bundle.blocked_reason


async def test_shallow_backfill_also_blocks_acting(store):
    await seed_backfilled(store, days_covered=5)  # window is 30 days
    gw = FakeGateway([make_info()])

    result = await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert result.dry_run is True
    assert "window" in result.bundle.blocked_reason


async def test_caps_are_enforced_during_execution(env):
    store = env
    members = [make_info() for _ in range(200)]
    for m in members[:190]:
        await with_activity(store, m.user_id, 5)
    gw = FakeGateway(members)
    cfg = make_config(safety={"max_flags_per_sweep": 4})

    result = await run_sweep(gateway=gw, store=store, config=cfg, now=NOW)

    assert len(gw.calls_of("add_role")) == 4
    assert Action.FLAG in result.plan.capped
    assert len(result.plan.deferred) == 6


async def test_a_failed_role_add_is_recorded_and_does_not_flag_the_member(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])
    gw.role_forbidden.add(member.user_id)

    result = await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert result.execution.failed[Action.FLAG] == 1
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row is None or row.state is not MemberState.FLAGGED


# ------------------------------------------------------------------- exemptions


async def test_whitelisted_member_is_never_flagged(env):
    store = env
    member = make_info()
    await store.whitelist_add(GUILD_ID, "user", member.user_id)
    gw = FakeGateway([member])

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert gw.mutations == []


async def test_whitelisted_role_exempts_its_holders(env):
    store = env
    role_id = 999
    await store.whitelist_add(GUILD_ID, "role", role_id)
    holder = make_info(role_ids=frozenset({role_id}))
    other = make_info()
    gw = FakeGateway([holder, other])

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert gw.calls_of("add_role") == [other.user_id]


async def test_whitelisting_a_flagged_member_unflags_them_on_the_next_sweep(env):
    store = env
    member = make_info(has_inactive_role=True)
    await store.update_member(
        GUILD_ID, member.user_id, state=MemberState.FLAGGED, flagged_at=1
    )
    await store.whitelist_add(GUILD_ID, "user", member.user_id)
    gw = FakeGateway([member])

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert gw.calls_of("remove_role") == [member.user_id]


async def test_clear_flag_acts_immediately_without_waiting_for_a_sweep(env):
    store = env
    member = make_info(has_inactive_role=True)
    await store.update_member(
        GUILD_ID, member.user_id, state=MemberState.FLAGGED, flagged_at=1
    )
    gw = FakeGateway([member])

    ok = await clear_flag(
        gateway=gw,
        store=store,
        config=make_config(),
        user_id=member.user_id,
        reason="whitelisted",
    )

    assert ok
    assert gw.calls_of("remove_role") == [member.user_id]
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.ACTIVE
    assert row.verified_at is None  # clearing a flag grants no grace of its own


async def test_bots_and_the_owner_are_never_touched(env):
    store = env
    gw = FakeGateway(
        [make_info(is_bot=True), make_info(is_owner=True), make_info()]
    )
    plan = (await build_sweep_plan(gateway=gw, store=store, config=make_config(), now=NOW)).plan
    assert plan.skipped.get("bot") == 1
    assert plan.skipped.get("server_owner") == 1
    assert plan.count(Action.FLAG) == 1


# ----------------------------------------------------------------- reconciliation


async def test_a_hand_applied_role_is_adopted(env):
    store = env
    member = make_info(has_inactive_role=True)
    await with_activity(store, member.user_id, 50)  # active, so no other action
    gw = FakeGateway([member])

    bundle = await build_sweep_plan(gateway=gw, store=store, config=make_config(), now=NOW)

    assert bundle.reconciled.adopted == [member.user_id]
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.FLAGGED


async def test_a_hand_removed_role_is_treated_as_a_pardon(env):
    store = env
    member = make_info(has_inactive_role=False)
    await store.update_member(
        GUILD_ID, member.user_id, state=MemberState.FLAGGED, flagged_at=1
    )
    gw = FakeGateway([member])

    bundle = await build_sweep_plan(gateway=gw, store=store, config=make_config(), now=NOW)

    assert bundle.reconciled.pardoned == [member.user_id]
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.PARDONED
    assert row.pardoned_until is not None


async def test_a_pardoned_member_is_not_immediately_reflagged(env):
    store = env
    member = make_info(has_inactive_role=False)
    await store.update_member(
        GUILD_ID, member.user_id, state=MemberState.FLAGGED, flagged_at=1
    )
    gw = FakeGateway([member])

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert gw.calls_of("add_role") == []


# ------------------------------------------------------------------------ status


async def test_status_reports_counts_and_the_pending_decision(env):
    store = env
    member = make_info()
    await with_activity(store, member.user_id, 3)
    gw = FakeGateway([member])

    snapshot, decision = await status_for(
        gateway=gw, store=store, config=make_config(), user_id=member.user_id, now=NOW
    )

    assert snapshot.message_count == 3
    assert decision.action is Action.NONE


async def test_status_for_an_unknown_member_returns_nothing(env):
    gw = FakeGateway([])
    snapshot, decision = await status_for(
        gateway=gw, store=env, config=make_config(), user_id=1, now=NOW
    )
    assert snapshot is None and decision is None


async def test_execute_plan_can_be_replayed_from_a_stored_bundle(env):
    """Plan and execute are genuinely separate phases."""
    store = env
    member = make_info()
    gw = FakeGateway([member])
    cfg = make_config()

    bundle = await build_sweep_plan(gateway=gw, store=store, config=cfg, now=NOW)
    assert gw.mutations == []  # planning alone touches nothing

    await execute_plan(bundle=bundle, gateway=gw, store=store, config=cfg, now=NOW)
    assert gw.calls_of("add_role") == [member.user_id]


# --------------------------------------------------- backfill honesty & warnings


async def test_a_preview_says_so_when_the_backfill_is_incomplete(store):
    """Silence here is how you talk yourself into acting on bad data."""
    gw = FakeGateway([make_info()])
    bundle = await build_sweep_plan(
        gateway=gw, store=store, config=make_config(), now=NOW, dry_run=True
    )
    assert bundle.blocked_reason is not None
    assert "backfill" in bundle.blocked_reason


async def test_a_completed_backfill_clears_the_warning(env):
    gw = FakeGateway([make_info()])
    bundle = await build_sweep_plan(
        gateway=gw, store=env, config=make_config(), now=NOW, dry_run=True
    )
    assert bundle.blocked_reason is None


async def test_only_a_forced_downgrade_is_reported_as_one(env):
    """An intentional dry run must not claim it was downgraded."""
    gw = FakeGateway([make_info()])
    result = await run_sweep(
        gateway=gw, store=env, config=make_config(), now=NOW, dry_run=True
    )
    assert result.dry_run is True
    assert result.downgraded is False


async def test_a_blocked_live_run_is_marked_as_downgraded(store):
    gw = FakeGateway([make_info()])
    result = await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)
    assert result.dry_run is True
    assert result.downgraded is True
    assert gw.mutations == []


# ------------------------------------------------------- public announcements


async def test_flagging_posts_a_public_announcement(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert gw.calls_of("announce") == [member.user_id]


async def test_a_dry_run_never_announces(env):
    """Telling members they were flagged when nothing happened is worse than
    saying nothing at all."""
    store = env
    gw = FakeGateway([make_info()])

    await run_sweep(
        gateway=gw, store=store, config=make_config(), now=NOW, dry_run=True
    )

    assert gw.calls_of("announce") == []


async def test_a_failed_announcement_does_not_undo_the_flag(env):
    """The role is already on by then; failing to announce must not block it."""
    store = env
    member = make_info()
    gw = FakeGateway([member])
    gw.announce_forbidden.add(member.user_id)

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert gw.calls_of("add_role") == [member.user_id]
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.FLAGGED


async def test_nothing_is_announced_when_a_member_is_not_flagged(env):
    store = env
    member = make_info()
    await with_activity(store, member.user_id, 5)
    gw = FakeGateway([member])

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW)

    assert gw.calls_of("announce") == []


# -------------------------------------------------------------------- pardon-all


async def _flag(store, member, *, flagged_days_ago=10):
    stamp = int(days_ago(flagged_days_ago).timestamp())
    await store.update_member(
        GUILD_ID,
        member.user_id,
        state=MemberState.FLAGGED,
        flagged_at=stamp,
        warned_at=stamp,
        warn_delivery="dm",
    )


async def test_pardon_all_finds_flagged_members_by_role_or_by_row(env):
    """Drift in either direction is still covered."""
    from prunebot.services.sweep import flagged_members

    store = env
    both = make_info(has_inactive_role=True)
    role_only = make_info(has_inactive_role=True)  # wearing it, db says active
    row_only = make_info(has_inactive_role=False)  # db says flagged, role gone
    clean = make_info()
    await _flag(store, both)
    await _flag(store, row_only)
    gw = FakeGateway([both, role_only, row_only, clean])

    manageable, unmanageable = await flagged_members(gateway=gw, store=store)

    assert set(manageable) == {both.user_id, role_only.user_id, row_only.user_id}
    assert unmanageable == []


async def test_pardon_all_removes_the_role_and_protects_everyone_it_names(env):
    from prunebot.services.sweep import flagged_members, pardon_all

    store = env
    flagged = [make_info(has_inactive_role=True) for _ in range(3)]
    for member in flagged:
        await _flag(store, member)
    bystander = make_info()
    gw = FakeGateway(flagged + [bystander])
    targets, unmanageable = await flagged_members(gateway=gw, store=store)

    result = await pardon_all(
        gateway=gw,
        store=store,
        config=make_config(),
        targets=targets,
        days=30,
        reason="amnesty",
        actor_id=42,
        unmanageable=unmanageable,
        now=NOW,
    )

    ids = sorted(m.user_id for m in flagged)
    assert sorted(result.pardoned) == ids
    assert sorted(gw.calls_of("remove_role")) == ids
    for member in flagged:
        row = await store.get_member(GUILD_ID, member.user_id)
        assert row.state is MemberState.PARDONED
        assert row.pardoned_until == int(NOW.timestamp()) + 30 * 86400
    assert bystander.user_id not in gw.calls_of("remove_role")


async def test_pardoned_members_are_not_reflagged_until_the_pardon_ends(env):
    from prunebot.services.sweep import pardon_all

    store = env
    member = make_info(has_inactive_role=True)
    await _flag(store, member)
    gw = FakeGateway([member])
    await pardon_all(
        gateway=gw, store=store, config=make_config(),
        targets=[member.user_id], days=30, reason="amnesty", now=NOW,
    )
    gw.set_members([make_info(user_id=member.user_id, has_inactive_role=False)])

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW + timedelta(days=5))
    assert gw.calls_of("add_role") == []

    await run_sweep(gateway=gw, store=store, config=make_config(), now=NOW + timedelta(days=31))
    assert gw.calls_of("add_role") == [member.user_id]


async def test_pardon_all_executes_exactly_the_confirmed_list(env):
    """What the moderator confirmed is what runs; someone flagged in between is
    not swept up by a recount."""
    from prunebot.services.sweep import pardon_all

    store = env
    confirmed = make_info(has_inactive_role=True)
    latecomer = make_info(has_inactive_role=True)
    await _flag(store, confirmed)
    await _flag(store, latecomer)
    gw = FakeGateway([confirmed, latecomer])

    await pardon_all(
        gateway=gw, store=store, config=make_config(),
        targets=[confirmed.user_id], days=30, reason="amnesty", now=NOW,
    )

    assert gw.calls_of("remove_role") == [confirmed.user_id]


async def test_unmanageable_flagged_members_are_reported_not_attempted(env):
    from prunebot.services.sweep import flagged_members

    store = env
    stuck = make_info(has_inactive_role=True, bot_can_manage=False)
    fine = make_info(has_inactive_role=True)
    gw = FakeGateway([stuck, fine])

    manageable, unmanageable = await flagged_members(gateway=gw, store=store)

    assert manageable == [fine.user_id]
    assert unmanageable == [stuck.user_id]


async def test_a_failed_removal_is_reported_and_not_marked_pardoned(env):
    from prunebot.services.sweep import pardon_all

    store = env
    member = make_info(has_inactive_role=True)
    await _flag(store, member)
    gw = FakeGateway([member])
    gw.role_forbidden.add(member.user_id)

    result = await pardon_all(
        gateway=gw, store=store, config=make_config(),
        targets=[member.user_id], days=30, reason="amnesty", now=NOW,
    )

    assert result.failed == [member.user_id]
    assert result.pardoned == []
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.FLAGGED


async def test_pardon_all_leaves_one_bulk_audit_record(env):
    from prunebot.services.sweep import pardon_all

    store = env
    member = make_info(has_inactive_role=True)
    await _flag(store, member)
    gw = FakeGateway([member])

    await pardon_all(
        gateway=gw, store=store, config=make_config(),
        targets=[member.user_id], days=14, reason="amnesty", actor_id=42, now=NOW,
    )

    rows = await store.recent_audit(GUILD_ID, limit=20)
    bulk = [r for r in rows if r["action"] == "pardon_all"]
    assert len(bulk) == 1
    assert bulk[0]["actor_id"] == 42
