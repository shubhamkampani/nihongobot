import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import View, Button, Select
import os
import json
import random
from threading import Thread
from flask import Flask
import time
from datetime import datetime
import pytz
from pymongo import MongoClient
import google.generativeai as genai

# --- 🌐 WEB SERVER ---
app = Flask('')
@app.route('/')
def home():
    return "Nihongo Bot is Live 24/7!"
def run():
    app.run(host='0.0.0.0', port=8000)
Thread(target=run).start()

# --- 🗄️ DATABASE & AI ---
MONGO_URI = os.environ.get("MONGO_URI")
try:
    cluster = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = cluster["nihongo_db"]
    quiz_db = db["weekly_scores"]
    print("✅ MongoDB Connected!")
except Exception as e:
    print(f"❌ MongoDB Error: {e}")

genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-1.5-flash')

# --- 🌸 10 UNIQUE ROMAJI GREETINGS ---
GREETINGS_LIST = [
    "Irasshaimase! 🌸", 
    "Konnichiwa! ✨", 
    "Yokoso! 🎌", 
    "Hajimemashite! ⛩️",
    "Okaerinasai! 🏡", 
    "Ohayou gozaimasu! 🌅", 
    "Konbanwa! 🌙", 
    "Yoroshiku onegaishimasu! 🤝",
    "Kangei shimasu! 🎊",
    "Nihongo no sekai e yōkoso! 🗾"
]

# --- 🎭 ROLE SELECTION UI ---
class JLPTSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="N5 Beginner", emoji="📍"),
            discord.SelectOption(label="N4 Elementary", emoji="📍"),
            discord.SelectOption(label="N3 Intermediate", emoji="📍"),
            discord.SelectOption(label="N2 Pre-Advanced", emoji="📍"),
            discord.SelectOption(label="N1 Advanced", emoji="📍")
        ]
        # Yahan maine custom_id="jlpt_dropdown" add kar diya hai
        super().__init__(placeholder="Select your target JLPT Level...", min_values=1, max_values=1, options=options, custom_id="jlpt_dropdown")


    async def callback(self, interaction: discord.Interaction):
        selected_level = self.values[0]
        role_name = f"📍 {selected_level}"
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        
        if role:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(f"✅ You are now an {selected_level}! Use `/quiz` in the commands channel to test your skills.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Role not found in server.", ephemeral=True)

class JLPTView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(JLPTSelect())

class WelcomeView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📸 Visitor", style=discord.ButtonStyle.secondary, custom_id="role_visitor")
    async def visitor_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.guild.roles, name="Visitor") 
        if role:
            await interaction.user.add_roles(role)
            await interaction.response.send_message("✅ You are now a Visitor! Enjoy exploring.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Visitor role not found. Ask Admin to create it.", ephemeral=True)

    @discord.ui.button(label="🎌 JP Learner", style=discord.ButtonStyle.primary, custom_id="role_learner")
    async def learner_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Awesome! Please select your target JLPT level below:", view=JLPTView(), ephemeral=True)

# --- 🤖 MAIN BOT CLASS ---
class NihongoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True 
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)

    async def setup_hook(self):
        self.weekly_leaderboard_loop.start()
        self.add_view(WelcomeView())
        self.add_view(JLPTView())
        await self.tree.sync()
        print("✅ Commands Synced & Tasks Started.")

    async def on_member_join(self, member: discord.Member):
        # specifically targets your custom channel name
        welcome_ch = discord.utils.find(lambda c: "welcome" in c.name.lower(), member.guild.channels)
        if welcome_ch:
            greeting = random.choice(GREETINGS_LIST)
            text = f"{greeting} Welcome to the community, {member.mention}!"
            
            embed = discord.Embed(
                title="🌸 Choose Your Path",
                description="To get started, please select your role below:\n\n📸 **Visitor:** I'm just exploring.\n🎌 **JP Learner:** I am studying Japanese.",
                color=0xffb6c1
            )
            try:
                await welcome_ch.send(content=text, embed=embed, view=WelcomeView())
            except Exception as e:
                print(f"Welcome Msg Error: {e}")

    @tasks.loop(minutes=1)
    async def weekly_leaderboard_loop(self):
        jst = pytz.timezone('Asia/Tokyo')
        now_jst = datetime.now(jst)
        if now_jst.weekday() == 6 and now_jst.hour == 22 and now_jst.minute == 0:
            if not getattr(self, 'weekly_reset_done', False):
                self.weekly_reset_done = True
                quiz_db.delete_many({})
        elif now_jst.weekday() == 6 and now_jst.hour == 22 and now_jst.minute == 1:
            self.weekly_reset_done = False

