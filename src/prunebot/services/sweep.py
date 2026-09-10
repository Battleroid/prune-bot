"""Sweep orchestration: reconcile -> snapshot -> plan -> execute.

Planning and execution are separate calls on purpose. `/prune preview` runs
`build_sweep_plan` and renders the result; a real run passes the very same object
to `execute_plan`. There is no second code path that could disagree with the
preview.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..config import Config
from ..db.store import Store
from ..domain.eligibility import evaluate
from ..domain.models import (
    Action,
    GuildPolicy,
    MemberSnapshot,
    MemberState,
    SweepPlan,
)
from ..domain.planner import build_plan
from ..domain.snapshot import build_snapshots
from ..domain.windows import window_start_day
from ..gateway import GuildGateway
from .actions import ActionExecutor, ExecutionReport
from .reconcile import ReconcileReport, reconcile

log = logging.getLogger(__name__)


def policy_from_config(config: Config) -> GuildPolicy:
    return GuildPolicy(
        window_days=config.activity.window_days,
        min_messages=config.activity.min_messages,
        grace_days_after_join=config.flagging.grace_days_after_join,
        kick_after_days=config.kicking.kick_after_days,
        final_warning_lead_days=config.kicking.final_warning_lead_days,
        auto_clear_on_activity=config.kicking.auto_clear_on_activity,
        kicking_enabled=config.kicking.enabled,
        require_warning_before_kick=config.safety.require_warning_before_kick,
        exempt_bots=config.whitelist.exempt_bots,
        max_flags_per_sweep=config.safety.max_flags_per_sweep,
        max_kicks_per_sweep=config.safety.max_kicks_per_sweep,
        max_unflags_per_sweep=config.safety.max_unflags_per_sweep,
        abort_if_flag_ratio_over=config.safety.abort_if_flag_ratio_over,
        abort_if_kick_ratio_over=config.safety.abort_if_kick_ratio_over,
        breaker_min_evaluable=config.safety.breaker_min_evaluable,
    )


@dataclass
class PlanBundle:
    """A plan plus the snapshots it was computed from."""

    plan: SweepPlan
    snapshots: dict[int, MemberSnapshot] = field(default_factory=dict)
    reconciled: ReconcileReport = field(default_factory=ReconcileReport)
    blocked_reason: str | None = None
    """Why this plan may not be acted on -- always computed, so a preview says so
    too rather than quietly showing numbers built on incomplete data."""


@dataclass
class SweepResult:
    bundle: PlanBundle
    execution: ExecutionReport | None = None
    dry_run: bool = True
    downgraded: bool = False
    """True when a live run was forced into a simulation by a safety gate."""

    @property
    def plan(self) -> SweepPlan:
        return self.bundle.plan


async def _backfill_gate(
    store: Store, guild_id: int, config: Config, now: datetime
) -> str | None:
    """Returns a reason to refuse to act, or None if the data is trustworthy."""
    meta = await store.guild_meta(guild_id)
    if meta.get("backfilled_at") is None:
        return (
            "history backfill has not completed for this guild, so activity counts "
            "are incomplete -- run /prune backfill"
        )
    covers = meta.get("backfill_covers")
    needed = (now - timedelta(days=config.activity.window_days)).timestamp()
    if covers is not None and covers > needed:
        covered_days = (now.timestamp() - covers) / 86400
        return (
            f"backfill only reaches {covered_days:.0f} days back but the activity "
            f"window is {config.activity.window_days} days"
        )
    return None


async def build_sweep_plan(
    *,
    gateway: GuildGateway,
    store: Store,
    config: Config,
    now: datetime,
    sweep_id: str | None = None,
    do_reconcile: bool = True,
    dry_run: bool | None = None,
) -> PlanBundle:
    """Phase 1. Reads Discord and the database; changes nothing in Discord."""
    guild_id = gateway.guild_id
    sweep_id = sweep_id or uuid.uuid4().hex[:12]
    effective_dry_run = config.safety.dry_run if dry_run is None else dry_run

    members = list(await gateway.members())
    rows = await store.all_members(guild_id)

    report = ReconcileReport()
    if do_reconcile:
        report = await reconcile(
            store=store,
            guild_id=guild_id,
            members=members,
            rows=rows,
            reverify_grace_days=config.kicking.reverify_grace_days,
            dry_run=effective_dry_run,
            now=now,
        )
        if report.changed and not effective_dry_run:
            rows = await store.all_members(guild_id)

    since_day = window_start_day(now, config.activity.window_days)
    counts = await store.window_counts(guild_id, since_day)
    whitelist_users, whitelist_roles = await store.whitelist_ids(guild_id)

    snapshots = build_snapshots(
        members,
        counts=counts,
        rows=rows,
        whitelist_users=whitelist_users,
        whitelist_roles=whitelist_roles,
    )

    plan = build_plan(
        guild_id=guild_id,
        sweep_id=sweep_id,
        snapshots=snapshots,
        policy=policy_from_config(config),
        now=now,
    )

    # Computed even for a dry run: a preview built on an unfinished backfill is
    # misleading, and silence about that is how you talk yourself into acting.
    blocked = None
    if config.safety.require_backfill_before_action:
        blocked = await _backfill_gate(store, guild_id, config, now)

    return PlanBundle(
        plan=plan,
        snapshots={s.user_id: s for s in snapshots},
        reconciled=report,
        blocked_reason=blocked,
    )


def _days_left(
    snapshot: MemberSnapshot, policy: GuildPolicy, now: datetime
) -> int:
    """Whole days until this member becomes eligible for a kick."""
    clock = snapshot.warned_at or snapshot.flagged_at
    if clock is None:
        return policy.kick_after_days
    deadline = clock + timedelta(days=policy.kick_after_days)
    return max(0, int((deadline - now).total_seconds() // 86400))


async def execute_plan(
    *,
    bundle: PlanBundle,
    gateway: GuildGateway,
    store: Store,
    config: Config,
    now: datetime,
    dry_run: bool | None = None,
    actor_id: int | None = None,
) -> SweepResult:
    """Phase 2. Carries out exactly what phase 1 planned."""
    plan = bundle.plan
    policy = policy_from_config(config)
    effective_dry_run = config.safety.dry_run if dry_run is None else dry_run

    downgraded = False
    if bundle.blocked_reason and not effective_dry_run:
        effective_dry_run = True
        downgraded = True
        log.warning("forcing dry run: %s", bundle.blocked_reason)
        await gateway.post_audit(
            f"Sweep downgraded to a dry run: {bundle.blocked_reason}", alert=True
        )

    if plan.aborted:
        await store.add_audit(
            plan.guild_id,
            "sweep_aborted",
            reason=plan.aborted_reason,
            sweep_id=plan.sweep_id,
            dry_run=effective_dry_run,
            actor_id=actor_id,
        )
        await gateway.post_audit(
            f"**Sweep aborted, nothing was changed.** {plan.aborted_reason}", alert=True
        )
        return SweepResult(
            bundle=bundle,
            execution=None,
            dry_run=effective_dry_run,
            downgraded=downgraded,
        )

    await store.add_audit(
        plan.guild_id,
        "sweep_start",
        reason=f"{plan.mutating_count} action(s) planned",
        sweep_id=plan.sweep_id,
        dry_run=effective_dry_run,
        actor_id=actor_id,
    )

    executor = ActionExecutor(
        gateway=gateway,
        store=store,
        guild_id=plan.guild_id,
        sweep_id=plan.sweep_id,
        dry_run=effective_dry_run,
        now=now,
        action_delay_seconds=config.safety.action_delay_seconds,
        caps={
            Action.FLAG: config.safety.max_flags_per_sweep,
            Action.KICK: config.safety.max_kicks_per_sweep,
            Action.UNFLAG: config.safety.max_unflags_per_sweep,
        },
        kick_reason_template=config.kicking.kick_reason_template,
        dm_before_kick=config.kicking.dm_before_kick,
        post_every_action=config.audit.post_every_action,
        actor_id=actor_id,
    )

    for item in plan.planned:
        snapshot = bundle.snapshots.get(item.user_id)
        left = _days_left(snapshot, policy, now) if snapshot else policy.kick_after_days

        if item.action is Action.FLAG:
            await executor.flag(item.user_id, reason=item.reason, days_left=policy.kick_after_days)
        elif item.action is Action.UNFLAG:
            await executor.unflag(item.user_id, reason=item.reason)
        elif item.action is Action.WARN_FINAL:
            await executor.warn_final(item.user_id, reason=item.reason, days_left=left)
        elif item.action is Action.RETRY_WARN:
            await executor.retry_warn(item.user_id, reason=item.reason, days_left=left)
        elif item.action is Action.KICK:
            reason = config.kicking.kick_reason_template.format(
                min_messages=policy.min_messages,
                window_days=policy.window_days,
                warned_days=policy.kick_after_days,
            )
            await executor.kick(item.user_id, reason=reason)

    await store.set_last_sweep(plan.guild_id, plan.sweep_id)
    await store.add_audit(
        plan.guild_id,
        "sweep_end",
        reason=(
            f"done={executor.report.total_done} failed={executor.report.total_failed}"
        ),
        sweep_id=plan.sweep_id,
        dry_run=effective_dry_run,
        actor_id=actor_id,
        payload={
            "done": {a.value: n for a, n in executor.report.done.items()},
            "failed": {a.value: n for a, n in executor.report.failed.items()},
            "deferred": len(plan.deferred),
        },
    )

    return SweepResult(
        bundle=bundle,
        execution=executor.report,
        dry_run=effective_dry_run,
        downgraded=downgraded,
    )


async def run_sweep(
    *,
    gateway: GuildGateway,
    store: Store,
    config: Config,
    now: datetime,
    dry_run: bool | None = None,
    actor_id: int | None = None,
) -> SweepResult:
    """Plan and then execute, in one call."""
    bundle = await build_sweep_plan(
        gateway=gateway, store=store, config=config, now=now, dry_run=dry_run
    )
    return await execute_plan(
        bundle=bundle,
        gateway=gateway,
        store=store,
        config=config,
        now=now,
        dry_run=dry_run,
        actor_id=actor_id,
    )


async def clear_flag(
    *,
    gateway: GuildGateway,
    store: Store,
    config: Config,
    user_id: int,
    reason: str,
    actor_id: int | None = None,
    new_state: MemberState = MemberState.ACTIVE,
    pardon_days: int | None = None,
    dry_run: bool | None = None,
    now: datetime | None = None,
) -> bool:
    """Immediately clear one member's flag.

    Used by /prune pardon and /prune whitelist add, which act at once rather than
    waiting for the next sweep because a moderator expects to see the result now.
    It grants no grace of its own: only a pardon does that.
    """
    effective_dry_run = config.safety.dry_run if dry_run is None else dry_run
    moment = now or datetime.now(tz=UTC)
    executor = ActionExecutor(
        gateway=gateway,
        store=store,
        guild_id=gateway.guild_id,
        sweep_id="manual",
        dry_run=effective_dry_run,
        now=moment,
        action_delay_seconds=0.0,
        post_every_action=config.audit.post_every_action,
        actor_id=actor_id,
    )
    ok = await executor.unflag(user_id, reason=reason, new_state=new_state)

    if not effective_dry_run:
        stamp = int(moment.timestamp())
        fields: dict[str, object] = {}
        if pardon_days is not None:
            fields["pardoned_until"] = stamp + pardon_days * 86400
            fields["state"] = MemberState.PARDONED
        if fields:
            await store.update_member(gateway.guild_id, user_id, **fields)
    return ok


async def status_for(
    *,
    gateway: GuildGateway,
    store: Store,
    config: Config,
    user_id: int,
    now: datetime,
) -> tuple[MemberSnapshot | None, object]:
    """Build one member's snapshot and decision, for `/prune status`."""
    members = [m for m in await gateway.members() if m.user_id == user_id]
    if not members:
        return None, None
    rows = await store.all_members(gateway.guild_id)
    since_day = window_start_day(now, config.activity.window_days)
    counts = await store.window_counts(gateway.guild_id, since_day)
    whitelist_users, whitelist_roles = await store.whitelist_ids(gateway.guild_id)
    snapshot = build_snapshots(
        members,
        counts=counts,
        rows=rows,
        whitelist_users=whitelist_users,
        whitelist_roles=whitelist_roles,
    )[0]
    return snapshot, evaluate(snapshot, policy_from_config(config), now)
