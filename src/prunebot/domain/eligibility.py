"""The per-member decision function.

`evaluate` is pure: same snapshot plus same policy plus same `now` always yields the
same Decision. Every check that *protects* a member is evaluated before any check
that could act against them, so a bug in the activity maths can never outrank the
whitelist.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import Action, Decision, GuildPolicy, MemberSnapshot, MemberState
from .windows import has_elapsed

# Reasons are stable identifiers, safe to assert on in tests and to render in embeds.
R_BOT = "bot"
R_OWNER = "server_owner"
R_WHITELIST_USER = "whitelist(user)"
R_WHITELIST_ROLE = "whitelist(role)"
R_UNMANAGEABLE = "unmanageable(role_hierarchy_or_permissions)"
R_CANNOT_KICK = "cannot_kick(role_hierarchy_or_permissions)"
R_PARDONED = "pardoned"
R_NEW_MEMBER = "new_member(join_grace)"
R_ACTIVE = "active"
R_INACTIVE = "inactive"
R_AWAITING_MODERATOR = "flagged_awaiting_moderator"
R_WARNING_UNDELIVERED = "warning_undelivered"
R_NOT_DUE = "not_due"
R_KICKING_DISABLED = "kicking_disabled"


def _pardon_active(snapshot: MemberSnapshot, now: datetime) -> bool:
    return snapshot.pardoned_until is not None and snapshot.pardoned_until > now


def _join_grace_active(
    snapshot: MemberSnapshot, policy: GuildPolicy, now: datetime
) -> bool:
    if snapshot.joined_at is None or policy.grace_days_after_join <= 0:
        return False
    return now < snapshot.joined_at + timedelta(days=policy.grace_days_after_join)


def _is_flagged(snapshot: MemberSnapshot) -> bool:
    """Treat a member wearing the role as flagged even if the row disagrees.

    Reconciliation normally keeps these in sync; being tolerant here means an
    exempt member always gets the role taken off, whatever the database thinks.
    """
    return snapshot.state is MemberState.FLAGGED or snapshot.has_inactive_role


def exemptions(
    snapshot: MemberSnapshot, policy: GuildPolicy, now: datetime
) -> tuple[str, ...]:
    """Every protection currently applying, in priority order.

    Unlike `evaluate`, this does not stop at the first match: `/prune status` shows
    the complete picture so an admin can tell whether removing one protection would
    actually expose someone.
    """
    found: list[str] = []
    if snapshot.is_bot and policy.exempt_bots:
        found.append(R_BOT)
    if snapshot.is_owner:
        found.append(R_OWNER)
    if snapshot.whitelisted_user:
        found.append(R_WHITELIST_USER)
    if snapshot.whitelisted_role:
        found.append(R_WHITELIST_ROLE)
    if not snapshot.bot_can_manage:
        found.append(R_UNMANAGEABLE)
    if _pardon_active(snapshot, now):
        found.append(R_PARDONED)
    if _join_grace_active(snapshot, policy, now):
        found.append(R_NEW_MEMBER)
    return tuple(found)


def is_active(snapshot: MemberSnapshot, policy: GuildPolicy) -> bool:
    return snapshot.message_count >= policy.min_messages


def evaluate(
    snapshot: MemberSnapshot, policy: GuildPolicy, now: datetime
) -> Decision:
    """Decide what should happen to one member. Performs no I/O."""
    all_exemptions = exemptions(snapshot, policy, now)
    flagged = _is_flagged(snapshot)

    def protect(reason: str) -> Decision:
        """An exempt member gets the role taken back off; otherwise left alone."""
        if flagged:
            return Decision(Action.UNFLAG, reason, all_exemptions)
        return Decision(Action.SKIP, reason, all_exemptions)

    # --- 1-2: never touchable at all -------------------------------------------
    if snapshot.is_bot and policy.exempt_bots:
        return Decision(Action.SKIP, R_BOT, all_exemptions)
    if snapshot.is_owner:
        # Discord forbids kicking the owner regardless; skipping keeps the audit clean.
        return Decision(Action.SKIP, R_OWNER, all_exemptions)

    # --- 3: the whitelist, the one guarantee the user asked for -----------------
    if snapshot.whitelisted:
        reason = R_WHITELIST_USER if snapshot.whitelisted_user else R_WHITELIST_ROLE
        return protect(reason)

    # --- 4: we physically cannot act on them ------------------------------------
    if not snapshot.bot_can_manage:
        # Deliberately not an UNFLAG: removing the role would also fail.
        return Decision(Action.SKIP, R_UNMANAGEABLE, all_exemptions)

    # --- 5-6: temporary protections ---------------------------------------------
    # Deliberately no self-service grace: members used to be able to press a
    # button for a month of immunity without posting a word.
    if _pardon_active(snapshot, now):
        return protect(R_PARDONED)
    if _join_grace_active(snapshot, policy, now):
        return Decision(Action.SKIP, R_NEW_MEMBER, all_exemptions)

    # --- 7: the actual activity test --------------------------------------------
    active = is_active(snapshot, policy)

    if not flagged:
        if active:
            return Decision(Action.NONE, R_ACTIVE, all_exemptions)
        return Decision(Action.FLAG, R_INACTIVE, all_exemptions)

    # --- 8: they are already flagged ---------------------------------------------
    if active:
        # Posting back over the threshold is the only way a member clears their
        # own flag. With auto-clear off there is no self-service route at all.
        if policy.auto_clear_on_activity:
            return Decision(Action.UNFLAG, R_ACTIVE, all_exemptions)
        return Decision(Action.NONE, R_AWAITING_MODERATOR, all_exemptions)

    # The kick clock runs from when the warning was *delivered*, never from
    # flagged_at -- nobody is kicked on a timer that started before they were told.
    if snapshot.warn_delivery is None:
        if policy.require_warning_before_kick:
            return Decision(Action.RETRY_WARN, R_WARNING_UNDELIVERED, all_exemptions)
        clock_start = snapshot.flagged_at
    else:
        clock_start = snapshot.warned_at or snapshot.flagged_at

    if not policy.kicking_enabled:
        return Decision(Action.NONE, R_KICKING_DISABLED, all_exemptions)

    if has_elapsed(clock_start, policy.kick_after_days, now):
        if not snapshot.bot_can_kick:
            return Decision(Action.SKIP, R_CANNOT_KICK, all_exemptions)
        return Decision(Action.KICK, R_INACTIVE, all_exemptions)

    if (
        policy.final_warning_lead_days > 0
        and snapshot.final_warned_at is None
        and has_elapsed(
            clock_start,
            policy.kick_after_days - policy.final_warning_lead_days,
            now,
        )
    ):
        return Decision(Action.WARN_FINAL, R_INACTIVE, all_exemptions)

    return Decision(Action.NONE, R_NOT_DUE, all_exemptions)
