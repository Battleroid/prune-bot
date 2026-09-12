"""The single chokepoint for everything that changes Discord.

No other module may call `kick`, `add_roles`, or `send` on a member. Routing every
mutation through here means dry-run, per-sweep caps, throttling, and the audit
trail each exist in exactly one place and cannot be forgotten at a call site.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..db.store import Store
from ..domain.models import Action, MemberState
from ..gateway import ActionResult, GuildGateway, WarnKind

log = logging.getLogger(__name__)


@dataclass
class ExecutionReport:
    """What actually happened, as opposed to what was planned."""

    done: Counter[Action] = field(default_factory=Counter)
    failed: Counter[Action] = field(default_factory=Counter)
    cap_blocked: Counter[Action] = field(default_factory=Counter)
    warnings_undelivered: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def total_done(self) -> int:
        return sum(self.done.values())

    @property
    def total_failed(self) -> int:
        return sum(self.failed.values())


class ActionExecutor:
    """Performs planned actions, or pretends to when `dry_run` is set.

    In dry-run nothing is called on the gateway and no member state is written --
    only audit rows, tagged `dry_run = 1`. That asymmetry is deliberate: a dry run
    must be able to run repeatedly without drifting the database.
    """

    def __init__(
        self,
        *,
        gateway: GuildGateway,
        store: Store,
        guild_id: int,
        sweep_id: str,
        dry_run: bool,
        now: datetime | None = None,
        action_delay_seconds: float = 1.5,
        caps: dict[Action, int] | None = None,
        kick_reason_template: str = "Inactive",
        dm_before_kick: bool = True,
        post_every_action: bool = True,
        actor_id: int | None = None,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.guild_id = guild_id
        self.sweep_id = sweep_id
        self.dry_run = dry_run
        # Injected so a simulated timeline and the timestamps written during it
        # cannot disagree. Defaults to the real clock in production.
        self.now = now or datetime.now(tz=UTC)
        self.delay = action_delay_seconds
        self.caps = caps or {}
        self.kick_reason_template = kick_reason_template
        self.dm_before_kick = dm_before_kick
        self.post_every_action = post_every_action
        self.actor_id = actor_id
        self.report = ExecutionReport()
        self._used: Counter[Action] = Counter()

    # ------------------------------------------------------------------ plumbing

    def _cap_available(self, action: Action) -> bool:
        limit = self.caps.get(action)
        if limit is None:
            return True
        return self._used[action] < limit

    async def _throttle(self) -> None:
        # Discord does not publish per-route limits for kick or role-modify, so we
        # serialize mutations behind a configurable delay rather than guessing.
        if self.delay > 0 and not self.dry_run:
            await asyncio.sleep(self.delay)

    async def _audit(
        self,
        action: str,
        user_id: int | None,
        reason: str,
        *,
        ok: bool = True,
        payload: dict | None = None,
    ) -> None:
        await self.store.add_audit(
            self.guild_id,
            action if ok else f"{action}_failed",
            user_id=user_id,
            reason=reason,
            actor_id=self.actor_id,
            dry_run=self.dry_run,
            sweep_id=self.sweep_id,
            payload=payload,
        )
        if self.post_every_action:
            prefix = "[DRY RUN] " if self.dry_run else ""
            status = "" if ok else " -- FAILED"
            who = f"<@{user_id}>" if user_id else "-"
            await self.gateway.post_audit(
                f"{prefix}`{action}`{status} {who} ({reason})", alert=not ok
            )

    def _record(self, action: Action, result: ActionResult, detail: str = "") -> bool:
        if result.ok:
            self._used[action] += 1
            self.report.done[action] += 1
            return True
        self.report.failed[action] += 1
        self.report.failures.append(detail or result.detail)
        return False

    def _now(self) -> int:
        return int(self.now.timestamp())

    # -------------------------------------------------------------------- actions

    async def flag(
        self,
        user_id: int,
        *,
        reason: str,
        days_left: int,
        forced_baseline: int | None = None,
    ) -> bool:
        """Assign the inactive role and deliver the first warning.

        The warning is part of flagging rather than a separate step, because the
        kick clock starts from delivery -- separating them risks a flagged member
        with no clock and no record of why.

        `forced_baseline` marks a moderator's forced flag: the messages they had
        already posted today, before it, which must not count toward clearing it.
        A forced flag also ends any pardon, which would otherwise lift it again.
        """
        if not self._cap_available(Action.FLAG):
            self.report.cap_blocked[Action.FLAG] += 1
            return False

        if self.dry_run:
            await self._audit("flag", user_id, reason)
            self._used[Action.FLAG] += 1
            self.report.done[Action.FLAG] += 1
            return True

        result = await self.gateway.add_inactive_role(user_id, reason=reason)
        if not self._record(Action.FLAG, result, f"flag {user_id}: {result.detail}"):
            await self._audit("flag", user_id, result.detail, ok=False)
            await self._throttle()
            return False

        now = self._now()
        warn = await self.gateway.warn(
            user_id, kind=WarnKind.INITIAL, days_left=days_left, reason=reason
        )
        if warn.delivery is None:
            self.report.warnings_undelivered += 1

        forced: dict[str, object] = {"forced_at": None}
        if forced_baseline is not None:
            forced.update(
                forced_at=now, forced_baseline=forced_baseline, pardoned_until=None
            )
        await self.store.update_member(
            self.guild_id,
            user_id,
            state=MemberState.FLAGGED,
            flagged_at=now,
            warned_at=now if warn.delivery else None,
            warn_delivery=warn.delivery,
            warning_channel_id=warn.channel_id,
            warning_message_id=warn.message_id,
            final_warned_at=None,
            **forced,
        )
        # Public announcement, after the role is really on. Deliberately never
        # sent during a dry run: telling members they have been flagged when
        # nothing happened would be worse than saying nothing at all.
        announced = await self.gateway.announce_flag(user_id, days_left=days_left)
        if not announced.ok:
            log.info("could not announce flag for %s: %s", user_id, announced.detail)

        await self._audit(
            "flag",
            user_id,
            reason,
            payload={
                "warn_delivery": warn.delivery,
                "warn_detail": warn.detail,
                "announced": announced.ok,
                "forced": forced_baseline is not None,
            },
        )
        if warn.delivery is None:
            await self._audit("warn_failed", user_id, warn.detail or "undeliverable", ok=False)
        await self._throttle()
        return True

    async def unflag(
        self, user_id: int, *, reason: str, new_state: MemberState = MemberState.ACTIVE
    ) -> bool:
        if not self._cap_available(Action.UNFLAG):
            self.report.cap_blocked[Action.UNFLAG] += 1
            return False

        if self.dry_run:
            await self._audit("unflag", user_id, reason)
            self._used[Action.UNFLAG] += 1
            self.report.done[Action.UNFLAG] += 1
            return True

        result = await self.gateway.remove_inactive_role(user_id, reason=reason)
        if not self._record(Action.UNFLAG, result, f"unflag {user_id}: {result.detail}"):
            await self._audit("unflag", user_id, result.detail, ok=False)
            await self._throttle()
            return False

        await self.store.update_member(
            self.guild_id,
            user_id,
            state=new_state,
            flagged_at=None,
            warned_at=None,
            warn_delivery=None,
            final_warned_at=None,
            forced_at=None,
        )
        await self._audit("unflag", user_id, reason)
        await self._throttle()
        return True

    async def warn_final(self, user_id: int, *, reason: str, days_left: int) -> bool:
        if self.dry_run:
            await self._audit("final_warn", user_id, reason)
            self.report.done[Action.WARN_FINAL] += 1
            return True

        warn = await self.gateway.warn(
            user_id, kind=WarnKind.FINAL, days_left=days_left, reason=reason
        )
        self._record(Action.WARN_FINAL, warn, f"final warn {user_id}: {warn.detail}")
        # Recorded even when undelivered, so we do not retry a closed DM every night.
        await self.store.update_member(
            self.guild_id, user_id, final_warned_at=self._now()
        )
        await self._audit(
            "final_warn",
            user_id,
            reason,
            ok=warn.delivery is not None,
            payload={"delivery": warn.delivery},
        )
        await self._throttle()
        return warn.ok

    async def retry_warn(self, user_id: int, *, reason: str, days_left: int) -> bool:
        """Re-attempt an undelivered initial warning; starts the clock if it lands."""
        if self.dry_run:
            await self._audit("warn", user_id, reason)
            self.report.done[Action.RETRY_WARN] += 1
            return True

        warn = await self.gateway.warn(
            user_id, kind=WarnKind.INITIAL, days_left=days_left, reason=reason
        )
        if warn.delivery is None:
            self.report.warnings_undelivered += 1
            self.report.failed[Action.RETRY_WARN] += 1
            await self._audit("warn", user_id, warn.detail or "undeliverable", ok=False)
            await self._throttle()
            return False

        self._record(Action.RETRY_WARN, warn)
        await self.store.update_member(
            self.guild_id,
            user_id,
            warned_at=self._now(),
            warn_delivery=warn.delivery,
            warning_channel_id=warn.channel_id,
            warning_message_id=warn.message_id,
        )
        await self._audit("warn", user_id, reason, payload={"delivery": warn.delivery})
        await self._throttle()
        return True

    async def kick(self, user_id: int, *, reason: str) -> bool:
        if not self._cap_available(Action.KICK):
            self.report.cap_blocked[Action.KICK] += 1
            return False

        if self.dry_run:
            await self._audit("kick", user_id, reason)
            self._used[Action.KICK] += 1
            self.report.done[Action.KICK] += 1
            return True

        # Courtesy DM first: once they are kicked we can no longer reach them.
        if self.dm_before_kick:
            await self.gateway.warn(
                user_id, kind=WarnKind.PRE_KICK, days_left=0, reason=reason
            )

        result = await self.gateway.kick(user_id, reason=reason)
        if not self._record(Action.KICK, result, f"kick {user_id}: {result.detail}"):
            await self._audit("kick", user_id, result.detail, ok=False)
            await self._throttle()
            return False

        await self.store.update_member(
            self.guild_id,
            user_id,
            state=MemberState.KICKED,
            kicked_at=self._now(),
        )
        await self._audit("kick", user_id, reason)
        await self._throttle()
        return True
