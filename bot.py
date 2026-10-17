from dotenv import load_dotenv
load_dotenv()
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
from pydub import AudioSegment
import requests
import speech_recognition as sr
from difflib import SequenceMatcher
from gtts import gTTS
import os
import base64 #Dependency for AI Vision (used in Vocab-Vault)
import subprocess # This and next 2 dependency for /updatebot command
import sys
import os

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
    # 🟢 Token Tracking Collection
    token_db = db["api_tokens"]
    # 🟢 For storing user flashcards (Vocab Vault)
    vocab_db = db["vocab_vault"]
    print("✅ MongoDB Connected!")
except Exception as e:
    print(f"❌ MongoDB Error: {e}")

# 🟢 API Key Rotation Setup
API_KEYS = []
for i in range(1, 20):
    k = os.environ.get(f"GEMINI_API_KEY_{i}")
    if k: 
        API_KEYS.append(k.strip())
if not API_KEYS and os.environ.get("GEMINI_API_KEY"):
    API_KEYS.append(os.environ.get("GEMINI_API_KEY").strip())

CURRENT_KEY_INDEX = 0

# --- 🎌 ROLE CONSTANTS ---
ROLE_NAMES = ["📍 N5 Beginner", "📍 N4 Elementary", "📍 N3 Intermediate", "📍 N2 Pre-Advanced", "📍 N1 Advanced"]
GREETINGS_LIST = [
    "Irasshaimase! 🌸", "Konnichiwa! ✨", "Yokoso! 🎌", "Hajimemashite! ⛩️",
    "Okaerinasai! 🏡", "Ohayou gozaimasu! 🌅", "Konbanwa! 🌙", 
    "Yoroshiku onegaishimasu! 🤝", "Kangei shimasu! 🎊", "Welcome to the world of Japanese! 🗾"
]

def has_main_role(member):
    return any(r.name in ROLE_NAMES or r.name in ["Visitor", "📍 Native Japanese"] for r in member.roles)

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

# 🟢 503 Error Tracker Global Dictionary
error_503_tracker = {"count": 0, "first_time": 0, "last_reported": 0}

async def trigger_admin_503_alert():
    try:
        if len(bot.guilds) > 0:
            guild = bot.guilds[0]
            log_ch = discord.utils.get(guild.channels, name="admin-logs")
            if log_ch:
                founder = discord.utils.get(guild.roles, name="Founder（ファウンダ）")
                sr_admin = discord.utils.get(guild.roles, name="Senior Admin（セィニア・アデュミン）")
                jr_admin = discord.utils.get(guild.roles, name="Junior Admin（ジュニア・アデュミン）")
                
                ping_str = f"{founder.mention if founder else ''} {sr_admin.mention if sr_admin else ''} {jr_admin.mention if jr_admin else ''}"
                embed = discord.Embed(
                    title="🚨 AI Server Overload (Error 503)", 
                    description="Google's Gemini API has crashed/overloaded **3 times in the last 1 minute!**\n\n*Note: Users are currently seeing a funny 'Brain Fried' message instead of the raw code. This is a Google-side issue, please wait for the traffic to reduce. Restarting the bot will NOT fix this, they just need to simply wait for sometime.*", 
                    color=0xe74c3c
                )
                bot.loop.create_task(log_ch.send(content=ping_str, embed=embed))
    except Exception as e:
        print(f"Failed to send 503 admin alert: {e}")

# --- 🧠 DIRECT GEMINI REST API ---
async def generate_gemini_response(prompt):
    global CURRENT_KEY_INDEX, error_503_tracker
    if not API_KEYS: 
        raise Exception("No GEMINI_API_KEY found in .env!")
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": 6000}
    }
    headers = {"Content-Type": "application/json"}
    backoff_times = [1, 2, 4, 8]
    
    async with aiohttp.ClientSession() as session:
        for attempt, wait_time in enumerate(backoff_times + [0]):
            current_key = API_KEYS[CURRENT_KEY_INDEX]
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={current_key}"
            
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    try:
                        usage = data.get('usageMetadata', {})
                        total_tokens = usage.get('totalTokenCount', 0)
                        
                        today_pst = datetime.now(pytz.timezone('US/Pacific')).strftime("%Y-%m-%d")
                        token_db.update_one(
                            {"date": today_pst}, 
                            {"$inc": {"requests": 1, "tokens": total_tokens}}, 
                            upsert=True
                        )
                    except Exception as e:
                        print(f"Token Tracking Error: {e}")
                        
                    try: 
                        return data['candidates'][0]['content']['parts'][0]['text']
                    except: 
                        raise Exception("API returned invalid structure.")
                    
                elif resp.status == 429:
                    print(f"⚠️ Key {CURRENT_KEY_INDEX + 1} Limit Hit (429)! Rotating Key...")
                    CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % len(API_KEYS)
                    if attempt < len(backoff_times):
                        await asyncio.sleep(wait_time)
                        continue
                        
                elif resp.status in [503, 500]:
                    # 🟢 503 Tracker Logic
                    now = time.time()
                    if now - error_503_tracker["first_time"] > 60:
                        error_503_tracker["count"] = 1
                        error_503_tracker["first_time"] = now
                    else:
                        error_503_tracker["count"] += 1
                        
                    # If 3 times in 1 minute, and hasn't reported in the last 2 minutes
                    if error_503_tracker["count"] >= 3 and (now - error_503_tracker["last_reported"] > 120):
                        error_503_tracker["last_reported"] = now
                        await trigger_admin_503_alert()
                        
                    # 🟢 Funny User-Facing Error
                    raise Exception("🤯 **Sensei's brain is frying after reading so many kanjis (AI is overloaded). Count to 10 and retry the same command again.** 🍵 *少し待ってから、もう一度やり直してください!*")
                    
                else:
                    err_text = await resp.text()
                    raise Exception(f"HTTP {resp.status}: {err_text}")

# --- 👁️ AI VISION ENGINE (For Vocab Vault) ---
async def generate_vision_response(prompt, image_url):
    global CURRENT_KEY_INDEX
    if not API_KEYS: 
        raise Exception("No GEMINI_API_KEY found!")
        
    async with aiohttp.ClientSession() as session:
        async with session.get(image_url) as img_resp:
            if img_resp.status != 200:
                raise Exception("Could not download image from Discord.")
            img_bytes = await img_resp.read()
            
        img_b64 = base64.b64encode(img_bytes).decode('utf-8')
        
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}}
                ]
            }],
            "generationConfig": {"temperature": 0.3} 
        }
        headers = {"Content-Type": "application/json"}
        
        current_key = API_KEYS[CURRENT_KEY_INDEX]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={current_key}"
        
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                candidate = data.get('candidates', [{}])[0]
                
                # 🟢 NAYA: Google's Auto NSFW/Safety Filter Check
                if candidate.get('finishReason') == 'SAFETY':
                    raise Exception("NSFW_FLAGGED")
                    
                return candidate['content']['parts'][0]['text']
            elif resp.status == 429:
                CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % len(API_KEYS)
                raise Exception("Rate limit hit. Retrying soon.")
            else:
                err_text = await resp.text()
                raise Exception(f"Vision API Error: {err_text}")

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
    def __init__(self, user, question_data, target_role_name, is_changerole=False, is_native=False):
        super().__init__(timeout=60)
        self.user, self.q, self.target_role = user, question_data, target_role_name
        self.is_changerole = is_changerole
        self.is_native = is_native 
        
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
            elif self.is_native:
                embed = discord.Embed(title="⚠️ Native Placement Test Failed", description=f"The correct answer was **{self.q['answer']}**.\n\nYou did not pass the Native Japanese test. Please choose your path again below to retry or select a different option.", color=discord.Color.red())
                await interaction.response.edit_message(content="", embed=embed, view=WelcomeView())
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

async def generate_kanji_quiz_data(level_short, kanjis_current, kanjis_lower, force_limit=10):
    # 🟢 70% Current Level Kanji, 30% Lower Levels
    current_limit = int(force_limit * 0.70)
    
    if kanjis_lower:
        selected_kanjis = random.sample(kanjis_current, min(current_limit, len(kanjis_current)))
        remaining_slots = force_limit - len(selected_kanjis)
        selected_kanjis += random.sample(kanjis_lower, min(remaining_slots, len(kanjis_lower)))
    else:
        # N5 ke liye sirf unka level hi aayega
        selected_kanjis = random.sample(kanjis_current, min(force_limit, len(kanjis_current)))
        
    random.shuffle(selected_kanjis)
    kanji_str = ", ".join(selected_kanjis)

    prompt = f"""You are an expert JLPT Examiner. Generate exactly {force_limit} multiple-choice Kanji Reading questions for JLPT {level_short}.
    CRITICAL RULES:
    1. You MUST use ONLY words formed from these specific Kanjis: {kanji_str}.
    2. The 'question' MUST be visually large using markdown. Format it exactly like this: "What is the correct reading for this word?\\n# [Insert Kanji Word Here]". Do NOT use bold markdown (**) around the Kanji.
    3. The 4 options (A, B, C, D) MUST be strictly in 100% Hiragana. Do not use Romaji or English.
    4. NO DUPLICATES: All 4 options MUST be completely different strings. Do NOT generate identical options. The 3 distractors should be genuine words in japanese language, if possible related to kanji meaning but as said not same as the correct answer.
    5. Provide a very brief English 'explanation' of the meaning of the Kanji word.
    6. CRITICAL JSON RULE: Use strictly double quotes (") for all keys and string values. Output ONLY a valid JSON array of objects.

    Output format:
    [{{
        "question": "What is the correct reading for this word?\\n# 毎日",
        "options": {{"A": "まいにち", "B": "まいんち", "C": "まにち", "D": "まんち"}},
        "answer": "A",
        "explanation": "毎日 (まいにち) means 'every day'."
    }}]"""
    
    raw_text = await generate_gemini_response(prompt)
    questions_data = extract_json(raw_text)
    if not questions_data or len(questions_data) == 0:
        raise ValueError("No Kanji questions generated.")
        
    for q in questions_data:
        try:
            correct_val = q['options'][q['answer']]
            vals = list(q['options'].values())
            random.shuffle(vals)
            labels = ["A", "B", "C", "D"]
            new_options = {}
            for idx, val in enumerate(vals):
                new_options[labels[idx]] = val
                if val == correct_val:
                    q['answer'] = labels[idx]
            q['options'] = new_options
        except KeyError:
            continue
            
    return questions_data
    
async def generate_quiz_data(level_full, is_grammar=False, topic="", force_limit=10):
    level_short = level_full.split(" ")[1] 
    prompt = f"You are an expert JLPT Examiner. Generate exactly {force_limit} multiple-choice questions for JLPT {level_short}."
    
    if is_grammar:
        prompt += f"""
    CRITICAL GRAMMAR TASK: You are testing the user purely on this specific grammar topic(s): '{topic}'.
    ANTI-EXPLOIT RULES FOR OPTIONS:
    1. THE CONTEXTUAL TRAP: The user must NOT be able to guess the correct answer just by looking for the requested topic. They MUST read the sentence context to solve it.
    2. SMART DISTRACTORS: The 3 incorrect options (A, B, C, D) MUST be either:
       - The EXACT same root word but with different, confusing conjugations (e.g., if testing Passive, the options must be Passive, Causative, Causative-Passive, and Active forms of the same verb).
       - Similar or commonly confused JLPT {level_short} grammar structures that look grammatically plausible but completely fail logically in the given sentence.
    3. Make it challenging. The difference between the right and wrong answers should rely strictly on reading comprehension and nuanced grammar rules."""
    else:
        prompt += f"\nGenerate a mix of Grammar and Vocabulary questions."
        
    prompt += """
    Strict Rules:
    1. CONTEXT-RICH TEXT ONLY: The sentence MUST provide enough logical context to be solved purely through reading.
    2. NO VISUAL QUESTIONS.
    3. Each question MUST have exactly one blank space represented by '___'.
    4. Use Furigana in brackets after all Kanji in questions and options (e.g., 漢字【かんじ】).
    5. The 4 options (A, B, C, D) must be logically distinct, but ONLY ONE fits.
    6. CRITICAL JSON RULE: Use strictly double quotes (") for all keys and string values. Output ONLY a valid JSON array of objects.
    
    Output format must EXACTLY match this structure:
    [{
        "question": "...",
        "options": {"A": "...", "B": "...", "C": "...", "D": "..."},
        "answer": "A",
        "explanation": "Provide a very brief 1-liner English explanation of why this answer is grammatically or semantically correct."
    }]"""
    
    raw_text = await generate_gemini_response(prompt)
    questions_data = extract_json(raw_text)
    if not questions_data or len(questions_data) == 0:
        raise ValueError("No questions generated.")
        
    # 🟢 STRICT PYTHON SHUFFLING LOGIC (Breaks the Option 'A' Pattern)
    for q in questions_data:
        try:
            correct_val = q['options'][q['answer']]
            vals = list(q['options'].values())
            random.shuffle(vals)
            labels = ["A", "B", "C", "D"]
            new_options = {}
            for idx, val in enumerate(vals):
                new_options[labels[idx]] = val
                if val == correct_val:
                    q['answer'] = labels[idx]
            q['options'] = new_options
        except KeyError:
            continue
            
    return questions_data

class QuizView(View):
    def __init__(self, user, questions, level, start_time, time_limit_per_q=60):
        super().__init__(timeout=time_limit_per_q * len(questions)) 
        self.user = user
        self.questions = questions
        self.level = level
        self.current_idx = 0
        self.score = 0
        self.start_time = start_time
        self.last_click = time.time() 
        self.total_q = len(questions)
        self.time_limit_per_q = time_limit_per_q # 🟢 Naya dynamic timer
        self.is_fallback = (self.total_q == 1 and "Formatting Error" in questions[0].get("question", ""))
        self.user_choices = [] 
        
        for label in ["A", "B", "C", "D"]:
            btn = Button(label=label, custom_id=label, style=discord.ButtonStyle.primary)
            btn.callback = self.button_callback
            self.add_item(btn)

    async def button_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id: 
            return await interaction.response.send_message("❌ Not your quiz!", ephemeral=True)
            
        # 🟢 Naya Anti-Cheat Strict Timer Check
        if time.time() - self.last_click > self.time_limit_per_q:
            await interaction.response.edit_message(content="⏳ **Time's up! quiz ended, please retry as partial score doesn't gets saved.**", embed=None, view=None)
            self.stop()
            return
            
        self.last_click = time.time() 
        
        # 🟢 Record the user's choice
        self.user_choices.append(interaction.data['custom_id'])
            
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
                return await interaction.response.edit_message(content="⚠️ An AI generation error occurred. Your score was not saved. Please try `/quiz` again.", embed=None, view=None)
                
            # 🟢 FIX to Prevent DuplicateKeyError when user changes JLPT level
            quiz_db.update_one(
                {"_id": self.user.id}, 
                {"$set": {"level": self.level}, "$inc": {"score": self.score, "time_taken": time_taken}}, 
                upsert=True
            )
            
            embed = discord.Embed(title="🏁 Quiz Completed!", description=f"Score: **{self.score}/{self.total_q}** in {time_taken}s.", color=discord.Color.green())
            await interaction.response.edit_message(embed=embed, view=None)
            
            # 🟢 NAYA LOGIC: Detailed Ephemeral Explanation
            has_explanations = any("explanation" in q for q in self.questions)
            if has_explanations:
                # 🟢 NAYA LOGIC: Title ab dynamic hoga
                quiz_type = "Listening" if "Listening" in self.level else "Mock Test"
                exp_embed = discord.Embed(title=f"📖 {quiz_type} Review & Explanations", color=0x3498db)
                for i, q in enumerate(self.questions):
                    user_ans = self.user_choices[i]
                    correct_ans = q['answer']
                    mark = "✅ Correct" if user_ans == correct_ans else f"❌ Incorrect (Answer was {correct_ans})"
                    
                    exp_embed.add_field(
                        name=f"Q{i+1}: You chose {user_ans} | {mark}", 
                        value=f"*{q.get('explanation', 'No explanation provided.')}*", 
                        inline=False
                    )
                try: await interaction.followup.send(embed=exp_embed, ephemeral=True)
                except: pass
            
            gopro_ch = discord.utils.get(interaction.guild.channels, name="💎・go-pro")
            ch_mention = gopro_ch.mention if gopro_ch else "#💎・go-pro"
            promo_msg = f"Thank you for completing the quiz, Result saved! 🎯\nKeep doing quizzes to top the leaderboard (results every Sunday).\n\nAlso visit {ch_mention} to unlock **Pro Mode** of the quiz and earn more points in one go! ✨"
            try: await interaction.followup.send(promo_msg, ephemeral=True)
            except: pass

    async def on_timeout(self):
        try: 
            if hasattr(self, 'message') and self.message:
                await self.message.edit(content="⏳ **Session Expired!** You took too long to complete the quiz.", view=None, embed=None)
        except: pass

