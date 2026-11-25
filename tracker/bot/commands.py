"""Slash commands: two ways into the bot's screens.

/flipscout opens your home screen; /stock jumps straight to a product. Everything else (watches,
flips, radar, server settings) is reached from there with buttons and menus that change the
same private message. See tracker.bot.screens.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from tracker.bot import screens
from tracker.guilds import GuildConfig
from tracker.permissions import check_access
from tracker.queries import ListingRow, search_variants

if TYPE_CHECKING:
    from tracker.bot.client import TrackerBot
    from tracker.context import TrackerContext

logger = logging.getLogger(__name__)


class TrackerCommands(commands.Cog):
    def __init__(self, bot: TrackerBot) -> None:
        self.bot = bot

    @property
    def ctx(self) -> TrackerContext:
        return self.bot.ctx

    async def interaction_check(self, interaction: discord.Interaction[Any]) -> bool:
        """Gate every slash command: approved server + the command's minimum role."""
        name = interaction.command.qualified_name if interaction.command else "?"
        role, guild_config = await screens.access(self.bot, interaction)
        reason = check_access(name, role, guild_config)
        if reason is not None:
            await interaction.response.send_message(f"⛔ {reason}", ephemeral=True)
            return False
        interaction.extras["role"] = role
        interaction.extras["guild_config"] = guild_config
        return True

    def _viewer(self, interaction: discord.Interaction[Any]) -> screens.Viewer:
        guild: GuildConfig = interaction.extras["guild_config"]
        return screens.Viewer(interaction, self.bot, interaction.extras["role"], guild)

    async def _resolve(self, text: str, region: str | None) -> tuple[list[ListingRow], str | None]:
        resolved = await screens.resolve(self.ctx, text, region)
        return resolved.rows, resolved.error

    async def product_autocomplete(
        self, interaction: discord.Interaction[Any], current: str
    ) -> list[app_commands.Choice[str]]:
        async with self.ctx.sessions() as session:
            matches = await search_variants(session, current, limit=25)
        return [app_commands.Choice(name=m.label[:100], value=m.sku) for m in matches]

    @app_commands.command(description="Open FlipScout: your watches, flips, radar and settings")
    async def flipscout(self, interaction: discord.Interaction[Any]) -> None:
        viewer = self._viewer(interaction)
        await viewer.open(await screens.home_screen(viewer))

    @app_commands.command(description="Look up a product: stock, history, profit, and watching it")
    @app_commands.describe(product="SKU, product name, or store link")
    @app_commands.autocomplete(product=product_autocomplete)
    async def stock(self, interaction: discord.Interaction[Any], product: str) -> None:
        # Acknowledge first: an unknown store link is fetched, which can take a few seconds.
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows, error = await self._resolve(product, None)
        if error or not rows:
            await interaction.followup.send(f"⚠️ {error or 'Nothing found.'}", ephemeral=True)
            return
        viewer = self._viewer(interaction)
        state = screens.product_state(rows[0].variant.id, screens.HOME)
        await viewer.open(await screens.product_screen(viewer, state))

    async def cog_app_command_error(
        self, interaction: discord.Interaction[Any], error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, screens.Denied):
            message = f"⚠️ {original}"
        else:
            logger.exception(
                "Slash command %s failed", getattr(interaction.command, "name", "?"), exc_info=error
            )
            message = "⚠️ Something went wrong. The error was logged."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
