import os
import logging
import asyncio
import time
import html
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from flask import Flask
from models import db, User, Request, Ban, Setting
from sqlalchemy import desc, or_, func

# ===================== CONFIG =====================
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID = 7370821047
OWNER_USERNAME = "@FEARFLESH"
ADMIN_GROUP_ID = None # Loaded from DB below

# Default Reply Markup (Bottom Menu)
def get_main_menu():
    keyboard = [
        ["📝 Send Message", "🌐 Language"],
        ["👤 Owner", "ℹ️ Help"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}
db.init_app(app)

with app.app_context():
    db.create_all()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# callback_data short rakho
CB_CONFIRM_YES = "C_Y"
CB_CONFIRM_NO = "C_N"
CB_REPLY_PREFIX = "R:"     # R:<request_id>
CB_REJECT_PREFIX = "X:"    # X:<request_id>
CB_CLEAN_YES = "CLN_Y"
CB_CLEAN_NO = "CLN_N"

# Group setup callbacks
CB_GRP_YES = "AGY:"        # AGY:<group_id>
CB_GRP_RETRY = "AGR:"      # AGR:<group_id>

# Status Callbacks
CB_STAT_PAGE = "ST_P:"      # ST_P:<page>
CB_STAT_USER = "ST_U:"      # ST_U:<user_id>:<page>
CB_STAT_BACK = "ST_B:"      # ST_B:<page>
CB_STAT_REFRESH = "ST_R:"  # ST_R:<page>

# in-memory states
ADMIN_WAITING_REPLY_FOR = {}  # {ADMIN_ID: request_id}
COOLDOWN_UNTIL = {}  # {user_id: unix_ts}

# ===================== HELPERS =====================
def is_owner(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def safe_send_message(bot, chat_id, text, reply_markup=None, parse_mode=None):
    """Helper to send messages safely and handle Telegram rate limits (429)."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return await bot.send_message(
                chat_id=chat_id, 
                text=text, 
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        except RetryAfter as e:
            logger.warning(f"Rate limited. Retrying after {e.retry_after} seconds. Attempt {attempt + 1}/{max_retries}")
            await asyncio.sleep(e.retry_after)
        except TelegramError as e:
            logger.error(f"Telegram error while sending message to {chat_id}: {e}")
            break
        except Exception as e:
            logger.error(f"Unexpected error while sending message to {chat_id}: {e}")
            break
    return None

async def safe_edit_message(bot, chat_id, message_id, text):
    """Helper to edit messages safely with retry logic for rate limits."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        except RetryAfter as e:
            logger.warning(f"Rate limited edit. Retrying after {e.retry_after} seconds. Attempt {attempt + 1}/{max_retries}")
            await asyncio.sleep(e.retry_after)
        except TelegramError as e:
            if "message is not modified" in str(e).lower():
                return True
            logger.error(f"Telegram error while editing message {message_id} in {chat_id}: {e}")
            break
        except Exception as e:
            logger.error(f"Unexpected error while editing message {message_id} in {chat_id}: {e}")
            break
    return None

async def countdown_tick(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    chat_id = data["chat_id"]
    msg_id = data["msg_id"]
    remaining = data["remaining"]

    if remaining > 0:
        await safe_edit_message(context.bot, chat_id, msg_id, str(remaining))
        data["remaining"] -= 1
    else:
        await safe_edit_message(context.bot, chat_id, msg_id, "0")
        await safe_send_message(context.bot, chat_id, f"Now you can send message to the owner {OWNER_USERNAME}")
        job.schedule_removal()

# ===================== DB WRAPPERS =====================
def upsert_user(u, lang=None):
    with app.app_context():
        user = db.session.get(User, u.id)
        if not user:
            user = User(user_id=u.id, username=u.username, first_name=u.first_name, lang=lang or 'hinglish')
            db.session.add(user)
        else:
            user.username = u.username
            user.first_name = u.first_name
            if lang:
                user.lang = lang
        user.updated_at = datetime.utcnow()
        db.session.commit()

def get_lang(user_id: int) -> str:
    with app.app_context():
        user = db.session.get(User, user_id)
        return user.lang if user else "hinglish"

def create_request(user_id: int, username: str, text: str) -> dict:
    with app.app_context():
        fifteen_mins_ago = datetime.utcnow() - timedelta(minutes=15)
        existing = db.session.query(Request).filter(
            Request.user_id == user_id,
            Request.status == 'pending',
            Request.created_at > fifteen_mins_ago,
            Request.thread_id.isnot(None)
        ).order_by(desc(Request.created_at)).first()

        if existing:
            thread_id = existing.thread_id
            max_part = db.session.query(func.max(Request.part_no)).filter(Request.thread_id == thread_id).scalar() or 0
            part_no = max_part + 1
        else:
            thread_id = None
            part_no = 1

        new_req = Request(
            user_id=user_id,
            username=username,
            text=text,
            status='pending',
            created_at=datetime.utcnow(),
            thread_id=thread_id,
            part_no=part_no
        )
        db.session.add(new_req)
        db.session.flush()

        if thread_id is None:
            new_req.thread_id = new_req.request_id
            thread_id = new_req.request_id

        db.session.commit()
        return {"request_id": new_req.request_id, "thread_id": thread_id, "part_no": part_no}

def get_request(rid: int):
    with app.app_context():
        req = db.session.get(Request, rid)
        if req:
            return {
                "request_id": req.request_id,
                "user_id": req.user_id,
                "username": req.username,
                "text": req.text,
                "status": req.status,
                "created_at": req.created_at,
                "thread_id": req.thread_id,
                "part_no": req.part_no
            }
        return None

def set_request_status(rid: int, status: str):
    with app.app_context():
        req = db.session.get(Request, rid)
        if req:
            req.status = status
            db.session.commit()

def search_requests(query: str):
    with app.app_context():
        q = db.session.query(Request)
        if query.startswith('@'):
            q = q.filter(Request.username.ilike(f"%{query[1:]}%"))
        elif query.isdigit():
            val = int(query)
            q = q.filter(or_(Request.user_id == val, Request.request_id == val))
        else:
            q = q.filter(Request.text.ilike(f"%{query}%"))
        
        results = q.order_by(desc(Request.created_at)).limit(10).all()
        return [{
            "request_id": r.request_id,
            "user_id": r.user_id,
            "username": r.username,
            "text": r.text,
            "status": r.status,
            "created_at": r.created_at,
            "thread_id": r.thread_id,
            "part_no": r.part_no
        } for r in results]

def clean_requests():
    with app.app_context():
        db.session.query(Request).delete()
        db.session.commit()

def ban_user(user_id: int, reason: str = "No reason"):
    with app.app_context():
        ban = db.session.get(Ban, user_id)
        if not ban:
            ban = Ban(user_id=user_id, reason=reason, banned_at=datetime.utcnow())
            db.session.add(ban)
        else:
            ban.reason = reason
            ban.banned_at = datetime.utcnow()
        db.session.commit()

def unban_user(user_id: int):
    with app.app_context():
        ban = db.session.get(Ban, user_id)
        if ban:
            db.session.delete(ban)
            db.session.commit()

def get_ban(user_id: int):
    with app.app_context():
        return db.session.get(Ban, user_id)

def list_bans(limit=20):
    with app.app_context():
        return db.session.query(Ban).order_by(desc(Ban.banned_at)).limit(limit).all()

def get_setting(key: str) -> str:
    with app.app_context():
        s = db.session.get(Setting, key)
        return s.value if s else os.environ.get(key.upper().replace("_", ""))

def set_setting(key: str, value: str):
    with app.app_context():
        s = db.session.get(Setting, key)
        if not s:
            s = Setting(key=key, value=value)
            db.session.add(s)
        else:
            s.value = value
        db.session.commit()

# Initial load for ADMIN_GROUP_ID
ADMIN_GROUP_ID = get_setting("admin_group_id")
if ADMIN_GROUP_ID:
    try:
        ADMIN_GROUP_ID = int(ADMIN_GROUP_ID)
    except:
        ADMIN_GROUP_ID = None

def get_paginated_users(page: int, per_page: int = 20):
    with app.app_context():
        offset = page * per_page
        query = db.session.query(User).filter(User.user_id != ADMIN_ID).order_by(desc(User.updated_at))
        total = query.count()
        users = query.offset(offset).limit(per_page).all()
        return users, total

def get_user_stats(user_id: int):
    with app.app_context():
        user = db.session.get(User, user_id)
        if not user:
            return None
        req_count = db.session.query(Request).filter(Request.user_id == user_id).count()
        is_banned = db.session.get(Ban, user_id) is not None
        return {
            "username": user.username,
            "first_name": user.first_name,
            "user_id": user.user_id,
            "req_count": req_count,
            "is_banned": is_banned
        }

# ===================== TEXTS =====================
def t(user_id: int, key: str) -> str:
    lang = get_lang(user_id)
    EN = {
        "start": "Bro, write everything in one message and send it 😁",
        "confirm": "Are you sure you have written all the things?",
        "write_again": "Okay, write again and send in one message.",
        "sent_to_admin": "Done. Your message has been sent.",
        "denied": "Your request has been denied.",
        "lang_choose": "Choose language / language choose karo:",
        "lang_now_en": "Language set to English.",
        "lang_now_hi": "Language set to Hinglish.",
        "admin_new": "New request received:",
        "admin_reply_prompt": "Write down the reply (it will go to the same user).",
        "admin_reply_sent": "Reply sent to user.",
        "admin_rejected": "Rejected and user informed.",
        "cooldown": "Please wait, cooldown running...",
        "banned": "You are banned from using this bot.",
    }
    HI = {
        "start": "Bro sab ek hi bari me likh ke bhej de 😁",
        "confirm": "Are you sure you have write all the things ??",
        "write_again": "Theek hai, dobara likh ke ek hi message me bhej.",
        "sent_to_admin": "Done, tumhara message send ho gaya.",
        "denied": "Your request has been denied.",
        "lang_choose": "Choose language / language choose karo:",
        "lang_now_en": "Language English set ho gayi.",
        "lang_now_hi": "Language Hinglish set ho gayi.",
        "admin_new": "New request aayi hai:",
        "admin_reply_prompt": "Reply likho (yeh same user ko jayega).",
        "admin_reply_sent": "User ko reply bhej diya.",
        "admin_rejected": "Reject kar diya aur user ko inform kar diya.",
        "cooldown": "Bhai ruk ja, cooldown chal raha hai...",
        "banned": "You are banned from using this bot.",
    }
    return (EN if lang == "en" else HI).get(key, key)

# ===================== HANDLERS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)
    
    welcome_msg = (
        "<b>👋 Welcome!</b>\n\n"
        "<b>How to message the owner:</b>\n"
        "1) 📝 Write everything in <b>one</b> message\n"
        "2) ✅ Confirm (Yes)\n"
        "3) ⏳ Wait for cooldown (10s)\n\n"
        f"<b>Owner:</b> {OWNER_USERNAME}"
    )
    
    if is_owner(user.id):
        welcome_msg += f"\n\nOwner Telegram ID: <code>{ADMIN_ID}</code>"
    
    await safe_send_message(context.bot, user.id, welcome_msg, parse_mode="HTML", reply_markup=get_main_menu())

async def owner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = f"Owner: {OWNER_USERNAME} | ID: <code>7370821047</code>"
    await safe_send_message(context.bot, user.id, msg, parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>ℹ️ How to use the bot:</b>\n\n"
        "1) 📝 Write your full message in <b>one</b> go.\n"
        "2) ✅ Tap <b>Yes</b> when asked to confirm.\n"
        "3) ⏳ Wait for 10 seconds before sending another.\n\n"
        "<i>Your message will be reviewed by the owner.</i>"
    )
    await safe_send_message(context.bot, update.effective_user.id, help_text, parse_mode="HTML")

async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    await safe_send_message(context.bot, update.effective_chat.id, f"Chat ID: <code>{update.effective_chat.id}</code>", parse_mode="HTML")

async def addgrp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await safe_send_message(context.bot, update.effective_user.id, "WTF you are not the owner man 😑")
        return
    
    if not context.args:
        usage = (
            "<b>Usage:</b> /addgrp -100xxxxxxxxxx\n\n"
            "1. Add me to the private group.\n"
            "2. Make me an <b>admin</b>.\n"
            "3. Send <code>/chatid</code> in that group to get the ID.\n"
            "4. Run <code>/addgrp [ID]</code> here."
        )
        await safe_send_message(context.bot, ADMIN_ID, usage, parse_mode="HTML")
        return

    try:
        gid = int(context.args[0])
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Yes ✅", callback_data=f"{CB_GRP_YES}{gid}")]])
        await safe_send_message(context.bot, ADMIN_ID, "Am I an admin in that grp 🤔 ?", reply_markup=kb)
    except ValueError:
        await safe_send_message(context.bot, ADMIN_ID, "Invalid group id.")

async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("English", callback_data="LANG:en"),
         InlineKeyboardButton("Hinglish", callback_data="LANG:hinglish")]
    ])
    await safe_send_message(context.bot, user.id, t(user.id, "lang_choose"), reply_markup=kb)

