import os
import asyncio
import sqlite3
import logging
import httpx
from typing import List, Dict

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

# ====================== CoinGecko 价格 ======================
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
            r = await client.get(
                f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
            )
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


# ====================== 异步区块链查询 (V2) ======================
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
        logger.info(f"{chain} 原生余额: {balance:.4f}")
        return [{"symbol": config["symbol"], "balance": balance}] if balance > 0.001 else []
    except Exception as e:
        logger.error(f"获取 {chain} 原生余额失败: {e}")
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
        "offset": 80,
        "sort": "desc"
    }
    
    tokens = []
    try:
        data = await etherscan_request(params)
        result = data.get("result", []) if isinstance(data, dict) else []
        logger.info(f"tokentx 返回 {len(result)} 条记录")

        token_dict = {}
        for tx in result:
            if isinstance(tx, dict):
                symbol = tx.get("tokenSymbol")
                contract = tx.get("contractAddress")
                decimal = int(tx.get("tokenDecimal", 18))
                if symbol and contract and symbol not in token_dict:
                    token_dict[symbol] = (contract, decimal)

        # 查询余额
        for symbol, (contract, decimal) in list(token_dict.items())[:25]:
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
                balance_raw = int(bal_data.get("result", 0))
                balance = balance_raw / (10 ** decimal)
                
                if balance > 0.0001:
                    tokens.append({"symbol": symbol, "balance": balance})
                    logger.info(f"✅ 找到 {symbol} = {balance:.4f}")
            except:
                continue
        return tokens
    except Exception as e:
        logger.error(f"获取 {chain} Token 失败: {e}")
        return []


async def get_wallet_tokens(address: str, chain: str):
    logger.info(f"开始查询 {chain} 地址: {address}")
    tokens = await get_native_balance(address, chain)
    
    if chain.upper() in ["BSC", "ETH"]:
        erc_tokens = await get_erc20_tokens(address, chain)
        tokens.extend(erc_tokens)
    
    logger.info(f"最终发现 {len(tokens)} 个资产")
    return tokens


# ====================== Telegram Handlers ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text("🎉 **钱包监控 Bot** 已就绪", 
                                  reply_markup=InlineKeyboardMarkup(kb), 
                                  parse_mode='Markdown')


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == 'add_wallet':
        context.user_data['action'] = 'adding'
        await query.message.reply_text("请发送钱包地址：")

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
        tokens = await get_wallet_tokens(addr, chain)   # ← 改为 await

        if not tokens:
            await query.message.reply_text("⚠️ 未查询到持仓\n请确认地址有余额")
            return

        msg = f"**{chain} 持仓**\n`{addr}`\n\n"
        total = 0
        for t in tokens[:15]:
            price = await get_token_price(t['symbol'])   # ← 改为 await
            usd = t['balance'] * (price or 0)
            total += usd
            msg += f"{t['symbol']}: {t['balance']:.4f} ≈ ${usd:.2f}\n"
        msg += f"\n**总价值 ≈ ${total:.2f}**"
        await query.message.reply_text(msg, parse_mode='Markdown')


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('action') == 'adding':
        context.user_data['pending_address'] = update.message.text.strip()
        context.user_data['action'] = 'choosing'
        kb = [[InlineKeyboardButton(c, callback_data=f'addchain|{c}')] for c in ["BSC","ETH","SOL"]]
        await update.message.reply_text("请选择链：", reply_markup=InlineKeyboardMarkup(kb))


# ====================== 主程序 ======================
def main():
    if not BOT_TOKEN:
        logger.error("缺少 BOT_TOKEN")
        return
    
    asyncio.run(load_coingecko_coins())   # 异步加载
    init_db()
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    logger.info("🚀 Bot 已启动（异步版本）")
    app.run_polling()


if __name__ == '__main__':
    main()
