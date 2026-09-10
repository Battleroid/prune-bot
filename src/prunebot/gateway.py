"""The port between the sweep and Discord.

The sweep talks only to this Protocol, which is what lets the whole execution path
run against a fake in tests. The real implementation lives in
`services.discord_gateway`; the test double in `tests/conftest.py`.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .domain.models import MemberInfo


class WarnKind(enum.StrEnum):
    INITIAL = "initial"
    FINAL = "final"
    PRE_KICK = "pre_kick"


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Outcome of one attempted mutation."""

    ok: bool
    detail: str = ""
    dry_run: bool = False

    @classmethod
    def done(cls, detail: str = "") -> ActionResult:
        return cls(True, detail)

    @classmethod
    def simulated(cls, detail: str = "") -> ActionResult:
        return cls(True, detail, dry_run=True)

    @classmethod
    def failed(cls, detail: str) -> ActionResult:
        return cls(False, detail)


@dataclass(frozen=True, slots=True)
class WarnResult(ActionResult):
    """A warning attempt, plus how it actually reached the member.

    `delivery` is None when nothing got through, which is what
    `require_warning_before_kick` keys off.
    """

    delivery: str | None = None
    channel_id: int | None = None
    message_id: int | None = None


@runtime_checkable
class GuildGateway(Protocol):
    """Everything the sweep needs to observe and change one guild."""

    guild_id: int

    async def members(self) -> Sequence[MemberInfo]:
        """Every current member, as plain data."""
        ...

    async def add_inactive_role(self, user_id: int, *, reason: str) -> ActionResult: ...

    async def remove_inactive_role(self, user_id: int, *, reason: str) -> ActionResult: ...

    async def kick(self, user_id: int, *, reason: str) -> ActionResult: ...

    async def warn(
        self, user_id: int, *, kind: WarnKind, days_left: int, reason: str
    ) -> WarnResult:
        """Deliver a warning. Must not raise for an undeliverable DM."""
        ...

    async def announce_flag(self, user_id: int, *, days_left: int) -> ActionResult:
        """Public post saying a member was flagged. Never raises."""
        ...

    async def post_audit(self, lines: str, *, title: str = "", alert: bool = False) -> None:
        """Best-effort post to the audit channel. Never raises."""
        ...
