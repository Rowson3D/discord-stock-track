"""The Discord client: slash commands, persistent buttons, command sync."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from tracker.bot.commands import TrackerCommands
from tracker.bot.views import WatchButton
from tracker.context import TrackerContext

logger = logging.getLogger(__name__)


class TrackerBot(commands.Bot):
    def __init__(self, ctx: TrackerContext) -> None:
        # Slash commands only: no privileged message-content intent needed.
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
        )
        self.ctx = ctx

    async def setup_hook(self) -> None:
        await self.add_cog(TrackerCommands(self))
        self.add_dynamic_items(WatchButton)

        guild_id = self.ctx.settings.discord.guild_id
        if guild_id:
            # Instant sync to one server, and clear stale global commands (e.g. the old bot's).
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            synced = await self.tree.sync()
        logger.info(
            "Synced %d slash command(s)%s",
            len(synced),
            f" to guild {guild_id}" if guild_id else " globally",
        )

    async def on_ready(self) -> None:
        assert self.user is not None
        logger.info(
            "Discord connected as %s (%s) in %d server(s)",
            self.user,
            self.user.id,
            len(self.guilds),
        )