async def on_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    _, code = q.data.split(":", 1)
    upsert_user(user, lang=code)
    try:
        await q.edit_message_text(t(user.id, "lang_now_en") if code == "en" else t(user.id, "lang_now_hi"))
    except TelegramError as e:
        logger.error(f"Error editing lang message: {e}")

async def user_or_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    # Handle Bottom Menu Buttons
    if text == "📝 Send Message":
        await safe_send_message(context.bot, user.id, "Write everything in one message and send it.")
        return
    elif text == "🌐 Language":
        await lang_cmd(update, context)
        return
    elif text == "👤 Owner":
        await owner_cmd(update, context)
        return
    elif text == "ℹ️ Help":
        await help_cmd(update, context)
        return

    if not is_owner(user.id):
        if get_ban(user.id):
            await safe_send_message(context.bot, user.id, t(user.id, "banned"))
            return
        until = COOLDOWN_UNTIL.get(user.id, 0)
        if time.time() < until:
            await safe_send_message(context.bot, user.id, t(user.id, "cooldown"))
            return
    else:
        # Owner check for self-messaging
        if text not in ["📝 Send Message", "🌐 Language", "👤 Owner", "ℹ️ Help"]:
            # If owner is not in a reply state, they might be trying to message themselves
            rid = ADMIN_WAITING_REPLY_FOR.get(ADMIN_ID)
            if not rid:
                await safe_send_message(context.bot, user.id, "You are the owner bruh 😑")
                return

    if is_owner(user.id):
        rid = ADMIN_WAITING_REPLY_FOR.get(ADMIN_ID)
        if rid:
            req = get_request(rid)
            if not req:
                ADMIN_WAITING_REPLY_FOR.pop(ADMIN_ID, None)
                await safe_send_message(context.bot, update.effective_chat.id, "Request not found.")
                return

            target_user_id = int(req["user_id"])
            if get_ban(target_user_id):
                await safe_send_message(context.bot, update.effective_chat.id, "Cannot reply: user is banned.")
                ADMIN_WAITING_REPLY_FOR.pop(ADMIN_ID, None)
                return

            aesthetic_reply = (
                "<b>✨ Response from Owner ✨</b>\n"
                "───────────────────\n"
                f"{text}\n"
                "───────────────────\n"
                "<i>Thank you for reaching out!</i>"
            )
            await safe_send_message(context.bot, target_user_id, aesthetic_reply, parse_mode="HTML")
            set_request_status(rid, "approved")
            ADMIN_WAITING_REPLY_FOR.pop(ADMIN_ID, None)
            await safe_send_message(context.bot, update.effective_chat.id, t(ADMIN_ID, "admin_reply_sent"))
            return

    # User logic below
    if not is_owner(user.id):
        upsert_user(user)
        
        context.user_data["draft_text"] = text
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Yes", callback_data=CB_CONFIRM_YES),
             InlineKeyboardButton("No", callback_data=CB_CONFIRM_NO)]
        ])
        confirm_text = (
            "❓ <b>Confirm submission</b>\n"
            "Send this message to the owner?"
        )
        await safe_send_message(context.bot, user.id, confirm_text, reply_markup=kb, parse_mode="HTML")

