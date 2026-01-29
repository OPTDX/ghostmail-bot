import os
import json
import asyncio
import secrets
import string
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Set, Any, List
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# GhostMail ✉️ — Render Web Service (Webhook) + Health Page
#
# Endpoints:
#   GET  /        -> GhostMail Bot is running ✅
#   GET  /health  -> GhostMail Bot is running ✅
#   POST /telegram -> Telegram webhook updates
#
# Requirements.txt:
#   python-telegram-bot[webhooks]==21.6
#   aiohttp
# ============================================================

MAILTM_BASE = "https://api.mail.tm"
STATE_FILE = "state.json"
USERS_FILE = "users.json"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8243001035"))

CHANNEL1 = os.environ.get("CHANNEL1", "-1003527524127").strip()
CHANNEL2 = os.environ.get("CHANNEL2", "-1003886262740").strip()
CHANNEL1_URL = os.environ.get("CHANNEL1_URL", "https://t.me/+GD6Z749osJhkOWE1").strip()
CHANNEL2_URL = os.environ.get("CHANNEL2_URL", "https://t.me/+lwO9-J-si8dkODFl").strip()

RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()  # https://your-app.onrender.com
PORT = int(os.environ.get("PORT", "10000"))

ENABLE_NOTIFIER = os.environ.get("ENABLE_NOTIFIER", "0").strip() == "1"
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))

# ---------------- MarkdownV2 helpers ----------------
MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"

def mdv2_escape(text: str) -> str:
    if text is None:
        return ""
    return "".join("\\" + c if c in MDV2_SPECIAL else c for c in text)

def to_blockquote(text: str) -> str:
    lines = (text or "").splitlines() or [""]
    lines = lines[:1200]
    return "\n".join([f"> {l}" if l.strip() else ">" for l in lines])

# ---------------- State ----------------
@dataclass
class Inbox:
    address: str
    password: str
    token: str
    seen_message_ids: Set[str]

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["seen_message_ids"] = list(self.seen_message_ids)
        return d

    @staticmethod
    def from_json(d: Dict[str, Any]) -> "Inbox":
        return Inbox(
            address=d["address"],
            password=d["password"],
            token=d.get("token", ""),
            seen_message_ids=set(d.get("seen_message_ids", [])),
        )

STATE: Dict[str, Inbox] = {}
USERS: Dict[str, Dict[str, Any]] = {}

def load_state() -> None:
    global STATE
    if not os.path.exists(STATE_FILE):
        STATE = {}
        return
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    STATE = {k: Inbox.from_json(v) for k, v in raw.items()}

def save_state() -> None:
    raw = {k: v.to_json() for k, v in STATE.items()}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

def load_users() -> None:
    global USERS
    if not os.path.exists(USERS_FILE):
        USERS = {}
        return
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        USERS = json.load(f)

def save_users() -> None:
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(USERS, f, ensure_ascii=False, indent=2)

def get_inbox(user_id: int) -> Optional[Inbox]:
    return STATE.get(str(user_id))

def upsert_user(update: Update, verified: Optional[bool] = None) -> None:
    u = update.effective_user
    if not u:
        return
    uid = str(u.id)
    name = ((u.first_name or "") + " " + (u.last_name or "")).strip()
    username = u.username or ""
    info = USERS.get(uid, {})
    if "first_seen" not in info:
        info["first_seen"] = datetime.now(timezone.utc).isoformat()
    info["last_seen"] = datetime.now(timezone.utc).isoformat()
    info["name"] = name
    info["username"] = username
    if verified is not None:
        info["verified"] = bool(verified)
    USERS[uid] = info
    save_users()

def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)

# ---------------- UI (persistent menu + clean DM) ----------------
def user_menu(has_inbox: bool) -> ReplyKeyboardMarkup:
    if not has_inbox:
        kb = [[KeyboardButton("New Email 📨")]]
    else:
        kb = [
            [KeyboardButton("Inbox 📥"), KeyboardButton("Delete 🚫")],
            [KeyboardButton("Copy Email 📋")],
        ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, is_persistent=True)

async def delete_user_message(update: Update) -> None:
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass

async def clean_last_bot_msg(chat_id: int, app: Application) -> None:
    last_id = USERS.get(str(chat_id), {}).get("last_bot_msg_id")
    if last_id:
        try:
            await app.bot.delete_message(chat_id=chat_id, message_id=int(last_id))
        except Exception:
            pass

async def send_clean(chat_id: int, app: Application, text: str, *, parse_mode=None, reply_markup=None, disable_preview=True):
    await clean_last_bot_msg(chat_id, app)
    msg = await app.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
        disable_web_page_preview=disable_preview,
    )
    USERS.setdefault(str(chat_id), {})
    USERS[str(chat_id)]["last_bot_msg_id"] = msg.message_id
    save_users()

