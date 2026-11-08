"""The Discord client: slash commands, persistent buttons, server allowlist, command sync."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import discord
from discord.ext import commands

from tracker.audit import audit
from tracker.bot.commands import TrackerCommands
from tracker.bot.views import WatchButton
from tracker.context import TrackerContext
from tracker.guilds import (
    GuildStatus,
    approved_configs,
    decide,
    ensure_guild,
    get_guild,
    seed_home,
)
from tracker.watchtest import WatchTest

logger = logging.getLogger(__name__)


class GuildDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"tracker:guild:(?P<action>approve|block):(?P<guild_id>\d+)",
):
    """Approve/Block buttons in the owner's DM when the bot is added to a new server."""

    def __init__(self, action: str, guild_id: int) -> None:
        approve = action == "approve"
        super().__init__(
            discord.ui.Button(
                label="Approve" if approve else "Block & leave",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"tracker:guild:{action}:{guild_id}",
            )
        )
        self.action = action
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> GuildDecisionButton:
        return cls(match["action"], int(match["guild_id"]))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        bot: TrackerBot = interaction.client  # type: ignore[assignment]
        if str(interaction.user.id) not in bot.owner_ids:
            await interaction.response.send_message("Only the owner can decide.", ephemeral=True)
            return
        status = GuildStatus.APPROVED if self.action == "approve" else GuildStatus.BLOCKED
        await bot.decide_guild(self.guild_id, status, interaction.user)
        await interaction.response.send_message(
            f"Server `{self.guild_id}` {status.value}.",
            ephemeral=True,
        )