async def on_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user

    if not is_owner(user.id):
        if get_ban(user.id):
            await q.edit_message_text(t(user.id, "banned"))
            return

    if is_owner(user.id):
        return

    if q.data == CB_CONFIRM_NO:
        context.user_data["draft_text"] = None
        try:
            await q.edit_message_text(t(user.id, "write_again"))
        except TelegramError as e:
            logger.error(f"Error editing confirm message: {e}")
        return

    draft = context.user_data.get("draft_text")
    if not draft:
        try:
            await q.edit_message_text(t(user.id, "write_again"))
        except TelegramError as e:
            logger.error(f"Error editing confirm message (no draft): {e}")
        return

    username_escaped = html.escape(("@" + user.username) if user.username else "(no username)")
    draft_escaped = html.escape(draft)
    res = create_request(user.id, ("@" + user.username) if user.username else "(no username)", draft)
    rid = res["request_id"]
    tid = res["thread_id"]
    pno = res["part_no"]

    admin_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✉️ Reply", callback_data=f"{CB_REPLY_PREFIX}{rid}"),
         InlineKeyboardButton("⛔ Reject", callback_data=f"{CB_REJECT_PREFIX}{rid}")]
    ])

    admin_msg = (
        f"<b>🧾 New Request</b>\n"
        f"<b>Thread:</b> <code>{tid}</code> | <b>Part:</b> <code>{pno}</code>\n\n"
        f"<b>User:</b> {username_escaped}\n"
        f"<b>TG ID:</b> <code>{user.id}</code>\n\n"
        f"<b>Message:</b>\n"
        f"<blockquote>{draft_escaped}</blockquote>"
    )
    
    target_chat = ADMIN_GROUP_ID if ADMIN_GROUP_ID else ADMIN_ID
    await safe_send_message(context.bot, target_chat, admin_msg, reply_markup=admin_kb, parse_mode="HTML")

    context.user_data["draft_text"] = None
    try:
        await q.edit_message_text(t(user.id, "sent_to_admin"))
    except TelegramError as e:
        logger.error(f"Error editing confirm message (sent to admin): {e}")

    COOLDOWN_UNTIL[user.id] = time.time() + 10

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await safe_send_message(context.bot, update.effective_user.id, "WTF you are not the owner man 😑")
        return

    if not context.args:
        await safe_send_message(context.bot, ADMIN_ID, "Usage: /search <user_id | @username | keyword>")
        return

    query = " ".join(context.args)
    results = search_requests(query)

    if not results:
        await safe_send_message(context.bot, ADMIN_ID, "No matching requests found.")
        return

    lines = []
    for r in results:
        created = r['created_at'].strftime("%Y-%m-%d %H:%M") if r['created_at'] else "N/A"
        txt = (r['text'][:80] + '...') if len(r['text']) > 80 else r['text']
        line = (f"ID:{r['request_id']} | T:{r['thread_id']} P:{r['part_no']} | {created} | {r['status']}\n"
                f"U:{r['user_id']} {r['username']}\n"
                f"Text: {txt}")
        lines.append(line)

    msg = "\n\n".join(lines)
    if len(msg) > 4000:
        for chunk in [msg[i:i+4000] for i in range(0, len(msg), 4000)]:
            await safe_send_message(context.bot, ADMIN_ID, chunk)
    else:
        await safe_send_message(context.bot, ADMIN_ID, msg)

