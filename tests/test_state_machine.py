"""Full lifecycles simulated across days.

Each test advances a simulated clock and runs real sweeps against a real database,
so these cover the transitions that only appear over time: the warning, the final
notice, the kick, and every way out of the flagged state.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from prunebot.domain.models import Action, MemberState
from prunebot.services.sweep import clear_flag, run_sweep

from .conftest import GUILD_ID, NOW, FakeGateway, make_info
from .test_sweep_integration import make_config, seed_backfilled, with_activity


def at(days: float):
    """A moment `days` after the reference point."""
    return NOW + timedelta(days=days)


@pytest.fixture
async def env(store):
    await seed_backfilled(store)
    return store


async def sweep(gw, store, day: float, *, config=None, **kwargs):
    return await run_sweep(
        gateway=gw,
        store=store,
        config=config or make_config(),
        now=at(day),
        **kwargs,
    )


def rejoin_ready(gw, user_id, *, has_role: bool, joined_day: float):
    """Put a member back in the guild, as on_member_join would.

    `joined_at` moves to the rejoin date because Discord updates it on rejoin, and
    build_snapshot treats Discord as the authority over the stored value.
    """
    gw.set_members(
        [
            make_info(
                user_id=user_id, has_inactive_role=has_role, joined_at=at(joined_day)
            )
        ]
    )


# ------------------------------------------------------------- the whole journey


async def test_quiet_member_is_flagged_warned_then_kicked_on_schedule(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])

    # Day 0: goes quiet, gets flagged and warned.
    await sweep(gw, store, 0)
    assert gw.calls_of("add_role") == [member.user_id]
    assert gw.calls_of("warn:initial") == [member.user_id]
    gw.set_members([make_info(user_id=member.user_id, has_inactive_role=True)])

    # Day 30: still quiet, still flagged, nothing new happens.
    await sweep(gw, store, 30)
    assert gw.calls_of("kick") == []
    assert gw.calls_of("warn:final") == []

    # Day 88: inside the 3-day final-warning window.
    await sweep(gw, store, 88)
    assert gw.calls_of("warn:final") == [member.user_id]

    # Day 89: the final warning is not repeated.
    await sweep(gw, store, 89)
    assert gw.calls_of("warn:final") == [member.user_id]

    # Day 91: the clock runs out.
    await sweep(gw, store, 91)
    assert gw.calls_of("kick") == [member.user_id]
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.KICKED


async def test_nothing_happens_a_day_before_the_deadline(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])
    await sweep(gw, store, 0)
    gw.set_members([make_info(user_id=member.user_id, has_inactive_role=True)])

    await sweep(gw, store, 89.5)
    assert gw.calls_of("kick") == []


# --------------------------------------------------------------- ways out again


async def test_verifying_clears_the_flag_and_protects_for_the_grace_period(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])
    config = make_config()

    await sweep(gw, store, 0)
    gw.set_members([make_info(user_id=member.user_id, has_inactive_role=True)])

    # Day 5: they press the button.
    await clear_flag(
        gateway=gw,
        store=store,
        config=config,
        user_id=member.user_id,
        reason="verified",
        verified=True,
        now=at(5),
        dry_run=False,
    )
    assert gw.calls_of("remove_role") == [member.user_id]
    gw.set_members([make_info(user_id=member.user_id, has_inactive_role=False)])

    # Day 20: still inside the 30-day reverify grace, so still safe.
    await sweep(gw, store, 20)
    assert len(gw.calls_of("add_role")) == 1  # only the original flag

    # Day 40: grace has expired and they are still silent, so they are flagged again.
    await sweep(gw, store, 40)
    assert len(gw.calls_of("add_role")) == 2


async def test_posting_again_does_not_clear_the_flag(env):
    """The user's explicit choice: only /verify or a moderator clears it."""
    store = env
    member = make_info()
    gw = FakeGateway([member])

    await sweep(gw, store, 0)
    gw.set_members([make_info(user_id=member.user_id, has_inactive_role=True)])

    await with_activity(store, member.user_id, 50)  # they start chatting again
    await sweep(gw, store, 5)

    assert gw.calls_of("remove_role") == []
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.FLAGGED


async def test_auto_clear_on_activity_unflags_when_enabled(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])
    config = make_config(kicking={"auto_clear_on_activity": True})

    await sweep(gw, store, 0, config=config)
    gw.set_members([make_info(user_id=member.user_id, has_inactive_role=True)])

    await with_activity(store, member.user_id, 50)
    await sweep(gw, store, 5, config=config)

    assert gw.calls_of("remove_role") == [member.user_id]


