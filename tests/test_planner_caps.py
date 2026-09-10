"""Caps and the circuit breaker -- the two things standing between a
misconfiguration and a mass kick."""

from __future__ import annotations

from prunebot.domain.models import Action
from prunebot.domain.planner import build_plan

from .conftest import GUILD_ID, days_ago, flagged_snapshot, make_policy, make_snapshot


def plan_for(snapshots, policy=None, now=None):
    from .conftest import NOW

    return build_plan(
        guild_id=GUILD_ID,
        sweep_id="test-sweep",
        snapshots=snapshots,
        policy=policy or make_policy(),
        now=now or NOW,
    )


def population(active: int, inactive: int = 0, **inactive_kwargs):
    """`active` healthy members plus `inactive` members with zero messages."""
    members = [make_snapshot(message_count=5) for _ in range(active)]
    members += [
        make_snapshot(message_count=0, **inactive_kwargs) for _ in range(inactive)
    ]
    return members


# ------------------------------------------------------------------------- caps


def test_flag_cap_limits_the_plan_and_defers_the_rest():
    plan = plan_for(population(active=400, inactive=100))
    assert plan.count(Action.FLAG) == 25
    assert len(plan.deferred) == 75
    assert Action.FLAG in plan.capped
    assert not plan.aborted


def test_uncapped_population_plans_everything():
    plan = plan_for(population(active=400, inactive=10))
    assert plan.count(Action.FLAG) == 10
    assert plan.deferred == ()
    assert plan.capped == frozenset()


def test_kick_cap_is_independent_of_the_flag_cap():
    # 5 kicks is the cap; keep both ratios under their limits with a big population.
    members = population(active=1000, inactive=20)
    members += [flagged_snapshot(flagged_days_ago=100) for _ in range(20)]
    plan = plan_for(members)
    assert plan.count(Action.KICK) == 5
    assert plan.count(Action.FLAG) == 20  # under the flag cap of 25, unaffected
    assert Action.KICK in plan.capped
    assert Action.FLAG not in plan.capped


def test_unflags_are_capped_separately_and_generously():
    members = [
        flagged_snapshot(flagged_days_ago=10, whitelisted_user=True) for _ in range(10)
    ]
    plan = plan_for(members + population(active=100))
    assert plan.count(Action.UNFLAG) == 10
    assert plan.capped == frozenset()


def test_kicks_are_ordered_oldest_clock_first_so_caps_are_fair():
    members = population(active=1000)
    # Deliberately built newest-first; the planner must reorder them.
    members += [flagged_snapshot(flagged_days_ago=d) for d in (95, 200, 150, 300, 91, 400)]
    plan = plan_for(members)
    kicks = plan.of(Action.KICK)
    assert len(kicks) == 5
    # The single most-recently-warned member is the one deferred.
    assert len(plan.deferred) == 1
    assert plan.deferred[0].action is Action.KICK


# -------------------------------------------------------------- circuit breaker


def test_flag_ratio_breaker_aborts_and_plans_nothing():
    # 30 of 100 evaluable = 30%, over the 25% limit.
    plan = plan_for(population(active=70, inactive=30))
    assert plan.aborted
    assert "flag ratio" in plan.aborted_reason
    assert plan.planned == ()
    assert plan.deferred == ()


def test_breaker_is_exclusive_at_exactly_the_limit():
    # 25 of 100 = exactly 25%, which is not *over* the limit.
    plan = plan_for(population(active=75, inactive=25))
    assert not plan.aborted
    assert plan.count(Action.FLAG) == 25


def test_wiped_database_scenario_aborts_rather_than_flagging_everyone():
    """If every member suddenly looks inactive, do nothing at all."""
    plan = plan_for(population(active=0, inactive=200))
    assert plan.aborted
    assert plan.planned == ()


def test_kick_ratio_breaker_has_its_own_tighter_limit():
    # 10 kicks of 100 evaluable = 10%, over the 5% kick limit but under 25%.
    members = population(active=90)
    members += [flagged_snapshot(flagged_days_ago=100) for _ in range(10)]
    plan = plan_for(members)
    assert plan.aborted
    assert "kick ratio" in plan.aborted_reason


def test_breaker_ignores_exempt_members_in_the_denominator():
    """Skipped members are not 'evaluable', so a mostly-whitelisted server
    still trips the breaker when the rest of it would be flagged."""
    members = [make_snapshot(message_count=0, whitelisted_user=True) for _ in range(900)]
    members += population(active=70, inactive=30)
    plan = plan_for(members)
    assert plan.evaluable_count == 100
    assert plan.aborted


# ------------------------------------------------------------------ bookkeeping


def test_counts_and_skip_reasons_are_reported():
    members = population(active=50, inactive=5)
    members += [make_snapshot(is_bot=True, message_count=0) for _ in range(3)]
    members += [make_snapshot(is_owner=True, message_count=0)]
    plan = plan_for(members)
    assert plan.considered_count == 59
    assert plan.evaluable_count == 55
    assert plan.skipped["bot"] == 3
    assert plan.skipped["server_owner"] == 1


def test_empty_guild_produces_an_empty_plan():
    plan = plan_for([])
    assert not plan.aborted
    assert plan.planned == ()
    assert plan.evaluable_count == 0


def test_plan_is_reproducible_for_the_same_input():
    members = population(active=400, inactive=100)
    first = plan_for(members)
    second = plan_for(members)
    assert [p.user_id for p in first.planned] == [p.user_id for p in second.planned]


def test_new_members_do_not_count_toward_the_flag_ratio():
    members = population(active=70, inactive=30, joined_at=days_ago(2))
    plan = plan_for(members)
    assert not plan.aborted
    assert plan.count(Action.FLAG) == 0
    assert plan.skipped["new_member(join_grace)"] == 30


# ------------------------------------------------- small guilds bypass the ratio


def test_small_guild_is_not_aborted_by_the_ratio():
    """A ratio over a handful of members is noise: 1 of 2 is 50% and would abort
    every sweep on a small server. Below the threshold, caps are the only limit."""
    plan = plan_for(population(active=1, inactive=1))
    assert not plan.aborted
    assert plan.count(Action.FLAG) == 1


def test_breaker_engages_exactly_at_the_minimum_population():
    p = make_policy(breaker_min_evaluable=20)
    just_under = plan_for(population(active=9, inactive=10), policy=p)
    just_over = plan_for(population(active=10, inactive=10), policy=p)
    assert not just_under.aborted  # 19 evaluable
    assert just_over.aborted  # 20 evaluable, 50% flagged


def test_caps_still_protect_a_small_guild():
    p = make_policy(breaker_min_evaluable=1000, max_flags_per_sweep=2)
    plan = plan_for(population(active=0, inactive=10), policy=p)
    assert not plan.aborted
    assert plan.count(Action.FLAG) == 2
    assert len(plan.deferred) == 8
