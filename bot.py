import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import View, Button, Select
import os
import json
import re
import random
from threading import Thread
from flask import Flask
import time
from datetime import datetime
import pytz
from pymongo import MongoClient
import aiohttp
import urllib.parse
import ast

# --- 🌐 WEB SERVER ---
app = Flask('')
@app.route('/')
def home(): return "Nihongo Bot is Live 24/7!"
def run(): app.run(host='0.0.0.0', port=8000)
Thread(target=run).start()

# --- 🗄️ DATABASE SETUP ---
MONGO_URI = os.environ.get("MONGO_URI")
try:
    parsed_uri = urllib.parse.unquote(MONGO_URI)
    cluster = MongoClient(parsed_uri, serverSelectionTimeoutMS=5000)
    cluster.admin.command('ping')
    db = cluster["nihongo_db"]
    quiz_db = db["weekly_scores"]
    print("✅ MongoDB Connected!")
except Exception as e:
    print(f"❌ MongoDB Error: {e}")

# --- 🎌 ROLE CONSTANTS ---
ROLE_NAMES = ["📍 N5 Beginner", "📍 N4 Elementary", "📍 N3 Intermediate", "📍 N2 Pre-Advanced", "📍 N1 Advanced"]
GREETINGS_LIST = [
    "Irasshaimase! 🌸", "Konnichiwa! ✨", "Yokoso! 🎌", "Hajimemashite! ⛩️",
    "Okaerinasai! 🏡", "Ohayou gozaimasu! 🌅", "Konbanwa! 🌙", 
    "Yoroshiku onegaishimasu! 🤝", "Kangei shimasu! 🎊", "Welcome to the world of Japanese! 🗾"
]

def has_main_role(member):
    return any(r.name in ROLE_NAMES or r.name == "Visitor" for r in member.roles)

def get_fallback_role(current_role_name):
    try:
        idx = ROLE_NAMES.index(current_role_name)
        return ROLE_NAMES[max(0, idx - 1)]
    except ValueError:
        return ROLE_NAMES[0]

def extract_json(raw_text):
    """Ultimate JSON Extractor with AST fallback for AI hallucinations"""
    try:
        # 1. Clean markdown formatting
        cleaned = re.sub(r'```(?:json|JSON)?\s*(.*?)\s*```', r'\1', raw_text, flags=re.DOTALL)
        
        # 2. Extract array bounds
        start = cleaned.find('[')
        end = cleaned.rfind(']')
        
        if start != -1 and end != -1:
            json_str = cleaned[start:end+1]
        else:
            start = cleaned.find('{')
            end = cleaned.rfind('}')
            if start != -1 and end != -1:
                json_str = f"[{cleaned[start:end+1]}]"
            else:
                raise ValueError("No JSON bounds found in AI output.")

        # 3. Fix trailing commas (common AI mistake)
        json_str = re.sub(r',\s*([\]}])', r'\1', json_str)
        
        # 4. Attempt strict JSON parsing
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # 5. AST Fallback: Parses Python-style dicts (handles single quotes perfectly)
            safe_python_str = json_str.replace("null", "None").replace("true", "True").replace("false", "False")
            return ast.literal_eval(safe_python_str)

    except Exception as e:
        print(f"⚠️ Ultimate JSON Parse Failed: {e}\n--- RAW AI OUTPUT ---\n{raw_text}\n---------------------")
        return [{"question": "AI Formatting Error: Please try clicking again.", "options": {"A": "Wait", "B": "Retry", "C": "Cancel", "D": "Help"}, "answer": "B"}]

# --- 🧠 DIRECT GROQ REST API ---
async def generate_gemini_response(prompt):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key: 
        raise Exception("GROQ_API_KEY missing from Render Environment!")
    
    clean_key = api_key.strip()
    url = "https://api.groq.com/openai/v1/chat/completions"
    
    payload = {
        "model": "openai/gpt-oss-20b", 
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": 6000
    }

    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Content-Type": "application/json"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                raise Exception(f"HTTP {resp.status}: {err_text}")
            
            data = await resp.json()
            try: 
                return data['choices'][0]['message']['content']
            except KeyError: 
                raise Exception("API returned invalid structure.")

