"""Structural checks on the bot itself.

These construct the real PruneBot and load the real cogs -- no network, but they
catch the mistakes that otherwise only show up at connect time: a malformed command
tree, a group nested too deeply, a duplicate command name, a bad intent set.
"""

from __future__ import annotations

import discord
import pytest

from prunebot.bot import PruneBot, build_intents
from prunebot.config import Config, Secrets

from .conftest import GUILD_ID
from .test_sweep_integration import make_config


@pytest.fixture
async def bot(tmp_path):
    config: Config = make_config()
    secrets = Secrets(token="x", db_path=str(tmp_path / "bot.db"), panic=False)
    instance = PruneBot(config, secrets)
    await instance.store.connect()
    instance.add_dynamic_items(
        __import__("prunebot.ui.verify_button", fromlist=["VerifyButton"]).VerifyButton
    )
    for cog in ("prunebot.cogs.tracking", "prunebot.cogs.admin", "prunebot.cogs.verify"):
        await instance.load_extension(cog)
    try:
        yield instance
    finally:
        await instance.store.close()


def test_message_content_intent_is_not_requested():
    """Author id and timestamp arrive without it; requesting it would mean an extra
    privileged intent to justify for no benefit."""
    intents = build_intents(make_config())
    assert intents.message_content is False
    assert intents.members is True
    assert intents.guilds is True
    assert intents.guild_messages is True


def test_unused_intents_stay_off():
    intents = build_intents(make_config())
    assert intents.presences is False
    assert intents.voice_states is False
    assert intents.typing is False


async def test_command_tree_has_the_expected_shape(bot):
    names = {c.name for c in bot.tree.get_commands()}
    assert "prune" in names
    assert "verify" in names

    prune = discord.utils.get(bot.tree.get_commands(), name="prune")
    sub = {c.name for c in prune.commands}
    for expected in (
        "status",
        "preview",
        "run",
        "pardon",
        "whitelist",
        "config",
        "backfill",
        "stats",
        "history",
        "backup",
        "sync",
    ):
        assert expected in sub, f"/prune {expected} is missing"


async def test_whitelist_subcommands_exist(bot):
    prune = discord.utils.get(bot.tree.get_commands(), name="prune")
    whitelist = discord.utils.get(prune.commands, name="whitelist")
    assert {c.name for c in whitelist.commands} == {"add", "remove", "list"}


async def test_whitelist_target_accepts_members_and_roles(bot):
    """One mentionable picker rather than separate user and role commands."""
    prune = discord.utils.get(bot.tree.get_commands(), name="prune")
    whitelist = discord.utils.get(prune.commands, name="whitelist")
    add = discord.utils.get(whitelist.commands, name="add")
    target = discord.utils.get(add.parameters, name="target")
    assert target.type is discord.AppCommandOptionType.mentionable
    assert target.required is True


async def test_verify_has_no_permission_gate(bot):
    """The escape hatch has to work for the people being pruned."""
    verify = discord.utils.get(bot.tree.get_commands(), name="verify")
    assert verify.default_permissions is None


async def test_prune_group_requires_manage_guild(bot):
    prune = discord.utils.get(bot.tree.get_commands(), name="prune")
    assert prune.default_permissions is not None
    assert prune.default_permissions.manage_guild is True


async def test_whitelist_context_menu_is_registered(bot):
    menus = [
        c
        for c in bot.tree.get_commands(type=discord.AppCommandType.user)
        if c.name == "Whitelist User"
    ]
    assert len(menus) == 1


async def test_command_tree_survives_a_full_payload_conversion(bot):
    """Discord rejects malformed trees at sync time; this is that validation,
    run locally instead of against the API."""
    for command in bot.tree.get_commands():
        payload = command.to_dict(bot.tree)
        assert payload["name"] == command.name


async def test_config_cache_applies_and_invalidates_overrides(bot):
    assert (await bot.config_for(GUILD_ID)).safety.max_flags_per_sweep == 25
    await bot.store.set_override(GUILD_ID, "safety.max_flags_per_sweep", 3)
    bot.invalidate_config(GUILD_ID)
    assert (await bot.config_for(GUILD_ID)).safety.max_flags_per_sweep == 3


async def test_cached_config_falls_back_to_the_base_config(bot):
    assert bot.cached_config(999999) is bot.base_config


async def test_is_managed_only_covers_configured_guilds(bot):
    assert bot.is_managed(GUILD_ID) is True
    assert bot.is_managed(424242) is False