async def clean_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await safe_send_message(context.bot, update.effective_user.id, "WTF you are not the owner man 😑")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes", callback_data=CB_CLEAN_YES),
         InlineKeyboardButton("No", callback_data=CB_CLEAN_NO)]
    ])
    await safe_send_message(context.bot, ADMIN_ID, "Are you sure you want to permanently delete ALL past requests? This cannot be undone.", reply_markup=kb)

async def clean_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    if not is_owner(q.from_user.id):
        return

    if q.data == CB_CLEAN_NO:
        await q.edit_message_text("Cancelled. Nothing was deleted.")
        return

    try:
        clean_requests()
        await q.edit_message_text("✅ Clean completed. All requests were deleted.")
    except Exception as e:
        logger.error(f"Error cleaning requests: {e}")
        await q.edit_message_text(f"❌ Error during clean: {str(e)}")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await safe_send_message(context.bot, update.effective_user.id, "WTF you are not the owner man 😑")
        return

    if not context.args:
        await safe_send_message(context.bot, ADMIN_ID, "Usage: /ban <user_id> [reason...]")
        return

    try:
        target_id = int(context.args[0])
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason"
        ban_user(target_id, reason)
        await safe_send_message(context.bot, ADMIN_ID, f"Banned {target_id}. Reason: {reason}")
    except ValueError:
        await safe_send_message(context.bot, ADMIN_ID, "Invalid user ID.")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await safe_send_message(context.bot, update.effective_user.id, "WTF you are not the owner man 😑")
        return

    if not context.args:
        await safe_send_message(context.bot, ADMIN_ID, "Usage: /unban <user_id>")
        return

    try:
        target_id = int(context.args[0])
        unban_user(target_id)
        await safe_send_message(context.bot, ADMIN_ID, f"Unbanned {target_id}.")
    except ValueError:
        await safe_send_message(context.bot, ADMIN_ID, "Invalid user ID.")