class GrammarWarningView(View):
    def __init__(self, user, level_full, topic):
        super().__init__(timeout=60)
        self.user = user
        self.level_full = level_full
        self.topic = topic

    @discord.ui.button(label="Yes, Continue (5 Qs)", style=discord.ButtonStyle.danger)
    async def btn_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        await interaction.response.edit_message(content=f"⏳ Generating 5 challenging questions for **{self.topic}**...", embed=None, view=None)
        try:
            q_data = await generate_quiz_data(self.level_full, is_grammar=True, topic=self.topic, force_limit=5)
            q = q_data[0]
            embed = discord.Embed(title=f"🎌 {self.level_full} Mock Test (1/{len(q_data)})", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x3498db)
            view = QuizView(self.user, q_data, self.level_full, time.time())
            await interaction.edit_original_response(content="", embed=embed, view=view)
            view.message = interaction.message
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ AI Fetch Error: {e}", embed=None, view=None)

    @discord.ui.button(label="No, Cancel", style=discord.ButtonStyle.secondary)
    async def btn_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        await interaction.response.edit_message(content="✅ Quiz cancelled.", embed=None, view=None)

class GrammarTopicModal(discord.ui.Modal, title='Grammar Practice'):
    topic_input = discord.ui.TextInput(
        label='Enter Grammar Topic(s)',
        style=discord.TextStyle.short,
        placeholder='e.g., Te-form, Passive, Causative...',
        max_length=100
    )

    def __init__(self, user, level_full):
        super().__init__()
        self.user = user
        self.level_full = level_full

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        topic = self.topic_input.value
        level_short = self.level_full.split(" ")[1]
        
        prompt = f"The user is currently studying for JLPT {level_short}. They want to practice the grammar concept(s): '{topic}'. Is this concept strictly within or below {level_short} syllabus? Reply ONLY with True or False."
        try:
            ai_verification = await generate_gemini_response(prompt)
            is_valid = "true" in ai_verification.lower()
            
            if not is_valid:
                role = discord.utils.get(interaction.guild.roles, name=self.level_full)
                role_mention = role.mention if role else self.level_full
                embed = discord.Embed(title="⚠️ Out of Syllabus Warning", description=f"This grammar concept **'{topic}'** doesn't feel relevant as per your current {role_mention} level.\n\nDo you still want to continue attempting this quiz? (You will only get 5 questions).", color=discord.Color.orange())
                view = GrammarWarningView(self.user, self.level_full, topic)
                msg = await interaction.edit_original_response(embed=embed, view=view)
                view.message = msg
                return
            
            has_pro = any(r.name == "金 Pro Learners 金" for r in interaction.user.roles)
            q_count = 10 if has_pro else 5
            
            await interaction.edit_original_response(content=f"⏳ Generating {q_count} questions for **{topic}**...")
            q_data = await generate_quiz_data(self.level_full, is_grammar=True, topic=topic, force_limit=q_count)
            q = q_data[0]
            embed = discord.Embed(title=f"🎌 {self.level_full} Mock Test (1/{len(q_data)})", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x3498db)
            view = QuizView(self.user, q_data, self.level_full, time.time())
            await interaction.edit_original_response(content="", embed=embed, view=view)
            view.message = await interaction.original_response()
            
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Verification/Fetch Error: {e}", embed=None, view=None)

# --- 🎙️ UNIVERSAL SMART VOICE QUEUE ---
voice_tasks = {} 
voice_processing = {} 

class ListeningSingleQView(View):
    def __init__(self, user, correct_ans):
        super().__init__(timeout=30.0)
        self.user = user
        self.correct_ans = correct_ans
        self.result = False
        self.user_choice = None
        
        for label in ["A", "B", "C", "D"]:
            btn = Button(label=label, custom_id=label, style=discord.ButtonStyle.primary)
            btn.callback = self.button_callback
            self.add_item(btn)
            
    async def button_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id: return await interaction.response.send_message("❌ Not your test!", ephemeral=True)
        self.user_choice = interaction.data['custom_id']
        self.result = (self.user_choice == self.correct_ans)
        await interaction.response.edit_message(content="✅ Answer locked! Wait for the next audio...", view=None, embed=None)
        self.stop()

# 🟢 THE MASTER PROCESSOR (Handles both Listening & Shadowing seamlessly)
async def process_voice_queue(guild, client):
    voice_processing[guild.id] = True
    while voice_tasks.get(guild.id, []):
        task = voice_tasks[guild.id][0]
        try:
            if task["type"] == "listening":
                await execute_listening_task(guild, client, task["data"])
            elif task["type"] == "shadow":
                await execute_shadow_task(guild, client, task["data"])
        except Exception as e:
            print(f"Voice Queue Error: {e}")
            
        if voice_tasks.get(guild.id):
            voice_tasks[guild.id].pop(0)
            
    voice_processing[guild.id] = False

# 🟢 1. LISTENING ENGINE (Updated Logic)
async def execute_listening_task(guild, client, data):
    user = data["user"]
    original_vc = data["vc"]
    level_full = data["level"]
    interaction = data["interaction"]
    q_count = data["length"]
    env = data["env"]
    
    new_vc = None
    voice_client = None
    
    try:
        mode_name = "Real Exam Hall" if env == "exam" else "Studio Clear"
        try: await interaction.edit_original_response(content=f"⏳ It's your turn! Preparing your **{mode_name}** session...", embed=None, view=None)
        except: pass
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, connect=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, move_members=True, manage_channels=True)
        }
        
        peer_role = discord.utils.get(guild.roles, name=level_full)
        if peer_role: overwrites[peer_role] = discord.PermissionOverwrite(view_channel=True, connect=False)
            
        jr_admin = discord.utils.get(guild.roles, name="Junior Admin（ジュニア・アデュミン）")
        sr_admin = discord.utils.get(guild.roles, name="Senior Admin（セィニア・アデュミン）")
        if jr_admin: overwrites[jr_admin] = discord.PermissionOverwrite(view_channel=True, connect=True)
        if sr_admin: overwrites[sr_admin] = discord.PermissionOverwrite(view_channel=True, connect=True)
        
        new_vc = await guild.create_voice_channel(
            name=f"Listening Practice - {user.display_name}",
            category=original_vc.category,
            overwrites=overwrites
        )
        
        if user.voice and user.voice.channel:
            await user.move_to(new_vc)
        
        voice_client = discord.utils.get(client.voice_clients, guild=guild)
        if voice_client and voice_client.is_connected(): await voice_client.move_to(new_vc)
        else: voice_client = await new_vc.connect()
            
        level_short = level_full.split(" ")[1]
        
        random_seed = random.randint(1000, 9999999)
        
        prompt = f"""You are an expert JLPT Examiner creating 'Nihongo Nook' style audio tests. Generate exactly {q_count} BRAND NEW, highly distinct Japanese listening scenarios for JLPT {level_short}.
        CRITICAL RULE 1 (DIVERSITY): Never repeat scenarios. Seed: [{random_seed}]. If generating multiple questions, EACH question MUST have a completely DIFFERENT topic/setting from the others. Do not stick to just one theme.
        CRITICAL RULE 2 (LENGTH & GRAMMAR): The conversation between the male and female characters MUST be detailed and realistic, and ideally 7 to 8 lines of dialogue, but can use more lines according to {level_short}, if required for conversation completion. You MUST strictly incorporate grammar structures specific to the {level_short} level.
        CRITICAL RULE 3 (MULTI-VOICE): You MUST write the script as an array of dialogue lines, specifying the speaker's gender for EACH line ("narrator", "male", or "female"). The narrator introduces the situation and asks the question.
        CRITICAL RULE 4 (FURIGANA OPTIONS): The 4 options (A, B, C, D) MUST be strictly in Japanese. NEVER translate them to English. DO NOT USE any kanji in options, only use hiragana and katakana where-ever necessary. Only the "question" field should be in English.
        CRITICAL RULE 5 (ZERO STUTTERING): ABSOLUTELY NO WORD REPETITION. Do NOT write things like "あの、あの" or "ふたり、ふたりは". The Japanese must be 100% grammatically flawless and flow perfectly without any human-like stutters.
        
        Output ONLY a valid JSON ARRAY of exactly {q_count} objects:
        [
            {{
                "scenario": [
                    {{"voice": "narrator", "text": "男の人と女の人が話しています。"}},
                    {{"voice": "narrator", "text": "男の人は何を買いますか。"}},
                    {{"voice": "male", "text": "..."}},
                    {{"voice": "female", "text": "..."}},
                    {{"voice": "narrator", "text": "男の人は何を買いますか。"}}
                ],
                "question": "The English translation of the main question",
                "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
                "answer": "A",
                "explanation": "Why this is correct."
            }}
        ]"""
        
        raw_text = await generate_gemini_response(prompt)
        questions_data = extract_json(raw_text)
        if isinstance(questions_data, dict): questions_data = [questions_data]
        
        for q in questions_data:
            try:
                correct_val = q['options'][q['answer']]
                vals = list(q['options'].values())
                random.shuffle(vals)
                labels = ["A", "B", "C", "D"]
                new_options = {}
                for idx, val in enumerate(vals):
                    new_options[labels[idx]] = val
                    if val == correct_val:
                        q['answer'] = labels[idx]
                q['options'] = new_options
            except KeyError:
                continue
        
        ffmpeg_options = '-af "lowpass=f=2500,aecho=0.8:0.7:60:0.4,volume=1.2"' if env == "exam" else ""
        total_score = 0
        start_time = time.time()
        user_choices = []
        
        import edge_tts
        from pydub import AudioSegment
        
        try:
            chime_sound = AudioSegment.from_file("chime.mp3")
        except:
            chime_sound = AudioSegment.silent(duration=1500)
        
        for i, q_data in enumerate(questions_data):
            scenario = q_data.get("scenario", [])
            if not scenario:
                scenario = [{"voice": "narrator", "text": q_data.get("script", "エラーが発生しました。")}]
            
            combined_audio = chime_sound
            
            for j, line in enumerate(scenario):
                voice_type = line.get("voice", "narrator").lower()
                text = line.get("text", "")
                if not text: continue
                
                if voice_type == "male":
                    voice_model = "ja-JP-KeitaNeural"
                    pitch = "+0Hz" 
                elif voice_type == "female":
                    voice_model = "ja-JP-NanamiNeural"
                    pitch = "+15Hz" 
                else:
                    voice_model = "ja-JP-NanamiNeural"
                    pitch = "-12Hz" 
                    
                temp_file = f"temp_{guild.id}_{i}_{j}.mp3"
                
                # 🟢 Safe File Handling, Retry & Alarm Flag
                max_retries = 3
                success = False
                for attempt in range(max_retries):
                    try:
                        communicate = edge_tts.Communicate(text, voice_model)
                        await communicate.save(temp_file)
                        await asyncio.sleep(0.5) 
                        
                        if os.path.exists(temp_file) and os.path.getsize(temp_file) > 100:
                            segment = AudioSegment.from_file(temp_file, format="mp3")
                            combined_audio += segment
                            success = True
                            break 
                        else:
                            raise Exception("File is empty.")
                            
                    except Exception as e:
                        print(f"TTS Error (Attempt {attempt+1}): {e}")
                        await asyncio.sleep(1) 
                
                if os.path.exists(temp_file): 
                    try: os.remove(temp_file)
                    except: pass
                    
                # 🚨 LOGIC: If 3 listening attempts failed, signal this to script
                if not success:
                    raise Exception("VOICE_ENGINE_FAILED")
            
            audio_file = f"listening_{guild.id}_{i}.mp3"
            combined_audio.export(audio_file, format="mp3")
            
            if not user.voice or user.voice.channel.id != new_vc.id:
                break
            
            voice_client.play(discord.FFmpegPCMAudio(audio_file, executable="ffmpeg", options=ffmpeg_options))
            while voice_client.is_playing():
                await asyncio.sleep(1)
            
            embed = discord.Embed(title=f"🎧 {level_full} Question ({i+1}/{q_count})", description=f"**{q_data.get('question', 'Question?')}**\n\n🇦 {q_data.get('options', {}).get('A', 'A')}\n🇧 {q_data.get('options', {}).get('B', 'B')}\n🇨 {q_data.get('options', {}).get('C', 'C')}\n🇩 {q_data.get('options', {}).get('D', 'D')}", color=0x9b59b6)
            embed.set_footer(text="⏳ You have 30 seconds to answer.")
            
            view = ListeningSingleQView(user, q_data.get('answer', 'A'))
            try:
                await interaction.edit_original_response(content=f"🎤 **What is the correct answer?**", embed=embed, view=view)
            except: pass 
            
            await view.wait() 
            
            if view.user_choice:
                user_choices.append(view.user_choice)
                if view.result: total_score += 1
            else:
                user_choices.append("Timeout")
                try: await interaction.edit_original_response(content="⏳ **Time's up for this question!**", view=None, embed=None)
                except: pass
                
            if os.path.exists(audio_file): os.remove(audio_file)
        
        time_taken = round(time.time() - start_time)
        quiz_db.update_one({"_id": user.id}, {"$set": {"level": level_full}, "$inc": {"score": total_score, "time_taken": time_taken}}, upsert=True)
        
        exp_embed = discord.Embed(title=f"🏁 Listening Complete! (Score: {total_score}/{len(user_choices)})", color=discord.Color.green())
        for i, q in enumerate(questions_data[:len(user_choices)]):
            user_ans = user_choices[i]
            correct_ans = q.get('answer', 'A')
            mark = "✅ Correct" if user_ans == correct_ans else f"❌ Incorrect (Answer was {correct_ans})"
            exp_embed.add_field(
                name=f"Q{i+1}: You chose {user_ans} | {mark}", 
                value=f"*{q.get('explanation', 'No explanation provided.')}*", 
                inline=False
            )
        try: await interaction.edit_original_response(content="🎉 **Session Finished! Returning you to your original channel...**", embed=exp_embed, view=None)
        except: pass
        
    except Exception as e:
        error_msg = str(e)
        # 🚨 THE AUTOMATED ALARM LOGIC
        if "VOICE_ENGINE_FAILED" in error_msg:
             # 1. Tell user who used listening quiz that bot has failed
            ticket_ch = discord.utils.get(guild.channels, name="🎫・create-a-ticket")
            ticket_mention = ticket_ch.mention if ticket_ch else "#🎫・create-a-ticket"
            
            user_msg = f"😷 **Sensei's throat doesn't feel good...**\n*The AI voice engine suddenly stopped working and needs update.* Please contact the Admins at once by clicking here -> {ticket_mention}"
            try: await interaction.edit_original_response(content=user_msg, embed=None, view=None)
            except: pass
            
            # 2. Tag admins in #admin-logs
            log_ch = discord.utils.get(guild.channels, name="admin-logs")
            if log_ch:
                sr_admin = discord.utils.get(guild.roles, name="Senior Admin（セィニア・アデュミン）")
                founder = discord.utils.get(guild.roles, name="Founder（ファウンダ）")
                ping_str = f"{founder.mention if founder else ''} {sr_admin.mention if sr_admin else ''}"
                
                admin_embed = discord.Embed(
                    title="🚨 Urgent: Voice Engine Down!", 
                    description=f"**User:** {user.mention}\n**Channel:** {original_vc.mention}\n\n`edge-tts` or an audio dependency has failed and needs an update immediately.\n\n👉 **Kindly run `/updatebot` now in  #bot-command channel!**", 
                    color=0xe74c3c
                )
                client.loop.create_task(log_ch.send(content=ping_str, embed=admin_embed))
        else:
            try: await interaction.edit_original_response(content=f"❌ Listening Error: {e}", embed=None, view=None)
            except: pass
    finally:
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect()
        
        if new_vc:
            try:
                if user.voice and user.voice.channel and user.voice.channel.id == new_vc.id:
                    await user.move_to(original_vc)
            except: pass
            
            await asyncio.sleep(10) 
            try: await new_vc.delete()
            except: pass

