"""
Stock Alert Discord Bot - Main Entry Point
"""
import asyncio
import logging
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from config import CONFIG
from monitor import StockMonitor

TOKEN_PLACEHOLDER = "YOUR_BOT_TOKEN_HERE"


class StockBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.monitor = StockMonitor(self)
        self.monitor_task: asyncio.Task | None = None
        self.cleanup_task: asyncio.Task | None = None
        self._start_time = time.monotonic()

    async def setup_hook(self):
        self.monitor.load_watchlist()
        self.monitor.load_subscribers()
        await self.add_cog(StockCommands(self))
        await self.tree.sync()
        if self.monitor_task is None or self.monitor_task.done():
            self.monitor_task = asyncio.create_task(self.monitor.run_loop(), name="stock-monitor")
        if CONFIG.get("message_cleanup", {}).get("enabled") and (self.cleanup_task is None or self.cleanup_task.done()):
            self.cleanup_task = asyncio.create_task(self.cleanup_loop(), name="message-cleanup")

    async def close(self):
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass

        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass

        await super().close()

    async def cleanup_loop(self):
        await self.wait_until_ready()

        cleanup = CONFIG.get("message_cleanup", {})
        interval_seconds = max(60, int(cleanup.get("interval_minutes", 10)) * 60)

        while not self.is_closed():
            await self.cleanup_channel_messages()
            await asyncio.sleep(interval_seconds)

    async def cleanup_channel_messages(self) -> int:
        cleanup = CONFIG.get("message_cleanup", {})
        channel = self.get_channel(CONFIG["discord"]["channel_id"])
        if not channel:
            try:
                channel = await self.fetch_channel(CONFIG["discord"]["channel_id"])
            except discord.DiscordException:
                return 0

        ttl_minutes = max(1, int(cleanup.get("ttl_minutes", 60)))
        cutoff = discord.utils.utcnow() - timedelta(minutes=ttl_minutes)
        scan_limit = max(1, int(cleanup.get("scan_limit", 200)))
        max_deletes = max(1, int(cleanup.get("max_deletes_per_run", 25)))
        delete_delay = max(0.5, float(cleanup.get("delete_delay_seconds", 1.25)))
        delete_user_commands = bool(cleanup.get("delete_user_commands", False))
        deleted = 0

        async for message in channel.history(limit=scan_limit):
            if deleted >= max_deletes:
                break
            if message.created_at > cutoff:
                continue
            if message.pinned:
                continue
            if not self._cleanup_can_delete(message, delete_user_commands):
                continue

            try:
                await message.delete()
                deleted += 1
                await asyncio.sleep(delete_delay)
            except discord.Forbidden:
                continue
            except discord.NotFound:
                continue
            except discord.HTTPException:
                continue

        return deleted

    def _cleanup_can_delete(self, message: discord.Message, delete_user_commands: bool) -> bool:
        if self.user and message.author.id == self.user.id:
            return True
        if delete_user_commands and message.content.startswith(str(self.command_prefix)):
            return True
        return False


