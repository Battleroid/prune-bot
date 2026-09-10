"""Shared fixtures and fakes.

Everything time-dependent takes `now` explicitly, so there is no clock freezing
anywhere in this suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from prunebot.domain.models import GuildPolicy, MemberSnapshot, MemberState

GUILD_ID = 111111111111111111

#: A fixed reference point. Chosen mid-month so month boundaries never matter.
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def days_ago(n: float, *, now: datetime = NOW) -> datetime:
    return now - timedelta(days=n)


def make_policy(**overrides: Any) -> GuildPolicy:
    """Policy matching the shipped defaults unless overridden."""
    base: dict[str, Any] = {
        "window_days": 30,
        "min_messages": 1,
        "grace_days_after_join": 14,
        "kick_after_days": 90,
        "final_warning_lead_days": 3,
        "auto_clear_on_activity": True,
        "kicking_enabled": True,
        "require_warning_before_kick": False,
        "exempt_bots": True,
        "max_flags_per_sweep": 25,
        "max_kicks_per_sweep": 5,
        "max_unflags_per_sweep": 200,
        "abort_if_flag_ratio_over": 0.25,
        "abort_if_kick_ratio_over": 0.05,
        "breaker_min_evaluable": 20,
    }
    base.update(overrides)
    return GuildPolicy(**base)


_snapshot_counter = [1000]


def make_snapshot(**overrides: Any) -> MemberSnapshot:
    """A long-standing, currently active, unprotected member unless overridden."""
    _snapshot_counter[0] += 1
    base: dict[str, Any] = {
        "user_id": _snapshot_counter[0],
        "message_count": 10,
        "state": MemberState.ACTIVE,
        "is_bot": False,
        "is_owner": False,
        "joined_at": days_ago(400),
        "has_inactive_role": False,
        "whitelisted_user": False,
        "whitelisted_role": False,
        "bot_can_manage": True,
        "bot_can_kick": True,
        "flagged_at": None,
        "warned_at": None,
        "warn_delivery": None,
        "final_warned_at": None,
        "pardoned_until": None,
        "display_name": "member",
    }
    base.update(overrides)
    return MemberSnapshot(**base)


def flagged_snapshot(*, flagged_days_ago: float, **overrides: Any) -> MemberSnapshot:
    """An inactive member who was flagged and warned by DM `flagged_days_ago`."""
    defaults: dict[str, Any] = {
        "message_count": 0,
        "state": MemberState.FLAGGED,
        "has_inactive_role": True,
        "flagged_at": days_ago(flagged_days_ago),
        "warned_at": days_ago(flagged_days_ago),
        "warn_delivery": "dm",
    }
    defaults.update(overrides)
    return make_snapshot(**defaults)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def policy() -> GuildPolicy:
    return make_policy()


@pytest.fixture
async def store(tmp_path):
    """A real SQLite store on disk, migrated and ready."""
    from prunebot.db.store import Store

    s = Store(tmp_path / "test.db")
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


# --------------------------------------------------------------------- fake gateway


class FakeGateway:
    """In-memory GuildGateway. Records every call and can be made to fail.

    `dm_blocked` models the closed-DM case, which is the single most important
    failure mode to exercise: Discord returns 403 (code 50007) or sometimes 400,
    and the bot must treat both as "warning undelivered" rather than crashing.
    """

    def __init__(self, members=None, *, guild_id: int = GUILD_ID):
        self.guild_id = guild_id
        self._members = list(members or [])
        self.calls: list[tuple[str, int, str]] = []
        self.audit: list[str] = []
        self.dm_blocked: set[int] = set()
        self.role_forbidden: set[int] = set()
        self.kick_forbidden: set[int] = set()
        self.announce_forbidden: set[int] = set()

    # -- observation
    async def members(self):
        return list(self._members)

    def set_members(self, members):
        self._members = list(members)

    def calls_of(self, name: str) -> list[int]:
        return [uid for call, uid, _ in self.calls if call == name]

    @property
    def mutations(self) -> list[tuple[str, int, str]]:
        """Calls that would actually change Discord."""
        return [c for c in self.calls if c[0] in {"add_role", "remove_role", "kick"}]

    # -- mutation
    async def add_inactive_role(self, user_id: int, *, reason: str):
        from prunebot.gateway import ActionResult

        self.calls.append(("add_role", user_id, reason))
        if user_id in self.role_forbidden:
            return ActionResult.failed("Missing Permissions")
        return ActionResult.done()

    async def remove_inactive_role(self, user_id: int, *, reason: str):
        from prunebot.gateway import ActionResult

        self.calls.append(("remove_role", user_id, reason))
        if user_id in self.role_forbidden:
            return ActionResult.failed("Missing Permissions")
        return ActionResult.done()

    async def kick(self, user_id: int, *, reason: str):
        from prunebot.gateway import ActionResult

        self.calls.append(("kick", user_id, reason))
        if user_id in self.kick_forbidden:
            return ActionResult.failed("Missing Permissions")
        self._members = [m for m in self._members if m.user_id != user_id]
        return ActionResult.done()

    async def warn(self, user_id: int, *, kind, days_left: int, reason: str):
        from prunebot.gateway import WarnResult

        self.calls.append((f"warn:{kind.value}", user_id, reason))
        if user_id in self.dm_blocked:
            return WarnResult(False, "Cannot send messages to this user", delivery=None)
        return WarnResult(True, "", delivery="dm", channel_id=1, message_id=2)

    async def announce_flag(self, user_id: int, *, days_left: int):
        from prunebot.gateway import ActionResult

        self.calls.append(("announce", user_id, f"days_left={days_left}"))
        if user_id in self.announce_forbidden:
            return ActionResult.failed("Missing Permissions")
        return ActionResult.done()

    async def post_audit(self, lines: str, *, title: str = "", alert: bool = False):
        self.audit.append(lines)


def make_info(**overrides):
    """A MemberInfo for FakeGateway, defaulting to an ordinary long-standing member."""
    from prunebot.domain.models import MemberInfo

    _snapshot_counter[0] += 1
    base = {
        "user_id": _snapshot_counter[0],
        "display_name": "member",
        "is_bot": False,
        "is_owner": False,
        "joined_at": days_ago(400),
        "role_ids": frozenset(),
        "has_inactive_role": False,
        "bot_can_manage": True,
        "bot_can_kick": True,
    }
    base.update(overrides)
    return MemberInfo(**base)

