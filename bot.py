import os
import sqlite3
import logging
import httpx
from typing import List, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

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
    return result or ["BSC"]


# ====================== 区块链查询 ======================
def etherscan_request(params: dict):
    if not ETHERSCAN_API_KEY:
        raise Exception("未设置 ETHERSCAN_API_KEY")
    params["apikey"] = ETHERSCAN_API_KEY
    r = httpx.get(BASE_URL, params=params, timeout=20)
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "0":
        error_msg = data.get('result', '')
        if "Free API access is not supported" in error_msg:
            raise Exception("FREE_API_LIMIT")
        raise Exception(f"API错误: {error_msg}")
    return data


def get_wallet_tokens(address: str, chain: str) -> List[Dict]:
    logger.info(f"查询 {chain} 地址: {address}")
    tokens = []
    config = CHAIN_CONFIG.get(chain.upper())
    if not config:
        return tokens

    # 原生代币
    try:
        data = etherscan_request({
            "chainid": config["chainid"], "module": "account", "action": "balance",
            "address": address, "tag": "latest"
        })
        balance = int(data.get("result", 0)) / 10**18
        if balance > 0.0001:
            tokens.append({"symbol": config["symbol"], "balance": balance})
    except:
        pass

    # ERC20 代币
    try:
        data = etherscan_request({
            "chainid": config["chainid"], "module": "account", "action": "tokentx",
            "address": address, "page": 1, "offset": 80, "sort": "desc"
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

        for symbol, (contract, decimal) in list(token_dict.items())[:20]:
            try:
                bal = etherscan_request({
                    "chainid": config["chainid"], "module": "account", "action": "tokenbalance",
                    "contractaddress": contract, "address": address, "tag": "latest"
                })
                balance = int(bal.get("result", 0)) / (10 ** decimal)
                if balance > 0.0001:
                    tokens.append({"symbol": symbol, "balance": balance})
            except:
                continue
    except Exception as e:
        if "FREE_API_LIMIT" in str(e):
            logger.warning("免费 API 额度不足")
        else:
            logger.error(f"查询失败: {e}")

    return tokens


# ====================== Handlers ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text("🎉 **Wallet Monitor Bot** 已启动\n支持 BSC & ETH", 
                                  reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')


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
            await query.message.reply_text("你还没有添加任何钱包")
            return
        kb = [[InlineKeyboardButton(a[:12]+"...", callback_data=f"addr|{a}")] for a in addrs]
        await query.message.reply_text("选择地址查看持仓：", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("addr|"):
        addr = data.split("|")[1]
        context.user_data['selected_addr'] = addr
        chains = get_address_chains(chat_id, addr)
        kb = [[InlineKeyboardButton(c, callback_data=f"chain|{c}|{addr}")] for c in chains]
        await query.message.reply_text(f"地址：`{addr}`\n请选择链：", 
                                     reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data.startswith("chain|"):
        parts = data.split("|")
        chain = parts[1]
        addr = parts[2] if len(parts) > 2 else context.user_data.get('selected_addr')
        
        await query.message.reply_text(f"🔍 正在查询 {chain} 持仓...\n`{addr}`", parse_mode='Markdown')
        
        tokens = get_wallet_tokens(addr, chain)
        
        if not tokens:
            await query.message.reply_text("⚠️ 未检测到持仓\n\n可能原因：\n1. 该地址近期无交易\n2. API 免费额度限制\n3. 持仓极少")
            return

        msg = f"**{chain} 持仓**\n`{addr}`\n\n"
        total = 0.0
        for t in tokens[:15]:
            price = get_token_price(t['symbol'])
            usd = t['balance'] * (price or 0)
            total += usd
            msg += f"• {t['symbol']}: {t['balance']:.4f} ≈ ${usd:.2f}\n"
        msg += f"\n**总价值 ≈ ${total:.2f} USD**"
        await query.message.reply_text(msg, parse_mode='Markdown')

    elif data.startswith("addchain|"):
        parts = data.split("|")
        chain = parts[1]
        address = parts[2]
        if add_wallet(chat_id, address, chain):
            await query.message.reply_text(f"✅ **添加成功！**\n地址：`{address}`\n链：**{chain}**", parse_mode='Markdown')
            context.user_data.clear()
        else:
            await query.message.reply_text("❌ 添加失败")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('action') == 'adding':
        address = update.message.text.strip()
        context.user_data['pending_address'] = address
        context.user_data['action'] = 'choosing'
        
        kb = [
            [InlineKeyboardButton("BSC", callback_data=f"addchain|BSC|{address}")],
            [InlineKeyboardButton("ETH", callback_data=f"addchain|ETH|{address}")]
        ]
        await update.message.reply_text("请选择链：", reply_markup=InlineKeyboardMarkup(kb))


def main():
    if not BOT_TOKEN:
        logger.error("❌ 未设置 BOT_TOKEN")
        return

    load_coingecko_coins()
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("🚀 Bot 已成功启动（完整版）")
    application.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
