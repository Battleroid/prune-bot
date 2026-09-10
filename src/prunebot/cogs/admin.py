"""The `/prune` command group.

Permission model, deliberately doubled up: `default_permissions` on the Group
constructor is only a UI hint that server admins can override in their client, and
Discord does not honour it on subcommands at all. So there is also a server-side
`interaction_check`, which is the one that actually decides.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from ..config import RUNTIME_SAFE_KEYS, ConfigError, parse_override
from ..domain.models import Action, MemberState
from ..domain.windows import window_start_day
from ..services.backfill import BackfillRunner
from ..services.sweep import build_sweep_plan, clear_flag, status_for
from ..ui.confirm import ConfirmView
from ..ui.embeds import INFO_COLOUR, OK_COLOUR, plan_embed, relative, status_embed
from ..ui.paginator import Paginator, chunk_lines

log = logging.getLogger(__name__)

Target = discord.Member | discord.Role


def may_moderate(interaction: discord.Interaction, config) -> bool:
    """Whether this member may run the /prune commands.

    Checked server-side on every invocation. `default_permissions` only decides
    who *sees* a command in the picker and server admins can override it, so it
    is a hint, never the gate.
    """
    user = interaction.user
    guild = interaction.guild

    # The guild owner is always allowed. Without this an operator could lock
    # themselves out with a bad allowlist and only recover by editing the config
    # file and restarting the container.
    if guild is not None and user.id == guild.owner_id:
        return True

    access = config.access
    if user.id in set(access.user_ids):
        return True
    allowed_roles = set(access.role_ids)
    if allowed_roles and any(r.id in allowed_roles for r in getattr(user, "roles", ())):
        return True
    if access.allow_manage_guild:
        perms = getattr(user, "guild_permissions", None)
        return bool(perms and perms.manage_guild)
    return False


#: Deliberately says nothing about who *is* allowed. Because configuring an
#: allowlist makes /prune visible to everyone, a message naming the permitted
#: roles or users would let any member enumerate the moderators just by running
#: a command they cannot use.
DENIAL = "You do not have permission to use this command."


def whitelist_line(entry, guild) -> str:
    """One rendered whitelist row. Mentions resolve to names in the client."""
    if entry.kind == "role":
        obj = guild.get_role(entry.target_id) if guild else None
        label = obj.mention if obj else f"`{entry.target_id}` *(deleted role)*"
    else:
        obj = guild.get_member(entry.target_id) if guild else None
        label = obj.mention if obj else f"`{entry.target_id}` *(not in server)*"
    by = f" by <@{entry.added_by}>" if entry.added_by else ""
    # The config-seed reason is on almost every row and carries no information;
    # showing it 60 times would push out entries that actually matter.
    reason = entry.reason or ""
    why = "" if reason.startswith("seeded from") else (f" -- {reason}" if reason else "")
    return f"- {label}{by}{why}"


def build_whitelist_pages(entries, guild) -> list[discord.Embed]:
    """Render the whole whitelist across as many pages as it needs.

    Truncating here would be actively misleading: someone checking who is
    protected has to be able to see all of them.
    """
    users = [e for e in entries if e.kind == "user"]
    roles = [e for e in entries if e.kind == "role"]

    sections: list[tuple[str, list[str]]] = []
    for label, group in (("Roles", roles), ("Members", users)):
        for chunk in chunk_lines([whitelist_line(e, guild) for e in group]):
            sections.append((label, chunk))

    total = f"{len(users)} member(s), {len(roles)} role(s)"
    embeds: list[discord.Embed] = []
    # Two field-sized chunks per page keeps each embed well inside the 6000
    # character cap while still filling the screen.
    for i in range(0, len(sections), 2):
        embed = discord.Embed(
            title="Prune whitelist",
            colour=OK_COLOUR,
            description=f"Never flagged, never kicked. {total}.",
        )
        for label, chunk in sections[i : i + 2]:
            embed.add_field(
                name=f"{label} ({len(chunk)} shown)",
                value="\n".join(chunk),
                inline=False,
            )
        embeds.append(embed)

    for n, embed in enumerate(embeds, 1):
        embed.set_footer(
            text=f"Page {n}/{len(embeds)} - tidy up stale entries with /prune whitelist remove"
        )
    return embeds


async def _whitelist_add(
    interaction: discord.Interaction, target: Target, reason: str | None
) -> str:
    """Shared by the command and the right-click context menu."""
    bot = interaction.client
    guild_id = interaction.guild_id
    kind = "role" if isinstance(target, discord.Role) else "user"

    added = await bot.store.whitelist_add(
        guild_id,
        kind,
        target.id,
        added_by=interaction.user.id,
        reason=reason,
    )
    if not added:
        return f"{target.mention} is already whitelisted."

    await bot.store.add_audit(
        guild_id,
        "whitelist_add",
        user_id=target.id if kind == "user" else None,
        reason=reason or f"{kind} whitelisted",
        actor_id=interaction.user.id,
    )

    # Act now rather than at the next sweep: an admin pressing this expects to see
    # the role come off immediately.
    cleared = ""
    if kind == "user":
        row = await bot.store.get_member(guild_id, target.id)
        if row and row.state is MemberState.FLAGGED:
            gateway = await bot.gateway_for(guild_id)
            config = await bot.config_for(guild_id)
            if gateway and await clear_flag(
                gateway=gateway,
                store=bot.store,
                config=config,
                user_id=target.id,
                reason="whitelisted",
                actor_id=interaction.user.id,
                dry_run=False,
            ):
                cleared = " They were flagged, so I removed the inactive role."

    scope = (
        "Everyone holding this role is now exempt."
        if kind == "role"
        else "They will never be flagged or kicked."
    )
    return f"Whitelisted {target.mention}. {scope}{cleared}"


class WhitelistGroup(app_commands.Group):
    """Permanent exemption. Contrast `/prune pardon`, which expires."""

    @app_commands.command(name="add", description="Exempt a member or role from pruning, permanently.")
    @app_commands.describe(
        target="The member or role that should never be pruned",
        reason="Why (shown in /prune whitelist list)",
    )
    async def add(
        self, interaction: discord.Interaction, target: Target, reason: str | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            await _whitelist_add(interaction, target, reason), ephemeral=True
        )

    @app_commands.command(name="remove", description="Remove a member or role from the whitelist.")
    async def remove(self, interaction: discord.Interaction, target: Target) -> None:
        bot = interaction.client
        kind = "role" if isinstance(target, discord.Role) else "user"
        removed = await bot.store.whitelist_remove(interaction.guild_id, kind, target.id)
        if removed:
            await bot.store.add_audit(
                interaction.guild_id,
                "whitelist_remove",
                user_id=target.id if kind == "user" else None,
                actor_id=interaction.user.id,
            )
        await interaction.response.send_message(
            f"Removed {target.mention} from the whitelist. They can be pruned again."
            if removed
            else f"{target.mention} was not on the whitelist.",
            ephemeral=True,
        )

    @app_commands.command(name="list", description="Show everyone who can never be pruned.")
    async def list_entries(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        guild = interaction.guild
        entries = await bot.store.whitelist_entries(interaction.guild_id)
        if not entries:
            await interaction.response.send_message(
                "The whitelist is empty. Add someone with `/prune whitelist add`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        embeds = build_whitelist_pages(entries, guild)
        if len(embeds) == 1:
            await interaction.followup.send(embed=embeds[0], ephemeral=True)
            return

        view = Paginator(embeds, requester_id=interaction.user.id)
        await interaction.followup.send(
            embed=view.current, view=view, ephemeral=True
        )


class ConfigGroup(app_commands.Group):
    @app_commands.command(name="show", description="Show the effective configuration.")
    async def show(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        config = await bot.config_for(interaction.guild_id, refresh=True)
        overrides = await bot.store.overrides(interaction.guild_id)

        embed = discord.Embed(
            title="Prune configuration",
            colour=INFO_COLOUR if config.safety.dry_run else discord.Color.red(),
            description=(
                "**DRY RUN is on** -- nothing will actually be changed."
                if config.safety.dry_run
                else "**DRY RUN is off** -- this bot will assign roles and kick members."
            ),
        )
        embed.add_field(
            name="Activity",
            value=(
                f"under **{config.activity.min_messages}** message(s) in "
                f"**{config.activity.window_days}** days is inactive\n"
                f"threads: {config.activity.count_threads} - "
                f"forums: {config.activity.count_forum_posts}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Kicking",
            value=(
                f"enabled: **{config.kicking.enabled}**\n"
                f"after **{config.kicking.kick_after_days}** days flagged\n"
                f"default pardon: {config.kicking.reverify_grace_days}d - "
                f"posting clears the flag: {config.kicking.auto_clear_on_activity}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Caps",
            value=(
                f"flags {config.safety.max_flags_per_sweep}/sweep - "
                f"kicks {config.safety.max_kicks_per_sweep}/sweep\n"
                f"abort over {config.safety.abort_if_flag_ratio_over:.0%} flagged "
                f"or {config.safety.abort_if_kick_ratio_over:.0%} kicked "
                f"(above {config.safety.breaker_min_evaluable} evaluable)"
            ),
            inline=False,
        )
        embed.add_field(
            name="Runtime overrides",
            value="\n".join(f"- `{k}` = `{v}`" for k, v in overrides.items()) or "none",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="set", description="Change a runtime-safe setting.")
    @app_commands.describe(key="Setting to change", value="New value")
    async def set_value(
        self, interaction: discord.Interaction, key: str, value: str
    ) -> None:
        bot = interaction.client
        try:
            parsed = parse_override(key, value)
        except ConfigError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await bot.store.set_override(
            interaction.guild_id, key, parsed, set_by=interaction.user.id
        )
        bot.invalidate_config(interaction.guild_id)
        try:
            await bot.config_for(interaction.guild_id, refresh=True)
        except Exception as exc:
            # The new value validates in isolation but breaks a cross-section rule.
            await bot.store.clear_override(interaction.guild_id, key)
            bot.invalidate_config(interaction.guild_id)
            await interaction.response.send_message(
                f"Rejected: that would make the configuration invalid.\n```{exc}```",
                ephemeral=True,
            )
            return

        await bot.store.add_audit(
            interaction.guild_id,
            "config_change",
            reason=f"{key} = {parsed}",
            actor_id=interaction.user.id,
        )
        extra = ""
        if key == "safety.dry_run" and parsed is False:
            extra = "\n\n**Dry run is now OFF.** The next sweep will really act."
        await interaction.response.send_message(
            f"Set `{key}` = `{parsed}`.{extra}", ephemeral=True
        )

    @set_value.autocomplete("key")
    async def _key_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=k, value=k)
            for k in sorted(RUNTIME_SAFE_KEYS)
            if current.lower() in k.lower()
        ][:25]

    @app_commands.command(name="clear", description="Remove a runtime override.")
    async def clear(self, interaction: discord.Interaction, key: str) -> None:
        bot = interaction.client
        removed = await bot.store.clear_override(interaction.guild_id, key)
        bot.invalidate_config(interaction.guild_id)
        await interaction.response.send_message(
            f"Cleared `{key}`; the config.toml value applies again."
            if removed
            else f"No override set for `{key}`.",
            ephemeral=True,
        )


class PruneGroup(app_commands.Group):
    whitelist = WhitelistGroup(
        name="whitelist", description="Members and roles that can never be pruned"
    )
    config = ConfigGroup(name="config", description="Inspect and adjust settings")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The real permission gate. default_permissions is only a client-side hint."""
        config = await interaction.client.config_for(interaction.guild_id)
        if not may_moderate(interaction, config):
            await interaction.response.send_message(DENIAL, ephemeral=True)
            return False
        if not interaction.client.is_managed(interaction.guild_id):
            await interaction.response.send_message(
                "I'm not configured to manage this server.", ephemeral=True
            )
            return False
        return True

    # ------------------------------------------------------------------- status

    @app_commands.command(name="status", description="Show one member's activity and standing.")
    async def status(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        bot = interaction.client
        member = member or interaction.user
        await interaction.response.defer(ephemeral=True)

        config = await bot.config_for(interaction.guild_id)
        gateway = await bot.gateway_for(interaction.guild_id)
        now = discord.utils.utcnow()
        snapshot, decision = await status_for(
            gateway=gateway,
            store=bot.store,
            config=config,
            user_id=member.id,
            now=now,
        )
        if snapshot is None:
            await interaction.followup.send("That member is not in this server.", ephemeral=True)
            return

        daily = await bot.store.user_daily(
            interaction.guild_id,
            member.id,
            window_start_day(now, config.activity.window_days),
        )
        await interaction.followup.send(
            embed=status_embed(
                snapshot=snapshot,
                decision=decision,
                window_days=config.activity.window_days,
                min_messages=config.activity.min_messages,
                kick_after_days=config.kicking.kick_after_days,
                daily=daily,
                now=now,
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ preview

    @app_commands.command(
        name="preview", description="Show what the next sweep would do. Changes nothing."
    )
    async def preview(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        await interaction.response.defer(ephemeral=True)
        config = await bot.config_for(interaction.guild_id, refresh=True)
        gateway = await bot.gateway_for(interaction.guild_id)

        await bot.activity_buffer.flush()
        bundle = await build_sweep_plan(
            gateway=gateway,
            store=bot.store,
            config=config,
            now=discord.utils.utcnow(),
            dry_run=True,  # keeps reconciliation read-only
        )
        embed = plan_embed(bundle.plan, dry_run=True)
        if bundle.blocked_reason:
            embed.add_field(
                name="These numbers are not trustworthy yet",
                value=(
                    f"{bundle.blocked_reason}\n\nMembers will look inactive simply "
                    f"because their messages have not been counted. Acting is "
                    f"blocked until this is resolved."
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------------------------------------------------------------- run

    @app_commands.command(name="run", description="Run a sweep now.")
    @app_commands.describe(
        dry_run="Override the configured dry-run setting for this run only"
    )
    async def run(
        self, interaction: discord.Interaction, dry_run: bool | None = None
    ) -> None:
        bot = interaction.client
        config = await bot.config_for(interaction.guild_id, refresh=True)
        effective = config.safety.dry_run if dry_run is None else dry_run
        sweeper = bot.get_cog("Sweeper")

        if effective:
            await interaction.response.defer(ephemeral=True)
            result = await sweeper.run_for(
                interaction.guild_id, dry_run=True, actor_id=interaction.user.id
            )
            await self._send_result(interaction, result)
            return

        # A live run gets a confirmation showing the real numbers first.
        await interaction.response.defer(ephemeral=True)
        gateway = await bot.gateway_for(interaction.guild_id)
        bundle = await build_sweep_plan(
            gateway=gateway,
            store=bot.store,
            config=config,
            now=discord.utils.utcnow(),
            dry_run=True,
        )
        plan = bundle.plan
        if plan.aborted or plan.mutating_count == 0:
            await interaction.followup.send(
                embed=plan_embed(plan, dry_run=True), ephemeral=True
            )
            return

        view = ConfirmView(requester_id=interaction.user.id)
        await interaction.followup.send(
            content=(
                f"**This will really act.** "
                f"{plan.count(Action.FLAG)} to flag, "
                f"{plan.count(Action.KICK)} to kick, "
                f"{plan.count(Action.UNFLAG)} to unflag."
            ),
            embed=plan_embed(plan, dry_run=True),
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.value:
            return

        result = await sweeper.run_for(
            interaction.guild_id, dry_run=False, actor_id=interaction.user.id
        )
        await self._send_result(interaction, result)

    @staticmethod
    async def _send_result(interaction: discord.Interaction, result) -> None:
        if result is None:
            bot = interaction.client
            config = await bot.config_for(interaction.guild_id)
            up = (discord.utils.utcnow() - bot.started_at).total_seconds() / 60
            need = config.safety.min_uptime_before_sweep_minutes
            await interaction.followup.send(
                f"The sweep did not run: I have only been up **{up:.1f} min** and "
                f"`safety.min_uptime_before_sweep_minutes` is **{need}**. This guard "
                f"exists so a sweep never runs against a half-loaded member list.\n\n"
                f"Either wait **{max(0.0, need - up):.1f} min**, or lower it now with "
                f"`/prune config set safety.min_uptime_before_sweep_minutes 0`.",
                ephemeral=True,
            )
            return
        embed = plan_embed(result.plan, dry_run=result.dry_run)
        if result.downgraded:
            embed.add_field(
                name="Downgraded to a dry run",
                value=result.bundle.blocked_reason or "a safety gate blocked acting",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------- pardon

    @app_commands.command(name="pardon", description="Temporarily protect a member and unflag them.")
    @app_commands.describe(
        member="Who to pardon",
        days="How long the pardon lasts (default: kicking.reverify_grace_days)",
        reason="Recorded in the audit log",
    )
    async def pardon(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        days: app_commands.Range[int, 1, 3650] | None = None,
        reason: str | None = None,
    ) -> None:
        bot = interaction.client
        await interaction.response.defer(ephemeral=True)
        config = await bot.config_for(interaction.guild_id)
        gateway = await bot.gateway_for(interaction.guild_id)
        span = days or config.kicking.reverify_grace_days

        await clear_flag(
            gateway=gateway,
            store=bot.store,
            config=config,
            user_id=member.id,
            reason=reason or "pardoned by a moderator",
            actor_id=interaction.user.id,
            pardon_days=span,
            dry_run=False,
        )
        until = datetime.fromtimestamp(time.time() + span * 86400, tz=discord.utils.utcnow().tzinfo)
        await interaction.followup.send(
            f"Pardoned {member.mention} until {discord.utils.format_dt(until, 'D')} "
            f"({span} days). For a permanent exemption use `/prune whitelist add`.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------ backfill

    @app_commands.command(
        name="backfill", description="Rescan channel history to rebuild activity counts."
    )
    @app_commands.describe(
        channel="Only scan this channel", force="Rescan even if already completed"
    )
    async def backfill(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        force: bool = False,
    ) -> None:
        bot = interaction.client
        await interaction.response.defer(ephemeral=True)
        config = await bot.config_for(interaction.guild_id)

        if force:
            await bot.store.reset_backfill(interaction.guild_id)

        runner = BackfillRunner(
            store=bot.store,
            config=config,
            guild=interaction.guild,
            before=discord.utils.utcnow(),
        )
        await interaction.followup.send(
            "Scanning history. This can take a while on a busy server; "
            "I'll report back here when it's done.",
            ephemeral=True,
        )
        try:
            await runner.run(channels=[channel] if channel else None)
        except Exception as exc:
            log.exception("manual backfill failed")
            await interaction.followup.send(f"Backfill failed: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(
            embed=discord.Embed(
                title="Backfill complete", description=runner.summary, colour=OK_COLOUR
            ),
            ephemeral=True,
        )

    # -------------------------------------------------------------------- stats

    @app_commands.command(name="stats", description="Overall prune statistics for this server.")
    async def stats(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        await interaction.response.defer(ephemeral=True)
        config = await bot.config_for(interaction.guild_id)
        now = discord.utils.utcnow()

        counts = await bot.store.window_counts(
            interaction.guild_id, window_start_day(now, config.activity.window_days)
        )
        by_state = await bot.store.count_by_state(interaction.guild_id)
        meta = await bot.store.guild_meta(interaction.guild_id)

        humans = [m for m in interaction.guild.members if not m.bot]
        silent = sum(
            1 for m in humans if counts.get(m.id, 0) < config.activity.min_messages
        )

        embed = discord.Embed(title="Prune statistics", colour=INFO_COLOUR)
        embed.add_field(name="Members (excluding bots)", value=str(len(humans)), inline=True)
        embed.add_field(
            name=f"Below threshold ({config.activity.window_days}d)",
            value=f"{silent} ({silent / max(len(humans), 1):.0%})",
            inline=True,
        )
        embed.add_field(name="Currently flagged", value=str(by_state.get("flagged", 0)), inline=True)
        embed.add_field(name="Pardoned", value=str(by_state.get("pardoned", 0)), inline=True)
        embed.add_field(name="Kicked (all time)", value=str(by_state.get("kicked", 0)), inline=True)
        embed.add_field(
            name="Backfill",
            value=(
                f"completed {relative(datetime.fromtimestamp(meta['backfilled_at'], tz=now.tzinfo))}"
                if meta.get("backfilled_at")
                else "**not done** -- flagging and kicking are blocked"
            ),
            inline=False,
        )
        embed.add_field(
            name="Dry run",
            value="on -- nothing is actually changed" if config.safety.dry_run else "**off**",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ history

    @app_commands.command(name="history", description="What the bot has done to one member.")
    async def history(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        bot = interaction.client
        rows = await bot.store.user_history(interaction.guild_id, member.id, limit=15)
        if not rows:
            await interaction.response.send_message(
                f"Nothing recorded for {member.mention}.", ephemeral=True
            )
            return
        lines = []
        for row in rows:
            when = discord.utils.format_dt(
                datetime.fromtimestamp(row["created_at"], tz=discord.utils.utcnow().tzinfo), "R"
            )
            tag = " *(dry run)*" if row["dry_run"] else ""
            actor = f" by <@{row['actor_id']}>" if row["actor_id"] else ""
            lines.append(f"- `{row['action']}`{tag} {when}{actor} -- {row['reason'] or ''}")
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"History for {member.display_name}",
                description="\n".join(lines)[:4000],
                colour=INFO_COLOUR,
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------- backup

    @app_commands.command(name="backup", description="Write a consistent copy of the database.")
    async def backup(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        await interaction.response.defer(ephemeral=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = bot.store.path.parent / "backups" / f"prune-{stamp}.db"
        try:
            written = await bot.store.backup_to(target)
        except Exception as exc:
            await interaction.followup.send(f"Backup failed: `{exc}`", ephemeral=True)
            return
        size = written.stat().st_size / 1024
        await interaction.followup.send(
            f"Wrote `{written}` ({size:.0f} KiB). It lives in the `prune-data` volume.",
            ephemeral=True,
        )

    # --------------------------------------------------------------------- sync

    @app_commands.command(name="sync", description="Re-sync slash commands (bot owner only).")
    async def sync(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        if not await bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "Only the bot owner can re-sync commands.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        guild = discord.Object(id=interaction.guild_id)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        await interaction.followup.send(f"Synced {len(synced)} commands.", ephemeral=True)


class Admin(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        # default_permissions decides who SEES the command. When an explicit
        # allowlist is configured, restricting it to Manage Server would hide
        # /prune from exactly the moderators the operator just granted access to,
        # so the visibility hint is dropped and the server-side check does the work.
        access = bot.base_config.access
        visibility = (
            None
            if (access.role_ids or access.user_ids)
            else discord.Permissions(manage_guild=True)
        )
        self.group = PruneGroup(
            name="prune",
            description="Inactivity pruning: status, previews, whitelist and settings",
            default_permissions=visibility,
            guild_only=True,
        )
        bot.tree.add_command(self.group)

        self.context_menu = app_commands.ContextMenu(
            name="Whitelist User",
            callback=self.whitelist_context,
        )
        self.context_menu.default_permissions = visibility
        self.context_menu.guild_only = True
        bot.tree.add_command(self.context_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.group.name, type=discord.AppCommandType.chat_input)
        self.bot.tree.remove_command(
            self.context_menu.name, type=discord.AppCommandType.user
        )

    async def whitelist_context(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        """Right-click a member -> Apps -> Whitelist User."""
        config = await self.bot.config_for(interaction.guild_id)
        if not may_moderate(interaction, config):
            await interaction.response.send_message(DENIAL, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            await _whitelist_add(interaction, member, "added via right-click"),
            ephemeral=True,
        )


async def setup(bot) -> None:
    await bot.add_cog(Admin(bot))