# ---------------- mail.tm helpers ----------------
async def mailtm_get(session: aiohttp.ClientSession, path: str, token: Optional[str] = None, params: Optional[dict] = None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with session.get(MAILTM_BASE + path, headers=headers, params=params) as r:
        data = await r.json(content_type=None)
        if r.status >= 400:
            raise RuntimeError(f"GET {path} failed {r.status}: {data}")
        return data

async def mailtm_post(session: aiohttp.ClientSession, path: str, payload: dict, token: Optional[str] = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with session.post(MAILTM_BASE + path, headers=headers, json=payload) as r:
        data = await r.json(content_type=None)
        if r.status >= 400:
            raise RuntimeError(f"POST {path} failed {r.status}: {data}")
        return data

async def mailtm_delete(session: aiohttp.ClientSession, path: str, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    async with session.delete(MAILTM_BASE + path, headers=headers) as r:
        if r.status not in (200, 202, 204):
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = await r.text()
            raise RuntimeError(f"DELETE {path} failed {r.status}: {data}")

async def refresh_token(session: aiohttp.ClientSession, inbox: Inbox) -> None:
    tok = await mailtm_post(session, "/token", {"address": inbox.address, "password": inbox.password})
    inbox.token = tok["token"]

async def safe_get(session: aiohttp.ClientSession, inbox: Inbox, path: str, params: Optional[dict] = None):
    try:
        return await mailtm_get(session, path, token=inbox.token, params=params)
    except Exception:
        await refresh_token(session, inbox)
        return await mailtm_get(session, path, token=inbox.token, params=params)

async def safe_delete(session: aiohttp.ClientSession, inbox: Inbox, path: str):
    try:
        await mailtm_delete(session, path, token=inbox.token)
    except Exception:
        await refresh_token(session, inbox)
        await mailtm_delete(session, path, token=inbox.token)

async def create_inbox(session: aiohttp.ClientSession) -> Inbox:
    domains = await mailtm_get(session, "/domains")
    items = domains.get("hydra:member", [])
    if not items:
        raise RuntimeError("No domains returned by mail.tm")
    domain = items[0]["domain"]

    local = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    address = f"{local}@{domain}"
    password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))

    await mailtm_post(session, "/accounts", {"address": address, "password": password})
    tok = await mailtm_post(session, "/token", {"address": address, "password": password})
    return Inbox(address=address, password=password, token=tok["token"], seen_message_ids=set())

async def list_messages(session: aiohttp.ClientSession, inbox: Inbox) -> List[dict]:
    data = await safe_get(session, inbox, "/messages", params={"page": 1})
    return data.get("hydra:member", [])

async def get_message(session: aiohttp.ClientSession, inbox: Inbox, msg_id: str) -> dict:
    return await safe_get(session, inbox, f"/messages/{msg_id}")

async def get_message_source(session: aiohttp.ClientSession, inbox: Inbox, msg_id: str) -> str:
    data = await safe_get(session, inbox, f"/sources/{msg_id}")
    return data.get("data", "")

# ---------------- Force-join gate ----------------
def gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Shein Loot 🎉", url=CHANNEL1_URL),
         InlineKeyboardButton("RageByte ⚡", url=CHANNEL2_URL)],
        [InlineKeyboardButton("Verify ✅", callback_data="verify_join")]
    ])

async def is_member(app: Application, user_id: int, chat_id: str) -> bool:
    try:
        cm = await app.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return cm.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

async def joined_both(app: Application, user_id: int) -> bool:
    return (await is_member(app, user_id, CHANNEL1)) and (await is_member(app, user_id, CHANNEL2))

async def show_gate(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool):
    u = update.effective_user
    profile_name = (((u.first_name or "") + " " + (u.last_name or "")).strip() or "there")
    text = f"Welcome {profile_name} 👋\n\nYou must join the two channels below to use this bot."

    if edit and update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=gate_keyboard())
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=gate_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=gate_keyboard())

async def require_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    ok = await joined_both(context.application, update.effective_user.id)
    upsert_user(update, verified=ok)
    if ok:
        return True
    await show_gate(update, context, edit=bool(update.callback_query))
    return False

