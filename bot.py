"""
Ujala Happy Pack Telegram Bot
Multi-user, per-session, with referral system and admin panel.

FIXES applied:
- asyncio.get_event_loop() → asyncio.get_running_loop()  (Python 3.10+ safe)
- Shared ThreadPoolExecutor with MAX_WORKERS limit so 100 concurrent users
  don't spawn unlimited threads.
- Per-user asyncio.Lock prevents duplicate submissions if a user taps the
  button twice quickly.
- Cleaner error messages with retry hints shown to the user.
"""

import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

import database as db
import ujala_api as api
from config import (
    TELEGRAM_BOT_TOKEN, ADMIN_IDS, REQUIRED_CHANNELS,
    REQUIRED_CHANNEL_LINKS, PACK_IMAGE_PATH,
    POINTS_PER_REFERRAL, MAX_WORKERS
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Shared thread pool — limits concurrent Ujala API calls for all users
_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# Per-user lock registry — prevents double-submission from fast double-taps
_user_locks: dict[int, asyncio.Lock] = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _user_locks:
        _user_locks[user_id] = asyncio.Lock()
    return _user_locks[user_id]


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def check_channels(bot, user_id: int) -> list:
    """Return list of channel usernames the user has NOT joined."""
    not_joined = []
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(
    f"@{channel}" if isinstance(channel, str) else channel,
    user_id
)
            if member.status in (ChatMember.LEFT, ChatMember.BANNED, "kicked", "left"):
                not_joined.append(channel)
        except TelegramError:
            not_joined.append(channel)
    return not_joined


def channel_join_keyboard():
    buttons = []
    for link in REQUIRED_CHANNEL_LINKS:
        name = link.split("/")[-1]
        buttons.append([InlineKeyboardButton(f"📢 Join @{name}", url=link)])
    buttons.append([InlineKeyboardButton("✅ Done! I Joined Both Channels", callback_data="check_joined")])
    return InlineKeyboardMarkup(buttons)


def main_menu_keyboard(user_id: int):
    buttons = [
        [InlineKeyboardButton("🎯 Submit Number & Earn", callback_data="submit_number")],
        [
            InlineKeyboardButton("💰 My Points", callback_data="my_points"),
            InlineKeyboardButton("🤝 Invite Friends", callback_data="referral"),
        ],
        [InlineKeyboardButton("📖 How It Works", callback_data="how_it_works")],
    ]
    if is_admin(user_id):
        buttons.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)


def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")]])


def cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]])


def otp_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Resend OTP", callback_data="resend_otp")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
    ])


async def send_or_edit(update: Update, text: str, keyboard=None, parse_mode=ParseMode.HTML):
    kwargs = {"text": text, "parse_mode": parse_mode}
    if keyboard:
        kwargs["reply_markup"] = keyboard
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(**kwargs)
        except TelegramError:
            await update.callback_query.message.reply_text(**kwargs)
    else:
        await update.message.reply_text(**kwargs)


# ── Channel gate ──────────────────────────────────────────────────────────────

