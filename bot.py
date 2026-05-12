import os
import asyncio
import sqlite3
import logging
import httpx
from typing import List, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# 配置
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY") or os.environ.get("BSCSCAN_API_KEY")

# ====================== 主程序 ======================
async def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN 未设置")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    # 简单测试命令
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("✅ Bot 已正常启动！\n\n发送 /start 测试")

    application.add_handler(CommandHandler("start", start))

    logger.info("🚀 Bot 启动成功")
    await application.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    asyncio.run(main())
