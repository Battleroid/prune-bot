"""Embed builders.

Kept together so the wording a member sees is in one place and easy to review --
these are messages telling real people they are about to be removed from a
community, and they should read like it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import discord

from ..domain.models import Action, MemberSnapshot, SweepPlan
from ..domain.windows import day_of

FLAG_COLOUR = discord.Color.orange()
KICK_COLOUR = discord.Color.red()
OK_COLOUR = discord.Color.green()
INFO_COLOUR = discord.Color.blurple()

_ACTION_LABEL = {
    Action.FLAG: "Would flag",
    Action.UNFLAG: "Would unflag",
    Action.KICK: "Would kick",
    Action.WARN_FINAL: "Would send a final warning",
    Action.RETRY_WARN: "Would retry the warning",
}


def relative(moment: datetime | None) -> str:
    return discord.utils.format_dt(moment, "R") if moment else "never"


def message_context(
    *,
    guild_name: str,
    member_name: str,
    member_id: int,
    window_days: int,
    min_messages: int,
    days_left: int,
    kick_after_days: int,
) -> dict[str, object]:
    """Values available to the templates in `[messages]`."""
    when = discord.utils.utcnow() + timedelta(days=max(days_left, 0))
    return {
        "guild": guild_name,
        "member": member_name,
        "mention": f"<@{member_id}>",
        "threshold": (
            "posted anything"
            if min_messages <= 1
            else f"posted at least {min_messages} messages"
        ),
        "window_days": window_days,
        "min_messages": min_messages,
        "days_left": days_left,
        "kick_after_days": kick_after_days,
        "deadline": discord.utils.format_dt(when, "R"),
        "deadline_date": discord.utils.format_dt(when, "D"),
    }


def warning_embed(*, messages, context: dict[str, object], final: bool = False):
    """The initial or final warning DM, rendered from the operator's templates."""
    title = messages.final_title if final else messages.warn_title
    body = messages.final_body if final else messages.warn_body
    return discord.Embed(
        title=title.format(**context)[:256],
        colour=KICK_COLOUR if final else FLAG_COLOUR,
        description=body.format(**context)[:4096],
    )


def pre_kick_embed(*, messages, context: dict[str, object]) -> discord.Embed:
    return discord.Embed(
        title=messages.kick_title.format(**context)[:256],
        colour=KICK_COLOUR,
        description=messages.kick_body.format(**context)[:4096],
    )


def status_embed(
    *,
    snapshot: MemberSnapshot,
    decision,
    window_days: int,
    min_messages: int,
    kick_after_days: int,
    daily: dict[int, int],
    now: datetime,
    last_post: datetime | None = None,
    last_active_day: int | None = None,
    days_until_flag: int | None = None,
) -> discord.Embed:
    verdict = {
        Action.FLAG: "Would be flagged by the next sweep",
        Action.KICK: "Would be kicked by the next sweep",
        Action.UNFLAG: "Would be unflagged by the next sweep",
        Action.WARN_FINAL: "Would receive a final warning",
        Action.RETRY_WARN: "Warning would be retried",
        Action.SKIP: "Exempt",
        Action.NONE: "No action pending",
    }[decision.action]

    colour = {
        Action.KICK: KICK_COLOUR,
        Action.FLAG: FLAG_COLOUR,
    }.get(decision.action, OK_COLOUR if decision.action is Action.SKIP else INFO_COLOUR)

    embed = discord.Embed(
        title=f"Prune status: {snapshot.display_name or snapshot.user_id}",
        colour=colour,
        description=f"**{verdict}** ({decision.reason})",
    )
    embed.add_field(
        name=f"Messages in the last {window_days}d",
        value=f"**{snapshot.message_count}** (needs {min_messages})",
        inline=True,
    )
    embed.add_field(name="State", value=snapshot.state.value, inline=True)
    embed.add_field(name="Joined", value=relative(snapshot.joined_at), inline=True)

    # Exact when the bot saw it live. For older posts only the day is known,
    # because history was stored as daily counts.
    if last_post is not None:
        seen = relative(last_post)
    elif last_active_day is not None:
        ago = day_of(now) - last_active_day
        seen = "today" if ago <= 0 else f"about {ago} day{'s' if ago != 1 else ''} ago"
    else:
        seen = "none recorded"
    embed.add_field(name="Last post", value=seen, inline=True)

    flagged = snapshot.state.value == "flagged" or snapshot.has_inactive_role
    if flagged:
        if decision.action is Action.UNFLAG:
            clear = "lifts at the next sweep"
        elif snapshot.forced:
            # Only posts since a forced flag count toward clearing it.
            short = max(0, min_messages - snapshot.messages_since_forced)
            clear = (
                f"{short} more message(s) since the forced flag"
                if short
                else "only a moderator can lift it"
            )
        else:
            short = max(0, min_messages - snapshot.message_count)
            clear = (
                f"{short} more message(s) in the last {window_days}d"
                if short
                else "only a moderator can lift it"
            )
        embed.add_field(name="To clear the flag", value=clear, inline=True)
    elif decision.action is Action.SKIP:
        embed.add_field(name="Flagged in", value="not while exempt", inline=True)
    elif days_until_flag is not None:
        countdown = (
            "at the next sweep"
            if days_until_flag <= 0
            else f"~{days_until_flag} day{'s' if days_until_flag != 1 else ''} "
            f"if they stop posting"
        )
        embed.add_field(name="Flagged in", value=countdown, inline=True)

    if snapshot.flagged_at:
        when = relative(snapshot.flagged_at)
        embed.add_field(
            name="Flagged",
            value=f"{when} (forced by a moderator)" if snapshot.forced else when,
            inline=True,
        )
    if snapshot.warned_at:
        embed.add_field(name="Warned", value=relative(snapshot.warned_at), inline=True)
    elif snapshot.state.value == "flagged":
        embed.add_field(name="Warned", value="**not delivered**", inline=True)

    clock = snapshot.warned_at or snapshot.flagged_at
    if clock:
        embed.add_field(
            name="Kick due",
            value=relative(clock + timedelta(days=kick_after_days)),
            inline=True,
        )

    if daily:
        embed.add_field(
            name="Recent activity", value=sparkline(daily, now, window_days), inline=False
        )

    # Every applicable protection, not just the one that matched first: an admin
    # needs to know whether removing one exemption would actually expose someone.
    embed.add_field(
        name="Exemptions",
        value="\n".join(f"- {e}" for e in decision.exemptions) or "none",
        inline=False,
    )
    return embed


