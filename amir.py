#!/usr/bin/env python3
"""
Telegram bot for looking up multi-subject scores by Iranian national code.
Features added:
- Multiple subject scores per person (subject normalized)
- Admins can add/edit/remove scores via bot commands
- Lookups by national code (returns all subjects) or by "code subject" (returns single subject)
- HMAC-SHA256 hashing of national codes before storing in DB
- Simple admin authorization using TELEGRAM admin IDs

Usage:
- Set environment variables TELEGRAM_TOKEN and HMAC_SECRET
- Optionally set ADMINS as comma-separated Telegram user IDs (numbers)
- Run: python telegram_bot_scores.py

Commands (admin only):
- /add <national_code>|<subject>|<score>   -> add or set score for subject
- /edit <national_code>|<subject>|<score>  -> alias for /add (upsert)
- /remove <national_code>|<subject>        -> remove specific subject score
- /remove_all <national_code>              -> remove all scores for a code
- /list_codes                               -> list hashed entries (admin)

For users:
- Send just the 10-digit national code to get all subjects and scores
- Send "<code> <subject>" (or use | or :) to get a single subject score

Note: This is an example for development. For production use, deploy on a server
and protect keys with a secret manager. Do not log raw national codes.
"""

import logging
import os
import re
import sqlite3
import hmac
import hashlib
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
HMAC_SECRET = os.environ.get("HMAC_SECRET", "replace-with-a-long-random-secret")
DB_PATH = os.environ.get("DB_PATH", "scores.db")
# ADMINS: comma separated Telegram user ids (integers)
ADMINS = set(int(x) for x in os.environ.get("ADMINS", "").split(',') if x.strip().isdigit())
# ----------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NATIONAL_CODE_RE = re.compile(r"^\d{10}$")
SPLIT_RE = re.compile(r"[|:\s]+")

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
    """Return HMAC-SHA256 hex of the national code using HMAC_SECRET."""
    return hmac.new(HMAC_SECRET.encode('utf-8'), code.encode('utf-8'), hashlib.sha256).hexdigest()


def valid_iranian_national_code(code: str) -> bool:
    if not NATIONAL_CODE_RE.match(code):
        return False
    # Reject same-digit codes like 0000000000
    if len(set(code)) == 1:
        return False
    digits = list(map(int, code))
    s = sum(digits[i] * (10 - i) for i in range(9))
    r = s % 11
    check = digits[9]
    if r < 2:
        return check == r
    else:
        return check == 11 - r

# ---------------- data ops ----------------

def add_or_update_score(national_code: str, subject: str, score: float):
    code_h = hmac_code(national_code)
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

# ---------------- telegram handlers ----------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! کافیه کد ملی ۱۰ رقمی خودت رو ارسال کنی تا نمراتت رو ببینی.\n"
        "اگر می‌خوای نمرهٔ یک درس خاص رو ببینی، فرمت زیر رو بفرست:\n"
        "<کدملی>|<نام درس>  یا  <کدملی> <نام درس>\n\n"
        "ادمین‌ها: /add, /edit, /remove, /remove_all, /list_codes"
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز به استفاده از این دستور هستند.")
        return
    text = update.message.text or ""
    payload = text.partition(' ')[2].strip()
    if not payload:
        await update.message.reply_text("فرمت: /add <کدملی>|<نام درس>|<نمره>")
        return
    parts = [p.strip() for p in SPLIT_RE.split(payload) if p.strip()]
    if len(parts) < 3:
        await update.message.reply_text("فرمت درست نیست. مثال: /add 0012345674|ریاضی|18")
        return
    code, subject, score_s = parts[0], ' '.join(parts[1:-1]) if len(parts)>3 else parts[1], parts[-1]
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


# edit is alias of add
async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add_cmd(update, context)


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("فقط ادمین‌ها مجاز به استفاده از این دستور هستند.")
        return
    payload = update.message.text.partition(' ')[2].strip()
    parts = [p.strip() for p in SPLIT_RE.split(payload) if p.strip()]
    if len(parts) < 2:
        await update.message.reply_text("فرمت: /remove <کدملی>|<نام درس>")
        return
    code, subject = parts[0], ' '.join(parts[1:])
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
    payload = update.message.text.partition(' ')[2].strip()
    code = payload.strip()
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    # try forms: "0012345674"  or "0012345674 ریاضی"  or "0012345674|ریاضی" or "0012345674:ریاضی"
    parts = [p.strip() for p in SPLIT_RE.split(text) if p.strip()]
    if len(parts) == 0:
        await update.message.reply_text("لطفاً کد ملی ۱۰ رقمی یا کد ملی همراه نام درس را ارسال کنید.")
        return
    code = parts[0]
    if not NATIONAL_CODE_RE.match(code):
        await update.message.reply_text("لطفاً کد ملی ۱۰ رقمی خود را ارسال کنید.")
        return
    if not valid_iranian_national_code(code):
        await update.message.reply_text("کد ملی نامعتبر است. لطفاً دوباره بررسی کنید.")
        return
    # if only code => list all
    if len(parts) == 1:
        rows = lookup_scores(code)
        if not rows:
            await update.message.reply_text("برای این کد ملی، نمره‌ای ثبت نشده است.")
            return
        lines = [f"{r[0].capitalize()}: {r[1]} (بروزرسانی: {r[2]})" for r in rows]
        await update.message.reply_text("\n".join(lines))
        return
    # else code + subject
    subject = ' '.join(parts[1:])
    row = lookup_subject(code, subject)
    if not row:
        await update.message.reply_text("نمرهٔ این درس برای این کد ملی ثبت نشده است.")
        return
    await update.message.reply_text(f"{row[0].capitalize()}: {row[1]} (بروزرسانی: {row[2]})")


# ---------------- main ----------------

def main():
    init_db()
    # optional: add sample entries for testing (only when DB empty)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM scores")
    count = cur.fetchone()[0]
    conn.close()
    if count == 0:
        # only for development/testing
        add_or_update_score("0012345674", "ریاضی", 18)
        add_or_update_score("0012345674", "فارسی", 17)
        add_or_update_score("0084571239", "زبان", 16)
        logger.info("داده‌های نمونه اضافه شد (فقط محیط توسعه).")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # commands
    app.add_handler(CommandHandler('start', start_cmd))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('add', add_cmd))
    app.add_handler(CommandHandler('edit', edit_cmd))
    app.add_handler(CommandHandler('remove', remove_cmd))
    app.add_handler(CommandHandler('remove_all', remove_all_cmd))
    app.add_handler(CommandHandler('list_codes', list_codes_cmd))

    # messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("بات آماده است — polling اجرا می‌شود (برای توسعه).")
    app.run_polling()


if __name__ == '__main__':
    main()
