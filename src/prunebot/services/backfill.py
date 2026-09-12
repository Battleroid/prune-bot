"""Seed activity counts by scanning channel history.

Discord has no API for "how many messages has this user sent recently", so the
window has to be reconstructed from history the first time the bot runs. Until
this completes, the sweep refuses to flag or kick anyone -- otherwise the first
run would flag the entire server.

The scan covers strictly *before* the bot came online and the live listener covers
everything after, so the two can never double-count the same message.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

import discord

from ..config import Config
from ..db.store import Store
from ..domain.windows import day_of
from .activity import COUNTED_TYPES, ActivityBuffer

log = logging.getLogger(__name__)

FLUSH_EVERY = 10_000


class NotReady(RuntimeError):
    """The guild cache is not populated enough to scan it accurately."""


class NothingScanned(RuntimeError):
    """No readable channel was found, so any claim of coverage would be a lie."""


#: Why `/prune backfill` refuses `force` together with `channel`.
FORCE_WITH_CHANNEL = (
    "`force` can't be combined with `channel`. Activity is stored as each member's "
    "daily total across the whole server, not per channel, so there is no way to "
    "remove just that channel's old counts first -- rescanning it would add its "
    "messages a second time. Run `/prune backfill force:true` without a channel to "
    "rebuild everything."
)


class BackfillRunner:
    def __init__(
        self,
        *,
        store: Store,
        config: Config,
        guild: discord.Guild,
        before: datetime,
        progress=None,
    ) -> None:
        self.store = store
        self.config = config
        self.guild = guild
        self.before = before
        self.progress = progress
        self.total = 0
        self.channels_done = 0
        self.channels_skipped: list[str] = []
        self.errors: list[str] = []

    # ------------------------------------------------------------------- helpers

    def _readable(self, channel) -> bool:
        """Check from cache rather than discovering it with a 403.

        A missing `guild.me` is deliberately NOT treated as "no permission": that
        made an unpopulated cache look identical to a locked-down server, and the
        backfill would silently scan nothing and then claim success.
        """
        me = self.guild.me
        if me is None:
            raise NotReady(
                "guild.me is not populated, so channel permissions cannot be read"
            )
        perms = channel.permissions_for(me)
        return bool(perms.view_channel and perms.read_message_history)

    def _in_scope(self, channel_id: int) -> bool:
        channels = self.config.activity.channels
        if channels.mode == "include":
            return channel_id in set(channels.include)
        return channel_id not in set(channels.exclude)

    def _target_channels(self) -> list:
        self.channels_skipped = []  # recomputed on every call
        out = []
        for channel in self.guild.text_channels + list(self.guild.forums):
            if not self._in_scope(channel.id):
                continue
            if not self._readable(channel):
                self.channels_skipped.append(f"#{channel.name} (no read access)")
                continue
            out.append(channel)
        return out

    def check_ready(self) -> None:
        """Raise NotReady or NothingScanned if a scan could not run. Reads only
        the cache, so it is cheap.

        A forced rebuild calls this before wiping the existing counts, so a scan
        that would read nothing never gets to leave the server with no counts.
        """
        if self.guild.me is None:
            raise NotReady("the bot's own member object is not in the cache yet")
        if not self._target_channels():
            raise NothingScanned(
                f"found no readable channels in {self.guild.name} "
                f"({len(self.channels_skipped)} skipped for lack of View Channel or "
                f"Read Message History)"
            )

    async def _threads_of(self, channel) -> list:
        if not (self.config.activity.count_threads or self.config.activity.count_forum_posts):
            return []
        is_forum = getattr(channel, "type", None) == discord.ChannelType.forum
        if is_forum and not self.config.activity.count_forum_posts:
            return []
        if not is_forum and not self.config.activity.count_threads:
            return []

        threads = list(getattr(channel, "threads", []))
        try:
            async for thread in channel.archived_threads(limit=None):
                threads.append(thread)
        except (discord.HTTPException, AttributeError) as exc:
            # Private archived threads need Manage Threads; public ones usually work.
            log.debug("archived threads unavailable for %s: %s", channel, exc)
        return threads

    # -------------------------------------------------------------------- scanning

    async def _scan_one(
        self,
        channel,
        *,
        after: datetime,
        counts: defaultdict[tuple[int, int, int], int],
        parent_id: int,
    ) -> None:
        cursor = await self.store.backfill_cursor(self.guild.id, channel.id)
        if cursor and cursor.get("completed_at"):
            return

        start: object = after
        seen = int(cursor["messages_seen"]) if cursor else 0
        if cursor and cursor.get("last_message_id"):
            start = discord.Object(id=int(cursor["last_message_id"]))

        last_id: int | None = cursor.get("last_message_id") if cursor else None
        cap = self.config.backfill.max_messages_per_channel

        try:
            async for message in channel.history(
                after=start, before=self.before, limit=None, oldest_first=True
            ):
                last_id = message.id
                seen += 1
                self.total += 1
                if (
                    not message.author.bot
                    and message.type in COUNTED_TYPES
                    and self._in_scope(parent_id)
                ):
                    counts[
                        (self.guild.id, message.author.id, day_of(message.created_at))
                    ] += 1

                if seen % FLUSH_EVERY == 0:
                    await self._flush(counts)
                    await self.store.save_backfill_cursor(
                        self.guild.id,
                        channel.id,
                        last_message_id=last_id,
                        messages_seen=seen,
                    )
                    await self._report()
                if seen >= cap:
                    self.errors.append(f"#{channel} hit max_messages_per_channel ({cap})")
                    break
        except discord.HTTPException as exc:
            self.errors.append(f"{channel}: {exc}")
            await self.store.save_backfill_cursor(
                self.guild.id,
                channel.id,
                last_message_id=last_id,
                messages_seen=seen,
                error=str(exc),
            )
            return

        await self.store.save_backfill_cursor(
            self.guild.id,
            channel.id,
            last_message_id=last_id,
            messages_seen=seen,
            window_start=int(after.timestamp()),
            completed=True,
        )
        self.channels_done += 1

    async def _flush(self, counts: defaultdict[tuple[int, int, int], int]) -> None:
        if not counts:
            return
        await self.store.bump_activity(
            [(g, u, day, n) for (g, u, day), n in counts.items()]
        )
        counts.clear()

    async def _report(self) -> None:
        if self.progress and self.total % self.config.backfill.progress_report_every == 0:
            await self.progress(self.total, self.channels_done)

    # ------------------------------------------------------------------- entrypoint

    async def run(self, *, channels: Iterable | None = None) -> None:
        if self.guild.me is None:
            raise NotReady(
                "the bot's own member object is not in the cache yet; refusing to "
                "scan, because every channel would look unreadable"
            )

        after = discord.utils.utcnow() - timedelta(
            days=self.config.storage.retention_days
        )
        targets = list(channels) if channels is not None else self._target_channels()
        if not targets:
            # Claiming coverage here would unlock the sweep against no data at all,
            # which is precisely the mass-flagging this gate exists to prevent.
            raise NothingScanned(
                f"found no readable channels in {self.guild.name}. "
                f"{len(self.channels_skipped)} were skipped for lack of View Channel "
                f"or Read Message History. Not marking this guild as backfilled."
            )
        semaphore = asyncio.Semaphore(self.config.backfill.concurrency)

        async def worker(channel) -> None:
            async with semaphore:
                counts: defaultdict[tuple[int, int, int], int] = defaultdict(int)
                await self._scan_one(
                    channel, after=after, counts=counts, parent_id=channel.id
                )
                for thread in await self._threads_of(channel):
                    if not self._readable(thread):
                        continue
                    await self._scan_one(
                        thread, after=after, counts=counts, parent_id=channel.id
                    )
                await self._flush(counts)

        await asyncio.gather(*(worker(c) for c in targets), return_exceptions=False)

        if self.channels_done == 0:
            raise NothingScanned(
                f"every channel in {self.guild.name} failed to scan. "
                f"Not marking this guild as backfilled."
            )

        # Only claim coverage back to `after` if nothing errored or was truncated;
        # otherwise the sweep's backfill gate would unlock on incomplete data.
        covers = int(after.timestamp())
        if self.errors:
            covers = int(discord.utils.utcnow().timestamp())
        await self.store.set_backfilled(self.guild.id, covers=covers)
        log.info(
            "backfill complete for %s: %d messages, %d channels, %d skipped, %d errors",
            self.guild.id,
            self.total,
            self.channels_done,
            len(self.channels_skipped),
            len(self.errors),
        )

    @property
    def summary(self) -> str:
        parts = [
            f"scanned **{self.total}** messages across **{self.channels_done}** channels"
        ]
        if self.channels_skipped:
            parts.append(
                f"skipped {len(self.channels_skipped)}: "
                + ", ".join(self.channels_skipped[:8])
            )
        if self.errors:
            parts.append("errors: " + "; ".join(self.errors[:5]))
        return "\n".join(parts)


async def prepare_forced_rebuild(
    *,
    store: Store,
    buffer: ActivityBuffer,
    guild_id: int,
    now: datetime | None = None,
) -> datetime:
    """Clear the way for a full rescan that rebuilds counts instead of adding to
    them. Returns the cutover to pass to the rescan as `before`.

    History before the cutover is rescanned; the live listener counts everything
    after it. So each message is counted exactly once:

    - the guild's stored counts are wiped, because the rescan recounts them;
    - pending buffered counts are dropped rather than flushed, for the same reason;
    - the listener ignores messages created before the cutover, which covers one
      sent a moment before it that only reaches the bot a moment after;
    - it all happens under the buffer's flush lock, so a periodic flush already in
      flight cannot write its counts after the wipe.

    Clearing the cursors also clears `backfilled_at`, which closes the backfill
    gate: sweeps and /prune flag stay paused until the rescan completes, rather
    than acting on half-rebuilt counts.
    """
    async with buffer.lock:
        cutover = now or discord.utils.utcnow()
        dropped = buffer.discard_counts(guild_id)
        buffer.set_floor(guild_id, cutover.timestamp())
        wiped = await store.clear_guild_activity(guild_id)
        await store.reset_backfill(guild_id)
    log.info(
        "forced rebuild for %s: wiped %d bucket(s), dropped %d buffered message(s), "
        "cutover %s",
        guild_id,
        wiped,
        dropped,
        cutover.isoformat(),
    )
    return cutover
