import os
import re
import asyncio
import time
import random
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ==========================================
# OWNER ONLY
# ==========================================
OWNER_ID = 7675369659

def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or user.id != OWNER_ID:
            try:
                await update.effective_message.reply_text("❌ You are not authorized.")
            except:
                pass
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


# ==========================================
# STORAGE
# ==========================================
FOLDER = "uploads"
os.makedirs(FOLDER, exist_ok=True)

user_files = {}
merge_tasks = {}
status_messages = {}
merge_status_msg = {}
awaiting_filename = {}
file_message_ids = {}


# ==========================================
# CLEANERS
# ==========================================
def remove_headers(text: str) -> str:
    pattern = r"""
        🔑?\s*PREMIUM\s+ACCOUNTS\s+FOR\s+\d+.*?
        Generated:\s*\d{4}-\d{2}-\d{2}.*?
        Total:\s*\d+.*?
        Format:\s*User:Pass\s*Format.*?
        (?:━+|_+|-+)?
    """
    cleaned = re.sub(pattern, "", text, flags=re.MULTILINE | re.DOTALL | re.VERBOSE)
    cleaned = re.sub(r"\n\s*\n", "\n", cleaned)
    return cleaned.strip()


def extract_userpass(text: str) -> list[str]:
    lines = text.splitlines()
    pattern = re.compile(r"^[^:\s]+:[^:\s]+$")
    return [line.strip() for line in lines if pattern.match(line.strip())]


# ==========================================
# START COMMAND
# ==========================================
@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Merge Now", callback_data="merge_now")]])

    await update.message.reply_text(
        "👋 Send .txt files.\nThey will auto-merge after 3 seconds.",
        reply_markup=keyboard
    )


# ==========================================
# RECEIVE FILE
# ==========================================
@owner_only
async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    document = update.message.document

    if not document or not document.file_name.lower().endswith(".txt"):
        await update.message.reply_text("⚠️ Send .txt files only.")
        return

    # Track message id for later deletion
    file_message_ids.setdefault(user_id, []).append(update.message.message_id)

    # Delete file message immediately
    try:
        await context.bot.delete_message(user_id, update.message.message_id)
    except:
        pass

    unique_name = f"{int(time.time()*1000)}_{random.randint(1000,9999)}_{document.file_name}"
    file_path = os.path.join(FOLDER, unique_name)

    tg_file = await document.get_file()
    await tg_file.download_to_drive(custom_path=file_path)

    user_files.setdefault(user_id, []).append(file_path)

    await update_status_message(update, context, user_id)

    # Reset merge timer
    if user_id in merge_tasks:
        merge_tasks[user_id].cancel()

    merge_tasks[user_id] = asyncio.create_task(schedule_merge(update, context, user_id))


# ==========================================
# STATUS UPDATE
# ==========================================
async def update_status_message(update, context, user_id: int):
    files_count = len(user_files.get(user_id, []))
    text = f"📥 Files received: **{files_count}**\n⏳ Waiting 3 seconds to Merge."

    if user_id not in status_messages:
        sent = await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
        status_messages[user_id] = sent.message_id
    else:
        try:
            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=status_messages[user_id],
                text=text,
                parse_mode="Markdown"
            )
        except:
            pass


# ==========================================
# MERGE TIMER
# ==========================================
async def schedule_merge(update, context, user_id):
    try:
        await asyncio.sleep(3)
        await ask_filename(update, context, user_id)
    except asyncio.CancelledError:
        pass


# ==========================================
# ASK FILENAME
# ==========================================
async def ask_filename(update, context, user_id):
    awaiting_filename[user_id] = True
    await context.bot.send_message(
        chat_id=user_id,
        text="📝 What should be the **name of the merged file**?\nExample: `combo.txt`",
        parse_mode="Markdown"
    )


# ==========================================
# HANDLE FILENAME
# ==========================================
@owner_only
async def handle_filename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in awaiting_filename:
        return

    filename = update.message.text.strip()
    if not filename.lower().endswith(".txt"):
        filename += ".txt"

    awaiting_filename[user_id] = False

    await update.message.reply_text(f"📦 Filename set to **{filename}**", parse_mode="Markdown")

    await perform_merge(update, context, user_id, filename)


# ==========================================
# MERGE PROCESS (20MB SAFE)
# ==========================================
@owner_only
async def perform_merge(update, context, user_id: int, filename: str):
    files = user_files.get(user_id, []).copy()
    user_files[user_id] = []

    if not files:
        return

    merged_path = os.path.join(FOLDER, filename)

    # Initial progress message
    msg = await context.bot.send_message(
        chat_id=user_id,
        text=f"🔄 Merging **{len(files)} files**…\n[░░░░░░░░░░░░░░░░░░] 0%",
        parse_mode="Markdown"
    )
    merge_status_msg[user_id] = msg.message_id

    seen = set()

    total_bytes = sum(os.path.getsize(f) for f in files)
    processed_bytes = 0

    def build_bar(p):
        bars = p // 5
        return "[" + "█" * bars + "░" * (20 - bars) + "]"

    async def update_progress():
        percent = int((processed_bytes / total_bytes) * 100)
        bar = build_bar(percent)
        try:
            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=merge_status_msg[user_id],
                text=f"🔄 Processing…\n{bar} {percent}%",
                parse_mode="Markdown"
            )
        except:
            pass

    # Streaming merge
    for filepath in files:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            buffer_lines = []

            for line in f:
                processed_bytes += len(line.encode("utf-8"))
                buffer_lines.append(line)

                # Process every 10k lines
                if len(buffer_lines) >= 10000:
                    text_block = "".join(buffer_lines)
                    cleaned = remove_headers(text_block)
                    for combo in extract_userpass(cleaned):
                        seen.add(combo)
                    buffer_lines = []

                if processed_bytes % 200000 < 500:
                    await update_progress()

            # Process leftovers
            if buffer_lines:
                text_block = "".join(buffer_lines)
                cleaned = remove_headers(text_block)
                for combo in extract_userpass(cleaned):
                    seen.add(combo)

    # Write output
    with open(merged_path, "w", encoding="utf-8") as out:
        out.write("\n".join(seen))

    # Send merged file
    await context.bot.send_document(user_id, open(merged_path, "rb"))

    # Cleanup
    for f in files:
        try: os.remove(f)
        except: pass

    try: os.remove(merged_path)
    except: pass

    # Delete progress
    try:
        await context.bot.delete_message(user_id, merge_status_msg[user_id])
    except:
        pass

    merge_status_msg.pop(user_id, None)
    awaiting_filename.pop(user_id, None)

    # Delete source uploaded messages
    for msg_id in file_message_ids.get(user_id, []):
        try:
            await context.bot.delete_message(user_id, msg_id)
        except:
            pass

    file_message_ids[user_id] = []

    # Delete "files received" status
    if user_id in status_messages:
        try:
            await context.bot.delete_message(user_id, status_messages[user_id])
        except:
            pass
        status_messages.pop(user_id, None)


# ==========================================
# BUTTON HANDLER
# ==========================================
@owner_only
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    await query.answer()

    if query.data == "merge_now":
        await query.edit_message_text("⚡ Manual merge triggered…")
        await ask_filename(update, context, user_id)


# ==========================================
# MAIN
# ==========================================
def main():
    TOKEN = "8321320025:AAFjz9S2PQe-uwC1BUBFVZRwv57XqAzmhfg"

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, receive_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_filename))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🤖 Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