async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ok = await joined_both(context.application, q.from_user.id)
    upsert_user(update, verified=ok)

    if not ok:
        await q.message.edit_text(
            "❌ Not verified yet.\n\nJoin both channels then press **Verify ✅** again.",
            reply_markup=gate_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    has_inbox = bool(get_inbox(q.from_user.id))
    await q.message.edit_text("✅ **Verified!**", parse_mode=ParseMode.MARKDOWN)
    await send_clean(
        chat_id=q.from_user.id,
        app=context.application,
        text="Use the menu buttons below 👇",
        reply_markup=user_menu(has_inbox=has_inbox),
    )

# ---------------- User actions ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return
    has_inbox = bool(get_inbox(update.effective_user.id))
    await send_clean(update.effective_user.id, context.application,
                     "Welcome to GhostMail ✉️\n\nUse the menu buttons below 👇",
                     reply_markup=user_menu(has_inbox=has_inbox))
    await delete_user_message(update)

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return
    await delete_user_message(update)
    user_id = update.effective_user.id

    if get_inbox(user_id):
        await send_clean(user_id, context.application,
                         "You already have a temp email.\n\nTap Inbox 📥 or Delete 🚫.",
                         reply_markup=user_menu(True))
        return

    await send_clean(user_id, context.application, "Creating your temp email…", reply_markup=user_menu(False))

    async with aiohttp.ClientSession() as session:
        inbox = await create_inbox(session)

    STATE[str(user_id)] = inbox
    save_state()

    await send_clean(user_id, context.application,
                     f"✅ Your temp email:\n\n{inbox.address}\n\nTap and hold to copy.\nUse Inbox 📥 to check mail.",
                     reply_markup=user_menu(True))

async def copy_email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return
    await delete_user_message(update)
    inbox = get_inbox(update.effective_user.id)
    if not inbox:
        await send_clean(update.effective_user.id, context.application,
                         "No temp email yet. Tap New Email 📨",
                         reply_markup=user_menu(False))
        return
    await send_clean(update.effective_user.id, context.application,
                     f"📋 Your temp email:\n\n{inbox.address}\n\nTap and hold to copy.",
                     reply_markup=user_menu(True))

async def inbox_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return
    await delete_user_message(update)
    user_id = update.effective_user.id
    inbox = get_inbox(user_id)

    if not inbox:
        await send_clean(user_id, context.application, "No temp email yet. Tap New Email 📨", reply_markup=user_menu(False))
        return

    async with aiohttp.ClientSession() as session:
        msgs = await list_messages(session, inbox)

        if not msgs:
            await send_clean(user_id, context.application, "Inbox is empty.", reply_markup=user_menu(True))
            return

        newest = msgs[0]
        mid = newest.get("id")
        if not mid:
            await send_clean(user_id, context.application, "Could not read latest email.", reply_markup=user_menu(True))
            return

        if mid in inbox.seen_message_ids:
            await send_clean(user_id, context.application, "No new email yet.", reply_markup=user_menu(True))
        else:
            full = await get_message(session, inbox, mid)
            subject = full.get("subject") or "(no subject)"
            from_obj = full.get("from") or {}
            from_addr = from_obj.get("address") or "unknown"
            from_name = from_obj.get("name") or ""
            from_line = f"{from_name} <{from_addr}>" if from_name else f"<{from_addr}>"

            body = (full.get("text") or "").strip()
            if not body:
                try:
                    body = (await get_message_source(session, inbox, mid)).strip()
                except Exception:
                    body = "(no body)"

            text = (
                f"*From:* {mdv2_escape(from_line)}\n"
                f"*{mdv2_escape(subject)}*\n"
                f"{to_blockquote(mdv2_escape(body))}"
            )

            await send_clean(
                user_id,
                context.application,
                text=text[:3800],
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=user_menu(True),
                disable_preview=True
            )

            inbox.seen_message_ids.add(mid)

        # delete all emails on server to keep clean
        for m in msgs:
            did = m.get("id")
            if did:
                try:
                    await safe_delete(session, inbox, f"/messages/{did}")
                except Exception:
                    pass

    save_state()

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return
    await delete_user_message(update)
    user_id = update.effective_user.id

    if str(user_id) not in STATE:
        await send_clean(user_id, context.application, "No temp email to delete.", reply_markup=user_menu(False))
        return

    STATE.pop(str(user_id), None)
    save_state()

    await send_clean(user_id, context.application, "🗑️ Deleted your temp email.\nTap New Email 📨 to create another.",
                     reply_markup=user_menu(False))

# ---------------- Menu router ----------------
async def menu_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "New Email 📨":
        return await new_cmd(update, context)
    if text == "Inbox 📥":
        return await inbox_cmd(update, context)
    if text == "Delete 🚫":
        return await delete_cmd(update, context)
    if text == "Copy Email 📋":
        return await copy_email_cmd(update, context)

    await delete_user_message(update)
    await send_clean(update.effective_user.id, context.application, "Use the menu buttons below 👇",
                     reply_markup=user_menu(bool(get_inbox(update.effective_user.id))))

# ---------------- Admin-only ----------------
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await delete_user_message(update)

    total = len(USERS)
    verified = sum(1 for v in USERS.values() if v.get("verified"))

    lines = []
    for uid, info in list(USERS.items())[:100]:
        name = info.get("name") or ""
        username = info.get("username") or ""
        tag = f"@{username}" if username else "(no username)"
        vmark = "✅" if info.get("verified") else "❌"
        lines.append(f"{vmark} {name} — {tag} — {uid}")

    msg = (
        f"📊 *Stats*\n"
        f"Total users: *{total}*\n"
        f"Verified users: *{verified}*\n\n"
        f"*Users (sample up to 100):*\n" + "\n".join(lines or ["(empty)"])
    )

    await send_clean(update.effective_user.id, context.application, msg, parse_mode=ParseMode.MARKDOWN)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await delete_user_message(update)

    text = " ".join(context.args).strip() if context.args else ""
    recipients = [int(uid) for uid, info in USERS.items() if info.get("verified")]

    if not recipients:
        await send_clean(update.effective_user.id, context.application, "No verified users to broadcast to.")
        return

    if not text and not (update.message and update.message.reply_to_message):
        await send_clean(update.effective_user.id, context.application,
                         "Usage:\n/broadcast <message>\nOR reply to a message then send /broadcast")
        return

    await send_clean(update.effective_user.id, context.application, f"📣 Broadcasting to {len(recipients)} verified users…")

    sent = 0
    failed = 0
    for uid in recipients:
        try:
            if text:
                await context.application.bot.send_message(chat_id=uid, text=text, disable_web_page_preview=True)
            else:
                await update.message.reply_to_message.copy(chat_id=uid)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await send_clean(update.effective_user.id, context.application, f"✅ Done.\nSent: {sent}\nFailed: {failed}")

# ---------------- Optional notifier ----------------
async def notify_newest(user_id: int, app: Application):
    inbox = get_inbox(user_id)
    if not inbox:
        return
    async with aiohttp.ClientSession() as session:
        msgs = await list_messages(session, inbox)
        if not msgs:
            return
        newest = msgs[0]
        mid = newest.get("id")
        if not mid or mid in inbox.seen_message_ids:
            return

        full = await get_message(session, inbox, mid)
        subject = full.get("subject") or "(no subject)"
        from_obj = full.get("from") or {}
        from_addr = from_obj.get("address") or "unknown"
        from_name = from_obj.get("name") or ""
        from_line = f"{from_name} <{from_addr}>" if from_name else f"<{from_addr}>"

        body = (full.get("text") or "").strip()
        if not body:
            body = (await get_message_source(session, inbox, mid)).strip() or "(no body)"

        text = (
            f"*From:* {mdv2_escape(from_line)}\n"
            f"*{mdv2_escape(subject)}*\n"
            f"{to_blockquote(mdv2_escape(body))}"
        )

        await send_clean(user_id, app, text=text[:3800], parse_mode=ParseMode.MARKDOWN_V2, reply_markup=user_menu(True))
        inbox.seen_message_ids.add(mid)

        for m in msgs:
            did = m.get("id")
            if did:
                try:
                    await safe_delete(session, inbox, f"/messages/{did}")
                except Exception:
                    pass
    save_state()

async def poll_loop(app: Application):
    while True:
        try:
            for uid_str in list(STATE.keys()):
                if not USERS.get(uid_str, {}).get("verified"):
                    continue
                await notify_newest(int(uid_str), app)
        except Exception:
            pass
        await asyncio.sleep(POLL_SECONDS)

# ---------------- Aiohttp handlers ----------------
async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text="GhostMail Bot is running ✅", content_type="text/plain")

