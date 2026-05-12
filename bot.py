import os
import sqlite3
import requests
import logging
import time
import asyncio
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "database.db"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BSCSCAN_API_KEY = os.environ.get("BSCSCAN_API_KEY")
PRICE_CACHE = {}
PRICE_CACHE_TTL = 60  # 秒

application = None  # 全局变量，用于发送通知

# ====================== 数据库 ======================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS wallets (
                    chat_id INTEGER, address TEXT, chain TEXT DEFAULT 'BSC',
                    UNIQUE(chat_id, address, chain))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS price_history (
                    chat_id INTEGER, token TEXT, price REAL, fdv REAL, timestamp INTEGER)""")
    conn.commit()
    conn.close()

def add_wallet(chat_id, address, chain="BSC"):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT OR IGNORE INTO wallets (chat_id, address, chain) VALUES (?,?,?)", 
            (chat_id, address.lower(), chain.upper())
        )
        conn.commit()
        return True
    except:
        return False
    finally:
        conn.close()

def get_user_wallets(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT address FROM wallets WHERE chat_id=?", (chat_id,))
    result = [row[0] for row in c.fetchall()]
    conn.close()
    return result

def get_address_chains(chat_id, address):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chain FROM wallets WHERE chat_id=? AND address=?", (chat_id, address))
    result = [row[0] for row in c.fetchall()]
    conn.close()
    return result

# ====================== DexScreener ======================
def get_token_info(symbol: str):
    try:
        symbol = symbol.upper().strip()
        # 缓存检查
        if symbol in PRICE_CACHE:
            cache = PRICE_CACHE[symbol]
            if time.time() - cache["time"] < PRICE_CACHE_TTL:
                return cache["data"]

        r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={symbol}", timeout=10)
        data = r.json()
        best = None
        for pair in data.get('pairs', []):
            if pair.get('chainId') == 'bsc':
                fdv = float(pair.get('fdv') or 0)
                if best is None or fdv > best.get('fdv', 0):
                    best = pair
        if best:
            result = {
                "symbol": best['baseToken']['symbol'],
                "price": float(best.get('priceUsd') or 0),
                "fdv": float(best.get('fdv') or 0),
                "liquidity": best.get('liquidity', {}).get('usd', 0)
            }
            PRICE_CACHE[symbol] = {"time": time.time(), "data": result}
            return result
    except Exception as e:
        logger.error(f"DEXScreener error: {e}")
    return None

# ====================== 获取钱包 Token ======================
def get_wallet_tokens(address, chain="BSC"):
    if chain != "BSC" or not BSCSCAN_API_KEY:
        return [{"symbol": "BSCSCAN_API_KEY_MISSING"}]

    tokens = []
    address = address.lower().strip()

    try:
        url = f"https://api.bscscan.com/api?module=account&action=balance&address={address}&apikey={BSCSCAN_API_KEY}"
        data = requests.get(url, timeout=12).json()
        status = data.get("status")
        result = data.get("result")
        if status == "1" and isinstance(result, str) and result.isdigit():
            bnb = int(result)/10**18
            if bnb >= 0.00005:
                tokens.append({"symbol": "BNB", "balance": round(bnb, 4)})
        else:
            logger.warning(f"BNB 查询失败 | Status: {status} | Result: {result}")
    except Exception as e:
        logger.error(f"BNB 查询异常: {e}")

    try:
        url = f"https://api.bscscan.com/api?module=account&action=tokentx&address={address}&page=1&offset=100&sort=desc&apikey={BSCSCAN_API_KEY}"
        data = requests.get(url, timeout=12).json()
        if data.get("status") == "1":
            seen = set()
            for tx in data.get("result", []):
                symbol = tx.get("tokenSymbol")
                if symbol and symbol not in seen and symbol.strip():
                    seen.add(symbol)
                    tokens.append({"symbol": symbol, "balance": 0})
        else:
            logger.warning(f"TokenTx 查询失败: {data.get('message')}")
    except Exception as e:
        logger.error(f"TokenTx 查询异常: {e}")

    if not tokens:
        tokens.append({"symbol": "NO_ASSETS_OR_API_LIMIT"})
    return tokens

# ====================== 监控函数 ======================
def monitor_prices():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT chat_id, address FROM wallets")
    entries = c.fetchall()
    for chat_id, address in entries:
        try:
            tokens = get_wallet_tokens(address, "BSC")
            for token in tokens:
                symbol = token['symbol']
                if symbol.upper() in ["BNB", "WBNB"]:
                    continue

                info = get_token_info(symbol)
                if not info or not info.get('fdv'):
                    continue

                current_price = info['price']
                current_fdv = info['fdv']

                c.execute("""SELECT fdv FROM price_history 
                           WHERE chat_id=? AND token=? 
                           ORDER BY timestamp DESC LIMIT 1""", (chat_id, symbol))
                last = c.fetchone()

                if last and last[0]:
                    last_fdv = last[0]
                    change = (current_fdv - last_fdv)/last_fdv*100 if last_fdv>0 else 0
                    if abs(change) >= 10:
                        direction = "🚀 **市值大涨**" if change>0 else "📉 **市值大跌**"
                        msg = f"{direction} **{symbol}**\n" \
                              f"💰 价格: ${current_price:.8f}\n" \
                              f"📊 市值: ${current_fdv:,.0f}\n" \
                              f"💧 流动性: ${info.get('liquidity',0):,.0f}\n" \
                              f"变化: {change:+.1f}%\n" \
                              f"地址: `{address[:8]}...{address[-6:]}`\n" \
                              f"时间: {datetime.now().strftime('%m-%d %H:%M')}"
                        asyncio.create_task(send_notification(chat_id, msg))

                # 保存记录
                c.execute(
                    "INSERT INTO price_history (chat_id, token, price, fdv, timestamp) VALUES (?,?,?,?,?)",
                    (chat_id, symbol, current_price, current_fdv, int(time.time()))
                )
            conn.commit()
        except Exception as e:
            logger.error(f"监控 {address} 出错: {e}")
    conn.close()

async def send_notification(chat_id: int, message: str):
    try:
        if application and application.bot:
            await application.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"发送通知失败: {e}")

# ====================== Telegram Handlers ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text(
        "🎉 **钱包监控 Bot** 已启动\n\n✅ 10% 市值异动自动播报已开启（每8分钟）", 
        reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == 'add_wallet':
        context.user_data['action'] = 'adding'
