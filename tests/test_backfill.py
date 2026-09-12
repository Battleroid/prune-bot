"""Forced rebuilds: a full rescan must rebuild the counts, never add to them.

Uses a fake channel with a real async history, so these run the actual
BackfillRunner and ActivityBuffer against a real SQLite store.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from prunebot.services.activity import ActivityBuffer
from prunebot.services.backfill import (
    BackfillRunner,
    NothingScanned,
    prepare_forced_rebuild,
)

from .conftest import GUILD_ID
from .test_sweep_integration import make_config

_ids = itertools.count(10_000)


def message(user_id: int, *, ago: timedelta):
    return SimpleNamespace(
        id=next(_ids),
        author=SimpleNamespace(id=user_id, bot=False),
        type=discord.MessageType.default,
        created_at=discord.utils.utcnow() - ago,
    )


class FakeChannel:
    """Just enough of a TextChannel for BackfillRunner."""

    type = discord.ChannelType.text

    def __init__(self, channel_id: int, messages, *, readable: bool = True):
        self.id = channel_id
        self.name = f"channel-{channel_id}"
        self.threads = []
        self.messages = list(messages)
        self.readable = readable

    def __str__(self) -> str:
        return self.name

    def permissions_for(self, _member):
        return SimpleNamespace(
            view_channel=self.readable, read_message_history=self.readable
        )

    def history(self, *, after=None, before=None, limit=None, oldest_first=True):
        async def pages():
            for msg in sorted(self.messages, key=lambda m: m.created_at):
                if before is not None and msg.created_at >= before:
                    continue
                if isinstance(after, datetime) and msg.created_at <= after:
                    continue
                yield msg

        return pages()

    def archived_threads(self, *, limit=None):
        async def none():
            return
            yield  # pragma: no cover -- makes this an async generator

        return none()


def guild_of(*channels):
    return SimpleNamespace(
        id=GUILD_ID, name="Test", me=object(), text_channels=list(channels), forums=[]
    )


def new_runner(store, guild):
    return BackfillRunner(
        store=store, config=make_config(), guild=guild, before=discord.utils.utcnow()
    )


async def totals(store):
    return await store.window_counts(GUILD_ID, 0)


async def forced_rebuild(store, guild, buffer):
    """What `/prune backfill force:true` does, minus Discord."""
    runner = new_runner(store, guild)
    runner.check_ready()
    runner.before = await prepare_forced_rebuild(
        store=store, buffer=buffer, guild_id=GUILD_ID
    )
    await runner.run()
    await buffer.flush()


# ----------------------------------------------------------------- the bug itself


async def test_a_forced_rebuild_leaves_the_totals_unchanged(store):
    channel = FakeChannel(
        1,
        [
            message(1, ago=timedelta(days=2)),
            message(1, ago=timedelta(days=5)),
            message(2, ago=timedelta(days=1)),
        ],
    )
    guild = guild_of(channel)
    await new_runner(store, guild).run()
    assert await totals(store) == {1: 2, 2: 1}

    await forced_rebuild(store, guild, ActivityBuffer(store))

    assert await totals(store) == {1: 2, 2: 1}


async def test_resetting_the_cursors_alone_would_double_count(store):
    """The old force path. Proves the fake rescans fully, so the test above
    would have caught the bug -- and shows why the rebuild must wipe."""
    guild = guild_of(FakeChannel(1, [message(1, ago=timedelta(days=2))]))
    await new_runner(store, guild).run()
    await store.reset_backfill(GUILD_ID)
    await new_runner(store, guild).run()
    assert await totals(store) == {1: 2}


# --------------------------------------------------- live counting vs the rescan


async def test_messages_counted_live_and_flushed_are_counted_once(store):
    recent = message(1, ago=timedelta(minutes=5))
    guild = guild_of(FakeChannel(1, [recent]))
    buffer = ActivityBuffer(store)
    buffer.record(GUILD_ID, 1, recent.created_at.timestamp())
    await buffer.flush()

    await forced_rebuild(store, guild, buffer)

    assert await totals(store) == {1: 1}


async def test_messages_still_in_the_buffer_are_not_counted_twice(store):
    """Seen live but not yet flushed when the rebuild starts."""
    recent = message(1, ago=timedelta(seconds=5))
    guild = guild_of(FakeChannel(1, [recent]))
    buffer = ActivityBuffer(store)
    buffer.record(GUILD_ID, 1, recent.created_at.timestamp())

    await forced_rebuild(store, guild, buffer)

    assert await totals(store) == {1: 1}


async def test_messages_after_the_cutover_are_left_to_the_live_listener(store):
    guild = guild_of(FakeChannel(1, [message(1, ago=timedelta(days=1))]))
    buffer = ActivityBuffer(store)
    runner = new_runner(store, guild)
    runner.check_ready()
    cutover = await prepare_forced_rebuild(store=store, buffer=buffer, guild_id=GUILD_ID)
    runner.before = cutover
    buffer.record(GUILD_ID, 1, cutover.timestamp() + 1)  # said after the cutover

    await runner.run()
    await buffer.flush()

    assert await totals(store) == {1: 2}


def test_a_message_sent_before_the_cutover_but_arriving_late_is_left_to_history():
    buffer = ActivityBuffer(store=None)  # record() never touches the store
    buffer.set_floor(GUILD_ID, 1_000.5)

    buffer.record(GUILD_ID, 1, 1_000.4)  # history has this one
    assert buffer.pending == 0
    buffer.record(GUILD_ID, 1, 1_000.5)  # at the cutover: the listener's
    assert buffer.pending == 1
    buffer.record(GUILD_ID + 1, 1, 999.0)  # other guilds are unaffected
    assert buffer.pending == 2


async def test_a_flush_already_in_flight_cannot_land_after_the_wipe(store):
    """Without the shared lock, this flush writes after the wipe and the rescan
    then counts the same message again."""
    recent = message(1, ago=timedelta(seconds=5))
    guild = guild_of(FakeChannel(1, [recent]))
    buffer = ActivityBuffer(store)
    buffer.record(GUILD_ID, 1, recent.created_at.timestamp())

    release = asyncio.Event()
    real_bump = store.bump_activity

    async def slow_bump(rows):
        await release.wait()
        return await real_bump(rows)

    store.bump_activity = slow_bump
    try:
        flushing = asyncio.create_task(buffer.flush())
        await asyncio.sleep(0)  # the flush takes the lock and swaps the buffer out
        rebuilding = asyncio.create_task(forced_rebuild(store, guild, buffer))
        await asyncio.sleep(0)  # the rebuild is now waiting on that lock
        release.set()
        await flushing
        await rebuilding
    finally:
        store.bump_activity = real_bump

    assert await totals(store) == {1: 1}


# ------------------------------------------------------------------------ safety


async def test_a_rebuild_that_could_not_scan_refuses_before_wiping(store):
    await store.bump_activity([(GUILD_ID, 1, 20_000, 3)])
    guild = guild_of(FakeChannel(1, [], readable=False))

    with pytest.raises(NothingScanned):
        new_runner(store, guild).check_ready()

    assert await totals(store) == {1: 3}  # untouched


async def test_the_rebuild_closes_the_backfill_gate_until_it_completes(store):
    """Counts are incomplete mid-rebuild, so nothing may act on them."""
    guild = guild_of(FakeChannel(1, [message(1, ago=timedelta(days=1))]))
    await store.set_backfilled(GUILD_ID, covers=0)

    await prepare_forced_rebuild(store=store, buffer=ActivityBuffer(store), guild_id=GUILD_ID)
    assert (await store.guild_meta(GUILD_ID))["backfilled_at"] is None

    runner = new_runner(store, guild)
    runner.before = discord.utils.utcnow()
    await runner.run()
    assert (await store.guild_meta(GUILD_ID))["backfilled_at"] is not None


async def test_the_wipe_only_touches_the_rebuilt_guild(store):
    await store.bump_activity([(GUILD_ID, 1, 20_000, 3), (GUILD_ID + 1, 1, 20_000, 4)])

    await prepare_forced_rebuild(store=store, buffer=ActivityBuffer(store), guild_id=GUILD_ID)

    assert await store.window_counts(GUILD_ID, 0) == {}
    assert await store.window_counts(GUILD_ID + 1, 0) == {1: 4}
