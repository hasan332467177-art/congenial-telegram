import ast
import asyncio
import logging
import os
import operator
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("telegram-assistant")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Asia/Tehran")
ALLOWED_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
       ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
       ast.UAdd: operator.pos}

def safe_calc(expression: str):
    if len(expression) > 100:
        raise ValueError("عبارت بیش از حد طولانی است.")
    tree = ast.parse(expression, mode="eval")
    def visit(node):
        if isinstance(node, ast.Expression): return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float): return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10: raise ValueError("توان خیلی بزرگ است.")
            value = OPS[type(node.op)](left, right)
            if abs(value) > 1e100: raise ValueError("نتیجه بیش از حد بزرگ است.")
            return value
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
            return OPS[type(node.op)](visit(node.operand))
        raise ValueError("فقط محاسبات عددی ساده مجاز است.")
    return visit(tree)

async def authorized(update: Update) -> bool:
    # Fail closed: only the configured owner in a one-to-one private chat.
    if not ALLOWED_USER_ID:
        return False
    user = update.effective_user
    chat = update.effective_chat
    return bool(
        user
        and chat
        and chat.type == "private"
        and str(user.id) == ALLOWED_USER_ID
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "به ربات خوش آمدید!\n\n"
        "برای گفت‌وگو درباره تأسیسات ساختمان و پرسش‌های تخصصی کارتان آماده‌ام. "
        "پاسخ‌گویی من به همین حوزه محدود است و به موضوعات خارج از این بحث پاسخ نمی‌دهم.\n\n"
        "برای شروع، سؤال کاری خود را بنویسید.\n"
        "/time [منطقه زمانی] — نمایش ساعت\n"
        "/calc عبارت — محاسبه ریاضی\n"
        "/help — راهنما"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("دستورات: /start ، /time [منطقه‌زمانی] ، /calc 31/38/9.1 ، /help\nبرای گفت‌وگو، متن دلخواهتان را ارسال کنید.")

async def time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    zone_name = " ".join(context.args) if context.args else DEFAULT_TIMEZONE
    try:
        now = datetime.now(ZoneInfo(zone_name))
        await update.effective_message.reply_text(f"{zone_name}\nتاریخ: {now:%Y-%m-%d}\nساعت: {now:%H:%M:%S}")
    except (ZoneInfoNotFoundError, ValueError):
        await update.effective_message.reply_text("منطقه زمانی معتبر نیست. نمونه: /time Asia/Tehran یا /time Europe/Berlin")

async def calc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update): return
    expression = " ".join(context.args)
    if not expression:
        await update.effective_message.reply_text("نمونه: /calc 31/38/9.1")
        return
    try:
        result = safe_calc(expression)
        await update.effective_message.reply_text(f"نتیجه: {result:.12g}" if isinstance(result, float) else f"نتیجه: {result}")
    except Exception:
        # Never echo the submitted expression or exception details.
        await update.effective_message.reply_text("عبارت محاسبه معتبر نیست یا قابل انجام نیست.")

async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    prompt = (message.text or "").strip()
    if not prompt: return
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    try:
        response = await client.responses.create(
            model=OPENAI_MODEL,
            instructions=(
            "You are a concise, accurate Persian-speaking assistant. Reply in the user language. "
            "Your ONLY subject area is building services engineering and the user professional work: HVAC, ventilation, heating, cooling, plumbing, drainage, fire protection, building electrical systems, energy efficiency, maintenance, technical design, calculations, and relevant standards. "
            "If a request is unrelated to these professional topics, politely refuse briefly and invite a question about building services engineering. Do not answer unrelated general-knowledge, entertainment, political, or personal requests. "
            "Never disclose, repeat, or ask the user to send API keys, bot tokens, passwords, or other credentials. Do not claim to schedule or send alerts. For current prices, explicitly say when live data is unavailable."
        ),
            input=prompt,
            max_output_tokens=900,
        )
        answer = (response.output_text or "پاسخی دریافت نشد.").strip()
        for start_at in range(0, len(answer), 4000):
            await message.reply_text(answer[start_at:start_at + 4000])
    except Exception:
        log.exception("OpenAI request failed")
        await message.reply_text("در ارتباط با سرویس هوش مصنوعی خطایی رخ داد. تنظیمات کلید API و وضعیت سرویس را بررسی کنید.")

async def post_init(application: Application):
    me = await application.bot.get_me()
    log.info("Bot started as @%s", me.username)


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("time", time_command))
    app.add_handler(CommandHandler("calc", calc_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))
    log.info("Starting long-polling bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