def approval_view(guild_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(GuildDecisionButton("approve", guild_id))
    view.add_item(GuildDecisionButton("block", guild_id))
    return view


class TrackerBot(commands.Bot):
    def __init__(self, ctx: TrackerContext) -> None:
        # Slash commands only: no privileged intents (message content, members, presence).
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
        )
        self.ctx = ctx
        self.owner_ids: set[str] = set(ctx.settings.discord.owner_ids)
        self._pending_leaves: dict[int, asyncio.Task[None]] = {}

    async def setup_hook(self) -> None:
        await self.add_cog(TrackerCommands(self))
        self.add_dynamic_items(WatchButton, GuildDecisionButton)
        if not self.owner_ids:
            info = await self.application_info()
            if info.team:
                self.owner_ids = {str(m.id) for m in info.team.members}
            elif info.owner:
                self.owner_ids = {str(info.owner.id)}
        logger.info("Owner(s): %s", ", ".join(sorted(self.owner_ids)) or "none!")

        async with self.ctx.sessions() as session:
            configs = await approved_configs(session, self.ctx.settings.discord)
        for config in configs:
            await self.sync_guild(int(config.guild_id))
        # Commands live per approved server; clear global ones (e.g. the old bot's).
        self.tree.clear_commands(guild=None)
        await self.tree.sync()

    async def sync_guild(self, guild_id: int) -> None:
        guild = discord.Object(id=guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d command(s) to server %s", len(synced), guild_id)
        except discord.HTTPException as exc:
            logger.warning("Command sync to server %s failed: %s", guild_id, exc)

    async def on_ready(self) -> None:
        assert self.user is not None
        logger.info(
            "Discord connected as %s (%s) in %d server(s)",
            self.user,
            self.user.id,
            len(self.guilds),
        )
        await self._adopt_legacy_home()
        for guild in self.guilds:
            await self.enforce_allowlist(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        logger.info("Added to server %s (%s)", guild.name, guild.id)
        await self.enforce_allowlist(guild)

    async def _adopt_legacy_home(self) -> None:
        """Single-server installs (only DISCORD_CHANNEL_ID set): approve that channel's server."""
        settings = self.ctx.settings.discord
        if settings.guild_id or not settings.channel_id:
            return
        channel = self.get_channel(settings.channel_id)
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        async with self.ctx.sessions() as session, session.begin():
            row = await get_guild(session, guild.id)
            if row is not None and row.status != GuildStatus.PENDING:
                return
            await decide(session, guild.id, GuildStatus.APPROVED, actor_id=None, name=guild.name)
            await seed_home(session, str(guild.id), settings)
        logger.info(
            "Adopted %s (%s) as the home server from DISCORD_CHANNEL_ID", guild.name, guild.id
        )
        await self.sync_guild(guild.id)

    async def enforce_allowlist(self, guild: discord.Guild) -> None:
        async with self.ctx.sessions() as session, session.begin():
            row, created = await ensure_guild(session, guild.id, guild.name)
            status = row.status
            if created:
                await audit(session, "guild.joined", guild_id=guild.id, target=guild.name)
        if status == GuildStatus.APPROVED:
            return
        if status == GuildStatus.BLOCKED:
            await self._leave(guild, "blocked")
            return
        if guild.id not in self._pending_leaves:
            await self._request_approval(guild)
            grace = self.ctx.settings.discord.approval_grace_minutes * 60
            self._pending_leaves[guild.id] = asyncio.create_task(
                self._leave_if_unapproved(guild, grace)
            )

    async def _request_approval(self, guild: discord.Guild) -> None:
        minutes = int(self.ctx.settings.discord.approval_grace_minutes)
        embed = discord.Embed(
            title="Approve this server?",
            description=(
                f"The tracker was added to **{discord.utils.escape_markdown(guild.name)}** "
                f"(`{guild.id}`, {guild.member_count or '?'} members).\n"
                f"It does nothing there until approved and leaves in {minutes} minutes otherwise."
            ),
            color=0xFEE75C,
        )
        for owner_id in self.owner_ids:
            try:
                user = await self.fetch_user(int(owner_id))
                await user.send(embed=embed, view=approval_view(guild.id))
            except discord.HTTPException as exc:
                logger.warning("Couldn't DM owner %s about server %s: %s", owner_id, guild.id, exc)

    async def _leave_if_unapproved(self, guild: discord.Guild, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self.ctx.sessions() as session:
                row = await get_guild(session, guild.id)
            if row is None or row.status != GuildStatus.APPROVED:
                await self._leave(guild, "not approved in time")
        finally:
            self._pending_leaves.pop(guild.id, None)

    async def _leave(self, guild: discord.Guild, reason: str) -> None:
        logger.warning("Leaving server %s (%s): %s", guild.name, guild.id, reason)
        async with self.ctx.sessions() as session, session.begin():
            await audit(
                session,
                "guild.left",
                guild_id=guild.id,
                target=guild.name,
                details={"reason": reason},
            )
        try:
            await guild.leave()
        except discord.HTTPException as exc:
            logger.error("Failed to leave server %s: %s", guild.id, exc)

    async def post_test_alert(self, test: WatchTest) -> str:
        """Post a watch test where the real alert would go (raises SampleAlertError)."""
        from tracker.bot.sink import send_test_alert

        return await send_test_alert(self, test)

    def channel_label(self, channel_id: int) -> str | None:
        channel = self.get_channel(channel_id)
        name = getattr(channel, "name", None)
        return f"#{name}" if name else None

    async def decide_guild(
        self, guild_id: int, status: GuildStatus, actor: discord.abc.User
    ) -> None:
        await self.apply_guild_decision(str(guild_id), status, str(actor.id), str(actor))

    async def apply_guild_decision(
        self, guild_id_text: str, status: GuildStatus, actor_id: str | None, actor_name: str
    ) -> None:
        """Approve or block a server from any front end (Discord command or owner panel)."""
        guild_id = int(guild_id_text)
        guild = self.get_guild(guild_id)
        async with self.ctx.sessions() as session, session.begin():
            await decide(
                session,
                guild_id,
                status,
                actor_id=actor_id,
                actor_name=actor_name,
                name=guild.name if guild else None,
            )
        pending = self._pending_leaves.pop(guild_id, None)
        if pending:
            pending.cancel()
        if status is GuildStatus.APPROVED:
            await self.sync_guild(guild_id)
        elif guild is not None:
            await self._leave(guild, "blocked by owner")