# --- 🧠 PLACEMENT & QUIZ UI LOGIC ---
class ForceClaimView(View):
    def __init__(self, user, target_role_name, fallback_role_name):
        super().__init__(timeout=120)
        self.user, self.target_role, self.fallback_role = user, target_role_name, fallback_role_name

    @discord.ui.button(label=f"Accept Recommended", style=discord.ButtonStyle.success)
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        role = discord.utils.get(interaction.guild.roles, name=self.fallback_role)
        if role: await interaction.user.add_roles(role)
        await interaction.response.edit_message(content=f"✅ You accepted the recommendation. You are now an **{self.fallback_role}**!", view=None, embed=None)

    @discord.ui.button(label="Force Keep Level", style=discord.ButtonStyle.danger)
    async def btn_force(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        role = discord.utils.get(interaction.guild.roles, name=self.target_role)
        if role: await interaction.user.add_roles(role)
        await interaction.response.edit_message(content=f"✅ You chose to keep your selected level. You are now an **{self.target_role}**!", view=None, embed=None)

class PlacementQuizView(View):
    def __init__(self, user, question_data, target_role_name, is_changerole=False):
        super().__init__(timeout=60)
        self.user, self.q, self.target_role = user, question_data, target_role_name
        self.is_changerole = is_changerole
        
        for label in ["A", "B", "C", "D"]:
            btn = Button(label=label, custom_id=label, style=discord.ButtonStyle.primary)
            btn.callback = self.button_callback
            self.add_item(btn)

    async def button_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id: return await interaction.response.send_message("❌ Not your test!", ephemeral=True)
        self.stop()
        role = discord.utils.get(interaction.guild.roles, name=self.target_role)
        passed = (interaction.data['custom_id'] == self.q['answer'])

        if passed:
            if self.is_changerole:
                for r in interaction.user.roles:
                    if r.name in ROLE_NAMES: await interaction.user.remove_roles(r)
            if role: await interaction.user.add_roles(role)
            await interaction.response.edit_message(content=f"🎉 **Correct!** You proved your skills. You are now an **{self.target_role}**!", embed=None, view=None)
        else:
            if self.is_changerole:
                await interaction.response.edit_message(content=f"❌ **Incorrect!** The correct answer was {self.q['answer']}. Your role has not been changed.", embed=None, view=None)
            else:
                fallback = get_fallback_role(self.target_role)
                if fallback == self.target_role:
                    if role: await interaction.user.add_roles(role)
                    await interaction.response.edit_message(content=f"❌ Incorrect! But since you selected Beginner, you are now an **{self.target_role}**.", embed=None, view=None)
                else:
                    embed = discord.Embed(title="⚠️ Placement Test Failed", description=f"The correct answer was **{self.q['answer']}**.\n\nWe recommend starting at **{fallback}**, but you can choose to force-claim your selected **{self.target_role}** role.", color=discord.Color.orange())
                    await interaction.response.edit_message(content="", embed=embed, view=ForceClaimView(self.user, self.target_role, fallback))

    async def on_timeout(self):
        try: await self.message.edit(content="⏳ **Time's up!** Please select your role again to retry the test.", view=None, embed=None)
        except: pass

class QuizView(View):
    def __init__(self, user, questions, level, start_time):
        super().__init__(timeout=60) 
        self.user = user
        self.questions = questions
        self.level = level
        self.current_idx = 0
        self.score = 0
        self.start_time = start_time
        self.total_q = len(questions)
        self.is_fallback = (self.total_q == 1 and "Formatting Error" in questions[0].get("question", ""))
        
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
        
        if self.current_idx < self.total_q:
            q = self.questions[self.current_idx]
            embed = discord.Embed(title=f"🎌 {self.level} Mock Test ({self.current_idx + 1}/{self.total_q})", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x3498db)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            self.stop()
            time_taken = round(time.time() - self.start_time)
            
            if self.is_fallback:
                await interaction.response.edit_message(content="⚠️ An AI generation error occurred. Your score was not saved. Please try `/quiz` again.", embed=None, view=None)
                return
                
            quiz_db.update_one({"_id": self.user.id}, {"$set": {"level": self.level, "score": self.score, "time_taken": time_taken}}, upsert=True)
            await interaction.response.edit_message(embed=discord.Embed(title="🏁 Quiz Completed!", description=f"Score: **{self.score}/{self.total_q}** in {time_taken}s.", color=discord.Color.green()), view=None)

    async def on_timeout(self):
        try: await self.message.edit(content="⏳ **Time's up!** Attempt cancelled to prevent spam.", view=None, embed=None)
        except: pass

# --- 🌸 ONBOARDING UI ---
class JLPTSelect(Select):
    def __init__(self):
        opts = [discord.SelectOption(label=r.split(" ", 1)[1], emoji="📍", value=r) for r in ROLE_NAMES]
        super().__init__(placeholder="Select your target JLPT Level...", min_values=1, max_values=1, options=opts, custom_id="jlpt_dropdown")

    async def callback(self, interaction: discord.Interaction):
        if has_main_role(interaction.user): return await interaction.response.send_message("❌ You already have a role! Please use `/changerole` to upgrade.", ephemeral=True)
            
        selected_role = self.values[0]
        level_short = selected_role.split(" ")[1] 
        await interaction.response.send_message(f"⏳ Generating a 1-question placement test for {level_short}...", ephemeral=True)
        
        prompt = f"""You are an expert JLPT Examiner. Generate exactly 1 multiple-choice question for JLPT {level_short} (Grammar or Vocab). Always shuffle the options.
        1. CONTEXT-RICH TEXT ONLY: The sentence MUST provide enough logical context to be solved purely through reading, without any images or audio (e.g., "雨が降っているので、___をさします。" -> Answer: かさ).
        2. NO VISUAL QUESTIONS: NEVER generate vague questions like "___は何ですか。" or "これは___です。"
        3. Must have exactly ONE blank represented by '___'.
        4. Ensure high-quality, natural Japanese.
        5. Ensure furigana of kanjis used, should be written in ([]) square brackets just after kanji used.
        6. CRITICAL JSON RULE: Use strictly double quotes (") for all keys and string values. Do not use single quotes. Do not add trailing commas.
        Output ONLY a valid JSON array format exactly like this:
        [{{"question": "りんごを ___ 買いました。", "options": {{"A": "みっつ", "B": "みつ", "C": "さん", "D": "さんこ"}}, "answer": "A"}}]"""
        
        try:
            raw_text = await generate_gemini_response(prompt)
            q = extract_json(raw_text)[0]
            embed = discord.Embed(title=f"🎌 {level_short} Placement Test", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x3498db)
            embed.set_footer(text="⏳ You have 60 seconds to answer.")
            view = PlacementQuizView(interaction.user, q, selected_role)
            msg = await interaction.edit_original_response(content="", embed=embed, view=view)
            view.message = msg
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ AI Initialization Error: {e}")

class WelcomeView(View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="📸 Visitor", style=discord.ButtonStyle.secondary, custom_id="role_visitor")
    async def visitor_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if has_main_role(interaction.user): return await interaction.response.send_message("❌ You already have a role! Please use `/changerole` to upgrade.", ephemeral=True)
        role = discord.utils.get(interaction.guild.roles, name="Visitor") 
        if role:
            await interaction.user.add_roles(role)
            await interaction.response.send_message("✅ You are now a Visitor! Enjoy exploring.", ephemeral=True)
        else: await interaction.response.send_message("❌ Visitor role not found.", ephemeral=True)

    @discord.ui.button(label="🎌 JP Learner", style=discord.ButtonStyle.primary, custom_id="role_learner")
    async def learner_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if has_main_role(interaction.user): return await interaction.response.send_message("❌ You already have a role! Please use `/changerole` to upgrade.", ephemeral=True)
        view = View(timeout=None)
        view.add_item(JLPTSelect())
        await interaction.response.send_message("Awesome! Please select your target JLPT level below:", view=view, ephemeral=True)

# --- 🤖 MAIN BOT CLASS & BACKGROUND TASKS ---
class NihongoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True 
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)

    async def setup_hook(self):
        self.weekly_leaderboard_loop.start()
        self.add_view(WelcomeView())
        view = View(timeout=None)
        view.add_item(JLPTSelect())
        self.add_view(view)
        await self.tree.sync()
        print("✅ Commands Synced & Tasks Started.")

    async def on_member_join(self, member: discord.Member):
        welcome_ch = discord.utils.find(lambda c: "welcome" in c.name.lower(), member.guild.channels)
        if welcome_ch:
            embed = discord.Embed(title="🌸 Choose Your Path", description="To get started, please select your role below:\n\n📸 **Visitor:** I'm just exploring.\n🎌 **JP Learner:** I am studying Japanese.", color=0xffb6c1)
            try: await welcome_ch.send(content=f"{random.choice(GREETINGS_LIST)} Welcome to the community, {member.mention}!", embed=embed, view=WelcomeView())
            except Exception: pass

    @tasks.loop(minutes=1)
    async def weekly_leaderboard_loop(self):
        now_jst = datetime.now(pytz.timezone('Asia/Tokyo'))
        
        # --- 1. MORNING HYPE ANNOUNCEMENT (Sunday 10:00 AM JST) - ALL IN ENGLISH ---
        if now_jst.weekday() == 6 and now_jst.hour == 10 and now_jst.minute == 0:
            if not getattr(self, 'morning_hype_done', False):
                self.morning_hype_done = True
                for guild in self.guilds:
                    for role_name in ROLE_NAMES:
                        level_short = role_name.split(" ")[1].lower() 
                        channel = discord.utils.find(lambda c: f"{level_short}-leaderboard" in c.name.lower(), guild.channels)
                        role = discord.utils.get(guild.roles, name=role_name)
                        
                        if channel and role:
                            hype_msg = (
                                f"🔔 **Attention {role.mention}!**\n\n"
                                f"Tonight at **10:00 PM (Japan Standard Time)**, this week's mock test results will be announced! 🏆\n"
                                f"Buckle up and give it your best! Securing the #1 spot earns you exclusive server perks and ultimate bragging rights.\n"
                                f"Take your mock tests using `/quiz` now to climb the ranks! 🎌✨"
                            )
                            try: await channel.send(hype_msg)
                            except Exception: pass
                                
        elif now_jst.weekday() == 6 and now_jst.hour == 10 and now_jst.minute == 1:
            self.morning_hype_done = False

        # --- 2. NIGHTLY AUTO-ANNOUNCEMENT (Sunday 10:00 PM JST) ---
        if now_jst.weekday() == 6 and now_jst.hour == 22 and now_jst.minute == 0:
            if not getattr(self, 'night_announce_done', False):
                self.night_announce_done = True
                
                for guild in self.guilds:
                    for role_name in ROLE_NAMES:
                        level_short = role_name.split(" ")[1].lower()
                        channel = discord.utils.find(lambda c: f"{level_short}-leaderboard" in c.name.lower(), guild.channels)
                        role = discord.utils.get(guild.roles, name=role_name)
                        
                        if channel and role:
                            top_scorers = quiz_db.find({"level": role_name}).sort([("score", -1), ("time_taken", 1)]).limit(3)
                            scorers_list = list(top_scorers)
                            
                            if scorers_list:
                                embed = discord.Embed(title=f"🏆 Weekly Leaderboard: {role_name}", color=0xf1c40f)
                                medals = ["🥇", "🥈", "🥉"]
                                for idx, user_data in enumerate(scorers_list):
                                    embed.add_field(
                                        name=f"{medals[idx]} Rank #{idx + 1}", 
                                        value=f"<@{user_data['_id']}> - **Score: {user_data['score']}/20** (Time: {user_data.get('time_taken', 'N/A')}s)", 
                                        inline=False
                                    )
                                try: await channel.send(content=f"🎉 **THE RESULTS ARE IN!** {role.mention}", embed=embed)
                                except Exception: pass
                                
        elif now_jst.weekday() == 6 and now_jst.hour == 22 and now_jst.minute == 1:
            self.night_announce_done = False
            
            # --- 3. DATABASE PURGE EXACTLY AT 10:01 PM JST ---
            if not getattr(self, 'weekly_reset_done', False):
                self.weekly_reset_done = True
                quiz_db.delete_many({}) 
                print("✅ 10:01 PM JST: All weekly leaderboard data wiped to save memory.")
                
        elif now_jst.weekday() == 6 and now_jst.hour == 22 and now_jst.minute == 2:
            self.weekly_reset_done = False

