"""Configuration: TOML file + environment, validated with pydantic.

Split rule enforced here:
  secrets          -> environment (.env)
  static policy    -> config.toml
  mutable runtime  -> SQLite (see db.store; applied via `apply_overrides`)

The whitelist deliberately lives in the database rather than this file: the config
file is mounted read-only in the container, so `/prune whitelist add` could not
persist to it. The `[whitelist]` section here is a first-run seed only.
"""

from __future__ import annotations

import json
import os
import string
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PositiveInt = Annotated[int, Field(ge=1)]
NonNegInt = Annotated[int, Field(ge=0)]
Ratio = Annotated[float, Field(gt=0.0, le=1.0)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BotSection(_Base):
    guild_ids: list[int] = Field(default_factory=list)
    timezone: str = "UTC"

    @field_validator("guild_ids")
    @classmethod
    def _real_guild_ids(cls, v: list[int]) -> list[int]:
        if any(gid <= 0 for gid in v):
            raise ValueError(
                "bot.guild_ids still contains the placeholder 0. Replace it with "
                "your server id (right-click the server -> Copy Server ID, with "
                "Developer Mode enabled)."
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"unknown timezone {v!r}. If the name looks right, the container is "
                f"probably missing the tzdata package."
            ) from exc
        return v

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class SafetySection(_Base):
    dry_run: bool = True
    max_flags_per_sweep: PositiveInt = 25
    max_kicks_per_sweep: PositiveInt = 5
    max_unflags_per_sweep: PositiveInt = 200
    abort_if_flag_ratio_over: Ratio = 0.25
    abort_if_kick_ratio_over: Ratio = 0.05
    # Below this many evaluable members the ratio breaker is skipped and the
    # absolute caps are the only limit -- a ratio over a handful of people is noise.
    breaker_min_evaluable: NonNegInt = 20
    require_backfill_before_action: bool = True
    min_uptime_before_sweep_minutes: NonNegInt = 10
    action_delay_seconds: float = Field(default=1.5, ge=0.0)
    require_warning_before_kick: bool = False


class ChannelsSection(_Base):
    mode: Literal["exclude", "include"] = "exclude"
    include: list[int] = Field(default_factory=list)
    exclude: list[int] = Field(default_factory=list)

    @field_validator("include", "exclude", mode="before")
    @classmethod
    def _ids_not_names(cls, v: object) -> object:
        # Channel names are the tempting wrong answer here, and the raw pydantic
        # error ("unable to parse string as an integer") does not say why.
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and not item.strip().lstrip("#").isdigit():
                    raise ValueError(
                        f"channel lists take numeric channel ids, not names like "
                        f"{item!r}. Right-click the channel -> Copy Channel ID "
                        f"(Developer Mode must be enabled in Discord settings)."
                    )
        return v

    @model_validator(mode="after")
    def _one_list_only(self) -> ChannelsSection:
        if self.include and self.exclude:
            raise ValueError(
                "set either activity.channels.include or .exclude, not both -- "
                "having both makes the effective channel set ambiguous"
            )
        if self.mode == "include" and not self.include:
            raise ValueError('channels.mode is "include" but the include list is empty')
        return self


class ActivitySection(_Base):
    window_days: PositiveInt = 30
    min_messages: PositiveInt = 1
    count_threads: bool = True
    count_forum_posts: bool = True
    count_voice: bool = False
    count_reactions: bool = False
    channels: ChannelsSection = Field(default_factory=ChannelsSection)

    @model_validator(mode="after")
    def _unsupported_sources(self) -> ActivitySection:
        # These are off in the shipped config and unimplemented; failing loudly beats
        # silently not counting something the operator believes is counted.
        if self.count_voice:
            raise ValueError(
                "activity.count_voice is not implemented. It needs the voice_states "
                "intent and cannot be backfilled (there is no voice history API)."
            )
        if self.count_reactions:
            raise ValueError(
                "activity.count_reactions is not implemented. It needs the "
                "guild_reactions intent and cannot be backfilled."
            )
        return self


class FlaggingSection(_Base):
    inactive_role_id: int = 0
    grace_days_after_join: NonNegInt = 14
    warn_via_dm: bool = True
    #: Fallback only: used when the DM could not be delivered.
    warn_channel_id: int = 0
    #: Public announcement posted every time someone is flagged. Distinct from
    #: warn_channel_id (a fallback) and from the audit channel (a log).
    announce_channel_id: int = 0
    announce_ping: bool = True


class KickingSection(_Base):
    enabled: bool = True
    kick_after_days: PositiveInt = 90
    #: Default length of a /prune pardon, and of the pardon given when a
    #: moderator takes the role off by hand. The name predates the removal of
    #: self-service reverification; it is kept so existing configs still load.
    reverify_grace_days: PositiveInt = 30
    #: Posting back over the threshold clears the flag at the next sweep. This
    #: is the only way a member can clear it themselves.
    auto_clear_on_activity: bool = True
    dm_before_kick: bool = True
    final_warning_lead_days: NonNegInt = 3
    kick_reason_template: str = (
        "Inactive: under {min_messages} message(s) in {window_days}d, "
        "warned {warned_days}d ago"
    )


class WhitelistSection(_Base):
    """First-run seed only. The database is the source of truth afterwards."""

    role_ids: list[int] = Field(default_factory=list)
    user_ids: list[int] = Field(default_factory=list)
    exempt_bots: bool = True


#: Placeholders every member-facing message template may use.
MESSAGE_PLACEHOLDERS = frozenset(
    {
        "guild",          # server name
        "member",         # the member's display name
        "mention",        # <@id>, pings them
        "threshold",      # "posted anything" / "posted at least 3 messages"
        "window_days",
        "min_messages",
        "days_left",      # whole days until they are eligible for removal
        "kick_after_days",
        "deadline",       # Discord relative timestamp, e.g. "in 3 months"
        "deadline_date",  # Discord absolute date
    }
)

DEFAULT_WARN_BODY = """You have not {threshold} in **{guild}** in the last {window_days} days, so you have been given the inactive role.

**How to keep your place**
Post in the server. As soon as you have {threshold} in the last {window_days} days, the role comes off at the next check.

**If you do nothing**
You will be removed from the server {deadline}. You would be welcome to rejoin later."""

DEFAULT_FINAL_BODY = """This is your last reminder. You have not {threshold} in **{guild}** recently, and you are due to be removed {deadline}.

Post in the server before then to keep your place."""

DEFAULT_ANNOUNCE_BODY = (
    "{mention} has been marked inactive for not having {threshold} in "
    "{window_days} days. Post in the server to keep your place, otherwise you "
    "will be removed {deadline}."
)

DEFAULT_KICK_BODY = """This was automatic, for inactivity. It is not a ban and it is not a judgement about you, and you are welcome to rejoin at any time."""


class MessagesSection(_Base):
    """Everything a member actually reads. Edit freely; see MESSAGE_PLACEHOLDERS.

    Templates are checked at startup, so a typo in a placeholder stops the bot
    rather than raising mid-sweep while trying to DM somebody.
    """

    warn_title: str = Field(default="You have been marked inactive", max_length=256)
    warn_body: str = Field(default=DEFAULT_WARN_BODY, max_length=4000)

    final_title: str = Field(default="Final notice before removal", max_length=256)
    final_body: str = Field(default=DEFAULT_FINAL_BODY, max_length=4000)

    kick_title: str = Field(default="You have been removed from {guild}", max_length=256)
    kick_body: str = Field(default=DEFAULT_KICK_BODY, max_length=4000)

    #: Posted to flagging.announce_channel_id when someone is flagged.
    announce_body: str = Field(default=DEFAULT_ANNOUNCE_BODY, max_length=1900)

    @model_validator(mode="after")
    def _templates_are_renderable(self) -> MessagesSection:
        problems: list[str] = []
        for name, template in self.model_dump().items():
            try:
                used = {
                    field
                    for _, field, _, _ in string.Formatter().parse(template)
                    if field is not None
                }
            except ValueError as exc:
                problems.append(
                    f"messages.{name} is not a valid template ({exc}). To show a "
                    f"literal brace, double it: {{{{ or }}}}."
                )
                continue
            unknown = {f for f in used if f.split(".")[0].split("[")[0] not in MESSAGE_PLACEHOLDERS}
            if unknown:
                problems.append(
                    f"messages.{name} uses unknown placeholder(s) "
                    f"{sorted(unknown)}. Available: {', '.join(sorted(MESSAGE_PLACEHOLDERS))}"
                )
        if problems:
            raise ValueError("\n".join(problems))
        return self


class AccessSection(_Base):
    """Who may run the moderator commands (/prune ...).
    """

    #: Roles whose holders may moderate. Evaluated live, so granting the role
    #: grants access and removing it takes access away.
    role_ids: list[int] = Field(default_factory=list)
    #: Individual members, for people who should not need a whole role.
    user_ids: list[int] = Field(default_factory=list)
    #: True  -- Manage Server also grants access; the lists above are additive.
    #: False -- ONLY the lists above grant access, administrators included.
    allow_manage_guild: bool = True

    @model_validator(mode="after")
    def _not_locked_out(self) -> AccessSection:
        if not self.allow_manage_guild and not (self.role_ids or self.user_ids):
            raise ValueError(
                "access.allow_manage_guild is false and no roles or users are "
                "listed, so nobody could run /prune. Add access.role_ids or "
                "access.user_ids, or set allow_manage_guild back to true."
            )
        return self


class RejoinSection(_Base):
    restore_flag_on_rejoin: bool = True
    reset_activity_on_rejoin: bool = False
    rejoin_grace_days: NonNegInt = 7


class AuditSection(_Base):
    channel_id: int = 0
    post_every_action: bool = True
    post_sweep_summary: bool = True
    verbose_skips: bool = False


class SweepSection(_Base):
    enabled: bool = True
    run_at: list[str] = Field(default_factory=lambda: ["04:00"])

    @field_validator("run_at")
    @classmethod
    def _valid_times(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("sweep.run_at must list at least one HH:MM time")
        for item in v:
            hh, sep, mm = item.partition(":")
            if not (
                sep
                and hh.isdigit()
                and mm.isdigit()
                and 0 <= int(hh) < 24
                and 0 <= int(mm) < 60
            ):
                raise ValueError(f"sweep.run_at entry {item!r} is not a valid HH:MM time")
        return v


class BackfillSection(_Base):
    enabled: bool = True
    on_first_run_only: bool = True
    concurrency: PositiveInt = 3
    progress_report_every: PositiveInt = 10000
    max_messages_per_channel: PositiveInt = 500000


class StorageSection(_Base):
    retention_days: PositiveInt = 120
    flush_interval_seconds: PositiveInt = 30


class Config(_Base):
    bot: BotSection = Field(default_factory=BotSection)
    safety: SafetySection = Field(default_factory=SafetySection)
    activity: ActivitySection = Field(default_factory=ActivitySection)
    flagging: FlaggingSection = Field(default_factory=FlaggingSection)
    kicking: KickingSection = Field(default_factory=KickingSection)
    whitelist: WhitelistSection = Field(default_factory=WhitelistSection)
    messages: MessagesSection = Field(default_factory=MessagesSection)
    access: AccessSection = Field(default_factory=AccessSection)
    rejoin: RejoinSection = Field(default_factory=RejoinSection)
    audit: AuditSection = Field(default_factory=AuditSection)
    sweep: SweepSection = Field(default_factory=SweepSection)
    backfill: BackfillSection = Field(default_factory=BackfillSection)
    storage: StorageSection = Field(default_factory=StorageSection)

    @model_validator(mode="after")
    def _cross_section_rules(self) -> Config:
        """Refuse to start in a config that could act wrongly, or act silently."""
        problems: list[str] = []

        if self.flagging.inactive_role_id == 0:
            # Required even in dry run. With no role the bot cannot evaluate role
            # hierarchy, so every member comes back "unmanageable" and previews are
            # silently empty -- which looks like the bot working, and is not.
            problems.append(
                "flagging.inactive_role_id is unset. Create the inactive role, put "
                "the bot's own role above it, and put its id here -- without it "
                "every member is skipped as unmanageable and previews are empty"
            )
        if self.storage.retention_days < self.activity.window_days:
            problems.append(
                f"storage.retention_days ({self.storage.retention_days}) is shorter "
                f"than activity.window_days ({self.activity.window_days}) -- activity "
                f"would be deleted while still inside the evaluation window"
            )
        if self.kicking.enabled and self.audit.channel_id == 0:
            problems.append(
                "kicking.enabled is true but audit.channel_id is unset -- this bot "
                "will not kick people without an audit trail in the server"
            )
        if self.kicking.enabled and self.kicking.kick_after_days <= self.activity.window_days:
            problems.append(
                f"kicking.kick_after_days ({self.kicking.kick_after_days}) must exceed "
                f"activity.window_days ({self.activity.window_days}) -- otherwise "
                f"members are kicked before posting could possibly clear them"
            )
        if (
            self.flagging.announce_channel_id
            and self.flagging.announce_channel_id == self.audit.channel_id
        ):
            problems.append(
                "flagging.announce_channel_id is the same as audit.channel_id. The "
                "announcement is for members and the audit log is for moderators; "
                "mixing them means members read every action the bot takes."
            )
        if not self.flagging.warn_via_dm and self.flagging.warn_channel_id == 0:
            problems.append(
                "flagging.warn_via_dm is false and flagging.warn_channel_id is unset "
                "-- members would be flagged with no warning at all"
            )

        if problems:
            raise ValueError(
                "invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems)
            )
        return self


# Keys `/prune config set` may change at runtime. Deliberately narrow: anything that
# would need a reconnect, a command re-sync, or a schema change is not in here.
RUNTIME_SAFE_KEYS: dict[str, type] = {
    "safety.dry_run": bool,
    "safety.max_flags_per_sweep": int,
    "safety.max_kicks_per_sweep": int,
    "safety.abort_if_flag_ratio_over": float,
    "safety.abort_if_kick_ratio_over": float,
    "safety.breaker_min_evaluable": int,
    "safety.action_delay_seconds": float,
    "safety.min_uptime_before_sweep_minutes": int,
    "safety.require_warning_before_kick": bool,
    "activity.window_days": int,
    "activity.min_messages": int,
    "flagging.grace_days_after_join": int,
    "kicking.enabled": bool,
    "kicking.kick_after_days": int,
    "kicking.reverify_grace_days": int,
    "kicking.auto_clear_on_activity": bool,
    "sweep.enabled": bool,
}


class ConfigError(RuntimeError):
    """Raised for anything that should stop the bot before it connects."""


def _coerce(value: str, target: type) -> Any:
    if target is bool:
        low = value.strip().lower()
        if low in {"true", "yes", "on", "1"}:
            return True
        if low in {"false", "no", "off", "0"}:
            return False
        raise ConfigError(f"{value!r} is not a boolean")
    try:
        return target(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{value!r} is not a valid {target.__name__}") from exc


def parse_override(key: str, raw: str) -> Any:
    """Validate a `/prune config set` pair, returning the coerced value."""
    if key not in RUNTIME_SAFE_KEYS:
        raise ConfigError(
            f"{key!r} is not runtime-settable. Settable keys: "
            + ", ".join(sorted(RUNTIME_SAFE_KEYS))
        )
    return _coerce(raw, RUNTIME_SAFE_KEYS[key])


def apply_overrides(config: Config, overrides: dict[str, Any]) -> Config:
    """Return a new Config with runtime overrides applied and re-validated.

    Unknown or unsafe keys are ignored rather than raising: a stale row left in the
    database by an older version must not stop the bot from starting.
    """
    if not overrides:
        return config
    data = config.model_dump()
    changed = False
    for key, value in overrides.items():
        if key not in RUNTIME_SAFE_KEYS:
            continue
        section, _, field = key.partition(".")
        if section in data and field in data[section]:
            data[section][field] = value
            changed = True
    return Config.model_validate(data) if changed else config


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Read and validate the TOML config. Raises ConfigError with actionable text."""
    resolved = Path(path or os.environ.get("PRUNE_CONFIG_PATH", "config.toml"))
    if not resolved.exists():
        raise ConfigError(
            f"config file not found at {resolved}. Copy config.example.toml and fill it in."
        )
    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{resolved} is not valid TOML: {exc}") from exc
    try:
        return Config.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"{resolved}: {exc}") from exc


class Secrets(_Base):
    token: str
    db_path: str
    panic: bool


def load_secrets() -> Secrets:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigError("DISCORD_TOKEN is not set (put it in .env, never in config.toml)")
    return Secrets(
        token=token,
        db_path=os.environ.get("PRUNE_DB_PATH", "data/prune.db"),
        panic=os.environ.get("PRUNE_PANIC", "0").strip().lower()
        in {"1", "true", "yes", "on"},
    )


def dumps_override(value: Any) -> str:
    return json.dumps(value)


def loads_override(raw: str) -> Any:
    return json.loads(raw)
