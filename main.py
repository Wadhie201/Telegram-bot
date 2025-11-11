"""
Telegram Scheduling Bot — Final Version
Features implemented:
- Multi-admin approval (ADMIN_IDS env var, comma-separated)
- Shows 10 upcoming Sun–Thu dates as inline buttons (no typing)
- Asks user how many files they will upload, then asks for date, then collects files
- Sends a text summary (not file attachments) to all admins with filenames and metadata
- Either admin can approve or reject; once acted, buttons are removed for all admins
- Admin who rejects is asked for a reason; reason is sent to the user
- Both admins are notified who approved/rejected
- /mybookings shows the user's bookings
- /cancel works anytime during scheduling
Notes:
- Requires python-telegram-bot v20.3 and Python 3.11 runtime (Railway runtime.txt recommended)
- Set environment variables: BOT_TOKEN, ADMIN_IDS (comma-separated, e.g. 808...,146...)
"""

import os
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import List, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)

# --- Config ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]
DB_PATH = os.environ.get("DB_PATH", "bookings.db")

if not BOT_TOKEN or not ADMIN_IDS:
    raise RuntimeError("Please set BOT_TOKEN and ADMIN_IDS environment variables (comma-separated)")

# --- Logging ---
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Conversation states ---
ASK_FILE_COUNT, ASK_DATE, ASK_DOC = range(3)

# --- In-memory pending rejection map: admin_id -> booking_id ---
pending_rejections = {}

