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
from datetime import datetime, timedelta
import pytz
from pymongo import MongoClient
import aiohttp
import urllib.parse
import ast
import asyncio
from kanjis import N5_KANJI, N4_KANJI, N3_KANJI, N2_KANJI, N1_KANJI

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
    kanji_db = db["kanji_tracker"]
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
    try:
        cleaned = re.sub(r'```(?:json|JSON)?\s*(.*?)\s*```', r'\1', raw_text, flags=re.DOTALL).strip()
        possible_jsons = []
        
        start_a = cleaned.find('[')
        end_a = cleaned.rfind(']')
        if start_a != -1 and end_a != -1 and end_a > start_a:
            possible_jsons.append(cleaned[start_a:end_a+1])
            
        start_d = cleaned.find('{')
        end_d = cleaned.rfind('}')
        if start_d != -1 and end_d != -1 and end_d > start_d:
            possible_jsons.append(cleaned[start_d:end_d+1])
            
        if not possible_jsons:
            raise ValueError("No JSON boundaries found.")
        
        possible_jsons.sort(key=len, reverse=True)
        
        for j_str in possible_jsons:
            j_str_fixed = re.sub(r',\s*([\]}])', r'\1', j_str)
            try:
                return json.loads(j_str_fixed)
            except json.JSONDecodeError:
                try:
                    safe_python_str = j_str_fixed.replace("null", "None").replace("true", "True").replace("false", "False")
                    return ast.literal_eval(safe_python_str)
                except (SyntaxError, ValueError):
                    continue
                    
        raise ValueError("All parsing attempts failed.")
        
    except Exception as e:
        print(f"⚠️ JSON Parse Failed: {e}\nRaw AI Output:\n{raw_text}")
        return [{"question": "AI Formatting Error", "options": {"A": "Wait", "B": "Retry", "C": "Cancel", "D": "Help"}, "answer": "B"}]

# --- 🧠 DIRECT GEMINI REST API ---
async def generate_gemini_response(prompt):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: 
        raise Exception("GEMINI_API_KEY missing from Environment!")
    
    clean_key = api_key.strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={clean_key}"
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 6000
        }
    }
    headers = {"Content-Type": "application/json"}
    
    backoff_times = [1, 2, 4, 8]
    async with aiohttp.ClientSession() as session:
        for attempt, wait_time in enumerate(backoff_times + [0]):
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    try: 
                        return data['candidates'][0]['content']['parts'][0]['text']
                    except (KeyError, IndexError): 
                        raise Exception("API returned invalid structure.")
                elif resp.status == 429 and attempt < len(backoff_times):
                    print(f"⚠️ API Rate Limit Hit (429). Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    err_text = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {err_text}")

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

