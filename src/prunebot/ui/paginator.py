"""Page through a long list of embeds.

Discord caps an embed field at 1024 characters and a whole embed at 6000, which a
whitelist of any size blows straight past. Truncating silently is the worst option
-- an operator checking who is protected needs to see all of them.

Not persistent: these are ephemeral, short-lived replies, and a stale page button
surviving a restart would be confusing rather than useful.
"""

from __future__ import annotations

from collections.abc import Sequence

import discord

#: Conservative against Discord's 1024-character field limit.
FIELD_BUDGET = 900


def chunk_lines(
    lines: Sequence[str], *, budget: int = FIELD_BUDGET, max_lines: int = 20
) -> list[list[str]]:
    """Split lines into groups that each fit inside one embed field.

    Splits on the character budget *or* a line count, whichever comes first, so a
    page stays readable rather than merely legal.
    """
    if not lines:
        return []
    pages: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        extra = len(line) + 1
        if current and (size + extra > budget or len(current) >= max_lines):
            pages.append(current)
            current, size = [], 0
        current.append(line)
        size += extra
    if current:
        pages.append(current)
    return pages


class Paginator(discord.ui.View):
    """Previous / next over a list of pre-built embeds."""

    def __init__(
        self,
        embeds: list[discord.Embed],
        *,
        requester_id: int,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.requester_id = requester_id
        self.index = 0
        self._sync()

    @property
    def current(self) -> discord.Embed:
        return self.embeds[self.index]

    def _sync(self) -> None:
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.embeds) - 1
        self.counter.label = f"{self.index + 1} / {len(self.embeds)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "This isn't your list -- run the command yourself.", ephemeral=True
            )
            return False
        return True

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.current, view=self)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def previous(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:  # pragma: no cover - permanently disabled, never invoked
        pass

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = min(len(self.embeds) - 1, self.index + 1)
        await self._show(interaction)