# --- Database helpers ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS booking_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_name TEXT,
            FOREIGN KEY(booking_id) REFERENCES bookings(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_messages (
            booking_id INTEGER,
            admin_id INTEGER,
            message_id INTEGER
        )
    """)
    conn.commit()
    conn.close()

def create_booking(user_id:int, username:str, date_str:str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO bookings (user_id, username, date, status, created_at) VALUES (?, ?, ?, 'PENDING', ?)", (user_id, username, date_str, datetime.utcnow().isoformat()))
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()
    return booking_id

def add_booking_file(booking_id:int, file_id:str, file_type:str, file_name:str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO booking_files (booking_id, file_id, file_type, file_name) VALUES (?, ?, ?, ?)", (booking_id, file_id, file_type, file_name))
    conn.commit()
    conn.close()

def get_booking_files(booking_id:int) -> List[Tuple[str,str,str]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT file_id, file_type, file_name FROM booking_files WHERE booking_id = ?", (booking_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def save_admin_message(booking_id:int, admin_id:int, message_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO admin_messages (booking_id, admin_id, message_id) VALUES (?, ?, ?)", (booking_id, admin_id, message_id))
    conn.commit()
    conn.close()

def get_admin_messages(booking_id:int) -> List[Tuple[int,int]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT admin_id, message_id FROM admin_messages WHERE booking_id = ?", (booking_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def clear_admin_messages(booking_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM admin_messages WHERE booking_id = ?", (booking_id,))
    conn.commit()
    conn.close()

def set_booking_status(booking_id:int, status:str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
    conn.commit()
    conn.close()

def count_approved_for_date(date_str:str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookings WHERE date = ? AND status = 'APPROVED'", (date_str,))
    (cnt,) = cur.fetchone()
    conn.close()
    return cnt

def user_has_booking_for_date(user_id:int, date_str:str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookings WHERE user_id = ? AND date = ? AND status != 'REJECTED'", (user_id, date_str))
    (cnt,) = cur.fetchone()
    conn.close()
    return cnt > 0

def get_user_bookings(user_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, date, status FROM bookings WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_booking(booking_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, username, date, status FROM bookings WHERE id = ?", (booking_id,))
    row = cur.fetchone()
    conn.close()
    return row

# --- Helpers ---
def is_allowed_weekday(dt:datetime) -> bool:
    return dt.weekday() in (6,0,1,2,3)

def next_n_sunthu(n:int=10):
    dates = []
    today = datetime.utcnow().date()
    d = today
    while len(dates) < n:
        d = d + timedelta(days=1)
        if d >= (today + timedelta(days=2)) and is_allowed_weekday(datetime.combine(d, datetime.min.time())):
            dates.append(d.isoformat())
    return dates

# --- Bot handlers ---
async def set_commands(app):
    await app.bot.set_my_commands([
        ('ابدأ','Start the bot'),
        ('حجز موعد','Make a booking'),
        ('مواعيدي','View your bookings'),
        ('الغاء الموعد','Cancel current action')
    ])

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("اهلا بك  — يمكنك حجز موعد لتقديم طلبك الان")


async def schedule_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("كم عدد الملفات التي ترغب في رفعها ؟ (برجاء إدخال رقم)")
    return ASK_FILE_COUNT

async def receive_file_count(update:Update, context:ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        count = int(text)
        if count < 1:
            raise ValueError()
    except Exception:
        await update.message.reply_text("برجاء ادخال أرقام فقط")
        return ASK_FILE_COUNT
    context.user_data['file_count'] = count
    context.user_data['received_files'] = []
    dates = next_n_sunthu(10)
    keyboard = []
    row = []
    for i, d in enumerate(dates):
        row.append(InlineKeyboardButton(d, callback_data=f"date:{d}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    await update.message.reply_text("من فضلك اختار موعد للزيارة ", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_DATE

async def receive_date_callback(update:Update, context:ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("date:"):
        await query.edit_message_text("إختيار خاطئ")
        return ConversationHandler.END
    date_str = data.split(":",1)[1]
    user = query.from_user
    if user_has_booking_for_date(user.id, date_str):
        await query.edit_message_text(f"بالفغل لديك موعد محدد {date_str}. لاتستطيع الحجز مرة اخري الان")
        return ConversationHandler.END
    if count_approved_for_date(date_str) >= 10:
        await query.edit_message_text(f"{date_str} التاريخ المحدد ملئ برجاء اختيار موعد اخر")
        return ConversationHandler.END
    context.user_data['chosen_date'] = date_str
    await query.edit_message_text(f"موعد الزيارة هو  {date_str}\nبرجاء رفع {context.user_data['file_count']} ملف/صورة. يمكنك رفع الملفات واحد تلو الاخر. إضغط الغاء للتوقف")
    return ASK_DOC

async def receive_file(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if 'chosen_date' not in context.user_data:
        await update.message.reply_text("Date missing. Start with /schedule.")
        return ConversationHandler.END
    if update.message.document:
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name or "document"
        file_type = "document"
    elif update.message.photo:
        p = update.message.photo[-1]
        file_id = p.file_id
        file_name = f"photo_{user.id}_{int(datetime.utcnow().timestamp())}.jpg"
        file_type = "photo"
    else:
        await update.message.reply_text("ارسل صورة او ملف")
        return ASK_DOC
    context.user_data.setdefault('received_files', []).append((file_id, file_type, file_name))
    remaining = context.user_data['file_count'] - len(context.user_data['received_files'])
    if remaining > 0:
        await update.message.reply_text(f"مستلم. مرسل {remaining} ملفات اخري" )
        return ASK_DOC
    date_str = context.user_data['chosen_date']
    booking_id = create_booking(user.id, user.username or "", date_str)
    for fid, ftype, fname in context.user_data['received_files']:
        add_booking_file(booking_id, fid, ftype, fname)
    files = get_booking_files(booking_id)
    files_text = "\n".join([f"- {row[2] or row[0]} ({row[1]})" for row in files])
    caption = f"New booking #{booking_id}\nUser: {user.full_name} (@{user.username})\nUserID: {user.id}\nDate: {date_str}\nFiles:\n{files_text}"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Approve", callback_data=f"approve:{booking_id}"), InlineKeyboardButton("Reject", callback_data=f"reject:{booking_id}" )]])
    for admin_id in ADMIN_IDS:
        try:
            msg = await context.bot.send_message(chat_id=admin_id, text=caption, reply_markup=keyboard)
            save_admin_message(booking_id, admin_id, msg.message_id)
        except Exception as e:
            logger.exception("Failed to send booking %s to admin %s: %s", booking_id, admin_id, e)
    await update.message.reply_text("في انتظار موافقة المسئولين لتأكيد طلبك")
    return ConversationHandler.END

async def admin_approve_reject(update:Update, context:ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admin = query.from_user
    data = query.data
    if not data:
        return
    action, booking_id_str = data.split(":",1)
    booking_id = int(booking_id_str)
    booking = get_booking(booking_id)
    if not booking:
        await query.edit_message_text("لم يتم تحديد موعد")
        return
    _, user_id, username, date_str, status = booking
    admin_msgs = get_admin_messages(booking_id)
    for a_id, msg_id in admin_msgs:
        try:
            await context.bot.edit_message_reply_markup(chat_id=a_id, message_id=msg_id, reply_markup=None)
        except Exception:
            pass
    clear_admin_messages(booking_id)
    if action == "approve":
        if count_approved_for_date(date_str) >= 10:
            set_booking_status(booking_id, "REJECTED")
            for a in ADMIN_IDS:
                await context.bot.send_message(chat_id=a, text=f"Booking #{booking_id} auto-rejected (date {date_str} is full)." )
            await context.bot.send_message(chat_id=user_id, text=f"Your booking #{booking_id} for {date_str} was rejected automatically: date is full.")
            await query.edit_message_text(f"Booking #{booking_id} auto-rejected (full)." )
            return
        set_booking_status(booking_id, "APPROVED")
        for a in ADMIN_IDS:
            await context.bot.send_message(chat_id=a, text=f"Booking #{booking_id} for {date_str} approved by {admin.first_name}.")
        await context.bot.send_message(chat_id=user_id, text=f"Your booking #{booking_id} for {date_str} was approved by {admin.first_name}.")
        await query.edit_message_text(f"Booking #{booking_id} APPROVED by {admin.first_name}.")
        return
    if action == "reject":
        pending_rejections[admin.id] = booking_id
        await context.bot.send_message(chat_id=admin.id, text=f"Please send the reason for rejecting booking #{booking_id}. (Your next message will be used as the reason)")
        await query.edit_message_text(f"Awaiting rejection reason from {admin.first_name}...")
        return

async def admin_rejection_reason_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    admin = update.message.from_user
    if admin.id not in pending_rejections:
        return
    reason = (update.message.text or "").strip()
    if not reason:
        await update.message.reply_text("Please send a non-empty text reason for rejection.")
        return
    booking_id = pending_rejections.pop(admin.id)
    booking = get_booking(booking_id)
    if not booking:
        await update.message.reply_text("Booking not found.")
        return
    _, user_id, username, date_str, status = booking
    set_booking_status(booking_id, "REJECTED")
    for a in ADMIN_IDS:
        await context.bot.send_message(chat_id=a, text=f"Booking #{booking_id} for {date_str} was rejected by {admin.first_name}. Reason: {reason}")
    await context.bot.send_message(chat_id=user_id, text=f"Your booking #{booking_id} for {date_str} was rejected by {admin.first_name}.\nReason: {reason}")
    await update.message.reply_text(f"Booking #{booking_id} rejected and notifications sent.")
    return

async def mybookings_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    rows = get_user_bookings(user.id)
    if not rows:
        await update.message.reply_text("You have no bookings.")
        return
    lines = [f"#{r[0]} — {r[1]} — {r[2]}" for r in rows]
    await update.message.reply_text("Your bookings:\n" + "\n".join(lines))

async def cancel_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Booking process canceled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(set_commands).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("schedule", schedule_start)],
        states={
            ASK_FILE_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_file_count)],
            ASK_DATE: [CallbackQueryHandler(receive_date_callback, pattern=r"^date:")],
            ASK_DOC: [MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, receive_file)],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler), CommandHandler("start", start)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(admin_approve_reject, pattern=r"^(approve|reject):"))
    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_IDS), admin_rejection_reason_handler))
    app.add_handler(CommandHandler("mybookings", mybookings_handler))
    app.add_handler(CommandHandler("cancel", cancel_handler))

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
