import os
import sqlite3
import requests
import logging
import time
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

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
    return [row[0] for row in c.fetchall()]

# ====================== DexScreener 获取价格 + 市值 ======================
def get_token_info(symbol: str):
    """返回 price 和 fdv (市值代理)"""
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={symbol}", timeout=10)
        data = r.json()
        best = None
        for pair in data.get('pairs', []):
            if pair.get('chainId') == 'bsc' and pair.get('fdv', 0) > 5000:
                if best is None or pair.get('fdv', 0) > best.get('fdv', 0):
                    best = pair
        if best:
            return {
                "symbol": best['baseToken']['symbol'],
                "price": float(best.get('priceUsd', 0)),
                "fdv": float(best.get('fdv', 0))   # Fully Diluted Value ≈ 市值
            }
    except:
        pass
    return None

# ====================== 后台监控（重点监控市值） ======================
async def monitor_prices(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT chat_id, address FROM wallets")
    entries = c.fetchall()
    
    for chat_id, address in entries:
        try:
            tokens = get_wallet_tokens(address)
            for token in tokens:
                symbol = token['symbol']
                if symbol.upper() in ["BNB", "WBNB"]:
                    continue
                
                info = get_token_info(symbol)
                if not info or not info['fdv']:
                    continue
                
                current_fdv = info['fdv']
                current_price = info['price']
                
                # 获取上次记录
                c.execute("""SELECT fdv FROM price_history 
                           WHERE chat_id=? AND token=? 
                           ORDER BY timestamp DESC LIMIT 1""", (chat_id, symbol))
                last = c.fetchone()
                
                if last and last[0]:
                    last_fdv = last[0]
                    change = (current_fdv - last_fdv) / last_fdv * 100 if last_fdv > 0 else 0
                    
                    if abs(change) >= 10:   # 10% 市值异动
                        direction = "🚀 **市值大涨**" if change > 0 else "📉 **市值大跌**"
                        msg = f"{direction} **{symbol}**\n" \
                              f"当前价格: ${current_price:.6f}\n" \
                              f"当前市值: ${current_fdv:,.0f}\n" \
                              f"变化: {change:+.1f}%\n" \
                              f"钱包: `{address[:6]}...{address[-4:]}`\n" \
                              f"时间: {datetime.now().strftime('%H:%M')}"
                        
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                
                # 保存记录
                c.execute("INSERT INTO price_history (chat_id, token, price, fdv, timestamp) VALUES (?,?,?,?,?)",
                         (chat_id, symbol, current_price, current_fdv, int(time.time())))
            conn.commit()
        except Exception as e:
            logger.error(f"监控 {address} 出错: {e}")
    
    conn.close()

# ====================== BSC 查询 Token ======================
def get_wallet_tokens(address):
    # ...（保持你原来的 BSCScan 查询逻辑，返回 symbol 列表）...
    tokens = [{"symbol": "BNB"}]  # 示例，实际用你原来的代码
    try:
        # Token tx 发现
        url = f"https://api.bscscan.com/api?module=account&action=tokentx&address={address}&page=1&offset=100&sort=desc&apikey={BSCSCAN_API_KEY}"
        data = requests.get(url, timeout=10).json()
        seen = set()
        for tx in data.get("result", []):
            symbol = tx.get("tokenSymbol")
            if symbol and symbol not in seen:
                seen.add(symbol)
                tokens.append({"symbol": symbol})
    except:
        pass
    return tokens

# ====================== Telegram Handlers（简化版） ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
          [InlineKeyboardButton("👀 查看钱包", callback_data='view_wallet')]]
    await update.message.reply_text("✅ **钱包市值监控 Bot** 已启动\n\n每8分钟自动检查10%+异动", 
                                  reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# 其他 handlers（button_handler, message_handler）请提供你原来的代码，我帮你融合

def main():
    if not BOT_TOKEN or not BSCSCAN_API_KEY:
        logger.error("缺少环境变量")
        return
    
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    # app.add_handler(CallbackQueryHandler(button_handler))
    # app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(monitor_prices, 'interval', minutes=8)
    scheduler.start()
    
    logger.info("Bot 启动成功 | 市值监控已开启（10% 异动通知）")
    app.run_polling()

if __name__ == '__main__':
    main()
