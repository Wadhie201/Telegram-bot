"""
Telegram Scheduling Bot (Multi-admin version)
Features:
- Multiple admins (list via ADMIN_IDS environment variable, comma-separated)
- Either admin can approve/reject
- Once one admin acts, buttons are removed for everyone
- Both admins receive a notification with the acting admin's name
- User receives notification with admin name and rejection reason if applicable
"""

import os
import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)

# --- Configuration ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = os.environ.get('ADMIN_IDS', '').split(',')
ADMIN_IDS = [int(a.strip()) for a in ADMIN_IDS if a.strip().isdigit()]
DB_PATH = os.environ.get('DB_PATH', 'bookings.db')

if not BOT_TOKEN or not ADMIN_IDS:
    raise RuntimeError('Please set BOT_TOKEN and ADMIN_IDS environment variables')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Conversation states ---
ASK_DATE, ASK_DOC, ASK_REASON = range(3)

# --- Database setup ---

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
    cur.execute('''
        CREATE TABLE IF NOT EXISTS admin_messages (
            booking_id INTEGER,
            admin_id INTEGER,
            message_id INTEGER
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


def save_admin_message(booking_id, admin_id, message_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO admin_messages (booking_id, admin_id, message_id) VALUES (?, ?, ?)", (booking_id, admin_id, message_id))
    conn.commit()
    conn.close()


def get_admin_messages(booking_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT admin_id, message_id FROM admin_messages WHERE booking_id = ?", (booking_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def count_bookings_for_date(date_str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookings WHERE date = ? AND status = 'APPROVED'", (date_str,))
    (count,) = cur.fetchone()
    conn.close()
    return count


def get_booking(booking_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, username, date, status, doc_file_id, doc_file_name FROM bookings WHERE id = ?", (booking_id,))
    row = cur.fetchone()
    conn.close()
    return row


def set_booking_status(booking_id, status):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
    conn.commit()
    conn.close()


# --- Helpers ---

def is_allowed_weekday(dt):
    return dt.weekday() in (6, 0, 1, 2, 3)


def parse_date(text):
    try:
        return datetime.strptime(text.strip(), '%Y-%m-%d')
    except Exception:
        return None

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! Use /schedule to make a booking (Sun–Thu).\nCommands:\n/schedule - start booking\n/mybookings - view your bookings"
    )


async def schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Please send the booking date in YYYY-MM-DD format (Sun–Thu, at least 2 days ahead).')
    return ASK_DATE


async def receive_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    dt = parse_date(text)
    if not dt:
        await update.message.reply_text('Invalid date format. Use YYYY-MM-DD.')
        return ASK_DATE

    today = datetime.utcnow().date()
    min_allowed = today + timedelta(days=2)
    if dt.date() < min_allowed:
        await update.message.reply_text(f'Too soon. Choose a date on or after {min_allowed.isoformat()}.')
        return ASK_DATE

    if not is_allowed_weekday(dt):
        await update.message.reply_text('Only Sun–Thu allowed.')
        return ASK_DATE

    date_str = dt.date().isoformat()
    if count_bookings_for_date(date_str) >= 10:
        await update.message.reply_text('That date is fully booked.')
        return ASK_DATE

    context.user_data['chosen_date'] = date_str
    await update.message.reply_text('Now upload your document (photo or file).')
    return ASK_DOC


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    date_str = context.user_data.get('chosen_date')
    if not date_str:
        await update.message.reply_text('Missing date. Start again with /schedule.')
        return ConversationHandler.END

    if update.message.document:
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name
    elif update.message.photo:
        file = update.message.photo[-1]
        file_id = file.file_id
        file_name = f'photo_{user.id}_{int(datetime.utcnow().timestamp())}.jpg'
    else:
        await update.message.reply_text('Send a valid document or photo.')
        return ASK_DOC

    booking_id = add_booking(user.id, user.username or '', date_str, file_id, file_name)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('Approve', callback_data=f'approve:{booking_id}'),
        InlineKeyboardButton('Reject', callback_data=f'reject:{booking_id}')
    ]])

    caption = f'New booking #{booking_id}\nUser: {user.full_name} (@{user.username})\nUserID: {user.id}\nDate: {date_str}'

    for admin_id in ADMIN_IDS:
        try:
            msg = await context.bot.send_document(chat_id=admin_id, document=file_id, filename=file_name, caption=caption, reply_markup=keyboard)
            save_admin_message(booking_id, admin_id, msg.message_id)
        except Exception as e:
            logger.exception("Failed to send to admin %s: %s", admin_id, e)

    await update.message.reply_text('Booking submitted and pending admin approval.')
    return ConversationHandler.END


async def approve_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = query.from_user
    data = query.data

    if not data:
        return

    action, booking_id_str = data.split(':')
    booking_id = int(booking_id_str)
    booking = get_booking(booking_id)

    if not booking:
        await query.edit_message_text('Booking not found.')
        return

    _, user_id, username, date_str, status, doc_file_id, doc_file_name = booking

    # Disable buttons for all admins
    admin_messages = get_admin_messages(booking_id)
    for a_id, msg_id in admin_messages:
        try:
            await context.bot.edit_message_reply_markup(chat_id=a_id, message_id=msg_id, reply_markup=None)
        except Exception:
            pass

    if action == 'approve':
        if count_bookings_for_date(date_str) >= 10:
            set_booking_status(booking_id, 'REJECTED')
            for a_id in ADMIN_IDS:
                await context.bot.send_message(chat_id=a_id, text=f'Booking #{booking_id} auto-rejected: date full.')
            await context.bot.send_message(chat_id=user_id, text=f'Booking #{booking_id} for {date_str} was rejected automatically (date full).')
            return

        set_booking_status(booking_id, 'APPROVED')
        for a_id in ADMIN_IDS:
            await context.bot.send_message(chat_id=a_id, text=f'Booking #{booking_id} for {date_str} approved by {admin.first_name}.')
        await context.bot.send_message(chat_id=user_id, text=f'Your booking #{booking_id} for {date_str} was approved by {admin.first_name}.')

    elif action == 'reject':
        context.user_data['reject_booking_id'] = booking_id
        context.user_data['reject_user_id'] = user_id
        context.user_data['reject_date'] = date_str
        context.user_data['reject_admin_name'] = admin.first_name
        await context.bot.send_message(chat_id=admin.id, text=f'Please send the reason for rejecting booking #{booking_id}.')
        return ASK_REASON


async def receive_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text
    booking_id = context.user_data.get('reject_booking_id')
    user_id = context.user_data.get('reject_user_id')
    date_str = context.user_data.get('reject_date')
    admin_name = context.user_data.get('reject_admin_name')

    set_booking_status(booking_id, 'REJECTED')

    for a_id in ADMIN_IDS:
        await context.bot.send_message(chat_id=a_id, text=f'Booking #{booking_id} for {date_str} was rejected by {admin_name}. Reason: {reason}')

    await context.bot.send_message(chat_id=user_id, text=f'Your booking #{booking_id} for {date_str} was rejected by {admin_name}.\nReason: {reason}')
    await update.message.reply_text(f'Booking #{booking_id} rejected successfully.')
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Booking canceled.', reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('schedule', schedule_start)],
        states={
            ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_date)],
            ASK_DOC: [MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, receive_document)],
            ASK_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rejection_reason)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(approve_reject_callback))

    logger.info('Bot starting...')
    app.run_polling()


if __name__ == '__main__':
    main()
