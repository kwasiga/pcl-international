import asyncio
import logging
import os
import signal

import discord
from discord.ext import commands, tasks

from bot.commands import StaffCommands
from bot.config import Settings
from bot.database.connection import Database
from bot.services.engine import Engine
from bot.services.rules import BotPausedError

log = logging.getLogger("pcl")


class PCLInternational(commands.Bot):
    def __init__(self, settings, db):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            application_id=settings.application_id,
            allowed_mentions=discord.AllowedMentions.none(),
            activity=discord.Game("PCL International"),
        )
        self.settings, self.db = settings, db
        self.engine = Engine(db, self)
        self.boot_reconciled = set()

    async def setup_hook(self):
        await self.add_cog(StaffCommands(self))
        if self.settings.test_guild_id:
            guild = discord.Object(id=self.settings.test_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.maintenance.start()

    async def on_ready(self):
        log.info("PCL International connected as %s in %s server(s)", self.user, len(self.guilds))

    async def on_member_update(self, before, after):
        try:
            await self.engine.member_update(before, after)
        except BotPausedError:
            await self.engine.defer_event(after)

    async def on_member_remove(self, member):
        try:
            await self.engine.member_left(member)
        except BotPausedError:
            await self.engine.defer_event(member)

    async def on_member_join(self, member):
        try:
            await self.engine.member_joined(member)
        except BotPausedError:
            await self.engine.defer_event(member, restore=True)

    @tasks.loop(seconds=60)
    async def maintenance(self):
        for guild in self.guilds:
            try:
                async with self.db.pool.acquire() as con:
                    if await self.engine.is_paused(con, guild.id):
                        continue
                if not guild.chunked:
                    await guild.chunk()
                await self.engine.recover(guild)
                if guild.id not in self.boot_reconciled:
                    async with self.db.pool.acquire() as con:
                        configured = await con.fetchval(
                            "SELECT 1 FROM guild_config WHERE guild_id=$1", guild.id
                        )
                    if configured:
                        # Reports missing roles; never blindly restores roles removed while online.
                        await self.engine.sync(guild)
                    self.boot_reconciled.add(guild.id)
                await self.engine.flush_outbox(guild)
            except BotPausedError:
                continue
            except Exception:
                log.exception("Maintenance failed for guild %s; retrying next minute", guild.id)

    @maintenance.before_loop
    async def before_maintenance(self):
        await self.wait_until_ready()

    async def close(self):
        if self.maintenance.is_running():
            task = self.maintenance.get_task()
            self.maintenance.cancel()
            if task and task is not asyncio.current_task():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await super().close()


async def run():
    settings = Settings.load()
    db = await Database.connect(settings.database_url)
    try:
        await db.migrate()
        async with PCLInternational(settings, db) as bot:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
            await bot.start(settings.token)
    finally:
        await db.close()


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # No message-content intent is needed: commands are slash commands only.
    try:
        asyncio.run(run())
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
