"""The live GuildGateway: the only module that mutates Discord.

Every method pre-checks permissions and role hierarchy from cache before making a
request. That is not only about correctness -- 10,000 401/403/429 responses in ten
minutes earns a temporary Cloudflare ban for the whole IP, so "try it and catch
Forbidden" is not an acceptable pattern at scale.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import discord

from ..config import Config
from ..domain.models import MemberInfo
from ..gateway import ActionResult, WarnKind, WarnResult
from ..ui.embeds import message_context, pre_kick_embed, warning_embed

log = logging.getLogger(__name__)


class DiscordGateway:
    """Wraps one guild."""

    def __init__(self, guild: discord.Guild, config: Config) -> None:
        self.guild = guild
        self.config = config

    @property
    def guild_id(self) -> int:
        return self.guild.id

    @property
    def inactive_role(self) -> discord.Role | None:
        return self.guild.get_role(self.config.flagging.inactive_role_id)

    # ----------------------------------------------------------------- observation

    def _member_info(self, member: discord.Member) -> MemberInfo:
        me = self.guild.me
        role = self.inactive_role
        perms = me.guild_permissions
        # Guild owners bypass hierarchy entirely and can never be kicked by a bot.
        above = me.top_role > member.top_role and member.id != self.guild.owner_id
        return MemberInfo(
            user_id=member.id,
            display_name=member.display_name,
            is_bot=member.bot,
            is_owner=member.id == self.guild.owner_id,
            joined_at=member.joined_at,
            role_ids=frozenset(r.id for r in member.roles),
            has_inactive_role=role is not None and role in member.roles,
            bot_can_manage=bool(
                perms.manage_roles and above and role is not None and me.top_role > role
            ),
            bot_can_kick=bool(perms.kick_members and above),
        )

    async def members(self) -> Sequence[MemberInfo]:
        return [self._member_info(m) for m in self.guild.members]

    # -------------------------------------------------------------------- mutation

    async def add_inactive_role(self, user_id: int, *, reason: str) -> ActionResult:
        member = self.guild.get_member(user_id)
        role = self.inactive_role
        if member is None:
            return ActionResult.failed("member is no longer in the guild")
        if role is None:
            return ActionResult.failed("the configured inactive role does not exist")
        try:
            await member.add_roles(role, reason=f"prunebot: {reason}"[:512])
        except discord.HTTPException as exc:
            log.warning("add_roles failed for %s: %s", user_id, exc)
            return ActionResult.failed(f"{type(exc).__name__}: {exc}")
        return ActionResult.done()

    async def remove_inactive_role(self, user_id: int, *, reason: str) -> ActionResult:
        member = self.guild.get_member(user_id)
        role = self.inactive_role
        if member is None:
            return ActionResult.failed("member is no longer in the guild")
        if role is None:
            return ActionResult.failed("the configured inactive role does not exist")
        if role not in member.roles:
            return ActionResult.done("already absent")
        try:
            await member.remove_roles(role, reason=f"prunebot: {reason}"[:512])
        except discord.HTTPException as exc:
            log.warning("remove_roles failed for %s: %s", user_id, exc)
            return ActionResult.failed(f"{type(exc).__name__}: {exc}")
        return ActionResult.done()

    async def kick(self, user_id: int, *, reason: str) -> ActionResult:
        member = self.guild.get_member(user_id)
        if member is None:
            return ActionResult.failed("member is no longer in the guild")
        if member.id == self.guild.owner_id:
            return ActionResult.failed("refusing to kick the guild owner")
        try:
            await member.kick(reason=f"prunebot: {reason}"[:512])
        except discord.HTTPException as exc:
            log.warning("kick failed for %s: %s", user_id, exc)
            return ActionResult.failed(f"{type(exc).__name__}: {exc}")
        return ActionResult.done()

    async def warn(
        self, user_id: int, *, kind: WarnKind, days_left: int, reason: str
    ) -> WarnResult:
        """Deliver a warning, returning how (or whether) it landed.

        Never raises. A closed DM is an ordinary outcome, not an error: Discord
        returns 403 with code 50007, and for a user who has blocked the bot it can
        return 400 instead -- so both are caught via the shared HTTPException base.
        """
        member = self.guild.get_member(user_id)
        if member is None:
            return WarnResult(False, "member is no longer in the guild", delivery=None)

        context = message_context(
            guild_name=self.guild.name,
            member_name=member.display_name,
            member_id=user_id,
            window_days=self.config.activity.window_days,
            min_messages=self.config.activity.min_messages,
            days_left=days_left,
            kick_after_days=self.config.kicking.kick_after_days,
        )
        messages = self.config.messages

        # No button, deliberately: a warning is information only. The one way a
        # member can clear the flag themselves is to post.
        if kind is WarnKind.PRE_KICK:
            embed = pre_kick_embed(messages=messages, context=context)
        else:
            embed = warning_embed(
                messages=messages, context=context, final=kind is WarnKind.FINAL
            )

        detail = ""
        if self.config.flagging.warn_via_dm:
            try:
                message = await member.send(embed=embed)
            except discord.HTTPException as exc:
                detail = f"DM failed: {exc}"
                log.info("DM to %s failed: %s", user_id, exc)
            else:
                return WarnResult(
                    True,
                    "",
                    delivery="dm",
                    channel_id=message.channel.id,
                    message_id=message.id,
                )

        # Optional public fallback. Off by default: warnings are DM-only unless a
        # channel is configured.
        channel_id = self.config.flagging.warn_channel_id
        if channel_id:
            channel = self.guild.get_channel(channel_id)
            if isinstance(channel, discord.abc.Messageable):
                try:
                    message = await channel.send(
                        content=member.mention, embed=embed
                    )
                except discord.HTTPException as exc:
                    detail = f"{detail}; channel post failed: {exc}".strip("; ")
                else:
                    return WarnResult(
                        True,
                        detail,
                        delivery="channel",
                        channel_id=channel.id,
                        message_id=message.id,
                    )

        return WarnResult(False, detail or "no warning route available", delivery=None)

    async def announce_flag(self, user_id: int, *, days_left: int) -> ActionResult:
        """Post to the public announcement channel that a member was flagged.

        Best effort: a member has already been given the role by this point, and
        failing to announce it must not undo or block that.
        """
        channel_id = self.config.flagging.announce_channel_id
        if not channel_id:
            return ActionResult.done("no announcement channel configured")
        member = self.guild.get_member(user_id)
        channel = self.guild.get_channel(channel_id)
        if member is None or not isinstance(channel, discord.abc.Messageable):
            return ActionResult.failed("announcement channel or member unavailable")

        context = message_context(
            guild_name=self.guild.name,
            member_name=member.display_name,
            member_id=user_id,
            window_days=self.config.activity.window_days,
            min_messages=self.config.activity.min_messages,
            days_left=days_left,
            kick_after_days=self.config.kicking.kick_after_days,
        )
        try:
            await channel.send(
                self.config.messages.announce_body.format(**context)[:2000],
                allowed_mentions=discord.AllowedMentions(
                    users=self.config.flagging.announce_ping,
                    roles=False,
                    everyone=False,
                ),
            )
        except discord.HTTPException as exc:
            log.warning("announcement failed for %s: %s", user_id, exc)
            return ActionResult.failed(f"{type(exc).__name__}: {exc}")
        return ActionResult.done()

    # ----------------------------------------------------------------- audit trail

    async def post_audit(
        self, lines: str, *, title: str = "", alert: bool = False
    ) -> None:
        """Best effort. The database audit_log is the real record, so a failure
        here is logged and swallowed rather than allowed to break a sweep."""
        channel_id = self.config.audit.channel_id
        if not channel_id:
            return
        channel = self.guild.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            if alert or title:
                embed = discord.Embed(
                    title=title or "Prune bot",
                    description=lines[:4000],
                    colour=discord.Color.red() if alert else discord.Color.blurple(),
                )
                await channel.send(embed=embed)
            else:
                await channel.send(lines[:2000], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("could not post to the audit channel: %s", exc)

    async def post_embed(self, embed: discord.Embed) -> None:
        channel_id = self.config.audit.channel_id
        if not channel_id:
            return
        channel = self.guild.get_channel(channel_id)
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as exc:
                log.warning("could not post to the audit channel: %s", exc)
