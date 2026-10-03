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

# --- 🧠 DIRECT GEMINI REST API ---
async def generate_gemini_response(prompt):
    global CURRENT_KEY_INDEX
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
                    
                    # 🟢 Usage Tracking to MongoDB (PST Midnight Reset Friendly)
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
    half_limit = force_limit // 2
    
    # 50% current level kanji, 50% lower levels (agar lower level exist karti hai)
    if kanjis_lower:
        selected_kanjis = random.sample(kanjis_current, min(half_limit, len(kanjis_current)))
        selected_kanjis += random.sample(kanjis_lower, min(force_limit - len(selected_kanjis), len(kanjis_lower)))
    else:
        # N5 walo ke liye sirf unka level hi aayega
        selected_kanjis = random.sample(kanjis_current, min(force_limit, len(kanjis_current)))
        
    random.shuffle(selected_kanjis)
    kanji_str = ", ".join(selected_kanjis)

    prompt = f"""You are an expert JLPT Examiner. Generate exactly {force_limit} multiple-choice Kanji Reading questions for JLPT {level_short}.
    CRITICAL RULES:
    1. You MUST use ONLY words formed from these specific Kanjis: {kanji_str}.
    2. The 'question' MUST be visually large using markdown. Format it exactly like this: "What is the correct reading for this word?\\n# [Insert Kanji Word Here]". Do NOT use bold markdown (**) around the Kanji. Do not add any furigana in the question.
    3. The 4 options (A, B, C, D) MUST be strictly in 100% Hiragana. Do not use Romaji or English.
    4. Provide a very brief English 'explanation' of the meaning of the Kanji word.
    5. CRITICAL JSON RULE: Use strictly double quotes (") for all keys and string values. Output ONLY a valid JSON array of objects.

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
        
    # 🟢 STRICT PYTHON SHUFFLING LOGIC (Breaks the Option 'A' always correct Pattern)
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
    6. CRITICAL JSON RULE: Use strictly double quotes (") for all keys and string values. Do not use single quotes. Do not add trailing commas.
    Output ONLY a valid JSON array of objects."""
    
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
                
            quiz_db.update_one(
                {"_id": self.user.id, "level": self.level}, 
                {"$inc": {"score": self.score, "time_taken": time_taken}}, 
                upsert=True
            )
            
            embed = discord.Embed(title="🏁 Quiz Completed!", description=f"Score: **{self.score}/{self.total_q}** in {time_taken}s.", color=discord.Color.green())
            await interaction.response.edit_message(embed=embed, view=None)
            
            # 🟢 NAYA LOGIC: Detailed Ephemeral Explanation
            has_explanations = any("explanation" in q for q in self.questions)
            if has_explanations:
                exp_embed = discord.Embed(title="📖 Listening Review & Explanations", color=0x3498db)
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

# --- 🎧 GLOBAL QUEUE FOR LISTENING ---
listening_queues = {} 
queue_processing = {} 