async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="GhostMail Bot is running ✅", content_type="text/plain")

async def handle_telegram(request: web.Request) -> web.Response:
    # Telegram sends JSON update
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad Request")

    application: Application = request.app["ptb_app"]
    update = Update.de_json(data, application.bot)

    # process update
    await application.process_update(update)
    return web.Response(text="OK")

# ---------------- Main ----------------
async def run() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var is missing")
    if not RENDER_EXTERNAL_URL:
        raise SystemExit("RENDER_EXTERNAL_URL env var is missing (set it to your Render public URL)")

    load_state()
    load_users()

    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_join$"))
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("new", new_cmd))
    application.add_handler(CommandHandler("inbox", inbox_cmd))
    application.add_handler(CommandHandler("delete", delete_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_router))

    # Start PTB
    await application.initialize()
    await application.start()

    # Set webhook
    await application.bot.set_webhook(f"{RENDER_EXTERNAL_URL}/telegram")

    if ENABLE_NOTIFIER:
        application.create_task(poll_loop(application))

    # Aiohttp server
    aio = web.Application()
    aio["ptb_app"] = application
    aio.router.add_get("/", handle_root)
    aio.router.add_get("/health", handle_health)
    aio.router.add_post("/telegram", handle_telegram)

    runner = web.AppRunner(aio)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    # Keep running forever
    while True:
        await asyncio.sleep(3600)

def main():
    asyncio.run(run())

if __name__ == "__main__":
    main()
