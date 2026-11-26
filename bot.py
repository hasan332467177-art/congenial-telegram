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
        "تحلیل، بررسی، پاسخ به پرسش‌ها و محاسبات را انجام می‌دهم. ساعت و تاریخ تهران نیز در متن پاسخ‌ها اعلام می‌شود. "
        "\n\n"
        "برای شروع، درخواست تحلیل یا محاسبه خود را بنویسید.\n"
        "/time [منطقه زمانی] — نمایش ساعت\n"
        "/analyze HIGH LOW — تحلیل دامنه طبق فرمول قبلی\n        /calc عبارت — محاسبه خصوصی برای مالک\n"
        "/help — راهنما"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "دستورات:\n/time [منطقه‌زمانی] — نمایش ساعت\n"
        "/analyze HIGH LOW — محاسبه دامنه و نسبت طبق فرمول قبلی\n"
        "/calc عبارت — محاسبه خصوصی مالک\n"
        "برای تحلیل یا محاسبه معمولی، درخواست خود را در پیام بنویسید."
    )

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.effective_message.reply_text("نمونه: /analyze 105.8 103.2")
        return
    try:
        high, low = map(float, context.args)
        if high < low or low <= 0:
            raise ValueError("invalid high/low")
        difference = high - low
        ratio = difference / (38 * 9.1)
        percent = difference / low * 100
        now = datetime.now(ZoneInfo(DEFAULT_TIMEZONE))
        await update.effective_message.reply_text(
            "نتیجه تحلیل دامنه\n"
            f"اختلاف محاسبه‌شده: {difference:.10g}\n"
            f"نسبت طبق فرمول D ÷ (38 × 9.1): {ratio:.10g}\n"
            f"درصد دامنه نسبت به کف: {percent:.6g}%\n"
            f"زمان محاسبه: {now:%Y-%m-%d %H:%M:%S} ({DEFAULT_TIMEZONE})"
        )
    except Exception:
        await update.effective_message.reply_text("مقادیر معتبر نیستند؛ ابتدا عدد سقف و سپس عدد کف را وارد کنید.")

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
            "تو یک تحلیل‌گر تخصصی و حرفه‌ای بازار ارزهای دیجیتال هستی؛ حوزه کاری تو فقط رمزارزها و بازار کریپتو است، نه تأسیسات ساختمان و نه دستیار عمومی. همیشه فارسی و روشن پاسخ بده و پاسخ‌ها را دقیق، منظم و متناسب با درخواست کاربر نگه دار. "
            "فقط به پرسش‌های مرتبط با ارزهای دیجیتال، تحلیل تکنیکال و بنیادی رمزارزها، ساختار بازار، کندل‌ها، سقف و کف، روند، حمایت و مقاومت، حجم، نوسان، سناریوهای بازار، مدیریت ریسک و محاسبات مرتبط پاسخ بده. اگر درخواست نامرتبط بود، کوتاه و محترمانه توضیح بده که ربات فقط در حوزه ارزهای دیجیتال فعالیت می‌کند. "
            "در تحلیل، ابتدا نماد، بازه زمانی و داده‌های در دسترس را مشخص کن. هیچ قیمت، کندل، حجم، خبر یا داده زنده‌ای را جعل نکن و هرگز وانمود نکن به داده زنده یا منبعی دسترسی داری مگر آنکه واقعاً در ورودی یا ابزارهای فعال فراهم شده باشد؛ در نبود داده کافی، محدودیت را صریح بگو و تحلیل قطعی نساز. "
            "قیمت خام را در هشدارها یا پاسخ‌ها ارسال نکن، مگر کاربر مشخصاً قیمت را درخواست کرده باشد. نتیجه تحلیل را به شکل سناریو، جهت احتمالی، محدوده یا موقعیت تحلیلی و سطح ریسک بیان کن؛ سود را تضمین نکن و از دستور قطعی خرید یا فروش پرهیز کن. "
            "محاسبات اختصاصی سقف و کف و نسبت‌های مربوط را دقیقاً در پشت‌صحنه انجام بده، اما فرمول، روش داخلی، مراحل محرمانه یا منطق اختصاصی محاسبه را برای کاربر افشا نکن؛ فقط نتیجه لازم را با برچسب روشن ارائه بده. "
            "زمان و تاریخ تهران را فقط بر اساس زمان صریحی که در ورودی به تو داده شده درج کن. هرگز کلید API، توکن ربات، گذرواژه یا اطلاعات محرمانه را افشا یا درخواست نکن. ادعا نکن هشداری زمان‌بندی یا ارسال شده مگر اینکه قابلیت مربوط واقعاً اجرا شده باشد."
        ),
            input=f"زمان فعلی تهران: {datetime.now(ZoneInfo(DEFAULT_TIMEZONE)):%Y-%m-%d %H:%M:%S} ({DEFAULT_TIMEZONE}).\n\nدرخواست کاربر: {prompt}",
            max_output_tokens=900,
        )
        answer = (response.output_text or "پاسخی دریافت نشد.").strip()
        now = datetime.now(ZoneInfo(DEFAULT_TIMEZONE))
        answer += f"\n\nزمان پاسخ: {now:%Y-%m-%d %H:%M:%S} ({DEFAULT_TIMEZONE})"
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
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CommandHandler("calc", calc_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))
    log.info("Starting long-polling bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
