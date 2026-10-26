import asyncio
import logging
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import google.generativeai as genai

# ---------------------------------------------------------
# НАЛАШТУВАННЯ КЛЮЧІВ (ВСТАВ СВОЇ)
# ---------------------------------------------------------
BOT_TOKEN = "8924606893:AAG0w63ec6uWZim_C879K0yDjbvgCSgZlyM"
GEMINI_API_KEY = "AIzaSyD8SN3zjHZtoUL9HZlLpPb2fKYtRQZuD2A"

# Налаштування Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Системний промпт для жорсткого мотиватора
SYSTEM_INSTRUCTION = """
Ти — токсичний, брутальний асистент-планувальник. 
Твій стиль: спілкуєшся на рівних, використовуєш мат, не лижеш дупи, не вживаєш солодких компліментів і не дипломатничаєш. 
Якщо користувач пише тобі завдання, коротко підтверди це у своєму жорсткому стилі з матами.
Якщо користувач підбиває підсумки дня або прокрастинує — нагадай йому, де він тупить і чому треба їбашити.
Відповідай чітко, коротко, без довгої хуйні.
"""

# Використовуємо стабільну модель Gemini
generation_config = {
    "temperature": 0.8,
    "max_output_tokens": 400,
}
model = genai.GenerativeModel(
    model_name="gemini-1.5-pro"
    generation_config=generation_config,
    system_instruction=SYSTEM_INSTRUCTION
)

# ---------------------------------------------------------
# БАЗА ДАНИХ (SQLite)
# ---------------------------------------------------------
def init_db():
    conn = sqlite3.connect("planner.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            task_text TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()

def add_user(user_id: int):
    conn = sqlite3.connect("planner.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def add_task(user_id: int, task_text: str):
    conn = sqlite3.connect("planner.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tasks (user_id, task_text) VALUES (?, ?)", (user_id, task_text))
    conn.commit()
    conn.close()

def get_pending_tasks(user_id: int):
    conn = sqlite3.connect("planner.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, task_text FROM tasks WHERE user_id = ? AND status = 'pending'", (user_id,))
    tasks = cursor.fetchall()
    conn.close()
    return tasks

def mark_task_done(task_id: int):
    conn = sqlite3.connect("planner.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

def clear_user_tasks(user_id: int):
    conn = sqlite3.connect("planner.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect("planner.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return users

# ---------------------------------------------------------
# ЛОГІКА LLM (Gemini)
# ---------------------------------------------------------
def ask_gemini(user_prompt: str) -> str:
    try:
        response = model.generate_content(user_prompt)
        return response.text
    except Exception as e:
        return f"Сука, щось здохло в API Gemini: {e}"

# ---------------------------------------------------------
# BOT & DISPATCHER
# ---------------------------------------------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    add_user(message.from_user.id)
    text = ask_gemini("Користувач запустив бота. Привітай його жорстко, скажи що тепер ти його особистий контролер і що прокрастинація тепер не пройде.")
    await message.answer(text)

@dp.message(Command("tasks"))
async def cmd_tasks(message: types.Message):
    tasks = get_pending_tasks(message.from_user.id)
    if not tasks:
        await message.answer("У тебе нема ніхя активних задач. Спиш чи хуйньою маєшся?")
        return
    
    reply = "📌 **Твої невиконані таски, блять:**\n\n"
    keyboard = []
    for task_id, task_text in tasks:
        reply += f"🔹 `{task_text}`\n"
        keyboard.append([types.InlineKeyboardButton(text=f"❌ Зроблено: {task_text[:20]}...", callback_data=f"done_{task_id}")])
    
    markup = types.InlineKeyboardMarkup(inline_keyboard=keyboard)
    await message.answer(reply, parse_mode="Markdown", reply_markup=markup)

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    clear_user_tasks(message.from_user.id)
    await message.answer("Почистив твій список задач під нуль. Тепер не відмажешся, закидай нові.")

@dp.callback_query(F.data.startswith("done_"))
async def process_task_done(callback: types.CallbackQuery):
    task_id = int(callback.data.split("_")[1])
    mark_task_done(task_id)
    await callback.answer("Зараховано!")
    await callback.message.edit_text("Красава, мінус одна хуйня зі списку. Їбаш далі!")

@dp.message(F.text)
async def handle_text(message: types.Message):
    user_id = message.from_user.id
    user_text = message.text
    add_task(user_id, user_text)
    
    prompt = f"Користувач додав завдання: '{user_text}'. Підтверди це жорстко, скористайся матом і скажи йому це зробити вчасно."
    reply = ask_gemini(prompt)
    await message.answer(reply)

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    user_id = message.from_user.id
    # Оскільки у Gemini поки немає вбудованого простого стт аудіо в цій лібі без додаткових танців, просто приймемо голосовуху текстом-заглушкою або скажемо кидати текстом
    await message.answer("Брат, кидай краще текстом, бо аудіо тут поки не чіпаємо, щоб знову нічого не здохло. Пиши задачу сюди текстом!")

async def morning_briefing():
    users = get_all_users()
    for user_id in users:
        tasks = get_pending_tasks(user_id)
        if tasks:
            task_list = "\n".join([f"- {t[1]}" for t in tasks])
            prompt = f"Сьогодні ранок. У користувача є завдання:\n{task_list}\nДай йому брутальний ранковий підранок, щоб він не провтикав день."
        else:
            prompt = "Сьогодні ранок. У користувача взагалі немає завдань. Напхай йому з матами, щоб він склав план і не займався хуйньою."
        
        msg = ask_gemini(prompt)
        try:
            await bot.send_message(user_id, f"☀️ **РАНКОВИЙ РОЗГОН**\n\n{msg}", parse_mode="Markdown")
        except:
            pass

async def evening_review():
    users = get_all_users()
    for user_id in users:
        tasks = get_pending_tasks(user_id)
        if tasks:
            task_list = "\n".join([f"- {t[1]}" for t in tasks])
            prompt = f"Вже вечір. Користувач НЕ ВИКОНАВ оці завдання:\n{task_list}\nЗроби йому жорсткий вечірній рознос, спитай де він провтикав свій день."
        else:
            prompt = "Вже вечір. У користувача немає невиконаних тасок. Похвали його у своєму жорсткому брутальному стилі (bez соплів)."
        
        msg = ask_gemini(prompt)
        try:
            await bot.send_message(user_id, f"🌙 **ВЕЧІРНІЙ ЄБОШ-РОЗБІР**\n\n{msg}", parse_mode="Markdown")
        except:
            pass

from aiohttp import web
import os

async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    
    # Піднімаємо фоновий вебсервер для Render
    await web_server()
    
    scheduler.add_job(morning_briefing, 'cron', hour=8, minute=30)
    scheduler.add_job(evening_review, 'cron', hour=21, minute=0)
    scheduler.start()
    
    print("⚡️ Бот на Gemini запущен і готовий єбашити!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
