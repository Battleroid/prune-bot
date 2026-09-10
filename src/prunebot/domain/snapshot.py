"""Combine Discord member data, stored state, and activity into snapshots.

Pure: takes plain data in, gives plain data out. This is the join point where a
member's live Discord facts meet what the database remembers about them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .models import (
    MemberInfo,
    MemberSnapshot,
    MemberState,
    MemberStateRow,
)
from .windows import from_epoch


def build_snapshot(
    info: MemberInfo,
    *,
    message_count: int,
    row: MemberStateRow | None,
    whitelist_users: frozenset[int] | set[int],
    whitelist_roles: frozenset[int] | set[int],
) -> MemberSnapshot:
    state = row.state if row else MemberState.ACTIVE
    # A member who is present again is not 'left' or 'kicked', whatever the row says.
    if state in (MemberState.LEFT, MemberState.KICKED):
        state = MemberState.ACTIVE
    # An expired pardon is just an ordinary active member.
    if state is MemberState.PARDONED:
        state = MemberState.ACTIVE

    return MemberSnapshot(
        user_id=info.user_id,
        message_count=message_count,
        state=state,
        is_bot=info.is_bot,
        is_owner=info.is_owner,
        # Prefer Discord's join date; fall back to what we stored if the cache is thin.
        joined_at=info.joined_at or (from_epoch(row.joined_at) if row else None),
        has_inactive_role=info.has_inactive_role,
        whitelisted_user=info.user_id in whitelist_users,
        whitelisted_role=bool(info.role_ids & set(whitelist_roles)),
        bot_can_manage=info.bot_can_manage,
        bot_can_kick=info.bot_can_kick,
        flagged_at=from_epoch(row.flagged_at) if row else None,
        warned_at=from_epoch(row.warned_at) if row else None,
        warn_delivery=row.warn_delivery if row else None,
        final_warned_at=from_epoch(row.final_warned_at) if row else None,
        pardoned_until=from_epoch(row.pardoned_until) if row else None,
        display_name=info.display_name,
    )


def build_snapshots(
    members: Iterable[MemberInfo],
    *,
    counts: Mapping[int, int],
    rows: Mapping[int, MemberStateRow],
    whitelist_users: frozenset[int] | set[int],
    whitelist_roles: frozenset[int] | set[int],
) -> list[MemberSnapshot]:
    return [
        build_snapshot(
            info,
            message_count=counts.get(info.user_id, 0),
            row=rows.get(info.user_id),
            whitelist_users=whitelist_users,
            whitelist_roles=whitelist_roles,
        )
        for info in members
    ]
