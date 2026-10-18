import os
import re
import sqlite3
import asyncio
import sys
import zipfile
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from subprocess import Popen, PIPE, run

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = '8894144559:AAEn_EoRioZNnj_mF2KLhY9YK3mhVQ0ZY_Y'
ADMIN_ID = 6784382795

BASE_DIR = Path("hosted_bots")
DB_FILE = Path("manager.db")

BASE_DIR.mkdir(exist_ok=True)
processes = {}


# =========================================================
# DATABASE
# =========================================================

def db():
    return sqlite3.connect(DB_FILE)


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            username TEXT UNIQUE NOT NULL,
            token TEXT NOT NULL,
            filename TEXT NOT NULL,
            folder TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            expires_at TEXT,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.commit()
    con.close()


# =========================================================
# ACCESS & ADMIN CHECK
# =========================================================

def is_admin(user_id):
    return user_id == ADMIN_ID


def has_access(user_id):
    if is_admin(user_id):
        return True
    
    con = db()
    cur = con.cursor()
    cur.execute("SELECT expires_at FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()

    if not row or not row[0]:
        return False
    
    if row[0] == "lifetime":
        return True
    
    try:
        expires = datetime.fromisoformat(row[0])
        if datetime.now() < expires:
            return True
    except Exception:
        pass

    return False


async def check_access(update: Update):
    user = update.effective_user
    if not user:
        return False
    
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
        (user.id, user.username or user.first_name)
    )
    con.commit()
    con.close()

    if not has_access(user.id):
        await update.effective_message.reply_text(
            "❌ Access denied.\n"
            "Contact admin to get access admin's user @nelhumble"
        )
        return False
    return True


# =========================================================
# HELP / START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
        (user.id, user.username or user.first_name)
    )
    con.commit()
    con.close()

    if not has_access(user.id):
        await update.message.reply_text(
            "👋 Welcome!\n\n"
            "❌ You do not have access to use this bot hosting manager.\n"
            "Contact admin to get access admin's user @nelhumble"
        )
        return

    admin_panel_text = "\n/admin - Open Admin Panel" if is_admin(user.id) else ""
    await update.message.reply_text(
        "👑 BOT HOSTING MANAGER\n\n"
        "Commands:\n"
        "/addbot - Add a new bot (.py or .zip)\n"
        "/bots - Show your hosted bots\n"
        f"{admin_panel_text}"
    )


# =========================================================
# ADD BOT (Interactive)
# =========================================================

