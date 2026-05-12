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

# ====================== 持仓查询 ======================
def get_wallet_tokens(address, chain):
    if chain != "BSC" or not BSCSCAN_API_KEY:
        return []
    tokens = []
    try:
        # BNB 余额
        url = f"https://api.bscscan.com/api?module=account&action=balance&address={address}&apikey={BSCSCAN_API_KEY}"
        data = requests.get(url, timeout=10).json()
        bnb = int(data.get("result", 0)) / 10**18
        if bnb > 0.0001:
            tokens.append({"ca": "BNB", "symbol": "BNB"})

        # Token 交易
        url = f"https://api.bscscan.com/api?module=account&action=tokentx&address={address}&page=1&offset=150&sort=desc&apikey={BSCSCAN_API_KEY}"
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

# ====================== 核心监控（已修复 await 问题） ======================
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
                if not ca or ca == "BNB":
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
                              f"初始市值：${initial_mc:,.0f}\n" \
                              f"当前市值：**${current_mc:,.0f}**\n" \
                              f"CA：`{ca}`\n" \
                              f"地址：`{address[:8]}...{address[-6:]}`\n" \
                              f"时间：{datetime.now().strftime('%H:%M:%S')}"

                        # 异步发送消息（修复 await 错误）
                        if context and context.application:
                            asyncio.create_task(context.application.bot.send_message(
                                chat_id=chat_id, 
                                text=msg, 
                                parse_mode='Markdown'
                            ))
                        else:
                            logger.info(f"【提醒】{chat_id} - {info['symbol']} +{total_increase:.1f}%")

                # 更新缓存
                conn = sqlite3.connect(DB_FILE)
                conn.execute("UPDATE price_cache SET last_mc=?, last_time=? WHERE ca=?",
                             (current_mc, int(time.time()), ca))
                conn.commit()
                conn.close()
        except Exception as e:
            logger.error(f"监控出错 {address}: {e}")

# ====================== Bot Handlers ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text(
        "🚀 **持仓市值监控 Bot**\n\n"
        "• 每20秒检测一次\n"
        "• 以首次进入市值为基准\n"
        "• 每涨10%推送提醒", 
        reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown'
    )

# （你的其他按钮处理逻辑保持不变，下面简化处理）
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # ... 保持你原来的按钮逻辑 ...

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('action') == 'adding':
        context.user_data['pending_address'] = update.message.text.strip()
        context.user_data['action'] = 'choosing'
        kb = [[InlineKeyboardButton("BSC", callback_data='addchain|BSC')]]
        await update.message.reply_text("请选择链：", reply_markup=InlineKeyboardMarkup(kb))

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
    logger.info("✅ 20秒监控任务已启动")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    logger.info("🚀 Bot 已启动")
    app.run_polling()

if __name__ == '__main__':
    main()
