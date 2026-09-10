"""Handler for the retired "I'm still here" button.

Warnings used to carry a button that took the inactive role off and granted a
month of immunity, which let a member stay without ever posting. That was
removed: the only way for a member to clear their own flag is to post.

Warnings already sitting in people's DMs still show the old button, though, so
this keeps answering it -- with an explanation instead of Discord's generic
"This interaction failed". A DynamicItem matches on the custom_id template
alone, so it resolves for any old message across any number of restarts.
"""

from __future__ import annotations

import re

import discord

#: Only used to rebuild the item from its custom_id. The label a member sees is
#: whatever was rendered into the original message.
LEGACY_LABEL = "I'm still here"

RETIRED = (
    "This button no longer does anything. To clear your inactive status, just "
    "post in the server -- the role comes off at the next check."
)


class RetiredVerifyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"prune:verify:(?P<guild_id>\d+):(?P<user_id>\d+)",
):
    def __init__(self, guild_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                label=LEGACY_LABEL,
                style=discord.ButtonStyle.success,
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
    ) -> RetiredVerifyButton:
        return cls(int(match["guild_id"]), int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(RETIRED, ephemeral=True)

        # Disable it so the old message stops inviting clicks.
        try:
            view = discord.ui.View.from_message(interaction.message)
            for child in view.children:
                child.disabled = True
            await interaction.message.edit(view=view)
        except (discord.HTTPException, AttributeError):
            pass  # cosmetic only
