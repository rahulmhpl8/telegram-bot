import logging
import sqlite3
import random
import string
import io
import asyncio
import os
import time
from datetime import datetime
from contextlib import contextmanager

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ------------------ ENVIRONMENT VARIABLES ------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8760098545:AAFiblfocewAgF7eLocumh6RlWprmRqTeWI")
BOT_USERNAME = os.getenv("BOT_USERNAME", "Geminiprolink_bot")
CREDITS_PER_REFERRAL = int(os.getenv("CREDITS_PER_REFERRAL", "5"))
ADMIN_USER_IDS = [int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "543578081").split(",") if x.strip()]

CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/lootjunctiontg")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@lootjunctiontg")

# Persistent DB path (Render Disk)
DB_PATH = os.getenv("DB_PATH", "/opt/data/users.db")
DB_NAME = DB_PATH

# Rate limiting
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "5"))
RATE_WINDOW = 3600

# ------------------ LOGGING ------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------ URL SHORTENER ------------------
def shorten_url(url):
    try:
        import requests
        response = requests.get(f"http://tinyurl.com/api-create.php?url={url}", timeout=3)
        if response.status_code == 200:
            short = response.text.strip()
            if short.startswith("http"):
                return short
        return url
    except Exception:
        return url

# ------------------ DATABASE ------------------
def init_db():
    os.makedirs(os.path.dirname(DB_NAME), exist_ok=True)
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            ref_code TEXT UNIQUE,
            credits INTEGER DEFAULT 0,
            referred_by INTEGER,
            join_date TEXT,
            refer_count INTEGER DEFAULT 0,
            verified INTEGER DEFAULT 0,
            full_name TEXT
        )
    """)
    c.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in c.fetchall()]
    if 'verified' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN verified INTEGER DEFAULT 0")
    if 'full_name' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {DB_NAME}")

@contextmanager
def get_db_cursor():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        yield c
        conn.commit()
    finally:
        conn.close()

def add_user(user_id, referred_by=None, full_name=None):
    with get_db_cursor() as c:
        c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if c.fetchone():
            return
        while True:
            ref_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            c.execute("SELECT ref_code FROM users WHERE ref_code = ?", (ref_code,))
            if not c.fetchone():
                break
        join_date = datetime.now().isoformat()
        c.execute(
            "INSERT INTO users (user_id, ref_code, referred_by, join_date, credits, verified, full_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, ref_code, referred_by, join_date, 0, 0, full_name)
        )
        if referred_by:
            c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (CREDITS_PER_REFERRAL, referred_by))
            c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (CREDITS_PER_REFERRAL, user_id))
            c.execute("UPDATE users SET refer_count = refer_count + 1 WHERE user_id = ?", (referred_by,))

def get_user(user_id):
    with get_db_cursor() as c:
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return c.fetchone()

def get_user_by_ref(ref_code):
    with get_db_cursor() as c:
        c.execute("SELECT user_id FROM users WHERE ref_code = ?", (ref_code,))
        row = c.fetchone()
        return row[0] if row else None

def get_all_users():
    with get_db_cursor() as c:
        c.execute("SELECT user_id, credits, refer_count FROM users ORDER BY credits DESC")
        return c.fetchall()

def get_total_users():
    with get_db_cursor() as c:
        c.execute("SELECT COUNT(*) FROM users")
        return c.fetchone()[0]

def get_total_credits():
    with get_db_cursor() as c:
        c.execute("SELECT SUM(credits) FROM users")
        total = c.fetchone()[0]
        return total or 0

def reset_user_credits(user_id):
    with get_db_cursor() as c:
        c.execute("UPDATE users SET credits = 0 WHERE user_id = ?", (user_id,))

def set_verified(user_id, verified=1):
    with get_db_cursor() as c:
        c.execute("UPDATE users SET verified = ? WHERE user_id = ?", (verified, user_id))

def delete_user(user_id):
    with get_db_cursor() as c:
        c.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

def add_credits(user_id, amount):
    with get_db_cursor() as c:
        c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))

def add_all_credits(amount):
    with get_db_cursor() as c:
        c.execute("UPDATE users SET credits = credits + ?", (amount,))

def deduct_credit(user_id):
    with get_db_cursor() as c:
        c.execute("UPDATE users SET credits = credits - 1 WHERE user_id = ? AND credits > 0", (user_id,))

# ------------------ KEYBOARDS ------------------
main_inline_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔵 Gemini", callback_data="main_gemini")],
    [InlineKeyboardButton("🔴 Profile", callback_data="main_profile"),
     InlineKeyboardButton("🟢 Refer", callback_data="main_refer")],
    [InlineKeyboardButton("🟡 Support", callback_data="main_support")],
])

join_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_LINK)],
    [InlineKeyboardButton("✅ I have Joined", callback_data="check_join")]
])

# ------------------ RATE LIMITER ------------------
user_requests = {}

def is_rate_limited(user_id):
    now = time.time()
    timestamps = user_requests.get(user_id, [])
    timestamps = [t for t in timestamps if now - t < RATE_WINDOW]
    if len(timestamps) >= RATE_LIMIT:
        return True
    timestamps.append(now)
    user_requests[user_id] = timestamps
    return False

# ------------------ HELPER: REPLACE BOT MESSAGE ------------------
async def replace_bot_message(chat_id, context, text, parse_mode=None, reply_markup=None, key='last_msg_id'):
    prev_id = context.user_data.get(key)
    if prev_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=prev_id)
        except Exception:
            pass
    new_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=reply_markup
    )
    context.user_data[key] = new_msg.message_id
    return new_msg

# ------------------ JOIN PROMPT ------------------
async def send_join_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🚨 *You must join our channel to use this bot!*\n\n"
        "1️⃣ Click the button below to join.\n"
        "2️⃣ Then click *'I have Joined'* to verify."
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=join_keyboard)

# ------------------ START ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    full_name = update.effective_user.full_name or "Unknown"

    user = get_user(user_id)
    if not user:
        add_user(user_id, referred_by=None, full_name=full_name)
        user = get_user(user_id)
    else:
        if not user[7] and full_name:
            with get_db_cursor() as c:
                c.execute("UPDATE users SET full_name = ? WHERE user_id = ?", (full_name, user_id))
            user = get_user(user_id)

    if user and user[6] == 1:
        try:
            member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
            if member.status in ["member", "administrator", "creator"]:
                display_name = user[7] or full_name or "User"
                await update.message.reply_text(
                    f"👋 Welcome back {display_name}!\nYour unique ID: `{user_id}`",
                    parse_mode="Markdown",
                    reply_markup=main_inline_keyboard
                )
                return
            else:
                set_verified(user_id, 0)
        except Exception as e:
            logger.error(f"Membership check error: {e}")
            set_verified(user_id, 0)

    if args and args[0].startswith("ref"):
        ref_code = args[0][3:]
        referrer_id = get_user_by_ref(ref_code)
        if referrer_id and referrer_id != user_id:
            context.user_data['pending_referrer'] = referrer_id

    display_name = full_name or "User"
    await update.message.reply_text(
        f"👋 Welcome {display_name}!\nYour unique ID: `{user_id}`",
        parse_mode="Markdown",
        reply_markup=main_inline_keyboard
    )
    await send_join_prompt(update, context)

# ------------------ CHECK JOIN ------------------
async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            set_verified(user_id, 1)

            referrer_id = context.user_data.get('pending_referrer')
            if referrer_id:
                with get_db_cursor() as c:
                    c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (CREDITS_PER_REFERRAL, referrer_id))
                    c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (CREDITS_PER_REFERRAL, user_id))
                    c.execute("UPDATE users SET refer_count = refer_count + 1 WHERE user_id = ?", (referrer_id,))
                    c.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer_id, user_id))
                context.user_data.pop('pending_referrer', None)

            await query.edit_message_text(
                text=f"✅ Verified! Welcome to the bot.\nYour unique ID: {user_id}",
                reply_markup=None
            )
            await update.effective_message.reply_text(
                "Use the buttons below to explore.",
                reply_markup=main_inline_keyboard
            )
        else:
            await query.answer("You haven't joined the channel yet! Please join first.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in check_join: {e}")
        await query.edit_message_text(
            "⚠️ *Unable to verify membership.*\n\n"
            "The bot is not a member of the channel. Please contact the admin to add the bot to the channel, then try again.",
            parse_mode="Markdown",
            reply_markup=join_keyboard
        )

# ------------------ VERIFICATION DECORATOR ------------------
async def require_verified(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        full_name = update.effective_user.full_name or "Unknown"
        add_user(user_id, referred_by=None, full_name=full_name)
        user = get_user(user_id)

    if user[6] == 0:
        await send_join_prompt(update, context)
        return False

    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        if member.status not in ["member", "administrator", "creator"]:
            set_verified(user_id, 0)
            await send_join_prompt(update, context)
            return False
    except Exception as e:
        logger.error(f"Membership check error: {e}")
        set_verified(user_id, 0)
        await update.message.reply_text(
            "⚠️ *Membership verification failed.*\n"
            "Please ensure the bot is a member of the channel, then try again.",
            parse_mode="Markdown"
        )
        await send_join_prompt(update, context)
        return False

    return True

# ------------------ MAIN MENU CALLBACK ------------------
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "main_profile":
        await profile(update, context)
    elif data == "main_refer":
        await refer(update, context)
    elif data == "main_gemini":
        await gemini_handler(update, context)
    elif data == "main_support":
        await support_button(update, context)

# ------------------ PROFILE ------------------
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user_id = update.callback_query.from_user.id
        chat_id = update.callback_query.message.chat.id
        message = update.callback_query.message
    else:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        message = update.message

    user = get_user(user_id)
    if not user:
        await message.reply_text("Please /start the bot first.")
        return

    if user[6] == 0:
        await send_join_prompt(update, context)
        return

    text = (
        f"👤 *Profile*\n"
        f"ID: `{user[0]}`\n"
        f"Name: {user[7] or 'N/A'}\n"
        f"Referral Code: `{user[1]}`\n"
        f"Credits: {user[2]}\n"
        f"Total Referrals: {user[5]}\n"
        f"Joined: {user[4][:10]}"
    )
    await message.reply_text(text, parse_mode="Markdown", reply_markup=main_inline_keyboard)

# ------------------ REFER ------------------
async def refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user_id = update.callback_query.from_user.id
        message = update.callback_query.message
    else:
        user_id = update.effective_user.id
        message = update.message

    user = get_user(user_id)
    if not user:
        await message.reply_text("Please /start the bot first.")
        return

    if user[6] == 0:
        await send_join_prompt(update, context)
        return

    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref{user[1]}"
    text = (
        f"🔗 *Your Referral Link*\n"
        f"Share this link with friends:\n`{ref_link}`\n\n"
        f"When someone joins using your link, both of you get *{CREDITS_PER_REFERRAL} credits*!"
    )
    await message.reply_text(text, parse_mode="Markdown", reply_markup=main_inline_keyboard)

# ------------------ LEADERBOARD ------------------
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_verified(update, context):
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("No users yet.")
        return
    top = users[:10]
    text = "🏆 *Leaderboard (Top 10 by Credits)*\n\n"
    for i, (uid, credits, refs) in enumerate(top, start=1):
        text += f"{i}. ID: `{uid}` – Credits: {credits} (Refs: {refs})\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_inline_keyboard)

# ------------------ GLOBAL OTP STORE (bridge between user messages and Playwright task) ------------------
OTP_STORE = {}

# ------------------ PLAYWRIGHT LOGIN (async) ------------------
async def perform_jio_login_playwright(mobile, context, chat_id, user_id):
    """Asynchronous Playwright login, returns dict with success/link/error."""
    try:
        async with async_playwright() as p:
            # Launch headless Chromium with Render-friendly args
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--window-size=1920,1080'
                ]
            )
            page = await browser.new_page()
            await page.goto("https://www.jio.com/selfcare/login/", wait_until="networkidle")

            # ----- Step 1: Enter Mobile -----
            # Try various selectors for mobile input
            mobile_input = await page.wait_for_selector(
                "input[type='tel'], input[name='mobile'], input#mobile, input[placeholder*='Mobile']",
                timeout=10000
            )
            await mobile_input.fill(mobile)

            # ----- Step 2: Click "Get OTP" -----
            # Try multiple possible selectors
            otp_button = None
            for selector in [
                "button:has-text('Get OTP')",
                "button:has-text('Generate OTP')",
                "button:has-text('Send OTP')",
                "button:has-text('OTP')",
                "button:has-text('Generate')",
                "button[type='submit']",
                ".otp-btn",
                "#otp-btn",
                ".login-btn",
                "#login-btn",
                ".btn-primary",
                "//button[contains(@class, 'otp') or contains(@id, 'otp')]"
            ]:
                try:
                    otp_button = await page.wait_for_selector(selector, timeout=2000)
                    if otp_button:
                        break
                except:
                    continue
            if not otp_button:
                raise Exception("Could not find 'Generate OTP' button")

            await otp_button.scroll_into_view_if_needed()
            await otp_button.click()

            # ----- Step 3: Wait for OTP fields to appear -----
            # Look for OTP input fields (could be multiple single-digit fields or one text field)
            otp_fields = []
            # Try multiple single-digit inputs
            try:
                otp_fields = await page.query_selector_all("input[maxlength='1'][type='text'], input[maxlength='1'][type='number']")
                if not otp_fields:
                    # Maybe a single OTP input
                    single_otp = await page.wait_for_selector("input[name='otp'], input#otp, input[autocomplete='one-time-code'], input[placeholder*='OTP']", timeout=5000)
                    if single_otp:
                        otp_fields = [single_otp]
            except:
                pass

            if not otp_fields:
                raise Exception("OTP input fields not found")

            # ----- Step 4: Ask user for OTP via Telegram -----
            # Send message to user asking for OTP (we need to send from this async function)
            # We have context.bot available, but we are inside a task; we can use bot.send_message
            # We'll send a message and then wait for OTP_STORE to be filled
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔐 *OTP Sent!*\n\nPlease enter the 4-6 digit OTP received on your mobile:",
                parse_mode="Markdown",
                reply_markup=main_inline_keyboard
            )

            # Wait for OTP (poll OTP_STORE[user_id])
            otp_received = None
            for _ in range(30):  # wait up to 30 seconds
                otp_received = OTP_STORE.get(user_id)
                if otp_received is not None:
                    break
                await asyncio.sleep(1)

            if otp_received is None:
                raise Exception("OTP timeout")

            # ----- Step 5: Fill OTP and submit -----
            if len(otp_fields) > 1:
                # Multiple single-digit fields
                otp_digits = list(str(otp_received))
                for i, inp in enumerate(otp_fields):
                    if i < len(otp_digits):
                        await inp.fill(otp_digits[i])
            else:
                # Single input field
                await otp_fields[0].fill(str(otp_received))

            # Find and click Submit/Verify button
            submit_button = None
            for selector in [
                "button:has-text('Submit')",
                "button:has-text('Verify')",
                "button:has-text('Login')",
                "button:has-text('Confirm')",
                "button[type='submit']",
                "button:has-text('OTP')",
                "//button[contains(@class, 'otp') or contains(@id, 'otp')]"
            ]:
                try:
                    submit_button = await page.wait_for_selector(selector, timeout=2000)
                    if submit_button:
                        break
                except:
                    continue
            if not submit_button:
                raise Exception("Submit button not found")

            await submit_button.scroll_into_view_if_needed()
            await submit_button.click()

            # Wait for navigation or error
            try:
                # Wait for either dashboard or error message
                await page.wait_for_selector("text=You have entered an invalid OTP", timeout=2000)
                # If we see error, we can retry, but we'll just fail for simplicity (retry logic can be added)
                raise Exception("Invalid OTP")
            except PlaywrightTimeoutError:
                # No error message, check URL
                await page.wait_for_url(lambda url: "dashboard" in url or "home" in url or "selfcare" in url, timeout=10000)
                # Login successful

            # ----- Step 6: Find Gemini Subscription Link -----
            # Navigate to the Gemini section (click on the specific element)
            target_selector = "/html/body/div[1]/div[2]/section/main/div/section[2]/div[1]/div/div/ul/li[3]/div/section/div/div"
            # Alternatively try more flexible selector
            try:
                target_element = await page.wait_for_selector(f"xpath={target_selector}", timeout=10000)
                await target_element.scroll_into_view_if_needed()
                await target_element.click()
            except:
                # Fallback: try to find by text or other selector
                target_element = await page.wait_for_selector("text=Gemini", timeout=5000)
                await target_element.click()

            # Wait for Google sign-in page (indicates link found)
            await page.wait_for_url(lambda url: "accounts.google.com" in url, timeout=15000)
            final_url = page.url
            short_link = shorten_url(final_url)

            # Deduct credit
            deduct_credit(user_id)

            return {"success": True, "link": short_link}

    except Exception as e:
        logger.error(f"Playwright error: {e}")
        return {"success": False, "error": str(e)[:150]}

# ------------------ TASK QUEUE FOR PLAYWRIGHT ------------------
task_queue = asyncio.Queue()
MAX_WORKERS = 1  # Playwright is lighter, but still keep 1 for memory

async def selenium_worker():  # name can remain, but it uses Playwright now
    """Background worker that processes Gemini tasks using Playwright."""
    while True:
        task = await task_queue.get()
        try:
            update = task['update']
            context = task['context']
            mobile = task['mobile']
            chat_id = task['chat_id']
            user_id = task['user_id']

            # Perform Playwright login (async)
            result = await perform_jio_login_playwright(mobile, context, chat_id, user_id)

            # Send result
            if result['success']:
                await replace_bot_message(
                    chat_id=chat_id,
                    context=context,
                    text=f"✅ *Gemini subscription link found*\n\n"
                         f"🔗 *Your Link:*\n`{result['link']}`\n\n"
                         "1 credit has been deducted for this request.",
                    parse_mode="Markdown",
                    reply_markup=main_inline_keyboard,
                    key='gemini_last_msg_id'
                )
            else:
                await replace_bot_message(
                    chat_id=chat_id,
                    context=context,
                    text=f"❌ *Error:* {result['error']}\n\nNo credits were deducted.",
                    parse_mode="Markdown",
                    reply_markup=main_inline_keyboard,
                    key='gemini_last_msg_id'
                )
            # Clear gemini step
            context.user_data.pop('gemini_step', None)
        except Exception as e:
            logger.error(f"Worker error: {e}")
        finally:
            task_queue.task_done()

# ------------------ GEMINI HANDLER ------------------
async def gemini_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user_id = update.callback_query.from_user.id
        chat_id = update.callback_query.message.chat.id
        message = update.callback_query.message
    else:
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        message = update.message

    user = get_user(user_id)
    if not user:
        await message.reply_text("Please /start the bot first.")
        return

    if user[6] == 0:
        await send_join_prompt(update, context)
        return

    if user[2] <= 0:
        await message.reply_text(
            "⚠️ *Insufficient Credits!*\n\n"
            "Please Refer This Bot to Earn Credit or Contact Our Support.\n\n"
            "Use the *Refer* button to share your referral link and earn 5 credits per new user.",
            parse_mode="Markdown",
            reply_markup=main_inline_keyboard
        )
        return

    if is_rate_limited(user_id):
        await message.reply_text(
            "⏳ *Rate limit exceeded!*\n"
            f"You can make {RATE_LIMIT} requests per hour. Please try again later.",
            parse_mode="Markdown",
            reply_markup=main_inline_keyboard
        )
        return

    await replace_bot_message(
        chat_id=chat_id,
        context=context,
        text="*Gemini Link Extractor*\n\nPlease Enter 10 Digit Mobile Number:",
        parse_mode="Markdown",
        reply_markup=main_inline_keyboard,
        key='gemini_last_msg_id'
    )
    context.user_data['gemini_step'] = 'awaiting_mobile'

# ------------------ GEMINI MOBILE/OTP HANDLER ------------------
async def gemini_mobile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    step = context.user_data.get('gemini_step')
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if step == 'awaiting_mobile':
        if not user_text.isdigit() or len(user_text) != 10:
            await update.message.reply_text(
                "❌ *Invalid number!*\nPlease enter a 10-digit number:",
                parse_mode="Markdown",
                reply_markup=main_inline_keyboard
            )
            return

        # Enqueue task
        context.user_data['gemini_step'] = 'processing'
        await replace_bot_message(
            chat_id=chat_id,
            context=context,
            text="⏳ Your request has been queued. Please wait...",
            reply_markup=main_inline_keyboard,
            key='gemini_last_msg_id'
        )

        # Put task in queue
        task = {
            'update': update,
            'context': context,
            'mobile': user_text,
            'chat_id': chat_id,
            'user_id': user_id
        }
        await task_queue.put(task)

        # Set step to awaiting_otp for the text handler to capture OTP
        context.user_data['gemini_step'] = 'awaiting_otp'

    elif step == 'awaiting_otp':
        # User sent OTP
        if not user_text.isdigit() or len(user_text) < 4:
            await update.message.reply_text(
                "❌ *Invalid OTP!*\nPlease enter the 4-6 digit OTP received on your mobile:",
                parse_mode="Markdown",
                reply_markup=main_inline_keyboard
            )
            return

        # Store OTP in global store for the worker
        OTP_STORE[user_id] = user_text
        await update.message.reply_text(
            "✅ OTP received. Processing...",
            reply_markup=main_inline_keyboard
        )
        # Worker will pick it up

# ------------------ SUPPORT SYSTEM ------------------
async def support_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        user_id = update.callback_query.from_user.id
        message = update.callback_query.message
    else:
        user_id = update.effective_user.id
        message = update.message

    user = get_user(user_id)
    if not user:
        await message.reply_text("Please /start the bot first.")
        return

    if user[6] == 0:
        await send_join_prompt(update, context)
        return

    context.user_data['support_mode'] = True
    await message.reply_text(
        "📩 *Support*\n\n"
        "Please type your question or message below.\n"
        "Our support team will get back to you shortly.\n\n"
        "You can send text messages only (no media).",
        parse_mode="Markdown",
        reply_markup=main_inline_keyboard
    )

async def support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('support_mode', False):
        user_id = update.effective_user.id
        user = get_user(user_id)
        name = user[7] if user else "Unknown"
        message = update.message.text

        for admin_id in ADMIN_USER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"📨 *New Support Message*\n"
                        f"From: User `{user_id}` ({name})\n"
                        f"Message:\n{message}\n\n"
                        f"Reply using: `/reply {user_id} <your message>`"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to send support message to admin {admin_id}: {e}")

        await update.message.reply_text(
            "✅ Your message has been sent to support.\n"
            "We'll get back to you as soon as possible.",
            reply_markup=main_inline_keyboard
        )
        context.user_data.pop('support_mode', None)

# ------------------ ADMIN REPLY ------------------
async def reply_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("You are not authorized to use this command.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /reply <user_id> <message>")
        return

    target_id = int(args[0])
    reply_text = " ".join(args[1:])

    user = get_user(target_id)
    if not user:
        await update.message.reply_text(f"User {target_id} not found.")
        return

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"📩 *Reply from Support:*\n\n{reply_text}",
            parse_mode="Markdown"
        )
        await update.message.reply_text(f"✅ Reply sent to user {target_id}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send reply: {e}")

# ------------------ ADMIN COMMANDS ------------------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("You are not authorized.")
        return
    total_users = get_total_users()
    total_credits = get_total_credits()
    text = (
        f"📊 *Bot Statistics*\n"
        f"Total Users: {total_users}\n"
        f"Total Credits in System: {total_credits}\n"
        f"Credits per Referral: {CREDITS_PER_REFERRAL}"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_inline_keyboard)

async def reset_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("You are not authorized.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /reset_credits <user_id>")
        return
    target_id = int(args[0])
    reset_user_credits(target_id)
    await update.message.reply_text(f"Credits for user {target_id} have been reset to 0.")

async def list_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("You are not authorized.")
        return

    with get_db_cursor() as c:
        c.execute("SELECT user_id, full_name, ref_code, credits, refer_count, join_date FROM users ORDER BY join_date DESC")
        rows = c.fetchall()

    if not rows:
        await update.message.reply_text("No users registered yet.")
        return

    content = "📋 FULL USER LIST (with Names)\n"
    content += "="*70 + "\n"
    content += f"{'User ID':<12} | {'Name':<20} | {'Ref Code':<8} | {'Credits':<7} | {'Refs':<4} | Join Date\n"
    content += "-"*70 + "\n"
    for uid, name, ref, credits, refs, joined in rows:
        name_display = (name or 'N/A')[:20]
        content += f"{uid:<12} | {name_display:<20} | {ref:<8} | {credits:<7} | {refs:<4} | {joined[:10]}\n"

    file_obj = io.BytesIO(content.encode('utf-8'))
    file_obj.name = "users_with_names.txt"
    await update.message.reply_document(
        document=file_obj,
        filename="users_with_names.txt",
        caption=f"✅ Total Users: {len(rows)}"
    )

    logger.info(f"Admin {user_id} requested all users list")

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("You are not authorized.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /removeuser <user_id>")
        return
    target_id = int(args[0])
    user = get_user(target_id)
    if not user:
        await update.message.reply_text(f"User {target_id} not found.")
        return
    delete_user(target_id)
    await update.message.reply_text(f"User {target_id} has been removed from the system.")

async def add_credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("You are not authorized.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /addcredits <user_id> <amount>")
        return
    try:
        target_id = int(args[0])
        amount = int(args[1])
    except ValueError:
        await update.message.reply_text("Please provide a valid user ID and amount (integer).")
        return

    if amount <= 0:
        await update.message.reply_text("Amount must be a positive integer.")
        return

    user = get_user(target_id)
    if not user:
        await update.message.reply_text(f"User {target_id} not found.")
        return

    add_credits(target_id, amount)
    new_credits = user[2] + amount
    await update.message.reply_text(
        f"✅ Added {amount} credits to user {target_id}.\n"
        f"New balance: {new_credits} credits."
    )

async def add_all_credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("You are not authorized.")
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /addallcredits <amount>")
        return
    try:
        amount = int(args[0])
    except ValueError:
        await update.message.reply_text("Please provide a valid integer amount.")
        return

    if amount <= 0:
        await update.message.reply_text("Amount must be a positive integer.")
        return

    total_users = get_total_users()
    if total_users == 0:
        await update.message.reply_text("No users registered to add credits.")
        return

    add_all_credits(amount)
    await update.message.reply_text(
        f"✅ Added {amount} credits to ALL {total_users} users.\n"
        f"Total credits distributed: {amount * total_users}."
    )

# ------------------ MAIN TEXT HANDLER ------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('support_mode', False):
        await support_message(update, context)
        return

    if context.user_data.get('gemini_step') in ['awaiting_otp', 'awaiting_mobile']:
        await gemini_mobile_handler(update, context)
        return

    await update.message.reply_text(
        "Please use the buttons below to navigate.",
        reply_markup=main_inline_keyboard
    )

# ------------------ SETUP COMMANDS ------------------
async def setup_commands(app):
    default_commands = [
        BotCommand("start", "🚀 Start the bot"),
        BotCommand("leaderboard", "🏆 Top 10 users"),
    ]
    admin_commands = [
        BotCommand("start", "🚀 Start the bot"),
        BotCommand("leaderboard", "🏆 Top 10 users"),
        BotCommand("stats", "📊 Bot statistics"),
        BotCommand("reset_credits", "🔄 Reset user credits"),
        BotCommand("allusers", "📋 Get full user list"),
        BotCommand("removeuser", "❌ Remove a user"),
        BotCommand("addcredits", "➕ Add credits to a user"),
        BotCommand("addallcredits", "➕ Add credits to all users"),
        BotCommand("reply", "💬 Reply to a user's support message"),
    ]
    await app.bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())
    for admin_id in ADMIN_USER_IDS:
        try:
            await app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logger.error(f"Failed to set admin commands for {admin_id}: {e}")
    logger.info("Bot commands configured.")

# ------------------ MAIN ------------------
def main():
    init_db()
    # Start worker pool
    for _ in range(MAX_WORKERS):
        asyncio.create_task(selenium_worker())

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset_credits", reset_credits))
    app.add_handler(CommandHandler("allusers", list_all_users))
    app.add_handler(CommandHandler("removeuser", remove_user))
    app.add_handler(CommandHandler("addcredits", add_credits_command))
    app.add_handler(CommandHandler("addallcredits", add_all_credits_command))
    app.add_handler(CommandHandler("reply", reply_user))

    app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.post_init = setup_commands

    logger.info("Bot started with Playwright and task queue.")
    app.run_polling()

if __name__ == "__main__":
    main()