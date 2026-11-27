"""The owner's update DMs: "a new version is out" with Update now, and how the update went."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import discord

from tracker.updates import Notice, UpdateError, short

if TYPE_CHECKING:
    from tracker.bot.client import TrackerBot

logger = logging.getLogger(__name__)

SHOWN_COMMITS = 10
GREEN, AMBER, RED = 0x3BA55C, 0xFEE75C, 0xED4245


class UpdateButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"flipscout:update:(?P<sha>[0-9a-f]{40})",
):
    """ "Update now" in the owner's DM. It only asks; the host's scripts/update.sh does the work."""

    def __init__(self, sha: str, label: str = "Update now") -> None:
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success,
                custom_id=f"flipscout:update:{sha}",
            )
        )
        self.sha = sha

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> UpdateButton:
        return cls(match["sha"])

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        bot: TrackerBot = interaction.client  # type: ignore[assignment]
        if str(interaction.user.id) not in bot.owner_ids:
            await interaction.response.send_message("Only the owner can update.", ephemeral=True)
            return
        if bot.updater is None:
            await interaction.response.send_message(
                "Updates aren't set up on this bot.", ephemeral=True
            )
            return
        try:
            message = bot.updater.request(self.sha)
        except (UpdateError, OSError) as exc:
            logger.warning("Update request failed: %s", exc)
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        # Answer first: Discord gives a button 3 seconds, and the audit log is a database write.
        await interaction.response.edit_message(content=message, view=None)
        await bot.audit_owner("bot.update", short(self.sha), interaction.user)


def update_view(sha: str, compare_url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(UpdateButton(sha))
    if compare_url:
        view.add_item(discord.ui.Button(label="See the changes ↗", url=compare_url))
    return view


def notice_message(notice: Notice) -> tuple[discord.Embed, discord.ui.View | None]:
    """The DM for one notice."""
    if notice.available is not None:
        new = notice.available
        lines = [
            f"[`{short(c.sha)}`]({c.url}) {discord.utils.escape_markdown(c.title)}"
            for c in reversed(new.commits[-SHOWN_COMMITS:])
        ]
        if new.ahead_by > SHOWN_COMMITS:
            lines.append(f"…and {new.ahead_by - SHOWN_COMMITS} older")
        if new.server_files:
            shown = ", ".join(f"`{f}`" for f in new.server_files[:6])
            lines += [
                "",
                f"⚠️ **Changes how it runs on your server:** {shown}. Update now builds and runs "
                "exactly this, so look at the changes first if you didn't make them.",
            ]
        embed = discord.Embed(
            title=f"FlipScout update: {new.ahead_by} new commit{'s' if new.ahead_by != 1 else ''}",
            description="\n".join(lines)[:4000],
            color=AMBER,
        )
        embed.set_footer(text="Update now rebuilds and restarts the bot on its server (~2 min).")
        return embed, update_view(new.head, new.compare_url)
    if notice.result is not None:
        result = notice.result
        if result.ok:
            running = notice.running or result.sha
            embed = discord.Embed(
                title=f"✅ FlipScout updated to {short(running)}" if running else "✅ Updated",
                # scripts/update.sh lists the new commits as "sha title|sha title|…".
                description="\n".join(f"• {line}" for line in result.detail.split("|") if line)
                or None,
                color=GREEN,
            )
        else:
            retry = result.target if result.target and result.target != notice.running else ""
            embed = discord.Embed(
                title="⚠️ FlipScout update failed",
                description=(
                    f"Still running `{short(notice.running)}`.\n" if notice.running else ""
                )
                + (f"```\n{result.detail}\n```" if result.detail else "")
                + "Details: `update.log` on the server.",
                color=RED,
            )
            if retry:
                view = discord.ui.View(timeout=None)
                view.add_item(UpdateButton(retry, label="Try again"))
                return embed, view
        return embed, None
    sha = notice.stale_request or ""
    embed = discord.Embed(
        title="⚠️ Nobody picked up the update",
        description=(
            f"The update to `{short(sha)}` waited 10 minutes. On the server, run "
            "`scripts/update.sh --install` once so updates run from cron, or "
            "`scripts/update.sh` to update now."
        ),
        color=RED,
    )
    return embed, None
