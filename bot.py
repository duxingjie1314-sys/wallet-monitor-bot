import os
import sqlite3
import requests
import logging
import time
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
MORALIS_API_KEY = os.environ.get("MORALIS_API_KEY")   # 你已添加

# ====================== DexScreener 市值查询 ======================
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
    
    # 新增价格缓存表（以首次进入市值为基准）
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

# ====================== 持仓查询（保留你原有逻辑） ======================
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

        # Token 交易记录发现持仓
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

# ====================== 核心监控（每20秒 + 以首次市值为基准） ======================
def monitor_prices(context: ContextTypes.DEFAULT_TYPE = None):
    bot = context.application.bot if context and context.application else None
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

                # 读取缓存
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("SELECT initial_mc FROM price_cache WHERE ca=?", (ca,))
                row = c.fetchone()
                conn.close()

                if row is None:   # 首次检测，记录为初始市值
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

                        if bot:
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                        else:
                            logger.info(f"【提醒】{chat_id} - {info['symbol']} +{total_increase:.1f}%")

                # 更新 last_mc
                conn = sqlite3.connect(DB_FILE)
                conn.execute("UPDATE price_cache SET last_mc=?, last_time=? WHERE ca=?",
                             (current_mc, int(time.time()), ca))
                conn.commit()
                conn.close()
        except Exception as e:
            logger.error(f"监控出错 {address}: {e}")

# ====================== Bot Handlers（你原有部分基本保留） ======================
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

    elif data.startswith("addr|"):
        addr = data.split("|")[1]
        context.user_data['selected'] = addr
        chains = get_address_chains(chat_id, addr)
        kb = [[InlineKeyboardButton(c, callback_data=f"chain|{c}")] for c in chains]
        await query.message.reply_text(f"地址：`{addr}`\n选择链：", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data.startswith("chain|"):
        chain = data.split("|")[1]
        addr = context.user_data.get('selected')
        tokens = get_wallet_tokens(addr, chain)
        msg = f"**{chain} 持仓**\n`{addr}`\n\n"
        for t in tokens[:25]:
            msg += f"{t.get('symbol')}\n"
        msg += f"\n共发现 {len(tokens)} 个资产"
        await query.message.reply_text(msg, parse_mode='Markdown')

    elif data.startswith("addchain|"):
        chain = data.split("|")[1]
        addr = context.user_data.get('pending_address')
        if addr and add_wallet(chat_id, addr, chain):
            await query.message.reply_text(f"✅ 添加成功\n{addr} ({chain})")
        else:
            await query.message.reply_text("❌ 添加失败")
        context.user_data.clear()

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
    
    # 启动定时监控任务
    scheduler = BackgroundScheduler()
    scheduler.add_job(monitor_prices, 'interval', seconds=20, args=[None])
    scheduler.start()
    logger.info("✅ 20秒一次市值监控已启动（以首次入场市值为基准）")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    logger.info("🚀 Bot 已启动")
    app.run_polling()

if __name__ == '__main__':
    main()
