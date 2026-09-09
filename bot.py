import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import View, Button
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

# --- 🌐 WEB SERVER (Keep-Alive) ---
app = Flask('')
@app.route('/')
def home():
    return "Nihongo Bot is Live 24/7!"
def run():
    app.run(host='0.0.0.0', port=8000)
Thread(target=run).start()

# --- 🗄️ DATABASE SETUP ---
MONGO_URI = os.environ.get("MONGO_URI")
try:
    cluster = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = cluster["nihongo_db"]
    quiz_db = db["weekly_scores"]
    print("✅ MongoDB Connected Successfully!")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")

# --- 🧠 GEMINI AI SETUP ---
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- 🌸 GREETINGS LIST ---
WELCOME_MESSAGES = [
    "Irasshaimase! Welcome to the community, {user}! 🌸",
    "Konnichiwa {user}! Yoroshiku onegaishimasu! ✨",
    "Yokoso {user}! We are excited to study Japanese with you. 🎌",
    "Hajimemashite {user}! Let's conquer the JLPT together! ⛩️"
]

class NihongoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True # Zaroori hai greetings ke liye
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)
        self.weekly_reset_done = False

    async def setup_hook(self):
        self.weekly_leaderboard_loop.start()
        await self.tree.sync()
        print("✅ Commands Synced & Tasks Started.")

    async def on_member_join(self, member: discord.Member):
        welcome_ch = discord.utils.find(lambda c: "welcome" in c.name.lower(), member.guild.channels)
        if welcome_ch:
            text = random.choice(WELCOME_MESSAGES).replace("{user}", member.mention)
            try: await welcome_ch.send(text)
            except Exception: pass

    # --- 🏆 WEEKLY LEADERBOARD TASK ---
    @tasks.loop(minutes=1)
    async def weekly_leaderboard_loop(self):
        jst = pytz.timezone('Asia/Tokyo')
        now_jst = datetime.now(jst)
        
        # Check if Sunday (6) and 10 PM (22:00)
        if now_jst.weekday() == 6 and now_jst.hour == 22 and now_jst.minute == 0:
            if not self.weekly_reset_done:
                self.weekly_reset_done = True
                
                for guild in self.guilds:
                    announce_ch = discord.utils.find(lambda c: "announcements" in c.name.lower(), guild.channels)
                    champ_role = discord.utils.get(guild.roles, name="Weekly Champion")
                    
                    if not announce_ch or not champ_role: continue
                    
                    # Remove old champions
                    for member in champ_role.members:
                        try: await member.remove_roles(champ_role)
                        except: pass
                        
                    # Find new toppers per level (N5 to N1)
                    levels = ["N5", "N4", "N3", "N2", "N1"]
                    embed = discord.Embed(title="🏆 THIS WEEK'S JLPT CHAMPIONS!", color=discord.Color.gold())
                    
                    for lvl in levels:
                        top_user = quiz_db.find_one({"level": lvl}, sort=[("score", -1), ("time_taken", 1)])
                        if top_user:
                            try:
                                member = await guild.fetch_member(top_user["_id"])
                                await member.add_roles(champ_role)
                                embed.add_field(name=f"🎌 {lvl} Champion", value=f"{member.mention} (Score: {top_user['score']}/20)", inline=False)
                            except: pass
                            
                    await announce_ch.send(content="<@&Weekly Champion> roles have been updated!", embed=embed)
                
                # Reset Database for the new week
                quiz_db.delete_many({})
                print("🔄 Weekly Leaderboard Reset Complete.")
                
        elif now_jst.weekday() == 6 and now_jst.hour == 22 and now_jst.minute == 1:
            self.weekly_reset_done = False # Reset the lock

bot = NihongoBot()

