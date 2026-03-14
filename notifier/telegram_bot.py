"""
Telegram Bot 通知模块
- 发送分析报告
- 发送买卖信号（带确认按钮）
- 处理用户确认回调（记录到日志，不自动下单）
"""
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

logger = logging.getLogger(__name__)


async def send_message(text: str, parse_mode: str = "Markdown") -> None:
    """发送普通文本消息"""
    app = Application.builder().token(BOT_TOKEN).build()
    async with app:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode=parse_mode,
        )


async def send_signal(signal: dict) -> None:
    """
    发送买卖信号，附带确认/忽略按钮
    signal: {"ticker", "name", "price", "action": "BUY"|"SELL", "reasons": [...]}
    """
    action = signal["action"]
    emoji = "🟢 买入信号" if action == "BUY" else "🔴 卖出信号"
    reasons = "\n".join(f"  • {r}" for r in signal.get("reasons", []))

    text = (
        f"{emoji}\n"
        f"*{signal['name']}* ({signal['ticker']})\n"
        f"当前价格：{signal['price']}\n\n"
        f"触发原因：\n{reasons}\n\n"
        f"⚠️ 这是提醒，*不会自动下单*。请在你的券商 App 手动操作。"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 已知晓，我会处理", callback_data=f"ack_{signal['ticker']}_{action}"),
            InlineKeyboardButton("🔕 忽略此信号", callback_data=f"ignore_{signal['ticker']}_{action}"),
        ]
    ])

    app = Application.builder().token(BOT_TOKEN).build()
    async with app:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理按钮回调，记录用户决策"""
    query = update.callback_query
    await query.answer()

    data = query.data  # e.g. "ack_AAPL_SELL"
    parts = data.split("_", 2)
    action_type, ticker, trade_action = parts[0], parts[1], parts[2]

    if action_type == "ack":
        msg = f"✅ 已记录：你将处理 {ticker} 的 {trade_action} 信号"
        logger.info(f"User acknowledged: {ticker} {trade_action}")
    else:
        msg = f"🔕 已忽略 {ticker} 的 {trade_action} 信号"
        logger.info(f"User ignored: {ticker} {trade_action}")

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(msg)


def run_bot() -> None:
    """启动 Bot 监听（用于接收按钮回调）"""
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    logger.info("Bot 开始监听...")
    app.run_polling()
