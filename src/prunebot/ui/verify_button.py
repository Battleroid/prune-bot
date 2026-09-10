"""The "I'm still here" button attached to warning DMs.

A DynamicItem rather than a persistent View: the member's identity rides in the
custom_id and is recovered by regex, so a button clicked three months and several
container restarts after it was sent still works, with no message-id bookkeeping.

`custom_id` is capped at 100 characters by Discord; `prune:verify:<guild>:<user>`
is about 52.
"""

from __future__ import annotations

import logging
import re

import discord

log = logging.getLogger(__name__)

#: Used when re-resolving a button from its custom_id. The label a member sees
#: is whatever was rendered into the original message, so this is only a
#: fallback for the reconstructed item.
DEFAULT_LABEL = "I'm still here"


class VerifyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"prune:verify:(?P<guild_id>\d+):(?P<user_id>\d+)",
):
    def __init__(
        self, guild_id: int, user_id: int, label: str = DEFAULT_LABEL
    ) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.success,
                emoji="\N{WAVING HAND SIGN}",
                custom_id=f"prune:verify:{guild_id}:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> VerifyButton:
        return cls(int(match["guild_id"]), int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client

        # The button belongs to one member. A moderator may also press it on their
        # behalf (in a channel warning), which is audited with their id as actor.
        actor_id: int | None = None
        if interaction.user.id != self.user_id:
            guild = bot.get_guild(self.guild_id)
            member = guild.get_member(interaction.user.id) if guild else None
            if not (member and member.guild_permissions.manage_guild):
                await interaction.response.send_message(
                    "This button is not for you.", ephemeral=True
                )
                return
            actor_id = interaction.user.id

        await interaction.response.defer(ephemeral=True)
        try:
            cleared = await bot.verify_member(
                guild_id=self.guild_id, user_id=self.user_id, actor_id=actor_id
            )
        except Exception:
            log.exception("verify button failed for %s", self.user_id)
            await interaction.followup.send(
                "Something went wrong clearing your inactive role. Try **/verify** in "
                "the server, or ask a moderator.",
                ephemeral=True,
            )
            return

        message = (
            "You're all set -- the inactive role has been removed and your place is safe."
            if cleared
            else "You aren't marked inactive, so there was nothing to clear."
        )
        await interaction.followup.send(message, ephemeral=True)

        # Retire the button so the message cannot be clicked again.
        try:
            view = discord.ui.View.from_message(interaction.message)
            for child in view.children:
                child.disabled = True
            await interaction.message.edit(view=view)
        except (discord.HTTPException, AttributeError):
            pass  # cosmetic only


def warning_view(
    guild_id: int, user_id: int, label: str = DEFAULT_LABEL
) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VerifyButton(guild_id, user_id, label))
    return view