async def process_listening_queue(guild, client):
    queue_processing[guild.id] = True
    while listening_queues.get(guild.id, []):
        task = listening_queues[guild.id][0] 
        user = task["user"]
        vc = task["vc"]
        level_full = task["level"]
        channel = task["channel"]
        
        try:
            await channel.send(f"🎧 {user.mention}, it's your turn! Joining **{vc.name}** now...", delete_after=15)
            
            voice_client = discord.utils.get(client.voice_clients, guild=guild)
            if voice_client and voice_client.is_connected():
                await voice_client.move_to(vc)
            else:
                voice_client = await vc.connect()
                
            level_short = level_full.split(" ")[1]
            has_pro = any(r.name == "金 Pro Learners 金" for r in user.roles)
            q_count = 5 if has_pro else 3 
            
            prompt = f"""You are an expert JLPT Examiner. Generate a short Japanese listening script for JLPT {level_short} level.
            Then generate {q_count} multiple-choice questions based ONLY on that script.
            
            CRITICAL RULES:
            1. NARRATOR INTRO: The script MUST start with a narrator providing context (e.g., "男の人と女の人が話しています。" or "田中さんと佐藤さんが話しています。"). This context will help understand the listener that converstaion is between which 2 people. 
            2. CLEAR ROLES: Use clear names or roles in the script and the questions so the listener knows exactly who is speaking. Also use the similar names or roles given throughout conversation, just before narrator's respective dialouge.
            3. NATURAL: The script should be natural conversational Japanese (max 350 chars). As the script will be used for practising for JLPT listening exam so it should be relevant for {level_short} level and the story/script should be randomized always meaning no same script should be repeated more than once.
            4. FURIGANA MANDATORY: In ALL multiple-choice questions and options, you MUST provide furigana in square brackets exactly after EVERY Kanji used (e.g., 毎日[まいにち]).
            5. JSON FORMAT: Use strictly double quotes (") for all keys. Do not add trailing commas.
            
            Output ONLY a valid JSON object matching this exact structure:
            {{
                "script": "Narrator intro... followed by dialogue...",
                "questions": [
                    {{"question": "...", "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}}, "answer": "A", "explanation": "Brief English explanation of why this answer is correct and others are wrong based on the story."}}
                ]
            }}"""
            
            raw_text = await generate_gemini_response(prompt)
            data = extract_json(raw_text)
            if isinstance(data, list): data = data[0]
            
            script = data.get("script", "")
            questions_data = data.get("questions", [])
            
            if not script or not questions_data:
                raise ValueError("AI failed to generate script or questions.")
                
            # Speed Control Logic (Slow for N5/N4)
            is_slow = True if level_short in ["N5", "N4"] else False
            
            from gtts import gTTS
            tts = gTTS(text=script, lang='ja', slow=is_slow)
            tts.save(f"listening_{guild.id}.mp3")
            
            voice_client.play(discord.FFmpegPCMAudio(f"listening_{guild.id}.mp3", executable="ffmpeg"))
            
            while voice_client.is_playing():
                await asyncio.sleep(1)
                
            await voice_client.disconnect()
            
            q = questions_data[0]
            embed = discord.Embed(title=f"🎧 {level_full} Listening Quiz (1/{len(questions_data)})", description=f"**{q['question']}**\n\n🇦 {q['options']['A']}\n🇧 {q['options']['B']}\n🇨 {q['options']['C']}\n🇩 {q['options']['D']}", color=0x9b59b6)
            
            view = QuizView(user, questions_data, level_full, time.time())
            msg = await channel.send(content=f"{user.mention}, here is your listening quiz!", embed=embed, view=view)
            view.message = msg
            
        except Exception as e:
            await channel.send(f"❌ Listening Error for {user.mention}: {e}")
            if 'voice_client' in locals() and voice_client and voice_client.is_connected():
                await voice_client.disconnect()
                
        # Remove completed task from queue
        if listening_queues.get(guild.id):
            listening_queues[guild.id].pop(0)
            
    queue_processing[guild.id] = False

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
            
        vc = interaction.user.voice.channel
        guild_id = interaction.guild.id
        
        if guild_id not in listening_queues:
            listening_queues[guild_id] = []
            
        # Prevent same user from spamming queue
        for task in listening_queues[guild_id]:
            if task["user"].id == interaction.user.id:
                return await interaction.response.send_message("⚠️ You are already in the listening queue! Please wait for your turn.", ephemeral=True)
                
        # Add to the global queue
        listening_queues[guild_id].append({
            "user": interaction.user,
            "vc": vc,
            "level": self.level_full,
            "channel": interaction.channel
        })
        
        queue_position = len(listening_queues[guild_id])
        
        if queue_position == 1 and not queue_processing.get(guild_id, False):
            # Immediate start
            await interaction.response.edit_message(content=f"⏳ Joining **{vc.name}** and preparing your listening practice... Please wait.", embed=None, view=None)
            asyncio.create_task(process_listening_queue(interaction.guild, interaction.client))
        else:
            # Show queue embed if someone else is already listening
            queue_text = f"⏳ **Listening Queue for {interaction.guild.name}:**\n"
            for idx, task in enumerate(listening_queues[guild_id]):
                queue_text += f"{idx + 1}. {task['user'].mention} in queue\n"
                
            queue_text += f"\n{interaction.user.mention}, let me concentrate on the current users. I'll come to you when it's your turn!"
            
            embed = discord.Embed(title="🎧 Listening Queue", description=queue_text, color=0x3498db)
            await interaction.response.edit_message(content="", embed=embed, view=None)
            
            if not queue_processing.get(guild_id, False):
                asyncio.create_task(process_listening_queue(interaction.guild, interaction.client))

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
        topics = ["Japanese Culture", "A Sci-Fi Adventure", "Daily Mysteries & Unspoken Rules", "A Slice of Life moment", "A Mystery", "Folklore", "School Life", "Japanese Food & Café Culture", "Modern Habits & Tech"]
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
                    
                # 🟢 NAYA LOGIC: Check if level needs Romaji (Only N5 & N4)
                needs_romaji = lvl_name in ["N5", "N4"]
                
                prompt = f"""You are an expert Japanese linguist and JLPT examiner.
                Task: Generate a short narrative (max 300 Japanese characters) about '{topic}'.
                Constraint 1: Strictly use ONLY vocabulary and grammar points from JLPT levels N5 up to {lvl_name}.
                Constraint 2: Do not use complex Kanji outside of the specified JLPT level unless furigana is provided in parenthesis."""
                
                # 🟢 PROMPT CONDITION: Romaji sirf tab maangega jab level N5/N4 ho
                if needs_romaji:
                    prompt += """\nOutput Format: Return ONLY valid JSON with exactly three keys: "title", "story_content", and "romaji" (the exact romaji pronunciation of the story text). Do not add trailing commas."""
                else:
                    prompt += """\nOutput Format: Return ONLY valid JSON with exactly two keys: "title" and "story_content". Do not add trailing commas."""
                
                try:
                    raw_text = await generate_gemini_response(prompt)
                    story_data = extract_json(raw_text)
                    
                    if isinstance(story_data, list):
                        story_data = story_data[0]

                    title = story_data.get("title", f"{lvl_name} Daily Reading")
                    content = story_data.get("story_content", "Could not generate story.")
                    romaji = story_data.get("romaji", "")

                    # 🟢 APPEND ROMAJI SPOILER: Story ke theek niche tumhare format mein
                    if needs_romaji and romaji:
                        content += f"\n\n**Romaji translation (click to reveal):**\n||{romaji}||"

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
        
        # 🟢 Transparent & Full-Width Aesthetic (RGBA)
        width, height = 900, 300
        # 100% Transparent Background - Discord ke native background ke sath blend hoga
        img = Image.new('RGBA', (width, height), color=(0, 0, 0, 0)) 
        draw = ImageDraw.Draw(img)
        
        # 🟢 Load Fonts (Pixel font for heading, Default for text)
        try:
            # Jo font humne abhi download kiya
            pixel_font = ImageFont.truetype("pixel.ttf", 28)
            # Default font size bada kiya
            main_font = ImageFont.load_default()
        except:
            pixel_font = ImageFont.load_default()
            main_font = ImageFont.load_default()
        
        # 🟢 Drawing the Pixel Heading
        # Thoda drop-shadow effect ke liye pehle black mein draw karenge
        draw.text((42, 22), "REALTIME A.I. TOKENS MONITOR", font=pixel_font, fill=(0, 0, 0, 200))
        # Phir asli color upar draw karenge
        draw.text((40, 20), "REALTIME A.I. TOKENS MONITOR", font=pixel_font, fill=(255, 255, 255, 255))
        
        # 🟢 Sub-Text (Larger & Spaced out)
        # Using default font but writing it bigger by scaling or just simple text for now
        draw.text((40, 80), f"⚡ PST Date: {today_pst}   |   Auto-refreshes every hour", fill=(148, 163, 184, 255))
        draw.text((40, 120), f"Active API Keys: {len(API_KEYS)}", fill=(52, 211, 153, 255))
        draw.text((40, 150), f"Daily Requests: {req_count} / {max_req}", fill=(251, 146, 60, 255))
        draw.text((40, 180), f"Tokens Processed: {token_count:,}", fill=(96, 165, 250, 255))
        
        # 🟢 Transparent Status Bar (Full Width & Thicker)
        bar_x, bar_y, bar_w, bar_h = 40, 220, 820, 35
        # Semi-transparent track (Background bar)
        draw.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], fill=(255, 255, 255, 30))
        
        fill_ratio = min(req_count / max_req, 1.0) if max_req > 0 else 0
        fill_w = int(fill_ratio * bar_w)
        
        if fill_w > 0:
            # Dynamic Glowing Bar
            bar_color = (16, 185, 129, 220) if fill_ratio < 0.75 else (245, 158, 11, 220) if fill_ratio < 0.9 else (239, 68, 68, 220)
            draw.rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h], fill=bar_color)
            
        pct_text = f"{int(fill_ratio * 100)}%"
        # Percentage text
        draw.text((bar_x + bar_w - 60, bar_y + 10), pct_text, fill=(255, 255, 255, 255))
        
        arr = io.BytesIO()
        img.save(arr, format='PNG')
        arr.seek(0)
        file = discord.File(arr, filename="tracker.png")
        
        try:
            await tracker_channel.purge(limit=3)
            # 🟢 THE FIX: Embed hata diya, ab sirf direct transparent image jayegi!
            await tracker_channel.send(file=file)
        except Exception as e:
            print(f"Tracker Drop Error: {e}")

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
    has_permission = any(role.name in ["Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied: You need Senior Admin or Founder role.", ephemeral=True)
        
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

@bot.tree.command(name="dropdokkai", description="[Admin] Manually trigger Missed Dokkai Drop.")
async def manual_dokkai(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    # 🟢 Sirf Admins/Founders chala payenge
    has_permission = any(role.name in ["Senior Admin（セィニア・アデュミン）", "Founder（ファウンダ）"] for role in interaction.user.roles)
    if not has_permission:
        return await interaction.followup.send("❌ Access Denied. Only Admins can trigger this.", ephemeral=True)
        
    await interaction.followup.send("⏳ Dropping today's Dokkai manually across all channels...", ephemeral=True)
    
    # Calling the exact same background function we use for the daily drop
    await bot.drop_freemium_dokkai_task()
    
    await interaction.followup.send("✅ Dokkai Drop successfully completed!", ephemeral=True)

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


bot.run(os.environ.get("BOT_TOKEN"))
