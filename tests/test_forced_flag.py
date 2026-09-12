"""Forced flags: `/prune flag force:true`, and how sweeps treat one afterwards.

A forced flag overrides the activity rules, a pardon, join grace and the backfill
gate, but never the whitelist, the owner, bots, or anyone above the bot. Only
messages posted after it can clear it, and a forced member who keeps posting is
never kicked.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from prunebot.cogs.admin import manual_flag_message
from prunebot.domain import eligibility as el
from prunebot.domain.eligibility import evaluate
from prunebot.domain.models import Action, MemberState
from prunebot.services.sweep import (
    ManualFlagResult,
    clear_flag,
    flag_member,
    run_sweep,
    status_for,
)

from .conftest import (
    GUILD_ID,
    NOW,
    FakeGateway,
    days_ago,
    flagged_snapshot,
    make_info,
    make_policy,
    make_snapshot,
)
from .test_sweep_integration import make_config, seed_backfilled, with_activity

LATER = NOW + timedelta(hours=6)  # the next sweep, still the same UTC day


@pytest.fixture
async def env(store):
    await seed_backfilled(store)
    return store


# ------------------------------------------------------------- the decision rules


def forced_snapshot(**overrides):
    """Force-flagged 10 days ago and still posting, but nothing since the flag."""
    defaults = {
        "flagged_days_ago": 10,
        "message_count": 10,
        "forced_at": days_ago(10),
        "messages_since_forced": 0,
    }
    defaults.update(overrides)
    return flagged_snapshot(**defaults)


def test_posting_from_before_a_forced_flag_does_not_clear_it(policy, now):
    d = evaluate(forced_snapshot(), policy, now)
    assert (d.action, d.reason) == (Action.NONE, el.R_FORCED_POSTING)


def test_posting_enough_after_a_forced_flag_clears_it(now):
    p = make_policy(min_messages=10)
    assert evaluate(forced_snapshot(messages_since_forced=9), p, now).action is Action.NONE
    d = evaluate(forced_snapshot(messages_since_forced=10), p, now)
    assert (d.action, d.reason) == (Action.UNFLAG, el.R_ACTIVE)


def test_with_auto_clear_off_a_forced_flag_waits_for_a_moderator(now):
    p = make_policy(auto_clear_on_activity=False)
    d = evaluate(forced_snapshot(messages_since_forced=5), p, now)
    assert (d.action, d.reason) == (Action.NONE, el.R_AWAITING_MODERATOR)


def test_a_forced_member_who_keeps_posting_is_never_kicked(policy, now):
    snap = forced_snapshot(flagged_days_ago=200, forced_at=days_ago(200))
    assert evaluate(snap, policy, now).action is Action.NONE


def test_a_forced_member_who_goes_quiet_is_on_the_kick_clock(policy, now):
    snap = forced_snapshot(flagged_days_ago=200, forced_at=days_ago(200), message_count=0)
    assert evaluate(snap, policy, now).action is Action.KICK


def test_join_grace_does_not_shield_a_forced_flag(policy, now):
    snap = forced_snapshot(joined_at=days_ago(3), messages_since_forced=1)
    d = evaluate(snap, policy, now)
    assert d.action is Action.UNFLAG
    assert el.R_NEW_MEMBER not in d.exemptions


def test_the_whitelist_still_outranks_a_forced_flag(policy, now):
    d = evaluate(forced_snapshot(whitelisted_user=True), policy, now)
    assert (d.action, d.reason) == (Action.UNFLAG, el.R_WHITELIST_USER)


def test_a_leftover_forced_at_on_an_unflagged_member_is_ignored(policy, now):
    snap = make_snapshot(message_count=0, forced_at=days_ago(10))
    assert not snap.forced
    assert evaluate(snap, policy, now).action is Action.FLAG


# ------------------------------------------------------------- /prune flag force


async def force(store, gw, member, config=None, now=NOW):
    return await flag_member(
        gateway=gw,
        store=store,
        config=config or make_config(),
        user_id=member.user_id,
        reason="forced",
        actor_id=42,
        force=True,
        now=now,
    )


async def refused(store, gw, member, config=None):
    result = await force(store, gw, member, config)
    assert not result.flagged
    assert gw.mutations == []
    return result.reason


def wearing_the_role(gw, member):
    """FakeGateway does not track role changes; show the member wearing it now."""
    gw.set_members([replace(member, has_inactive_role=True)])


async def sweep(gw, store, now=LATER):
    return await run_sweep(gateway=gw, store=store, config=make_config(), now=now)


async def test_force_flags_someone_who_is_posting(env):
    member = make_info()
    await with_activity(env, member.user_id, 5)
    gw = FakeGateway([member])

    result = await force(env, gw, member)

    assert result.flagged and result.forced
    assert gw.calls_of("add_role") == [member.user_id]
    assert gw.calls_of("warn:initial") == [member.user_id]
    row = await env.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.FLAGGED
    assert row.forced_at == int(NOW.timestamp())
    assert row.forced_baseline == 5  # posted today, but before the flag
    history = await env.user_history(GUILD_ID, member.user_id)
    assert any(r["action"] == "flag" and r["actor_id"] == 42 for r in history)


async def test_the_next_sweep_leaves_a_forced_flag_on(env):
    member = make_info()
    await with_activity(env, member.user_id, 5)
    gw = FakeGateway([member])
    await force(env, gw, member)
    wearing_the_role(gw, member)

    await sweep(gw, env)

    assert gw.calls_of("remove_role") == []
    row = await env.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.FLAGGED


async def test_older_posts_never_count_toward_clearing_it(env):
    member = make_info()
    await with_activity(env, member.user_id, 50, day_offset=3)
    gw = FakeGateway([member])
    await force(env, gw, member)
    wearing_the_role(gw, member)

    await sweep(gw, env)

    assert gw.calls_of("remove_role") == []


@pytest.mark.parametrize("days_later", [0, 1])
async def test_posting_after_a_forced_flag_lifts_it(env, days_later):
    """Later the same day (past the baseline), or on a following day."""
    member = make_info()
    await with_activity(env, member.user_id, 5)
    gw = FakeGateway([member])
    await force(env, gw, member)
    wearing_the_role(gw, member)

    await with_activity(env, member.user_id, 1, day_offset=-days_later)
    await sweep(gw, env, now=LATER + timedelta(days=days_later))

    assert gw.calls_of("remove_role") == [member.user_id]
    row = await env.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.ACTIVE
    assert row.forced_at is None


async def test_status_counts_only_posts_since_the_forced_flag(env):
    member = make_info()
    await with_activity(env, member.user_id, 5)
    gw = FakeGateway([member])
    await force(env, gw, member)
    wearing_the_role(gw, member)
    await with_activity(env, member.user_id, 2)

    snapshot, _ = await status_for(
        gateway=gw, store=env, config=make_config(), user_id=member.user_id, now=LATER
    )

    assert snapshot.forced
    assert snapshot.message_count == 7
    assert snapshot.messages_since_forced == 2


async def test_force_overrides_a_pardon(env):
    member = make_info()
    await env.update_member(
        GUILD_ID,
        member.user_id,
        state=MemberState.PARDONED,
        pardoned_until=int((NOW + timedelta(days=10)).timestamp()),
    )
    gw = FakeGateway([member])

    assert (await force(env, gw, member)).flagged
    assert (await env.get_member(GUILD_ID, member.user_id)).pardoned_until is None

    wearing_the_role(gw, member)
    await sweep(gw, env)
    assert gw.calls_of("remove_role") == []


async def test_force_overrides_join_grace_and_posting_still_clears_it(env):
    member = make_info(joined_at=days_ago(2))
    gw = FakeGateway([member])
    assert (await force(env, gw, member)).flagged

    wearing_the_role(gw, member)
    await with_activity(env, member.user_id, 1)
    await sweep(gw, env)

    assert gw.calls_of("remove_role") == [member.user_id]


async def test_force_does_not_wait_for_the_backfill(store):
    member = make_info()  # deliberately not seeded: backfill never completed
    assert (await force(store, FakeGateway([member]), member)).flagged


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"bot_can_manage": False}, el.R_UNMANAGEABLE),
        ({"is_owner": True}, el.R_OWNER),
        ({"is_bot": True}, el.R_BOT),
    ],
)
async def test_force_cannot_touch_the_owner_bots_or_anyone_above_the_bot(
    env, overrides, reason
):
    member = make_info(**overrides)
    assert await refused(env, FakeGateway([member]), member) == reason


async def test_force_cannot_override_the_whitelist(env):
    member = make_info()
    await env.whitelist_add(GUILD_ID, "user", member.user_id)
    assert await refused(env, FakeGateway([member]), member) == el.R_WHITELIST_USER


async def test_force_respects_dry_run(env):
    member = make_info()
    config = make_config(safety={"dry_run": True})
    assert await refused(env, FakeGateway([member]), member, config) == "dry_run"


async def test_force_leaves_an_existing_flag_alone(env):
    member = make_info(has_inactive_role=True)
    assert await refused(env, FakeGateway([member]), member) == "already_flagged"


# -------------------------------------------------------------------- ending one


async def test_a_pardon_ends_a_forced_flag(env):
    member = make_info()
    gw = FakeGateway([member])
    await force(env, gw, member)

    await clear_flag(
        gateway=gw,
        store=env,
        config=make_config(),
        user_id=member.user_id,
        reason="changed my mind",
        new_state=MemberState.PARDONED,
        pardon_days=30,
        now=NOW,
    )

    row = await env.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.PARDONED
    assert row.forced_at is None


async def test_taking_the_role_off_by_hand_ends_a_forced_flag(env):
    member = make_info()
    gw = FakeGateway([member])
    await force(env, gw, member)
    # FakeGateway still shows them without the role: a moderator took it off.

    await sweep(gw, env)

    row = await env.get_member(GUILD_ID, member.user_id)
    assert row.state is MemberState.PARDONED
    assert row.forced_at is None


# --------------------------------------------------------------------- the reply


def test_the_reply_says_how_a_forced_flag_lifts():
    member = SimpleNamespace(mention="<@1>")
    config = make_config(activity={"min_messages": 10})
    text = manual_flag_message(ManualFlagResult(True, "flagged", forced=True), member, config)
    assert text.startswith("Force-flagged <@1>: role on, warning sent.")
    assert "10 messages from now on" in text


def test_an_overridable_refusal_points_at_force():
    member = SimpleNamespace(mention="<@1>")
    result = ManualFlagResult(False, el.R_ACTIVE, snapshot=make_snapshot())
    assert "force:true" in manual_flag_message(result, member, make_config())
