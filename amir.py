#!/usr/bin/env python3
import os
import hmac
import hashlib
import sqlite3
import logging
import re
from datetime import datetime
from typing import Optional
from threading import Thread

from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
import pandas as pd

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
HMAC_SECRET = os.environ.get("HMAC_SECRET", "replace-with-a-secret")
DB_PATH = os.environ.get("DB_PATH", "scores.db")
ADMINS = set(int(x) for x in os.environ.get("ADMINS", "").split(",") if x.strip().isdigit())
PORT = int(os.environ.get("PORT", 5000))
# ----------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
NATIONAL_CODE_RE = re.compile(r"^\d{10}$")
SPLIT_RE = re.compile(r"[|:\s]+")

# ---------- Helper functions ----------

def persian_to_english_number(text: str) -> str:
    persian_numbers = '۰۱۲۳۴۵۶۷۸۹'
    english_numbers = '0123456789'
    return text.translate(str.maketrans(persian_numbers, english_numbers))

def hmac_code(code: str) -> str:
    return hmac.new(HMAC_SECRET.encode('utf-8'), code.encode('utf-8'), hashlib.sha256).hexdigest()

def valid_iranian_national_code(code: str) -> bool:
    if not NATIONAL_CODE_RE.match(code) or len(set(code)) == 1:
        return False
    digits = list(map(int, code))
    s = sum(digits[i] * (10 - i) for i in range(9))
    r = s % 11
    check = digits[9]
    return check == r if r < 2 else check == 11 - r

# ---------- Database ----------

def get_conn():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_hmac TEXT NOT NULL,
            subject TEXT NOT NULL,
            score REAL NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(code_hmac, subject)
        )
    """)
    conn.close()

def add_or_update_score(code: str, subject: str, score: float):
    code_h = hmac_code(code)
    subj = subject.strip().lower()
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT INTO scores (code_hmac, subject, score, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(code_hmac, subject) DO UPDATE SET score=excluded.score, updated_at=excluded.updated_at
    """, (code_h, subj, score, now))
    conn.close()

def remove_score(code: str, subject: str) -> bool:
    code_h = hmac_code(code)
    subj = subject.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM scores WHERE code_hmac=? AND subject=?", (code_h, subj))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed > 0

def remove_all_scores(code: str) -> int:
    code_h = hmac_code(code)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM scores WHERE code_hmac=?", (code_h,))
    removed = cur.rowcount
    conn.commit()
    conn.close()
    return removed

def lookup_scores(code: str):
    code_h = hmac_code(code)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT subject, score, updated_at FROM scores WHERE code_hmac=? ORDER BY subject", (code_h,))
    rows = cur.fetchall()
    conn.close()
    return rows

def lookup_subject(code: str, subject: str) -> Optional[tuple]:
    code_h = hmac_code(code)
    subj = subject.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT subject, score, updated_at FROM scores WHERE code_hmac=? AND subject=?", (code_h, subj))
    row = cur.fetchone()
    conn.close()
    return row

# ---------- Telegram Handlers ----------

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! کافیه کد ملی ۱۰ رقمی خودت رو ارسال کنی.\n"
        "برای یک درس خاص: <کدملی> <درس>\n"
        "ادمین‌ها: /add, /edit, /remove, /remove_all, /list_codes, /add_excel"
    )

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز هستند.")
        return
    text = update.message.text.partition(' ')[2].strip()
    parts = [p.strip() for p in SPLIT_RE.split(text) if p.strip()]
    if len(parts) < 3:
        await update.message.reply_text("فرمت: /add <کدملی> <درس> <نمره>")
        return
    code, *middle, score_s = parts
    code = persian_to_english_number(code)
    score_s = persian_to_english_number(score_s)
    subject = ' '.join(middle)
    if not valid_iranian_national_code(code):
        await update.message.reply_text("کد ملی نامعتبر است.")
        return
    try:
        score = float(score_s)
    except ValueError:
        await update.message.reply_text("نمره باید عدد باشد.")
        return
    add_or_update_score(code, subject, score)
    await update.message.reply_text(f"{subject} برای {code} ثبت شد.")

async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_cmd(update, context)

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز هستند.")
        return
    text = update.message.text.partition(' ')[2].strip()
    parts = [p.strip() for p in SPLIT_RE.split(text) if p.strip()]
    if len(parts) < 2:
        await update.message.reply_text("فرمت: /remove <کدملی> <درس>")
        return
    code, *subject_parts = parts
    code = persian_to_english_number(code)
    subject = ' '.join(subject_parts)
    ok = remove_score(code, subject)
    await update.message.reply_text("حذف شد." if ok else "چنین نمره‌ای پیدا نشد.")

async def remove_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز هستند.")
        return
    code = persian_to_english_number(update.message.text.partition(' ')[2].strip())
    removed = remove_all_scores(code)
    await update.message.reply_text(f"{removed} ردیف حذف شد.")

async def list_codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز هستند.")
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT code_hmac FROM scores LIMIT 200")
    rows = cur.fetchall()
    conn.close()
    text = "کدهای هش‌شده:\n" + "\n".join(r[0] for r in rows)
    await update.message.reply_text(text)

# ---------- Excel bulk add ----------
async def add_excel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز هستند.")
        return
    if not update.message.document:
        await update.message.reply_text("لطفاً فایل Excel بفرستید (.xlsx یا .csv)")
        return
    file = await context.bot.get_file(update.message.document.file_id)
    file_path = f"/tmp/{update.message.document.file_name}"
    await file.download_to_drive(file_path)
    try:
        if file_path.endswith(".csv"):
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)
        for idx, row in df.iterrows():
            code = persian_to_english_number(str(row[0]))
            subject = str(row[1])
            score = float(persian_to_english_number(str(row[2])))
            if valid_iranian_national_code(code):
                add_or_update_score(code, subject, score)
        await update.message.reply_text("نمرات با موفقیت اضافه شد.")
    except Exception as e:
        await update.message.reply_text(f"خطا در پردازش فایل: {e}")

# ---------- Messages ----------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    parts = [p.strip() for p in SPLIT_RE.split(text) if p.strip()]
    code = persian_to_english_number(parts[0])
    if not valid_iranian_national_code(code):
        await update.message.reply_text("کد ملی نامعتبر است.")
        return
    if len(parts) == 1:
        rows = lookup_scores(code)
        if not rows:
            await update.message.reply_text("نمره‌ای ثبت نشده است.")
            return
        lines = [f"{r[0].capitalize()}: {r[1]}" for r in rows]
        await update.message.reply_text("\n".join(lines))
    else:
        subject = ' '.join(parts[1:])
        row = lookup_subject(code, subject)
        if row:
            await update.message.reply_text(f"{row[0].capitalize()}: {row[1]}")
        else:
            await update.message.reply_text("نمره این درس ثبت نشده است.")

# ---------- Telegram Polling in Thread ----------

def run_telegram_bot():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start_cmd))
    app.add_handler(CommandHandler('help', start_cmd))
    app.add_handler(CommandHandler('add', add_cmd))
    app.add_handler(CommandHandler('edit', edit_cmd))
    app.add_handler(CommandHandler('remove', remove_cmd))
    app.add_handler(CommandHandler('remove_all', remove_all_cmd))
    app.add_handler(CommandHandler('list_codes', list_codes_cmd))
    app.add_handler(CommandHandler('add_excel', add_excel_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("بات در حال اجراست...")
    app.run_polling()

# ---------- Flask App for Render ----------

flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Bot is running."

def main():
    Thread(target=run_telegram_bot, daemon=True).start()
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
