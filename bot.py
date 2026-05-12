import os
import logging
import aiohttp
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# --- 配置 ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BSC_API_KEY = os.getenv("BSC_API_KEY")
ETH_API_KEY = os.getenv("ETH_API_KEY")
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# --- 钱包查询异步函数 ---
async def get_bsc_balance(address: str) -> float:
    url = f"https://api.bscscan.com/api/v2/account/balance?address={address}&apikey={BSC_API_KEY}"
    headers = {"Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.content_type != "application/json":
                    text = await resp.text()
                    logger.error(f"BSC 返回非JSON: {text}")
                    return 0
                data = await resp.json()
        except Exception as e:
            logger.error(f"BSC 请求异常: {e}")
            return 0
        try:
            return int(data["data"]["balance"]) / 10**18
        except Exception as e:
            logger.error(f"BSC 解析余额失败: {e}, data={data}")
            return 0

async def get_eth_balance(address: str) -> float:
    url = f"https://api.etherscan.io/api/v2/account/balance?address={address}&apikey={ETH_API_KEY}"
    headers = {"Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.content_type != "application/json":
                    text = await resp.text()
                    logger.error(f"ETH 返回非JSON: {text}")
                    return 0
                data = await resp.json()
        except Exception as e:
            logger.error(f"ETH 请求异常: {e}")
            return 0
        try:
            return int(data["data"]["balance"]) / 10**18
        except Exception as e:
            logger.error(f"ETH 解析余额失败: {e}, data={data}")
            return 0

# --- Telegram 命令 ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("查询钱包", callback_data="query_wallet")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("欢迎使用钱包监控 Bot", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    address = "0x24e44f4a9325734708d0b250629de7b9e0b3fe8f"  # 示例地址
    bsc_balance, eth_balance = await asyncio.gather(
        get_bsc_balance(address),
        get_eth_balance(address),
    )
    msg = f"钱包地址: {address}\nBSC: {bsc_balance:.6f} BNB\nETH: {eth_balance:.6f} ETH"
    await query.edit_message_text(msg)

# --- 主程序 ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("🚀 Bot 已启动")
    # 仅使用轮询，避免冲突
    app.run_polling()
