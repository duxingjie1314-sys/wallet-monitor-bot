import os
import asyncio
import sqlite3
import requests
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "database.db"
MONITOR_INTERVAL = 90

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BSCSCAN_API_KEY = os.environ.get("BSCSCAN_API_KEY")
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")

COINGECKO_COINS = {}

# ====================== CoinGecko ======================
def load_coingecko_coins():
    global COINGECKO_COINS
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/list", timeout=15)
        if r.status_code == 200:
            COINGECKO_COINS = {coin['symbol'].lower(): coin['id'] for coin in r.json()}
            logger.info(f"✅ CoinGecko 加载 {len(COINGECKO_COINS)} 个币种")
    except Exception as e:
        logger.warning(f"CoinGecko 加载失败: {e}")

def get_token_price(symbol: str):
    if not symbol or not COINGECKO_COINS:
        return None
    coin_id = COINGECKO_COINS.get(symbol.lower())
    if not coin_id:
        return None
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd", timeout=8)
        return r.json().get(coin_id, {}).get("usd")
    except:
        return None

# ====================== 数据库 ======================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS wallets (
                    chat_id INTEGER, address TEXT, chain TEXT, last_value REAL DEFAULT 0,
                    UNIQUE(chat_id, address, chain))""")
    conn.commit()
    conn.close()

def add_wallet(chat_id, address, chain):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT INTO wallets (chat_id, address, chain) VALUES (?,?,?)", 
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

# ====================== 查询函数 ======================
def get_native_balance(address, chain):
    if chain != "BSC" or not BSCSCAN_API_KEY:
        return []
    try:
        url = f"https://api.bscscan.com/api?module=account&action=balance&address={address}&apikey={BSCSCAN_API_KEY}"
        data = requests.get(url, timeout=10).json()
        balance = int(data.get("result", 0)) / 10**18
        logger.info(f"BNB 余额: {balance:.4f}")
        return [{"symbol": "BNB", "balance": balance}] if balance > 0.001 else []
    except Exception as e:
        logger.error(f"BNB 查询失败: {e}")
        return []

def get_erc20_tokens(address):
    logger.info("查询 Token 持仓...")
    tokens = []
    try:
        url = f"https://api.bscscan.com/api?module=account&action=tokentx&address={address}&page=1&offset=100&sort=desc&apikey={BSCSCAN_API_KEY}"
        data = requests.get(url, timeout=12).json()
        result = data.get("result", [])
        logger.info(f"tokentx 返回 {len(result)} 条")

        token_dict = {}
        for tx in result:
            if isinstance(tx, dict):
                symbol = tx.get("tokenSymbol")
                contract = tx.get("contractAddress")
                if symbol and contract:
                    token_dict[symbol] = contract

        for symbol, contract in list(token_dict.items())[:25]:
            try:
                bal_url = f"https://api.bscscan.com/api?module=account&action=tokenbalance&contractaddress={contract}&address={address}&apikey={BSCSCAN_API_KEY}"
                bal_data = requests.get(bal_url, timeout=8).json()
                balance = int(bal_data.get("result", 0)) / 10**18
                if balance > 0.0001:
                    tokens.append({"symbol": symbol, "balance": balance})
                    logger.info(f"✅ 找到 {symbol} = {balance:.4f}")
            except:
                continue
        return tokens
    except Exception as e:
        logger.error(f"Token 查询失败: {e}")
        return []

def get_wallet_tokens(address, chain):
    tokens = get_native_balance(address, chain)
    if chain == "BSC":
        tokens.extend(get_erc20_tokens(address))
    logger.info(f"最终找到 {len(tokens)} 个资产")
    return tokens

# ====================== Handlers ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text("🎉 **钱包监控 Bot** 已就绪", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == 'add_wallet':
        context.user_data['action'] = 'adding'
        await query.message.reply_text("请发送 BSC 钱包地址：")

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

        if not tokens:
            await query.message.reply_text("⚠️ 未查询到持仓\n\n请使用**有 BNB** 的 BSC 地址测试")
            return

        msg = f"**{chain} 持仓**\n`{addr}`\n\n"
        total = 0
        for t in tokens[:15]:
            price = get_token_price(t['symbol'])
            usd = t['balance'] * (price or 0)
            total += usd
            msg += f"{t['symbol']}: {t['balance']:.4f} ≈ ${usd:.2f}\n"
        msg += f"\n**总价值 ≈ ${total:.2f}**"
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
        kb = [[InlineKeyboardButton(c, callback_data=f'addchain|{c}')] for c in ["BSC", "ETH", "SOL"]]
        await update.message.reply_text("请选择链：", reply_markup=InlineKeyboardMarkup(kb))

def main():
    if not BOT_TOKEN:
        logger.error("缺少 BOT_TOKEN")
        return
    if not BSCSCAN_API_KEY:
        logger.warning("⚠️ 未设置 BSCSCAN_API_KEY，BSC 查询可能失败")

    load_coingecko_coins()
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("🚀 Bot 已启动")
    app.run_polling()

if __name__ == '__main__':
    main()
