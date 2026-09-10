"""Table-driven coverage of every row of the decision table, plus its boundaries."""

from __future__ import annotations

import pytest

from prunebot.domain import eligibility as el
from prunebot.domain.eligibility import evaluate, exemptions
from prunebot.domain.models import Action, MemberState

from .conftest import NOW, days_ago, flagged_snapshot, make_policy, make_snapshot

# --------------------------------------------------------------------- exemptions


def test_bot_is_skipped(policy, now):
    d = evaluate(make_snapshot(is_bot=True, message_count=0), policy, now)
    assert d.action is Action.SKIP
    assert d.reason == el.R_BOT


def test_bot_is_evaluated_when_exempt_bots_is_off(now):
    p = make_policy(exempt_bots=False)
    d = evaluate(make_snapshot(is_bot=True, message_count=0), p, now)
    assert d.action is Action.FLAG


def test_owner_is_never_actioned(policy, now):
    d = evaluate(make_snapshot(is_owner=True, message_count=0), policy, now)
    assert d.action is Action.SKIP
    assert d.reason == el.R_OWNER


@pytest.mark.parametrize(
    ("field", "reason"),
    [("whitelisted_user", el.R_WHITELIST_USER), ("whitelisted_role", el.R_WHITELIST_ROLE)],
)
def test_whitelisted_inactive_member_is_skipped(policy, now, field, reason):
    d = evaluate(make_snapshot(**{field: True}, message_count=0), policy, now)
    assert d.action is Action.SKIP
    assert d.reason == reason


@pytest.mark.parametrize("field", ["whitelisted_user", "whitelisted_role"])
def test_whitelisting_a_flagged_member_unflags_them(policy, now, field):
    snap = flagged_snapshot(flagged_days_ago=10, **{field: True})
    d = evaluate(snap, policy, now)
    assert d.action is Action.UNFLAG


def test_whitelist_outranks_being_overdue_for_a_kick(policy, now):
    # The whole point of the whitelist: it wins over every activity calculation.
    snap = flagged_snapshot(flagged_days_ago=999, whitelisted_user=True)
    assert evaluate(snap, policy, now).action is Action.UNFLAG


def test_unmanageable_member_is_skipped_not_unflagged(policy, now):
    # Removing the role would fail too, so claiming an UNFLAG would be a lie.
    snap = flagged_snapshot(flagged_days_ago=200, bot_can_manage=False)
    d = evaluate(snap, policy, now)
    assert d.action is Action.SKIP
    assert d.reason == el.R_UNMANAGEABLE


def test_active_pardon_protects_and_unflags(policy, now):
    snap = flagged_snapshot(flagged_days_ago=200, pardoned_until=NOW.replace(year=2027))
    d = evaluate(snap, policy, now)
    assert d.action is Action.UNFLAG
    assert d.reason == el.R_PARDONED


def test_expired_pardon_no_longer_protects(policy, now):
    snap = flagged_snapshot(flagged_days_ago=200, pardoned_until=days_ago(1))
    assert evaluate(snap, policy, now).action is Action.KICK


def test_new_members_are_skipped_during_join_grace(policy, now):
    d = evaluate(make_snapshot(message_count=0, joined_at=days_ago(3)), policy, now)
    assert d.action is Action.SKIP
    assert d.reason == el.R_NEW_MEMBER


def test_join_grace_boundary(policy, now):
    # grace_days_after_join = 14
    assert (
        evaluate(make_snapshot(message_count=0, joined_at=days_ago(13.9)), policy, now).action
        is Action.SKIP
    )
    assert (
        evaluate(make_snapshot(message_count=0, joined_at=days_ago(14.1)), policy, now).action
        is Action.FLAG
    )


def test_missing_join_date_does_not_grant_grace(policy, now):
    assert evaluate(make_snapshot(message_count=0, joined_at=None), policy, now).action is Action.FLAG


# ------------------------------------------------------------------ activity test


def test_active_member_is_left_alone(policy, now):
    d = evaluate(make_snapshot(message_count=5), policy, now)
    assert d.action is Action.NONE
    assert d.reason == el.R_ACTIVE


def test_zero_messages_gets_flagged(policy, now):
    d = evaluate(make_snapshot(message_count=0), policy, now)
    assert d.action is Action.FLAG
    assert d.reason == el.R_INACTIVE


def test_min_messages_boundary_is_inclusive(now):
    p = make_policy(min_messages=3)
    assert evaluate(make_snapshot(message_count=3), p, now).action is Action.NONE
    assert evaluate(make_snapshot(message_count=2), p, now).action is Action.FLAG


# --------------------------------------------------------------- already flagged


def test_posting_again_clears_the_flag_by_default(policy, now):
    """The only self-service way out: post back over the threshold."""
    snap = flagged_snapshot(flagged_days_ago=10, message_count=50)
    d = evaluate(snap, policy, now)
    assert d.action is Action.UNFLAG
    assert d.reason == el.R_ACTIVE


