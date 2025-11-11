"""
Telegram Scheduling Bot (Inline Date Buttons)
Features:
- Schedule Sun-Thu only
- Must schedule at least 2 days in advance
- Max 10 people per day
- User must upload a document (file/photo)
- Booking is sent to ADMIN_ID for final approval (Approve / Reject)

Requirements:
- python-telegram-bot>=20.3
- sqlite3 (built-in)

Config via environment variables:
- BOT_TOKEN
- ADMIN_ID (integer Telegram user id)

Run: python main.py
"""

import os
import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)

# --- Configuration ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID') or 0)
DB_PATH = os.environ.get('DB_PATH', 'bookings.db')
FILES_DIR = os.environ.get('FILES_DIR', 'uploaded_docs')

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError('Please set BOT_TOKEN and ADMIN_ID environment variables')

os.makedirs(FILES_DIR, exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Conversation states ---
ASK_DATE, ASK_DOC = range(2)

# --- Database helpers ---

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            doc_file_id TEXT,
            doc_file_name TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


def add_booking(user_id, username, date_str, doc_file_id, doc_file_name):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO bookings (user_id, username, date, status, doc_file_id, doc_file_name, created_at)
        VALUES (?, ?, ?, 'PENDING', ?, ?, ?)
    ''', (user_id, username, date_str, doc_file_id, doc_file_name, datetime.utcnow().isoformat()))
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()
    return booking_id


def count_bookings_for_date(date_str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookings WHERE date = ? AND status = 'APPROVED'", (date_str,))
    (count,) = cur.fetchone()
    conn.close()
    return count


def get_pending_bookings():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, username, date, doc_file_id, doc_file_name FROM bookings WHERE status = 'PENDING'")
    rows = cur.fetchall()
    conn.close()
    return rows


def set_booking_status(booking_id, status):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
    conn.commit()
    conn.close()


def get_booking(booking_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, username, date, status, doc_file_id, doc_file_name FROM bookings WHERE id = ?", (booking_id,))
    row = cur.fetchone()
    conn.close()
    return row


def user_bookings(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, date, status FROM bookings WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

# --- Helpers ---

def is_allowed_weekday(dt):
    # Python weekday(): Mon=0 Sun=6; we allow Sun–Thu (6,0,1,2,3)
    return dt.weekday() in (6, 0, 1, 2, 3)

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! Use /schedule to make a booking (Sun–Thu).\nCommands:\n"
        "/schedule - start booking\n"
        "/mybookings - view your bookings\n"
        "/pending - view pending bookings (admin)"
    )

# --- New: show inline date buttons ---
async def schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.utcnow().date()
    dates = []
    for i in range(1, 15):
        dt = today + timedelta(days=i)
        if is_allowed_weekday(dt):
            dates.append(dt)

    keyboard = []
    row = []
    for idx, dt in enumerate(dates):
        btn = InlineKeyboardButton(dt.isoformat(), callback_data=f'date:{dt.isoformat()}')
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Please choose a booking date (Sun–Thu, at least 2 days in advance):",
        reply_markup=reply_markup
    )
    return ASK_DATE

# --- New: receive selected date from button ---
async def receive_date_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith('date:'):
        await query.edit_message_text("Invalid selection. Please try again.")
        return ASK_DATE

    date_str = data.split(':')[1]
    dt = datetime.strptime(date_str, '%Y-%m-%d').date()

    # enforce 2-day advance
    if dt < (datetime.utcnow().date() + timedelta(days=2)):
        await query.edit_message_text("Date too soon. Please choose a date at least 2 days in advance.")
        return ASK_DATE

    # enforce max 10 per day
    if count_bookings_for_date(date_str) >= 10:
        await query.edit_message_text("Sorry, that date is fully booked. Please choose another date.")
        return ASK_DATE

    context.user_data['chosen_date'] = date_str
    await query.edit_message_text(
        f"Great — you selected {date_str}.\n"
        "Now please upload the document you want to attach (photo or file)."
    )
    return ASK_DOC

# --- Document upload & admin approval (same as before) ---
async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    date_str = context.user_data.get('chosen_date')
    if not date_str:
        await update.message.reply_text('Date missing. Please run /schedule again.')
        return ConversationHandler.END

    file_id = None
    file_name = None

    if update.message.document:
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name
    elif update.message.photo:
        file = update.message.photo[-1]
        file_id = file.file_id
        file_name = f'photo_{user.id}_{int(datetime.utcnow().timestamp())}.jpg'
    else:
        await update.message.reply_text('Please send a file or photo as the document.')
        return ASK_DOC

    booking_id = add_booking(user.id, user.username or '', date_str, file_id, file_name)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('Approve', callback_data=f'approve:{booking_id}'),
        InlineKeyboardButton('Reject', callback_data=f'reject:{booking_id}')
    ]])

    caption = f'New booking #{booking_id}\nUser: {user.full_name} (@{user.username})\nUserID: {user.id}\nDate: {date_str}'

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=caption)
        await context.bot.send_document(chat_id=ADMIN_ID, document=file_id, filename=file_name, reply_markup=keyboard)
    except Exception as e:
        logger.exception('Failed to send to admin: %s', e)
        await update.message.reply_text('Failed to send booking to admin. Please contact support.')
        return ConversationHandler.END

    await update.message.reply_text(
        'Your booking has been submitted and is pending admin approval. '
        'You will be notified when it is approved or rejected.'
    )
    return ConversationHandler.END

# --- Admin approve/reject ---
async def approve_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data:
        return

    action, booking_id_str = data.split(':')
    booking_id = int(booking_id_str)
    booking = get_booking(booking_id)
    if not booking:
        await query.edit_message_text('Booking not found (maybe deleted).')
        return

    _, user_id, username, date_str, status, doc_file_id, doc_file_name = booking

    if action == 'approve':
        if count_bookings_for_date(date_str) >= 10:
            set_booking_status(booking_id, 'REJECTED')
            await context.bot.send_message(chat_id=ADMIN_ID, text=f'Cannot approve #{booking_id}: date {date_str} full. Marked REJECTED.')
            await context.bot.send_message(chat_id=user_id, text=f'Your booking #{booking_id} for {date_str} was rejected: date full.')
            await query.edit_message_text(f'Booking #{booking_id} rejected automatically (date full).')
            return
        set_booking_status(booking_id, 'APPROVED')
        await context.bot.send_message(chat_id=user_id, text=f'Your booking #{booking_id} for {date_str} has been APPROVED.')
        await query.bot.send_message(chat_id=ADMIN_ID, text=f'Booking #{booking_id} APPROVED.')
        await query.edit_message_text(f'Booking #{booking_id} APPROVED.')
    elif action == 'reject':
        set_booking_status(booking_id, 'REJECTED')
        await context.bot.send_message(chat_id=user_id, text=f'Your booking #{booking_id} for {date_str} has been REJECTED by admin.')
        await query.edit_message_text(f'Booking #{booking_id} REJECTED.')

# --- User bookings ---
async def mybookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    rows = user_bookings(user.id)
    if not rows:
        await update.message.reply_text('You have no bookings.')
        return

    lines = [f'#{bid} — {date_str} — {status}' for bid, date_str, status in rows]
    await update.message.reply_text('\n'.join(lines))

# --- Admin pending ---
async def pending_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text('Unauthorized')
        return
    rows = get_pending_bookings()
    if not rows:
        await update.message.reply_text('No pending bookings.')
        return

    for bid, user_id, username, date_str, doc_file_id, doc_file_name in rows:
        caption = f'Pending #{bid}\nUser: {username} (id: {user_id})\nDate: {date_str}\n'
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton('Approve', callback_data=f'approve:{bid}'),
            InlineKeyboardButton('Reject', callback_data=f'reject:{bid}')
        ]])
        await context.bot.send_message(chat_id=ADMIN_ID, text=caption)
        await context.bot.send_document(chat_id=ADMIN_ID, document=doc_file_id, filename=doc_file_name, reply_markup=keyboard)

# --- Cancel / unknown ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Booking canceled.')
    return ConversationHandler.END

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Sorry, I did not understand that command.')

# --- Main ---
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('schedule', schedule_start)],
        states={
            ASK_DATE: [CallbackQueryHandler(receive_date_button, pattern=r'^date:')],
            ASK_DOC: [MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, receive_document)]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('mybookings', mybookings))
    app.add_handler(CommandHandler('pending', pending_admin))
    app.add_handler(CallbackQueryHandler(approve_reject_callback))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info('Bot starting...')
    app.run_polling()

if __name__ == '__main__':
    main()
