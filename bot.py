import os
import asyncio
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackContext,
)

import httpx
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

async def start(update: Update, context: CallbackContext.DEFAULT_TYPE):
    await update.message.reply_text("Bot 已启动 🚀")

async def get_bsc_balance(address: str) -> int:
    url = f"https://api.bscscan.com/api/v2/account/balance?address={address}&apikey={BSCSCAN_API_KEY}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        data = resp.json()
        # V2 API 返回在 data.balance 或 data.result[0].balance
        try:
            balance = int(data["data"]["balance"])
        except (KeyError, TypeError):
            balance = 0
            logger.error(f"BSC 查询失败: {data}")
        return balance

async def get_eth_balance(address: str) -> int:
    url = f"https://api.etherscan.io/api?module=account&action=balance&address={address}&tag=latest&apikey={ETHERSCAN_API_KEY}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        data = resp.json()
        try:
            balance = int(data["result"])
        except (KeyError, TypeError):
            balance = 0
            logger.error(f"ETH 查询失败: {data}")
        return balance

async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    # 启动 Bot（异步轮询）
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
