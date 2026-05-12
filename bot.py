import os
import asyncio
import logging
from typing import List, Dict

import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# -------------------------------
# 日志配置
# -------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -------------------------------
# 环境变量
# -------------------------------
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("Telegram Bot Token 没有设置，请在环境变量 BOT_TOKEN 中配置")

# -------------------------------
# BSC & ETH 查询函数
# -------------------------------

async def fetch_bsc_balance(address: str) -> Dict:
    """查询 BSC V2 API 帐号余额"""
    url = f"https://api.bscscan.com/api/v2/account/balance?address={address}&apikey={BSCSCAN_API_KEY}"
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        try:
            data = resp.json()
            # V2 API 返回格式通常在 data.balance
            balance = int(data.get("data", {}).get("balance", "0"))
            return {"address": address, "balance": balance}
        except Exception as e:
            logger.error("BSC 查询失败: %s", e)
            return {"address": address, "balance": 0}

async def fetch_eth_balance(address: str) -> Dict:
    """查询 ETH V2 API 帐号余额"""
    url = f"https://api.etherscan.io/api/v2/account/balance?address={address}&apikey={ETHERSCAN_API_KEY}"
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        try:
            data = resp.json()
            balance = int(data.get("data", {}).get("balance", "0"))
            return {"address": address, "balance": balance}
        except Exception as e:
            logger.error("ETH 查询失败: %s", e)
            return {"address": address, "balance": 0}

# -------------------------------
# Telegram Bot Command
# -------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("欢迎使用钱包监控 Bot! 发送 /balance <地址> 查询 BSC & ETH 余额。")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) == 0:
        await update.message.reply_text("请提供钱包地址，例如：/balance 0x123...")
        return

    address = context.args[0]

    # 并行查询 BSC & ETH
    bsc_task = fetch_bsc_balance(address)
    eth_task = fetch_eth_balance(address)
    results: List[Dict] = await asyncio.gather(bsc_task, eth_task)

    msg = f"地址: {address}\n"
    for res in results:
        chain = "BSC" if res["address"] == address else "ETH"
        bal = res["balance"] / 1e18  # 转为标准单位
        msg += f"{chain} 余额: {bal:.6f}\n"

    await update.message.reply_text(msg)

# -------------------------------
# 主函数
# -------------------------------

async def main():
    # 单实例 polling 避免 getUpdates 冲突
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # 添加命令
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))

    # 启动 Bot
    logger.info("🚀 Bot 已启动")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
