import os
import json
import asyncio
import secrets
import string
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Set, Any, List
from datetime import datetime, timezone

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

MAILTM_BASE = "https://api.mail.tm"

STATE_FILE = "state.json"     # stores per-user active inbox
USERS_FILE = "users.json"     # stores user registry for stats/broadcast

POLL_SECONDS = 12  # optional notifier polling interval (gentle)

# ✅ Admin (you only)
ADMIN_ID = 8243001035

# ✅ Force-join channels (IDs for verification)
CHANNEL1 = "-1003527524127"  # Shein Loot 🎉
CHANNEL2 = "-1003886262740"  # RageByte ⚡

# ✅ Join buttons (invite links)
CHANNEL1_URL = "https://t.me/+GD6Z749osJhkOWE1"
CHANNEL2_URL = "https://t.me/+lwO9-J-si8dkODFl"


# ---------------- MarkdownV2 helpers ----------------
MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"

def mdv2_escape(text: str) -> str:
    if text is None:
        return ""
    out = []
    for ch in text:
        out.append("\\" + ch if ch in MDV2_SPECIAL else ch)
    return "".join(out)

def to_blockquote(text: str) -> str:
    lines = (text or "").splitlines() or [""]
    lines = lines[:1200]
    return "\n".join([f"> {line}" if line.strip() else ">" for line in lines])


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

# user_id -> Inbox (single active inbox only)
STATE: Dict[str, Inbox] = {}

# user_id -> {name, username, verified, first_seen, last_seen}
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


def upsert_user(update: Update, verified: Optional[bool] = None):
    u = update.effective_user
    if not u:
        return

    uid = str(u.id)
    first = u.first_name or ""
    last = u.last_name or ""
    name = (first + " " + last).strip()
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


# ---------------- Force join (clean edits) ----------------
def gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Shein Loot 🎉", url=CHANNEL1_URL),
            InlineKeyboardButton("RageByte ⚡", url=CHANNEL2_URL),
        ],
        [InlineKeyboardButton("Verify ✅", callback_data="verify_join")]
    ])

async def is_member(app: Application, user_id: int, chat_id: str) -> bool:
    try:
        cm = await app.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return cm.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

async def joined_both(app: Application, user_id: int) -> bool:
    a = await is_member(app, user_id, CHANNEL1)
    b = await is_member(app, user_id, CHANNEL2)
    return a and b

async def show_gate(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool):
    first = update.effective_user.first_name or ""
    last = update.effective_user.last_name or ""
    profile_name = (first + " " + last).strip() or "there"

    text = (
        f"Welcome {profile_name} 👋\n\n"
        "You must join the two channels below to use this bot."
    )

    if edit and update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=gate_keyboard())
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

    await q.message.edit_text(
        "✅ **Verified!**\n\n"
        "Commands:\n"
        "/new – get a temp email\n"
        "/inbox – show latest email\n"
        "/delete – delete your temp email",
        parse_mode=ParseMode.MARKDOWN
    )


# ---------------- User commands (ONLY 3) ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Just show gate / or show minimal help
    if not await require_join(update, context):
        return
    await update.message.reply_text(
        "✅ You’re verified.\n\n"
        "Commands:\n"
        "/new – get a temp email\n"
        "/inbox – show latest email\n"
        "/delete – delete your temp email"
    )

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return

    user_id = update.effective_user.id
    await update.message.reply_text("Creating your temp email…")

    async with aiohttp.ClientSession() as session:
        inbox = await create_inbox(session)

    STATE[str(user_id)] = inbox
    save_state()

    await update.message.reply_text(
        f"✅ Your temp email:\n{inbox.address}\n\n"
        "Use /inbox to check latest mail."
    )

