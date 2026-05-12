import os
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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DB_FILE = "database.db"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BSCSCAN_API_KEY = os.environ.get("BSCSCAN_API_KEY")

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

# ====================== V2 接口查询 ======================
def get_wallet_tokens(address, chain):
    if chain != "BSC" or not BSCSCAN_API_KEY:
        return []
    tokens = []
    try:
        # V2 - 原生币 BNB
        url = f"https://api.etherscan.io/v2/api?chainid=56&module=account&action=balance&address={address}&apikey={BSCSCAN_API_KEY}"
        data = requests.get(url, timeout=10).json()
        bnb = int(data.get("result", 0)) / 10**18
        if bnb > 0.001:
            tokens.append({"symbol": "BNB", "balance": bnb})
            logger.info(f"✅ BNB: {bnb:.4f}")

        # V2 - Token 交易记录
        url = f"https://api.etherscan.io/v2/api?chainid=56&module=account&action=tokentx&address={address}&page=1&offset=80&sort=desc&apikey={BSCSCAN_API_KEY}"
        data = requests.get(url, timeout=12).json()
        result = data.get("result", [])
        logger.info(f"tokentx 返回 {len(result)} 条记录")

        token_dict = {}
        for tx in result:
            if isinstance(tx, dict):
                symbol = tx.get("tokenSymbol")
                contract = tx.get("contractAddress")
                if symbol and contract:
                    token_dict[symbol] = contract

        for symbol, contract in list(token_dict.items())[:20]:
            try:
                bal_url = f"https://api.etherscan.io/v2/api?chainid=56&module=account&action=tokenbalance&contractaddress={contract}&address={address}&apikey={BSCSCAN_API_KEY}"
                bal_data = requests.get(bal_url, timeout=8).json()
                balance = int(bal_data.get("result", 0)) / 10**18
                if balance > 0.0001:
                    tokens.append({"symbol": symbol, "balance": balance})
                    logger.info(f"✅ 找到 {symbol} = {balance:.4f}")
            except:
                continue
    except Exception as e:
        logger.error(f"查询异常: {e}")
    
    logger.info(f"最终找到 {len(tokens)} 个资产")
    return tokens

# ====================== Bot ======================
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
            await query.message.reply_text("⚠️ 未查询到持仓\n请使用有 BNB 的活跃 BSC 地址")
            return

        msg = f"**{chain} 持仓**\n`{addr}`\n\n"
        for t in tokens[:15]:
            msg += f"{t['symbol']}: {t['balance']:.4f}\n"
        msg += f"\n共找到 {len(tokens)} 个资产"
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
