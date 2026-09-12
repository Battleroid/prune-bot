"""Resolve drift between the inactive role in Discord and our stored state.

Discord is the authority on who is wearing the role. This runs before every sweep
so that manual moderator action -- adding or removing the role by hand -- is
understood rather than fought with. It is also how "a mod can just take the role
off" works without any command at all.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..db.store import Store
from ..domain.models import MemberInfo, MemberState, MemberStateRow

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    adopted: list[int] = field(default_factory=list)
    pardoned: list[int] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return len(self.adopted) + len(self.pardoned)


async def reconcile(
    *,
    store: Store,
    guild_id: int,
    members: Iterable[MemberInfo],
    rows: Mapping[int, MemberStateRow],
    reverify_grace_days: int,
    dry_run: bool = False,
    now: datetime | None = None,
) -> ReconcileReport:
    report = ReconcileReport()
    now = int((now or datetime.now(tz=UTC)).timestamp())

    for info in members:
        row = rows.get(info.user_id)
        stored_flagged = row is not None and row.state is MemberState.FLAGGED

        if info.has_inactive_role and not stored_flagged:
            # Someone applied the role by hand. Adopt it, and start the clock now
            # rather than pretending we warned them at some point in the past.
            report.adopted.append(info.user_id)
            if not dry_run:
                await store.update_member(
                    guild_id,
                    info.user_id,
                    state=MemberState.FLAGGED,
                    flagged_at=now,
                    warned_at=None,
                    warn_delivery=None,
                    final_warned_at=None,
                    forced_at=None,
                )
                await store.add_audit(
                    guild_id,
                    "flag",
                    user_id=info.user_id,
                    reason="adopted: role applied outside the bot",
                )

        elif stored_flagged and not info.has_inactive_role:
            # A moderator took the role off. Treat that as a deliberate pardon so
            # the next sweep does not simply put it straight back on.
            report.pardoned.append(info.user_id)
            if not dry_run:
                await store.update_member(
                    guild_id,
                    info.user_id,
                    state=MemberState.PARDONED,
                    pardoned_until=now + reverify_grace_days * 86400,
                    flagged_at=None,
                    warned_at=None,
                    warn_delivery=None,
                    final_warned_at=None,
                    forced_at=None,
                )
                await store.add_audit(
                    guild_id,
                    "pardon",
                    user_id=info.user_id,
                    reason="role removed by a moderator",
                )

    if report.changed:
        log.info(
            "reconciled guild %s: adopted=%d pardoned=%d",
            guild_id,
            len(report.adopted),
            len(report.pardoned),
        )
    return report