async def addbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    context.user_data.clear()
    context.user_data["adding"] = True
    context.user_data["step"] = "username"

    await update.message.reply_text(
        "➕ ADD NEW BOT\n\n"
        "Step 1/3\n"
        "Send the bot username.\n\n"
        "Example:\n"
        "@my_test_bot"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not await check_access(update):
        return

    if not context.user_data.get("adding"):
        if context.user_data.get("waiting_custom_days") and is_admin(user.id):
            target_user_id = context.user_data.get("target_user_id")
            try:
                days = int(update.message.text.strip())
                expires = datetime.now() + timedelta(days=days)
                
                con = db()
                cur = con.cursor()
                cur.execute("UPDATE users SET expires_at=? WHERE user_id=?", (expires.isoformat(), target_user_id))
                con.commit()
                con.close()

                context.user_data.clear()
                await update.message.reply_text(f"✅ Successfully granted access for {days} days to user ID `{target_user_id}`.", parse_mode="Markdown")
            except ValueError:
                await update.message.reply_text("❌ Please enter a valid number of days.")
            return
        return

    text = update.message.text.strip()
    step = context.user_data.get("step")

    if step == "username":
        username = text.lstrip("@").lower()
        if not re.fullmatch(r"[a-zA-Z0-9_]{5,64}", username):
            await update.message.reply_text("❌ Invalid username.\nExample: @my_test_bot")
            return

        context.user_data["username"] = username
        context.user_data["step"] = "token"
        await update.message.reply_text("Step 2/3\n\nSend the BotFather token of this bot.")
        return

    if step == "token":
        token = text
        if ":" not in token or len(token) < 20:
            await update.message.reply_text("❌ Invalid Telegram bot token.")
            return

        context.user_data["token"] = token
        context.user_data["step"] = "file"
        await update.message.reply_text("Step 3/3\n\nUpload the bot's MAIN Python file (.py) or ZIP archive (.zip).")
        return


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not await check_access(update):
        return

    if not context.user_data.get("adding") or context.user_data.get("step") != "file":
        return

    document = update.message.document
    file_name = document.file_name.lower()

    if not (file_name.endswith(".py") or file_name.endswith(".zip")):
        await update.message.reply_text("❌ Only .py files or .zip archives are supported.")
        return

    username = context.user_data["username"]
    token = context.user_data["token"]

    folder = BASE_DIR / username
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)

    file_path = folder / document.file_name
    tg_file = await document.get_file()
    await tg_file.download_to_drive(file_path)

    main_py = None
    if file_name.endswith(".zip"):
        try:
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(folder)
            os.remove(file_path)
        except Exception as e:
            await update.message.reply_text(f"❌ Invalid ZIP file: {e}")
            return
        
        py_files = list(folder.glob("*.py"))
        if not py_files:
            await update.message.reply_text("❌ No Python file found inside ZIP.")
            return
        main_py = py_files[0].name
    else:
        main_py = document.file_name

    req_file = folder / "requirements.txt"
    if req_file.exists():
        try:
            run([sys.executable, "-m", "pip", "install", "-r", str(req_file)], check=True, capture_output=True)
        except Exception:
            pass

    try:
        test_bot = Application.builder().token(token).build()
        me = await test_bot.bot.get_me()
        if (me.username or "").lower() != username:
            await update.message.reply_text(f"❌ Username mismatch! Token belongs to @{me.username}")
            return
    except Exception as e:
        await update.message.reply_text(f"❌ Could not verify token: {str(e)[:200]}")
        return

    con = db()
    cur = con.cursor()
    cur.execute("DELETE FROM bots WHERE username=?", (username,))
    cur.execute(
        "INSERT INTO bots (owner_id, username, token, filename, folder, status) VALUES (?, ?, ?, ?, ?, 'active')",
        (user.id, username, token, main_py, str(folder))
    )
    con.commit()
    con.close()

    context.user_data.clear()
    await update.message.reply_text(f"✅ BOT @{username} ADDED SUCCESSFULLY!\nStarting bot...")

    await start_process(username)
    await update.message.reply_text(f"🟢 @{username} started.")


# =========================================================
# BOT MANAGEMENT FUNCTIONS
# =========================================================

def get_bot(username):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT owner_id, username, token, filename, folder, status FROM bots WHERE username=?", (username,))
    row = cur.fetchone()
    con.close()
    return row


async def start_process(username):
    row = get_bot(username)
    if not row:
        return False, "Bot not found."
    owner_id, username, token, filename, folder, status_val = row
    if status_val == 'disabled':
        return False, "Bot is disabled by admin."

    if username in processes:
        proc = processes[username]
        if proc.poll() is None:
            return False, "Already running."

    bot_folder = Path(folder).resolve()
    script = bot_folder / filename
    if not script.exists():
        return False, "Source file missing."

    log_file = bot_folder / "output.log"
    try:
        log = open(log_file, "a", buffering=1, encoding="utf-8")
        proc = Popen(
            [sys.executable, filename],
            cwd=str(bot_folder),
            stdout=log,
            stderr=log,
            stdin=PIPE,
            text=True,
        )
        processes[username] = proc
        return True, "Started."
    except Exception as e:
        return False, str(e)


async def stop_process(username):
    proc = processes.get(username)
    if not proc:
        return False, "Not running."
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        processes.pop(username, None)
        return True, "Stopped."
    except Exception as e:
        return False, str(e)


