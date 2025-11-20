"""
Telegram Scheduling Bot — Auto Date Assignment
Features:
- Multi-admin approval (ADMIN_IDS env var, comma-separated)
- Users choose option (فتح — غلق — فتح وغلق) and provide scheduler info
- Max reservations per date = 15
- Admin approve / reject flow
- Rejection reason
- Upon approval, earliest free date (Sun–Thu, <15 bookings) is assigned automatically
- /mybookings shows user's bookings
- /cancel works anytime
- /help shows commands
"""

import os
import logging
import sqlite3
from datetime import datetime, timedelta
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
ASK_OPTION, ASK_SCHEDULER_INFO = range(2)

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
            option TEXT,
            scheduler_info TEXT,
            date TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
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

def create_booking(user_id:int, username:str, option:str, scheduler_info:str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bookings (user_id, username, option, scheduler_info, date, status, created_at)
        VALUES (?, ?, ?, ?, NULL, 'PENDING', ?)
    """, (user_id, username, option, scheduler_info, datetime.utcnow().isoformat()))
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()
    return booking_id

def save_admin_message(booking_id:int, admin_id:int, message_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO admin_messages (booking_id, admin_id, message_id) VALUES (?, ?, ?)", (booking_id, admin_id, message_id))
    conn.commit()
    conn.close()

def get_admin_messages(booking_id:int):
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

def set_booking_date(booking_id:int, date_str:str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE bookings SET date = ? WHERE id = ?", (date_str, booking_id))
    conn.commit()
    conn.close()

def count_approved_for_date(date_str:str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookings WHERE date = ? AND status = 'APPROVED'", (date_str,))
    (cnt,) = cur.fetchone()
    conn.close()
    return cnt

def get_booking(booking_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, username, option, scheduler_info, date, status FROM bookings WHERE id = ?", (booking_id,))
    row = cur.fetchone()
    conn.close()
    return row

def next_available_date():
    """Return the next Sun–Thu date with less than 15 approved bookings."""
    today = datetime.utcnow().date()
    d = today
    while True:
        d += timedelta(days=1)
        if d.weekday() in (6,0,1,2,3):  # Sun-Thu
            if count_approved_for_date(d.isoformat()) < 15:
                return d.isoformat()


def get_user_bookings(user_id:int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, date, status FROM bookings WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

# --- Helpers ---
def is_allowed_weekday(dt:datetime) -> bool:
    return dt.weekday() in (6,0,1,2,3)  # Sun-Thu

def next_n_sunthu(n:int=30):
    dates = []
    today = datetime.utcnow().date()
    d = today
    while len(dates) < n:
        d = d + timedelta(days=1)
        if is_allowed_weekday(datetime.combine(d, datetime.min.time())):
            dates.append(d.isoformat())
    return dates

def earliest_available_date():
    for d in next_n_sunthu():
        if count_approved_for_date(d) < 15:
            return d
    return None

# --- Bot handlers ---
async def set_commands(app):
    await app.bot.set_my_commands([
        ('start','إبدأ من جديد'),
        ('schedule','حجز موعد'),
        ('mybookings','عرض مواعيدي'),
        ('cancel','الغاء العملية الحالية'),
        ('help','قائمة الأوامر')
    ])

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Available Commands:\n\n"
        "/start — إبدأ من جديد\n"
        "/schedule — حجز موعد\n"
        "/mybookings — عرض مواعيدي\n"
        "/cancel — الغاء العملية الحالية\n"
        "/help — قائمة الأوامر هذه"
    )
    await update.message.reply_text(help_text)

async def mybookings_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    rows = get_user_bookings(user.id)
    if not rows:
        await update.message.reply_text("ليس لديك أي حجوزات.")
        return
    lines = [f"#{r[0]} — {r[1]} — {r[2]}" for r in rows]
    await update.message.reply_text("حجوزاتك:\n" + "\n".join(lines))

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
    "اهلا بك في هيئة الدواء المصرية فرع المنيا — يمكنك التقديم لحجز موعد لتقديم طلبك الأن برجاء الضغط علي زر MENU للبدء\n"
    " برجاء الانتباه ان مقدم الطلب يجب ان يكون صاحب المؤسسة الصيدلية او موكل عنه وأن الدخول بأسبقية التواجد بمقر الهيئة"
)


async def schedule_start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("فتح منشأة صيدلية", callback_data="option:فتح")],
        [InlineKeyboardButton("غلق منشأة صيدلية", callback_data="option:غلق")],
    ]
    await update.message.reply_text("برجاء اختيار نوع الحجز:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_OPTION

async def receive_option(update:Update, context:ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    option = query.data.split(":",1)[1]
    context.user_data['option'] = option
    await query.edit_message_text("من فضلك ارسل اسم صاحب المؤسسة الصيدلية ورقم الترخيص")
    return ASK_SCHEDULER_INFO

async def receive_scheduler_info(update:Update, context:ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("برجاء إدخال نص صحيح")
        return ASK_SCHEDULER_INFO
    context.user_data['scheduler_info'] = text

    user = update.message.from_user
    booking_id = create_booking(user.id, user.username or "",
                                context.user_data['option'],
                                context.user_data['scheduler_info'])

    caption = (
        f"New booking #{booking_id}\n"
        f"User: {user.full_name} (@{user.username})\n"
        f"UserID: {user.id}\n"
        f"Option: {context.user_data['option']}\n"
        f"Scheduler: {context.user_data['scheduler_info']}\n"
        f"Date: (To be assigned upon approval)"
    )

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Approve", callback_data=f"approve:{booking_id}"),
                                      InlineKeyboardButton("Reject", callback_data=f"reject:{booking_id}")]])

    for admin_id in ADMIN_IDS:
        try:
            msg = await context.bot.send_message(chat_id=admin_id, text=caption, reply_markup=keyboard)
            save_admin_message(booking_id, admin_id, msg.message_id)
        except Exception as e:
            logger.exception("Failed to send booking %s to admin %s: %s", booking_id, admin_id, e)

    await update.message.reply_text("تم إرسال الطلب الي المسئولين وفي انتظار الموافقة وسيتم اخبار سيادتكم بموعد الزيارة ")
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
    _, user_id, username, option, scheduler_info, date_str, status = booking

    # Remove buttons for all admins
    for a_id, msg_id in get_admin_messages(booking_id):
        try:
            await context.bot.edit_message_reply_markup(chat_id=a_id, message_id=msg_id, reply_markup=None)
        except Exception:
            pass
    clear_admin_messages(booking_id)

    if action == "approve":
        # Step 1: Assign next available date
        assigned_date = next_available_date()  # You need to define this helper
    
        # Step 2: Update booking in DB with assigned date and status
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("UPDATE bookings SET date = ?, status = 'APPROVED' WHERE id = ?", (assigned_date, booking_id))
        conn.commit()
        conn.close()
    
        # Step 3: Prepare message with assigned date
        details_text = (
            f"Booking #{booking_id} approved by {admin.first_name}\n"
            f"Assigned date: {assigned_date}\n"
            f"Option: {option}\n"
            f"Scheduler: {scheduler_info}"
        )
    
        # Step 4: Notify all admins
        for a in ADMIN_IDS:
            await context.bot.send_message(chat_id=a, text=details_text)
    
        # Step 5: Notify user
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"تمت الموافقة على حجزك #{booking_id} لـ {assigned_date} من قبل {admin.first_name}.\n"
                f"Option: {option}\n"
                f"Scheduler: {scheduler_info}"
            )
        )
    
        await query.edit_message_text(f"Booking #{booking_id} APPROVED by {admin.first_name}.")
        return
    
    if action == "reject":
        pending_rejections[admin.id] = booking_id
        await context.bot.send_message(chat_id=admin.id, text=f"برجاء إرسال سبب رفض الحجز #{booking_id}. الرسالة القادمة سيتم اعتمادها كسبب.")
        await query.edit_message_text(f"انتظار سبب الرفض من {admin.first_name}...")
        return


async def admin_rejection_reason_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    admin = update.message.from_user
    if admin.id not in pending_rejections:
        return
    reason = (update.message.text or "").strip()
    if not reason:
        await update.message.reply_text("برجاء إدخال سبب صحيح لرفض الحجز.")
        return
    booking_id = pending_rejections.pop(admin.id)
    booking = get_booking(booking_id)
    if not booking:
        await update.message.reply_text("الحجز غير موجود.")
        return
    _, user_id, username, option, scheduler_info, date_str, status = booking
    set_booking_status(booking_id, "REJECTED")
    for a in ADMIN_IDS:
        await context.bot.send_message(chat_id=a, text=f"Booking #{booking_id} was rejected by {admin.first_name}. Reason: {reason}")
    await context.bot.send_message(chat_id=user_id, text=f"تم رفض حجزك #{booking_id} من قبل {admin.first_name}.\nالسبب: {reason}")
    await update.message.reply_text(f"تم رفض الحجز #{booking_id} وإرسال الإشعارات.")

async def cancel_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("تم إلغاء العملية الحالية.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(set_commands).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("schedule", schedule_start)],
        states={
            ASK_OPTION: [CallbackQueryHandler(receive_option, pattern=r"^option:")],
            ASK_SCHEDULER_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_scheduler_info)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_handler),
            CommandHandler("start", start),
            CommandHandler("mybookings", mybookings_handler),
            CommandHandler("help", help_handler)
        ],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(admin_approve_reject, pattern=r"^(approve|reject):"))
    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_IDS), admin_rejection_reason_handler))
    app.add_handler(CommandHandler("mybookings", mybookings_handler))
    app.add_handler(CommandHandler("cancel", cancel_handler))
    app.add_handler(CommandHandler("help", help_handler))

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
