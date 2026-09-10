"""Scheduled work: the nightly sweep, the activity flush, and daily maintenance."""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord.ext import commands, tasks

from ..domain.windows import retention_cutoff_day
from ..services.backfill import BackfillRunner, NothingScanned, NotReady
from ..services.sweep import run_sweep
from ..ui.embeds import summary_embed

log = logging.getLogger(__name__)


class Sweeper(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._backfilled: set[int] = set()

    async def cog_load(self) -> None:
        config = self.bot.base_config
        tz = config.bot.tzinfo
        times = [
            dt.time(hour=int(h), minute=int(m), tzinfo=tz)
            for h, _, m in (item.partition(":") for item in config.sweep.run_at)
        ]
        # ZoneInfo, not a fixed offset: a bare .astimezone() tz carries no DST
        # transitions, so the sweep would drift by an hour twice a year.
        self.sweep_loop.change_interval(time=times)
        self.flush_loop.change_interval(seconds=config.storage.flush_interval_seconds)

        self.flush_loop.start()
        self.maintenance_loop.start()

        if self.bot.secrets.panic:
            log.warning("PRUNE_PANIC is set: the sweep loop will not start")
        elif not config.sweep.enabled:
            log.warning("sweep.enabled is false: the sweep loop will not start")
        else:
            self.sweep_loop.start()
            log.info(
                "sweep scheduled at %s (%s)",
                ", ".join(config.sweep.run_at),
                config.bot.timezone,
            )

    async def cog_unload(self) -> None:
        self.flush_loop.cancel()
        self.maintenance_loop.cancel()
        self.sweep_loop.cancel()

    # ------------------------------------------------------------------- flushing

    @tasks.loop(seconds=30)
    async def flush_loop(self) -> None:
        try:
            await self.bot.activity_buffer.flush()
        except Exception:
            log.exception("activity flush failed")

    @flush_loop.before_loop
    async def _before_flush(self) -> None:
        # Never in setup_hook: wait_until_ready() deadlocks there.
        await self.bot.wait_until_ready()

    # ---------------------------------------------------------------- maintenance

    @tasks.loop(hours=24)
    async def maintenance_loop(self) -> None:
        try:
            cutoff = retention_cutoff_day(
                discord.utils.utcnow(), self.bot.base_config.storage.retention_days
            )
            removed = await self.bot.store.prune_activity(cutoff)
            if removed:
                log.info("pruned %d expired activity bucket(s)", removed)
        except Exception:
            log.exception("maintenance failed")

    @maintenance_loop.before_loop
    async def _before_maintenance(self) -> None:
        await self.bot.wait_until_ready()
        await self._run_initial_backfills()

    async def _run_initial_backfills(self) -> None:
        """Seed the window on first run. Runs in the background, never blocks."""
        for guild in self.bot.guilds:
            if not self.bot.is_managed(guild.id) or guild.id in self._backfilled:
                continue
            config = await self.bot.config_for(guild.id)
            if not config.backfill.enabled:
                continue
            meta = await self.bot.store.guild_meta(guild.id)
            if meta.get("backfilled_at") and config.backfill.on_first_run_only:
                self._backfilled.add(guild.id)
                continue

            gateway = await self.bot.gateway_for(guild.id)
            log.info("starting history backfill for guild %s", guild.id)
            if gateway:
                await gateway.post_audit(
                    "Starting history backfill. Flagging and kicking stay disabled "
                    "until it finishes.",
                    title="Backfill started",
                )

            async def progress(total: int, channels: int, _gw=gateway) -> None:
                if _gw:
                    await _gw.post_audit(f"Backfill progress: {total} messages, {channels} channels done.")

            runner = BackfillRunner(
                store=self.bot.store,
                config=config,
                guild=guild,
                before=self.bot.started_at,
                progress=progress,
            )
            try:
                await runner.run()
            except (NotReady, NothingScanned) as exc:
                # Deliberately NOT added to self._backfilled: these are transient or
                # fixable, and must be retried rather than remembered as done.
                log.error("backfill did NOT run for guild %s: %s", guild.id, exc)
                if gateway:
                    await gateway.post_audit(
                        f"{exc} Flagging and kicking stay blocked. Fix the cause, "
                        f"then run /prune backfill force:true",
                        title="Backfill could not run",
                        alert=True,
                    )
                continue
            except Exception:
                log.exception("backfill failed for guild %s", guild.id)
                if gateway:
                    await gateway.post_audit(
                        "Backfill failed; see the container logs. Flagging and "
                        "kicking remain disabled.",
                        title="Backfill failed",
                        alert=True,
                    )
                continue

            self._backfilled.add(guild.id)
            log.info("backfill summary for %s: %s", guild.id, runner.summary)
            if gateway:
                await gateway.post_audit(
                    runner.summary, title="Backfill complete"
                )

    # ---------------------------------------------------------------------- sweep

    @tasks.loop(time=dt.time(hour=4, tzinfo=dt.UTC))
    async def sweep_loop(self) -> None:
        for guild in self.bot.guilds:
            if not self.bot.is_managed(guild.id):
                continue
            try:
                await self.run_for(guild.id)
            except Exception:
                log.exception("sweep failed for guild %s", guild.id)

    @sweep_loop.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    async def run_for(self, guild_id: int, *, dry_run: bool | None = None, actor_id=None):
        config = await self.bot.config_for(guild_id, refresh=True)

        # The member cache is still chunking right after connect; sweeping then
        # would see a partial guild and could flag people who simply are not loaded.
        uptime = (discord.utils.utcnow() - self.bot.started_at).total_seconds() / 60
        if uptime < config.safety.min_uptime_before_sweep_minutes:
            log.info(
                "skipping sweep for %s: only %.1f minutes of uptime", guild_id, uptime
            )
            return None

        gateway = await self.bot.gateway_for(guild_id)
        if gateway is None:
            return None

        # Flush first so the sweep counts everything said up to this moment.
        await self.bot.activity_buffer.flush()

        result = await run_sweep(
            gateway=gateway,
            store=self.bot.store,
            config=config,
            now=discord.utils.utcnow(),
            dry_run=dry_run,
            actor_id=actor_id,
        )
        if config.audit.post_sweep_summary:
            await gateway.post_embed(summary_embed(result, config=config))
        return result


async def setup(bot) -> None:
    await bot.add_cog(Sweeper(bot))
