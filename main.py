"""
Telegram Scheduling Bot
Features:
- Schedule Sun-Thu only
- Must schedule at least 2 days in advance
- Max 10 people per day
- User must upload a document (file/photo)
- Booking is sent to ADMIN_ID for final approval (Approve / Reject)

Requirements:
- python-telegram-bot>=20.0
- aiosqlite (or use builtin sqlite3 for sync DB; here we use sqlite3)

Config via environment variables:
- BOT_TOKEN
- ADMIN_ID (integer Telegram user id)

Run: python telegram_scheduler_bot.py

"""

import os
import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
    # Sun=6? Python weekday(): Mon=0 Sun=6. We want Sun-Thu allowed.
    # Allowed weekdays: Sunday(6), Monday(0), Tuesday(1), Wednesday(2), Thursday(3)
    return dt.weekday() in (6, 0, 1, 2, 3)


def parse_date(text):
    try:
        # accept YYYY-MM-DD
        dt = datetime.strptime(text.strip(), '%Y-%m-%d')
        return dt
    except Exception:
        return None

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! Use /schedule to make a booking (Sun–Thu).\nCommands:\n/schedule - start booking\n/mybookings - view your bookings\n/status - view pending bookings (admin)"
    )


async def schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Please send the booking date in YYYY-MM-DD format. (Only Sun–Thu; at least 2 days in advance).'
    )
    return ASK_DATE


async def receive_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    dt = parse_date(text)
    if not dt:
        await update.message.reply_text('Invalid date format. Please send date as YYYY-MM-DD.')
        return ASK_DATE

    # enforce 2-day advance
    today = datetime.utcnow().date()
    min_allowed = today + timedelta(days=2)
    if dt.date() < min_allowed:
        await update.message.reply_text(f'Date too soon. Please choose a date on or after {min_allowed.isoformat()}')
        return ASK_DATE

    # enforce allowed weekdays
    if not is_allowed_weekday(dt):
        await update.message.reply_text('Selected date is not allowed. Bookings are allowed only from Sunday to Thursday.')
        return ASK_DATE

    # enforce max 10 per day (count only APPROVED bookings)
    date_str = dt.date().isoformat()
    if count_bookings_for_date(date_str) >= 10:
        await update.message.reply_text('Sorry, that date is fully booked. Please choose another date.')
        return ASK_DATE

    # save temporary choice in user_data
    context.user_data['chosen_date'] = date_str
    await update.message.reply_text('Great — now please upload the document you want to attach (photo or file).')
    return ASK_DOC


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # accept photo or document
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
        # choose highest-res photo
        file = update.message.photo[-1]
        file_id = file.file_id
        file_name = f'photo_{user.id}_{int(datetime.utcnow().timestamp())}.jpg'
    else:
        await update.message.reply_text('Please send a file or photo as the document.')
        return ASK_DOC

    # store booking in DB
    booking_id = add_booking(user.id, user.username or '', date_str, file_id, file_name)

    # forward file to admin with approve/reject buttons
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('Approve', callback_data=f'approve:{booking_id}'),
            InlineKeyboardButton('Reject', callback_data=f'reject:{booking_id}')
        ]
    ])

    # send to admin: include user, date, booking id
    caption = f'New booking #{booking_id}\nUser: {user.full_name} (@{user.username})\nUserID: {user.id}\nDate: {date_str}'

    try:
        # try to forward the actual file to admin
        await context.bot.send_message(chat_id=ADMIN_ID, text=caption)
        await context.bot.send_document(chat_id=ADMIN_ID, document=file_id, filename=file_name, reply_markup=keyboard)
    except Exception as e:
        logger.exception('Failed to send to admin: %s', e)
        await update.message.reply_text('Failed to send booking to admin. Please contact support.')
        return ConversationHandler.END

    await update.message.reply_text('Your booking has been submitted and is pending admin approval. You will be notified when it is approved or rejected.')
    return ConversationHandler.END


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

    # booking: (id, user_id, username, date, status, doc_file_id, doc_file_name)
    _, user_id, username, date_str, status, doc_file_id, doc_file_name = booking

    if action == 'approve':
        # check capacity again (in case other approvals happened meantime)
        if count_bookings_for_date(date_str) >= 10:
            set_booking_status(booking_id, 'REJECTED')
            await context.bot.send_message(chat_id=ADMIN_ID, text=f'Cannot approve #{booking_id}: date {date_str} is now full. Marked as REJECTED.')
            await context.bot.send_message(chat_id=user_id, text=f'Your booking #{booking_id} for {date_str} was rejected: date is full.')
            await query.edit_message_text(f'Booking #{booking_id} rejected automatically because date is full.')
            return

        set_booking_status(booking_id, 'APPROVED')
        await context.bot.send_message(chat_id=user_id, text=f'Your booking #{booking_id} for {date_str} has been APPROVED. Thank you!')
        await query.edit_message_text(f'Booking #{booking_id} APPROVED.')

    elif action == 'reject':
        set_booking_status(booking_id, 'REJECTED')
        await context.bot.send_message(chat_id=user_id, text=f'Your booking #{booking_id} for {date_str} has been REJECTED by admin.')
        await query.edit_message_text(f'Booking #{booking_id} REJECTED.')


async def mybookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    rows = user_bookings(user.id)
    if not rows:
        await update.message.reply_text('You have no bookings.')
        return

    lines = []
    for r in rows:
        bid, date_str, status = r
        lines.append(f'#{bid} — {date_str} — {status}')
    await update.message.reply_text('\n'.join(lines))


async def pending_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only admin can call
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text('Unauthorized')
        return
    rows = get_pending_bookings()
    if not rows:
        await update.message.reply_text('No pending bookings.')
        return

    for r in rows:
        bid, user_id, username, date_str, doc_file_id, doc_file_name = r
        caption = f'Pending #{bid}\nUser: {username} (id: {user_id})\nDate: {date_str}\n'
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton('Approve', callback_data=f'approve:{bid}'),
                InlineKeyboardButton('Reject', callback_data=f'reject:{bid}')
            ]
        ])
        await context.bot.send_message(chat_id=ADMIN_ID, text=caption)
        await context.bot.send_document(chat_id=ADMIN_ID, document=doc_file_id, filename=doc_file_name, reply_markup=keyboard)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Booking canceled.')
    return ConversationHandler.END


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Sorry, I did not understand that command.')


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('schedule', schedule_start)],
        states={
            ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_date)],
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
