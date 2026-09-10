"""`/verify` -- how a flagged member keeps their place.

No permission gate: this is the escape hatch, and it must work for anyone. It is
also the fallback if the DM button ever fails to resolve, which is why it exists
as a command at all rather than only as a button.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


class Verify(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="verify",
        description="Confirm you're still active and remove the inactive role.",
    )
    @app_commands.guild_only()
    async def verify(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None or not self.bot.is_managed(guild_id):
            await interaction.response.send_message(
                "I'm not managing this server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            cleared = await self.bot.verify_member(
                guild_id=guild_id, user_id=interaction.user.id
            )
        except Exception:
            log.exception("/verify failed for %s", interaction.user.id)
            await interaction.followup.send(
                "Something went wrong. Please ask a moderator to clear it for you.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "You're all set -- the inactive role has been removed and your place is safe."
            if cleared
            else "You aren't marked inactive, so there's nothing to clear. "
            "Just keep posting now and then and you'll stay off the list.",
            ephemeral=True,
        )


async def setup(bot) -> None:
    await bot.add_cog(Verify(bot))
