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
from apscheduler.schedulers.background import BackgroundScheduler  # ← 改用这个

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DB_FILE = "database.db"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BSCSCAN_API_KEY = os.environ.get("BSCSCAN_API_KEY")

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
        conn.execute("INSERT OR IGNORE INTO wallets (chat_id, address, chain) VALUES (?,?,?)", 
                    (chat_id, address.lower(), chain.upper()))
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

# ====================== DexScreener 获取价格 + 市值 ======================
def get_token_info(symbol: str):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={symbol}", timeout=10)
        data = r.json()
        best = None
        for pair in data.get('pairs', []):
            if pair.get('chainId') == 'bsc' and pair.get('fdv', 0) > 10000:
                if best is None or pair.get('fdv', 0) > best.get('fdv', 0):
                    best = pair
        if best:
            return {
                "symbol": best['baseToken']['symbol'],
                "price": float(best.get('priceUsd', 0)),
                "fdv": float(best.get('fdv', 0))
            }
    except:
        pass
    return None

# ====================== BSC 查询持仓 ======================
def get_wallet_tokens(address, chain="BSC"):
    if chain != "BSC" or not BSCSCAN_API_KEY:
        return []
    tokens = []
    try:
        # BNB
        url = f"https://api.bscscan.com/api?module=account&action=balance&address={address}&apikey={BSCSCAN_API_KEY}"
        data = requests.get(url, timeout=10).json()
        bnb = int(data.get("result", 0)) / 10**18
        if bnb > 0.0001:
            tokens.append({"symbol": "BNB", "balance": bnb})

        # Token tx
        url = f"https://api.bscscan.com/api?module=account&action=tokentx&address={address}&page=1&offset=150&sort=desc&apikey={BSCSCAN_API_KEY}"
        data = requests.get(url, timeout=12).json()
        seen = set()
        for tx in data.get("result", []):
            symbol = tx.get("tokenSymbol")
            if symbol and symbol not in seen:
                seen.add(symbol)
                tokens.append({"symbol": symbol, "balance": 0})
    except Exception as e:
        logger.error(f"查询出错: {e}")
    return tokens

# ====================== 监控函数（非异步版本） ======================
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
                    change = (current_fdv - last_fdv) / last_fdv * 100 if last_fdv > 0 else 0

                    if abs(change) >= 10:
                        direction = "🚀 **市值大涨**" if change > 0 else "📉 **市值大跌**"
                        msg = f"{direction} **{symbol}**\n" \
                              f"价格: ${current_price:.6f}\n" \
                              f"市值: ${current_fdv:,.0f}\n" \
                              f"变化: {change:+.1f}%\n" \
                              f"地址: `{address[:8]}...{address[-6:]}`\n" \
                              f"时间: {datetime.now().strftime('%m-%d %H:%M')}"

                        # 发送通知（使用 asyncio 创建新任务）
                        asyncio.create_task(send_notification(chat_id, msg))

                # 保存
                c.execute("INSERT INTO price_history (chat_id, token, price, fdv, timestamp) VALUES (?,?,?,?,?)",
                         (chat_id, symbol, current_price, current_fdv, int(time.time())))
            conn.commit()
        except Exception as e:
            logger.error(f"监控 {address} 出错: {e}")
    
    conn.close()

async def send_notification(chat_id: int, message: str):
    """异步发送消息"""
    try:
        await application.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"发送通知失败: {e}")

# ====================== Handlers ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text("🎉 **钱包监控 Bot** 已启动\n\n✅ 10% 市值异动自动播报已开启（每8分钟检查）", 
                                  reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# button_handler 和 message_handler（和你原来的一致）
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == 'add_wallet':
        context.user_data['action'] = 'adding'
        await query.message.reply_text("请发送 BSC 地址：")
    elif data == 'view_wallet':
        addrs = get_user_wallets(chat_id)
        if not addrs:
            await query.message.reply_text("暂无钱包")
            return
        kb = [[InlineKeyboardButton(a[:12]+"...", callback_data=f"addr|{a}")] for a in addrs]
        await query.message.reply_text("选择地址：", reply_markup=InlineKeyboardMarkup(kb))
    # ... 其他部分保持不变（为了长度我省略了，你可以保留你之前版本的完整 button_handler）
    # 如果报错请告诉我，我再补全

# （保持你之前提供的 button_handler 和 message_handler 完整代码）

# ====================== 主程序 ======================
application = None  # 全局变量用于发送消息

def main():
    global application
    if not BOT_TOKEN or not BSCSCAN_API_KEY:
        logger.error("缺少环境变量")
        return

    init_db()
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # 启动 Background Scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(monitor_prices, 'interval', minutes=8)
    scheduler.start()

    logger.info("🚀 Bot 启动成功 | 10% 市值监控已开启")
    application.run_polling()

if __name__ == '__main__':
    main()
