#!/usr/bin/env python3
"""
Telegram bot for multi-subject scores (Iranian national code)
Features:
- Add/Edit/Remove scores
- Multi-entry via single command
- Excel import
- HMAC hashing of national codes
- Admin-only commands
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
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
HMAC_SECRET = os.environ.get("HMAC_SECRET", "replace-with-a-long-random-secret")
DB_PATH = os.environ.get("DB_PATH", "scores.db")
ADMINS = set(int(x) for x in os.environ.get("ADMINS", "").split(',') if x.strip().isdigit())

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NATIONAL_CODE_RE = re.compile(r"^\d{10}$")
SPLIT_RE = re.compile(r"[|:\s]+")

# ---------------- Persian number helper ----------------
def persian_to_english_number(text: str) -> str:
    persian_numbers = '۰۱۲۳۴۵۶۷۸۹'
    english_numbers = '0123456789'
    return text.translate(str.maketrans(persian_numbers, english_numbers))

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

# ---------------- security ----------------
def hmac_code(code: str) -> str:
    return hmac.new(HMAC_SECRET.encode(), code.encode(), hashlib.sha256).hexdigest()

def valid_iranian_national_code(code: str) -> bool:
    if not NATIONAL_CODE_RE.match(code): return False
    if len(set(code)) == 1: return False
    digits = list(map(int, code))
    s = sum(digits[i]*(10-i) for i in range(9))
    r = s % 11
    return digits[9]==r if r<2 else digits[9]==11-r

# ---------------- data ops ----------------
def add_or_update_score(code: str, subject: str, score: float):
    code_h = hmac_code(code)
    subj = subject.strip().lower()
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scores (code_hmac, subject, score, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(code_hmac, subject) DO UPDATE SET score=excluded.score, updated_at=excluded.updated_at",
        (code_h, subj, score, now)
    )
    conn.commit()
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

# ---------------- telegram helpers ----------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

# ---------------- command handlers ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! کافیه کد ملی ۱۰ رقمی خودت رو ارسال کنی تا نمراتت رو ببینی.\n"
        "فرمت برای درس خاص: <کدملی>|<نام درس> یا <کدملی> <نام درس>\n\n"
        "ادمین‌ها: /add, /edit, /remove, /remove_all, /list_codes, /import_excel"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها می‌توانند از این دستور استفاده کنند.")
        return
    payload = update.message.text.partition(' ')[2].strip()
    if not payload:
        await update.message.reply_text("فرمت: /add <کدملی> <درس> <نمره>")
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
    await update.message.reply_text(f"نمرهٔ {subject} برای {code} ثبت شد.")

async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_cmd(update, context)

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها می‌توانند از این دستور استفاده کنند.")
        return
    payload = update.message.text.partition(' ')[2].strip()
    parts = [p.strip() for p in SPLIT_RE.split(payload) if p.strip()]
    if len(parts) < 2:
        await update.message.reply_text("فرمت: /remove <کدملی> <درس>")
        return
    code, *subject_parts = parts
    subject = ' '.join(subject_parts)
    code = persian_to_english_number(code)
    if not valid_iranian_national_code(code):
        await update.message.reply_text("کد ملی نامعتبر است.")
        return
    ok = remove_score(code, subject)
    if ok:
        await update.message.reply_text(f"نمرهٔ {subject} برای {code} حذف شد.")
    else:
        await update.message.reply_text("چنین نمره‌ای پیدا نشد.")

async def remove_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها می‌توانند از این دستور استفاده کنند.")
        return
    code = persian_to_english_number(update.message.text.partition(' ')[2].strip())
    if not valid_iranian_national_code(code):
        await update.message.reply_text("فرمت: /remove_all <کدملی>")
        return
    removed = remove_all_scores(code)
    await update.message.reply_text(f"{removed} ردیف حذف شد.")

async def list_codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها می‌توانند از این دستور استفاده کنند.")
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT code_hmac FROM scores LIMIT 200")
    rows = cur.fetchall()
    conn.close()
    text = "کدهای هش‌شده:\n" + "\n".join(r[0] for r in rows) if rows else "هیچ داده‌ای ثبت نشده."
    await update.message.reply_text(text)

async def import_excel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: import scores from uploaded Excel file"""
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها می‌توانند از این دستور استفاده کنند.")
        return
    if not update.message.document:
        await update.message.reply_text("یک فایل اکسل ارسال کنید.")
        return
    file = await update.message.document.get_file()
    file_path = f"/tmp/{update.message.document.file_name}"
    await file.download_to_drive(file_path)
    try:
        df = pd.read_excel(file_path)
        count = 0
        for _, row in df.iterrows():
            code = str(row['code'])
            subject = str(row['subject'])
            score = float(row['score'])
            code = persian_to_english_number(code)
            if valid_iranian_national_code(code):
                add_or_update_score(code, subject, score)
                count += 1
        await update.message.reply_text(f"{count} رکورد از فایل اکسل ثبت شد.")
    except Exception as e:
        await update.message.reply_text(f"خطا در پردازش فایل: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    parts = [p.strip() for p in SPLIT_RE.split(text) if p.strip()]
    if len(parts) == 0:
        await update.message.reply_text("لطفاً کد ملی ۱۰ رقمی یا کد ملی همراه درس را ارسال کنید.")
        return
    code = persian_to_english_number(parts[0])
    if not valid_iranian_national_code(code):
        await update.message.reply_text("کد ملی نامعتبر است.")
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
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # commands
    app.add_handler(CommandHandler('start', start_cmd))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('add', add_cmd))
    app.add_handler(CommandHandler('edit', edit_cmd))
    app.add_handler(CommandHandler('remove', remove_cmd))
    app.add_handler(CommandHandler('remove_all', remove_all_cmd))
    app.add_handler(CommandHandler('list_codes', list_codes_cmd))
    app.add_handler(CommandHandler('import_excel', import_excel_cmd))

    # messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("بات آماده است — polling اجرا می‌شود.")
    app.run_polling()

if __name__ == '__main__':
    main()
