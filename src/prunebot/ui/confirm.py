"""Confirmation gate for anything that will really change Discord."""

from __future__ import annotations

import discord


class ConfirmView(discord.ui.View):
    """Two-button confirm, locked to the person who invoked the command.

    Deliberately not persistent: a stale confirm button surviving a restart and
    being clicked days later is exactly the accident this is here to prevent.
    """

    def __init__(self, *, requester_id: int, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who ran the command can confirm it.", ephemeral=True
            )
            return False
        return True

    def _finish(self) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="Run it", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = True
        self._finish()
        await interaction.response.edit_message(content="Running...", view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = False
        self._finish()
        await interaction.response.edit_message(content="Cancelled.", view=self)

    async def on_timeout(self) -> None:
        self.value = False
