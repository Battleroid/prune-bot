"""Schema versioning via PRAGMA user_version.

Version 1 is the base schema in schema.sql. Later versions append to MIGRATIONS as
lists of statements; `migrate` steps a database forward one version at a time.
"""

from __future__ import annotations

from importlib import resources

import aiosqlite

SCHEMA_VERSION = 1

# version -> statements taking the database from (version - 1) to version.
# Version 1 is special: it comes from schema.sql.
MIGRATIONS: dict[int, list[str]] = {}


def base_schema() -> str:
    return resources.files("prunebot.db").joinpath("schema.sql").read_text(encoding="utf-8")


async def current_version(db: aiosqlite.Connection) -> int:
    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def migrate(db: aiosqlite.Connection) -> tuple[int, int]:
    """Bring the database up to SCHEMA_VERSION. Returns (from_version, to_version)."""
    start = await current_version(db)
    if start > SCHEMA_VERSION:
        raise RuntimeError(
            f"database is at schema version {start}, but this build only understands "
            f"{SCHEMA_VERSION}. Refusing to run against a newer database -- "
            f"downgrade the data or upgrade the bot."
        )
    if start == 0:
        await db.executescript(base_schema())
        await db.execute(f"PRAGMA user_version = {1}")
        start = 1

    for version in range(start + 1, SCHEMA_VERSION + 1):
        for statement in MIGRATIONS.get(version, []):
            await db.execute(statement)
        await db.execute(f"PRAGMA user_version = {version}")

    await db.commit()
    return start, SCHEMA_VERSION