class StoryReaderView(View):
    def __init__(self, user, level, story_content):
        super().__init__(timeout=None)
        self.user = user
        self.level = level
        self.story_content = story_content

    async def handle_analysis(self, interaction: discord.Interaction, task_type: str):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("❌ This is not your reading session!", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        
        prompts = {
            "translate": f"Translate this Japanese story to English naturally:\n\n{self.story_content}",
            "grammar": f"Analyze the key JLPT {self.level} grammar points used in this story. Explain them simply:\n\n{self.story_content}",
            "vocab": f"Extract the key JLPT {self.level} vocabulary from this story. Provide the Kanji, reading (Romaji), and meaning:\n\n{self.story_content}"
        }
        
        try:
            response_text = await generate_gemini_response(prompts[task_type])
            embed = discord.Embed(title=f"📖 {task_type.capitalize()} Analysis", description=response_text[:4000], color=0x2ecc71)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Analysis failed: {e}", ephemeral=True)

    @discord.ui.button(label="🇬🇧 Translate", style=discord.ButtonStyle.primary)
    async def btn_translate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_analysis(interaction, "translate")

    @discord.ui.button(label="🧠 Analyze Grammar", style=discord.ButtonStyle.success)
    async def btn_grammar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_analysis(interaction, "grammar")

    @discord.ui.button(label="📖 Extract Vocab", style=discord.ButtonStyle.secondary)
    async def btn_vocab(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_analysis(interaction, "vocab")

# --- 🟢 PERSISTENT VIEWS (Fixes Restart Dead Buttons) ---
class PersistentFreemiumStoryView(View):
    def __init__(self):
        super().__init__(timeout=None) 
        self.cached_responses = {}

    async def check_premium(self, interaction: discord.Interaction):
        has_pro = any(r.name == "金 Pro Learners 金" for r in interaction.user.roles)
        if not has_pro:
            embed = discord.Embed(
                title="🔒 Premium Feature Unlocked", 
                description="Oops! Grammar and Vocab analysis are exclusively available for our **金 Pro Learners 金**.\n\nUnlock the full potential of your Japanese journey with unlimited personalized AI stories, deep grammar analysis, and much more! Upgrade today to access this and other pro tools. ✨", 
                color=0xf1c40f
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

    async def handle_analysis(self, interaction: discord.Interaction, task_type: str):
        await interaction.response.defer(ephemeral=True)
        
        # Reads the story content directly from the existing Discord message!
        embed_data = interaction.message.embeds[0]
        story_content = embed_data.description
        footer_text = embed_data.footer.text
        level = footer_text.split(" | ")[0].replace("Level: ", "")
        
        cache_key = f"{interaction.message.id}_{task_type}"
        
        if cache_key in self.cached_responses:
            response_text = self.cached_responses[cache_key]
        else:
            prompts = {
                "translate": f"Translate this Japanese story to English naturally:\n\n{story_content}",
                "grammar": f"Analyze the key JLPT {level} grammar points used in this story. Explain them simply:\n\n{story_content}",
                "vocab": f"Extract the key JLPT {level} vocabulary from this story. Provide the Kanji, reading (Romaji), and meaning:\n\n{story_content}"
            }
            try:
                response_text = await generate_gemini_response(prompts[task_type])
                self.cached_responses[cache_key] = response_text 
            except Exception as e:
                return await interaction.followup.send(f"❌ Analysis failed: {e}", ephemeral=True)

        embed = discord.Embed(title=f"📖 {task_type.capitalize()} Analysis", description=response_text[:4000], color=0x2ecc71)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="🇬🇧 Translate (Free)", style=discord.ButtonStyle.primary, custom_id="freemium_trans")
    async def btn_translate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_analysis(interaction, "translate")

    @discord.ui.button(label="🧠 Analyze Grammar (Pro)", style=discord.ButtonStyle.success, custom_id="freemium_gram")
    async def btn_grammar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.check_premium(interaction):
            await self.handle_analysis(interaction, "grammar")

    @discord.ui.button(label="📖 Extract Vocab (Pro)", style=discord.ButtonStyle.secondary, custom_id="freemium_voc")
    async def btn_vocab(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.check_premium(interaction):
            await self.handle_analysis(interaction, "vocab")

class PersistentDailyKanjiView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.cached_responses = {}

    async def handle_trick(self, interaction: discord.Interaction, trick_type: str):
        await interaction.response.defer(ephemeral=True)
        
        embed_data = interaction.message.embeds[0]
        footer_text = embed_data.footer.text
        kanji_str = footer_text.split(" | ")[0].replace("Kanji: ", "")
        level = footer_text.split(" | ")[1].replace("Level: ", "")
        
        cache_key = f"{interaction.message.id}_{trick_type}"

        if cache_key in self.cached_responses:
            response_text = self.cached_responses[cache_key]
        else:
            if trick_type == "visual":
                prompt = f"Create a short, logical visual memory trick to remember the shape of these JLPT {level} Kanji(s): {kanji_str}. Format cleanly in English."
            else:
                prompt = f"Create a short, logical pronunciation trick to remember the Onyomi/Kunyomi reading of these JLPT {level} Kanji(s): {kanji_str}. Format cleanly in English."
            
            try:
                response_text = await generate_gemini_response(prompt)
                self.cached_responses[cache_key] = response_text
            except Exception as e:
                return await interaction.followup.send(f"❌ Failed to fetch trick: {e}", ephemeral=True)

        embed = discord.Embed(
            title=f"💡 {'Visual Memory' if trick_type == 'visual' else 'Pronunciation'} Trick", 
            description=response_text[:4000], 
            color=0xf1c40f
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="🧠 Visual Memory Trick", style=discord.ButtonStyle.primary, custom_id="kanji_visual")
    async def btn_visual(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_trick(interaction, "visual")

    @discord.ui.button(label="🗣️ Pronunciation Trick", style=discord.ButtonStyle.success, custom_id="kanji_pronounce")
    async def btn_pronunciation(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_trick(interaction, "pronunciation")

class JournalThreadView(View):
    def __init__(self, user, level, original, corrected, thread):
        super().__init__(timeout=None)
        self.user = user
        self.level = level
        self.original = original
        self.corrected = corrected
        self.thread = thread
        self.cached_responses = {} 

    async def handle_analysis(self, interaction: discord.Interaction, task_type: str):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("❌ This is not your journal session!", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        
        if task_type in self.cached_responses:
            response_text = self.cached_responses[task_type]
        else:
            if task_type == "translate":
                prompt = f"Analyze these two Japanese texts. 1) Original: {self.original} 2) Corrected: {self.corrected}. Explain briefly how the original sounded (awkward/literal) versus how the corrected sentence sounds in natural English."
            elif task_type == "reading":
                prompt = f"Rewrite this Japanese text, adding furigana in square brackets exactly after EVERY Kanji used: {self.corrected}. Example format: 毎日 [まいにち] 漢字 [かんじ] を 勉強 [べんきょう] します。"
            elif task_type == "vocab":
                prompt = f"Extract key JLPT {self.level} vocabulary from this text: {self.corrected}. Provide the Kanji, reading (Romaji), and English meaning in a clean bulleted list."
            
            try:
                response_text = await generate_gemini_response(prompt)
                self.cached_responses[task_type] = response_text
            except Exception as e:
                return await interaction.followup.send(f"❌ API Error: {e}", ephemeral=True)

        embed = discord.Embed(title=f"📖 {task_type.capitalize()} Analysis", description=response_text[:4000], color=0x9b59b6)
        await self.thread.send(content=f"{interaction.user.mention}, here is your {task_type} analysis:", embed=embed)
        await interaction.followup.send(f"✅ Sent to your thread: {self.thread.mention}", ephemeral=True)

    @discord.ui.button(label="🇬🇧 Translate (Before/After)", style=discord.ButtonStyle.primary)
    async def btn_trans(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_analysis(interaction, "translate")

    @discord.ui.button(label="📖 Reading Guide", style=discord.ButtonStyle.success)
    async def btn_read(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_analysis(interaction, "reading")

    @discord.ui.button(label="🧠 Extract Words", style=discord.ButtonStyle.secondary)
    async def btn_vocab(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_analysis(interaction, "vocab")

class JournalModal(discord.ui.Modal, title='Daily Japanese Journal'):
    journal_input = discord.ui.TextInput(
        label='Write your journal in Japanese...',
        style=discord.TextStyle.paragraph,
        placeholder='Type here (Max 4000 characters)...',
        max_length=4000
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        user_level_role = next((r.name for r in interaction.user.roles if r.name in ROLE_NAMES), None)
        level_short = user_level_role.split(" ")[1] if user_level_role else "N5"
        user_text = self.journal_input.value

        prompt = f"""You are an expert Japanese Sensei. The user is currently at the JLPT {level_short} level.
        Task: Correct their Japanese journal entry. Fix grammatical errors, unnatural phrasing, and particle mistakes.
        CRITICAL RULE: Strictly limit the suggested vocabulary and grammar to the {level_short} level. Do not use overly advanced structures.
        Output ONLY valid JSON with two keys: "corrected_text" (the fixed Japanese text) and "explanation" (a brief 1-2 sentence explanation of the main mistakes in simple english language).
        User's Text: {user_text}"""

        try:
            raw_text = await generate_gemini_response(prompt)
            data = extract_json(raw_text)
            if isinstance(data, list): data = data[0]
            
            corrected_text = data.get("corrected_text", "Correction failed.")
            explanation = data.get("explanation", "No explanation provided.")

            embed = discord.Embed(title=f"📓 {interaction.user.display_name}'s Journal Analysis", color=0xf1c40f)
            embed.add_field(name="❌ Original Input", value=user_text[:1024], inline=False)
            embed.add_field(name="✅ Native Correction", value=corrected_text[:1024], inline=False)
            embed.add_field(name="👨‍🏫 Sensei's Note", value=explanation[:1024], inline=False)
            embed.set_footer(text=f"Level Restricted: {level_short}")

            msg = await interaction.channel.send(content=interaction.user.mention, embed=embed)
            thread = await msg.create_thread(name=f"🧵 {interaction.user.display_name}'s Sensei Thread", auto_archive_duration=1440)
            
            view = JournalThreadView(interaction.user, level_short, user_text, corrected_text, thread)
            await msg.edit(view=view)
            
            await interaction.followup.send("✅ Journal submitted successfully! Check the channel for your results.", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Failed to process journal: {e}", ephemeral=True)

class PersistentJournalView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📝 Submit Daily Journal", style=discord.ButtonStyle.primary, custom_id="premium_journal_btn")
    async def submit_journal(self, interaction: discord.Interaction, button: discord.ui.Button):
        has_pro = any(r.name == "金 Pro Learners 金" for r in interaction.user.roles)
        if not has_pro:
            embed = discord.Embed(
                title="🔒 Premium Feature Unlocked", 
                description="Oops! The **Guided Journaling & AI Sensei** feature is exclusively available for our **金 Pro Learners 金**.\n\nPractice output, get instant native corrections, and trigger the 'noticing' effect to skyrocket your Japanese fluency. Upgrade today to unlock unlimited personalized tutoring! ✨", 
                color=0xf1c40f
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        await interaction.response.send_modal(JournalModal())

class ScenarioSelect(Select):
    def __init__(self, options_data):
        self.options_data = options_data
        select_options = []
        emojis = ["🇦", "🇧", "🇨", "🇩"]
        
        for idx, opt in enumerate(options_data):
            label_name = f"Option {chr(65+idx)}" 
            select_options.append(discord.SelectOption(label=label_name, value=str(idx), emoji=emojis[idx]))
            
        super().__init__(placeholder="Select your answer (A, B, C, or D)...", min_values=1, max_values=1, options=select_options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        has_pro = any(r.name == "金 Pro Learners 金" for r in interaction.user.roles)
        if not has_pro:
            return await interaction.followup.send("❌ You need the **金 Pro Learners 金** role to answer Scenario Drills!", ephemeral=True)

        selected_idx = int(self.values[0])
        selected_opt = self.options_data[selected_idx]
        
        is_correct = selected_opt.get("is_correct", False)
        status_emoji = "✅ Correct!" if is_correct else "❌ Incorrect!"
        color = 0x2ecc71 if is_correct else 0xe74c3c

        embed = discord.Embed(title=f"{status_emoji} Detailed Breakdown", color=color)
        
        embed.add_field(name="Your Choice", value=f"**{selected_opt['label']}**\n{selected_opt['explanation']}", inline=False)
        
        all_exp = ""
        for opt in self.options_data:
            mark = "✅" if opt.get("is_correct") else "❌"
            all_exp += f"{mark} **{opt['label']}**\n{opt['explanation']}\n\n"
        
        embed.add_field(name="All Options Analyzed", value=all_exp[:1024], inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=True)

class ScenarioView(View):
    def __init__(self, options_data):
        super().__init__(timeout=None)
        self.add_item(ScenarioSelect(options_data))

class PersistentTicketCloseView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.secondary, emoji="🔒", custom_id="ticket_close_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        # Check if already closed
        if interaction.channel.category and "CLOSED TICKETS" in interaction.channel.category.name:
            return await interaction.followup.send("⚠️ This ticket is already closed.", ephemeral=True)

        guild = interaction.guild
        closed_category = discord.utils.get(guild.categories, name="🎟️ CLOSED TICKETS")
        if not closed_category:
            closed_category = await guild.create_category("🎟️ CLOSED TICKETS")

        # Move channel to closed category and sync permissions (so it's hidden from regular members)
        await interaction.channel.edit(category=closed_category, sync_permissions=True)
        
        # 🟢 SMART TRICK: Save timestamp in channel topic for DB-free auto-deletion
        close_timestamp = int(time.time())
        current_topic = interaction.channel.topic or ""
        
        # Don't schedule deletion for GoPro tickets
        if "gopro" not in interaction.channel.name.lower():
            await interaction.channel.edit(topic=f"{current_topic} | closed_at:{close_timestamp}")
            msg = "🔒 Ticket closed. It will be permanently deleted in 48 hours."
        else:
            msg = "🔒 Pro Ticket closed. This ticket will be archived permanently for payment records."

        embed = discord.Embed(title="Ticket Closed", description=msg, color=0xe74c3c)
        await interaction.channel.send(embed=embed)

class PersistentTicketPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)

    async def create_ticket(self, interaction: discord.Interaction, ticket_type: str, prefix: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user

        # Fetch or create the Open Tickets category
        open_category = discord.utils.get(guild.categories, name="🎟️ OPEN TICKETS")
        if not open_category:
            open_category = await guild.create_category("🎟️ OPEN TICKETS")

        # Generate a short 4-character random serial to avoid DB usage
        serial = ''.join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4))
        channel_name = f"{prefix}-{user.name}-{serial}".lower()

        # Channel Permissions: Only User, Bot, and Admins can see it
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        # Create the ticket channel
        ticket_channel = await guild.create_text_channel(name=channel_name, category=open_category, overwrites=overwrites)

        # Welcome message inside the ticket
        embed = discord.Embed(
            title=f"🎫 {ticket_type}", 
            description=f"Welcome {user.mention}!\n\nPlease describe your problem or inquiry in detail below. An Admin will be with you ASAP.\n\n*Click the 🔒 button below when your issue is resolved to close this ticket.*",
            color=0x3498db
        )
        await ticket_channel.send(content=user.mention, embed=embed, view=PersistentTicketCloseView())
        await interaction.followup.send(f"✅ Ticket created successfully! Jump to your ticket here: {ticket_channel.mention}", ephemeral=True)

        # Notification to #new-support-ticket
        log_channel = discord.utils.get(guild.channels, name="new-support-ticket")
        if log_channel:
            if prefix == "gopro":
                sr_admin = discord.utils.get(guild.roles, name="Senior Admin（セィニア・アデュミン）")
                mod = discord.utils.get(guild.roles, name="Moderator")
                ping_str = f"{sr_admin.mention if sr_admin else ''} {mod.mention if mod else ''}"
                note = "\n⚠️ **NOTE:** *Only Senior Admins and Moderators are authorized to deal with Pro Subscriptions. Junior Admins should ignore this ticket.*"
            else:
                jr_admin = discord.utils.get(guild.roles, name="Junior Admin（ジュニア・アデュミン）")
                sr_admin = discord.utils.get(guild.roles, name="Senior Admin（セィニア・アデュミン）")
                ping_str = f"{jr_admin.mention if jr_admin else ''} {sr_admin.mention if sr_admin else ''}"
                note = ""

            log_embed = discord.Embed(title=f"🚨 New {ticket_type}", description=f"**User:** {user.mention}\n**Channel:** {ticket_channel.mention}\n**Type:** {ticket_type}{note}", color=0xe67e22)
            await log_channel.send(content=ping_str, embed=log_embed)

    @discord.ui.button(label="CREATE A SUPPORT TICKET", style=discord.ButtonStyle.primary, custom_id="panel_support")
    async def btn_support(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_main_role(interaction.user) and not any(r.name == "Visitor" for r in interaction.user.roles):
            return await interaction.response.send_message("❌ You need a role to create a ticket.", ephemeral=True)
        await self.create_ticket(interaction, "SUPPORT TICKET", "support")

    @discord.ui.button(label="REPORT AN INCIDENT", style=discord.ButtonStyle.danger, custom_id="panel_incident")
    async def btn_incident(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Strictly N5 to N1 (No Visitors allowed)
        if not any(r.name in ROLE_NAMES for r in interaction.user.roles):
            return await interaction.response.send_message("❌ Only official JP Learners (N5-N1) can report incidents. Visitors cannot use this feature.", ephemeral=True)
        await self.create_ticket(interaction, "INCIDENT REPORT", "incident")

    @discord.ui.button(label="GO PRO", style=discord.ButtonStyle.success, emoji="💱", custom_id="panel_gopro")
    async def btn_gopro(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.create_ticket(interaction, "PRO ENQUIRY", "GoPro")

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
        1. CONTEXT-RICH TEXT ONLY: The sentence MUST provide enough logical context to be solved purely through reading, without any images or audio.
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
            #... existing tasks ...
        self.weekly_leaderboard_loop.start()
        self.daily_kanji_loop.start()
        self.freemium_dokkai_loop.start()
        self.ticket_cleanup_loop.start() # 🟢 Start ticket auto-deletion loop
            #... existing views ...
        self.add_view(WelcomeView())
        self.add_view(PersistentJournalView())
        self.add_view(PersistentFreemiumStoryView()) 
        self.add_view(PersistentDailyKanjiView())    
        self.add_view(PersistentTicketPanelView())
        self.add_view(PersistentTicketCloseView())
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
            
            if not getattr(self, 'weekly_reset_done', False):
                self.weekly_reset_done = True
                quiz_db.delete_many({}) 
                print("✅ 10:01 PM JST: All weekly leaderboard data wiped to save memory.")
                
        elif now_jst.weekday() == 6 and now_jst.hour == 22 and now_jst.minute == 2:
            self.weekly_reset_done = False

    @tasks.loop(minutes=1)
    async def daily_kanji_loop(self):
        now_jst = datetime.now(pytz.timezone('Asia/Tokyo'))
        
        if now_jst.hour == 9 and now_jst.minute == 0:
            if not getattr(self, 'daily_kanji_done', False):
                self.daily_kanji_done = True
                await self.drop_kanjis_task()
        elif now_jst.hour == 9 and now_jst.minute == 1:
            self.daily_kanji_done = False

    async def drop_kanjis_task(self):
        level_configs = [
            ("N5", N5_KANJI, 1, "n5-daily-kanji"),
            ("N4", N4_KANJI, 1, "n4-daily-kanji"),
            ("N3", N3_KANJI, 1, "n3-daily-kanji"),
            ("N2", N2_KANJI, 2, "n2-daily-kanji"),
            ("N1", N1_KANJI, 3, "n1-daily-kanji")
        ]

        for guild in self.guilds:
            for lvl_name, kanji_list, drop_count, ch_keyword in level_configs:
                channel = discord.utils.find(lambda c: ch_keyword in c.name.lower(), guild.channels)
                if not channel: 
                    continue

                data = kanji_db.find_one({"level": lvl_name})
                current_index = data["current_index"] if data else 0

                if current_index >= len(kanji_list):
                    current_index = 0
                
                revision_kanjis = []
                if current_index >= drop_count:
                    revision_kanjis = kanji_list[current_index - drop_count : current_index]
                elif current_index > 0:
                    revision_kanjis = kanji_list[0 : current_index]

                new_kanjis = kanji_list[current_index : current_index + drop_count]
                
                if not new_kanjis: 
                    current_index = 0
                    new_kanjis = kanji_list[current_index : current_index + drop_count]

                next_index = current_index + len(new_kanjis)
                kanji_db.update_one({"level": lvl_name}, {"$set": {"current_index": next_index}}, upsert=True)

                kanji_str = ", ".join(new_kanjis)
                prompt = f"""You are an expert Japanese Sensei. Explain ALL of the following {len(new_kanjis)} Kanji(s): {kanji_str}.
                For EACH Kanji, strictly provide:
                1. Meaning
                2. Onyomi & Kunyomi (with Romaji)
                3. One example of each kanji in a word with Onyomi & Kunyomi pronunciation being used (with romaji).
                Format the entire response beautifully in Discord Markdown using headings and bullet points. Keep the script entirely in English. Do NOT include memory or pronunciation tricks."""
                
                try:
                    explanation = await generate_gemini_response(prompt)
                    
                    rev_text = f"**🔄 Yesterday's Revision:** {', '.join(revision_kanjis)}\n\n" if revision_kanjis else ""
                    
                    embed = discord.Embed(
                        title=f"㊗️ {lvl_name} Daily Kanji Drop!",
                        description=f"{rev_text}{explanation}"[:4096],
                        color=0xe74c3c
                    )
                    embed.set_footer(text=f"Kanji: {kanji_str} | Level: {lvl_name}")
                    
                    view = PersistentDailyKanjiView()
                    await channel.send(embed=embed, view=view)

                except Exception as e:
                    print(f"❌ Error dropping {lvl_name} Kanji: {e}")
                await asyncio.sleep(5)

    @tasks.loop(minutes=1)
    async def freemium_dokkai_loop(self):
        now_jst = datetime.now(pytz.timezone('Asia/Tokyo'))
        
        # 🟢 NEW: Fixed exactly at 6:00 PM (18:00) JST to prevent restart spam
        if now_jst.hour == 18 and now_jst.minute == 0:
            if not getattr(self, 'daily_dokkai_done', False):
                self.daily_dokkai_done = True
                await self.drop_freemium_dokkai_task()
        elif now_jst.hour == 18 and now_jst.minute == 1:
            self.daily_dokkai_done = False

    async def drop_freemium_dokkai_task(self):
        topics = ["Japanese Culture", "A Sci-Fi Adventure", "A Slice of Life moment", "A Mystery", "Japanese Food", "Folklore", "School Life"]
        topic = random.choice(topics)
        
        level_configs = [
            ("N5", "n5-daily-dokkai"),
            ("N4", "n4-daily-dokkai"),
            ("N3", "n3-daily-dokkai"),
            ("N2", "n2-daily-dokkai"),
            ("N1", "n1-daily-dokkai")
        ]
        
        for guild in self.guilds:
            for lvl_name, ch_keyword in level_configs:
                channel = discord.utils.find(lambda c: ch_keyword in c.name.lower(), guild.channels)
                if not channel:
                    continue
                    
                prompt = f"""You are an expert Japanese linguist and JLPT examiner.
                Task: Generate a short narrative (max 300 Japanese characters) about '{topic}'.
                Constraint 1: Strictly use ONLY vocabulary and grammar points from JLPT levels N5 up to {lvl_name}.
                Constraint 2: Do not use complex Kanji outside of the specified JLPT level unless furigana is provided in parenthesis.
                Output Format: Return ONLY valid JSON with exactly two keys: "title" and "story_content". Do not add trailing commas."""
                
                try:
                    raw_text = await generate_gemini_response(prompt)
                    story_data = extract_json(raw_text)
                    
                    if isinstance(story_data, list):
                        story_data = story_data[0]

                    title = story_data.get("title", f"{lvl_name} Daily Reading")
                    content = story_data.get("story_content", "Could not generate story.")

                    embed = discord.Embed(title=f"🎁 Daily Free Reading: {title}", description=content, color=0x3498db)
                    embed.set_footer(text=f"Level: {lvl_name} | Topic: {topic}")
                    
                    view = PersistentFreemiumStoryView()
                    await channel.send(embed=embed, view=view)
                    
                except Exception as e:
                    print(f"❌ Error dropping {lvl_name} Dokkai: {e}")
                    
                await asyncio.sleep(5)

    #Loop for deleting support tickets automatically after 48 hours, except Go Pro queries.
    @tasks.loop(hours=1)
    async def ticket_cleanup_loop(self):
        # Runs every hour to check for 48h old closed tickets
        for guild in self.guilds:
            closed_category = discord.utils.get(guild.categories, name="🎟️ CLOSED TICKETS")
            if not closed_category:
                continue
                
            for channel in closed_category.channels:
                if not channel.topic or "closed_at:" not in channel.topic:
                    continue
                
                try:
                    # Extract timestamp from topic
                    topic_data = channel.topic.split("closed_at:")
                    closed_timestamp = int(topic_data[-1].strip())
                    
                    # 48 hours = 172800 seconds
                    if time.time() - closed_timestamp >= 172800:
                        await channel.delete(reason="Ticket auto-deletion after 48 hours.")
                except Exception as e:
                    print(f"Error deleting ticket channel: {e}")

bot = NihongoBot()

# --- ⌨️ COMMANDS ---
@bot.tree.command(name="help", description="Shows bot commands.")
async def help_command(interaction: discord.Interaction):
    if "bot-commands" not in interaction.channel.name.lower(): return await interaction.response.send_message("❌ Use `#🤖・bot-commands`.", ephemeral=True)
    await interaction.response.send_message(embed=discord.Embed(title="🤖 Commands", description="🧠 `/quiz` - JLPT Mock Test\n🏆 `/leaderboard` - Check weekly standings\n⬆️ `/changerole` - Upgrade your Japanese level\n📢 `/leaderboardannounce` - [Admin] Announce results manually", color=0xffb6c1), ephemeral=True)

@bot.tree.command(name="leaderboard", description="[Admin Only] Check the Top 5 performing players for a specific level.")
@app_commands.choices(target_level=[app_commands.Choice(name=r.split(" ", 1)[1], value=r) for r in ROLE_NAMES])
async def leaderboard(interaction: discord.Interaction, target_level: app_commands.Choice[str]):
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
    1. CONTEXT-RICH TEXT ONLY: The sentence MUST provide enough logical context to be solved purely through reading, without any images or audio.
    2. NO VISUAL QUESTIONS: NEVER generate vague questions like "___は何ですか。" or "これは___です。"
    3. Each question MUST have exactly one blank space represented by '___'.
    4. When using kanjis write furigana in ([]) square brackets just after the word ends.
    5. The 4 options (A, B, C, D) must be logically distinct, but ONLY ONE fits grammatically and semantically. Always shuffle the options for each question.
    6. CRITICAL JSON RULE: Use strictly double quotes (") for all keys and string values. Do not use single quotes. Do not add trailing commas.
    
    Output ONLY a valid JSON array of 1 objects. DO NOT output any other text or markdown outside the JSON array."""
    
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
    1. CONTEXT-RICH TEXT ONLY: The sentence MUST provide enough logical context to be solved purely through reading.
    2. NO VISUAL QUESTIONS: NEVER generate vague questions like "___は何ですか。" or "これは___です。"
    3. Each question MUST have exactly one blank space represented by '___'.
    4. {furigana_rule}
    5. The 4 options (A, B, C, D) must be logically distinct, but ONLY ONE fits grammatically and semantically. Always shuffle the options.
    6. CRITICAL JSON RULE: Use strictly double quotes (") for all keys and string values. Do not use single quotes. Do not add trailing commas.
    
    Output ONLY a valid JSON array of 20 objects. DO NOT output any other text or markdown outside the JSON array."""
    
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

@bot.tree.command(name="read", description="[Premium] Generate a personalized Japanese short story based on your JLPT level.")
@app_commands.describe(topic="What should the story be about? (e.g., Cyberpunk, Romance, Tokyo Trip)")
async def read(interaction: discord.Interaction, topic: str):
    has_pro = any(r.name == "金 Pro Learners 金" for r in interaction.user.roles)
    if not has_pro:
        embed = discord.Embed(
            title="🔒 Premium Feature Locked", 
            description="Oops! This feature is exclusively available for our **金 Pro Learners 金**.\n\nUnlock the full potential of your Japanese journey with unlimited personalized AI stories, deep grammar analysis, and much more! Upgrade today to access this and other pro tools. ✨", 
            color=0xf1c40f
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    user_level_role = next((r.name for r in interaction.user.roles if r.name in ROLE_NAMES), None)
    if not user_level_role:
        return await interaction.response.send_message("❌ Please select a JLPT level first using the welcome channel or `/changerole`.", ephemeral=True)
    
    level_short = user_level_role.split(" ")[1] 
    await interaction.response.defer(ephemeral=False) 

    prompt = f"""You are an expert Japanese linguist and JLPT examiner.
    Task: Generate a short narrative (max 300 Japanese characters) about '{topic}'.
    Constraint 1: Strictly use ONLY mix of vocabulary and grammar points from JLPT levels N5 up to {level_short}.
    Constraint 2: Do not use complex Kanji outside of the specified JLPT level unless furigana is provided in parenthesis.
    Output Format: Return ONLY valid JSON with exactly two keys: "title" (the story title) and "story_content" (the Japanese story). Do not add trailing commas."""
    
    try:
        raw_text = await generate_gemini_response(prompt)
        story_data = extract_json(raw_text)
        if isinstance(story_data, list):
            story_data = story_data[0]

        title = story_data.get("title", f"{level_short} Story")
        content = story_data.get("story_content", "Could not generate story.")

        embed = discord.Embed(title=f"🎌 {title}", description=content, color=0x9b59b6)
        embed.set_footer(text=f"Level: {level_short} | Topic: {topic}")
        
        view = StoryReaderView(interaction.user, level_short, content)
        await interaction.followup.send(embed=embed, view=view)

    except Exception as e:
        await interaction.followup.send(f"❌ Failed to generate story: {e}")

@bot.tree.command(name="setupjournal", description="[Admin Only] Drop the Premium Journaling button in the current channel.")
async def setup_journal(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    has_permission = any(role.name in ["Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied. Premium Feature setup command. Can be used by Senior Admins.", ephemeral=True)
    
    embed = discord.Embed(title="📓 Premium Guided Journaling", description="Welcome to your personal AI Sensei! Click the button below to submit your daily Japanese journal (Max 4000 chars).\n\nAI Sensei will evaluate your text against your current JLPT level, provide a native-sounding correction, and open a private thread for you to explore grammar, vocab, and reading guides.", color=0x3498db)
    
    await interaction.channel.send(embed=embed, view=PersistentJournalView())
    await interaction.followup.send("✅ Journal button deployed successfully!", ephemeral=True)

@bot.tree.command(name="scenario", description="[Premium] Spawn an interactive Japanese nuance scenario.")
async def scenario(interaction: discord.Interaction):
    has_pro = any(r.name == "金 Pro Learners 金" for r in interaction.user.roles)
    is_correct_channel = "scenario-based-learning" in interaction.channel.name.lower()
    
    if not has_pro or not is_correct_channel:
        embed = discord.Embed(
            title="🔒 Premium Feature or Wrong Channel", 
            description="Oops! To use this feature, you must be a **金 Pro Learners 金** AND use the command exclusively in the `#🧟‍♀️・scenario-based-learning` channel.\n\nUpgrade to Pro to unlock advanced real-world scenario drills!", 
            color=0xf1c40f
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    
    await interaction.response.defer(ephemeral=True) 
    user_level_role = next((r.name for r in interaction.user.roles if r.name in ROLE_NAMES), None)
    level_short = user_level_role.split(" ")[1] if user_level_role else "N3"

    prompt = f"""You are an expert Japanese linguist. The user is at the JLPT {level_short} level.
    Create a highly engaging, realistic, and tense real-world situation in English. Examples of good themes: dealing with a strict police officer for a visa check, managing a dispute with a foreign Airbnb guest, an intense corporate IT rollout meeting, coordinating a tactical push in a multiplayer FPS game, or a technical debate about car maintenance.
    
    Provide exactly 4 Japanese responses the user could say. Only ONE is contextually and pragmatically appropriate for the formality and nuance of the situation. The other 3 should be grammatically similar but contextually wrong (rude, unnatural, or wrong nuance). 
    CRITICAL RULES:
    1. Keep Japanese vocabulary strictly within {level_short} or below.
    2. If ANY Kanji is used in the options, you MUST provide its furigana in square brackets exactly after the Kanji (e.g., 毎日[まいにち]).
    3. If SAME Kanji is repeated in another option, then just provide furigana only once in first option.
    4. The EXAMPLES of interesting scenarios are just to provide context of how interesting and realistic the situations must be. Just take inspiration from examples and don't just repeat the scenarios or examples.
    
    Output ONLY valid JSON format exactly like this:
    {{
        "situation": "The English scenario description (max 3 sentences).",
        "options": [
            {{"label": "Japanese response 1", "explanation": "Why this is correct/wrong and its nuance.", "is_correct": false}},
            {{"label": "Japanese response 2", "explanation": "Why this is correct/wrong and its nuance.", "is_correct": true}},
            {{"label": "Japanese response 3", "explanation": "Why this is correct/wrong and its nuance.", "is_correct": false}},
            {{"label": "Japanese response 4", "explanation": "Why this is correct/wrong and its nuance.", "is_correct": false}}
        ]
    }}"""
    
    try:
        raw_text = await generate_gemini_response(prompt)
        data = extract_json(raw_text)
        if isinstance(data, list): data = data[0]
        
        situation = data.get("situation", "Scenario generation failed.")
        options_data = data.get("options", [])
        
        if not options_data:
            raise ValueError("No options generated.")
            
        options_display = ""
        emojis = ["🇦", "🇧", "🇨", "🇩"]
        for idx, opt in enumerate(options_data):
            options_display += f"{emojis[idx]} **{opt['label']}**\n\n"
            
        embed = discord.Embed(title=f"🎯 {level_short} Nuance Simulator", description=f"**Situation:**\n{situation}\n\n**Options:**\n{options_display}*Select your answer (A, B, C, or D) from the dropdown below!*", color=0xe67e22)
        embed.set_author(name=f"Requested by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        
        view = ScenarioView(options_data)
        
        await interaction.channel.send(embed=embed, view=view, delete_after=86400)
        await interaction.followup.send("✅ Scenario generated successfully in the channel! It will automatically disappear in 24 hours.", ephemeral=True)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to generate scenario: {e}", ephemeral=True)

#Reset Kanji Counter for daily kanji drops
@bot.tree.command(name="resetkanji", description="[Admin Only] Reset Daily Kanji tracker.")
async def reset_kanji(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not any(r.name in ["Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for r in interaction.user.roles): 
        return await interaction.followup.send("❌ Access Denied.", ephemeral=True)
    kanji_db.delete_many({})
    await interaction.followup.send("✅ Kanji tracker reset to Day 1.", ephemeral=True)
    
#Setup Ticket option in 🎫・create-a-ticket channel.
@bot.tree.command(name="setuptickets", description="[Admin Only] Drop the Support Ticket panel in the current channel.")
async def setup_tickets(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    has_permission = any(role.name in ["Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied. Only Senior Admins can setup the ticket panel.", ephemeral=True)
    
    if "create-a-ticket" not in interaction.channel.name.lower():
        return await interaction.followup.send("❌ Please use this command in the `#🎫・create-a-ticket` channel.", ephemeral=True)
    
    embed = discord.Embed(
        title="🎫 Server Support & Enquiries", 
        description="Need help? Click a button below to open a ticket. Please choose correct option as needed.\n\n"
                    "🟦 **SUPPORT TICKET:** General help, bot issues, or server queries.\n"
                    "🟥 **REPORT AN INCIDENT:** Report rule-breaking or severe glitches (Learner Roles Only).\n"
                    "🟩 **GO PRO:** Enquire about or purchase the Pro Subscription and get access to exclusive features.", 
        color=0x2c3e50
    )
    
    await interaction.channel.send(embed=embed, view=PersistentTicketPanelView())
    await interaction.followup.send("✅ Ticket panel deployed successfully!", ephemeral=True)

#Announce command for admin announcement in server
@bot.tree.command(name="announce", description="[Admin Only] Send an official announcement in the current channel.")
@app_commands.describe(message="The announcement message you want to broadcast.")
async def announce(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True)
    
    # 1. Strict Role Check
    has_permission = any(role.name in ["Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied. Only Senior Admins and Founders can make official announcements.", ephemeral=True)
    
    # 2. 🟢 THE PING FIX: Extract all mentions from the message
    # Discord embeds do NOT ping users/roles. We must put them in the regular message content.
    mentions = re.findall(r'<@!?\d+>|<@&\d+>|@everyone|@here', message)
    ping_content = " ".join(mentions) if mentions else ""
    
    # 3. Build the visually appealing Embed
    announcement_text = f"{message}\n\n*~ sent on behalf of {interaction.user.mention}*"
    
    embed = discord.Embed(
        title="📢 Official Announcement", 
        description=announcement_text, 
        color=0xf1c40f # Premium Golden color
    )
    
    if interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
        
    embed.set_footer(text="にほご学習者の社会 ~Server Updates")
    
    # 4. Send mentions as content (to trigger notifications) and the aesthetic Embed
    await interaction.channel.send(content=ping_content, embed=embed)
    
    await interaction.followup.send("✅ Announcement broadcasted, mentioned roles have been notified!", ephemeral=True)
    

bot.run(os.environ.get("BOT_TOKEN"))
