import os
import sqlite3
import requests
import logging
import time
import asyncio
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DB_FILE = "database.db"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BSCSCAN_API_KEY = os.environ.get("BSCSCAN_API_KEY")
MORALIS_API_KEY = os.environ.get("MORALIS_API_KEY")

# ====================== DexScreener 市值 ======================
def get_market_cap(ca: str):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("pairs"):
            pair = data["pairs"][0]
            return {
                "mc": pair.get("fdv") or pair.get("marketCap", 0),
                "symbol": pair.get("baseToken", {}).get("symbol", "Unknown"),
                "name": pair.get("baseToken", {}).get("name", "")
            }
    except:
        pass
    return None

# ====================== 数据库 ======================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS wallets (
                    chat_id INTEGER, address TEXT, chain TEXT,
                    UNIQUE(chat_id, address, chain))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS price_cache (
                    ca TEXT PRIMARY KEY, 
                    initial_mc REAL, 
                    last_mc REAL,
                    last_time INTEGER)""")
    conn.commit()
    conn.close()

def add_wallet(chat_id, address, chain):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT OR IGNORE INTO wallets (chat_id, address, chain) VALUES (?,?,?)",
                     (chat_id, address.lower(), chain.upper()))
        conn.commit()
        return True
    finally:
        conn.close()

def get_user_wallets(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT address, chain FROM wallets WHERE chat_id=?", (chat_id,))
    result = c.fetchall()
    conn.close()
    return result

# ====================== 持仓查询 ======================
def get_wallet_tokens(address, chain):
    if chain != "BSC" or not BSCSCAN_API_KEY:
        return []
    tokens = []
    try:
        url = f"https://api.bscscan.com/api?module=account&action=tokentx&address={address}&page=1&offset=100&sort=desc&apikey={BSCSCAN_API_KEY}"
        data = requests.get(url, timeout=12).json()
        seen = set()
        for tx in data.get("result", []):
            symbol = tx.get("tokenSymbol")
            ca = tx.get("contractAddress")
            if symbol and ca and symbol not in seen:
                seen.add(symbol)
                tokens.append({"ca": ca, "symbol": symbol})
    except Exception as e:
        logger.error(f"持仓查询异常: {e}")
    return tokens

# ====================== 核心监控 ======================
def monitor_prices(context: ContextTypes.DEFAULT_TYPE = None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chat_id, address, chain FROM wallets")
    wallets = c.fetchall()
    conn.close()

    for chat_id, address, chain in wallets:
        try:
            tokens = get_wallet_tokens(address, chain)
            for token in tokens:
                ca = token.get("ca")
                if not ca:
                    continue
                info = get_market_cap(ca)
                if not info or info["mc"] < 5000:
                    continue

                current_mc = info["mc"]
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("SELECT initial_mc FROM price_cache WHERE ca=?", (ca,))
                row = c.fetchone()
                conn.close()

                if row is None:
                    conn = sqlite3.connect(DB_FILE)
                    conn.execute("INSERT OR REPLACE INTO price_cache (ca, initial_mc, last_mc, last_time) VALUES (?,?,?,?)",
                                 (ca, current_mc, current_mc, int(time.time())))
                    conn.commit()
                    conn.close()
                    continue

                initial_mc = row[0]
                if initial_mc > 0:
                    total_increase = (current_mc - initial_mc) / initial_mc * 100
                    if total_increase >= 10:
                        level = int(total_increase // 10) * 10
                        msg = f"🚀 **相对入场市值上涨 {level}%**！\n\n" \
                              f"代币：**{info['symbol']}**\n" \
                              f"涨幅：**+{total_increase:.1f}%**\n" \
                              f"当前市值：**${current_mc:,.0f}**\n" \
                              f"CA：`{ca}`\n" \
                              f"链：{chain}\n" \
                              f"地址：`{address[:8]}...`"

                        if context and context.application:
                            asyncio.create_task(context.application.bot.send_message(
                                chat_id=chat_id, text=msg, parse_mode='Markdown'
                            ))
        except Exception as e:
            logger.error(f"监控出错: {e}")

# ====================== Bot 命令 ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ 添加地址", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 我的地址", callback_data='view_wallets')]
    ]
    await update.message.reply_text(
        "🚀 **持仓市值监控 Bot**\n\n每20秒检测 • 每涨10%提醒", 
        reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == 'add_wallet':
        context.user_data['action'] = 'adding'
        await query.message.reply_text("请发送钱包地址：")

    elif data == 'view_wallets':
        wallets = get_user_wallets(chat_id)
        if not wallets:
            await query.message.reply_text("暂无监控地址")
            return
        text = "**我的监控地址：**\n\n"
        for addr, ch in wallets:
            text += f"• `{addr}` ({ch})\n"
        await query.message.reply_text(text, parse_mode='Markdown')

    elif data.startswith('chain|'):
        chain = data.split('|')[1]
        addr = context.user_data.get('pending_address')
        if addr and add_wallet(chat_id, addr, chain):
            await query.message.reply_text(f"✅ 添加成功！\n链：{chain}\n地址：`{addr}`\n\n开始监控...", parse_mode='Markdown')
            logger.info(f"用户 {chat_id} 添加了 {chain} 地址")
        else:
            await query.message.reply_text("❌ 添加失败")
        context.user_data.clear()

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('action') == 'adding':
        address = update.message.text.strip()
        context.user_data['pending_address'] = address
        kb = [
            [InlineKeyboardButton("BSC", callback_data='chain|BSC')],
            [InlineKeyboardButton("ETH", callback_data='chain|ETH')],
            [InlineKeyboardButton("SOL", callback_data='chain|SOL')]
        ]
        await update.message.reply_text(f"地址已接收：`{address}`\n请选择链：", 
                                       reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# ====================== 启动 ======================
def main():
    if not BOT_TOKEN:
        logger.error("缺少 BOT_TOKEN")
        return

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    scheduler = BackgroundScheduler()
    scheduler.add_job(monitor_prices, 'interval', seconds=20, args=[None])
    scheduler.start()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("🚀 Bot 已启动")
    app.run_polling()

if __name__ == '__main__':
    main()