bot = NihongoBot()

# --- 🎮 QUIZ UI ENGINE ---
class QuizView(View):
    def __init__(self, user, questions, level, start_time):
        super().__init__(timeout=60) 
        self.user, self.questions, self.level, self.current_idx, self.score, self.start_time = user, questions, level, 0, 0, start_time
        
        for label in ["A", "B", "C", "D"]:
            btn = Button(label=label, custom_id=label, style=discord.ButtonStyle.primary)
            btn.callback = self.button_callback
            self.add_item(btn)

    async def button_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("❌ Not your quiz!", ephemeral=True)
            
        if interaction.data['custom_id'] == self.questions[self.current_idx]['answer']:
            self.score += 1
            
        self.current_idx += 1
        if self.current_idx < 20:
            q = self.questions[self.current_idx]
            embed = discord.Embed(title=f"🎌 {self.level} Mock Test ({self.current_idx + 1}/20)", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x3498db)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            self.stop()
            time_taken = round(time.time() - self.start_time)
            quiz_db.update_one({"_id": self.user.id}, {"$set": {"level": self.level, "score": self.score, "time_taken": time_taken}}, upsert=True)
            await interaction.response.edit_message(embed=discord.Embed(title="🏁 Quiz Completed!", description=f"Score: **{self.score}/20** in {time_taken}s.", color=discord.Color.green()), view=None)

    async def on_timeout(self):
        try: await self.message.edit(content="⏳ **Time's up!** Attempt cancelled to prevent spam.", view=None, embed=None)
        except: pass

# --- ⌨️ COMMANDS ---
@bot.tree.command(name="help", description="Shows bot commands.")
async def help_command(interaction: discord.Interaction):
    if "bot-commands" not in interaction.channel.name.lower():
        return await interaction.response.send_message("❌ Use `#🤖・bot-commands`.", ephemeral=True)
    await interaction.response.send_message(embed=discord.Embed(title="🤖 Commands", description="🧠 `/quiz` - JLPT Mock Test", color=0xffb6c1), ephemeral=True)

@bot.tree.command(name="quiz", description="Take a 20-question JLPT mock test.")
async def quiz(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_level = next((r.name.replace("📍 ", "").strip() for r in interaction.user.roles if r.name in ["📍 N5", "📍 N4", "📍 N3", "📍 N2", "📍 N1"]), None)
            
    if not user_level:
        return await interaction.followup.send("❌ Get an N5-N1 role first.", ephemeral=True)
        
    await interaction.followup.send(f"⏳ Generating 20 {user_level} questions...", ephemeral=True)
    prompt = f"""Generate 20 multiple-choice questions for JLPT {user_level} (10 Grammar, 10 Vocab). Difficulty: Low to Medium. Output ONLY a valid JSON array format: [{{"question": "...", "options": {{"A": "a", "B": "b", "C": "c", "D": "d"}}, "answer": "B"}}]"""
    
    try:
        raw_text = model.generate_content(prompt).text.strip().replace("```json", "").replace("```", "")
        questions_data = json.loads(raw_text)
        
        q = questions_data[0]
        embed = discord.Embed(title=f"🎌 {user_level} Mock Test (1/20)", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x3498db)
        view = QuizView(interaction.user, questions_data, user_level, time.time())
        msg = await interaction.edit_original_response(content="", embed=embed, view=view)
        view.message = msg 
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ AI Error. Please try again.")

bot.run(os.environ.get("BOT_TOKEN"))
