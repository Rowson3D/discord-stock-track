"""
Stock Alert Discord Bot - Main Entry Point
"""
import asyncio
from datetime import timedelta

import discord
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

    async def setup_hook(self):
        self.monitor.load_watchlist()
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


bot = StockBot()


def _checkout_authorized(ctx) -> bool:
    allowed = CONFIG.get("checkout", {}).get("allowed_approvers") or []
    return bool(allowed) and str(ctx.author.id) in allowed


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    print(f"📡 Monitoring channel: {CONFIG['discord']['channel_id']}")


@bot.command(name="watch")
async def watch(ctx, url: str):
    """Add a product URL to the watchlist. Usage: !watch <url>"""
    result = bot.monitor.add_product(url)
    await ctx.send(result)


@bot.command(name="whoami")
async def whoami(ctx):
    """Show your Discord user ID for checkout approval config."""
    await ctx.send(f"Your Discord user ID: `{ctx.author.id}`")


@bot.command(name="unwatch")
async def unwatch(ctx, target: str):
    """Remove a watch by list index or exact URL. Usage: !unwatch <index|url>"""
    result = bot.monitor.remove_product_by_index(int(target)) if target.isdigit() else bot.monitor.remove_product(target)
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
    """List all currently monitored products."""
    for embed in bot.monitor.build_watchlist_embeds():
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
async def checkout_test(ctx, index: int):
    """Run a no-charge checkout readiness test. Usage: !checkout_test <index>"""
    if not _checkout_authorized(ctx):
        await ctx.send("⚠️ You are not allowed to test checkout.")
        return

    await ctx.send(f"🔎 Running no-charge checkout test for watch `{index}`...")
    result = await bot.monitor.test_checkout_by_index(index)
    await ctx.send(result)


@bot.command(name="test_alert")
async def test_alert(ctx, status: str = "in_stock"):
    """Send a simulated stock alert after 15 seconds. Usage: !test_alert [in_stock|low_stock|out_of_stock]"""
    status = status.lower()
    valid_statuses = {"in_stock", "low_stock", "out_of_stock"}
    if status not in valid_statuses:
        await ctx.send("⚠️ Status must be one of: `in_stock`, `low_stock`, `out_of_stock`.")
        return

    embed = bot.monitor.build_test_embed(status)
    await asyncio.sleep(15)
    await ctx.send(embed=embed)


@bot.command(name="help_stock")
async def help_stock(ctx):
    """Show help for the stock bot."""
    embed = discord.Embed(
        title="📦 Stock Alert Bot — Commands",
        color=0x5865F2
    )
    embed.add_field(name="!watch <url>", value="Add a product URL to monitor", inline=False)
    embed.add_field(name="!whoami", value="Show your Discord user ID for checkout approval config", inline=False)
    embed.add_field(name="!unwatch <index|url>", value="Remove a watch by list number or exact URL", inline=False)
    embed.add_field(name="!unwatch_pack <pack_id>", value="Remove all entries for a product pack", inline=False)
    embed.add_field(name="!remove_sku <sku>", value="Remove all entries for a SKU", inline=False)
    embed.add_field(name="!clear_watches confirm", value="Clear the entire watchlist", inline=False)
    embed.add_field(name="!list", value="Show watchlist in paged embeds", inline=False)
    embed.add_field(name="!packs", value="Show available product packs", inline=False)
    embed.add_field(name="!watch_pack <pack_id>", value="Add a product pack to monitor", inline=False)
    embed.add_field(name="!report", value="Show current stock summary", inline=False)
    embed.add_field(name="!check", value="Force an immediate check now", inline=False)
    embed.add_field(name="!cleanup_now", value="Delete old bot messages using cleanup settings", inline=False)
    embed.add_field(name="!checkout_config <index> <on|off> [qty] [max_qty] [max_unit] [max_order]", value="Configure guarded checkout for a watch", inline=False)
    embed.add_field(name="!checkout_test <index>", value="No-charge checkout readiness test", inline=False)
    embed.add_field(name="!checkout <index>", value="Run guarded checkout now for a watch", inline=False)
    embed.add_field(name="!test_alert [status]", value="Send a simulated alert embed after 15 seconds", inline=False)
    embed.add_field(
        name="Supported Sites",
        value="• ui.com (Ubiquiti)\n• amazon.com\n• bhphotovideo.com\n• newegg.com",
        inline=False
    )
    await ctx.send(embed=embed)


if __name__ == "__main__":
    if CONFIG["discord"]["token"] == TOKEN_PLACEHOLDER:
        raise SystemExit("Set DISCORD_BOT_TOKEN or update config.py before starting the bot.")

    bot.run(CONFIG["discord"]["token"])
