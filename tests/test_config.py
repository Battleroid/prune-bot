from __future__ import annotations

import pytest
from pydantic import ValidationError

from prunebot.config import (
    Config,
    ConfigError,
    apply_overrides,
    load_config,
    load_secrets,
    parse_override,
)

# A configuration that passes every cross-section rule; tests patch pieces of it.
VALID = """
[bot]
guild_ids = [123]
timezone = "America/New_York"

[flagging]
inactive_role_id = 456

[audit]
channel_id = 789
"""


def write(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_minimal_valid_config_loads_with_documented_defaults(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.safety.dry_run is True  # ships safe
    assert cfg.activity.window_days == 30
    assert cfg.activity.min_messages == 1  # "0 messages in 30 days"
    assert cfg.kicking.kick_after_days == 90
    assert cfg.kicking.auto_clear_on_activity is True  # posting is the only self-service clear
    assert cfg.activity.count_threads is True
    assert cfg.activity.count_voice is False
    assert cfg.bot.tzinfo.key == "America/New_York"


def test_missing_file_says_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="config.example.toml"):
        load_config(tmp_path / "nope.toml")


def test_malformed_toml_is_reported_as_such(tmp_path):
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(write(tmp_path, "this is not = = toml"))


def test_unknown_key_is_rejected_rather_than_silently_ignored(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, VALID + "\n[activity]\nwindo_days = 30\n"))


def test_unknown_timezone_is_rejected(tmp_path):
    body = VALID.replace('timezone = "America/New_York"', 'timezone = "Mars/Olympus"')
    with pytest.raises(ConfigError, match="unknown timezone"):
        load_config(write(tmp_path, body))


# ---------------------------------------------------- cross-section safety rules


def test_acting_without_a_role_configured_is_refused(tmp_path):
    body = VALID.replace("inactive_role_id = 456", "inactive_role_id = 0")
    body += "\n[safety]\ndry_run = false\n"
    with pytest.raises(ConfigError, match="inactive_role_id is unset"):
        load_config(write(tmp_path, body))


def test_retention_shorter_than_the_window_is_refused(tmp_path):
    body = VALID + "\n[storage]\nretention_days = 10\n"
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(write(tmp_path, body))


def test_kicking_without_an_audit_channel_is_refused(tmp_path):
    body = VALID.replace("channel_id = 789", "channel_id = 0")
    with pytest.raises(ConfigError, match="will not kick people without an audit trail"):
        load_config(write(tmp_path, body))


def test_kicking_may_be_disabled_without_an_audit_channel(tmp_path):
    body = VALID.replace("channel_id = 789", "channel_id = 0")
    body += "\n[kicking]\nenabled = false\n"
    cfg = load_config(write(tmp_path, body))
    assert cfg.kicking.enabled is False


def test_kick_deadline_inside_the_window_is_refused(tmp_path):
    body = VALID + "\n[kicking]\nkick_after_days = 20\n"
    with pytest.raises(ConfigError, match="must exceed"):
        load_config(write(tmp_path, body))


def test_flagging_with_no_warning_route_at_all_is_refused(tmp_path):
    body = VALID + "\n[flagging]\nwarn_via_dm = false\nwarn_channel_id = 0\n"
    with pytest.raises(ConfigError, match="no warning at all"):
        load_config(write(tmp_path, body.replace("[flagging]\ninactive_role_id = 456", "")))


def test_both_include_and_exclude_is_refused(tmp_path):
    body = VALID + "\n[activity.channels]\ninclude = [1]\nexclude = [2]\n"
    with pytest.raises(ConfigError, match="not both"):
        load_config(write(tmp_path, body))


def test_include_mode_with_an_empty_list_is_refused(tmp_path):
    body = VALID + '\n[activity.channels]\nmode = "include"\n'
    with pytest.raises(ConfigError, match="include list is empty"):
        load_config(write(tmp_path, body))


def test_unimplemented_activity_sources_fail_loudly(tmp_path):
    """Better to refuse than to silently not count something the operator enabled."""
    body = VALID + "\n[activity]\ncount_voice = true\n"
    with pytest.raises(ConfigError, match="count_voice is not implemented"):
        load_config(write(tmp_path, body))