async def test_moderator_removing_the_role_pardons_rather_than_re_flagging(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])

    await sweep(gw, store, 0)
    # A moderator strips the role by hand.
    gw.set_members([make_info(user_id=member.user_id, has_inactive_role=False)])

    await sweep(gw, store, 1)
    assert len(gw.calls_of("add_role")) == 1  # not re-flagged
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.PARDONED

    # Day 40: the pardon has run out, so normal rules apply again.
    await sweep(gw, store, 40)
    assert len(gw.calls_of("add_role")) == 2


async def test_whitelisting_mid_flight_stops_the_clock_permanently(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])

    await sweep(gw, store, 0)
    gw.set_members([make_info(user_id=member.user_id, has_inactive_role=True)])

    await store.whitelist_add(GUILD_ID, "user", member.user_id)

    await sweep(gw, store, 1)
    assert gw.calls_of("remove_role") == [member.user_id]
    gw.set_members([make_info(user_id=member.user_id, has_inactive_role=False)])

    # Far past the kick deadline, and still untouched.
    await sweep(gw, store, 200)
    assert gw.calls_of("kick") == []
    assert len(gw.calls_of("add_role")) == 1


# ----------------------------------------------------------------- leave/rejoin


async def test_leaving_records_the_flag_for_when_they_come_back(env):
    store = env
    member = make_info()
    gw = FakeGateway([member])

    await sweep(gw, store, 0)
    # They leave (what on_member_remove records).
    await store.update_member(
        GUILD_ID,
        member.user_id,
        state=MemberState.LEFT,
        left_at=int(at(1).timestamp()),
        flagged_on_leave=True,
    )
    row = await store.get_member(GUILD_ID, member.user_id)
    assert row.flagged_on_leave is True


async def test_rejoining_without_the_flag_restored_starts_them_clean(env):
    """With the flag restored by on_member_join the dodge fails; this checks the
    other half -- that a returning member is not silently still on the old clock."""
    store = env
    member = make_info()
    gw = FakeGateway([member])

    await sweep(gw, store, 0)
    await store.update_member(
        GUILD_ID,
        member.user_id,
        state=MemberState.ACTIVE,
        flagged_at=None,
        warned_at=None,
        warn_delivery=None,
        joined_at=int(at(2).timestamp()),
    )
    rejoin_ready(gw, member.user_id, has_role=False, joined_day=2)

    # Day 3: inside the join grace, so nothing happens to them at all.
    await sweep(gw, store, 3)
    assert len(gw.calls_of("add_role")) == 1

    # Day 200: the old 90-day clock is gone, so they are flagged afresh, not kicked.
    await sweep(gw, store, 200)
    assert gw.calls_of("kick") == []
    assert len(gw.calls_of("add_role")) == 2


async def test_a_restored_flag_restarts_the_clock_rather_than_resuming_it(env):
    """Rejoining should not instantly kick someone whose old clock had expired."""
    store = env
    member = make_info()
    gw = FakeGateway([member])

    await store.update_member(
        GUILD_ID,
        member.user_id,
        state=MemberState.FLAGGED,
        flagged_at=int(at(0).timestamp()),
        warned_at=int(at(0).timestamp()),
        warn_delivery="dm",
        joined_at=int(at(100).timestamp()),
    )
    rejoin_ready(gw, member.user_id, has_role=True, joined_day=100)

    # Day 101: the original clock is 101 days old, but they only just rejoined and
    # the join grace protects them.
    await sweep(gw, store, 101)
    assert gw.calls_of("kick") == []


# ---------------------------------------------------------------- dry-run soak


async def test_a_month_of_dry_runs_changes_nothing_and_stays_consistent(env):
    """What the first 30 days of deployment should look like."""
    store = env
    members = [make_info() for _ in range(5)]
    await with_activity(store, members[0].user_id, 10)
    gw = FakeGateway(members)

    predictions = []
    for day in range(0, 28, 3):
        result = await sweep(gw, store, day, dry_run=True)
        predictions.append(result.plan.count(Action.FLAG))

    assert gw.mutations == []
    assert all(p == 4 for p in predictions)  # stable, no drift

    # Turning dry run off then does exactly what every preview said it would.
    await sweep(gw, store, 27, dry_run=False)
    assert len(gw.calls_of("add_role")) == 4


async def test_the_window_really_rolls(env):
    """A single message does not protect someone forever: once it falls out of the
    trailing window they are inactive again."""
    store = env
    member = make_info()
    await with_activity(store, member.user_id, 1)
    gw = FakeGateway([member])

    assert (await sweep(gw, store, 20, dry_run=True)).plan.count(Action.FLAG) == 0
    assert (await sweep(gw, store, 40, dry_run=True)).plan.count(Action.FLAG) == 1