async def ensure_joined(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if user has joined all channels. Sends prompt if not."""
    user_id = update.effective_user.id
    not_joined = await check_channels(context.bot, user_id)
    if not_joined:
        channels_text = "\n".join(f"   📌 @{c}" for c in not_joined)
        await send_or_edit(
            update,
            f"🔔 <b>Almost there!</b>\n\n"
            f"You need to join our channels first:\n\n"
            f"{channels_text}\n\n"
            f"👇 Click below to join, then tap <b>Done!</b>",
            keyboard=channel_join_keyboard(),
        )
        return False
    return True


# ── /start handler ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or ""
    first_name = user.first_name or "Friend"

    # Parse referral
    referred_by = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referred_by = int(arg[4:])
                if referred_by == user_id:
                    referred_by = None
            except ValueError:
                referred_by = None

    existing = await db.get_user(user_id)
    is_new = existing is None

    if is_new:
        await db.create_user(user_id, username, first_name, referred_by)
        await db.upsert_session(user_id)

        if referred_by:
            referrer = await db.get_user(referred_by)
            if referrer and not referrer.get("is_banned"):
                new_pts = await db.update_points(referred_by, POINTS_PER_REFERRAL)
                try:
                    await context.bot.send_message(
                        referred_by,
                        f"🎉 <b>Referral Bonus!</b>\n\n"
                        f"🙋 <b>{first_name}</b> just joined using your link!\n"
                        f"💰 You earned <b>+{POINTS_PER_REFERRAL} points!</b>\n"
                        f"🏆 Your total: <b>{new_pts} points</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramError:
                    pass

    user_data = await db.get_user(user_id)
    if user_data and user_data.get("is_banned"):
        await update.message.reply_text(
            "🚫 <b>Access Denied</b>\n\nYou have been banned from using this bot.\nContact admin if you think this is a mistake.",
            parse_mode=ParseMode.HTML,
        )
        return

    not_joined = await check_channels(context.bot, user_id)
    if not_joined:
        channels_text = "\n".join(f"   📌 @{c}" for c in not_joined)
        await update.message.reply_text(
            f"👋 <b>Welcome to Ujala Happy Pack Bot!</b>\n\n"
            f"🎁 Submit mobile numbers & earn points!\n"
            f"🤝 Invite friends & earn even more!\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📋 <b>Step 1:</b> Join our channels:\n\n"
            f"{channels_text}\n\n"
            f"📋 <b>Step 2:</b> Tap <b>Done!</b> below ✅\n"
            f"━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML,
            reply_markup=channel_join_keyboard(),
        )
        return

    points = user_data.get("points", 0)
    ref_count = await db.get_referral_count(user_id)

    if is_new:
        greet = (
            f"🎉 <b>Welcome, {first_name}!</b>\n\n"
            f"You're all set! Here's what you can do:\n\n"
            f"🤝 <b>Invite friends</b> → Earn +{POINTS_PER_REFERRAL} points each\n"
            f"🎯 <b>Use points</b> → Submit numbers on Ujala\n\n"
        )
        if referred_by:
            greet += f"✨ You joined via a referral link — welcome aboard!\n\n"
    else:
        greet = (
            f"👋 <b>Welcome back, {first_name}!</b>\n\n"
            f"💰 Points: <b>{points}</b>\n"
            f"👥 Referrals: <b>{ref_count}</b>\n\n"
        )

    greet += f"👇 What would you like to do?"

    await update.message.reply_text(
        greet,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(user_id),
    )


# ── Callback query handler ────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    user_id = user.id

    user_data = await db.get_user(user_id)
    if user_data and user_data.get("is_banned"):
        await query.edit_message_text(
            "🚫 <b>Access Denied</b>\n\nYou have been banned.",
            parse_mode=ParseMode.HTML,
        )
        return

    data = query.data

    # ── Channel check ──────────────────────────────────────────────────────
    if data == "check_joined":
        not_joined = await check_channels(context.bot, user_id)
        if not_joined:
            channels_text = "\n".join(f"   📌 @{c}" for c in not_joined)
            await query.edit_message_text(
                f"⚠️ <b>Oops! Not joined yet</b>\n\n"
                f"You still need to join:\n\n{channels_text}\n\n"
                f"👇 Tap the channel button, join, then come back!",
                parse_mode=ParseMode.HTML,
                reply_markup=channel_join_keyboard(),
            )
        else:
            if not user_data:
                await db.create_user(user_id, user.username or "", user.first_name or "Friend")
                await db.upsert_session(user_id)
                user_data = await db.get_user(user_id)
            await query.edit_message_text(
                f"🎊 <b>All channels joined!</b>\n\n"
                f"Welcome to <b>Ujala Happy Pack Bot</b>!\n\n"
                f"🎯 Submit numbers → Earn points\n"
                f"🤝 Invite friends → Earn more points\n\n"
                f"💰 Your points: <b>{user_data.get('points', 0)}</b>\n\n"
                f"👇 Let's get started!",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(user_id),
            )
        return

    if not await ensure_joined(update, context):
        return

    # ── Main menu ──────────────────────────────────────────────────────────
    if data == "main_menu":
        user_data = await db.get_user(user_id)
        ref_count = await db.get_referral_count(user_id)
        await query.edit_message_text(
            f"🏠 <b>Main Menu</b>\n\n"
            f"👤 <b>{user.first_name}</b>\n"
            f"💰 Points: <b>{user_data.get('points', 0)}</b>  |  👥 Referrals: <b>{ref_count}</b>\n\n"
            f"👇 What do you want to do?",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(user_id),
        )

    # ── Submit number ──────────────────────────────────────────────────────
    elif data == "submit_number":
        if not os.path.isfile(PACK_IMAGE_PATH):
            await query.edit_message_text(
                "⚠️ <b>Bot is under maintenance</b>\n\n"
                "The admin needs to upload the pack image to activate this feature.\n"
                "Please try again later or contact the admin!",
                parse_mode=ParseMode.HTML,
                reply_markup=back_keyboard(),
            )
            return
        user_data = await db.get_user(user_id)
        pts_now = user_data.get("points", 0)
        if pts_now < 1:
            bot_username = (await context.bot.get_me()).username
            ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
            await query.edit_message_text(
                f"🚫 <b>Not Enough Points!</b>\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🎯 Scanning a number costs <b>1 Point</b>\n"
                f"💎 Your Points: <b>{pts_now}</b>\n"
                f"━━━━━━━━━━━━━━━━\n\n"
                f"📢 <b>How to get Points?</b>\n"
                f"Invite friends using your link → earn <b>+{POINTS_PER_REFERRAL} points per friend!</b>\n\n"
                f"🔗 <b>Your invite link:</b>\n"
                f"<code>{ref_link}</code>\n\n"
                f"<i>Share it on WhatsApp, Instagram, anywhere!</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🤝 Invite Friends", callback_data="referral")],
                    [InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")],
                ]),
            )
            return
        await db.upsert_session(user_id, state="waiting_phone")
        await query.edit_message_text(
            f"📱 <b>Submit a Number</b>\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📝 <b>Step 1 of 2:</b> Enter Phone Number\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"Type a <b>10-digit Indian mobile number</b> below 👇\n\n"
            f"<i>Example: 9876543210</i>\n\n"
            f"🎯 Cost: <b>1 Point</b> | Your balance: <b>{pts_now}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_keyboard(),
        )

    # ── My points ──────────────────────────────────────────────────────────
    elif data == "my_points":
        user_data = await db.get_user(user_id)
        ref_count = await db.get_referral_count(user_id)
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        pts = user_data.get("points", 0)

        if pts >= 50:
            badge = "👑 Legend"
        elif pts >= 20:
            badge = "🥇 Gold"
        elif pts >= 10:
            badge = "🥈 Silver"
        elif pts >= 5:
            badge = "🥉 Bronze"
        else:
            badge = "🌱 Starter"

        await query.edit_message_text(
            f"💰 <b>Your Points</b>\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🏆 Rank: <b>{badge}</b>\n"
            f"💎 Total Points: <b>{pts}</b>\n"
            f"👥 Friends Invited: <b>{ref_count}</b>\n"
            f"📅 Member Since: {user_data.get('join_date', 'N/A')[:10]}\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"<b>💡 How to earn points:</b>\n"
            f"🤝 Invite a friend → +{POINTS_PER_REFERRAL} points\n"
            f"🎁 Admin bonus → admin can add points anytime\n\n"
            f"🔗 <b>Your invite link:</b>\n"
            f"<code>{ref_link}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
        )

    # ── Referral ───────────────────────────────────────────────────────────
    elif data == "referral":
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        ref_count = await db.get_referral_count(user_id)
        earned = ref_count * POINTS_PER_REFERRAL
        await query.edit_message_text(
            f"🤝 <b>Invite Friends & Earn!</b>\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"👥 Friends Invited: <b>{ref_count}</b>\n"
            f"💰 Earned from Referrals: <b>{earned} points</b>\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"🎁 <b>You earn +{POINTS_PER_REFERRAL} points</b> every time someone joins using your link!\n\n"
            f"📲 <b>Share this link with friends:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"<i>💡 Tap the link above to copy it, then share on WhatsApp, Instagram or anywhere!</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
        )

    # ── How it works ───────────────────────────────────────────────────────
    elif data == "how_it_works":
        await query.edit_message_text(
            f"📖 <b>How It Works</b>\n\n"
            f"1️⃣ Invite friends → Earn <b>+{POINTS_PER_REFERRAL} points</b> per friend\n"
            f"2️⃣ Tap <b>Submit Number</b> (costs 1 point)\n"
            f"3️⃣ Enter a <b>10-digit mobile number</b>\n"
            f"4️⃣ The bot registers on the Ujala website\n"
            f"5️⃣ An <b>OTP will be sent</b> to that number via SMS\n"
            f"6️⃣ Enter the OTP in the bot\n"
            f"7️⃣ Bot does the rest automatically! 🤖\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>Important Notes:</b>\n"
            f"• If the number is <b>already registered</b> → Point is refunded!\n"
            f"• Each number can only be used <b>once</b>\n"
            f"• OTP expires quickly — enter it fast!\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"💡 <b>Earn points by inviting friends!</b>\n"
            f"Share your invite link → +{POINTS_PER_REFERRAL} points per friend!",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
        )

    # ── Resend OTP ─────────────────────────────────────────────────────────
    elif data == "resend_otp":
        session = await db.get_session(user_id)
        if not session or session.get("state") != "waiting_otp":
            await query.answer("❌ No active session. Please start again.", show_alert=True)
            return

        reg_cookies_raw = session.get("reg_cookies_json")
        if not reg_cookies_raw:
            await query.answer("❌ Can't resend — session data missing. Please start again.", show_alert=True)
            return

        sess_phone = session.get("phone")
        sess_user_key = session.get("user_key")
        sess_data_key = session.get("data_key")
        sess_rnd_name = session.get("rnd_name")
        sess_rnd_city = session.get("rnd_city")

        try:
            sess_user_key = int(sess_user_key)
        except (ValueError, TypeError):
            pass

        reg_cookies = json.loads(reg_cookies_raw)

        try:
            with open(PACK_IMAGE_PATH, "rb") as f:
                image_bytes = f.read()
        except FileNotFoundError:
            await query.answer("⚠️ Bot maintenance — cannot resend.", show_alert=True)
            return

        await query.edit_message_text(
            f"🔄 <b>Resending OTP...</b>\n\n"
            f"📱 Number: <code>{sess_phone}</code>\n"
            f"⏳ Please wait...",
            parse_mode=ParseMode.HTML,
        )

        loop = asyncio.get_running_loop()
        get_otp_fn = partial(
            api.api_get_otp,
            reg_cookies, sess_user_key, sess_data_key,
            sess_phone, sess_rnd_name, sess_rnd_city, image_bytes
        )
        otp_result, cookies2 = await loop.run_in_executor(_executor, get_otp_fn)

        if otp_result.get("statusCode") not in (200, 201):
            if api.is_already_used_error(otp_result):
                await db.clear_session(user_id)
                await db.update_points(user_id, 1)
                await query.edit_message_text(
                    f"⚠️ <b>Number Already Used!</b>\n\n"
                    f"📱 <code>{sess_phone}</code> is already registered on Ujala.\n\n"
                    f"✅ <b>Point refunded!</b> Try another number.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_menu_keyboard(user_id),
                )
            else:
                await query.edit_message_text(
                    f"❌ <b>Resend Failed</b>\n\n"
                    f"Ujala couldn't send the OTP again. Please wait a moment and try once more.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=otp_keyboard(),
                )
            return

        # Update session with fresh cookies
        await db.upsert_session(user_id, cookies_json=json.dumps(cookies2))

        await query.edit_message_text(
            f"📲 <b>OTP Resent!</b>\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"✅ A new OTP has been sent to <code>{sess_phone}</code>\n\n"
            f"📩 Check the SMS and type the OTP below:\n\n"
            f"⚡ <i>Be quick — OTP expires soon!</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=otp_keyboard(),
        )

    # ── Cancel ─────────────────────────────────────────────────────────────
    elif data == "cancel_operation":
        await db.clear_session(user_id)
        user_data = await db.get_user(user_id)
        await query.edit_message_text(
            f"❌ <b>Cancelled!</b>\n\n"
            f"No worries — you can try again anytime!\n"
            f"💰 Your points: <b>{user_data.get('points', 0)}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(user_id),
        )

    # ── Admin panel ────────────────────────────────────────────────────────
    elif data == "admin_panel":
        if not is_admin(user_id):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        await show_admin_panel(update, context)

    elif data.startswith("admin_"):
        if not is_admin(user_id):
            await query.answer("⛔ Access denied.", show_alert=True)
            return
        await handle_admin_callback(update, context, data)


# ── Admin panel ───────────────────────────────────────────────────────────────

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_count = await db.get_user_count()
    stats = await db.get_usage_stats()
    buttons = [
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
            InlineKeyboardButton("📊 Full Stats", callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("➕ Points → One User", callback_data="admin_addpoints_user"),
            InlineKeyboardButton("🌐 Points → All Users", callback_data="admin_addpoints_all"),
        ],
        [
            InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban"),
            InlineKeyboardButton("✅ Unban User", callback_data="admin_unban"),
        ],
        [
            InlineKeyboardButton("🔍 User Lookup", callback_data="admin_lookup"),
            InlineKeyboardButton("📋 Top Users", callback_data="admin_list_users"),
        ],
        [InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")],
    ]
    text = (
        f"🛠 <b>Admin Panel</b>\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users: <b>{user_count}</b>\n"
        f"🔢 Total Submissions: <b>{stats['total']}</b>\n"
        f"✅ Successful: <b>{stats['successful']}</b>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"👇 Choose an action:"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons)
        )


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    user_id = update.effective_user.id
    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]])

    if data == "admin_stats":
        user_count = await db.get_user_count()
        usage = await db.get_usage_stats()
        all_users = await db.get_all_users()
        total_points = sum(u["points"] for u in all_users)
        banned = len([u for u in all_users if u.get("is_banned")])
        await query.edit_message_text(
            f"📊 <b>Full Statistics</b>\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"👥 Total Users: <b>{user_count}</b>\n"
            f"🚫 Banned Users: <b>{banned}</b>\n"
            f"💰 Total Points Given: <b>{total_points}</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🔢 Total Submissions: <b>{usage['total']}</b>\n"
            f"✅ Successful: <b>{usage['successful']}</b>\n"
            f"❌ Failed: <b>{usage['total'] - usage['successful']}</b>\n"
            f"👤 Unique Submitters: <b>{usage['unique_users']}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_btn,
        )

    elif data == "admin_broadcast":
        await db.upsert_session(user_id, state="admin_broadcast")
        await query.edit_message_text(
            f"📢 <b>Broadcast Message</b>\n\n"
            f"Type your message below and it will be sent to <b>ALL users</b>!\n\n"
            f"You can use HTML:\n"
            f"• <code>&lt;b&gt;bold&lt;/b&gt;</code>\n"
            f"• <code>&lt;i&gt;italic&lt;/i&gt;</code>\n\n"
            f"✍️ Type your message now:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]),
        )

    elif data == "admin_addpoints_user":
        await db.upsert_session(user_id, state="admin_addpoints_user")
        await query.edit_message_text(
            f"➕ <b>Add / Remove Points</b>\n\n"
            f"Send in this format:\n"
            f"<code>USER_ID POINTS</code>\n\n"
            f"Examples:\n"
            f"• <code>123456789 50</code> → add 50 pts\n"
            f"• <code>123456789 -10</code> → remove 10 pts",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]),
        )

    elif data == "admin_addpoints_all":
        await db.upsert_session(user_id, state="admin_addpoints_all")
        await query.edit_message_text(
            f"🌐 <b>Add Points to ALL Users</b>\n\n"
            f"How many points should everyone get?\n\n"
            f"Example: <code>10</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]),
        )

    elif data == "admin_ban":
        await db.upsert_session(user_id, state="admin_ban")
        await query.edit_message_text(
            f"🚫 <b>Ban a User</b>\n\n"
            f"Enter the Telegram User ID to ban:\n\n"
            f"Example: <code>123456789</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]),
        )

    elif data == "admin_unban":
        await db.upsert_session(user_id, state="admin_unban")
        await query.edit_message_text(
            f"✅ <b>Unban a User</b>\n\n"
            f"Enter the Telegram User ID to unban:\n\n"
            f"Example: <code>123456789</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]),
        )

    elif data == "admin_lookup":
        await db.upsert_session(user_id, state="admin_lookup")
        await query.edit_message_text(
            f"🔍 <b>User Lookup</b>\n\n"
            f"Enter the Telegram User ID:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]),
        )

    elif data == "admin_list_users":
        all_users = await db.get_all_users()
        if not all_users:
            text = "😕 No users found."
        else:
            lines = []
            for i, u in enumerate(all_users[:25], 1):
                status = "🚫" if u.get("is_banned") else "✅"
                name = (u.get("first_name") or "?")[:12]
                lines.append(
                    f"{i}. {status} <b>{name}</b> | <code>{u['telegram_id']}</code> | 💰{u['points']}"
                )
            text = f"📋 <b>Top 25 Users (by points)</b>\n\n" + "\n".join(lines)
            if len(all_users) > 25:
                text += f"\n\n<i>...and {len(all_users)-25} more users</i>"
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=back_btn)


# ── Message handler (state machine) ──────────────────────────────────────────

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip() if update.message.text else ""

    user_data = await db.get_user(user_id)
    if not user_data:
        await cmd_start(update, context)
        return
    if user_data.get("is_banned"):
        await update.message.reply_text(
            "🚫 <b>Access Denied</b>\n\nYou have been banned from this bot.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await ensure_joined(update, context):
        return

    session = await db.get_session(user_id)
    if not session:
        await db.upsert_session(user_id)
        session = {"state": "idle"}

    state = session.get("state", "idle")

    # ── Admin: broadcast ───────────────────────────────────────────────────
    if state == "admin_broadcast" and is_admin(user_id):
        await db.clear_session(user_id)
        all_users = await db.get_all_users()
        sent, failed = 0, 0
        status_msg = await update.message.reply_text(
            f"📡 <b>Sending broadcast to {len(all_users)} users...</b>",
            parse_mode=ParseMode.HTML,
        )
        for u in all_users:
            if u.get("is_banned"):
                continue
            try:
                await context.bot.send_message(u["telegram_id"], text, parse_mode=ParseMode.HTML)
                sent += 1
            except TelegramError:
                failed += 1
            await asyncio.sleep(0.05)
        await status_msg.edit_text(
            f"📢 <b>Broadcast Complete!</b>\n\n"
            f"✅ Sent: <b>{sent}</b>\n"
            f"❌ Failed (blocked/left): <b>{failed}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]),
        )

    # ── Admin: add points to one user ──────────────────────────────────────
    elif state == "admin_addpoints_user" and is_admin(user_id):
        await db.clear_session(user_id)
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit():
            await update.message.reply_text(
                "❌ <b>Wrong format!</b>\n\nUse: <code>USER_ID POINTS</code>\n\nExample: <code>123456789 50</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        target_id, points = int(parts[0]), int(parts[1])
        target = await db.get_user(target_id)
        if not target:
            await update.message.reply_text("❌ User not found!", reply_markup=back_keyboard())
            return
        new_pts = await db.update_points(target_id, points)
        action = "Added ➕" if points >= 0 else "Removed ➖"
        await update.message.reply_text(
            f"✅ <b>Done!</b>\n\n"
            f"{action} <b>{abs(points)} points</b> for <code>{target_id}</code>\n"
            f"💰 New balance: <b>{new_pts} points</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]),
        )
        try:
            if points > 0:
                await context.bot.send_message(
                    target_id,
                    f"🎁 <b>Points Added!</b>\n\n"
                    f"An admin just gave you <b>+{points} points!</b>\n"
                    f"💰 Your total: <b>{new_pts} points</b>",
                    parse_mode=ParseMode.HTML,
                )
        except TelegramError:
            pass

    # ── Admin: add points to all ───────────────────────────────────────────
    elif state == "admin_addpoints_all" and is_admin(user_id):
        await db.clear_session(user_id)
        if not text.lstrip("-").isdigit():
            await update.message.reply_text("❌ Enter a valid number.", reply_markup=back_keyboard())
            return
        points = int(text)
        await db.add_points_all(points)
        user_count = await db.get_user_count()
        await update.message.reply_text(
            f"🌐 <b>Points Added to Everyone!</b>\n\n"
            f"💰 <b>+{points} points</b> given to all <b>{user_count}</b> users!",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]),
        )

    # ── Admin: ban ─────────────────────────────────────────────────────────
    elif state == "admin_ban" and is_admin(user_id):
        await db.clear_session(user_id)
        if not text.isdigit():
            await update.message.reply_text("❌ Invalid User ID.", reply_markup=back_keyboard())
            return
        target_id = int(text)
        if not await db.get_user(target_id):
            await update.message.reply_text("❌ User not found!", reply_markup=back_keyboard())
            return
        await db.set_ban(target_id, True)
        await update.message.reply_text(
            f"🚫 User <code>{target_id}</code> has been <b>banned</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]),
        )

    # ── Admin: unban ───────────────────────────────────────────────────────
    elif state == "admin_unban" and is_admin(user_id):
        await db.clear_session(user_id)
        if not text.isdigit():
            await update.message.reply_text("❌ Invalid User ID.", reply_markup=back_keyboard())
            return
        target_id = int(text)
        await db.set_ban(target_id, False)
        await update.message.reply_text(
            f"✅ User <code>{target_id}</code> has been <b>unbanned</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]),
        )

    # ── Admin: lookup ──────────────────────────────────────────────────────
    elif state == "admin_lookup" and is_admin(user_id):
        await db.clear_session(user_id)
        if not text.isdigit():
            await update.message.reply_text("❌ Invalid User ID.", reply_markup=back_keyboard())
            return
        target = await db.get_user(int(text))
        if not target:
            await update.message.reply_text("❌ User not found!", reply_markup=back_keyboard())
            return
        ref_count = await db.get_referral_count(int(text))
        status = "🚫 Banned" if target.get("is_banned") else "✅ Active"
        await update.message.reply_text(
            f"🔍 <b>User Info</b>\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🆔 ID: <code>{target['telegram_id']}</code>\n"
            f"👤 Name: <b>{target.get('first_name', 'N/A')}</b>\n"
            f"🔗 Username: @{target.get('username') or 'N/A'}\n"
            f"💰 Points: <b>{target['points']}</b>\n"
            f"👥 Referrals: <b>{ref_count}</b>\n"
            f"📅 Joined: {target.get('join_date', 'N/A')[:10]}\n"
            f"Status: {status}\n"
            f"━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]),
        )

    # ── User: waiting for phone ────────────────────────────────────────────
    elif state == "waiting_phone":
        phone = text.replace(" ", "").replace("-", "")
        if not phone.isdigit() or len(phone) != 10:
            await update.message.reply_text(
                f"❌ <b>Invalid number!</b>\n\n"
                f"Please enter exactly <b>10 digits</b> — no spaces, no dashes.\n\n"
                f"📱 Example: <code>9876543210</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=cancel_keyboard(),
            )
            return

        # Per-user lock: prevents duplicate submission from fast double-sends
        lock = get_user_lock(user_id)
        if lock.locked():
            await update.message.reply_text(
                "⏳ Already processing your previous request. Please wait...",
                parse_mode=ParseMode.HTML,
            )
            return

        async with lock:
            try:
                with open(PACK_IMAGE_PATH, "rb") as f:
                    image_bytes = f.read()
            except FileNotFoundError:
                await db.clear_session(user_id)
                await update.message.reply_text(
                    "⚠️ Bot is under maintenance. Contact admin!",
                    reply_markup=main_menu_keyboard(user_id),
                )
                return

            # Deduct 1 point before starting
            u = await db.get_user(user_id)
            if u.get("points", 0) < 1:
                await db.clear_session(user_id)
                await update.message.reply_text(
                    f"🚫 <b>No Points!</b>\n\nYou need points to scan numbers.\n"
                    f"Invite friends to earn them!",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_menu_keyboard(user_id),
                )
                return
            await db.update_points(user_id, -1)

            processing_msg = await update.message.reply_text(
                f"⏳ <b>Processing your number...</b>\n\n"
                f"📱 Number: <code>{phone}</code>\n"
                f"🔄 Registering on Ujala website...\n\n"
                f"<i>This may take up to 30 seconds. Please wait...</i>",
                parse_mode=ParseMode.HTML,
            )

            loop = asyncio.get_running_loop()

            # Step 1: Register (now has retry logic)
            reg_result, cookies = await loop.run_in_executor(_executor, api.api_register)

            if reg_result.get("statusCode") != 200:
                await db.clear_session(user_id)
                await db.update_points(user_id, 1)   # refund
                if api.is_already_used_error(reg_result):
                    await db.log_usage(user_id, phone, False, 0)
                    await processing_msg.edit_text(
                        f"⚠️ <b>Number Already Used!</b>\n\n"
                        f"📱 <code>{phone}</code> is already registered on Ujala.\n\n"
                        f"✅ <b>Point refunded!</b> Try another number.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=main_menu_keyboard(user_id),
                    )
                else:
                    await processing_msg.edit_text(
                        f"❌ <b>Registration Failed</b>\n\n"
                        f"The Ujala website didn't respond after several retries.\n"
                        f"✅ Your point was refunded.\n\n"
                        f"Please try again in a few minutes.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=main_menu_keyboard(user_id),
                    )
                return

            user_key = reg_result.get("userKey")
            data_key = str(reg_result.get("dataKey", ""))

            # Step 2: Get OTP
            rnd_n = api.rnd_name()
            rnd_c = api.rnd_city()
            await processing_msg.edit_text(
                f"⏳ <b>Processing...</b>\n\n"
                f"📱 Number: <code>{phone}</code>\n"
                f"✅ Registered!\n"
                f"📤 Sending OTP to your number...",
                parse_mode=ParseMode.HTML,
            )

            get_otp_fn = partial(api.api_get_otp, cookies, user_key, data_key, phone, rnd_n, rnd_c, image_bytes)
            otp_result, cookies2 = await loop.run_in_executor(_executor, get_otp_fn)

            if otp_result.get("statusCode") not in (200, 201):
                await db.clear_session(user_id)
                await db.update_points(user_id, 1)   # refund
                if api.is_already_used_error(otp_result):
                    await db.log_usage(user_id, phone, False, 0)
                    await processing_msg.edit_text(
                        f"⚠️ <b>Number Already Used!</b>\n\n"
                        f"📱 <code>{phone}</code> is already registered on Ujala.\n\n"
                        f"✅ <b>Point refunded!</b> Try another number.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=main_menu_keyboard(user_id),
                    )
                else:
                    await processing_msg.edit_text(
                        f"❌ <b>OTP Request Failed</b>\n\n"
                        f"Ujala couldn't send the OTP after several retries.\n"
                        f"✅ Your point was refunded.\n\n"
                        f"Please try again.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=main_menu_keyboard(user_id),
                    )
                return

            # Save session
            await db.upsert_session(
                user_id,
                state="waiting_otp",
                phone=phone,
                user_key=str(user_key),
                data_key=data_key,
                cookies_json=json.dumps(cookies2),
                reg_cookies_json=json.dumps(cookies),
                rnd_name=rnd_n,
                rnd_city=rnd_c,
            )

            await processing_msg.edit_text(
                f"📲 <b>OTP Sent!</b>\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📝 <b>Step 2 of 2:</b> Enter OTP\n"
                f"━━━━━━━━━━━━━━━━\n\n"
                f"✅ OTP has been sent to <code>{phone}</code>\n\n"
                f"📩 Check the SMS and type the OTP below:\n\n"
                f"⚡ <i>Be quick — OTP expires soon!</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=otp_keyboard(),
            )

    # ── User: waiting for OTP ─────────────────────────────────────────────
    elif state == "waiting_otp":
        otp = text.strip()
        if not otp.isdigit() or len(otp) < 4:
            await update.message.reply_text(
                f"❌ <b>Invalid OTP!</b>\n\n"
                f"Please enter the numeric OTP from your SMS.\n\n"
                f"<i>Example: 123456</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=cancel_keyboard(),
            )
            return

        # Per-user lock: prevents double-submit
        lock = get_user_lock(user_id)
        if lock.locked():
            await update.message.reply_text(
                "⏳ Already verifying. Please wait...",
                parse_mode=ParseMode.HTML,
            )
            return

        async with lock:
            processing_msg = await update.message.reply_text(
                f"⏳ <b>Verifying OTP...</b> Please wait!\n\n"
                f"<i>This may take a few seconds...</i>",
                parse_mode=ParseMode.HTML,
            )

            sess_data = session
            user_key = sess_data.get("user_key")
            try:
                user_key = int(user_key)
            except (ValueError, TypeError):
                pass
            data_key = sess_data.get("data_key")
            phone = sess_data.get("phone")
            cookies = json.loads(sess_data.get("cookies_json") or "{}")

            loop = asyncio.get_running_loop()

            # Step 3: Verify OTP
            verify_fn = partial(api.api_verify_otp, cookies, user_key, data_key, otp)
            verify_result, cookies3 = await loop.run_in_executor(_executor, verify_fn)

            if verify_result.get("statusCode") not in (200, 201):
                await processing_msg.edit_text(
                    f"❌ <b>Wrong OTP!</b>\n\n"
                    f"The OTP you entered is incorrect or expired.\n\n"
                    f"🔄 <b>What to do?</b>\n"
                    f"• Check the SMS again carefully\n"
                    f"• Make sure you're entering the latest OTP\n"
                    f"• Tap <b>Resend OTP</b> to get a new one\n"
                    f"• If expired, cancel and submit the number again",
                    parse_mode=ParseMode.HTML,
                    reply_markup=otp_keyboard(),
                )
                return

            # Extract access token
            access_token = (
                verify_result.get("accessToken")
                or verify_result.get("access_token")
                or verify_result.get("token")
                or ""
            )
            if not access_token:
                v_data = verify_result.get("data") or verify_result.get("result") or {}
                if isinstance(v_data, dict):
                    access_token = (
                        v_data.get("accessToken")
                        or v_data.get("access_token")
                        or v_data.get("token")
                        or ""
                    )
            if not access_token:
                access_token = data_key

            await processing_msg.edit_text(
                f"✅ <b>OTP Verified!</b>\n\n🎡 Spinning the wheel... 🤞",
                parse_mode=ParseMode.HTML,
            )

            # Step 4: Spin
            spin_fn = partial(api.api_spin, cookies3, user_key, access_token, data_key)
            spin_result, cookies4 = await loop.run_in_executor(_executor, spin_fn)

            if spin_result.get("statusCode") not in (200, 201):
                await db.log_usage(user_id, phone, False, 0)
                await db.clear_session(user_id)
                await db.update_points(user_id, 1)   # refund
                await processing_msg.edit_text(
                    f"⚠️ <b>Spin Failed</b>\n\n"
                    f"The Ujala website didn't respond at the spin step.\n\n"
                    f"✅ <b>Point refunded!</b>\n"
                    f"🔄 Please try submitting the number again.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_menu_keyboard(user_id),
                )
                return

            reward = (
                spin_result.get("reward")
                or spin_result.get("rewardType")
                or spin_result.get("prize")
                or spin_result.get("voucher")
                or "Reward claimed! Check your SMS."
            )

            # Step 5: Claim
            await processing_msg.edit_text(
                f"🎁 <b>Claiming your reward...</b>",
                parse_mode=ParseMode.HTML,
            )
            claim_fn = partial(api.api_claim, cookies4, user_key, access_token, data_key)
            claim_result = await loop.run_in_executor(_executor, claim_fn)

            await db.log_usage(user_id, phone, True, 0)
            await db.clear_session(user_id)
            cur_pts = (await db.get_user(user_id)).get("points", 0)

            claim_msg = ""
            if claim_result and claim_result.get("message"):
                claim_msg = f"\n📬 <b>Claim:</b> {claim_result.get('message')}"

            await processing_msg.edit_text(
                f"🎉 <b>SUCCESS! Well done!</b>\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📱 Number: <code>{phone}</code>\n"
                f"🎁 Reward: <b>{str(reward)}</b>{claim_msg}\n"
                f"━━━━━━━━━━━━━━━━\n\n"
                f"💰 Points remaining: <b>{cur_pts}</b>\n\n"
                f"🤝 <i>Invite friends to earn more points!</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(user_id),
            )

    # ── Default ────────────────────────────────────────────────────────────
    else:
        await update.message.reply_text(
            f"👇 Use the menu to get started!",
            reply_markup=main_menu_keyboard(user_id),
        )


# ── Admin slash commands ──────────────────────────────────────────────────────

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await show_admin_panel(update, context)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    message = " ".join(context.args)
    all_users = await db.get_all_users()
    sent, failed = 0, 0
    for u in all_users:
        if u.get("is_banned"):
            continue
        try:
            await context.bot.send_message(u["telegram_id"], message, parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"📢 Done! ✅ Sent: {sent} | ❌ Failed: {failed}")


async def cmd_addpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /addpoints <user_id> <points>")
        return
    try:
        target_id, points = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments.")
        return
    new_pts = await db.update_points(target_id, points)
    await update.message.reply_text(f"✅ Done! New balance: {new_pts} pts for {target_id}")


async def cmd_setpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /setpoints <user_id> <points>")
        return
    try:
        target_id, points = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid arguments.")
        return
    await db.set_points(target_id, points)
    await update.message.reply_text(f"✅ Set to {points} pts for {target_id}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    await db.set_ban(int(context.args[0]), True)
    await update.message.reply_text(f"🚫 User {context.args[0]} banned.")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    await db.set_ban(int(context.args[0]), False)
    await update.message.reply_text(f"✅ User {context.args[0]} unbanned.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    user_count = await db.get_user_count()
    usage = await db.get_usage_stats()
    await update.message.reply_text(
        f"📊 <b>Quick Stats</b>\n\n"
        f"👥 Users: {user_count}\n"
        f"🔢 Submissions: {usage['total']}\n"
        f"✅ Successful: {usage['successful']}",
        parse_mode=ParseMode.HTML,
    )


# ── Error handler ─────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set!")

    asyncio.run(db.init_db())
    logger.info("Database initialized.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("addpoints", cmd_addpoints))
    app.add_handler(CommandHandler("setpoints", cmd_setpoints))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("stats", cmd_stats))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    logger.info(f"Bot starting with polling (thread pool: {MAX_WORKERS} workers)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