# =========================================================
# BOTS LIST & INTERACTIVE PANELS
# =========================================================

async def bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not await check_access(update):
        return

    con = db()
    cur = con.cursor()
    if is_admin(user.id):
        cur.execute("SELECT username, filename, status FROM bots ORDER BY id")
    else:
        cur.execute("SELECT username, filename, status FROM bots WHERE owner_id=? ORDER BY id", (user.id,))
    rows = cur.fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("📭 No hosted bots found.")
        return

    keyboard = []
    for username, filename, status_val in rows:
        proc = processes.get(username)
        run_state = "🟢" if (proc and proc.poll() is None) else "🔴"
        keyboard.append([InlineKeyboardButton(f"{run_state} @{username} ({filename})", callback_data=f"manage_{username}")])

    await update.message.reply_text(
        "🤖 YOUR HOSTED BOTS:\nClick on any bot to manage it without typing usernames:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def manage_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user

    data = query.data
    if not data.startswith("manage_"):
        return

    username = data.replace("manage_", "")
    row = get_bot(username)
    if not row:
        await query.edit_message_text("❌ Bot not found.")
        return

    owner_id, _, _, filename, folder, status_val = row
    if not is_admin(user.id) and owner_id != user.id:
        await query.edit_message_text("❌ You do not own this bot.")
        return

    proc = processes.get(username)
    run_state = "🟢 RUNNING" if (proc and proc.poll() is None) else "🔴 STOPPED"

    keyboard = [
        [InlineKeyboardButton("▶️ Start", callback_data=f"cmd_start_{username}"),
         InlineKeyboardButton("⏹ Stop", callback_data=f"cmd_stop_{username}"),
         InlineKeyboardButton("🔄 Restart", callback_data=f"cmd_restart_{username}")],
        [InlineKeyboardButton("📊 Status & Logs", callback_data=f"cmd_logs_{username}"),
         InlineKeyboardButton("📦 Extract Source", callback_data=f"cmd_extract_{username}")],
        [InlineKeyboardButton("🗑 Delete Bot", callback_data=f"cmd_delete_{username}")],
        [InlineKeyboardButton("« Back to List", callback_data="cmd_back_bots")]
    ]

    await query.edit_message_text(
        f"🤖 Bot: @{username}\n"
        f"📄 File: {filename}\n"
        f"⚡ Status: {run_state}\n"
        f"🔒 Mode: {status_val.upper()}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def bot_actions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cmd_back_bots":
        await bots(update, context)
        return

    parts = data.split("_")
    if len(parts) < 3:
        return

    action = parts[1]
    username = "_".join(parts[2:])

    if action == "start":
        ok, msg = await start_process(username)
        await query.answer(msg, show_alert=True)
    elif action == "stop":
        ok, msg = await stop_process(username)
        await query.answer(msg, show_alert=True)
    elif action == "restart":
        await stop_process(username)
        await asyncio.sleep(1)
        ok, msg = await start_process(username)
        await query.answer(msg, show_alert=True)
    elif action == "logs":
        log_file = Path(BASE_DIR) / username / "output.log"
        if not log_file.exists():
            await query.answer("No logs yet.", show_alert=True)
            return
        logs_data = log_file.read_text(errors="replace")
        if len(logs_data) > 3000:
            logs_data = logs_data[-3000:]
        await query.message.reply_text(f"📜 Logs for @{username}:\n```text\n{logs_data}\n```", parse_mode="Markdown")
        return
    elif action == "extract":
        row = get_bot(username)
        if row:
            source = Path(row[4]) / row[3]
            if source.exists():
                await query.message.reply_document(document=source, caption=f"📦 Source of @{username}")
        return
    elif action == "delete":
        await stop_process(username)
        row = get_bot(username)
        if row:
            shutil.rmtree(row[4], ignore_errors=True)
        con = db()
        cur = con.cursor()
        cur.execute("DELETE FROM bots WHERE username=?", (username,))
        con.commit()
        con.close()
        await query.edit_message_text(f"🗑️ @{username} deleted successfully.")
        return

    row = get_bot(username)
    if row:
        proc = processes.get(username)
        run_state = "🟢 RUNNING" if (proc and proc.poll() is None) else "🔴 STOPPED"
        keyboard = [
            [InlineKeyboardButton("▶️ Start", callback_data=f"cmd_start_{username}"),
             InlineKeyboardButton("⏹ Stop", callback_data=f"cmd_stop_{username}"),
             InlineKeyboardButton("🔄 Restart", callback_data=f"cmd_restart_{username}")],
            [InlineKeyboardButton("📊 Logs", callback_data=f"cmd_logs_{username}"),
             InlineKeyboardButton("📦 Extract", callback_data=f"cmd_extract_{username}")],
            [InlineKeyboardButton("🗑 Delete", callback_data=f"cmd_delete_{username}")],
            [InlineKeyboardButton("« Back to List", callback_data="cmd_back_bots")]
        ]
        await query.edit_message_text(f"🤖 Bot: @{username}\n⚡ Status: {run_state}", reply_markup=InlineKeyboardMarkup(keyboard))


# =========================================================
# ADMIN PANEL (`/admin`)
# =========================================================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ You are not authorized.")
        return

    keyboard = [
        [InlineKeyboardButton("🔥 All Hosted Bots", callback_data="adm_all_bots")],
        [InlineKeyboardButton("👽 Started Users", callback_data="adm_started_users")]
    ]
    await update.message.reply_text("👑 ADMIN PANEL\nSelect an option below:", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not user or not is_admin(user.id):
        return

    data = query.data

    if data == "adm_all_bots":
        con = db()
        cur = con.cursor()
        cur.execute("SELECT username, owner_id, filename, status FROM bots")
        rows = cur.fetchall()
        con.close()

        if not rows:
            await query.edit_message_text("📭 No hosted bots.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="adm_back")]]))
            return

        keyboard = []
        for username, owner_id, filename, status_val in rows:
            keyboard.append([InlineKeyboardButton(f"@{username} (Owner: {owner_id}) [{status_val}]", callback_data=f"adm_bot_{username}")])
        keyboard.append([InlineKeyboardButton("« Back", callback_data="adm_back")])

        await query.edit_message_text("🔥 ALL HOSTED BOTS:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("adm_bot_"):
        username = data.replace("adm_bot_", "")
        row = get_bot(username)
        if not row:
            await query.edit_message_text("❌ Bot not found.")
            return
        owner_id, _, _, filename, folder, status_val = row
        
        con = db()
        cur = con.cursor()
        cur.execute("SELECT username FROM users WHERE user_id=?", (owner_id,))
        u_row = cur.fetchone()
        con.close()
        owner_name = f"@{u_row[0]}" if u_row and u_row[0] else str(owner_id)

        toggle_text = "🟢 Enable Bot" if status_val == 'disabled' else "🔴 Disable Bot"
        toggle_action = "enable" if status_val == 'disabled' else "disable"

        keyboard = [
            [InlineKeyboardButton(toggle_text, callback_data=f"adm_toggle_{toggle_action}_{username}")],
            [InlineKeyboardButton("« Back to Bots", callback_data="adm_all_bots")]
        ]
        await query.edit_message_text(
            f"🤖 Bot: @{username}\n"
            f"👤 Hosted By: {owner_name} (ID: {owner_id})\n"
            f"📄 File: {filename}\n"
            f"⚡ Status: {status_val.upper()}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("adm_toggle_"):
        parts = data.split("_")
        action = parts[2]
        username = "_".join(parts[3:])
        new_status = 'active' if action == 'enable' else 'disabled'

        con = db()
    cur = con.cursor()
        cur.execute("SELECT user_id, username, expires_at FROM users")
        rows = cur.fetchall()
        con.close()

        if not rows:
            await query.edit_message_text("📭 No users found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="adm_back")]]))
            return

        keyboard = []
        for user_id, uname, exp in rows:
            disp_name = f"@{uname}" if uname else str(user_id)
            keyboard.append([InlineKeyboardButton(f"👤 {disp_name} (ID: {user_id})", callback_data=f"adm_usr_{user_id}")])
        keyboard.append([InlineKeyboardButton("« Back", callback_data="adm_back")])

        await query.edit_message_text("👽 STARTED USERS:\nClick on any user to manage their access duration:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("adm_usr_"):
        target_user_id = int(data.replace("adm_usr_", ""))
        keyboard = [
            [InlineKeyboardButton("1 Day", callback_data=f"settime_{target_user_id}_1"),
             InlineKeyboardButton("7 Days", callback_data=f"settime_{target_user_id}_7")],
            [InlineKeyboardButton("30 Days", callback_data=f"settime_{target_user_id}_30"),
             InlineKeyboardButton("Lifetime", callback_data=f"settime_{target_user_id}_lifetime")],
            [InlineKeyboardButton("Custom Days ⌨️", callback_data=f"settime_{target_user_id}_custom")],
            [InlineKeyboardButton("« Back to Users", callback_data="adm_started_users")]
        ]
        await query.edit_message_text(f"⏳ Select access duration for User ID `{target_user_id}`:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("settime_"):
        parts = data.split("_")
        target_user_id = int(parts[1])
        duration = parts[2]

        if duration == "custom":
            context.user_data["waiting_custom_days"] = True
            context.user_data["target_user_id"] = target_user_id
            await query.message.reply_text(f"⌨️ Send the number of days for user ID `{target_user_id}` as a message:", parse_mode="Markdown")
            return

        if duration == "lifetime":
            expires_val = "lifetime"
        else:
            days = int(duration)
            expires_val = (datetime.now() + timedelta(days=days)).isoformat()

        con = db()
        cur = con.cursor()
        cur.execute("UPDATE users SET expires_at=? WHERE user_id=?", (expires_val, target_user_id))
        con.commit()
        con.close()

        await query.edit_message_text(f"✅ Successfully updated access for user ID `{target_user_id}` to `{duration}`.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="adm_started_users")]]))

    elif data == "adm_back":
        keyboard = [
            [InlineKeyboardButton("🔥 All Hosted Bots", callback_data="adm_all_bots")],
            [InlineKeyboardButton("👽 Started Users", callback_data="adm_started_users")]
        ]
        await query.edit_message_text("👑 ADMIN PANEL\nSelect an option below:", reply_markup=InlineKeyboardMarkup(keyboard))


# =========================================================
# MONITOR
# =========================================================

async def monitor_bots(app):
    while True:
        await asyncio.sleep(10)
        con = db()
        cur = con.cursor()
        cur.execute("SELECT username, status FROM bots")
        rows = cur.fetchall()
        con.close()

        for username, status_val in rows:
            if status_val == 'disabled':
                continue
            proc = processes.get(username)
            if proc and proc.poll() is not None:
                processes.pop(username, None)
                await asyncio.sleep(2)
                await start_process(username)


# =========================================================
# MAIN
# =========================================================

def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set.")
        return

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addbot", addbot))
    app.add_handler(CommandHandler("bots", bots))
    app.add_handler(CommandHandler("admin", admin_panel))

    app.add_handler(CallbackQueryHandler(manage_bot_callback, pattern="^manage_"))
    app.add_handler(CallbackQueryHandler(bot_actions_callback, pattern="^cmd_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(adm_|settime_)"))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def post_init(application):
        application.create_task(monitor_bots(application))

    app.post_init = post_init

    print("================================")
    print(" BOT HOSTING MANAGER RUNNING")
    print("================================")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
