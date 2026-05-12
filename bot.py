import os
import asyncio
import sqlite3
import logging
import httpx
from typing import List, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
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

# ====================== 配置 ======================
DB_FILE = "database.db"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY") or os.environ.get("BSCSCAN_API_KEY")

COINGECKO_COINS = {}

BASE_URL = "https://api.etherscan.io/v2/api"

CHAIN_CONFIG = {
    "BSC": {"chainid": 56, "symbol": "BNB"},
    "ETH": {"chainid": 1,  "symbol": "ETH"},
}

# ====================== CoinGecko ======================
async def load_coingecko_coins():
    global COINGECKO_COINS
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.coingecko.com/api/v3/coins/list")
            if r.status_code == 200:
                COINGECKO_COINS = {coin['symbol'].lower(): coin['id'] for coin in r.json()}
                logger.info(f"✅ CoinGecko 加载 {len(COINGECKO_COINS)} 个币种")
    except Exception as e:
        logger.warning(f"CoinGecko 加载失败: {e}")

async def get_token_price(symbol: str) -> float | None:
    if not symbol or not COINGECKO_COINS:
        return None
    coin_id = COINGECKO_COINS.get(symbol.lower())
    if not coin_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd")
            return r.json().get(coin_id, {}).get("usd")
    except:
        return None


# ====================== 数据库 ======================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS wallets (
                    chat_id INTEGER, 
                    address TEXT, 
                    chain TEXT,
                    UNIQUE(chat_id, address, chain))""")
    conn.commit()
    conn.close()

def add_wallet(chat_id: int, address: str, chain: str) -> bool:
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

def get_user_wallets(chat_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT address FROM wallets WHERE chat_id=?", (chat_id,))
    result = [row[0] for row in c.fetchall()]
    conn.close()
    return result

def get_address_chains(chat_id: int, address: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chain FROM wallets WHERE chat_id=? AND address=?", (chat_id, address))
    result = [row[0] for row in c.fetchall()]
    conn.close()
    return result


# ====================== 区块链查询 ======================
async def etherscan_request(params: dict):
    if not ETHERSCAN_API_KEY:
        raise Exception("未设置 ETHERSCAN_API_KEY")
    params["apikey"] = ETHERSCAN_API_KEY
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(BASE_URL, params=params)
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "0":
            raise Exception(f"API错误: {data.get('result')}")
        return data


async def get_native_balance(address: str, chain: str = "BSC") -> List[Dict]:
    config = CHAIN_CONFIG.get(chain.upper())
    if not config:
        return []
    params = {
        "chainid": config["chainid"],
        "module": "account",
        "action": "balance",
        "address": address,
        "tag": "latest"
    }
    try:
        data = await etherscan_request(params)
        balance = int(data.get("result", 0)) / 10**18
        return [{"symbol": config["symbol"], "balance": balance}] if balance > 0.001 else []
    except Exception as e:
        logger.error(f"原生余额失败 {chain}: {e}")
        return []


async def get_erc20_tokens(address: str, chain: str = "BSC") -> List[Dict]:
    config = CHAIN_CONFIG.get(chain.upper())
    if not config:
        return []
    
    params = {
        "chainid": config["chainid"],
        "module": "account",
        "action": "tokentx",
        "address": address,
        "page": 1,
        "offset": 60,
        "sort": "desc"
    }
    
    tokens = []
    try:
        data = await etherscan_request(params)
        result = data.get("result", []) if isinstance(data, dict) else []
        
        token_dict = {}
        for tx in result:
            if isinstance(tx, dict):
                symbol = tx.get("tokenSymbol")
                contract = tx.get("contractAddress")
                decimal = int(tx.get("tokenDecimal", 18))
                if symbol and contract and symbol not in token_dict:
                    token_dict[symbol] = (contract, decimal)

        for symbol, (contract, decimal) in list(token_dict.items())[:20]:
            try:
                bal_params = {
                    "chainid": config["chainid"],
                    "module": "account",
                    "action": "tokenbalance",
                    "contractaddress": contract,
                    "address": address,
                    "tag": "latest"
                }
                bal_data = await etherscan_request(bal_params)
                balance = int(bal_data.get("result", 0)) / (10 ** decimal)
                if balance > 0.0001:
                    tokens.append({"symbol": symbol, "balance": balance})
            except:
                continue
        return tokens
    except Exception as e:
        logger.error(f"ERC20 查询失败: {e}")
        return []


async def get_wallet_tokens(address: str, chain: str):
    logger.info(f"查询 {chain} 地址: {address}")
    tokens = await get_native_balance(address, chain)
    if chain.upper() in ["BSC", "ETH"]:
        tokens.extend(await get_erc20_tokens(address, chain))
    logger.info(f"发现 {len(tokens)} 个资产")
    return tokens


# ====================== Telegram Handlers ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text("🎉 **钱包监控 Bot** 已启动", 
                                  reply_markup=InlineKeyboardMarkup(kb), 
                                  parse_mode='Markdown')


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == 'add_wallet':
        context.user_data['action'] = 'adding'
        await query.message.reply_text("📍 请发送钱包地址：")

    elif data == 'view_wallet':
        addrs = get_user_wallets(chat_id)
        if not addrs:
            await query.message.reply_text("暂无保存的钱包")
            return
        kb = [[InlineKeyboardButton(a[:12]+"...", callback_data=f"addr|{a}")] for a in addrs]
        await query.message.reply_text("选择地址查看持仓：", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("addr|"):
        addr = data.split("|")[1]
        context.user_data['selected'] = addr
        chains = get_address_chains(chat_id, addr)
        kb = [[InlineKeyboardButton(c, callback_data=f"chain|{c}")] for c in chains]
        await query.message.reply_text(f"地址：`{addr}`\n请选择链：", 
                                     reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data.startswith("chain|"):
        chain = data.split("|")[1]
        addr = context.user_data.get('selected')
        tokens = await get_wallet_tokens(addr, chain)

        if not tokens:
            await query.message.reply_text("⚠️ 未检测到持仓（可能地址新或无近期交易）")
            return

        msg = f"**{chain} 持仓**\n`{addr}`\n\n"
        total = 0.0
        for t in tokens[:15]:
            price = await get_token_price(t['symbol'])
            usd = t['balance'] * (price or 0)
            total += usd
            msg += f"• {t['symbol']}: {t['balance']:.4f} ≈ ${usd:.2f}\n"
        msg += f"\n**总价值 ≈ ${total:.2f}**"
        await query.message.reply_text(msg, parse_mode='Markdown')


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if context.user_data.get('action') == 'adding':
        context.user_data['pending_address'] = text
        context.user_data['action'] = 'choosing'
        kb = [[InlineKeyboardButton(c, callback_data=f'addchain|{c}')] for c in ["BSC", "ETH"]]
        await update.message.reply_text("请选择链：", reply_markup=InlineKeyboardMarkup(kb))


# ====================== 主程序 ======================
async def run_bot():
    if not BOT_TOKEN:
        logger.error("❌ 未设置 BOT_TOKEN")
        return

    await load_coingecko_coins()
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("🚀 Bot 已成功启动（异步版本）")
    await application.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    asyncio.run(run_bot())