bot = NihongoBot()

# --- ⌨️ COMMANDS ---
@bot.tree.command(name="help", description="Shows bot commands.")
async def help_command(interaction: discord.Interaction):
    if "bot-commands" not in interaction.channel.name.lower(): return await interaction.response.send_message("❌ Use `#🤖・bot-commands`.", ephemeral=True)
    await interaction.response.send_message(embed=discord.Embed(title="🤖 Commands", description="🧠 `/quiz` - JLPT Mock Test\n🏆 `/leaderboard` - Check weekly standings\n⬆️ `/changerole` - Upgrade your Japanese level\n📢 `/leaderboardannounce` - [Admin] Announce results manually", color=0xffb6c1), ephemeral=True)

@bot.tree.command(name="leaderboard", description="[Admin Only] Check the Top 5 performing players for a specific level.")
@app_commands.choices(target_level=[app_commands.Choice(name=r.split(" ", 1)[1], value=r) for r in ROLE_NAMES])
async def leaderboard(interaction: discord.Interaction, target_level: app_commands.Choice[str]):
    # 1. Setting response to Ephemeral (Private)
    await interaction.response.defer(ephemeral=True)
    
    # 2. Permission Check
    has_permission = any(role.name in ["Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied: You need `セィニア・アデュミン` or `ファウンダ` role to use this.", ephemeral=True)
    
    level_full_name = target_level.value
    
    # 3. Fetching strictly Top 5
    top_scorers = quiz_db.find({"level": level_full_name}).sort([("score", -1), ("time_taken", 1)]).limit(5)
    scorers_list = list(top_scorers)
    
    if not scorers_list:
        return await interaction.followup.send(f"⚠️ No data found for {level_full_name} this week.", ephemeral=True)
        
    # 4. Creating the Embed
    embed = discord.Embed(title=f"🏆 Top 5 Leaderboard: {level_full_name}", color=0xf1c40f)
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for idx, user_data in enumerate(scorers_list):
        embed.add_field(
            name=f"{medals[idx]} Rank #{idx + 1}", 
            value=f"<@{user_data['_id']}> - **Score: {user_data['score']}/20** (Time: {user_data.get('time_taken', 'N/A')}s)", 
            inline=False
        )
    
    embed.set_footer(text="Results reset every Sunday at 10:01 PM JST")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="changerole", description="Upgrade your JLPT level.")
