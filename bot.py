import os
import sqlite3
import logging
import httpx
from typing import List, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ====================== 配置 ======================
DB_FILE = "database.db"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY") or os.environ.get("BSCSCAN_API_KEY")

COINGECKO_COINS = {}

BASE_URL = "https://api.etherscan.io/v2/api"

CHAIN_CONFIG = {
    "BSC": {"chainid": 56, "symbol": "BNB"},
    "ETH": {"chainid": 1, "symbol": "ETH"},
}

# ====================== CoinGecko ======================
def load_coingecko_coins():
    global COINGECKO_COINS
    try:
        r = httpx.get("https://api.coingecko.com/api/v3/coins/list", timeout=15)
        if r.status_code == 200:
            COINGECKO_COINS = {coin['symbol'].lower(): coin['id'] for coin in r.json()}
            logger.info(f"✅ CoinGecko 加载 {len(COINGECKO_COINS)} 个币种")
    except Exception as e:
        logger.warning(f"CoinGecko 加载失败: {e}")

def get_token_price(symbol: str) -> float | None:
    if not symbol or not COINGECKO_COINS:
        return None
    coin_id = COINGECKO_COINS.get(symbol.lower())
    if not coin_id:
        return None
    try:
        r = httpx.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd", timeout=10)
        return r.json().get(coin_id, {}).get("usd")
    except:
        return None


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

def get_address_chains(chat_id, address):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chain FROM wallets WHERE chat_id=? AND address=?", (chat_id, address))
    return [row[0] for row in c.fetchall()]


# ====================== 区块链查询 ======================
def etherscan_request(params: dict):
    if not ETHERSCAN_API_KEY:
        raise Exception("未设置 ETHERSCAN_API_KEY")
    params["apikey"] = ETHERSCAN_API_KEY
    r = httpx.get(BASE_URL, params=params, timeout=20)
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "0":
        raise Exception(f"API错误: {data.get('result')}")
    return data


def get_wallet_tokens(address: str, chain: str):
    tokens = []
    config = CHAIN_CONFIG.get(chain.upper())
    if not config:
        return tokens

    # 原生余额
    try:
        data = etherscan_request({
            "chainid": config["chainid"],
            "module": "account",
            "action": "balance",
            "address": address,
            "tag": "latest"
        })
        balance = int(data.get("result", 0)) / 10**18
        if balance > 0.001:
            tokens.append({"symbol": config["symbol"], "balance": balance})
    except:
        pass

    # ERC20 代币
    try:
        data = etherscan_request({
            "chainid": config["chainid"],
            "module": "account",
            "action": "tokentx",
            "address": address,
            "page": 1,
            "offset": 50,
            "sort": "desc"
        })
        result = data.get("result", []) if isinstance(data, dict) else []
        token_dict = {}
        for tx in result:
            if isinstance(tx, dict):
                symbol = tx.get("tokenSymbol")
                contract = tx.get("contractAddress")
                decimal = int(tx.get("tokenDecimal", 18))
                if symbol and contract and symbol not in token_dict:
                    token_dict[symbol] = (contract, decimal)

        for symbol, (contract, decimal) in list(token_dict.items())[:15]:
            try:
                bal = etherscan_request({
                    "chainid": config["chainid"],
                    "module": "account",
                    "action": "tokenbalance",
                    "contractaddress": contract,
                    "address": address,
                    "tag": "latest"
                })
                balance = int(bal.get("result", 0)) / (10 ** decimal)
                if balance > 0.0001:
                    tokens.append({"symbol": symbol, "balance": balance})
            except:
                continue
    except:
        pass

    return tokens


# ====================== Handlers ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
          [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]]
    await update.message.reply_text("🎉 **Wallet Monitor Bot** 已启动", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat.id

    if data == 'add_wallet':
        context.user_data['action'] = 'adding'
        await query.message.reply_text("请发送钱包地址：")
    # 其他按钮逻辑暂时简化，后续再加


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('action') == 'adding':
        address = update.message.text.strip()
        # 暂时默认 BSC
        if add_wallet(update.message.chat.id, address, "BSC"):
            await update.message.reply_text(f"✅ 已添加地址：\n`{address}`\n链：BSC", parse_mode='Markdown')
        else:
            await update.message.reply_text("添加失败")


def main():
    load_coingecko_coins()
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("🚀 Bot 已启动（同步稳定版）")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    if BOT_TOKEN:
        main()
    else:
        logger.error("缺少 BOT_TOKEN")
