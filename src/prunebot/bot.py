"""The bot object: intents, wiring, and the shared services the cogs use."""

from __future__ import annotations

import logging
from collections import Counter

import discord
from discord.ext import commands

from .config import Config, Secrets, apply_overrides
from .db.store import Store
from .services.activity import ActivityBuffer
from .services.discord_gateway import DiscordGateway
from .ui.verify_button import RetiredVerifyButton

log = logging.getLogger(__name__)

COGS = (
    "prunebot.cogs.tracking",
    "prunebot.cogs.sweeper",
    "prunebot.cogs.admin",
)


def build_intents(config: Config) -> discord.Intents:
    """Only what we actually need.

    `message_content` is deliberately off: without it `content` and friends come
    back empty, but `author`, `id` and `created_at` are always present, and those
    are the only fields this bot reads. Leaving it off means one fewer privileged
    intent to justify.
    """
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True  # privileged: member list, joins, leaves, role updates
    intents.guild_messages = True
    return intents


class PruneBot(commands.Bot):
    def __init__(self, config: Config, secrets: Secrets) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=build_intents(config),
            help_command=None,
            # The message cache is pure overhead here; we never re-read messages.
            max_messages=None,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True
            ),
        )
        self.base_config = config
        self.secrets = secrets
        self.store = Store(secrets.db_path)
        self.activity_buffer = ActivityBuffer(self.store)
        self.started_at = discord.utils.utcnow()
        self._config_cache: dict[int, Config] = {}

    # ------------------------------------------------------------------ lifecycle

    async def setup_hook(self) -> None:
        start, end = await self.store.connect()
        log.info("database ready (schema %s -> %s) at %s", start, end, self.store.path)

        # Warnings no longer carry a button, but ones already sent still do.
        # Registering the retired handler answers those with an explanation
        # rather than Discord's generic "This interaction failed".
        self.add_dynamic_items(RetiredVerifyButton)

        for cog in COGS:
            await self.load_extension(cog)
        log.info("loaded %d cogs", len(COGS))

        # Guild-scoped sync is instant; global propagation takes up to an hour.
        for guild_id in self.base_config.bot.guild_ids:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d commands to guild %s", len(synced), guild_id)

        if self.secrets.panic:
            log.warning(
                "PRUNE_PANIC is set: no sweep will run until it is cleared and the "
                "container is restarted"
            )

    async def on_ready(self) -> None:
        log.info("connected as %s (%s)", self.user, self.user.id)
        for guild in self.guilds:
            if guild.id not in self.base_config.bot.guild_ids:
                log.warning(
                    "in guild %s (%s) which is not in bot.guild_ids; ignoring it",
                    guild.name,
                    guild.id,
                )
                continue
            config = await self.config_for(guild.id, refresh=True)
            await self.store.seed_whitelist(
                guild.id,
                user_ids=config.whitelist.user_ids,
                role_ids=config.whitelist.role_ids,
            )
            await self._warn_about_setup(guild, config)

    async def _warn_about_setup(self, guild: discord.Guild, config: Config) -> None:
        """Surface the misconfigurations that otherwise show up as silent 403s."""
        problems = []
        role = guild.get_role(config.flagging.inactive_role_id)
        if config.flagging.inactive_role_id and role is None:
            problems.append("the configured inactive role does not exist in this guild")
        elif role and guild.me.top_role <= role:
            problems.append(
                f"my highest role is not above {role.name}, so I cannot assign it"
            )
        if not guild.me.guild_permissions.manage_roles:
            problems.append("I am missing Manage Roles")

        # Having the permission is not enough: Discord requires the bot's top role
        # to be STRICTLY above a member's top role, so sharing a role with the
        # membership silently blocks everything while looking correctly configured.
        blocked = [
            m
            for m in guild.members
            if not m.bot and m.id != guild.owner_id and not guild.me.top_role > m.top_role
        ]
        humans = [m for m in guild.members if not m.bot and m.id != guild.owner_id]
        if humans and len(blocked) / len(humans) > 0.1:
            share = len(blocked) / len(humans)
            worst = Counter(m.top_role.name for m in blocked).most_common(3)
            listed = ", ".join(f"{name} ({n})" for name, n in worst)
            problems.append(
                f"I cannot manage {len(blocked)} of {len(humans)} members "
                f"({share:.0%}) because their highest role is not below mine "
                f"(mine is {guild.me.top_role.name}). Mostly: {listed}. "
                f"Drag my role above theirs in Server Settings -> Roles."
            )
        if config.kicking.enabled and not guild.me.guild_permissions.kick_members:
            problems.append("kicking is enabled but I am missing Kick Members")
        announce_id = config.flagging.announce_channel_id
        if announce_id:
            channel = guild.get_channel(announce_id)
            if channel is None:
                problems.append("the configured announcement channel does not exist")
            else:
                perms = channel.permissions_for(guild.me)
                if not (perms.view_channel and perms.send_messages):
                    problems.append(
                        f"I cannot post flag announcements in #{channel.name}: "
                        f"missing View Channel or Send Messages"
                    )

        if config.audit.channel_id:
            channel = guild.get_channel(config.audit.channel_id)
            if channel is None:
                problems.append("the configured audit channel does not exist")
            else:
                # Checked explicitly because the audit trail is how you find out
                # anything else went wrong -- a silent failure here is the worst
                # kind. Note the irony: if this is what is broken, the warning
                # about it can only reach the container log.
                perms = channel.permissions_for(guild.me)
                missing = [
                    name
                    for name, ok in (
                        ("View Channel", perms.view_channel),
                        ("Send Messages", perms.send_messages),
                        ("Embed Links", perms.embed_links),
                    )
                    if not ok
                ]
                if missing:
                    problems.append(
                        f"I cannot post to the audit channel #{channel.name}: "
                        f"missing {', '.join(missing)}"
                    )

        for problem in problems:
            log.warning("guild %s: %s", guild.id, problem)
        if problems:
            gateway = self.gateway(guild.id)
            if gateway:
                await gateway.post_audit(
                    "\n".join(f"- {p}" for p in problems),
                    title="Prune bot needs attention",
                    alert=True,
                )

    async def close(self) -> None:
        # Flush before the connection goes away, or a redeploy silently loses up to
        # flush_interval_seconds of message counts.
        try:
            written = await self.activity_buffer.flush()
            if written:
                log.info("flushed %d activity bucket(s) on shutdown", written)
        except Exception:
            log.exception("failed to flush activity on shutdown")
        await super().close()
        await self.store.close()

    # -------------------------------------------------------------------- helpers

    async def config_for(self, guild_id: int, *, refresh: bool = False) -> Config:
        """Base config with this guild's runtime overrides applied."""
        if refresh or guild_id not in self._config_cache:
            overrides = await self.store.overrides(guild_id)
            self._config_cache[guild_id] = apply_overrides(self.base_config, overrides)
        return self._config_cache[guild_id]

    def cached_config(self, guild_id: int) -> Config:
        """Synchronous read for hot paths such as on_message."""
        return self._config_cache.get(guild_id, self.base_config)

    def invalidate_config(self, guild_id: int) -> None:
        self._config_cache.pop(guild_id, None)

    def gateway(self, guild_id: int) -> DiscordGateway | None:
        guild = self.get_guild(guild_id)
        if guild is None:
            return None
        return DiscordGateway(guild, self.cached_config(guild_id))

    async def gateway_for(self, guild_id: int) -> DiscordGateway | None:
        guild = self.get_guild(guild_id)
        if guild is None:
            return None
        return DiscordGateway(guild, await self.config_for(guild_id))

    def is_managed(self, guild_id: int) -> bool:
        return guild_id in self.base_config.bot.guild_ids