async def banned_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await safe_send_message(context.bot, update.effective_user.id, "WTF you are not the owner man 😑")
        return

    bans = list_bans()
    if not bans:
        await safe_send_message(context.bot, ADMIN_ID, "No banned users.")
        return

    lines = []
    for b in bans:
        b_at = b.banned_at.strftime("%Y-%m-%d %H:%M") if b.banned_at else "N/A"
        lines.append(f"{b.user_id} | {b_at} | {b.reason}")

    msg = "\n".join(lines)
    await safe_send_message(context.bot, ADMIN_ID, msg)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await safe_send_message(context.bot, update.effective_user.id, "WTF you are not the owner man 😑")
        return
    await render_status_page(update, 0)

async def render_status_page(update_or_query, page: int):
    users, total = get_paginated_users(page)
    
    buttons = []
    # 2 buttons per row
    for i in range(0, len(users), 2):
        row = []
        for user in users[i:i+2]:
            label = f"👉{user.username}" if user.username else f"👉(no username) - {user.first_name}"
            row.append(InlineKeyboardButton(label, callback_data=f"{CB_STAT_USER}{user.user_id}:{page}"))
        buttons.append(row)

    # Nav buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{CB_STAT_PAGE}{page-1}"))
    
    if (page + 1) * 20 < total:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"{CB_STAT_PAGE}{page+1}"))
    
    if nav_row:
        buttons.append(nav_row)
    
    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"{CB_STAT_REFRESH}{page}")])
    
    kb = InlineKeyboardMarkup(buttons)
    text = f"<b>👤 User List (Page {page + 1})</b>\nTotal Users: {total - 1 if total > 0 else 0}"

    try:
        if isinstance(update_or_query, Update):
            await safe_send_message(update_or_query.get_bot(), ADMIN_ID, text, reply_markup=kb, parse_mode="HTML")
        else:
            await update_or_query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramError as e:
        logger.error(f"Error rendering status page: {e}")

