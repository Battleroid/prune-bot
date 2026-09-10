"""Pure data types for the decision engine.

Nothing in `prunebot.domain` may import discord or perform I/O. That boundary is
what makes the entire decision path testable offline; `services.snapshot` is the
only adapter that turns a live discord.Member plus database rows into a
MemberSnapshot.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class MemberState(enum.StrEnum):
    ACTIVE = "active"
    FLAGGED = "flagged"
    PARDONED = "pardoned"
    KICKED = "kicked"
    LEFT = "left"


class Action(enum.StrEnum):
    """What the sweep intends to do about one member."""

    NONE = "none"
    SKIP = "skip"
    FLAG = "flag"
    UNFLAG = "unflag"
    WARN_FINAL = "warn_final"
    RETRY_WARN = "retry_warn"
    KICK = "kick"


#: Actions that change something in Discord, and therefore count against caps.
MUTATING = frozenset({Action.FLAG, Action.UNFLAG, Action.KICK, Action.WARN_FINAL, Action.RETRY_WARN})


class WarnDelivery(enum.StrEnum):
    DM = "dm"
    CHANNEL = "channel"


@dataclass(frozen=True, slots=True)
class MemberStateRow:
    """A row of `member_state`, as stored. Times are unix seconds."""

    guild_id: int
    user_id: int
    state: MemberState = MemberState.ACTIVE
    joined_at: int | None = None
    first_seen_at: int = 0
    last_message_at: int | None = None
    flagged_at: int | None = None
    warned_at: int | None = None
    warn_delivery: str | None = None
    warning_channel_id: int | None = None
    warning_message_id: int | None = None
    final_warned_at: int | None = None
    verified_at: int | None = None
    pardoned_until: int | None = None
    kicked_at: int | None = None
    left_at: int | None = None
    rejoin_count: int = 0
    flagged_on_leave: bool = False


@dataclass(frozen=True, slots=True)
class WhitelistEntry:
    guild_id: int
    kind: str  # 'user' | 'role'
    target_id: int
    added_by: int | None
    added_at: int
    reason: str | None


@dataclass(frozen=True, slots=True)
class GuildPolicy:
    """The subset of configuration the decision engine actually needs."""

    window_days: int = 30
    min_messages: int = 1
    grace_days_after_join: int = 14
    kick_after_days: int = 90
    final_warning_lead_days: int = 3
    auto_clear_on_activity: bool = True
    kicking_enabled: bool = True
    require_warning_before_kick: bool = False
    exempt_bots: bool = True
    max_flags_per_sweep: int = 25
    max_kicks_per_sweep: int = 5
    max_unflags_per_sweep: int = 200
    abort_if_flag_ratio_over: float = 0.25
    abort_if_kick_ratio_over: float = 0.05
    #: A ratio is meaningless on a handful of members: 1 of 2 is 50% and would
    #: abort every sweep on a small server. Below this many evaluable members the
    #: absolute caps are the only limit.
    breaker_min_evaluable: int = 20


@dataclass(frozen=True, slots=True)
class MemberSnapshot:
    """Everything needed to decide one member's fate, with no further lookups."""

    user_id: int
    message_count: int
    state: MemberState = MemberState.ACTIVE
    is_bot: bool = False
    is_owner: bool = False
    joined_at: datetime | None = None
    has_inactive_role: bool = False
    whitelisted_user: bool = False
    whitelisted_role: bool = False
    bot_can_manage: bool = True
    bot_can_kick: bool = True
    flagged_at: datetime | None = None
    warned_at: datetime | None = None
    warn_delivery: str | None = None
    final_warned_at: datetime | None = None
    pardoned_until: datetime | None = None
    display_name: str = ""

    @property
    def whitelisted(self) -> bool:
        return self.whitelisted_user or self.whitelisted_role


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str
    #: Every protection that applies, not just the first one matched. `/prune status`
    #: shows all of them so an admin can see *why* someone is safe, completely.
    exemptions: tuple[str, ...] = ()

    @property
    def is_mutating(self) -> bool:
        return self.action in MUTATING


@dataclass(frozen=True, slots=True)
class PlannedAction:
    user_id: int
    action: Action
    reason: str
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class SweepPlan:
    """The immutable output of planning. Executing it is a separate phase.

    `/prune preview` renders exactly this object, which is what guarantees the
    preview matches what a real run does.
    """

    guild_id: int
    sweep_id: str
    planned: tuple[PlannedAction, ...] = ()
    evaluable_count: int = 0
    considered_count: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    capped: frozenset[Action] = frozenset()
    aborted_reason: str | None = None
    #: Actions dropped because a cap was hit; they roll over to the next sweep.
    deferred: tuple[PlannedAction, ...] = ()

    @property
    def aborted(self) -> bool:
        return self.aborted_reason is not None

    def of(self, action: Action) -> tuple[PlannedAction, ...]:
        return tuple(p for p in self.planned if p.action == action)

    def count(self, action: Action) -> int:
        return sum(1 for p in self.planned if p.action == action)

    @property
    def mutating_count(self) -> int:
        return sum(1 for p in self.planned if p.action in MUTATING)


@dataclass(frozen=True, slots=True)
class MemberInfo:
    """What the Discord side knows about one member, as plain data.

    Produced by the gateway (real or fake) and combined with stored activity and
    state by `domain.snapshot.build_snapshots`.
    """

    user_id: int
    display_name: str = ""
    is_bot: bool = False
    is_owner: bool = False
    joined_at: datetime | None = None
    role_ids: frozenset[int] = frozenset()
    has_inactive_role: bool = False
    bot_can_manage: bool = True
    bot_can_kick: bool = True