class StockCommands(commands.Cog):
    """User-facing slash commands."""

    def __init__(self, bot: "StockBot"):
        self.bot = bot

    async def _safe_respond(self, interaction: discord.Interaction, *args, **kwargs):
        """Send a response, using followup if already acknowledged."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(*args, **kwargs)
            else:
                await interaction.response.send_message(*args, **kwargs)
        except discord.HTTPException as e:
            _bot_logger.error(f"Failed to respond to interaction: {e}")

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        _bot_logger.exception(f"Slash command error ({interaction.command and interaction.command.name}): {error}")
        await self._safe_respond(interaction, f"⚠️ Something went wrong: {error}", ephemeral=True)

    @app_commands.command(name="watch", description="Start watching a product URL for stock alerts")
    @app_commands.describe(url="Product URL — ui.com, amazon.com, bestbuy.com, bhphotovideo.com, newegg.com")
    async def slash_watch(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(ephemeral=True)
        result = self.bot.monitor.add_product(url, interaction.user.id)
        await interaction.followup.send(result, ephemeral=True)

    @app_commands.command(name="unwatch", description="Stop watching an item by its list number")
    @app_commands.describe(number="Item number from /list")
    async def slash_unwatch(self, interaction: discord.Interaction, number: int):
        await interaction.response.defer(ephemeral=True)
        result = self.bot.monitor.unwatch_user_product(number, interaction.user.id)
        await interaction.followup.send(result, ephemeral=True)

    @app_commands.command(name="list", description="Show the products you are currently watching")
    async def slash_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embeds = self.bot.monitor.build_user_watchlist_embeds(interaction.user.id)
        await interaction.followup.send(embed=embeds[0], ephemeral=True)
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="status", description="Show bot health and current stock summary")
    async def slash_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        monitor_running = bool(self.bot.monitor_task and not self.bot.monitor_task.done())
        uptime_sec = int(time.monotonic() - self.bot._start_time)
        hours, rem = divmod(uptime_sec, 3600)
        minutes = rem // 60
        watching = len(self.bot.monitor.watchlist)

        counts: dict[str, int] = {}
        for p in self.bot.monitor.watchlist:
            s = p.get("last_status", "unknown")
            counts[s] = counts.get(s, 0) + 1

        emoji_map = {"in_stock": "🟢", "low_stock": "🟡", "out_of_stock": "🔴", "unknown": "⚪"}
        embed = discord.Embed(title="📡 Bot Status", color=0x57F287 if monitor_running else 0xED4245)
        embed.add_field(name="Monitor", value="🟢 Running" if monitor_running else "🔴 Stopped", inline=True)
        embed.add_field(name="Uptime", value=f"{hours}h {minutes}m", inline=True)
        embed.add_field(name="Watching", value=f"{watching} product(s)", inline=True)
        if counts:
            lines = [f"{emoji_map.get(s, '⚪')} {s.replace('_', ' ').title()}: {n}" for s, n in sorted(counts.items())]
            embed.add_field(name="Stock Summary", value="\n".join(lines), inline=False)
        await interaction.followup.send(embed=embed)


bot = StockBot()


def _checkout_authorized(ctx) -> bool:
    allowed = CONFIG.get("checkout", {}).get("allowed_approvers") or []
    return bool(allowed) and str(ctx.author.id) in allowed


async def _send_chunks(ctx, message: str, limit: int = 1900):
    if len(message) <= limit:
        await ctx.send(message)
        return

    for start in range(0, len(message), limit):
        await ctx.send(message[start:start + limit])


_bot_logger = logging.getLogger(__name__)


@bot.event
async def on_ready():
    _bot_logger.info(f"Logged in as {bot.user} ({bot.user.id})")
    _bot_logger.info(f"Monitoring channel: {CONFIG['discord']['channel_id']}")
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    print(f"📡 Monitoring channel: {CONFIG['discord']['channel_id']}")

    # Restart the monitor task if it died (e.g. unhandled exception before a reconnect)
    if bot.monitor_task is None or bot.monitor_task.done():
        exc = bot.monitor_task.exception() if bot.monitor_task and not bot.monitor_task.cancelled() else None
        if exc:
            _bot_logger.error(f"Monitor task had died with exception: {exc!r} — restarting.")
        else:
            _bot_logger.warning("Monitor task was not running on on_ready — restarting.")
        bot.monitor_task = asyncio.create_task(
            bot.monitor.run_loop(), name="stock-monitor"
        )


@bot.command(name="watch")
async def watch(ctx, url: str):
    """Add a product URL to the watchlist. Usage: !watch <url>"""
    result = bot.monitor.add_product(url, ctx.author.id)
    await ctx.send(result)


@bot.command(name="whoami")
async def whoami(ctx):
    """Show your Discord user ID for checkout approval config."""
    await ctx.send(f"Your Discord user ID: `{ctx.author.id}`")


@bot.command(name="subscribe")
async def subscribe(ctx):
    """Subscribe yourself to stock alert mentions."""
    await ctx.send(bot.monitor.subscribe_user(ctx.author.id))


@bot.command(name="unsubscribe")
async def unsubscribe(ctx):
    """Unsubscribe yourself from stock alert mentions."""
    await ctx.send(bot.monitor.unsubscribe_user(ctx.author.id))


@bot.command(name="notify")
async def notify(ctx, index: int):
    """Get mentioned when a specific watch entry changes stock. Usage: !notify <index>"""
    await ctx.send(bot.monitor.notify_user(index, ctx.author.id))


@bot.command(name="unnotify")
async def unnotify(ctx, index: int):
    """Stop being mentioned for a specific watch entry. Usage: !unnotify <index>"""
    await ctx.send(bot.monitor.unnotify_user(index, ctx.author.id))


@bot.command(name="subscribers")
async def subscribers(ctx):
    """Show current stock alert subscribers."""
    await ctx.send(bot.monitor.build_subscribers_message())


@bot.command(name="unwatch")
async def unwatch(ctx, target: str):
    """Remove a watch by list index or exact URL. Usage: !unwatch <index|url>"""
    if target.isdigit():
        result = bot.monitor.unwatch_user_product(int(target), ctx.author.id)
    else:
        result = bot.monitor.remove_product(target)
    await ctx.send(result)


@bot.command(name="unwatch_pack")
async def unwatch_pack(ctx, pack_id: str):
    """Remove all watch entries for a product pack. Usage: !unwatch_pack <pack_id>"""
    await ctx.send(bot.monitor.remove_products_by_pack(pack_id))


@bot.command(name="remove_sku")
async def remove_sku(ctx, sku: str):
    """Remove all watch entries for a SKU. Usage: !remove_sku <sku>"""
    await ctx.send(bot.monitor.remove_products_by_sku(sku))


@bot.command(name="clear_watches")
async def clear_watches(ctx, confirm: str | None = None):
    """Clear the watchlist. Usage: !clear_watches confirm"""
    if confirm != "confirm":
        await ctx.send("⚠️ Confirm full watchlist removal with: `!clear_watches confirm`")
        return

    await ctx.send(bot.monitor.clear_watchlist())


@bot.command(name="list")
async def list_products(ctx):
    """List your watched products."""
    for embed in bot.monitor.build_user_watchlist_embeds(ctx.author.id):
        await ctx.send(embed=embed)


@bot.command(name="packs")
async def list_packs(ctx):
    """List available product packs."""
    packs = bot.monitor.list_product_packs()
    if not packs:
        await ctx.send("📭 No product packs found.")
        return

    embed = discord.Embed(title="Product Packs", color=0x5865F2)
    for pack in packs:
        embed.add_field(
            name=f"{pack['id']} — {pack['name']}",
            value=(
                f"{pack.get('description') or 'No description'}\n"
                f"Products: `{pack['product_count']}` · Watch entries: `{pack['watch_entry_count']}`"
            ),
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command(name="watch_pack")
async def watch_pack(ctx, pack_id: str):
    """Add a product pack to the watchlist. Usage: !watch_pack <pack_id>"""
    result = bot.monitor.add_product_pack(pack_id)
    await ctx.send(result)


@bot.command(name="report")
async def report(ctx):
    """Show current stock watch summary."""
    await ctx.send(embed=bot.monitor.build_report_embed())


@bot.command(name="check")
async def force_check(ctx):
    """Force an immediate check of all products."""
    await ctx.send("🔄 Running manual check on all products...")
    await bot.monitor.check_all(force=True)
    await ctx.send("✅ Manual check complete.")


@bot.command(name="cleanup_now")
async def cleanup_now(ctx):
    """Delete old bot messages immediately using message cleanup settings."""
    deleted = await bot.cleanup_channel_messages()
    await ctx.send(f"🧹 Cleanup deleted `{deleted}` old messages.")


@bot.command(name="checkout_config")
async def checkout_config(ctx, index: int, enabled: str, quantity: int | None = None, max_quantity: int | None = None, max_unit_price: float | None = None, max_order_total: float | None = None):
    """Configure checkout for a watch. Usage: !checkout_config <index> <on|off> [qty] [max_qty] [max_unit] [max_order]"""
    if not _checkout_authorized(ctx):
        await ctx.send("⚠️ You are not allowed to configure checkout.")
        return

    enabled_value = enabled.strip().lower()
    if enabled_value not in {"on", "off", "true", "false", "yes", "no"}:
        await ctx.send("⚠️ Enabled must be `on` or `off`.")
        return

    result = bot.monitor.configure_checkout(
        index=index,
        enabled=enabled_value in {"on", "true", "yes"},
        quantity=quantity,
        max_quantity=max_quantity,
        max_unit_price=max_unit_price,
        max_order_total=max_order_total,
    )
    await ctx.send(result)


@bot.command(name="checkout")
async def checkout(ctx, index: int):
    """Run checkout for a watch by index. Usage: !checkout <index>"""
    if not _checkout_authorized(ctx):
        await ctx.send("⚠️ You are not allowed to run checkout.")
        return

    await ctx.send(f"🔄 Running checkout for watch `{index}`...")
    result = await bot.monitor.checkout_product_by_index(index, force=True)
    await ctx.send(result)


@bot.command(name="checkout_test")
async def checkout_test(ctx, index: int, depth: str = "page"):
    """Run a checkout readiness test. Usage: !checkout_test <index> [page|cart]"""
    if not _checkout_authorized(ctx):
        await ctx.send("⚠️ You are not allowed to test checkout.")
        return

    depth = depth.strip().lower()
    if depth not in {"page", "cart"}:
        await ctx.send("⚠️ Checkout test depth must be `page` or `cart`.")
        return

    action = "cart-depth" if depth == "cart" else "no-click"
    timeout = max(15, int(CONFIG.get("checkout", {}).get("test_timeout_seconds", 90)))
    await ctx.send(f"🔎 Running {action} checkout test for watch `{index}` (timeout `{timeout}s`)...")
    result = await bot.monitor.test_checkout_by_index(index, depth)
    await _send_chunks(ctx, result)


@bot.command(name="test_alert")
async def test_alert(ctx, status: str = "in_stock"):
    """Send a simulated stock alert after 15 seconds. Usage: !test_alert [in_stock|low_stock|out_of_stock]"""
    status = status.lower()
    valid_statuses = {"in_stock", "low_stock", "out_of_stock"}
    if status not in valid_statuses:
        await ctx.send("⚠️ Status must be one of: `in_stock`, `low_stock`, `out_of_stock`.")
        return

    embed = bot.monitor.build_test_embed(status)
    message = bot.monitor._build_alert_message(
        bot.monitor.watchlist[0] if bot.monitor.watchlist else CONFIG["default_products"][0],
        {
            "name": embed.description.replace("**", "") if embed.description else "Test Product",
        },
        status,
    )
    await asyncio.sleep(15)
    await ctx.send(content=message, embed=embed)


@bot.command(name="help_stock")
async def help_stock(ctx):
    """Show help for the stock bot."""
    embed = discord.Embed(title="📦 Stock Alert Bot", color=0x5865F2)
    embed.add_field(
        name="Slash Commands",
        value=(
            "`/watch <url>` — Start watching a product\n"
            "`/unwatch <number>` — Stop watching (number from `/list`)\n"
            "`/list` — Your watched items\n"
            "`/status` — Bot health and stock summary"
        ),
        inline=False,
    )
    embed.add_field(
        name="Supported Sites",
        value="ui.com · amazon.com · bestbuy.com · bhphotovideo.com · newegg.com",
        inline=False,
    )
    await ctx.send(embed=embed)


if __name__ == "__main__":
    if CONFIG["discord"]["token"] == TOKEN_PLACEHOLDER:
        raise SystemExit("Set DISCORD_BOT_TOKEN or update config.py before starting the bot.")

    bot.run(CONFIG["discord"]["token"])
