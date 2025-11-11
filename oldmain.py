"""
Telegram Scheduling Bot with file count, date buttons, duplicate prevention, and admin notifications
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

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError('Please set BOT_TOKEN and ADMIN_ID environment variables')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Conversation states ---
ASK_FILE_COUNT, ASK_DATE, ASK_DOC = range(3)

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
            created_at TEXT NOT NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS booking_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_name TEXT,
            FOREIGN KEY(booking_id) REFERENCES bookings(id)
        )
    ''')
    conn.commit()
    conn.close()


def add_booking(user_id, username, date_str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO bookings (user_id, username, date, status, created_at)
        VALUES (?, ?, ?, 'PENDING', ?)
    ''', (user_id, username, date_str, datetime.utcnow().isoformat()))
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()
    return booking_id


def add_booking_file(booking_id, file_id, file_type, file_name):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO booking_files (booking_id, file_id, file_type, file_name)
        VALUES (?, ?, ?, ?)
    ''', (booking_id, file_id, file_type, file_name))
    conn.commit()
    conn.close()


def count_bookings_for_date(date_str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookings WHERE date = ? AND status = 'APPROVED'", (date_str,))
    (count,) = cur.fetchone()
    conn.close()
    return count


def has_user_booking_for_date(user_id, date_str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookings WHERE user_id=? AND date=? AND status!='REJECTED'", (user_id, date_str))
    (count,) = cur.fetchone()
    conn.close()
    return count > 0


def get_pending_bookings():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, username, date FROM bookings WHERE status = 'PENDING'")
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
    cur.execute("SELECT id, user_id, username, date, status FROM bookings WHERE id = ?", (booking_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_booking_files(booking_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT file_id, file_type, file_name FROM booking_files WHERE booking_id = ?", (booking_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def user_bookings(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, date, status FROM bookings WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


# --- Helpers ---
def is_allowed_weekday(dt):
    # Sun=6, Mon=0 ... Thu=3
    return dt.weekday() in (6, 0, 1, 2, 3)


# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! Use /schedule to make a booking (Sun–Thu).\n"
        "Commands:\n"
        "/schedule - start booking\n"
        "/mybookings - view your bookings\n"
        "/pending - view pending bookings (admin)"
    )


async def schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "How many files/photos do you want to attach for this booking?"
    )
    return ASK_FILE_COUNT


async def receive_file_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        count = int(update.message.text)
        if count < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid number (1 or more).")
        return ASK_FILE_COUNT

    context.user_data['file_count'] = count
    context.user_data['received_files'] = []

    # show date buttons (next 2 weeks, skipping too soon)
    today = datetime.utcnow().date()
    min_allowed = today + timedelta(days=2)
    dates = []
    for i in range(2, 16):
        dt = today + timedelta(days=i)
        if dt >= min_allowed and is_allowed_weekday(dt):
            dates.append(dt)

    buttons = [[InlineKeyboardButton(d.isoformat(), callback_data=f'date:{d.isoformat()}')] for d in dates]
    await update.message.reply_text("Select a booking date:", reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_DATE


async def receive_date_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith('date:'):
        await query.edit_message_text("Invalid selection.")
        return ConversationHandler.END

    date_str = data.split(':')[1]

    user = query.from_user
    # prevent duplicate booking for same user/date
    if has_user_booking_for_date(user.id, date_str):
        await query.edit_message_text(f"You already have a booking for {date_str}. Cannot schedule twice.")
        return ConversationHandler.END

    # enforce max 10 per day
    if count_bookings_for_date(date_str) >= 10:
        await query.edit_message_text("Sorry, that date is fully booked. Start again with /schedule.")
        return ConversationHandler.END

    context.user_data['chosen_date'] = date_str
    await query.edit_message_text(f"Great! Now please send {context.user_data['file_count']} file(s)/photo(s).")
    return ASK_DOC


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    file_id = None
    file_name = None
    file_type = None

    if update.message.document:
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name
        file_type = 'document'
    elif update.message.photo:
        file = update.message.photo[-1]
        file_id = file.file_id
        file_name = f'photo_{user.id}_{int(datetime.utcnow().timestamp())}.jpg'
        file_type = 'photo'
    else:
        await update.message.reply_text("Please send a file or photo.")
        return ASK_DOC

    context.user_data['received_files'].append((file_id, file_type, file_name))
    remaining = context.user_data['file_count'] - len(context.user_data['received_files'])
    if remaining > 0:
        await update.message.reply_text(f"Received. Send {remaining} more file(s).")
        return ASK_DOC

    # all files received -> create booking
    booking_id = add_booking(user.id, user.username or '', context.user_data['chosen_date'])
    for f_id, f_type, f_name in context.user_data['received_files']:
        add_booking_file(booking_id, f_id, f_type, f_name)

    # send to admin
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Approve", callback_data=f"approve:{booking_id}"),
         InlineKeyboardButton("Reject", callback_data=f"reject:{booking_id}")]
    ])
    caption = f"New booking #{booking_id}\nUser: {user.full_name} (@{user.username})\nUserID: {user.id}\nDate: {context.user_data['chosen_date']}"

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=caption)
        for f_id, f_type, f_name in context.user_data['received_files']:
            if f_type == 'photo':
                await context.bot.send_photo(chat_id=ADMIN_ID, photo=f_id, reply_markup=keyboard)
            else:
                await context.bot.send_document(chat_id=ADMIN_ID, document=f_id, reply_markup=keyboard)
    except Exception as e:
        logger.exception("Failed to send to admin: %s", e)
        await update.message.reply_text("Failed to send booking to admin. Please contact support.")
        return ConversationHandler.END

    await update.message.reply_text("Booking submitted! Admin will approve/reject it.")
    return ConversationHandler.END


async def approve_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, booking_id = query.data.split(':')
    booking_id = int(booking_id)
    booking = get_booking(booking_id)
    if not booking:
        await query.edit_message_text("Booking not found.")
        return

    _, user_id, username, date_str, status = booking

    if action == 'approve':
        if count_bookings_for_date(date_str) >= 10:
            set_booking_status(booking_id, 'REJECTED')
            await context.bot.send_message(chat_id=user_id, text=f"Your booking #{booking_id} rejected: date full.")
            await query.edit_message_text(f"Booking #{booking_id} rejected automatically: date full.")
            # Notify admin
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"Booking #{booking_id} rejected automatically: date full.")
            return
        set_booking_status(booking_id, 'APPROVED')
        await context.bot.send_message(chat_id=user_id, text=f"Your booking #{booking_id} for {date_str} has been APPROVED.")
        await query.edit_message_text(f"Booking #{booking_id} APPROVED.")
    else:
        set_booking_status(booking_id, 'REJECTED')
        await context.bot.send_message(chat_id=user_id, text=f"Your booking #{booking_id} for {date_str} has been REJECTED by admin.")
        await query.edit_message_text(f"Booking #{booking_id} REJECTED.")
        # Notify admin
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"Booking #{booking_id} rejected successfully.")


async def mybookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    rows = user_bookings(user.id)
    if not rows:
        await update.message.reply_text("You have no bookings.")
        return
    text = "\n".join(f"#{bid} — {date_str} — {status}" for bid, date_str, status in rows)
    await update.message.reply_text(text)


async def pending_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized")
        return
    rows = get_pending_bookings()
    if not rows:
        await update.message.reply_text("No pending bookings.")
        return
    for bid, user_id, username, date_str in rows:
        files = get_booking_files(bid)
        caption = f"Pending #{bid}\nUser: {username} (id: {user_id})\nDate: {date_str}\n"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Approve", callback_data=f"approve:{bid}"),
                                          InlineKeyboardButton("Reject", callback_data=f"reject:{bid}")]])
        await context.bot.send_message(chat_id=ADMIN_ID, text=caption)
        for f_id, f_type, f_name in files:
            if f_type == 'photo':
                await context.bot.send_photo(chat_id=ADMIN_ID, photo=f_id, reply_markup=keyboard)
            else:
                await context.bot.send_document(chat_id=ADMIN_ID, document=f_id, reply_markup=keyboard)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Booking canceled.")
    return ConversationHandler.END


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sorry, I did not understand that command.")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('schedule', schedule_start)],
        states={
            ASK_FILE_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_file_count)],
            ASK_DATE: [CallbackQueryHandler(receive_date_button, pattern=r'^date:')],
            ASK_DOC: [MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, receive_document)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('mybookings', mybookings))
    app.add_handler(CommandHandler('pending', pending_admin))
    app.add_handler(CallbackQueryHandler(approve_reject_callback, pattern=r'^(approve|reject):'))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    # set commands for Telegram UI
    app.bot.set_my_commands([
        ('start', 'Start the bot'),
        ('schedule', 'Make a booking'),
        ('mybookings', 'View your bookings'),
        ('pending', 'View pending bookings (admin only)'),
        ('cancel', 'Cancel current action')
    ])

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == '__main__':
    main()
