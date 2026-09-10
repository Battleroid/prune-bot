"""Event listeners: count messages, and keep membership state honest."""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands

from ..domain.models import MemberState
from ..services.activity import should_count

log = logging.getLogger(__name__)


class Tracking(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    # -------------------------------------------------------------------- messages

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or not self.bot.is_managed(guild.id):
            return
        if should_count(message, self.bot.cached_config(guild.id)):
            self.bot.activity_buffer.record(
                guild.id, message.author.id, int(message.created_at.timestamp())
            )

    # ------------------------------------------------------------------ membership

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or not self.bot.is_managed(member.guild.id):
            return

        store = self.bot.store
        config = await self.bot.config_for(member.guild.id)
        row = await store.get_member(member.guild.id, member.id)
        now = int(time.time())

        if row is None:
            await store.ensure_member(member.guild.id, member.id, joined_at=now)
            return

        fields: dict[str, object] = {
            "joined_at": now,
            "rejoin_count": row.rejoin_count + 1,
            "left_at": None,
        }

        if row.flagged_on_leave and config.rejoin.restore_flag_on_rejoin:
            # Closes the leave-and-rejoin dodge. The clock restarts from zero and
            # they are warned again, so this costs an honest returner nothing.
            gateway = await self.bot.gateway_for(member.guild.id)
            if gateway and not config.safety.dry_run:
                await gateway.add_inactive_role(
                    member.id, reason="rejoined while flagged as inactive"
                )
            fields.update(
                state=MemberState.FLAGGED,
                flagged_at=now,
                warned_at=None,
                warn_delivery=None,
                final_warned_at=None,
                flagged_on_leave=False,
            )
            await store.add_audit(
                member.guild.id,
                "flag",
                user_id=member.id,
                reason="restored on rejoin",
                dry_run=config.safety.dry_run,
            )
        else:
            fields.update(state=MemberState.ACTIVE, flagged_on_leave=False)
            if config.rejoin.rejoin_grace_days:
                # A clean returner gets a short breather before being counted again.
                fields["pardoned_until"] = now + config.rejoin.rejoin_grace_days * 86400

        if config.rejoin.reset_activity_on_rejoin:
            await store.clear_activity(member.guild.id, member.id)

        await store.update_member(member.guild.id, member.id, **fields)
        log.info("member %s rejoined guild %s", member.id, member.guild.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot or not self.bot.is_managed(member.guild.id):
            return
        row = await self.bot.store.get_member(member.guild.id, member.id)
        if row is None:
            return
        if row.state is MemberState.KICKED:
            return  # our own kick already recorded this
        # The row and its activity buckets are kept: if they come back, the fact
        # that they used to post here is still true.
        await self.bot.store.update_member(
            member.guild.id,
            member.id,
            state=MemberState.LEFT,
            left_at=int(time.time()),
            flagged_on_leave=row.state is MemberState.FLAGGED,
        )

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Log manual role changes; the next sweep's reconcile applies them.

        Deliberately does not write state here. The bot's own role changes raise
        this event too, and racing our own writes against the gateway event would
        mislabel the bot's own unflag as a moderator pardon. Reconciliation at the start of
        every sweep settles drift without any such race, and nothing acts on a
        member in between sweeps anyway.
        """
        if not self.bot.is_managed(after.guild.id):
            return
        config = self.bot.cached_config(after.guild.id)
        role_id = config.flagging.inactive_role_id
        if not role_id:
            return
        had = any(r.id == role_id for r in before.roles)
        has = any(r.id == role_id for r in after.roles)
        if had != has:
            log.info(
                "inactive role %s for %s in guild %s",
                "added to" if has else "removed from",
                after.id,
                after.guild.id,
            )

    # ----------------------------------------------------------------------- roles

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """Losing the inactive role would make every flagged member look clean."""
        if not self.bot.is_managed(role.guild.id):
            return
        config = await self.bot.config_for(role.guild.id)
        if role.id != config.flagging.inactive_role_id:
            return

        log.error("the inactive role was deleted in guild %s", role.guild.id)
        await self.bot.store.set_override(
            role.guild.id, "safety.dry_run", True, set_by=None
        )
        await self.bot.store.add_audit(
            role.guild.id,
            "config_change",
            reason="inactive role deleted; forced safety.dry_run = true",
        )
        self.bot.invalidate_config(role.guild.id)
        gateway = self.bot.gateway(role.guild.id)
        if gateway:
            await gateway.post_audit(
                f"The inactive role (`{role.id}`) was deleted. I have forced "
                f"**dry run** on so that nothing is acted on against incomplete "
                f"state. Recreate the role, update `flagging.inactive_role_id`, "
                f"then turn dry run back off.",
                title="Inactive role deleted",
                alert=True,
            )


async def setup(bot) -> None:
    await bot.add_cog(Tracking(bot))
