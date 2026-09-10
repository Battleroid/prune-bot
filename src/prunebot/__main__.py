"""Entrypoint. Validates configuration before opening a connection."""

from __future__ import annotations

import asyncio
import logging
import sys

import discord

from .bot import PruneBot
from .config import ConfigError, load_config, load_secrets
from .logging_setup import configure

log = logging.getLogger(__name__)


def main() -> int:
    configure()
    try:
        config = load_config()
        secrets = load_secrets()
    except ConfigError as exc:
        # Fail here, loudly, rather than three hours into a run.
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2

    if not config.bot.guild_ids:
        print(
            "Configuration error:\n  - bot.guild_ids is empty, so the bot would "
            "manage nothing. Add your server's id.",
            file=sys.stderr,
        )
        return 2

    if config.safety.dry_run:
        log.info("DRY RUN is on: actions will be logged and audited but not performed")
    else:
        log.warning("DRY RUN is OFF: this bot will assign roles and kick members")

    bot = PruneBot(config, secrets)
    try:
        bot.run(secrets.token, log_handler=None)
    except discord.LoginFailure:
        print("Discord rejected the token in DISCORD_TOKEN.", file=sys.stderr)
        return 3
    except discord.PrivilegedIntentsRequired:
        print(
            "The Server Members intent is not enabled for this application.\n"
            "Enable it at https://discord.com/developers/applications -> your app "
            "-> Bot -> Privileged Gateway Intents -> Server Members Intent.",
            file=sys.stderr,
        )
        return 4
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except asyncio.CancelledError:
        sys.exit(0)