def test_bad_sweep_time_is_refused(tmp_path):
    body = VALID + '\n[sweep]\nrun_at = ["4am"]\n'
    with pytest.raises(ConfigError, match="not a valid HH:MM"):
        load_config(write(tmp_path, body))


@pytest.mark.parametrize("value", ["24:00", "12:60", "-1:00", "0400"])
def test_out_of_range_sweep_times_are_refused(tmp_path, value):
    body = VALID + f'\n[sweep]\nrun_at = ["{value}"]\n'
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, body))


# ------------------------------------------------------------ runtime overrides


def test_parse_override_coerces_types():
    assert parse_override("safety.dry_run", "false") is False
    assert parse_override("safety.dry_run", "ON") is True
    assert parse_override("activity.window_days", "45") == 45
    assert parse_override("safety.abort_if_flag_ratio_over", "0.5") == 0.5


def test_parse_override_refuses_unsafe_keys():
    with pytest.raises(ConfigError, match="not runtime-settable"):
        parse_override("bot.timezone", "UTC")


def test_parse_override_reports_bad_values():
    with pytest.raises(ConfigError, match="not a boolean"):
        parse_override("safety.dry_run", "maybe")
    with pytest.raises(ConfigError, match="not a valid int"):
        parse_override("activity.window_days", "thirty")