async def render_user_detail(q, user_id: int, page: int):
    stats = get_user_stats(user_id)
    if not stats:
        await q.answer("User not found")
        return

    username_disp = html.escape(stats["username"]) if stats["username"] else "(no username)"
    detail_text = (
        f"<blockquote>"
        f"✨ {username_disp}\n"
        f"🆔 ID : <code>{stats['user_id']}</code>\n"
        f"📟 Requests : {stats['req_count']}\n"
        f"😕 Blocked : {'YES' if stats['is_banned'] else 'NO'}"
        f"</blockquote>"
    )
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"{CB_STAT_BACK}{page}")]])
    
    try:
        await q.edit_message_text(detail_text, reply_markup=kb, parse_mode="HTML")
    except TelegramError as e:
        logger.error(f"Error rendering user detail: {e}")

async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    
    if q.from_user.id != ADMIN_ID:
        await q.answer("Not allowed", show_alert=True)
        return

    await q.answer()
    user = q.from_user
    data = q.data or ""

    # Status Handlers
    if data.startswith(CB_STAT_PAGE) or data.startswith(CB_STAT_REFRESH):
        page = int(data.split(":")[1])
        await render_status_page(q, page)
        return

    if data.startswith(CB_STAT_USER):
        _, uid, page = data.split(":")
        await render_user_detail(q, int(uid), int(page))
        return

    if data.startswith(CB_STAT_BACK):
        page = int(data.split(":")[1])
        await render_status_page(q, page)
        return

    if data.startswith(CB_GRP_YES) or data.startswith(CB_GRP_RETRY):
        global ADMIN_GROUP_ID
        gid = int(data.split(":")[1])
        try:
            member = await context.bot.get_chat_member(gid, context.bot.id)
            if member.status in ["administrator", "creator"]:
                await q.edit_message_text("Done ✅")
                
                # Persistence
                set_setting("admin_group_id", str(gid))
                ADMIN_GROUP_ID = gid
                
                perms = (
                    "<b>Group Activated!</b>\n\n"
                    "Please ensure I have these permissions:\n"
                    "• Send Messages\n"
                    "• Delete Messages (for cleanup)"
                )
                await safe_send_message(context.bot, gid, perms, parse_mode="HTML")
            else:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("Retry ♻️", callback_data=f"{CB_GRP_RETRY}{gid}")]])
                await q.edit_message_text("Check again buddy after making me admin 🙄!", reply_markup=kb)
        except Exception as e:
            logger.error(f"Error checking group admin: {e}")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Retry ♻️", callback_data=f"{CB_GRP_RETRY}{gid}")]])
            await q.edit_message_text(f"Error: {html.escape(str(e))}\nCheck again buddy after making me admin 🙄!", reply_markup=kb)
        return

    dt = datetime.now()
    time_str = dt.strftime("%I:%M %p")
    date_str = dt.strftime("%d %B")
    year_str = dt.strftime("%Y")

    if data.startswith(CB_REPLY_PREFIX):
        rid = int(data.split(":", 1)[1])
        req = get_request(rid)
        if not req:
            await safe_send_message(context.bot, ADMIN_ID, "Request not found.")
            return
        
        target_user_id = int(req["user_id"])
        if get_ban(target_user_id):
            await safe_send_message(context.bot, ADMIN_ID, "Cannot reply: user is banned.")
            return

        ADMIN_WAITING_REPLY_FOR[ADMIN_ID] = rid
        await safe_send_message(context.bot, ADMIN_ID, t(ADMIN_ID, "admin_reply_prompt"))

        # Update admin message
        status_stamp = (
            "\n\n✨Request has been replied ✅\n"
            f"👉Time ⏲️ : {time_str}\n"
            f"👉Date 📅 : {date_str}\n"
            f"👉Year 🧧 : {year_str}"
        )
        try:
            await q.edit_message_text(
                text=q.message.text_html + status_stamp,
                parse_mode="HTML",
                reply_markup=None
            )
        except TelegramError as e:
            logger.error(f"Error stamping reply: {e}")
        return

    if data.startswith(CB_REJECT_PREFIX):
        rid = int(data.split(":", 1)[1])
        req = get_request(rid)
        if not req:
            await safe_send_message(context.bot, ADMIN_ID, "Request not found.")
            return
        target_user_id = int(req["user_id"])
        
        # Clean reject message
        reject_msg = (
            "✨ Your request has been denied ❌\n\n"
            "You can send a new message after the cooldown."
        )
        if not get_ban(target_user_id):
            await safe_send_message(context.bot, target_user_id, reject_msg)
        
        set_request_status(rid, "rejected")
        await safe_send_message(context.bot, ADMIN_ID, t(ADMIN_ID, "admin_rejected"))

        # Update admin message
        status_stamp = (
            "\n\n✨Request denied ❌\n"
            f"👉Time ⏰ : {time_str}\n"
            f"👉Date 📅 : {date_str}\n"
            f"👉Year 🎊 : {year_str}"
        )
        try:
            await q.edit_message_text(
                text=q.message.text_html + status_stamp,
                parse_mode="HTML",
                reply_markup=None
            )
        except TelegramError as e:
            logger.error(f"Error stamping rejection: {e}")
        return

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("owner", owner_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("clean", clean_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("banned", banned_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("addgrp", addgrp_cmd))
    app.add_handler(CommandHandler("chatid", chatid_cmd))

    app.add_handler(CallbackQueryHandler(on_lang_callback, pattern=r"^LANG:"))
    app.add_handler(CallbackQueryHandler(on_confirm_callback, pattern=r"^(C_Y|C_N)$"))
    app.add_handler(CallbackQueryHandler(clean_callback, pattern=r"^(CLN_Y|CLN_N)$"))
    app.add_handler(CallbackQueryHandler(admin_buttons)) # Catch-all for status and other buttons

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, user_or_admin_text))

    # Production Detection Logic
    APP_URL = os.environ.get("APP_URL")
    # For Replit, we must use port 5000 for webview exposure,
    # as configured in the Replit environment.
    PORT = int(os.environ.get("PORT", "5000"))

    if APP_URL:
        # Webhook Mode
        logger.info(f"Starting in WEBHOOK mode on port {PORT}")
        # Replit needs the app to bind to 0.0.0.0 and the PORT environment variable
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="telegram",
            webhook_url=f"{APP_URL}/telegram",
            # This ensures the bot waits for the webhook to be set before continuing
            # and helps Replit detect the open port during the health check.
            drop_pending_updates=True
        )
    else:
        # Polling Mode
        logger.info("Starting in POLLING mode")
        # Ensure we bind to a port even in polling mode for Replit health checks
        # We'll start a small health check server in the background
        from threading import Thread
        from http.server import HTTPServer, BaseHTTPRequestHandler

        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            def log_message(self, format, *args): return

        def run_health_server():
            server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
            server.serve_forever()

        Thread(target=run_health_server, daemon=True).start()
        logger.info(f"Health check server started on port {PORT}")
        app.run_polling()

if __name__ == "__main__":
    main()