async def inbox_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return

    user_id = update.effective_user.id
    inbox = get_inbox(user_id)
    if not inbox:
        await update.message.reply_text("No temp email yet. Use /new")
        return

    async with aiohttp.ClientSession() as session:
        msgs = await list_messages(session, inbox)
        if not msgs:
            await update.message.reply_text("Inbox is empty.")
            return

        newest = msgs[0]
        mid = newest.get("id")
        if not mid:
            await update.message.reply_text("Could not read latest email.")
            return

        # Only newest triggers once
        if mid in inbox.seen_message_ids:
            await update.message.reply_text("No new email yet.")
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

            await update.message.reply_text(
                text[:3800],
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True
            )

            inbox.seen_message_ids.add(mid)

        # Auto-delete all emails (keeps inbox clean)
        for m in msgs:
            did = m.get("id")
            if not did:
                continue
            try:
                await safe_delete(session, inbox, f"/messages/{did}")
            except Exception:
                pass

    save_state()

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_join(update, context):
        return

    user_id = update.effective_user.id
    if str(user_id) not in STATE:
        await update.message.reply_text("No temp email to delete.")
        return

    STATE.pop(str(user_id), None)
    save_state()
    await update.message.reply_text("🗑️ Deleted your temp email. Use /new to create another.")


# ---------------- Admin-only commands ----------------
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    total = len(USERS)
    verified = sum(1 for v in USERS.values() if v.get("verified"))

    # Show up to 80 users (adjust if you want)
    lines = []
    for uid, info in list(USERS.items())[:80]:
        name = info.get("name") or ""
        username = info.get("username") or ""
        tag = f"@{username}" if username else "(no username)"
        vmark = "✅" if info.get("verified") else "❌"
        lines.append(f"{vmark} {name} — {tag} — {uid}")

    msg = (
        f"📊 *Stats*\n"
        f"Total users: *{total}*\n"
        f"Verified users: *{verified}*\n\n"
        f"*Users (sample up to 80):*\n" + "\n".join(lines or ["(empty)"])
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    text = " ".join(context.args).strip() if context.args else ""

    recipients = [int(uid) for uid, info in USERS.items() if info.get("verified")]
    if not recipients:
        await update.message.reply_text("No verified users to broadcast to.")
        return

    if not text and not update.message.reply_to_message:
        await update.message.reply_text(
            "Usage:\n"
            "/broadcast <message>\n"
            "OR reply to a message and send /broadcast"
        )
        return

    await update.message.reply_text(f"📣 Broadcasting to {len(recipients)} verified users…")

    sent = 0
    failed = 0

    for uid in recipients:
        try:
            if text:
                await context.application.bot.send_message(
                    chat_id=uid,
                    text=text,
                    disable_web_page_preview=True
                )
            else:
                # copy keeps it clean (no “forwarded from” header)
                await update.message.reply_to_message.copy(chat_id=uid)

            sent += 1
            await asyncio.sleep(0.05)  # reduce flood risk
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ Done.\nSent: {sent}\nFailed: {failed}")


# ---------------- Optional notifier (newest-only) ----------------
async def check_newest_and_notify(user_id: int, app: Application):
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

        await app.bot.send_message(
            chat_id=user_id,
            text=text[:3800],
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True
        )

        inbox.seen_message_ids.add(mid)

        # auto-delete all
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
                uid = int(uid_str)
                # only notify verified users
                info = USERS.get(uid_str, {})
                if not info.get("verified"):
                    continue
                await check_newest_and_notify(uid, app)
        except Exception:
            pass
        await asyncio.sleep(POLL_SECONDS)


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set BOT_TOKEN env var")

    load_state()
    load_users()

    app = Application.builder().token(token).build()

    # User
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("inbox", inbox_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))

    # Force join
    app.add_handler(CallbackQueryHandler(verify_callback, pattern="^verify_join$"))

    # Admin-only
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    async def on_start(app_: Application):
        # If you want ONLY manual /inbox and NO auto notifications, comment next line.
        app_.create_task(poll_loop(app_))

    app.post_init = on_start
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