def test_apply_overrides_returns_a_revalidated_config(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    updated = apply_overrides(cfg, {"activity.window_days": 45})
    assert updated.activity.window_days == 45
    assert cfg.activity.window_days == 30  # original untouched


def test_overrides_are_validated_not_blindly_applied(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    # retention (120) < window (200) must still be caught when set at runtime.
    with pytest.raises(ValidationError):
        apply_overrides(cfg, {"activity.window_days": 200})


def test_stale_unknown_override_rows_are_ignored(tmp_path):
    """A row left by an older build must not stop the bot from starting."""
    cfg = load_config(write(tmp_path, VALID))
    assert apply_overrides(cfg, {"removed.old_key": 1}) is cfg


def test_dry_run_can_be_turned_off_at_runtime(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert apply_overrides(cfg, {"safety.dry_run": False}).safety.dry_run is False


# -------------------------------------------------------------------- secrets


def test_missing_token_is_refused(monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        load_secrets()


def test_panic_flag_is_parsed(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setenv("PRUNE_PANIC", "1")
    assert load_secrets().panic is True
    monkeypatch.setenv("PRUNE_PANIC", "0")
    assert load_secrets().panic is False


def test_config_is_immutable(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    with pytest.raises(ValidationError):
        cfg.safety.dry_run = False


def test_defaults_alone_are_not_a_valid_config():
    """Bare defaults enable kicking with no audit channel, which must be refused."""
    with pytest.raises(ValidationError):
        Config()


# ----------------------------------------------------- the shipped example file


def _example_path():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "config.example.toml"


def test_shipped_example_is_valid_toml():
    """Leading-zero placeholders are not valid TOML, and would surface as a
    baffling 'Unclosed array' instead of 'fill in your server id'."""
    import tomllib

    tomllib.loads(_example_path().read_text(encoding="utf-8"))


def test_shipped_example_names_the_placeholder_it_wants_filled_in(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(_example_path().read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConfigError, match="placeholder"):
        load_config(target)


def test_filled_in_example_loads_in_the_safe_shipped_state(tmp_path):
    filled = _example_path().read_text(encoding="utf-8").replace(
        "= [0]  #", "= [123456789012345678]  #"
    ).replace("= 0  # <- replace", "= 123456789012345678  # <- replace")
    target = tmp_path / "config.toml"
    target.write_text(filled, encoding="utf-8")
    cfg = load_config(target)
    assert cfg.safety.dry_run is True
    assert cfg.kicking.enabled is False
    assert cfg.activity.window_days == 30
    assert cfg.kicking.kick_after_days == 90


def test_channel_names_are_rejected_with_an_actionable_message(tmp_path):
    """Names are the tempting wrong answer; the raw pydantic error doesn't explain."""
    body = VALID + '\n[activity.channels]\nexclude = ["bot-spam"]\n'
    with pytest.raises(ConfigError, match="Copy Channel ID"):
        load_config(write(tmp_path, body))


def test_channel_ids_are_accepted(tmp_path):
    body = VALID + "\n[activity.channels]\nexclude = [111111111111111111]\n"
    cfg = load_config(write(tmp_path, body))
    assert cfg.activity.channels.exclude == [111111111111111111]


# ------------------------------------------------------- member-facing messages


def test_message_templates_default_to_the_shipped_copy(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert "{guild}" in cfg.messages.warn_body
    for body in (cfg.messages.warn_body, cfg.messages.final_body, cfg.messages.announce_body):
        assert "/verify" not in body and "button" not in body  # both retired
    # The announce channel may be excluded from activity, so "post here" could be a lie.
    assert "post here" not in cfg.messages.announce_body.lower()


def test_a_typo_in_a_placeholder_is_caught_at_startup(tmp_path):
    """Otherwise it would raise mid-sweep, while trying to DM a real member."""
    body = VALID + '\n[messages]\nwarn_body = "Hello {membr}"\n'
    with pytest.raises(ConfigError, match="unknown placeholder"):
        load_config(write(tmp_path, body))


def test_the_error_lists_the_placeholders_that_do_exist(tmp_path):
    body = VALID + '\n[messages]\nwarn_body = "Hello {nope}"\n'
    with pytest.raises(ConfigError, match="deadline"):
        load_config(write(tmp_path, body))


def test_an_unbalanced_brace_is_caught_at_startup(tmp_path):
    body = VALID + '\n[messages]\nwarn_body = "Hello {guild"\n'
    with pytest.raises(ConfigError, match="not a valid template"):
        load_config(write(tmp_path, body))


def test_a_doubled_brace_is_a_literal_and_is_allowed(tmp_path):
    body = VALID + '\n[messages]\nwarn_body = "Braces {{like this}} are literal"\n'
    cfg = load_config(write(tmp_path, body))
    assert cfg.messages.warn_body.format() == "Braces {like this} are literal"


def test_custom_wording_is_accepted(tmp_path):
    body = (
        VALID
        + '\n[messages]\nwarn_title = "Oi {member}"\n'
        + 'warn_body = "No posts in {window_days}d. Gone {deadline}."\n'
    )
    cfg = load_config(write(tmp_path, body))
    assert cfg.messages.warn_title == "Oi {member}"


def test_a_leftover_button_label_is_reported(tmp_path):
    """The button was retired. A config still setting its label should say so at
    startup rather than silently doing nothing."""
    body = VALID + '\n[messages]\nbutton_label = "still here"\n'
    with pytest.raises(ConfigError, match="button_label"):
        load_config(write(tmp_path, body))


def test_the_inactive_role_is_required_even_in_dry_run(tmp_path):
    """With no role, hierarchy cannot be evaluated, so every member is skipped as
    unmanageable -- an empty preview that looks like the bot working."""
    body = VALID.replace("inactive_role_id = 456", "inactive_role_id = 0")
    with pytest.raises(ConfigError, match="previews are empty"):
        load_config(write(tmp_path, body))


def test_the_uptime_gate_is_settable_at_runtime(tmp_path):
    """It is a numeric guard -- needing a restart to lower it is self-defeating,
    since restarting resets the uptime it measures."""
    cfg = load_config(write(tmp_path, VALID))
    assert parse_override("safety.min_uptime_before_sweep_minutes", "0") == 0
    updated = apply_overrides(cfg, {"safety.min_uptime_before_sweep_minutes": 0})
    assert updated.safety.min_uptime_before_sweep_minutes == 0


def test_announcement_channel_must_not_be_the_audit_channel(tmp_path):
    """One is for members, the other is a moderator log."""
    # 789 is the audit channel in VALID; add the announcement to the existing
    # [flagging] section rather than declaring a second one.
    body = VALID.replace(
        "inactive_role_id = 456", "inactive_role_id = 456\nannounce_channel_id = 789"
    )
    with pytest.raises(ConfigError, match="same as audit.channel_id"):
        load_config(write(tmp_path, body))


def test_announcement_body_is_template_checked(tmp_path):
    body = VALID + '\n[messages]\nannounce_body = "hi {membr}"\n'
    with pytest.raises(ConfigError, match="unknown placeholder"):
        load_config(write(tmp_path, body))