def test_posting_below_the_threshold_does_not_clear_it(now):
    p = make_policy(min_messages=3)
    snap = flagged_snapshot(flagged_days_ago=10, message_count=2)
    assert evaluate(snap, p, now).action is not Action.UNFLAG


def test_with_auto_clear_off_only_a_moderator_can_clear_it(now):
    p = make_policy(auto_clear_on_activity=False)
    snap = flagged_snapshot(flagged_days_ago=10, message_count=50)
    d = evaluate(snap, p, now)
    assert d.action is Action.NONE
    assert d.reason == el.R_AWAITING_MODERATOR


def test_flagged_but_not_yet_due_is_a_no_op(policy, now):
    d = evaluate(flagged_snapshot(flagged_days_ago=10), policy, now)
    assert d.action is Action.NONE
    assert d.reason == el.R_NOT_DUE


def test_kick_once_the_clock_runs_out(policy, now):
    assert evaluate(flagged_snapshot(flagged_days_ago=91), policy, now).action is Action.KICK


def test_kick_boundary_is_exactly_kick_after_days(policy, now):
    assert evaluate(flagged_snapshot(flagged_days_ago=89.9), policy, now).action is not Action.KICK
    assert evaluate(flagged_snapshot(flagged_days_ago=90.0), policy, now).action is Action.KICK


def test_kick_clock_runs_from_warning_not_from_flagging(policy, now):
    """Flagged long ago but only warned recently: the clock follows the warning."""
    snap = flagged_snapshot(flagged_days_ago=200, warned_at=days_ago(10))
    assert evaluate(snap, policy, now).action is Action.NONE


def test_undelivered_warning_pauses_the_clock_when_required(now):
    p = make_policy(require_warning_before_kick=True)
    snap = flagged_snapshot(flagged_days_ago=200, warned_at=None, warn_delivery=None)
    d = evaluate(snap, p, now)
    assert d.action is Action.RETRY_WARN
    assert d.reason == el.R_WARNING_UNDELIVERED


def test_undelivered_warning_still_kicks_when_not_required(now):
    """The shipped default, matching the DM-only choice: closed DMs still get kicked."""
    p = make_policy(require_warning_before_kick=False)
    snap = flagged_snapshot(flagged_days_ago=200, warned_at=None, warn_delivery=None)
    assert evaluate(snap, p, now).action is Action.KICK


def test_kicking_disabled_stops_at_no_op(now):
    p = make_policy(kicking_enabled=False)
    assert evaluate(flagged_snapshot(flagged_days_ago=200), p, now).action is Action.NONE


def test_overdue_member_the_bot_cannot_kick_is_skipped(policy, now):
    snap = flagged_snapshot(flagged_days_ago=200, bot_can_kick=False)
    d = evaluate(snap, policy, now)
    assert d.action is Action.SKIP
    assert d.reason == el.R_CANNOT_KICK


def test_final_warning_fires_inside_the_lead_window(policy, now):
    # kick_after_days=90, lead=3 -> final warning from day 87.
    assert evaluate(flagged_snapshot(flagged_days_ago=88), policy, now).action is Action.WARN_FINAL
    assert evaluate(flagged_snapshot(flagged_days_ago=86), policy, now).action is Action.NONE


def test_final_warning_is_not_repeated(policy, now):
    snap = flagged_snapshot(flagged_days_ago=88, final_warned_at=days_ago(1))
    assert evaluate(snap, policy, now).action is Action.NONE


def test_kick_takes_precedence_over_a_final_warning(policy, now):
    assert evaluate(flagged_snapshot(flagged_days_ago=120), policy, now).action is Action.KICK


def test_role_worn_without_a_flagged_row_is_still_treated_as_flagged(policy, now):
    """Tolerating drift means an exempt member always gets the role taken off."""
    snap = make_snapshot(
        state=MemberState.ACTIVE, has_inactive_role=True, whitelisted_user=True, message_count=0
    )
    assert evaluate(snap, policy, now).action is Action.UNFLAG


# ------------------------------------------------------------------- exemptions()


def test_exemptions_reports_every_applicable_protection(policy, now):
    snap = make_snapshot(
        whitelisted_user=True,
        whitelisted_role=True,
        pardoned_until=NOW.replace(year=2027),
        joined_at=days_ago(1),
        message_count=0,
    )
    found = exemptions(snap, policy, now)
    assert el.R_WHITELIST_USER in found
    assert el.R_WHITELIST_ROLE in found
    assert el.R_PARDONED in found
    assert el.R_NEW_MEMBER in found


def test_exemptions_is_empty_for_an_ordinary_member(policy, now):
    assert exemptions(make_snapshot(), policy, now) == ()


def test_decision_carries_exemptions_even_when_acting(policy, now):
    snap = make_snapshot(message_count=0, joined_at=days_ago(400))
    assert evaluate(snap, policy, now).exemptions == ()
