import os
import sqlite3
import logging
import aiohttp
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
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BSCSCAN_API_KEY = os.environ.get("BSCSCAN_API_KEY")
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")

SUPPORTED_CHAINS = {
    "BSC": {"api": "https://api.bscscan.com/api", "key": BSCSCAN_API_KEY},
    "ETH": {"api": "https://api.etherscan.io/api", "key": ETHERSCAN_API_KEY},
}

# ====================== 数据库 ======================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS wallets (
                    chat_id INTEGER, address TEXT, chain TEXT,
                    UNIQUE(chat_id, address, chain))""")
    conn.commit()
    conn.close()

def add_wallet(chat_id, address, chain):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT INTO wallets (chat_id, address, chain) VALUES (?,?,?)",
            (chat_id, address.lower(), chain.upper())
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
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

# ====================== 异步 HTTP ======================
async def fetch_json(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=12) as resp:
            return await resp.json()
    except Exception as e:
        logger.error(f"HTTP 请求失败 {url} : {e}")
        return {}

# ====================== 查询钱包 ======================
async def get_wallet_tokens(address, chain):
    chain = chain.upper()
    if chain not in SUPPORTED_CHAINS:
        return []

    api_info = SUPPORTED_CHAINS[chain]
    if not api_info['key']:
        logger.warning(f"{chain} API Key 未设置")
        return []

    tokens = []

    params_balance = {
        "module": "account",
        "action": "balance",
        "address": address,
        "apikey": api_info['key']
    }

    params_token = {
        "module": "account",
        "action": "tokentx",
        "address": address,
        "page": 1,
        "offset": 150,
        "sort": "desc",
        "apikey": api_info['key']
    }

    async with aiohttp.ClientSession() as session:
        # 查询主币余额
        data = await fetch_json(session, api_info['api'], params=params_balance)
        if data.get("status") == "1" and "result" in data:
            try:
                balance = int(data["result"]) / 10**18
                symbol = {"BSC": "BNB", "ETH": "ETH"}.get(chain, "NATIVE")
                if balance > 0.0001:
                    tokens.append({"symbol": symbol, "balance": balance})
            except ValueError:
                logger.error(f"{chain} 主币余额转换异常: {data['result']}")
        else:
            logger.warning(f"{chain} 查询主币余额失败: {data}")

        # 查询 ERC20 token 交易记录
        data = await fetch_json(session, api_info['api'], params=params_token)
        if data.get("status") == "1" and "result" in data:
            result = data["result"]
            seen = set()
            for tx in result:
                sym = tx.get("tokenSymbol")
                if sym and sym not in seen:
                    seen.add(sym)
                    tokens.append({"symbol": sym, "balance": 0})
            logger.info(f"[{chain}] {address} 发现 {len(tokens)} 个资产")
        else:
            logger.warning(f"{chain} 查询 ERC20 token 失败: {data}")

    return tokens

# ====================== Bot Handlers ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text("🎉 **钱包监控 Bot**",
                                    reply_markup=InlineKeyboardMarkup(kb),
                                    parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == 'add_wallet':
        context.user_data['action'] = 'adding'
        await query.message.reply_text("请发送钱包地址（BSC 或 ETH）：")

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
        await query.message.reply_text(f"地址：`{addr}`\n选择链：",
                                       reply_markup=InlineKeyboardMarkup(kb),
                                       parse_mode='Markdown')

    elif data.startswith("chain|"):
        chain = data.split("|")[1]
        addr = context.user_data.get('selected')
        tokens = await get_wallet_tokens(addr, chain)

        if not tokens:
            await query.message.reply_text("未查询到资产")
            return

        msg = f"**{chain} 持仓**\n`{addr}`\n\n"
        for t in tokens[:25]:
            msg += f"{t['symbol']}\n"
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
        kb = [[InlineKeyboardButton(c, callback_data=f'addchain|{c}')] for c in SUPPORTED_CHAINS.keys()]
        await update.message.reply_text("请选择链：", reply_markup=InlineKeyboardMarkup(kb))

# ====================== 主函数 ======================
def main():
    if not BOT_TOKEN:
        logger.error("缺少 BOT_TOKEN")
        return
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    logger.info("🚀 Bot 已启动")
    app.run_polling()

if __name__ == '__main__':
    main()