# --- 🎮 QUIZ UI ENGINE ---
class QuizView(View):
    def __init__(self, user, questions, level, start_time):
        super().__init__(timeout=60) # 1 Minute timer per question
        self.user = user
        self.questions = questions
        self.level = level
        self.current_idx = 0
        self.score = 0
        self.start_time = start_time
        
        self.btn_a = Button(label="A", custom_id="A", style=discord.ButtonStyle.primary)
        self.btn_b = Button(label="B", custom_id="B", style=discord.ButtonStyle.primary)
        self.btn_c = Button(label="C", custom_id="C", style=discord.ButtonStyle.primary)
        self.btn_d = Button(label="D", custom_id="D", style=discord.ButtonStyle.primary)
        
        for btn in [self.btn_a, self.btn_b, self.btn_c, self.btn_d]:
            btn.callback = self.button_callback
            self.add_item(btn)

    async def button_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("❌ This is not your quiz!", ephemeral=True)
            
        selected_answer = interaction.data['custom_id']
        correct_answer = self.questions[self.current_idx]['answer']
        
        if selected_answer == correct_answer:
            self.score += 1
            
        self.current_idx += 1
        
        if self.current_idx < 20:
            # Load next question
            q = self.questions[self.current_idx]
            embed = discord.Embed(title=f"🎌 JLPT {self.level} Mock Test ({self.current_idx + 1}/20)", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x3498db)
            embed.set_footer(text="⏳ You have 1 minute for this question.")
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            # Quiz Finished - Save to DB
            self.stop()
            time_taken = round(time.time() - self.start_time)
            
            # Save logic (Only update if score is higher)
            existing = quiz_db.find_one({"_id": self.user.id})
            if not existing or self.score > existing.get("score", 0):
                quiz_db.update_one({"_id": self.user.id}, {"$set": {"level": self.level, "score": self.score, "time_taken": time_taken}}, upsert=True)
            
            finish_embed = discord.Embed(title="🏁 Quiz Completed!", description=f"You scored **{self.score}/20** in {time_taken} seconds.\n\nYour score has been recorded for the Sunday Leaderboard!", color=discord.Color.green())
            await interaction.response.edit_message(embed=finish_embed, view=None)

    async def on_timeout(self):
        # Anti-Spam: Timeout par data save nahi hoga
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(content="⏳ **Time's up!** You took more than 1 minute. This attempt has been cancelled to prevent spam.", view=self, embed=None)
        except: pass

# --- ⌨️ COMMANDS ---

@bot.tree.command(name="help", description="Shows all bot commands.")
async def help_command(interaction: discord.Interaction):
    if "bot-commands" not in interaction.channel.name.lower():
        return await interaction.response.send_message("❌ Please use this command in the `#🤖・bot-commands` channel.", ephemeral=True)
        
    embed = discord.Embed(title="🤖 Nihongo Bot Commands", color=0xffb6c1)
    embed.add_field(name="🧠 `/quiz`", value="Start a 20-question AI JLPT Mock Test.", inline=False)
    # Future commands like /jisho will go here
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="quiz", description="Take a 20-question JLPT mock test.")
async def quiz(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True) # Bypass 3-second limit
    
    # Check User Level
    user_level = None
    for role in interaction.user.roles:
        if role.name in ["📍 N5", "📍 N4", "📍 N3", "📍 N2", "📍 N1"]: # Update names as per your server
            user_level = role.name.replace("📍 ", "").strip()
            break
            
    if not user_level:
        return await interaction.followup.send("❌ You need an N5 to N1 role to take a quiz. Please select your level first.", ephemeral=True)
        
    await interaction.followup.send(f"⏳ Gemini AI is generating 20 {user_level} questions for you. Please wait ~5 seconds...", ephemeral=True)
    
    # Prompt Gemini AI
    prompt = f"""
    Generate exactly 20 multiple-choice questions for the Japanese Language Proficiency Test (JLPT) level {user_level}.
    10 questions must be Grammar, 10 must be Vocabulary. Difficulty: Low to Medium.
    Provide exactly 4 options (A, B, C, D) for each.
    Output strictly in this JSON array format (no markdown, no extra text):
    [
      {{"question": "日本語で___", "options": {{"A": "a", "B": "b", "C": "c", "D": "d"}}, "answer": "B"}}
    ]
    """
    
    try:
        response = model.generate_content(prompt)
        raw_text = response.text.strip().replace("```json", "").replace("```", "")
        questions_data = json.loads(raw_text)
        
        if len(questions_data) < 20:
            return await interaction.edit_original_response(content="❌ AI failed to generate all 20 questions. Please try again.")
            
        # Start Quiz
        start_time = time.time()
        q = questions_data[0]
        embed = discord.Embed(title=f"🎌 JLPT {user_level} Mock Test (1/20)", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x3498db)
        embed.set_footer(text="⏳ You have 1 minute for this question.")
        
        view = QuizView(interaction.user, questions_data, user_level, start_time)
        msg = await interaction.edit_original_response(content="", embed=embed, view=view)
        view.message = msg # Save message reference for timeout editing
        
    except Exception as e:
        await interaction.edit_original_response(content=f"❌ Error communicating with AI or parsing data. Please try again.")
        print(f"Quiz Error: {e}")

bot.run(os.environ.get("BOT_TOKEN"))
