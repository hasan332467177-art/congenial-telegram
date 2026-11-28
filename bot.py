import ast
import asyncio
import logging
import operator
import os
import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("crypto-telegram-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
TIMEZONE_NAME = os.getenv("DEFAULT_TIMEZONE", "Asia/Tehran")
ALERT_TIMES = [x.strip() for x in os.getenv("ALERT_TIMES", "").split(",") if x.strip()]
DB_PATH = os.getenv("BOT_DB_PATH", "bot_users.sqlite3")
HISTORY_LIMIT = int(os.getenv("CHAT_HISTORY_LIMIT", "16"))
client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
awaiting_calculation = set()

OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def zone():
    return ZoneInfo(TIMEZONE_NAME)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY)")
    conn.execute("""CREATE TABLE IF NOT EXISTS conversation_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_chat_id ON conversation_history(chat_id, id)")
    conn.commit()
    return conn


def remember_chat(chat_id):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO subscribers(chat_id) VALUES (?)", (chat_id,))


def subscriber_ids():
    with db() as conn:
        return [row[0] for row in conn.execute("SELECT chat_id FROM subscribers").fetchall()]


def get_history(chat_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM conversation_history WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, HISTORY_LIMIT),
        ).fetchall()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def save_message(chat_id, role, content):
    with db() as conn:
        conn.execute(
            "INSERT INTO conversation_history(chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, datetime.now(zone()).isoformat()),
        )
        conn.execute(
            """DELETE FROM conversation_history WHERE chat_id=? AND id NOT IN
               (SELECT id FROM conversation_history WHERE chat_id=? ORDER BY id DESC LIMIT ?)""",
            (chat_id, chat_id, HISTORY_LIMIT),
        )


def safe_calc(expression):
    if len(expression) > 100:
        raise ValueError("expression too long")
    tree = ast.parse(expression, mode="eval")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("power too large")
            value = OPS[type(node.op)](left, right)
            if abs(value) > 1e100:
                raise ValueError("result too large")
            return value
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
            return OPS[type(node.op)](visit(node.operand))
        raise ValueError("unsupported expression")

    return visit(tree)


def format_number(value):
    return f"{value:.12g}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    remember_chat(chat_id)
    await update.effective_message.reply_text(
        "سلام، به ربات تحلیل ارز دیجیتال خوش آمدید.\n"
        "می‌توانید درباره رمزارزها گفتگو کنید، تحلیل بخواهید یا محاسبه انجام دهید.\n"
        "ربات زمینه گفتگوی اخیر شما را برای ادامه تعامل نگه می‌دارد.\n\n"
        "/analyze سقف کف — محاسبه دامنه\n"
        "/calc عدد یا عبارت — محاسبه\n"
        "/time — نمایش ساعت تهران\n"
        "/reset — پاک‌کردن حافظه گفتگوی همین چت\n"
        "/help — راهنما"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_chat(update.effective_chat.id)
    await update.effective_message.reply_text(
        "این ربات درباره ارزهای دیجیتال و محاسبات مرتبط پاسخ می‌دهد.\n"
        "/analyze HIGH LOW — تحلیل دامنه با دریافت سقف و کف\n"
        "/calc عدد — محاسبه عددی\n"
        "/time — نمایش تاریخ و ساعت تهران\n"
        "/reset — پاک‌کردن زمینه گفتگوی ذخیره‌شده"
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with db() as conn:
        conn.execute("DELETE FROM conversation_history WHERE chat_id=?", (chat_id,))
    awaiting_calculation.discard(chat_id)
    await update.effective_message.reply_text("زمینه گفتگوی ذخیره‌شده این چت پاک شد.")


async def time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_chat(update.effective_chat.id)
    now = datetime.now(zone())
    await update.effective_message.reply_text(f"زمان تهران: {now:%Y-%m-%d %H:%M:%S}")


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_chat(update.effective_chat.id)
    if len(context.args) != 2:
        await update.effective_message.reply_text("نمونه: /analyze 105.8 103.2")
        return
    try:
        high, low = map(float, context.args)
        if high < low or low <= 0:
            raise ValueError("invalid range")
        result = (high - low) / 38 / 9.1
        percent = (high - low) / low * 100
        await update.effective_message.reply_text(
            "نتیجه محاسبه دامنه:\n"
            f"خروجی محاسبه: {format_number(result)}\n"
            f"درصد دامنه: {percent:.6g}%"
        )
    except Exception:
        await update.effective_message.reply_text("ورودی معتبر نیست؛ ابتدا سقف و سپس کف را وارد کنید.")


async def calc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_chat(update.effective_chat.id)
    expression = " ".join(context.args).strip()
    if not expression:
        await update.effective_message.reply_text("یک عدد یا عبارت عددی وارد کنید.")
        return
    try:
        value = safe_calc(expression)
        result = value / 38 / 9.1
        await update.effective_message.reply_text(f"نتیجه: {format_number(result)}")
    except Exception:
        await update.effective_message.reply_text("محاسبه انجام نشد؛ ورودی عددی معتبر بفرستید.")


async def scheduled_alert(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(zone())
    if now.weekday() not in (0, 1, 2, 3):
        return
    message = (
        f"هشدار خودکار ارز دیجیتال\n"
        f"زمان اعلام: {now:%Y-%m-%d %H:%M} (تهران)\n"
        "چه عددی می‌خواهی برایت محاسبه کنم؟"
    )
    for chat_id in subscriber_ids():
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
            awaiting_calculation.add(chat_id)
        except Exception:
            log.exception("Could not send scheduled alert to chat %s", chat_id)


def agent_instructions():
    return (
        "تو عامل گفتگویی تخصصی برای تحلیل و محاسبات بازار ارزهای دیجیتال هستی. "
        "مدل تعامل تو چهار مرحله دارد: "
        "۱) آگاهی: منظور، هدف، دارایی و محدودیت‌های مخاطب را از پیام و سابقه همین گفتگو تشخیص بده؛ "
        "۲) رفتار و بیان: لحن روشن، محترمانه و متناسب انتخاب کن و در صورت ابهام فقط سؤال ضروری بپرس؛ "
        "۳) پردازش: داده‌های موجود را تحلیل کن، محاسبات را دقیق انجام بده، فرض‌ها و عدم قطعیت را بررسی کن؛ "
        "۴) بازسازی: نتیجه را با زمینه قبلی تطبیق بده و در پاسخ‌های بعدی از اطلاعات همین گفتگو استفاده کن. "
        "در پایان پاسخ، اگر اقدام بعدی مشخص و مفید است آن را کوتاه پیشنهاد بده. "
        "فقط درباره رمزارزها و محاسبات مرتبط پاسخ تخصصی بده؛ درباره موضوعات نامرتبط وارد پاسخ تخصصی نشو. "
        "داده زنده، قیمت، کندل، حجم یا خبر را جعل نکن و ادعای دسترسی زنده نداشته باش. "
        "اگر داده بازار لازم است و در اختیار تو نیست، صریحاً آن را از مخاطب بخواه یا محدودیت را اعلام کن. "
        "قیمت خام را در هشدارهای زمان‌بندی‌شده ارسال نکن. "
        "روش محاسبات داخلی را محرمانه نگه دار و فقط نتیجه لازم را ارائه کن. "
        "سود را تضمین نکن، دستور قطعی خرید یا فروش نده و ریسک را روشن بیان کن. "
        "بدون ابزار و مجوز صریح، هیچ معامله یا اقدام مالی انجام نده."
    )


async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat_id = message.chat_id
    remember_chat(chat_id)
    prompt = (message.text or "").strip()
    if not prompt:
        return

    if chat_id in awaiting_calculation:
        try:
            value = safe_calc(prompt)
            result = value / 38 / 9.1
            awaiting_calculation.discard(chat_id)
            await message.reply_text(f"نتیجه محاسبه: {format_number(result)}")
            return
        except Exception:
            await message.reply_text("لطفاً یک عدد یا عبارت عددی معتبر بفرستید.")
            return

    if client is None:
        await message.reply_text("بخش گفتگوی هوشمند پیکربندی نشده است؛ دستورات محاسبه و ساعت فعال‌اند.")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        history = get_history(chat_id)
        input_items = history + [{"role": "user", "content": prompt}]
        response = await client.responses.create(
            model=OPENAI_MODEL,
            instructions=agent_instructions(),
            input=input_items,
            max_output_tokens=900,
        )
        answer = (response.output_text or "پاسخی دریافت نشد.").strip()
        if not answer:
            answer = "پاسخی دریافت نشد."
        save_message(chat_id, "user", prompt)
        save_message(chat_id, "assistant", answer)
        answer += f"\n\nزمان پاسخ: {datetime.now(zone()):%Y-%m-%d %H:%M:%S} (تهران)"
        for offset in range(0, len(answer), 4000):
            await message.reply_text(answer[offset:offset + 4000])
    except Exception:
        log.exception("AI response failed")
        await message.reply_text("در حال حاضر پاسخ هوشمند در دسترس نیست؛ دستورات محاسبه همچنان فعال‌اند.")


async def post_init(application: Application):
    me = await application.bot.get_me()
    log.info("Bot started as @%s", me.username)
    for item in ALERT_TIMES:
        try:
            hour_text, minute_text = item.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("time out of range")
            application.job_queue.run_daily(
                scheduled_alert,
                time=time(hour, minute, tzinfo=zone()),
                days=(0, 1, 2, 3),
                name=f"crypto-alert-{hour:02d}{minute:02d}",
            )
        except Exception:
            log.error("Ignoring invalid ALERT_TIMES entry: %s", item)
    log.info("Configured %s alert times", len(ALERT_TIMES))


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("time", time_command))
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CommandHandler("calc", calc_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))
    log.info("Starting public long-polling bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