class QuizSelectionView(View):
    def __init__(self, user, level_full):
        super().__init__(timeout=120)
        self.user = user
        self.level_full = level_full

    @discord.ui.button(label="General Quiz", style=discord.ButtonStyle.primary, emoji="📚")
    async def btn_general(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        has_pro = any(r.name == "金 Pro Learners 金" for r in interaction.user.roles)
        q_count = 20 if has_pro else 10
        
        await interaction.response.edit_message(content=f"⏳ Generating {q_count} General {self.level_full} questions...", embed=None, view=None)
        try:
            q_data = await generate_quiz_data(self.level_full, is_grammar=False, force_limit=q_count)
            q = q_data[0]
            embed = discord.Embed(title=f"🎌 {self.level_full} Mock Test (1/{len(q_data)})", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x3498db)
            view = QuizView(self.user, q_data, self.level_full, time.time())
            await interaction.edit_original_response(content="", embed=embed, view=view)
            view.message = await interaction.original_response()
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ AI Fetch Error: {e}")

    @discord.ui.button(label="Grammar Quiz", style=discord.ButtonStyle.success, emoji="🧠")
    async def btn_grammar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        await interaction.response.send_modal(GrammarTopicModal(self.user, self.level_full))

    @discord.ui.button(label="Listening Practice", style=discord.ButtonStyle.secondary, emoji="🎧")
    async def btn_listening(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("❌ **You need to join a Voice Channel first to start Listening Practice!**", ephemeral=True)
            
        class ListeningOptionsView(View):
            def __init__(self, u, lvl):
                super().__init__(timeout=60)
                self.user = u
                self.level_full = lvl
                self.length = 1
                self.env = "clear"

            @discord.ui.select(placeholder="Select Length...", options=[
                discord.SelectOption(label="Quick Practice (1 Question)", value="1", emoji="⏱️"),
                discord.SelectOption(label="Exam Mode (5 Questions)", value="5", emoji="🎓")
            ], custom_id="len_sel")
            async def len_callback(self, i, select):
                if i.user.id != self.user.id: return
                self.length = int(select.values[0])
                await i.response.defer()

            @discord.ui.select(placeholder="Select Environment...", options=[
                discord.SelectOption(label="Studio Clear (Perfect Audio)", value="clear", emoji="🎧"),
                discord.SelectOption(label="Real Exam Hall (Echo & Muffled)", value="exam", emoji="🏫")
            ], custom_id="env_sel")
            async def env_callback(self, i, select):
                if i.user.id != self.user.id: return
                self.env = select.values[0]
                await i.response.defer()

            @discord.ui.button(label="🚀 Start Listening", style=discord.ButtonStyle.success, row=2)
            async def start_btn(self, i, btn):
                if i.user.id != self.user.id: return
                
                if self.length == 5 and not any(r.name == "金 Pro Learners 金" for r in i.user.roles):
                    # 🟢 Mentioning Go Pro channel
                    gopro_ch = discord.utils.get(i.guild.channels, name="💎・go-pro")
                    ch_mention = gopro_ch.mention if gopro_ch else "#💎・go-pro"
                    
                    return await i.response.send_message(f"❌ **Exam Mode (5 Qs)** is exclusively for **金 Pro Learners 金**.\nVisit {ch_mention} to upgrade!", ephemeral=True)
                
                vc = i.user.voice.channel
                guild_id = i.guild.id
                
                # 🟢 Universal Queue Check
                if guild_id not in voice_tasks: voice_tasks[guild_id] = []
                for t in voice_tasks[guild_id]:
                    if t["data"]["user"].id == i.user.id:
                        return await i.response.send_message("⚠️ You are already in the voice queue!", ephemeral=True)
                
                task_data = {
                    "user": i.user,
                    "vc": vc,
                    "level": self.level_full,
                    "interaction": i, 
                    "length": self.length,
                    "env": self.env
                }
                
                voice_tasks[guild_id].append({"type": "listening", "data": task_data})
                queue_position = len(voice_tasks[guild_id])
                
                if queue_position == 1 and not voice_processing.get(guild_id, False):
                    # We send the Ephemeral processing message here, so we can edit it later!
                    await i.response.send_message(f"⏳ Please wait! Setting up your session...", ephemeral=True)
                    asyncio.create_task(process_voice_queue(i.guild, i.client))
                else:
                    est_wait = (queue_position - 1) * 3
                    await i.response.send_message(f"⏳ **Added to Global Voice Queue (Position: {queue_position})**\n⏱️ **Estimated Wait Time:** ~{est_wait} minutes.\n*Sensei is assisting someone else right now. Please wait in {vc.name}.*", ephemeral=True)

        embed = discord.Embed(title="🎧 Listening Setup", description="Customize your practice session before starting:", color=0x3498db)
        await interaction.response.send_message(embed=embed, view=ListeningOptionsView(self.user, self.level_full), ephemeral=True)
            
    @discord.ui.button(label="Kanji Reading Quiz", style=discord.ButtonStyle.danger, emoji="🈴")
    async def btn_kanji(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        has_pro = any(r.name == "金 Pro Learners 金" for r in interaction.user.roles)
        q_count = 20 if has_pro else 10
        level_short = self.level_full.split(" ")[1]
        
        # 🟢 Timer and Syllabus Setup Based on Level
        kanjis_current = []
        kanjis_lower = []
        time_limit = 60
        
        if level_short == "N5":
            kanjis_current = N5_KANJI
            time_limit = 30
        elif level_short == "N4":
            kanjis_current = N4_KANJI
            kanjis_lower = N5_KANJI
            time_limit = 30
        elif level_short == "N3":
            kanjis_current = N3_KANJI
            kanjis_lower = N5_KANJI + N4_KANJI
            time_limit = 20
        elif level_short == "N2":
            kanjis_current = N2_KANJI
            kanjis_lower = N5_KANJI + N4_KANJI + N3_KANJI
            time_limit = 10
        elif level_short == "N1":
            kanjis_current = N1_KANJI
            kanjis_lower = N5_KANJI + N4_KANJI + N3_KANJI + N2_KANJI
            time_limit = 10

        warning_msg = f"⏳ **Anti-Cheat Active:** You will have strictly **{time_limit} seconds** per question to answer.\nGenerating {q_count} Kanji questions (50% from {level_short}, 50% from lower levels)..."
        await interaction.response.edit_message(content=warning_msg, embed=None, view=None)
        
        try:
            q_data = await generate_kanji_quiz_data(level_short, kanjis_current, kanjis_lower, force_limit=q_count)
            q = q_data[0]
            embed = discord.Embed(title=f"🎌 {self.level_full} Kanji Mock Test (1/{len(q_data)})", description=f"{q['question']}\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0xe74c3c)
            # Pass the strict time limit to the view
            view = QuizView(self.user, q_data, self.level_full, time.time(), time_limit_per_q=time_limit)
            await interaction.edit_original_response(content="", embed=embed, view=view)
            view.message = await interaction.original_response()
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ AI Fetch Error: {e}")

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
                title="🔒 Premium Feature Locked", 
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
                prompt = f"Create a short, logical visual memory trick for an English speaker to remember the shape/radicals of these JLPT {level} Kanji(s): {kanji_str}. Relate the shapes to a real-life object or scene which can be imagined easily."
            elif trick_type == "pronunciation":
                prompt = f"Create a highly creative pronunciation trick for an English speaker to remember the reading of these JLPT {level} Kanji(s): {kanji_str}. Find English words or puns that sound exactly/similar to the Romaji reading."
            elif trick_type == "story":
                prompt = f"For the JLPT {level} Kanji(s) '{kanji_str}', create a highly imaginative 1-2 sentence 'Memory Story'. Combine its Visual shape and its Pronunciation (using an English pun) into one unforgettable scenario."
            
            try:
                response_text = await generate_gemini_response(prompt)
                self.cached_responses[cache_key] = response_text
            except Exception as e:
                return await interaction.followup.send(f"❌ Failed to fetch trick: {e}", ephemeral=True)

        titles = {
            "visual": "👀 Visual Memory Trick",
            "pronunciation": "🎵 Pronunciation Trick",
            "story": "🧠 The Memory Story"
        }
        
        embed = discord.Embed(
            title=titles[trick_type], 
            description=response_text[:4000], 
            color=0xf1c40f
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="👀 Visual Trick", style=discord.ButtonStyle.primary, custom_id="kanji_visual")
    async def btn_visual(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_trick(interaction, "visual")

    @discord.ui.button(label="🗣️ Pronunciation Trick", style=discord.ButtonStyle.success, custom_id="kanji_pronounce")
    async def btn_pronunciation(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_trick(interaction, "pronunciation")

    @discord.ui.button(label="🧠 Memory Story", style=discord.ButtonStyle.danger, custom_id="kanji_story")
    async def btn_story(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_trick(interaction, "story")

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
        super().__init__(placeholder="Select JLPT Level you're preparing for...", min_values=1, max_values=1, options=opts, custom_id="jlpt_dropdown")

    async def callback(self, interaction: discord.Interaction):
        if has_main_role(interaction.user): return await interaction.response.send_message("❌ You already have a role! Please use `/changerole` to upgrade to your current JLPT preparation level.", ephemeral=True)
            
        selected_role = self.values[0]
        level_short = selected_role.split(" ")[1] 
        await interaction.response.send_message(f"⏳ Generating a 1-question placement test for {level_short}...", ephemeral=True)
        
        prompt = f"""You are an expert JLPT Examiner. Generate exactly 1 multiple-choice question for JLPT {level_short} (Grammar or Vocab). Always shuffle the options.
        1. CONTEXT-RICH TEXT ONLY: The sentence MUST provide enough logical context to be solved purely through reading, without any images or audio.
        2. NO VISUAL QUESTIONS: NEVER generate vague questions like "___は何ですか。" or "これは___です。"
        3. Must have exactly ONE blank represented by '___'.
        4. Ensure high-quality, natural Japanese.
        5. Ensure furigana of kanjis used, should be written in ([]) square brackets just after kanji used. The difficulty level of question on a scale of 1-5 should not be more than 2.
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
        if has_main_role(interaction.user): return await interaction.response.send_message("❌ You already have a role! Please use `/changerole` to upgrade your current preparation level.", ephemeral=True)
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

    # 🟢 NEW NATIVE BUTTON (Green/Success color)
    @discord.ui.button(label="⛩️ Native Japanese", style=discord.ButtonStyle.success, custom_id="role_native")
    async def native_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if has_main_role(interaction.user): 
            return await interaction.response.send_message("❌ You already have a role!", ephemeral=True)
        
        await interaction.response.send_message("⏳ Generating a hard Native/N1+ level placement test...", ephemeral=True)
        
        prompt = """You are an expert Japanese linguist. Generate exactly 1 extremely hard multiple-choice question at a Native/Post-N1 level (e.g., highly advanced Kanji reading, obscure idioms, or complex classical grammar that a native japanese can understand).
        1. CONTEXT-RICH TEXT ONLY: No images or audio.
        2. Must have exactly ONE blank represented by '___'.
        3. Ensure high-quality, natural Japanese.
        4. Do NOT provide furigana for this level, The difficulty level of question on a scale of 1-5 should not be less than 4.
        5. CRITICAL JSON RULE: Use strictly double quotes (") for all keys and string values. Do not use single quotes. Do not add trailing commas.
        Output ONLY a valid JSON array format exactly like this:
        [{"question": "...", "options": {"A": "...", "B": "...", "C": "...", "D": "..."}, "answer": "A"}]"""
        
        try:
            raw_text = await generate_gemini_response(prompt)
            q = extract_json(raw_text)[0]
            embed = discord.Embed(title=f"⛩️ Native Japanese Placement Test", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x2ecc71)
            embed.set_footer(text="⏳ You have 60 seconds to answer.")
            
            # Pass is_native=True so it loops back on fail
            view = PlacementQuizView(interaction.user, q, "📍 Native Japanese", is_native=True)
            msg = await interaction.edit_original_response(content="", embed=embed, view=view)
            view.message = msg
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ AI Initialization Error: {e}")

# 🗑️ DELETE VOCAB VAULT
class DeleteVaultView(View):
    def __init__(self, user, thread):
        super().__init__(timeout=60)
        self.user = user
        self.thread = thread

    @discord.ui.button(label="Yes, Delete Permanently", style=discord.ButtonStyle.danger)
    async def btn_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        await interaction.response.edit_message(content="⏳ Deleting your vault data...", embed=None, view=None)
        
        # 1. Database se user ka saara srs deck delete karna
        vocab_db.delete_one({"_id": interaction.user.id})
        
        # 2. Discord se Thread delete karna
        try:
            await self.thread.delete()
        except:
            pass
            
        try: await interaction.edit_original_response(content="✅ Your Vocab Vault and all saved words have been permanently deleted. You can create a fresh one anytime using command `/vocabvault`.")
        except: pass

    @discord.ui.button(label="No, Cancel", style=discord.ButtonStyle.secondary)
    async def btn_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        await interaction.response.edit_message(content="✅ Deletion cancelled. Your vault is safe.", embed=None, view=None)

# --- 🤖 MAIN BOT CLASS & BACKGROUND TASKS ---
class NihongoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True 
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents, help_command=None)

    # 🟢 Global Interaction Logger (Background Task) in #user-interation-logs channel
    async def on_interaction(self, interaction: discord.Interaction):
        
        # Background logging (Error-proof)
        try:
            if not interaction.guild: return
            
            log_channel = discord.utils.get(interaction.guild.channels, name="user-interaction-logs")
            if not log_channel: return
                
            feature_name = None
            
            # Agar Slash Command ya Context Menu use kiya (e.g., /quiz, /read)
            if interaction.type == discord.InteractionType.application_command:
                feature_name = f"/{interaction.command.name}"
                
            # Agar kisi Persistent Panel ka Button use kiya
            elif interaction.type == discord.InteractionType.component:
                cid = interaction.data.get('custom_id', '')
                
                # STRICT RULE: Ignore Onboarding & Setup roles completely (No spam)
                if cid in ["role_visitor", "role_learner", "role_native", "jlpt_dropdown"]:
                    return
                    
                # Mapping system buttons to readable names
                friendly_names = {
                    "premium_journal_btn": "Daily Journal (Pro)",
                    "shadow_start": "Shadow Speaking",
                    "shadow_help": "Shadow Speaking Guide",
                    "panel_support": "Support Ticket",
                    "panel_incident": "Incident Report",
                    "panel_gopro": "Go Pro Enquiry",
                    "freemium_trans": "Story Translate (Free)",
                    "freemium_gram": "Story Grammar (Pro)",
                    "freemium_voc": "Story Vocab (Pro)",
                    "kanji_visual": "Kanji Visual Trick",
                    "kanji_pronounce": "Kanji Audio Trick",
                    "kanji_story": "Kanji Memory Story",
                    "ticket_close_btn": "Close Ticket"
                }
                
                if cid in friendly_names:
                    feature_name = friendly_names[cid]
            
            # Agar valid feature mila, toh log channel mein bhej do
            if feature_name:
                log_msg = f"{interaction.user.mention} used `{feature_name}` in {interaction.channel.mention}"
                self.loop.create_task(log_channel.send(log_msg))
                
        except Exception as e:
            print(f"Logging error silently ignored: {e}")

    async def setup_hook(self):
            #... existing tasks ...
        self.weekly_leaderboard_loop.start()
        self.daily_kanji_loop.start()
        self.freemium_dokkai_loop.start()
        self.ticket_cleanup_loop.start() # 🟢 Start ticket auto-deletion loop
        self.srs_review_loop.start() # 🟢 Start Visual Vault loop
            #... existing views ...
        self.add_view(WelcomeView())
        self.add_view(PersistentJournalView())
        self.add_view(PersistentFreemiumStoryView()) 
        self.add_view(PersistentDailyKanjiView())    
        self.add_view(PersistentTicketPanelView())
        self.add_view(PersistentTicketCloseView())
        self.add_view(PersistentShadowPanelView())
        view = View(timeout=None)
        view.add_item(JLPTSelect())
        self.add_view(view)
        self.ai_tracker_loop.start()
        self.vc_cleanup_loop.start()
        await self.tree.sync()
        print("✅ Commands Synced & Tasks Started.")

    async def on_member_join(self, member: discord.Member):
        welcome_ch = discord.utils.find(lambda c: "welcome" in c.name.lower(), member.guild.channels)
        if welcome_ch:
            # 🟢 UPDATED TEXT WITH PREPARING FOR INSTRUCTION
            embed = discord.Embed(
                title="🌸 Choose Your Path", 
                description="To get started, please select your role below:\n\n"
                            "📸 **Visitor:** I'm just exploring.\n"
                            "🎌 **JP Learner:** I am studying Japanese. *(Please select the level you are PREPARING FOR, not the one you have already passed!)*\n"
                            "⛩️ **Native Japanese:** I am a native speaker (Requires passing 1 hard N1+ test).", 
                color=0xffb6c1
            )
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
                    # 1. Purane sabhi champions se role wapas lena
                    champion_role = discord.utils.get(guild.roles, name="Weekly Champion")
                    if champion_role:
                        for member in champion_role.members:
                            try: await member.remove_roles(champion_role)
                            except: pass
                    
                    # 2. Naye winners announce karna aur role dena
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
                                        value=f"<@{user_data['_id']}> - **Total Score: {user_data['score']}** (Total Time: {user_data.get('time_taken', 0)}s)", 
                                        inline=False
                                    )
                                
                                # Assign role to Rank #1
                                winner_id = scorers_list[0]['_id']
                                winner = guild.get_member(winner_id)
                                if winner and champion_role:
                                    try: await winner.add_roles(champion_role)
                                    except: pass
                                    
                                try: await channel.send(content=f"🎉 **THE RESULTS ARE IN!** {role.mention}\nCongratulations to <@{winner_id}> for becoming the {level_short.upper()} Weekly Champion! 👑", embed=embed)
                                except Exception: pass
                            else:
                                empty_embed = discord.Embed(
                                    title="😔 No Champions This Week", 
                                    description="There are no Weekly Champions for this week.\n\nWant to become one? Type `/quiz` and start now to claim the #1 spot! Winners get exclusive role.", 
                                    color=0x95a5a6
                                )
                                empty_embed.set_footer(text=f"Level: {role_name}")
                                try: await channel.send(content=role.mention, embed=empty_embed)
                                except Exception: pass
                                
        elif now_jst.weekday() == 6 and now_jst.hour == 22 and now_jst.minute == 1:
            self.night_announce_done = False
            
            if not getattr(self, 'weekly_reset_done', False):
                self.weekly_reset_done = True
                quiz_db.delete_many({}) 
                print("✅ 10:01 PM JST: All weekly leaderboard data wiped to save memory.")
                
        elif now_jst.weekday() == 6 and now_jst.hour == 22 and now_jst.minute == 2:
            self.weekly_reset_done = False

    #TASK LOOP FOR VOCAB VAULT
    @tasks.loop(minutes=1)
    async def srs_review_loop(self):
        now_jst = datetime.now(pytz.timezone('Asia/Tokyo'))
        
        # 🟢 Runs at 8:30 PM JST (20:30) Every Day
        if now_jst.hour == 20 and now_jst.minute == 30:
            if not getattr(self, 'srs_review_done', False):
                self.srs_review_done = True
                await self.trigger_daily_srs()
        elif now_jst.hour == 20 and now_jst.minute == 31:
            self.srs_review_done = False

    async def trigger_daily_srs(self):
        current_time = int(time.time())
        due_words = list(vocab_db.find({"next_review": {"$lte": current_time}}))
        if not due_words: return
        
        user_reviews = {}
        for w in due_words:
            uid = w["user_id"]
            if uid not in user_reviews: user_reviews[uid] = []
            user_reviews[uid].append(w)
            
        for guild in self.guilds:
            main_channel = discord.utils.get(guild.channels, name="📸・vocab-vault")
            if not main_channel: continue
            
            for uid, words in user_reviews.items():
                user = guild.get_member(uid)
                if not user: continue
                
                thread_name = f"🧵 {user.name}'s Vault"
                thread = discord.utils.get(main_channel.threads, name=thread_name)
                if not thread: continue
                
                embed = discord.Embed(
                    title="🔔 Daily Review Time!", 
                    description=f"Sensei is here! You have **{len(words)}** visual flashcards due for review today.\n\nKeep your streak alive to move words into your long-term memory (Level 5).\n*Note: This is a self-practice SRS quiz, your scores here will not be added to `/quiz` leaderboard!)*", 
                    color=0xf1c40f
                )
                view = StartReviewView(user, words)
                try: await thread.send(content=user.mention, embed=embed, view=view)
                except: pass

    
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

                kanji_str = ", ".join(new_kanjis)
                prompt = f"""You are an expert Japanese Sensei who cleared N1 within 2 years by smart-work and not hard-work. Explain ALL of the following {len(new_kanjis)} Kanji(s): {kanji_str}.
                For EACH Kanji, strictly provide:
                1. Meaning
                2. Onyomi & Kunyomi (with Romaji)
                3. One example of each kanji in a word with Onyomi & Kunyomi pronunciation being used (with romaji).
                Format the entire response beautifully in Discord Markdown.
                CRITICAL RULE: For the Kanji character heading itself, you MUST use a Markdown H1 tag (e.g., `# 妹` or `# 業`) so the Kanji appears massively large on Discord.
                Keep the script entirely in English. Do NOT include memory or pronunciation tricks."""
                
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
                    msg = await channel.send(embed=embed, view=view)
                    
                    # 🟢 Save Message Hyperlink to DB for Dokkai linking!
                    for k in new_kanjis:
                        kanji_db.update_one({"kanji_char": k}, {"$set": {"url": msg.jump_url, "level": lvl_name}}, upsert=True)

                    # 🟢 DB is ONLY updated if the message is actually generated and sent successfully!
                    next_index = current_index + len(new_kanjis)
                    kanji_db.update_one({"level": lvl_name}, {"$set": {"current_index": next_index}}, upsert=True)

                except Exception as e:
                    print(f"❌ Error dropping {lvl_name} Kanji: {e}")
                
                await asyncio.sleep(5)

    @tasks.loop(minutes=1)
    async def freemium_dokkai_loop(self):
        now_jst = datetime.now(pytz.timezone('Asia/Tokyo'))
        
        # 🟢 Fixed exactly at 6:00 PM (18:00) JST to prevent restart spam
        if now_jst.hour == 18 and now_jst.minute == 0:
            if not getattr(self, 'daily_dokkai_done', False):
                self.daily_dokkai_done = True
                await self.drop_freemium_dokkai_task()
        elif now_jst.hour == 18 and now_jst.minute == 1:
            self.daily_dokkai_done = False

    async def drop_freemium_dokkai_task(self):
        topics = ["Japanese Culture", "A Sci-Fi Adventure", "Daily Mysteries & Unspoken Rules", "A Slice of Life moment", "A Mystery", "Folklore", "School Life", "Japanese Food & Café Culture", "Modern Habits & Tech"]
        topic = random.choice(topics)
        
        # 🟢 Added 'lower_levels' array so the bot remembers past JLPT levels
        level_configs = [
            ("N5", "n5-daily-dokkai", N5_KANJI, []),
            ("N4", "n4-daily-dokkai", N4_KANJI, [N5_KANJI]),
            ("N3", "n3-daily-dokkai", N3_KANJI, [N5_KANJI, N4_KANJI]),
            ("N2", "n2-daily-dokkai", N2_KANJI, [N5_KANJI, N4_KANJI, N3_KANJI]),
            ("N1", "n1-daily-dokkai", N1_KANJI, [N5_KANJI, N4_KANJI, N3_KANJI, N2_KANJI])
        ]
        
        for guild in self.guilds:
            for lvl_name, ch_keyword, kanji_list, lower_levels in level_configs:
                channel = discord.utils.find(lambda c: ch_keyword in c.name.lower(), guild.channels)
                if not channel:
                    continue
                    
                # 🟢 LOGIC: Build a Master List of ALL Known Kanjis
                known_kanjis_list = []
                # 1. Add all Kanjis from lower levels
                for lower_list in lower_levels:
                    known_kanjis_list.extend(lower_list)
                    
                # 2. Add dropped Kanjis from the current level
                db_data = kanji_db.find_one({"level": lvl_name})
                current_idx = db_data["current_index"] if db_data else 0
                
                if current_idx > 0:
                    known_kanjis_list.extend(kanji_list[:current_idx])
                    
                known_kanjis_str = "".join(known_kanjis_list)
                needs_romaji = lvl_name in ["N5", "N4"]
                
                # 🟢 LOGIC: Advanced Strict Prompt for Partial Kanji Words
                prompt = f"""You are an expert Japanese linguist and JLPT examiner.
                Task: Generate a short narrative (max 300 Japanese characters) about '{topic}'.
                Constraint 1: Strictly use ONLY vocabulary and grammar points majorly from {lvl_name} till N5 level.
                Constraint 2 (SMART FURIGANA): The user ALREADY KNOWS these kanjis: [{known_kanjis_str}].
                CRITICAL FURIGANA RULE: 
                - If a Japanese word contains multiple kanjis (e.g., 毎日), and EVEN ONE of those kanjis is in the known list, DO NOT provide furigana for that entire word.
                - Only provide furigana in parentheses (e.g., 漢字（かんじ）) for words where EVERY SINGLE KANJI in the word is UNKNOWN (not in the list). 
                - Do NOT add furigana for any single kanjis the user already knows or lower-level kanjis."""
                
                if needs_romaji:
                    prompt += """\nOutput Format: Return ONLY valid JSON with exactly three keys: "title", "story_content", and "romaji" (the exact romaji pronunciation of the story text)."""
                else:
                    prompt += """\nOutput Format: Return ONLY valid JSON with exactly two keys: "title" and "story_content"."""
                
                try:
                    raw_text = await generate_gemini_response(prompt)
                    story_data = extract_json(raw_text)
                    if isinstance(story_data, list): story_data = story_data[0]

                    title = story_data.get("title", f"{lvl_name} Daily Reading")
                    content = story_data.get("story_content", "Could not generate story.")
                    romaji = story_data.get("romaji", "")

                    # 🟢 PYTHON HYPERLINK INJECTOR (Unchanged, it's perfect)
                    db_kanjis = kanji_db.find({"level": lvl_name, "url": {"$exists": True}})
                    for doc in db_kanjis:
                        k_char = doc.get("kanji_char")
                        k_url = doc.get("url")
                        if k_char and k_char in content:
                            content = content.replace(k_char, f"[{k_char}]({k_url})")

                    if needs_romaji and romaji:
                        content += f"\n\n**Romaji translation (click to reveal):**\n||{romaji}||"

                    embed = discord.Embed(title=f"🎁 Daily Free Reading: {title}", description=content, color=0x3498db)
                    embed.set_footer(text=f"Level: {lvl_name} | Click the blue kanjis to jump to their Sensei Breakdown!")
                    
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


    # 🟢 1-Hour Token Tracker Image Loop (0 API Tokens Used)
    @tasks.loop(hours=1)
    async def ai_tracker_loop(self):
        tracker_channel = None
        for guild in self.guilds:
            tracker_channel = discord.utils.find(lambda c: "ai-token-tracker" in c.name.lower(), guild.channels)
            if tracker_channel: 
                break
            
        if not tracker_channel: 
            return

        from PIL import Image, ImageDraw
        import io
        
        today_pst = datetime.now(pytz.timezone('US/Pacific')).strftime("%Y-%m-%d")
        data = token_db.find_one({"date": today_pst})
        
        # 🟢 AUTO-CLEANUP: AI BOT TOKEN TRACKER DATA AUTO-DELETE AFTER 7 DAYS FROM MongoDB
        seven_days_ago = (datetime.now(pytz.timezone('US/Pacific')) - timedelta(days=7)).strftime("%Y-%m-%d")
        token_db.delete_many({"date": {"$lt": seven_days_ago}})
        
        req_count = data["requests"] if data else 0
        token_count = data["tokens"] if data else 0
        max_req = max(1500 * len(API_KEYS), 1)
        
        from PIL import Image, ImageDraw, ImageFont
        import io
        
        # 🟢 Transparent & Ultra-Wide Aesthetic (RGBA)
        width, height = 1100, 380
        img = Image.new('RGBA', (width, height), color=(0, 0, 0, 0)) 
        draw = ImageDraw.Draw(img)
        
        # 🟢 Load Fonts (3 Sizes for perfect hierarchy)
        try:
            pixel_font_large = ImageFont.truetype("pixel.ttf", 36) # Heading
            pixel_font_medium = ImageFont.truetype("pixel.ttf", 24) # Main Data
            pixel_font_tiny = ImageFont.truetype("pixel.ttf", 14)  # PST Line
        except:
            pixel_font_large = ImageFont.load_default()
            pixel_font_medium = ImageFont.load_default()
            pixel_font_tiny = ImageFont.load_default()
        
        # 🟢 Drawing the Pixel Heading
        draw.text((32, 22), "REALTIME A.I. TOKENS MONITOR", font=pixel_font_large, fill=(0, 0, 0, 200))
        draw.text((30, 20), "REALTIME A.I. TOKENS MONITOR", font=pixel_font_large, fill=(255, 255, 255, 255))
        
        # 🟢 PST Line (Normal size)
        draw.text((30, 85), f"⚡ PST Date: {today_pst}   |   Auto-Refresh every 1 hour", font=pixel_font_tiny, fill=(148, 163, 184, 255))
        
        # 🟢 Main Data (Bada aur Bold)
        draw.text((30, 130), f"Active API Keys: {len(API_KEYS)}", font=pixel_font_medium, fill=(52, 211, 153, 255))
        draw.text((30, 175), f"Daily Requests: {req_count} / {max_req}", font=pixel_font_medium, fill=(251, 146, 60, 255))
        draw.text((30, 220), f"Tokens Processed: {token_count:,}", font=pixel_font_medium, fill=(96, 165, 250, 255))
        
        # 🟢 Transparent Status Bar (Aur Zada Wide & Thick)
        bar_x, bar_y, bar_w, bar_h = 30, 280, 1040, 45
        draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], fill=(255, 255, 255, 30))
        
        fill_ratio = min(req_count / max_req, 1.0) if max_req > 0 else 0
        fill_w = int(fill_ratio * bar_w)
        
        if fill_w > 0:
            # Glowing Bar
            bar_color = (16, 185, 129, 220) if fill_ratio < 0.75 else (245, 158, 11, 220) if fill_ratio < 0.9 else (239, 68, 68, 220)
            draw.rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h], fill=bar_color)
            
        pct_text = f"{int(fill_ratio * 100)}%"
        # Percentage text ko center-align kiya bar ke andar!
        draw.text((bar_x + bar_w - 70, bar_y + 12), pct_text, font=pixel_font_medium, fill=(255, 255, 255, 255))
        
        arr = io.BytesIO()
        img.save(arr, format='PNG')
        arr.seek(0)
        file = discord.File(arr, filename="tracker.png")
        
        try:
            await tracker_channel.purge(limit=3)
            await tracker_channel.send(file=file)
        except Exception as e:
            print(f"Tracker Drop Error: {e}")

    @ai_tracker_loop.before_loop
    async def before_ai_tracker_loop(self):
        await self.wait_until_ready()

    # 🟢 VC Cleanup Loop
    @tasks.loop(minutes=10)
    async def vc_cleanup_loop(self):
        for guild in self.guilds:
            category = discord.utils.get(guild.categories, name="📣 ボイソ・チャト")
            if not category:
                continue
                
            for vc in category.voice_channels:
                # Bot check karega ki VC ko bane hue 8 ghante (8 hours) ho gaye hain ya nahi
                if discord.utils.utcnow() - vc.created_at > timedelta(hours=8):
                    try:
                        await vc.delete(reason="8 hour auto-delete limit reached.")
                    except Exception as e:
                        print(f"Failed to delete VC: {e}")

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
    
    has_permission = any(role.name in ["Junior Admin（ジュニア・アデュミン）", "Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied: You need to be an Admin to use this.", ephemeral=True)
    
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

# ---------------------------------------------------------
# 1. THE SLASH COMMAND (For manual typing)
# ---------------------------------------------------------
@bot.tree.command(name="translate", description="Translate any text (Any Language ↔ Japanese).")
async def translate_slash(interaction: discord.Interaction, text: str):
    # 🟢 NEW: Role Restriction Check
    allowed_roles = ["Weekly Champion"]
    if not any(r.name in allowed_roles for r in interaction.user.roles):
        return await interaction.response.send_message("❌ Can only be used by `Weekly Champion`, become one by topping your `Weekly Leaderboard`. Use `/quiz` now.", ephemeral=True)
        
    await interaction.response.defer()
    
    # 🟢 ULTRA-OPTIMIZED PROMPT
    prompt = f'Translate "{text}" to Polite Native Japanese (written in language other than JP). For JP output text, use Kanji【kana】 format. Return ONLY valid JSON: {{"t": "translation", "r": "romaji"}}'
    
    try:
        raw_text = await generate_gemini_response(prompt)
        data = extract_json(raw_text)
        
        if isinstance(data, list):
            data = data[0]
            
        # 🟢 AESTHETIC EMBED DESIGN
        embed = discord.Embed(color=0x1abc9c)
        embed.set_author(name="🌐 日本語 Translator", icon_url=bot.user.display_avatar.url if bot.user.display_avatar else None)
        
        # Original text with a quote block for elegance
        embed.add_field(name="📝 Original Text", value=f"> {text}", inline=False)
        
        # Translation with bold and clear spacing
        embed.add_field(name="🎌 Japanese Translation", value=f"**{data.get('t', 'Error fetching translation.')}**", inline=False)
        
        # Romaji only if it exists, styled as a subtle sub-text
        if data.get("r"):
            embed.add_field(name="🗣️ Romaji Reading", value=f"*{data.get('r')}*", inline=False)
            
        embed.set_footer(text="Powered by 司会者 - AI SENSEI")
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        print(f"Translation Error (Slash): {e}")
        await interaction.followup.send("❌ Error translating text. Please try again.", ephemeral=True)

# ---------------------------------------------------------
# 2. THE "APPS" CONTEXT MENU COMMAND (Right-Click / Long Press)
# ---------------------------------------------------------
@bot.tree.context_menu(name="Translate to EN/JP")
async def translate_context_menu(interaction: discord.Interaction, message: discord.Message):
    # 🟢 NEW: Role Restriction Check
    allowed_roles = ["Weekly Champion", "📍 Native Japanese"]
    if not any(r.name in allowed_roles for r in interaction.user.roles):
        return await interaction.response.send_message("❌ Only available to use by `Native Japanese` or `Weekly Champion`, become one by topping your `Weekly Leaderboard`. Use `/quiz` now.", ephemeral=True)

    # Check agar message khali hai (jaise sirf image ya sticker)
    if not message.content:
        return await interaction.response.send_message("❌ There is no text in this message to translate.", ephemeral=True)
        
    await interaction.response.defer()
    
    # Text agar bohot lamba hai toh thoda limit kar dete hain token bachane ke liye (optional)
    text_to_translate = message.content[:1000] 
    
    # 🟢 EXACT SAME OPTIMIZED PROMPT
    prompt = f'Translate "{text_to_translate}" to Japanese (if EN/HI) or English (if JP). For JP text, use Kanji【kana】 format. Return ONLY valid JSON: {{"t": "translation", "r": "romaji"}}'
    
    try:
        raw_text = await generate_gemini_response(prompt)
        data = extract_json(raw_text)
        
        if isinstance(data, list):
            data = data[0]
            
        # 🟢 AESTHETIC EMBED DESIGN
        embed = discord.Embed(color=0x3498db)
        
        # Show who requested the translation and whose message it was
        embed.set_author(name=f"Translated for {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None)
        
        # The actual translated text, prominent and bold
        embed.add_field(name="🎌 Translation", value=f"**{data.get('t', 'Error fetching translation.')}**", inline=False)
        
        # Romaji in italics for subtle styling
        if data.get("r"):
            embed.add_field(name="🗣️ Romaji Reading", value=f"*{data.get('r')}*", inline=False)
            
        # Add a beautiful jump link format at the bottom
        embed.add_field(name="🔗 Original Message", value=f"[Click here to jump to {message.author.display_name}'s message]({message.jump_url})", inline=False)
        
        # Footer to show it's a context action
        embed.set_footer(text="Context Menu Translation")
            
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        print(f"Translation Error (Context Menu): {e}")
        await interaction.followup.send("❌ Error translating the message. Please try again.", ephemeral=True)

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

@bot.tree.command(name="quiz", description="Start a customized Japanese (General, Grammar, Listening or Kanji Reading) Quiz. Send to START")
async def quiz(interaction: discord.Interaction):
    user_level_role = next((r.name for r in interaction.user.roles if r.name in ROLE_NAMES), None)
    if not user_level_role: 
        return await interaction.response.send_message("❌ You need a JLPT Learner role (N5-N1) to start a quiz. Select your role in the welcome channel.", ephemeral=True)
        
    embed = discord.Embed(
        title="🎯 Choose Your Quiz Mode", 
        description=f"Your current level is **{user_level_role}**.\n\nSelect a practice mode below:", 
        color=0x3498db
    )
    view = QuizSelectionView(interaction.user, user_level_role)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="leaderboardannounce", description="[Admin] Manually trigger Weekly Announcement for ALL levels & wipe DB.")
async def leaderboard_announce(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    has_permission = any(role.name in ["Junior Admin（ジュニア・アデュミン）", "Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied: You need to be an Admin to use this command.", ephemeral=True)
        
    guild = interaction.guild
    
    # 1. Remove Champion role from previous winners
    champion_role = discord.utils.get(guild.roles, name="Weekly Champion")
    if champion_role:
        for member in champion_role.members:
            try: await member.remove_roles(champion_role)
            except: pass
            
    # 2. Announce all levels and assign new roles
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
                        value=f"<@{user_data['_id']}> - **Total Score: {user_data['score']}** (Total Time: {user_data.get('time_taken', 0)}s)", 
                        inline=False
                    )
                
                winner_id = scorers_list[0]['_id']
                winner = guild.get_member(winner_id)
                if winner and champion_role:
                    try: await winner.add_roles(champion_role)
                    except: pass
                    
                try: await channel.send(content=f"🎉 **THE RESULTS ARE IN!** {role.mention}\nCongratulations to <@{winner_id}> for becoming the {level_short.upper()} Weekly Champion! 👑", embed=embed)
                except Exception: pass
            else:
                empty_embed = discord.Embed(title="😔 No Champions This Week", description="No one played this week. Type `/quiz` to claim #1!", color=0x95a5a6)
                try: await channel.send(content=role.mention, embed=empty_embed)
                except: pass
                
    # 3. Wipe Database completely to reset scores
    quiz_db.delete_many({})
    await interaction.followup.send("✅ Manual Leaderboard Announcement complete! Roles updated and Database wiped.", ephemeral=True)

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
    has_permission = any(role.name in ["Junior Admin（ジュニア・アデュミン）", "Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied. Only Admins and Founders can make official announcements.", ephemeral=True)
    
    # 2. 🟢 Extract all mentions from the message
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

@bot.tree.command(name="vc", description="Manage your personal 8-hour on-demand Voice Channel.")
@app_commands.choices(action=[
    app_commands.Choice(name="Create New VC", value="create"),
    app_commands.Choice(name="Add Members to existing VC", value="add"),
    app_commands.Choice(name="Revoke VC Access", value="revoke")
])
@app_commands.describe(mentions="Mention users (e.g., @user1 @user2). Leave empty if creating just for yourself.")
async def manage_vc(interaction: discord.Interaction, action: app_commands.Choice[str], mentions: str = ""):
    # 1. Must be used in bot-commands
    if "bot-commands" not in interaction.channel.name.lower():
        return await interaction.response.send_message("❌ Please use this command exclusively in `#🤖・bot-commands`.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)

    # 2. Level Validation Setup
    valid_roles = ROLE_NAMES + ["📍 Native Japanese"]
    user_levels = [r.name for r in interaction.user.roles if r.name in valid_roles]
    has_champion = any(r.name == "Weekly Champion" for r in interaction.user.roles)

    if not user_levels:
        return await interaction.followup.send("❌ You must have a JLPT (N5-N1) or Native Japanese role to use this feature.", ephemeral=True)

    # 3. Category Setup
    category = discord.utils.get(interaction.guild.categories, name="📣 ボイソ・チャト")
    if not category:
        category = await interaction.guild.create_category("📣 ボイソ・チャト")

    # 4. Parse Mentions & Check Level Matching
    target_members = []
    if mentions:
        # Extracts user IDs from mentions
        member_ids = [int(uid) for uid in re.findall(r'<@!?(\d+)>', mentions)]
        for uid in member_ids:
            member = interaction.guild.get_member(uid)
            if member and member != interaction.user:
                if not has_champion:
                    target_levels = [r.name for r in member.roles if r.name in valid_roles]
                    # Check if they share at least one JLPT role
                    if not set(user_levels).intersection(set(target_levels)):
                        return await interaction.followup.send(f"❌ JLPT Role levels not matching with {member.mention}. Only Weekly Champions can invite mixed levels!", ephemeral=True)
                target_members.append(member)

    # 5. Find if user already has an active VC
    # We identify ownership by checking if the user has specific 'move_members' permissions in the VC
    user_vc = None
    for vc in category.voice_channels:
        if vc.overwrites_for(interaction.user).move_members:
            user_vc = vc
            break

    # 🟢 ACTION: CREATE
    if action.value == "create":
        if user_vc:
            return await interaction.followup.send("❌ Your personal voice room already exists! You cannot make another until it gets automatically deleted after 8 hours.", ephemeral=True)

        overwrites = {
            # Visible to everyone, but they cannot connect by default
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
            # Creator can connect and gets hidden owner tag (move_members)
            interaction.user: discord.PermissionOverwrite(view_channel=True, connect=True, move_members=True)
        }
        
        # Add targets
        for tm in target_members:
            overwrites[tm] = discord.PermissionOverwrite(view_channel=True, connect=True)
            
        # Add Safety Admins
        jr_admin = discord.utils.get(interaction.guild.roles, name="Junior Admin（ジュニア・アデュミン）")
        sr_admin = discord.utils.get(interaction.guild.roles, name="Senior Admin（セィニア・アデュミン）")
        if jr_admin: overwrites[jr_admin] = discord.PermissionOverwrite(view_channel=True, connect=True)
        if sr_admin: overwrites[sr_admin] = discord.PermissionOverwrite(view_channel=True, connect=True)

        new_vc = await interaction.guild.create_voice_channel(
            name=f"{interaction.user.display_name}'s VC",
            category=category,
            overwrites=overwrites
        )
        
        ping_str = " ".join([m.mention for m in target_members])
        notif_msg = f"✅ {interaction.user.mention} created a personal VC: {new_vc.mention}! {ping_str}\n*(This channel and message will auto-delete in 8 hours)*"
        
        # Send public ping in channel, and it auto-deletes in 28800 seconds (8 hours)
        await interaction.channel.send(notif_msg, delete_after=28800)
        await interaction.followup.send("✅ VC Created successfully.", ephemeral=True)

    # 🟢 ACTION: ADD MEMBERS
    elif action.value == "add":
        if not user_vc:
            return await interaction.followup.send("❌ You don't have an active personal voice channel. Please create one first.", ephemeral=True)
        if not target_members:
            return await interaction.followup.send("❌ Please mention at least one valid user to add.", ephemeral=True)

        for tm in target_members:
            await user_vc.set_permissions(tm, view_channel=True, connect=True)

        ping_str = " ".join([m.mention for m in target_members])
        notif_msg = f"✅ {interaction.user.mention} granted VC access to {ping_str} for {user_vc.mention}!\n*(This message will auto-delete in 8 hours)*"
        
        await interaction.channel.send(notif_msg, delete_after=28800)
        await interaction.followup.send("✅ Members added successfully.", ephemeral=True)

    # 🟢 ACTION: REVOKE MEMBERS
    elif action.value == "revoke":
        if not user_vc:
            return await interaction.followup.send("❌ You don't have an active personal voice channel. Please create one first.", ephemeral=True)
        if not target_members:
            return await interaction.followup.send("❌ Please mention at least one valid user to revoke.", ephemeral=True)

        for tm in target_members:
            # Remove permissions
            await user_vc.set_permissions(tm, overwrite=None)
            # If they are currently in the VC, physically disconnect them
            if tm in user_vc.members:
                try: 
                    await tm.move_to(None) 
                except Exception: 
                    pass

        ping_str = " ".join([m.mention for m in target_members])
        notif_msg = f"🚫 {interaction.user.mention} revoked VC access from {ping_str} for {user_vc.mention}!\n*(This message will auto-delete in 8 hours)*"
        
        await interaction.channel.send(notif_msg, delete_after=28800)
        await interaction.followup.send("✅ Members revoked and disconnected successfully.", ephemeral=True)

# ==========================================
# 🎙️ SHADOW SPEAKING (OPTION B + UI PANEL + REPEAT PRIORITY)
# ==========================================
shadow_queues = {}
shadow_processing = {}

def check_accuracy(original, spoken):
    if not spoken: return 0.0
    return SequenceMatcher(None, original, spoken).ratio() * 100

async def delete_thread_later(thread, delay=1200):
    # 20 Minutes (1200 seconds) timer for auto-delete
    await asyncio.sleep(delay)
    try: await thread.delete()
    except: pass

async def process_shadow_queue(guild, client):
    shadow_processing[guild.id] = True
    
    while shadow_queues.get(guild.id, []):
        task = shadow_queues[guild.id][0]
        interaction = task["interaction"]
        user = interaction.user
        vc_channel = task["vc"]
        topic = task["topic"]
        level_short = task["level_short"]
        thread = task["thread"] 
        
        try:
            await thread.send(f"⏳ **{user.mention}, Sensei is preparing your session in {vc_channel.name}!** Generating {level_short} script...", delete_after=10)
            
            # 1. Fetch AI Data (4-Line Conversation Request)
            prompt = f"""You are a Japanese Sensei. 
            Task: Generate a natural 4-line conversational paragraph about '{topic}'.
            Constraint: You MUST strictly use grammar points and vocabulary specifically introduced at the JLPT {level_short} level.
            Return ONLY a valid JSON with keys: 
            "conversation": an array of EXACTLY 4 objects. Each object must have keys: "sentence" (kanji/kana), "romaji" (romaji reading), "english" (english meaning).
            "vocab": array of objects with "word", "reading", "meaning" for the vocabulary used in the whole conversation.
            "grammar": brief explanation of the specific {level_short} grammar structures used in the paragraph."""
            
            raw_text = await generate_gemini_response(prompt)
            data = extract_json(raw_text)
            if isinstance(data, list): data = data[0]
            
            conversation = data.get("conversation", [])
            # Fallback if AI messes up the array
            if not conversation:
                conversation = [{"sentence": data.get("sentence", "エラー"), "romaji": data.get("romaji", "era-"), "english": data.get("english", "Error")}]
                
            vocab_list = data.get("vocab", [])
            grammar_text = data.get("grammar", "")
            
            # 🟢 2. RANDOM NATIVE VOICE SELECTION FOR THIS SESSION
            import edge_tts
            import random
            session_voice = random.choice(["ja-JP-KeitaNeural", "ja-JP-NanamiNeural"])
            
            # 3. Connect to VC (Stays connected for the whole loop)
            voice_client = await vc_channel.connect()
            
            i = 0
            max_rounds = min(4, len(conversation))
            accumulated_ja, accumulated_ro, accumulated_en = "", "", ""
            timeout_occurred = False
            
            # 4. 🟢 THE PROGRESSIVE SHADOWING LOOP (4 ROUNDS)
            while i < max_rounds:
                current_line = conversation[i]
                
                # Accumulate gracefully
                if accumulated_ja:
                    current_ja = accumulated_ja + "。 " + current_line.get("sentence", "").replace("。", "")
                    current_ro = accumulated_ro + ". " + current_line.get("romaji", "").replace(".", "")
                    current_en = accumulated_en + ". " + current_line.get("english", "").replace(".", "")
                else:
                    current_ja = current_line.get("sentence", "").replace("。", "")
                    current_ro = current_line.get("romaji", "").replace(".", "")
                    current_en = current_line.get("english", "").replace(".", "")
                    
                if not current_ja.endswith("。"): current_ja += "。"
                    
                # 🟢 BOT SPEAKS USING EDGE-TTS
                tts_text = f"{current_ja} さあ、あなたの番です。"
                tts_filename = f"sensei_{guild.id}_round{i}.mp3"
                
                communicate = edge_tts.Communicate(tts_text, session_voice)
                await communicate.save(tts_filename)
                
                voice_client.play(discord.FFmpegPCMAudio(tts_filename))
                while voice_client.is_playing():
                    await asyncio.sleep(1)
                    
                repeat_event = asyncio.Event()

                class TurnView(View):
                    def __init__(self):
                        super().__init__(timeout=180.0)
                    @discord.ui.button(label="🔁 Repeat Sensei", style=discord.ButtonStyle.secondary)
                    async def btn_repeat(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
                        if btn_interaction.user.id != user.id:
                            return await btn_interaction.response.send_message("❌ This is not your session!", ephemeral=True)
                        await btn_interaction.response.defer()
                        repeat_event.set()

                turn_view = TurnView()
                turn_msg = await thread.send(
                    content=f"🎤 **ROUND {i+1}/{max_rounds} - IT'S YOUR TURN!**\nSend a **Voice Note / Audio File** here within 3 minutes repeating:\n\n> **{current_ja}**\n> *{current_ro}*", 
                    view=turn_view
                )
                
                def check_voice_msg(m):
                    return m.author.id == user.id and m.channel.id == thread.id and m.attachments
                    
                msg_task = asyncio.create_task(client.wait_for('message', check=check_voice_msg))
                rep_task = asyncio.create_task(repeat_event.wait())
                
                done, pending = await asyncio.wait([msg_task, rep_task], timeout=180.0, return_when=asyncio.FIRST_COMPLETED)
                for p in pending: p.cancel()
                
                if rep_task in done:
                    await turn_msg.edit(content=f"🔁 {user.mention} requested a repeat. Replaying...", view=None)
                    if os.path.exists(tts_filename): os.remove(tts_filename)
                    continue # Replay same round (doesn't increment i)
                    
                if not msg_task in done:
                    # INACTIVITY TIMEOUT
                    await turn_msg.edit(content=f"⏳ {user.mention}, 3 minutes inactivity! Session ended to save queue time.", view=None)
                    timeout_occurred = True
                    break 

                msg = msg_task.result()
                attachment = msg.attachments[0]
                
                if attachment.size > 2 * 1024 * 1024:
                    await thread.send(f"❌ File too large. Max 2MB. Replaying round...")
                    if os.path.exists(tts_filename): os.remove(tts_filename)
                    continue

                filename_lower = attachment.filename.lower()
                valid_extensions = ['.ogg', '.wav', '.mp3', '.m4a']
                is_valid_audio = ('voice-message' in filename_lower) or any(filename_lower.endswith(ext) for ext in valid_extensions)
                
                if not is_valid_audio:
                    await thread.send(f"❌ Invalid audio file format. Replaying round...")
                    if os.path.exists(tts_filename): os.remove(tts_filename)
                    continue
                
                await turn_msg.edit(view=None)

                # Process Audio (No Discord Message Deletion - User Keeps Voice Notes!)
                ext = os.path.splitext(attachment.filename)[1].lower()
                if not ext: ext = ".ogg"
                
                raw_audio_file = f"user_{user.id}_raw{ext}"
                wav_file = f"user_{user.id}.wav"
                
                await attachment.save(raw_audio_file)
                audio = AudioSegment.from_file(raw_audio_file)
                audio.export(wav_file, format="wav")
                
                # STT & Evaluate
                recognizer = sr.Recognizer()
                spoken_text = ""
                try:
                    with sr.AudioFile(wav_file) as source:
                        audio_data = recognizer.record(source)
                        spoken_text = recognizer.recognize_google(audio_data, language="ja-JP")
                except sr.UnknownValueError:
                    await thread.send("❌ Couldn't hear clear Japanese. Replaying this round...")
                    for f in [raw_audio_file, wav_file, tts_filename]:
                        if os.path.exists(f): os.remove(f)
                    continue
                    
                accuracy = check_accuracy(current_ja, spoken_text)
                color = 0x2ecc71 if accuracy >= 75 else (0xf1c40f if accuracy >= 50 else 0xe74c3c)
                
                round_embed = discord.Embed(title=f"Round {i+1} Result", color=color)
                round_embed.add_field(name="What We Heard", value=spoken_text, inline=False)
                round_embed.add_field(name="Accuracy", value=f"**{accuracy:.1f}%**", inline=False)
                await thread.send(embed=round_embed)
                
                # Prepare for next loop & Local Cleanup
                accumulated_ja = current_ja
                accumulated_ro = current_ro
                accumulated_en = current_en
                
                for f in [raw_audio_file, wav_file, tts_filename]:
                    if os.path.exists(f): os.remove(f)
                    
                i += 1 # Move to next round
                
            # 5. End of Loop & Final Summary
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect()
                
            if not timeout_occurred and i == max_rounds:
                final_embed = discord.Embed(title="🏆 Shadow Speaking Complete!", description="Awesome job completing all 4 rounds!", color=0x3498db)
                final_embed.add_field(name="Full Conversation", value=f"**{accumulated_ja}**\n\n*{accumulated_en}*", inline=False)
                
                class FinalView(View):
                    def __init__(self, u, v_data, g_data):
                        super().__init__(timeout=None)
                        self.user = u
                        self.v_data = v_data
                        self.g_data = g_data

                    @discord.ui.button(label="📖 Vocabulary Used", style=discord.ButtonStyle.primary)
                    async def btn_vocab(self, i: discord.Interaction, btn: discord.ui.Button):
                        if i.user.id != self.user.id: return
                        v_txt = "".join([f"**{v.get('word', '')}** ({v.get('reading', '')}) - {v.get('meaning', '')}\n" for v in self.v_data])
                        await i.response.send_message(embed=discord.Embed(title="📖 Vocabulary List", description=v_txt, color=0x3498db), ephemeral=True)

                    @discord.ui.button(label="🧠 Grammar Breakdown (Pro)", style=discord.ButtonStyle.success)
                    async def btn_grammar(self, i: discord.Interaction, btn: discord.ui.Button):
                        if i.user.id != self.user.id: return
                        if not any(r.name == "金 Pro Learners 金" for r in i.user.roles):
                            return await i.response.send_message("🔒 **Pro Feature:** Upgrade to view deep grammar breakdowns!", ephemeral=True)
                        await i.response.send_message(embed=discord.Embed(title="🧠 Grammar Breakdown", description=self.g_data, color=0x2ecc71), ephemeral=True)
                
                view = FinalView(user, vocab_list, grammar_text)
                await thread.send(content=f"✅ {user.mention} Session Finished! *(This thread will auto-delete in 20 minutes)*", embed=final_embed, view=view)
                
            if isinstance(thread, discord.Thread):
                client.loop.create_task(delete_thread_later(thread, 1200)) # 20 Min Timer starts AFTER session ends
                
        except Exception as e:
            await thread.send(f"❌ An error occurred: {e}")
            if 'voice_client' in locals() and voice_client and voice_client.is_connected():
                await voice_client.disconnect()
            if isinstance(thread, discord.Thread): client.loop.create_task(delete_thread_later(thread, 1200))
        finally:
            if shadow_queues.get(guild.id):
                shadow_queues[guild.id].pop(0)
                
    shadow_processing[guild.id] = False


class ShadowTopicModal(discord.ui.Modal, title='Start Shadow Speaking'):
    topic_input = discord.ui.TextInput(
        label='What should Sensei talk about?',
        style=discord.TextStyle.short,
        placeholder='e.g., At a cafe, introducing myself, Anime...',
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        topic = self.topic_input.value
        
        user_level_role = next((r.name for r in interaction.user.roles if r.name in ROLE_NAMES), None)
        if not user_level_role:
            return await interaction.response.send_message("❌ Please select a JLPT Learner role (N5-N1) first.", ephemeral=True)
            
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("❌ Please join a Voice Channel first! Sensei will speak there.", ephemeral=True)
            
        level_short = user_level_role.split(" ")[1]
        vc_channel = interaction.user.voice.channel
        guild_id = interaction.guild.id
        
        if guild_id not in shadow_queues:
            shadow_queues[guild_id] = []
            
        for task in shadow_queues[guild_id]:
            if task["interaction"].user.id == interaction.user.id:
                return await interaction.response.send_message("⚠️ You are already in the queue! Please wait for your turn.", ephemeral=True)
                
        try:
            thread = await interaction.channel.create_thread(
                name=f"🎙️ {interaction.user.name}'s Session", 
                type=discord.ChannelType.private_thread, 
                invitable=False
            )
            await thread.add_user(interaction.user)
        except Exception:
            thread = interaction.channel
            
        shadow_queues[guild_id].append({
            "interaction": interaction,
            "vc": vc_channel,
            "topic": topic,
            "level_short": level_short,
            "thread": thread 
        })
        
        queue_position = len(shadow_queues[guild_id])
        
        if queue_position == 1 and not shadow_processing.get(guild_id, False):
            msg = f"✅ **Session Started!**\n👉 **Click here to join your private room:** {thread.mention}\n\n*Listen to Sensei in {vc_channel.mention} and send your audio in the {thread.mention} only.*"
            await interaction.response.send_message(msg, ephemeral=True)
            asyncio.create_task(process_shadow_queue(interaction.guild, interaction.client))
        else:
            # 🟢 DYNAMIC QUEUE TIMER ADDED
            estimated_wait = (queue_position - 1) * 10
            msg = f"⏳ **Added to Queue (Position: {queue_position})**\n⏱️ **Estimated Wait Time:** ~{estimated_wait} minutes.\n👉 **Your private room:** {thread.mention}\n\n*Please wait in the thread for your turn.*"
            await interaction.response.send_message(msg, ephemeral=True)
            if not shadow_processing.get(guild_id, False):
                asyncio.create_task(process_shadow_queue(interaction.guild, interaction.client))

class PersistentShadowPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="▶️ Start Shadow Speaking", style=discord.ButtonStyle.success, custom_id="shadow_start")
    async def btn_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ShadowTopicModal())
        
    @discord.ui.button(label="❓ How it Works", style=discord.ButtonStyle.secondary, custom_id="shadow_help")
    async def btn_help(self, interaction: discord.Interaction, button: discord.ui.Button):
        help_text = (
            "**🎙 Shadow Speaking Guide:**\n\n"
            "1. **Join any Voice Channel**.\n"
            "2. Click **▶️ Start Shadow Speaking** and type a topic.\n"
            "3. The bot will create a **Private Thread** just for you.\n"
            "4. AI Sensei will join your VC and start a **4-Round Conversation**.\n"
            "5. Send your Voice Recording by repeating what Sensei said. Each round, the conversation gets longer!\n"
            "6. Use native discord voice recording (bottom right), if using discord mobile app, if not then use your device's voice recording software and share the audio file in private channel.\n"
            "7. Get your pronunciation score, vocab list, and grammar breakdown at the end.\n\n"
            "*Note: The private thread will auto-delete 20 minutes after completion.*"
        )
        embed = discord.Embed(title="How to Shadow Speak", description=help_text, color=0x3498db)
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setupshadow", description="[Admin Only] Drop the Shadow Speaking UI panel in the current channel.")
async def setup_shadow(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    has_permission = any(role.name in ["Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied.", ephemeral=True)
        
    embed = discord.Embed(
        title="🎙️ A.I. SHADOW SPEAKING PANEL", 
        description="Master your Japanese pronunciation and fluency!\n\nClick the **▶️ Start Shadow Speaking** button below to spawn AI Sensei in your voice channel. You will be assigned a private room to record and check your speaking accuracy progressively.", 
        color=0x9b59b6
    )
    
    await interaction.channel.send(embed=embed, view=PersistentShadowPanelView())
    await interaction.followup.send("✅ Panel deployed successfully!", ephemeral=True)

# ---------------------------------------------------------
# 📸 VISUAL MEMORY VAULT (VOCAB-DEX)
# ---------------------------------------------------------
@bot.tree.command(name="vocabvault", description="Create your personal Smart Visual Vocabulary Vault.")
async def vocab_vault(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    # 🟢 NAYA 1: Strict Channel Restriction Check
    expected_channel_name = "📸・vocab-vault"
    if interaction.channel.name != expected_channel_name:
        vault_channel = discord.utils.get(interaction.guild.channels, name=expected_channel_name)
        ch_mention = vault_channel.mention if vault_channel else f"#{expected_channel_name}"
        return await interaction.followup.send(f"❌ This command only runs in this channel {ch_mention}.\n\nPlease click to navigate directly and re-run the command.", ephemeral=True)
    
    # 🟢 2. Check if user has learner role
    if not any(r.name in ROLE_NAMES for r in interaction.user.roles):
        return await interaction.followup.send("❌ You need a JLPT Learner role (N5-N1) to unlock the Vocab Vault.", ephemeral=True)
        
    guild = interaction.guild
    
    # Ensure the main category and channel exist
    vault_category = discord.utils.get(guild.categories, name="🧠 SMART LEARNING")
    if not vault_category:
        vault_category = await guild.create_category("🧠 SMART LEARNING")
        
    main_channel = discord.utils.get(guild.channels, name="📸・vocab-vault")
    if not main_channel:
        main_channel = await guild.create_text_channel(name="📸・vocab-vault", category=vault_category)

    # 🟢 3. CHECK IF VAULT ALREADY EXISTS
    thread_name = f"🧵 {interaction.user.name}'s Vault"
    existing_thread = discord.utils.get(main_channel.threads, name=thread_name)
    
    if existing_thread:
        return await interaction.followup.send(f"⚠️ You already have an active Vocab Vault!\n👉 **Jump here:** {existing_thread.mention}\n\n*Just drop your photos inside that thread!*", ephemeral=True)

    # 🟢 4. CREATE NEW VAULT THREAD
    try:
        dummy_msg = await main_channel.send(f"Vault created for {interaction.user.mention}.")
        thread = await dummy_msg.create_thread(
            name=thread_name, 
            auto_archive_duration=10080 
        )
        await dummy_msg.delete() 
        
        embed = discord.Embed(
            title="📸 Welcome to your Visual Memory Vault!", 
            description="Your personal Japanese Pokédex is ready.\n\n**How to use:**\n1. Take a photo of ANY object in real life.\n2. **Drop/Upload the photo directly into your personal thread not visible to anyone else.**\n3. Sensei will automatically scan it and help you memorize it forever via flashcard quizes!\n\n*(No commands needed. Just send photos!)*\n\n:lock: **`Privacy Disclaimer:** Your images are processed securely and are NEVER stored on our servers except for discord, still please avoid uploading sensitive personal information (IDs, faces, etc).`", 
            color=0x2ecc71
        )
        await thread.send(content=interaction.user.mention, embed=embed)
        await interaction.followup.send(f"✅ **Vault Activated!**\n👉 **Click here to enter:** {thread.mention}", ephemeral=True)
        
        # 🟢 Public Announcement for everyone to see
        await main_channel.send(f"🎉 **{interaction.user.display_name}** has successfully created their private **Vocabulary Vault**! You can also type `/vocabvault` to start your vocab learning by proven scientific method **`SRS`**.")
        
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to create Vault: {e}", ephemeral=True)

@bot.tree.command(name="deletevault", description="Permanently delete your personal Vocab Vault and all saved words.")
async def delete_vault(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    guild = interaction.guild
    main_channel = discord.utils.get(guild.channels, name="📸・vocab-vault")
    if not main_channel:
        return await interaction.followup.send("❌ Vault channel not found.", ephemeral=True)
        
    thread_name = f"🧵 {interaction.user.name}'s Vault"
    existing_thread = discord.utils.get(main_channel.threads, name=thread_name)
    
    if not existing_thread:
        return await interaction.followup.send("❌ You don't have an active Vocab Vault to delete.", ephemeral=True)
        
    embed = discord.Embed(
        title="⚠️ Delete Vocab Vault?", 
        description="Are you sure you want to permanently delete your Visual Memory Vault? This will wipe all your saved words and study progress.\n\n**This action CANNOT be undone.**", 
        color=discord.Color.red()
    )
    
    view = DeleteVaultView(interaction.user, existing_thread)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# ==========================================
# 📸 VISUAL MEMORY VAULT (SILENT SCANNER CORE)
# ==========================================
async def handle_nsfw_vault(guild, thread, user):
    log_ch = discord.utils.get(guild.channels, name="admin-logs")
    if not log_ch: return
    
    founder = discord.utils.get(guild.roles, name="Founder（ファウンダ）")
    sr_admin = discord.utils.get(guild.roles, name="Senior Admin（セィニア・アデュミン）")
    
    ping_str = f"{founder.mention if founder else ''} {sr_admin.mention if sr_admin else ''}"
    embed = discord.Embed(
        title="🚨 NSFW Upload Detected in Vocab Vault", 
        description=f"User {user.mention} uploaded an image that triggered Content Safety Filters in their private vault: {thread.mention}.\n\nSenior Admins have been temporarily added to the thread for 24 hours to review the content. Access will be revoked automatically after 24 hours.", 
        color=0xe74c3c
    )
    
    await log_ch.send(content=ping_str, embed=embed)
    
    if sr_admin:
        for admin_member in sr_admin.members:
            try: await thread.add_user(admin_member)
            except: pass
            
        async def remove_admin_access():
            await asyncio.sleep(86400) # Wait exactly 24 hours
            for admin_member in sr_admin.members:
                try: await thread.remove_user(admin_member)
                except: pass
                
        bot.loop.create_task(remove_admin_access())

class VaultVocabSelect(Select):
    def __init__(self, options_data, img_url):
        self.options_data = options_data
        self.img_url = img_url
        
        select_options = []
        for idx, opt in enumerate(options_data):
            label_name = f"{opt.get('kanji', 'Unknown')} - {opt.get('english', 'Unknown')}"
            select_options.append(discord.SelectOption(label=label_name[:100], value=str(idx), description=f"{opt.get('kana', '')} [{opt.get('jlpt', 'Unknown')}]", emoji="🇯🇵"))
            
        super().__init__(placeholder="Select the word to memorize...", min_values=1, max_values=1, options=select_options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_vocab = self.options_data[int(self.values[0])]
        word_kanji = selected_vocab.get('kanji', '')
        word_kana = selected_vocab.get('kana', '')
        word_jlpt = selected_vocab.get('jlpt', 'Unknown')
        
        # 🟢 SMART JLPT FEEDBACK LOGIC
        user_roles = [r.name for r in interaction.user.roles if r.name in ROLE_NAMES]
        user_lvl_short = user_roles[0].split(" ")[1] if user_roles else "N5"
        
        level_map = {"N5": 1, "N4": 2, "N3": 3, "N2": 4, "N1": 5, "Rare": 6}
        u_val = level_map.get(user_lvl_short, 1)
        w_val = level_map.get(word_jlpt, 6)
        
        if w_val == 6:
            feedback_msg = "Rare catch! This word is outside the standard syllabus. Impressive vocabulary!"
        elif w_val == u_val:
            feedback_msg = f"Perfect! This {word_jlpt} word is exactly what you need to focus on right now."
        elif w_val > u_val:
            feedback_msg = f"Whoa, ambitious! This is an advanced {word_jlpt} word. It might be challenging, but great for your future!"
        else:
            feedback_msg = f"Nice revision! A solid {word_jlpt} word to keep your foundations strong."

        linked_kanji_display = word_kanji
        db_kanjis = kanji_db.find({"url": {"$exists": True}})
        for doc in db_kanjis:
            k_char = doc.get("kanji_char")
            k_url = doc.get("url")
            if k_char and k_char in linked_kanji_display:
                linked_kanji_display = linked_kanji_display.replace(k_char, f"[{k_char}]({k_url})")
        
        # 🟢 DYNAMIC IMAGE URL GRABBER (Fixes the blank flashcard bug!)
        final_img_url = self.img_url
        if interaction.message.embeds and interaction.message.embeds[0].image:
            final_img_url = interaction.message.embeds[0].image.url
        elif interaction.message.attachments:
            final_img_url = interaction.message.attachments[0].url
            
        # Save to MongoDB
        next_review = int(time.time()) + 86400 # 24 hours
        vocab_db.insert_one({
            "user_id": interaction.user.id,
            "kanji": word_kanji,
            "kana": word_kana,
            "english": selected_vocab.get("english"),
            "image_url": final_img_url,  # 🟢 Safe URL used
            "level": 1,
            "next_review": next_review
        })
        
        await interaction.edit_original_response(content=f"✅ **{linked_kanji_display}** ({word_kana}) has been secured in your Level 1 Memory!\n💡 *Sensei says: {feedback_msg}* I'll ping you for flashcard quiz after sometime.", embed=None, view=None)

class VaultVocabSelectView(View):
    def __init__(self, user, options_data, img_url, original_message):
        super().__init__(timeout=120)
        self.user = user
        self.original_message = original_message
        self.add_item(VaultVocabSelect(options_data, img_url))
        
    @discord.ui.button(label="Not what I wanted (Delete Image)", style=discord.ButtonStyle.danger, row=1, emoji="🗑️")
    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        
        # Acknowledge the button click quietly
        await interaction.response.defer() 
        # 1. Deleting user's message
        try: 
            await self.original_message.delete()
        except: pass
        # 2. Deleting bot's flashcard from chat
        try:
            await interaction.message.delete()
        except: pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ This is not your photo!", ephemeral=True)
            return False
        return True

# ==========================================
# 🟢 HELPER: PREMIUM FLASHCARD GENERATOR
# ==========================================
async def generate_flashcard_bytes(word_eng):
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    import random, io, textwrap
    
    size = 600
    img = Image.new('RGB', (size, size), color=(15, 15, 20))
    draw = ImageDraw.Draw(img)
    
    palettes = [
        [(255, 0, 128), (0, 255, 255), (128, 0, 255)], [(255, 128, 0), (255, 0, 128), (255, 200, 0)],
        [(0, 255, 128), (0, 128, 255), (0, 255, 255)], [(147, 51, 234), (236, 72, 153), (245, 158, 11)]
    ]
    card_colors = random.choice(palettes)
    
    for color in card_colors:
        x, y = random.randint(0, size), random.randint(0, size)
        r = random.randint(200, 400)
        draw.ellipse([x-r, y-r, x+r, y+r], fill=color)
        
    img = img.filter(ImageFilter.GaussianBlur(radius=120))
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 140))
    img = img.convert('RGBA')
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)
    
    display_text = word_eng.upper()
    max_width, font_size = size - 80, 75
    
    # 🟢 FIXED: Default values set explicitly so it NEVER crashes on long texts
    text_w, text_h = max_width, size / 2 
    wrapped_text = display_text
    
    try: font = ImageFont.truetype("pixel.ttf", font_size)
    except: font = ImageFont.load_default()
    
    while font_size > 15:
        try: font = ImageFont.truetype("pixel.ttf", font_size)
        except: font = ImageFont.load_default(); break
            
        char_width_estimate = font_size * 0.75 
        chars_per_line = max(int(max_width / char_width_estimate), 8)
        wrapped_lines = textwrap.wrap(display_text, width=chars_per_line, break_long_words=True)
        wrapped_text = "\n".join(wrapped_lines)
        
        max_line_width = 0
        for line in wrapped_lines:
            lw = draw.textlength(line, font=font)
            if lw > max_line_width: max_line_width = lw
                
        text_h = len(wrapped_lines) * (font_size * 1.3)
        text_w = max_line_width # 🟢 Variable assigned safely in EVERY loop iteration
        
        if max_line_width <= max_width and text_h <= (size - 80):
            break # Perfect fit!
            
        font_size -= 4
        
    draw.multiline_text(((size - text_w) / 2, (size - text_h) / 2), wrapped_text, font=font, fill=(255, 255, 255, 255), align="center")
    arr = io.BytesIO()
    img.save(arr, format='PNG')
    arr.seek(0)
    return arr

class BulkVocabSelect(Select):
    def __init__(self, options_data, original_message):
        self.options_data = options_data
        self.original_message = original_message
        
        select_options = []
        for idx, opt in enumerate(options_data):
            label_name = f"{opt.get('kanji', 'Unknown')} - {opt.get('english', 'Unknown')}"
            select_options.append(discord.SelectOption(label=label_name[:100], value=str(idx), description=f"{opt.get('kana', '')} [{opt.get('jlpt', 'Unknown')}]", emoji="🎌"))
            
        # User can select multiple options!
        super().__init__(placeholder="Select correct words to save...", min_values=1, max_values=len(options_data), options=select_options)

    async def callback(self, interaction: discord.Interaction):
        import time
        await interaction.response.defer()
        
        try: await self.original_message.delete()
        except: pass
        
        await interaction.edit_original_response(content="⏳ **Generating premium flashcards and saving... Please wait!**", embed=None, view=None)
        
        files = []
        embeds = []
        
        for i, val in enumerate(self.values):
            vocab_item = self.options_data[int(val)]
            word_eng = str(vocab_item.get("english", "Unknown"))
            
            arr = await generate_flashcard_bytes(word_eng)
            filename = f"flashcard_{i}.png"
            file = discord.File(arr, filename=filename)
            files.append(file)
            
            embed = discord.Embed(color=0x3498db)
            embed.set_image(url=f"attachment://{filename}")
            embed.set_footer(text=f"{vocab_item.get('kanji')} ({vocab_item.get('kana')}) - {vocab_item.get('jlpt')}")
            embeds.append(embed)
            
        # Send one single message with ALL generated flashcards attached!
        msg = await interaction.channel.send(files=files, embeds=embeds)
        
        # Save to DB using the generated Discord URLs
        for i, val in enumerate(self.values):
            vocab_item = self.options_data[int(val)]
            
            # 🟢 SAFE URL GRABBER FOR BULK (Fixes the IndexError crash!)
            img_url = ""
            if len(msg.embeds) > i and msg.embeds[i].image:
                img_url = msg.embeds[i].image.url
            elif len(msg.attachments) > i:
                img_url = msg.attachments[i].url
            
            vocab_db.insert_one({
                "user_id": interaction.user.id,
                "kanji": vocab_item.get("kanji", ""),
                "kana": vocab_item.get("kana", ""),
                "english": vocab_item.get("english", ""),
                "image_url": img_url,
                "level": 1,
                "next_review": int(time.time()) + 86400
            })
            
        await interaction.edit_original_response(content=f"✅ **{len(self.values)} Flashcards successfully generated and saved to your Vault! I will ping you for SRS quiz eventually.**", embed=None, view=None)

class BulkVocabSelectView(View):
    def __init__(self, user, options_data, original_message):
        super().__init__(timeout=120)
        self.user = user
        self.original_message = original_message
        self.add_item(BulkVocabSelect(options_data, original_message))
        
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1, emoji="🗑️")
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        await interaction.response.defer()
        try: await self.original_message.delete()
        except: pass
        try: await interaction.message.delete()
        except: pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ This is not your session!", ephemeral=True)
            return False
        return True

# 🟢 THE SILENT EVENT LISTENER
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # 🟢 STRICT MODERATION FOR THE #vocab-vault CHANNEL
    if not isinstance(message.channel, discord.Thread) and message.channel.name == "📸・vocab-vault":
        # Delete user's text message silently
        try: await message.delete()
        except: pass
        
        # Find the ticket channel to ping it dynamically
        ticket_ch = discord.utils.get(message.guild.channels, name="🎫・create-a-ticket")
        ticket_mention = ticket_ch.mention if ticket_ch else "#🎫・create-a-ticket"
        
        # Send a warning message that auto-deletes after 10 seconds
        await message.channel.send(f"⚠️ {message.author.mention}, you can only use the `/vocabvault` command here.\nAny queries please click here -> {ticket_mention} and an admin will answer them ASAP.", delete_after=10)
        return # Stop processing, don't run commands for this message
        
    # 🟢 SILENT PHOTO SCANNER (For 📸・vocab-vault threads)    
    if isinstance(message.channel, discord.Thread) and message.channel.parent:
        if message.channel.parent.name == "📸・vocab-vault" and message.attachments:
            attachment = message.attachments[0]
            
            if attachment.content_type and attachment.content_type.startswith("image/"):
                thread = message.channel
                user = message.author
                
                if f"{user.name}'s Vault" not in thread.name:
                    return
                    
                temp_msg = await thread.send(f"👀 Sensei is analyzing your photo, {user.mention}...")
                
                prompt = """Analyze this image. Identify the 3 most prominent objects, concepts, or actions shown. 
                Return the results STRICTLY as a JSON array of 3 objects. 
                Each object must have: 'kanji' (the Japanese word in kanji or kana), 'kana' (hiragana/katakana reading), 'english' (meaning), and 'jlpt' (the approximate JLPT level of this word, strictly one of: "N5", "N4", "N3", "N2", "N1", or "Rare").
                Do not return anything outside the JSON array.
                Example: [{"kanji": "車", "kana": "くるま", "english": "Car", "jlpt": "N5"}]"""
                
                try:
                    raw_text = await generate_vision_response(prompt, attachment.url)
                    vocab_options = extract_json(raw_text)
                    
                    if not vocab_options or len(vocab_options) < 1:
                        raise Exception("Could not extract vocabulary.")
                        
                    view = VaultVocabSelectView(user, vocab_options, attachment.url, message)
                    
                    embed = discord.Embed(title="👀 Sensei analyzed your photo!", description="What do you want to learn? Choose from the dropdown below:", color=0x3498db)
                    for idx, v in enumerate(vocab_options):
                        embed.add_field(name=f"Option {idx+1}", value=f"**{v.get('kanji', '')}** ({v.get('kana', '')}) - [{v.get('jlpt', 'Unknown')}]\n{v.get('english', '')}", inline=False)
                        
                    await temp_msg.edit(content=user.mention, embed=embed, view=view)
                    
                except Exception as e:
                    error_str = str(e)
                    if "NSFW_FLAGGED" in error_str:
                        await temp_msg.edit(content="⚠️ **Sensei cannot process this image due to safety filters. This incident has been logged. An Admin might contact you.**")
                        await handle_nsfw_vault(message.guild, thread, user)
                    else:
                        await temp_msg.edit(content=f"❌ Failed to analyze image: {error_str}")

    # ---------------------------------------------------------
    # 🟢 TEXT TO FLASHCARD (AI Text Vocab Analyzer & Image Generator)
    # ---------------------------------------------------------
    if isinstance(message.channel, discord.Thread) and message.channel.parent:
        if message.channel.parent.name == "📸・vocab-vault" and not message.attachments:
            thread = message.channel
            user = message.author
            
            if f"{user.name}'s Vault" not in thread.name:
                return

            user_text = message.content.strip()
            if len(user_text) < 3 or len(user_text) > 500: return 
            if len(set(user_text.replace(" ", ""))) < 3: return
            if len(user_text.split()) > 100: return
                
            temp_msg = await thread.send(f"⏱️ Give me a sec, 🎴 analyzing your words, {user.mention}...")
            
            prompt = f"""The user typed: {message.content}
            Task: Extract ALL Japanese vocabulary words or phrases provided by the user (even if typed in romaji, english or hindi).
            If the text is just conversational noise (e.g., 'hello', 'how does this work?'), return strictly an empty array: []
            If it contains vocab words (e.g., 'Oboeru means to remember' or 'Arimasu - to exist'), and if there is a spelling mistake correct it then extract EACH ONE into a separate object and return an array of objects.
            Format of each object: {{"kanji": "The proper kanji (if any)", "kana": "The hiragana/katakana reading", "english": "The English meaning", "jlpt": "Approximate JLPT Level (N5-N1 or Rare)"}}
            Output ONLY a valid JSON array."""
            
            try:
                raw_text = await generate_gemini_response(prompt)
                data = extract_json(raw_text)
                
                if not isinstance(data, list):
                    if isinstance(data, dict) and "kanji" in data: data = [data]
                    else: data = []
                
                if len(data) == 0 or not data[0].get("kanji"):
                    return await temp_msg.delete()
                    
                # 🟢 THE HYBRID LOGIC
                if len(data) > 1:
                    # SCENARIO B: BULK ADD (2 to 5+ words)
                    extra_words_flag = False
                    if len(data) > 5:
                        data = data[:5] # ✂️ CUT TO MAX 5 (Hard Cap)
                        extra_words_flag = True
                        
                    warn_text = "\n\n⚠️ *Sensei noticed you sent a lot of words! To maintain focus, only the first 5 words have been extracted.*" if extra_words_flag else ""
                    
                    embed = discord.Embed(
                        title="👀 Bulk Vocab Detected!", 
                        description=f"Sensei extracted multiple words! Please select the correct ones from the dropdown below to generate flashcards.{warn_text}", 
                        color=0x3498db
                    )
                    
                    for idx, v in enumerate(data):
                        embed.add_field(name=f"Option {idx+1}", value=f"**{v.get('kanji', '')}** ({v.get('kana', '')}) - {v.get('english', '')}", inline=False)
                        
                    view = BulkVocabSelectView(user, data, message)
                    await temp_msg.edit(content=user.mention, embed=embed, view=view)
                    return # End here. Let the Dropdown take over!
                    
                # SCENARIO A: SINGLE WORD (Classic Flow)
                vocab_item = data[0]
                word_kanji = vocab_item.get("kanji", "")
                word_kana = vocab_item.get("kana", "")
                word_eng = str(vocab_item.get("english", "Unknown"))
                word_jlpt = vocab_item.get("jlpt", "N5")
                
                # 🟢 CALLING HELPER FUNCTION line 3112
                arr = await generate_flashcard_bytes(word_eng)
                file = discord.File(arr, filename="flashcard.png")
                
                embed = discord.Embed(
                    title="👀 Sensei extracted a new word!", 
                    description=f"Sensei decoded your text into:\n**{word_kanji}** ({word_kana}) - *{word_eng}* [{word_jlpt}]\n\nDo you want to save this flashcard to your Vocab-Vault?", 
                    color=0x3498db
                )
                embed.set_image(url="attachment://flashcard.png")
                
                # Single Word Dropdown View
                vocab_options = [{"kanji": word_kanji, "kana": word_kana, "english": word_eng, "jlpt": word_jlpt}]
                view = VaultVocabSelectView(user, vocab_options, "attachment://flashcard.png", message)
                
                # 🟢 New Logic
                try: 
                    await temp_msg.delete()
                except: 
                    pass
                
                # Sirf message send karo. Yahan se URL nikalne ki koi "kachra" line mat likhna!
                await thread.send(content=user.mention, embed=embed, file=file, view=view)
                
            except Exception as e:
                # Agar Gemini API fail hoti hai sirf tabhi ye error dikhega
                try: await temp_msg.edit(content=f"❌ Failed to analyze text: {e}")
                except: await thread.send(content=f"❌ Failed to analyze text: {e}")
                        
    await bot.process_commands(message)

# ==========================================
# 🧠 VISUAL MEMORY VAULT (SPACED REPETITION ENGINE)
# ==========================================
import random

class FlashcardQuizView(View):
    def __init__(self, user, words, current_idx=0):
        super().__init__(timeout=None)
        self.user = user
        self.words = words
        self.current_idx = current_idx
        self.setup_ui()
        
    def setup_ui(self):
        word = self.words[self.current_idx]
        self.correct_kanji = word.get('kanji', 'Error')
        self.current_word_data = word
        
        # 🟢 SMART DISTRACTORS: Pick 3 random words from DB as wrong options (Zero API Call!)
        distractors = list(vocab_db.aggregate([
            {"$match": {"kanji": {"$ne": self.correct_kanji}}},
            {"$sample": {"size": 3}}
        ]))
        
        options = [word] + distractors
        
        # Fallback words if DB is too new/empty
        fallbacks = [
            {"kanji": "猫", "kana": "ねこ", "english": "Cat"},
            {"kanji": "水", "kana": "みず", "english": "Water"},
            {"kanji": "本", "kana": "ほん", "english": "Book"},
            {"kanji": "車", "kana": "くるま", "english": "Car"}
        ]
        while len(options) < 4:
            options.append(fallbacks.pop())
            
        random.shuffle(options)
        
        for idx, opt in enumerate(options):
            label = f"{opt.get('kanji', '')} ({opt.get('kana', '')})"
            btn = Button(label=label[:80], custom_id=f"vocab_{idx}_{opt.get('kanji')}", style=discord.ButtonStyle.primary)
            btn.callback = self.answer_callback
            self.add_item(btn)

    async def answer_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id: return
        
        selected_kanji = interaction.data['custom_id'].split("_", 2)[2]
        is_correct = (selected_kanji == self.correct_kanji)
        
        current_lvl = self.current_word_data.get('level', 1)
        doc_id = self.current_word_data.get('_id')
        
        if is_correct:
            # 🟢 NAYA LOGIC: Mastery & DB Cleanup
            if current_lvl >= 4: 
                vocab_db.delete_one({"_id": doc_id})
                msg = f"🏆 **MASTERED!**\n\n✅ **Correct!** You have successfully committed **{self.correct_kanji}** to your long-term memory! It has been officially archived from your daily flashcards.\n\n*🧠 Sensei's Logic: You've proven you know this word deeply. We have removed it from the database to keep your daily reviews fresh and focused on newer, harder words!*"
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                new_lvl = current_lvl + 1
                intervals = {1: 1, 2: 3, 3: 7, 4: 14} # Spaced Repetition Days
                add_days = intervals[new_lvl]
                next_review = int(time.time()) + (add_days * 86400)
                
                vocab_db.update_one({"_id": doc_id}, {"$set": {"level": new_lvl, "next_review": next_review}})
                
                msg = f"✅ **Correct!**\n\n📈 **Level Promoted:** Level {current_lvl} ➡️ Level {new_lvl}\n📅 **Next Review:** In {add_days} Days.\n\n*🧠 Sensei's Logic: Memory builds through Spaced Repetition. You remembered it today, so we will wait longer ({add_days} days) before testing you again to lock it in your long-term memory!*"
                await interaction.response.send_message(msg, ephemeral=True)
            
        else:
            new_lvl = 1
            next_review = int(time.time()) + 86400 # 1 Day
            vocab_db.update_one({"_id": doc_id}, {"$set": {"level": new_lvl, "next_review": next_review}})
            
            msg = f"❌ **Incorrect! The correct answer was {self.correct_kanji} ({self.current_word_data.get('english')}).**\n\n📉 **Level Demoted:** Level {current_lvl} ➡️ Level 1\n📅 **Next Review:** Tomorrow.\n\n*🧠 Sensei's Logic: Forgetting is a normal part of learning. Since you missed it, we reset the word to Level 1 and will test you again tomorrow to strengthen the foundation.*"
            
            has_pro = any(r.name == "金 Pro Learners 金" for r in interaction.user.roles)
            if has_pro:
                await interaction.response.defer(ephemeral=True)
                prompt = f"The user forgot the Japanese word '{self.correct_kanji}' (reading: {self.current_word_data.get('kana')}, meaning: {self.current_word_data.get('english')}). Create a very short, funny, 1-2 sentence memory trick/mnemonic combining its English pronunciation and meaning to help them remember it forever."
                try:
                    trick = await generate_gemini_response(prompt)
                    msg += f"\n\n🎁 **Pro Mnemonic Trick:**\n*{trick}*"
                except: pass
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
                
        # 🟢 Move to next question
        self.current_idx += 1
        if self.current_idx < len(self.words):
            new_view = FlashcardQuizView(self.user, self.words, self.current_idx)
            next_word = self.words[self.current_idx]
            
            embed = discord.Embed(title=f"📸 Visual Flashcard ({self.current_idx+1}/{len(self.words)})", description="What is the Japanese word for the object shown in this image?", color=0x3498db)
            embed.set_image(url=next_word['image_url'])
            
            await interaction.message.edit(embed=embed, view=new_view)
        else:
            embed = discord.Embed(title="🎉 Review Complete!", description="You have completed all your visual flashcards for today. Awesome job maintaining your study streak!", color=0x2ecc71)
            await interaction.message.edit(embed=embed, view=None)

class StartReviewView(View):
    def __init__(self, user, words):
        super().__init__(timeout=None)
        self.user = user
        self.words = words
        
    @discord.ui.button(label="▶️ Start Daily Review", style=discord.ButtonStyle.success)
    async def btn_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id: return
        await interaction.response.defer()
        
        new_view = FlashcardQuizView(self.user, self.words, 0)
        word = self.words[0]
        
        embed = discord.Embed(title=f"📸 Visual Flashcard (1/{len(self.words)})", description="What is the Japanese word for the object shown in this image?", color=0x3498db)
        embed.set_image(url=word['image_url'])
        
        await interaction.message.edit(embed=embed, view=new_view)

@bot.tree.command(name="dropkanji", description="[Admin Only] Manually trigger a Kanji drop for a specific level.")
@app_commands.choices(level=[app_commands.Choice(name=lvl, value=lvl) for lvl in ["N5", "N4", "N3", "N2", "N1"]])
@app_commands.describe(rollback="Set True to go back 1 step (use this to recover a skipped/missed drop).")
async def drop_kanji(interaction: discord.Interaction, level: app_commands.Choice[str], rollback: bool = False):
    await interaction.response.defer(ephemeral=True)
    
    # 1. Check Permissions
    has_permission = any(role.name in ["Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied.", ephemeral=True)
        
    lvl_name = level.value
    
    # 2. Configs for the chosen level
    configs = {
        "N5": (N5_KANJI, 1, "n5-daily-kanji"),
        "N4": (N4_KANJI, 1, "n4-daily-kanji"),
        "N3": (N3_KANJI, 1, "n3-daily-kanji"),
        "N2": (N2_KANJI, 2, "n2-daily-kanji"),
        "N1": (N1_KANJI, 3, "n1-daily-kanji")
    }
    kanji_list, drop_count, ch_keyword = configs[lvl_name]
    
    channel = discord.utils.find(lambda c: ch_keyword in c.name.lower(), interaction.guild.channels)
    if not channel:
        return await interaction.followup.send(f"❌ Channel {ch_keyword} not found.", ephemeral=True)
        
    # 3. Get Current DB State
    data = kanji_db.find_one({"level": lvl_name})
    current_index = data["current_index"] if data else 0
    
    # 🟢 4. ROLLBACK LOGIC (Fixing missed drops)
    if rollback:
        current_index = max(0, current_index - drop_count)
        
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
        
    kanji_str = ", ".join(new_kanjis)
    prompt = f"""You are an expert Japanese Sensei who cleared N1 within 2 years by smart-work and not hard-work. Explain ALL of the following {len(new_kanjis)} Kanji(s): {kanji_str}.
    For EACH Kanji, strictly provide:
    1. Meaning
    2. Onyomi & Kunyomi (with Romaji)
    3. One example of each kanji in a word with Onyomi & Kunyomi pronunciation being used (with romaji).
    Format the entire response beautifully in Discord Markdown.
    CRITICAL RULE: For the Kanji character heading itself, you MUST use a Markdown H1 tag (e.g., `# 妹` or `# 業`) so the Kanji appears massively large on Discord.
    Keep the script entirely in English. Do NOT include memory or pronunciation tricks."""
    
    await interaction.followup.send(f"⏳ Generating manual drop for {lvl_name} (Kanjis: {kanji_str})...", ephemeral=True)
    
    try:
        explanation = await generate_gemini_response(prompt)
        rev_text = f"**🔄 Yesterday's Revision:** {', '.join(revision_kanjis)}\n\n" if revision_kanjis else ""
        
        embed = discord.Embed(
            title=f"㊗️ {lvl_name} Daily Kanji Drop! (Manual Trigger)",
            description=f"{rev_text}{explanation}"[:4096],
            color=0xe74c3c
        )
        embed.set_footer(text=f"Kanji: {kanji_str} | Level: {lvl_name}")
        
        view = PersistentDailyKanjiView()
        msg = await channel.send(embed=embed, view=view)
        
        # Update URLs for Dokkai
        for k in new_kanjis:
            kanji_db.update_one({"kanji_char": k}, {"$set": {"url": msg.jump_url, "level": lvl_name}}, upsert=True)
            
        # Update index safely
        next_index = current_index + len(new_kanjis)
        kanji_db.update_one({"level": lvl_name}, {"$set": {"current_index": next_index}}, upsert=True)
        
        await interaction.channel.send(f"✅ Manual drop for {lvl_name} completed successfully in {channel.mention}!")
        
    except Exception as e:
        await interaction.channel.send(f"❌ Failed to generate manual drop: {e}")


@bot.tree.command(name="updatebot", description="[Admin Only] Auto-update all bot dependencies and safely restart.")
async def update_bot(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    # 1. Admin Check
    has_permission = any(role.name in ["Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied. Only Senior Admins can trigger system updates.", ephemeral=True)
        
    await interaction.followup.send("⏳ **Starting Auto-Update Protocol...**\n1️⃣ Fetching latest packages from Python Package Index...", ephemeral=True)
    
    try:
        packages_to_update = [
            "edge-tts", 
            "discord.py", 
            "pymongo", 
            "aiohttp", 
            "pydub", 
            "SpeechRecognition", 
            "flask",
            "pytz"
        ]
        
        # Run pip upgrade for all packages at once
        command = [sys.executable, "-m", "pip", "install", "--upgrade"] + packages_to_update
        result = subprocess.run(command, capture_output=True, text=True)
        
        if result.returncode == 0:
            terminal_logs = result.stdout[-800:] if len(result.stdout) > 800 else result.stdout
            
            # 🟢 Send success message with the last few lines of the terminal output
            await interaction.followup.send(f"✅ **System Dependencies Checked & Updated!**\n*(Already updated packages were skipped automatically)*\n```bash\n...{terminal_logs}\n```\n🔄 **Bot is now RESTARTING to apply patches. It will be back online in ~5-10 seconds!**", ephemeral=True)
            
            # 3. Restart the Bot Process
            os.execv(sys.executable, ['python'] + sys.argv)
        else:
            terminal_errors = result.stderr[-800:] if len(result.stderr) > 800 else result.stderr
            # 🟢 Error log 
            await interaction.followup.send(f"❌ **Update Failed in Console! Contact *Founder（ファウンダ）* **\n```bash\n...{terminal_errors}\n```", ephemeral=True)
            
    except Exception as e:
        # 🟢 Crash log
        await interaction.followup.send(f"❌ **Critical System Error during update,  Contact *Founder（ファウンダ）*:** {e}", ephemeral=True)

# Command for fixing missed images in vault
@bot.tree.command(name="healvault", description="[Admin Only] Auto-regenerate missing/broken flashcards in the database.")
async def heal_vault(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    # 1. Admin Check
    has_permission = any(role.name in ["Junior Admin（ジュニア・アデュミン）", "Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied. Can only be used by Admins. Contact Admins if you want to fix your vault.", ephemeral=True)
        
    # 2. Find cards with missing or broken URLs
    broken_cards = list(vocab_db.find({
        "$or": [
            {"image_url": {"$exists": False}},
            {"image_url": ""},
            {"image_url": {"$regex": "^attachment://"}} 
        ]
    }))
    
    if not broken_cards:
        return await interaction.followup.send("✅ **Vault is 100% Healthy!** No broken flashcards found.", ephemeral=True)
        
    await interaction.followup.send(f"🛠️ **Found {len(broken_cards)} broken flashcards.** Initiating Smart Auto-Heal & Garbage Collection...", ephemeral=True)
    
    main_channel = discord.utils.get(interaction.guild.channels, name="📸・vocab-vault")
    log_ch = discord.utils.get(interaction.guild.channels, name="admin-logs")
    
    sr_admin = discord.utils.get(interaction.guild.roles, name="Senior Admin（セィニア・アデュミン）")
    founder = discord.utils.get(interaction.guild.roles, name="Founder（ファウンダ）")
    ping_str = f"{founder.mention if founder else ''} {sr_admin.mention if sr_admin else ''}"
    
    healed_count = 0
    deleted_users = set() # To track left users and avoid spamming the log
    
    for doc in broken_cards:
        try:
            uid = doc.get("user_id")
            
            #Skipping alraeady left user, if rejoins server
            if uid in deleted_users:
                continue
                
            user = interaction.guild.get_member(uid)
            
            # 🚨 1. SMART GARBAGE COLLECTION (User has left the server)
            if not user:
                deleted_users.add(uid)
                
                # Fetch Discord API to find username (as user is no more in server)
                try:
                    fetched_user = await interaction.client.fetch_user(uid)
                    username = fetched_user.name
                    user_mention = fetched_user.mention
                except:
                    username = f"Unknown_{uid}"
                    user_mention = f"<@{uid}>"
                
                # A. Purana Thread dhoondh ke delete karo
                if main_channel:
                    thread_name = f"🧵 {username}'s Vault"
                    user_thread = discord.utils.get(main_channel.threads, name=thread_name)
                    if user_thread:
                        try: await user_thread.delete()
                        except: pass
                
                # B. Clearing database of user who has left the server
                vocab_db.delete_many({"user_id": uid})
                
                # C. Reporting in Admin Logs 
                if log_ch:
                    embed = discord.Embed(title="🗑️ Orphaned Vault Deleted", description=f"The user {user_mention} ({username}) has left the server.\n\nDuring the Auto-Heal process, all their **Vocab Vault data and private thread** were permanently deleted instead of being fixed to save DB space.", color=0xe74c3c)
                    await log_ch.send(content=ping_str, embed=embed)
                    
                continue # Aage badho (is word ko fix mat karo)

            # 🛠️ 2. AUTO-HEALING PROCESS (User is still in server)
            dump_channel = interaction.channel 
            thread_name = f"🧵 {user.name}'s Vault"
            
            if main_channel:
                user_thread = discord.utils.get(main_channel.threads, name=thread_name)
                if user_thread:
                    dump_channel = user_thread 
            
            word_eng = str(doc.get("english", "Unknown"))
            
            arr = await generate_flashcard_bytes(word_eng)
            file = discord.File(arr, filename="heal_flashcard.png")
            
            msg = await dump_channel.send(content=f"🛠️ `[HealBot Auto-Recovery]` Fixed missing image for: **{word_eng}**", file=file)
            
            new_url = ""
            if msg.attachments:
                new_url = msg.attachments[0].url
                
            if new_url:
                vocab_db.update_one({"_id": doc["_id"]}, {"$set": {"image_url": new_url}})
                healed_count += 1
                
        except Exception as e:
            print(f"Heal Error for {doc.get('english', 'Unknown')}: {e}")
            
    await interaction.followup.send(f"✅ **Healing & Cleanup Complete!**\n- Successfully recovered: **{healed_count} flashcards**.\n- Orphaned vaults deleted: **{len(deleted_users)} users**.", ephemeral=True)
    
bot.run(os.environ.get("BOT_TOKEN"))
