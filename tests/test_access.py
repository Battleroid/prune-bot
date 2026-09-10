"""Who may run the moderator commands."""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from prunebot.cogs.admin import DENIAL, may_moderate
from prunebot.config import ConfigError, load_config

from .test_sweep_integration import make_config

OWNER_ID = 1
MOD_ROLE = 555
OTHER_ROLE = 777


def interaction(*, user_id: int = 2, roles=(), manage_guild: bool = False):
    it = MagicMock(spec=discord.Interaction)
    guild = MagicMock(spec=discord.Guild)
    guild.owner_id = OWNER_ID
    it.guild = guild
    it.guild_id = 999
    user = MagicMock()
    user.id = user_id
    user.roles = [MagicMock(id=r) for r in roles]
    user.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    it.user = user
    return it


# ------------------------------------------------------------- default behaviour


def test_manage_server_grants_access_by_default():
    cfg = make_config()
    assert may_moderate(interaction(manage_guild=True), cfg) is True
    assert may_moderate(interaction(manage_guild=False), cfg) is False


def test_the_guild_owner_is_always_allowed():
    """Otherwise a bad allowlist locks the operator out until they edit the file."""
    cfg = make_config(access={"role_ids": [MOD_ROLE], "allow_manage_guild": False})
    assert may_moderate(interaction(user_id=OWNER_ID), cfg) is True


# ------------------------------------------------------------------- allowlists


def test_a_listed_role_grants_access_without_manage_server():
    cfg = make_config(access={"role_ids": [MOD_ROLE]})
    assert may_moderate(interaction(roles=[MOD_ROLE]), cfg) is True
    assert may_moderate(interaction(roles=[OTHER_ROLE]), cfg) is False


def test_a_listed_user_grants_access():
    cfg = make_config(access={"user_ids": [42]})
    assert may_moderate(interaction(user_id=42), cfg) is True
    assert may_moderate(interaction(user_id=43), cfg) is False


def test_role_access_is_evaluated_live():
    """Granting the role grants access; removing it takes access away."""
    cfg = make_config(access={"role_ids": [MOD_ROLE]})
    person = interaction(roles=[])
    assert may_moderate(person, cfg) is False
    person.user.roles = [MagicMock(id=MOD_ROLE)]
    assert may_moderate(person, cfg) is True


def test_allowlist_is_additive_to_manage_server_by_default():
    cfg = make_config(access={"role_ids": [MOD_ROLE]})
    assert may_moderate(interaction(manage_guild=True), cfg) is True
    assert may_moderate(interaction(roles=[MOD_ROLE]), cfg) is True


def test_exclusive_mode_shuts_out_administrators():
    cfg = make_config(
        access={"role_ids": [MOD_ROLE], "allow_manage_guild": False}
    )
    assert may_moderate(interaction(manage_guild=True), cfg) is False
    assert may_moderate(interaction(roles=[MOD_ROLE]), cfg) is True


# ------------------------------------------------------------------- lockout


def test_config_refuses_a_setup_nobody_could_use(tmp_path):
    from .test_config import VALID, write

    body = VALID + "\n[access]\nallow_manage_guild = false\n"
    with pytest.raises(ConfigError, match="nobody could run"):
        load_config(write(tmp_path, body))


def test_exclusive_mode_is_fine_with_an_allowlist(tmp_path):
    from .test_config import VALID, write

    body = VALID + "\n[access]\nallow_manage_guild = false\nrole_ids = [555]\n"
    cfg = load_config(write(tmp_path, body))
    assert cfg.access.allow_manage_guild is False


# --------------------------------------------------------------- refusal text


def test_the_refusal_does_not_leak_who_is_allowed():
    """An allowlist makes /prune visible to everyone, so a refusal naming the
    permitted roles or users would let any member enumerate the moderators."""
    assert "<@" not in DENIAL   # no user or role mentions
    assert "&" not in DENIAL
    assert not any(ch.isdigit() for ch in DENIAL)  # no raw ids


def test_the_refusal_is_the_same_whatever_is_configured():
    """Varying the wording by config would itself disclose how access is set up."""
    assert isinstance(DENIAL, str) and DENIAL
    # There is one message, not a function of config, so this holds by construction.
    assert "Manage Server" not in DENIAL


# ------------------------------------------------------- whitelist pagination


def _entry(kind: str, target_id: int, reason: str | None = "seeded from config.toml"):
    from prunebot.domain.models import WhitelistEntry

    return WhitelistEntry(
        guild_id=999, kind=kind, target_id=target_id, added_by=None,
        added_at=0, reason=reason,
    )


def _guild_without_cache():
    guild = MagicMock(spec=discord.Guild)
    guild.get_member.return_value = None
    guild.get_role.return_value = None
    return guild


def test_a_long_whitelist_is_paginated_rather_than_truncated():
    """63 entries used to render as one 1024-char field, silently showing ~17."""
    from prunebot.cogs.admin import build_whitelist_pages

    entries = [_entry("user", 700000000000000000 + i) for i in range(63)]
    pages = build_whitelist_pages(entries, _guild_without_cache())

    rendered = "\n".join(f.value for p in pages for f in p.fields)
    for entry in entries:
        assert str(entry.target_id) in rendered, "an entry went missing"
    assert len(pages) > 1


def test_every_embed_stays_inside_discords_limits():
    from prunebot.cogs.admin import build_whitelist_pages

    entries = [_entry("user", 700000000000000000 + i) for i in range(400)]
    for page in build_whitelist_pages(entries, _guild_without_cache()):
        assert len(page) <= 6000          # whole-embed cap
        assert len(page.fields) <= 25
        for field in page.fields:
            assert len(field.value) <= 1024


def test_roles_and_members_are_listed_separately():
    from prunebot.cogs.admin import build_whitelist_pages

    entries = [_entry("role", 1), _entry("user", 2)]
    pages = build_whitelist_pages(entries, _guild_without_cache())
    names = [f.name for p in pages for f in p.fields]
    assert any(n.startswith("Roles") for n in names)
    assert any(n.startswith("Members") for n in names)


def test_the_seed_reason_is_not_repeated_on_every_row():
    """It is on almost every entry and carries no information; showing it 60
    times would push out the entries that actually matter."""
    from prunebot.cogs.admin import whitelist_line

    seeded = whitelist_line(_entry("user", 5), _guild_without_cache())
    manual = whitelist_line(_entry("user", 6, "long-time member"), _guild_without_cache())
    assert "seeded from" not in seeded
    assert "long-time member" in manual


def test_a_short_whitelist_is_a_single_page():
    from prunebot.cogs.admin import build_whitelist_pages

    pages = build_whitelist_pages([_entry("user", 1)], _guild_without_cache())
    assert len(pages) == 1


def test_chunking_splits_on_both_size_and_line_count():
    from prunebot.ui.paginator import chunk_lines

    assert chunk_lines([]) == []
    assert len(chunk_lines(["x"] * 20, max_lines=20)) == 1
    assert len(chunk_lines(["x"] * 21, max_lines=20)) == 2
    assert len(chunk_lines(["y" * 500] * 4, budget=900)) == 4