@app_commands.choices(target_level=[app_commands.Choice(name=r.split(" ", 1)[1], value=r) for r in ROLE_NAMES])
async def changerole(interaction: discord.Interaction, target_level: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    if "bot-commands" not in interaction.channel.name.lower(): return await interaction.followup.send("❌ Use `#🤖・bot-commands`.", ephemeral=True)
    if any(r.name == target_level.value for r in interaction.user.roles): return await interaction.followup.send("❌ You already have this role!", ephemeral=True)
    
    lvl_short = target_level.value.split(" ")[1]
    await interaction.followup.send(f"⏳ Generating test for {lvl_short}...", ephemeral=True)
    
    prompt = f"""You are an expert JLPT Examiner. Generate exactly 1 multiple-choice question for JLPT {lvl_short} (Grammar/Vocab). Difficulty: Medium.
    1. CONTEXT-RICH TEXT ONLY: The sentence MUST provide enough logical context to be solved purely through reading, without any images or audio (e.g., "雨が降っているので、___をさします。" -> Answer: かさ).
    2. NO VISUAL QUESTIONS: NEVER generate vague questions like "___は何ですか。" or "これは___です。"
    3. Each question MUST have exactly one blank space represented by '___'.
    4. When using kanjis write furigana in ([]) square brackets just after the word ends.
    5. The 4 options (A, B, C, D) must be logically distinct, but ONLY ONE fits grammatically and semantically. Always shuffle the options for each question.
    6. CRITICAL JSON RULE: Use strictly double quotes (") for all keys and string values. Do not use single quotes. Do not add trailing commas.
    
    Output ONLY a valid JSON array of 1 objects. DO NOT output any other text or markdown outside the JSON array. Example:
    [{{"question": "外は寒いので、___を着てください。", "options": {{"A": "コート", "B": "かばん", "C": "めがね", "D": "くつ"}}, "answer": "A"}}]"""
    
    try:
        raw_text = await generate_gemini_response(prompt)
        q = extract_json(raw_text)[0]
        embed = discord.Embed(title=f"🎌 {lvl_short} Upgrade Test", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0xe67e22)
        view = PlacementQuizView(interaction.user, q, target_level.value, is_changerole=True)
        msg = await interaction.edit_original_response(content="", embed=embed, view=view)
        view.message = msg 
    except Exception as e: await interaction.edit_original_response(content=f"❌ AI Fetch Error: {e}")

@bot.tree.command(name="quiz", description="Take a JLPT test.")
@app_commands.choices(furigana=[
    app_commands.Choice(name="Yes, include Furigana (Reading aids)", value="with_furigana"),
    app_commands.Choice(name="No, just standard Kanji", value="without_furigana")
])
async def quiz(interaction: discord.Interaction, furigana: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    user_level = next((r.name.replace("📍 ", "").strip() for r in interaction.user.roles if r.name in ROLE_NAMES), None)
    if not user_level: return await interaction.followup.send("❌ Get a N5-N1 role first.", ephemeral=True)
    
    furigana_rule = "Use Furigana in brackets after all Kanji in questions as well as options generated wherever required (e.g., 漢字【かんじ】)." if furigana.value == "with_furigana" else "DO NOT use Furigana/reading aids. Use standard Kanji."
    await interaction.followup.send(f"⏳ Generating 20 {user_level} questions ({furigana.name})...", ephemeral=True)
    
    prompt = f"""You are an expert JLPT Examiner. Generate exactly 20 multiple-choice questions for JLPT {user_level} (10 Grammar, 10 Vocab).
    Strict Rules:
    1. CONTEXT-RICH TEXT ONLY: The sentence MUST provide enough logical context to be solved purely through reading, without any images or audio (e.g., "雨が降っているので、___をさします。" -> Answer: かさ).
    2. NO VISUAL QUESTIONS: NEVER generate vague questions like "___は何ですか。" or "これは___です。"
    3. Each question MUST have exactly one blank space represented by '___'.
    4. {furigana_rule}
    5. The 4 options (A, B, C, D) must be logically distinct, but ONLY ONE fits grammatically and semantically. Always shuffle the options for each question.
    6. CRITICAL JSON RULE: Use strictly double quotes (") for all keys and string values. Do not use single quotes. Do not add trailing commas.
    
    Output ONLY a valid JSON array of 20 objects. DO NOT output any other text or markdown outside the JSON array. Example:
    [{{"question": "外は寒いので、___を着てください。", "options": {{"A": "コート", "B": "かばん", "C": "めがね", "D": "くつ"}}, "answer": "A"}}]"""
    
    try:
        raw_text = await generate_gemini_response(prompt)
        questions_data = extract_json(raw_text)
        q = questions_data[0]
        embed = discord.Embed(title=f"🎌 {user_level} Mock Test (1/{len(questions_data)})", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x3498db)
        view = QuizView(interaction.user, questions_data, user_level, time.time())
        msg = await interaction.edit_original_response(content="", embed=embed, view=view)
        view.message = msg 
    except Exception as e: await interaction.edit_original_response(content=f"❌ AI Fetch Error: {e}")

@bot.tree.command(name="leaderboardannounce", description="[Admin Only] Check the Top 5 performing players for a specific level.")
@app_commands.choices(target_level=[app_commands.Choice(name=r.split(" ", 1)[1], value=r) for r in ROLE_NAMES])
async def leaderboard_announce(interaction: discord.Interaction, target_level: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    
    has_permission = any(role.name in ["Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied: You need `セィニア・アデュミン` or `ファウンダ` role to use this.", ephemeral=True)
    
    level_full_name = target_level.value
    
    top_scorers = quiz_db.find({"level": level_full_name}).sort([("score", -1), ("time_taken", 1)]).limit(5)
    scorers_list = list(top_scorers)
    
    if not scorers_list:
        return await interaction.followup.send(f"⚠️ No data found for {level_full_name} this week.", ephemeral=True)
        
    embed = discord.Embed(title=f"🏆 Top 5 Leaderboard: {level_full_name}", color=0xf1c40f)
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for idx, user_data in enumerate(scorers_list):
        embed.add_field(
            name=f"{medals[idx]} Rank #{idx + 1}", 
            value=f"<@{user_data['_id']}> - **Score: {user_data['score']}/20** (Time: {user_data.get('time_taken', 'N/A')}s)", 
            inline=False
        )
    
    await interaction.followup.send(embed=embed, ephemeral=True)

bot.run(os.environ.get("BOT_TOKEN"))
