#!/usr/bin/env python3
"""
Telegram bot for looking up multi-subject scores by Iranian national code.

Features:
- Multiple subject scores per person
- Admins can add/edit/remove scores via bot commands
- Bulk add via text or Excel/CSV files
- Lookups by national code (all subjects) or by "code subject" (single subject)
- HMAC-SHA256 hashing of national codes
"""

import logging
import os
import re
import sqlite3
import hmac
import hashlib
from datetime import datetime
from typing import Optional

import pandas as pd
from telegram import Update, Document
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
HMAC_SECRET = os.environ.get("HMAC_SECRET", "replace-with-a-long-random-secret")
DB_PATH = os.environ.get("DB_PATH", "scores.db")
ADMINS = set(int(x) for x in os.environ.get("ADMINS", "").split(',') if x.strip().isdigit())
# ----------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NATIONAL_CODE_RE = re.compile(r"^\d{10}$")
SPLIT_RE = re.compile(r"[|:\s]+")

# ---------------- Persian number helper ----------------
def persian_to_english_number(text: str) -> str:
    persian_numbers = '۰۱۲۳۴۵۶۷۸۹'
    english_numbers = '0123456789'
    translation_table = str.maketrans(persian_numbers, english_numbers)
    return text.translate(translation_table)

# ---------------- DB ----------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code_hmac TEXT NOT NULL,
        subject TEXT NOT NULL,
        score REAL NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(code_hmac, subject)
    );
    """)
    conn.commit()
    conn.close()

# ---------------- security helpers ----------------
def hmac_code(code: str) -> str:
    return hmac.new(HMAC_SECRET.encode('utf-8'), code.encode('utf-8'), hashlib.sha256).hexdigest()

def valid_iranian_national_code(code: str) -> bool:
    if not NATIONAL_CODE_RE.match(code):
        return False
    if len(set(code)) == 1:
        return False
    digits = list(map(int, code))
    s = sum(digits[i] * (10 - i) for i in range(9))
    r = s % 11
    check = digits[9]
    return check == r if r < 2 else check == 11 - r

# ---------------- data ops ----------------
def add_or_update_score(national_code: str, subject: str, score: float):
    code_h = hmac_code(national_code)
    subj = subject.strip().lower()
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scores (code_hmac, subject, score, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(code_hmac, subject) DO UPDATE SET score=excluded.score, updated_at=excluded.updated_at",
        (code_h, subj, score, now)
    )
    conn.commit()
    conn.close()

def remove_score(national_code: str, subject: str) -> bool:
    code_h = hmac_code(national_code)
    subj = subject.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM scores WHERE code_hmac = ? AND subject = ?", (code_h, subj))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed > 0

def remove_all_scores(national_code: str) -> int:
    code_h = hmac_code(national_code)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM scores WHERE code_hmac = ?", (code_h,))
    removed = cur.rowcount
    conn.commit()
    conn.close()
    return removed

def lookup_scores(national_code: str):
    code_h = hmac_code(national_code)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT subject, score, updated_at FROM scores WHERE code_hmac = ? ORDER BY subject", (code_h,))
    rows = cur.fetchall()
    conn.close()
    return rows

def lookup_subject(national_code: str, subject: str) -> Optional[tuple]:
    code_h = hmac_code(national_code)
    subj = subject.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT subject, score, updated_at FROM scores WHERE code_hmac = ? AND subject = ?", (code_h, subj))
    row = cur.fetchone()
    conn.close()
    return row

# ---------------- telegram helpers ----------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

# ---------------- telegram handlers ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! کافیه کد ملی ۱۰ رقمی خودت رو ارسال کنی تا نمراتت رو ببینی.\n"
        "اگر می‌خوای نمرهٔ یک درس خاص رو ببینی، فرمت زیر رو بفرست:\n"
        "<کدملی>|<نام درس>  یا  <کدملی> <نام درس>\n\n"
        "ادمین‌ها: /add, /edit, /remove, /remove_all, /list_codes, /add_bulk, /add_file"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)

# --------------- single add/edit ---------------
async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز به استفاده از این دستور هستند.")
        return
    text = update.message.text or ""
    payload = text.partition(' ')[2].strip()
    if not payload:
        await update.message.reply_text("فرمت: /add <کدملی> <نام درس> <نمره>")
        return
    parts = [p.strip() for p in SPLIT_RE.split(payload) if p.strip()]
    if len(parts) < 3:
        await update.message.reply_text("فرمت درست نیست. مثال: /add 0012345674 ریاضی 18")
        return
    code, *middle, score_s = parts
    subject = ' '.join(middle)
    code = persian_to_english_number(code)
    score_s = persian_to_english_number(score_s)
    if not valid_iranian_national_code(code):
        await update.message.reply_text("کد ملی نامعتبر است.")
        return
    try:
        score = float(score_s)
    except ValueError:
        await update.message.reply_text("نمره باید عدد باشد (مثلاً 18 یا 17.5).")
        return
    add_or_update_score(code, subject, score)
    await update.message.reply_text(f"نمرهٔ {subject} برای کد {code} ثبت/بروزرسانی شد.")

async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_cmd(update, context)

# --------------- remove ---------------
async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز به استفاده از این دستور هستند.")
        return
    payload = update.message.text.partition(' ')[2].strip()
    parts = [p.strip() for p in SPLIT_RE.split(payload) if p.strip()]
    if len(parts) < 2:
        await update.message.reply_text("فرمت: /remove <کدملی> <نام درس>")
        return
    code, *subject_parts = parts
    subject = ' '.join(subject_parts)
    code = persian_to_english_number(code)
    if not valid_iranian_national_code(code):
        await update.message.reply_text("کد ملی نامعتبر است.")
        return
    ok = remove_score(code, subject)
    if ok:
        await update.message.reply_text(f"نمرهٔ {subject} برای کد {code} حذف شد.")
    else:
        await update.message.reply_text("چنین نمره‌ای پیدا نشد.")

async def remove_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز به استفاده از این دستور هستند.")
        return
    code = persian_to_english_number(update.message.text.partition(' ')[2].strip())
    if not valid_iranian_national_code(code):
        await update.message.reply_text("فرمت: /remove_all <کدملی>")
        return
    removed = remove_all_scores(code)
    await update.message.reply_text(f"{removed} ردیف برای این کد حذف شد.")

async def list_codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز به استفاده از این دستور هستند.")
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT code_hmac FROM scores LIMIT 200")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("هیچ داده‌ای ثبت نشده است.")
        return
    text = "کدهای هش‌شده (تا 200):\n" + '\n'.join(r[0] for r in rows)
    await update.message.reply_text(text)

# ---------------- bulk add text ----------------
async def add_bulk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز به استفاده از این دستور هستند.")
        return
    payload = update.message.text.partition(' ')[2].strip()
    if not payload:
        await update.message.reply_text("فرمت: /add_bulk\nکدملی نام_درس نمره")
        return
    lines = payload.splitlines()
    results = []
    for line in lines:
        parts = [p.strip() for p in SPLIT_RE.split(line) if p.strip()]
        if len(parts) < 3:
            results.append(f"خط '{line}': فرمت اشتباه")
            continue
        code, *subject_parts, score_s = parts
        subject = ' '.join(subject_parts)
        code = persian_to_english_number(code)
        score_s = persian_to_english_number(score_s)
        if not valid_iranian_national_code(code):
            results.append(f"{code}: کد ملی نامعتبر")
            continue
        try:
            score = float(score_s)
        except ValueError:
            results.append(f"{code} {subject}: نمره باید عدد باشد")
            continue
        add_or_update_score(code, subject, score)
        results.append(f"{code} {subject}: ثبت شد")
    await update.message.reply_text("\n".join(results))

# ---------------- bulk add file ----------------
async def add_file_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز به استفاده از این دستور هستند.")
        return
    if not update.message.document:
        await update.message.reply_text("لطفاً فایل Excel یا CSV ارسال کنید.")
        return
    file: Document = update.message.document
    file_path = f"/tmp/{file.file_name}"
    await file.get_file().download_to_drive(file_path)
    try:
        if file.file_name.endswith(".csv"):
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)
        results = []
        for idx, row in df.iterrows():
            code = persian_to_english_number(str(row[0]))
            subject = str(row[1])
            score_s = persian_to_english_number(str(row[2]))
            if not valid_iranian_national_code(code):
                results.append(f"{code}: کد ملی نامعتبر")
                continue
            try:
                score = float(score_s)
            except ValueError:
                results.append(f"{code} {subject}: نمره باید عدد باشد")
                continue
            add_or_update_score(code, subject, score)
            results.append(f"{code} {subject}: ثبت شد")
        await update.message.reply_text("\n".join(results))
    except Exception as e:
        await update.message.reply_text(f"خطا در پردازش فایل: {e}")

# ---------------- handle messages ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    parts = [p.strip() for p in SPLIT_RE.split(text) if p.strip()]
    if len(parts) == 0:
        await update.message.reply_text("لطفاً کد ملی ۱۰ رقمی یا کد ملی همراه نام درس را ارسال کنید.")
        return
    code = persian_to_english_number(parts[0])
    if not NATIONAL_CODE_RE.match(code):
        await update.message.reply_text("لطفاً کد ملی ۱۰ رقمی خود را ارسال کنید.")
        return
    if not valid_iranian_national_code(code):
        await update.message.reply_text("کد ملی نامعتبر است. لطفاً دوباره بررسی کنید.")
        return
    if len(parts) == 1:
        rows = lookup_scores(code)
        if not rows:
            await update.message.reply_text("برای این کد ملی، نمره‌ای ثبت نشده است.")
            return
        lines = [f"{r[0].capitalize()}: {r[1]} (بروزرسانی: {r[2]})" for r in rows]
        await update.message.reply_text("\n".join(lines))
        return
    subject = ' '.join(parts[1:])
    row = lookup_subject(code, subject)
    if not row:
        await update.message.reply_text("نمرهٔ این درس برای این کد ملی ثبت نشده است.")
        return
    await update.message.reply_text(f"{row[0].capitalize()}: {row[1]} (بروزرسانی: {row[2]})")

# ---------------- main ----------------
def main():
    init_db()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM scores")
    count = cur.fetchone()[0]
    conn.close()
    if count == 0:
        # نمونه داده برای توسعه
        add_or_update_score("0012345674", "ریاضی", 18)
        add_or_update_score("0012345674", "فارسی", 17)
        add_or_update_score("0084571239", "زبان", 16)
        logger.info("داده‌های نمونه اضافه شد (محیط توسعه).")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # commands
    app.add_handler(CommandHandler('start', start_cmd))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('add', add_cmd))
    app.add_handler(CommandHandler('edit', edit_cmd))
    app.add_handler(CommandHandler('remove', remove_cmd))
    app.add_handler(CommandHandler('remove_all', remove_all_cmd))
    app.add_handler(CommandHandler('list_codes', list_codes_cmd))
    app.add_handler(CommandHandler('add_bulk', add_bulk_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex(r'/add_file'), add_file_cmd))

    # messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("بات آماده است — polling اجرا می‌شود.")
    app.run_polling()

if __name__ == '__main__':
    main()
from flask import Flask

app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is running", 200

if __name__ == '__main__':
    import threading

    # Start Telegram bot in a separate thread
    import asyncio
    from telegram_bot import main as telegram_main  # فرض کن بقیه کدها داخل telegram_bot.py هست

    bot_thread = threading.Thread(target=telegram_main)
    bot_thread.start()

    # Start Flask server
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