_BLOCKS = " ▁▂▃▄▅▆▇█"


def sparkline(daily: dict[int, int], now: datetime, window_days: int) -> str:
    """A per-day bar chart of message counts across the window."""
    today = day_of(now)
    days = [daily.get(today - offset, 0) for offset in range(window_days - 1, -1, -1)]
    peak = max(days) if days else 0
    if peak == 0:
        return "`" + "_" * len(days) + "`  (no messages)"
    scaled = "".join(
        _BLOCKS[min(len(_BLOCKS) - 1, 1 + int(v / peak * (len(_BLOCKS) - 2)))] if v else "_"
        for v in days
    )
    return f"`{scaled}`  peak {peak}/day"


def plan_embed(plan: SweepPlan, *, dry_run: bool, limit: int = 15) -> discord.Embed:
    if plan.aborted:
        return discord.Embed(
            title="Sweep aborted -- nothing would change",
            colour=KICK_COLOUR,
            description=(
                f"{plan.aborted_reason}\n\n"
                "This is the circuit breaker. It fires when an implausible share of "
                "the server would be actioned, which usually means the activity data "
                "or the thresholds are wrong rather than the members."
            ),
        )

    title = "Sweep preview" if dry_run else "Sweep result"
    embed = discord.Embed(
        title=title,
        colour=INFO_COLOUR,
        description=(
            f"{plan.considered_count} members considered, "
            f"{plan.evaluable_count} evaluable, "
            f"{plan.mutating_count} action(s) planned."
        ),
    )

    for action in (Action.FLAG, Action.KICK, Action.WARN_FINAL, Action.UNFLAG, Action.RETRY_WARN):
        items = plan.of(action)
        if not items:
            continue
        shown = items[:limit]
        lines = [f"- <@{p.user_id}> ({p.reason})" for p in shown]
        if len(items) > limit:
            lines.append(f"- ...and {len(items) - limit} more")
        embed.add_field(
            name=f"{_ACTION_LABEL[action]} ({len(items)})",
            value="\n".join(lines)[:1024],
            inline=False,
        )

    if plan.capped:
        embed.add_field(
            name="Caps hit",
            value=(
                ", ".join(sorted(a.value for a in plan.capped))
                + f" -- {len(plan.deferred)} action(s) roll over to the next sweep"
            ),
            inline=False,
        )

    if plan.skipped:
        top = sorted(plan.skipped.items(), key=lambda kv: -kv[1])[:6]
        embed.add_field(
            name="Skipped",
            value="\n".join(f"- {reason}: {count}" for reason, count in top),
            inline=False,
        )

    embed.set_footer(text=f"sweep {plan.sweep_id}")
    return embed


def summary_embed(result, *, config) -> discord.Embed:
    """Posted to the audit channel at the end of every sweep."""
    plan = result.plan
    if plan.aborted:
        return plan_embed(plan, dry_run=result.dry_run)

    report = result.execution
    prefix = "[DRY RUN] " if result.dry_run else ""
    embed = discord.Embed(
        title=f"{prefix}Sweep complete",
        colour=INFO_COLOUR if result.dry_run else OK_COLOUR,
    )
    if report:
        done = ", ".join(f"{a.value}={n}" for a, n in sorted(report.done.items())) or "nothing"
        embed.add_field(name="Actions", value=done, inline=False)
        if report.total_failed:
            embed.add_field(
                name="Failures",
                value="\n".join(f"- {f}" for f in report.failures[:10])[:1024],
                inline=False,
            )
        if report.warnings_undelivered:
            embed.add_field(
                name="Warnings undelivered",
                value=(
                    f"{report.warnings_undelivered} member(s) have DMs closed. "
                    + (
                        "Their kick clock has not started."
                        if config.safety.require_warning_before_kick
                        else "They will still be kicked on schedule."
                    )
                ),
                inline=False,
            )
    embed.add_field(
        name="Population",
        value=f"{plan.considered_count} considered / {plan.evaluable_count} evaluable",
        inline=True,
    )
    embed.set_footer(text=f"sweep {plan.sweep_id}")
    return embed
