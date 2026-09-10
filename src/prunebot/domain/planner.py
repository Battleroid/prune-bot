"""Planning: turn snapshots into an immutable SweepPlan.

This is phase 1 of the sweep and performs no I/O whatsoever. `/prune preview`
renders exactly this object, which is what guarantees that a preview and a real run
agree -- they are literally the same computation.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from .eligibility import evaluate
from .models import (
    Action,
    GuildPolicy,
    MemberSnapshot,
    PlannedAction,
    SweepPlan,
)

#: Per-action caps. Actions not listed here are uncapped (they are all no-ops).
_CAP_FIELDS: dict[Action, str] = {
    Action.FLAG: "max_flags_per_sweep",
    Action.KICK: "max_kicks_per_sweep",
    Action.UNFLAG: "max_unflags_per_sweep",
}


def _order_key(action: Action, snapshot: MemberSnapshot) -> tuple[float, int]:
    """Stable ordering, so previews are reproducible and caps are fair.

    Kicks and final warnings are ordered oldest-clock-first: when a cap defers some
    of them, the people who have been waiting longest are handled first rather than
    whoever happens to have the lowest snowflake.
    """
    if action in (Action.KICK, Action.WARN_FINAL):
        clock = snapshot.warned_at or snapshot.flagged_at
        return (clock.timestamp() if clock else 0.0, snapshot.user_id)
    return (0.0, snapshot.user_id)


def build_plan(
    *,
    guild_id: int,
    sweep_id: str,
    snapshots: list[MemberSnapshot],
    policy: GuildPolicy,
    now: datetime,
) -> SweepPlan:
    """Evaluate every member and assemble the plan, applying breakers and caps."""
    candidates: list[tuple[Action, PlannedAction, MemberSnapshot]] = []
    skipped: Counter[str] = Counter()

    for snapshot in snapshots:
        decision = evaluate(snapshot, policy, now)
        if decision.action is Action.SKIP:
            skipped[decision.reason] += 1
            continue
        if decision.action is Action.NONE:
            continue
        candidates.append(
            (
                decision.action,
                PlannedAction(
                    user_id=snapshot.user_id,
                    action=decision.action,
                    reason=decision.reason,
                    display_name=snapshot.display_name,
                ),
                snapshot,
            )
        )

    considered = len(snapshots)
    evaluable = considered - sum(skipped.values())

    base = SweepPlan(
        guild_id=guild_id,
        sweep_id=sweep_id,
        considered_count=considered,
        evaluable_count=evaluable,
        skipped=dict(skipped),
    )

    flag_count = sum(1 for a, _, _ in candidates if a is Action.FLAG)
    kick_count = sum(1 for a, _, _ in candidates if a is Action.KICK)

    # --- circuit breaker ------------------------------------------------------
    # If an implausible share of the server is about to be actioned, something is
    # wrong (wiped database, mistyped window) and the right answer is to do nothing
    # at all rather than to do a capped amount of damage.
    denominator = max(evaluable, 1)
    if evaluable >= policy.breaker_min_evaluable:
        flag_ratio = flag_count / denominator
        if flag_ratio > policy.abort_if_flag_ratio_over:
            return _replace(
                base,
                aborted_reason=(
                    f"flag ratio {flag_ratio:.0%} exceeds the "
                    f"{policy.abort_if_flag_ratio_over:.0%} limit "
                    f"({flag_count} of {evaluable} evaluable members)"
                ),
            )
        kick_ratio = kick_count / denominator
        if kick_ratio > policy.abort_if_kick_ratio_over:
            return _replace(
                base,
                aborted_reason=(
                    f"kick ratio {kick_ratio:.0%} exceeds the "
                    f"{policy.abort_if_kick_ratio_over:.0%} limit "
                    f"({kick_count} of {evaluable} evaluable members)"
                ),
            )

    # --- caps ------------------------------------------------------------------
    candidates.sort(key=lambda item: _order_key(item[0], item[2]))

    used: Counter[Action] = Counter()
    planned: list[PlannedAction] = []
    deferred: list[PlannedAction] = []
    capped: set[Action] = set()

    for action, plan_item, _snapshot in candidates:
        field = _CAP_FIELDS.get(action)
        if field is None:
            planned.append(plan_item)
            continue
        limit = getattr(policy, field)
        if used[action] >= limit:
            capped.add(action)
            deferred.append(plan_item)
            continue
        used[action] += 1
        planned.append(plan_item)

    return _replace(
        base,
        planned=tuple(planned),
        deferred=tuple(deferred),
        capped=frozenset(capped),
    )


def _replace(plan: SweepPlan, **changes: object) -> SweepPlan:
    """dataclasses.replace, spelled out to keep SweepPlan frozen and slotted."""
    return SweepPlan(
        guild_id=changes.get("guild_id", plan.guild_id),  # type: ignore[arg-type]
        sweep_id=changes.get("sweep_id", plan.sweep_id),  # type: ignore[arg-type]
        planned=changes.get("planned", plan.planned),  # type: ignore[arg-type]
        evaluable_count=changes.get("evaluable_count", plan.evaluable_count),  # type: ignore[arg-type]
        considered_count=changes.get("considered_count", plan.considered_count),  # type: ignore[arg-type]
        skipped=changes.get("skipped", plan.skipped),  # type: ignore[arg-type]
        capped=changes.get("capped", plan.capped),  # type: ignore[arg-type]
        aborted_reason=changes.get("aborted_reason", plan.aborted_reason),  # type: ignore[arg-type]
        deferred=changes.get("deferred", plan.deferred),  # type: ignore[arg-type]
    )
