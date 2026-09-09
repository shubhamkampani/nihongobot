import discord
from discord.ext import commands
from discord.ui import View, Button, Select
import os
from threading import Thread
from flask import Flask

# --- WEB SERVER (To keep bot awake) ---
app = Flask('')
@app.route('/')
def home():
    return "Nihongo Bot is Live 24/7!"
def run():
    app.run(host='0.0.0.0', port=8000)
def keep_alive():
    t = Thread(target=run)
    t.start()

# --- THE JLPT DROPDOWN MENU ---
class JLPTSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="N5 Beginner", emoji="📍"),
            discord.SelectOption(label="N4 Elementary", emoji="📍"),
            discord.SelectOption(label="N3 Intermediate", emoji="📍"),
            discord.SelectOption(label="N2 Pre-Advanced", emoji="📍"),
            discord.SelectOption(label="N1 Advanced", emoji="📍")
        ]
        super().__init__(placeholder="Select your target JLPT Level...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_level = self.values[0]
        # (Agla step: Yahan humhara random quiz wala logic aayega!)
        await interaction.response.send_message(f"⏳ Loading random quiz for {selected_level}...", ephemeral=True)

class JLPTView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(JLPTSelect())

# --- THE WELCOME BUTTONS ---
class WelcomeView(View):
    def __init__(self):
        super().__init__(timeout=None) # Timeout None zaroori hai taaki buttons hamesha kaam karein

    @discord.ui.button(label="📸 Visitor", style=discord.ButtonStyle.secondary, custom_id="role_visitor")
    async def visitor_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # NOTE: Agar apne Visitor role ka naam kuch aur rakha hai, toh niche update kar lena
        role = discord.utils.get(interaction.guild.roles, name="Visitor") 
        if role:
            await interaction.user.add_roles(role)
            await interaction.response.send_message("✅ You've been given the Visitor role! Enjoy exploring.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Visitor role not found in server.", ephemeral=True)

    @discord.ui.button(label="🎌 JP Learner", style=discord.ButtonStyle.primary, custom_id="role_learner")
    async def learner_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = JLPTView()
        await interaction.response.send_message("Awesome! Please select your target JLPT level below:", view=view, ephemeral=True)

# --- BOT SETUP ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    bot.add_view(WelcomeView()) # Bot restart hone pe buttons zinda rahenge

@bot.command()
@commands.has_permissions(administrator=True)
async def send_welcome(ctx):
    embed = discord.Embed(
        title="🌸 Welcome to the Japanese Learning Community!",
        description="**Hajimemashite!** To get started, please select your path below:\n\n"
                    "📸 **Visitor:** I'm just here to explore the community & culture.\n"
                    "🎌 **JP Learner:** I am actively studying Japanese and want access to study materials.",
        color=0xffb6c1
    )
    await ctx.send(embed=embed, view=WelcomeView())

keep_alive()
bot.run(os.environ.get("BOT_TOKEN"))
