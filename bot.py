"""
Stock Alert Discord Bot - Main Entry Point
"""
import asyncio
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

    async def setup_hook(self):
        self.monitor.load_watchlist()
        if self.monitor_task is None or self.monitor_task.done():
            self.monitor_task = asyncio.create_task(self.monitor.run_loop(), name="stock-monitor")

    async def close(self):
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass

        await super().close()


bot = StockBot()


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    print(f"📡 Monitoring channel: {CONFIG['discord']['channel_id']}")


@bot.command(name="watch")
async def watch(ctx, url: str):
    """Add a product URL to the watchlist. Usage: !watch <url>"""
    result = bot.monitor.add_product(url)
    await ctx.send(result)


@bot.command(name="unwatch")
async def unwatch(ctx, url: str):
    """Remove a product URL from the watchlist. Usage: !unwatch <url>"""
    result = bot.monitor.remove_product(url)
    await ctx.send(result)


@bot.command(name="list")
async def list_products(ctx):
    """List all currently monitored products."""
    products = bot.monitor.get_watchlist()
    if not products:
        await ctx.send("📭 No products currently being monitored.")
        return

    lines = ["**📋 Currently Monitoring:**\n"]
    for i, p in enumerate(products, 1):
        status = p.get("last_status", "unknown")
        emoji = {"in_stock": "🟢", "out_of_stock": "🔴", "low_stock": "🟡", "unknown": "⚪"}.get(status, "⚪")
        label = p.get("sku") or p["name"]
        vendor = p.get("vendor") or p["site"]
        pack = f" · Pack: `{p['pack']}`" if p.get("pack") else ""
        lines.append(
            f"`{i}.` {emoji} **{label}**\n"
            f"    {p['url']}\n"
            f"    Site: `{p['site']}` · Vendor: `{vendor}`{pack}\n"
        )

    await ctx.send("\n".join(lines))


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
    embed.add_field(name="!unwatch <url>", value="Remove a product URL", inline=False)
    embed.add_field(name="!list", value="Show all monitored products", inline=False)
    embed.add_field(name="!packs", value="Show available product packs", inline=False)
    embed.add_field(name="!watch_pack <pack_id>", value="Add a product pack to monitor", inline=False)
    embed.add_field(name="!report", value="Show current stock summary", inline=False)
    embed.add_field(name="!check", value="Force an immediate check now", inline=False)
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
