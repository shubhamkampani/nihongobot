import discord
from discord.ext import commands
import os
from threading import Thread
from flask import Flask

# --- WEB SERVER TO KEEP BOT AWAKE ---
app = Flask('')
@app.route('/')
def home():
    return "AI Sensei is Live 24/7!"
def run():
    app.run(host='0.0.0.0', port=8000)
def keep_alive():
    t = Thread(target=run)
    t.start()
# ------------------------------------

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def setup_server(ctx):
    await ctx.send("⏳ Server setup started! Please wait...")
    
    # 1. Global Category & Channels
    general_cat = await ctx.guild.create_category("💬 COMMUNITY & TOOLS")
    await ctx.guild.create_text_channel("💬・general-lounge", category=general_cat)
    await ctx.guild.create_text_channel("🤖・bot-commands", category=general_cat)
    await ctx.guild.create_text_channel("⛩️・ask-ai-sensei", category=general_cat)

    # 2. Level Specific Roles & Categories (N5 to N1)
    levels = ["N5 Beginner", "N4 Elementary", "N3 Intermediate", "N2 Pre-Advanced", "N1 Advanced"]
    
    for level in levels:
        # Create Role
        role = await ctx.guild.create_role(name=f"📍 {level}", hoist=True)
        
        # Set Permissions (Only this role and Bot can see the category)
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        
        # Create Category and Channels
        cat = await ctx.guild.create_category(f"🎌 {level.upper()}", overwrites=overwrites)
        await ctx.guild.create_text_channel(f"💬・{level[:2].lower()}-chat", category=cat)
        await ctx.guild.create_text_channel(f"📝・{level[:2].lower()}-doubts", category=cat)

    await ctx.send("✅ Setup Complete! All roles, categories, and channels are ready.")

# Start the web server in background
keep_alive()

# Run the bot using Environment Variable
bot.run(os.environ.get("BOT_TOKEN"))
