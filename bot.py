import os
import asyncio
import sqlite3
import requests
import logging
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

# --------------------------- 配置 ---------------------------
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DB_FILE = "database.db"
ALERT_THRESHOLD = 10  # 百分比
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BSCSCAN_API_KEY = os.environ.get("BSCSCAN_API_KEY")
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
MONITOR_INTERVAL = 30  # 秒，建议30+避免API限速

COINGECKO_COINS = {}

def load_coingecko_coins():
    global COINGECKO_COINS
    try:
        resp = requests.get("https://api.coingecko.com/api/v3/coins/list", timeout=15).json()
        COINGECKO_COINS = {coin['symbol'].lower(): coin['id'] for coin in resp}
        logger.info(f"Loaded {len(COINGECKO_COINS)} coins from CoinGecko")
    except Exception as e:
        logger.error(f"加载 CoinGecko 列表失败: {e}")

def get_token_price(symbol: str):
    symbol_lower = symbol.lower()
    coin_id = COINGECKO_COINS.get(symbol_lower)
    if not coin_id:
        return None
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd",
            timeout=8
        ).json()
        return resp.get(coin_id, {}).get("usd")
    except:
        return None

# --------------------------- 数据库 ---------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS wallets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    address TEXT,
                    chain TEXT,
                    last_value REAL DEFAULT 0,
                    UNIQUE(chat_id, address, chain)
                )""")
    conn.commit()
    conn.close()

def add_wallet(chat_id, address, chain):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO wallets (chat_id, address, chain) VALUES (?, ?, ?)", 
                 (chat_id, address.lower(), chain.upper()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # 已存在
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

def get_wallets_all():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chat_id, address, chain, last_value FROM wallets")
    rows = c.fetchall()
    conn.close()
    return rows

def update_last_value(chat_id, address, chain, value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""UPDATE wallets SET last_value=? 
                 WHERE chat_id=? AND address=? AND chain=?""", 
              (value, chat_id, address, chain))
    conn.commit()
    conn.close()

# --------------------------- 链上查询 ---------------------------
def get_erc20_tokens(address, chain):
    if chain == "BSC":
        api_key = BSCSCAN_API_KEY
        base_url = "https://api.bscscan.com/api"
    elif chain == "ETH":
        api_key = ETHERSCAN_API_KEY
        base_url = "https://api.etherscan.io/api"
    else:
        return []

    if not api_key:
        return []

    try:
        # 获取 token tx（简化版，实际生产建议用 tokenbalance 多合约查询）
        url = f"{base_url}?module=account&action=tokentx&address={address}&page=1&offset=200&sort=desc&apikey={api_key}"
        resp = requests.get(url, timeout=10).json()
        tokens = {}
        for tx in resp.get("result", []):
            if tx.get("tokenSymbol"):
                tokens[tx["tokenSymbol"]] = tx["contractAddress"]

        balances = []
        for symbol, contract in list(tokens.items())[:30]:  # 限制数量
            token_url = f"{base_url}?module=account&action=tokenbalance&contractaddress={contract}&address={address}&tag=latest&apikey={api_key}"
            bal_resp = requests.get(token_url, timeout=8).json()
            bal_str = bal_resp.get("result", "0")
            try:
                balance = int(bal_str) / (10 ** 18)
                if balance > 0.0001:
                    balances.append({"symbol": symbol, "balance": balance, "contract": contract})
            except:
                continue
        return balances
    except Exception as e:
        logger.error(f"ERC20 query error {chain}: {e}")
        return []

def get_solana_tokens(address):
    try:
        headers = {"Content-Type": "application/json"}
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                address,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed"}
            ]
        }
        resp = requests.post(SOLANA_RPC_URL, json=payload, headers=headers, timeout=10).json()
        tokens = []
        for account in resp.get("result", {}).get("value", []):
            try:
                info = account["account"]["data"]["parsed"]["info"]
                mint = info["mint"]
                amount = int(info["tokenAmount"]["amount"])
                decimals = int(info["tokenAmount"]["decimals"])
                balance = amount / (10 ** decimals)
                if balance > 0.0001:
                    tokens.append({"symbol": mint[:8], "balance": balance, "contract": mint})
            except:
                continue
        return tokens
    except Exception as e:
        logger.error(f"SOL query error: {e}")
        return []

def get_wallet_tokens(address, chain):
    chain = chain.upper()
    if chain in ["BSC", "ETH"]:
        return get_erc20_tokens(address, chain)
    elif chain == "SOL":
        return get_solana_tokens(address)
    return []

# --------------------------- Telegram Handlers ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ 添加钱包地址", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text("欢迎使用 **钱包监控Bot**！\n支持 BSC / ETH / SOL", 
                                  reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == 'add_wallet':
        context.user_data['action'] = 'adding_wallet'
        await query.message.reply_text("请发送钱包地址（BSC/ETH/SOL）：")

    elif data == 'view_wallet':
        addresses = get_user_wallets(chat_id)
        if not addresses:
            await query.message.reply_text("你还没有添加钱包。")
            return
        keyboard = [[InlineKeyboardButton(addr, callback_data=f"addr|{addr}")] for addr in addresses]
        await query.message.reply_text("选择地址查看：", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("addr|"):
        address = data.split("|")[1]
        context.user_data['selected_address'] = address
        chains = get_address_chains(chat_id, address)
        keyboard = [[InlineKeyboardButton(chain, callback_data=f"chain|{chain}")] for chain in chains]
        await query.message.reply_text(f"地址: `{address}`\n选择链：", 
                                     reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif data.startswith("chain|"):
        chain = data.split("|")[1]
        address = context.user_data.get('selected_address')
        tokens = get_wallet_tokens(address, chain)
        if not tokens:
            await query.message.reply_text("未查询到持仓或查询失败。")
            return
        msg = f"**{chain} 持仓**\n地址: `{address}`\n\n"
        total_usd = 0
        for t in tokens[:15]:
            price = get_token_price(t['symbol'])
            usd = t['balance'] * (price or 0)
            total_usd += usd
            price_str = f"${price:.4f}" if price else "?"
            msg += f"{t['symbol']}: {t['balance']:.4f} (≈${usd:.2f}) | {price_str}\n"
        msg += f"\n总价值 ≈ ${total_usd:.2f}"
        await query.message.reply_text(msg, parse_mode='Markdown')

    elif data.startswith("addchain|"):
        chain = data.split("|")[1]
        address = context.user_data.get('pending_address')
        if address and add_wallet(chat_id, address, chain):
            await query.message.reply_text(f"✅ 已添加 {address} ({chain})")
        else:
            await query.message.reply_text("添加失败（可能已存在）")
        context.user_data.clear()

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('action') == 'adding_wallet':
        address = update.message.text.strip()
        context.user_data['pending_address'] = address
        context.user_data['action'] = 'choosing_chain'
        keyboard = [
            [InlineKeyboardButton("BSC", callback_data='addchain|BSC')],
            [InlineKeyboardButton("ETH", callback_data='addchain|ETH')],
            [InlineKeyboardButton("SOL", callback_data='addchain|SOL')]
        ]
        await update.message.reply_text("请选择链：", reply_markup=InlineKeyboardMarkup(keyboard))

# --------------------------- 监控任务 ---------------------------
async def monitor_task(context: ContextTypes.DEFAULT_TYPE):
    while True:
        try:
            wallets = get_wallets_all()
            for chat_id, address, chain, last_value in wallets:
                tokens = get_wallet_tokens(address, chain)
                current_value = sum(t['balance'] * (get_token_price(t['symbol']) or 0) for t in tokens)
                
                if last_value > 0 and current_value > 0:
                    change_pct = (current_value - last_value) / last_value * 100
                    if abs(change_pct) >= ALERT_THRESHOLD:
                        sign = "📈" if change_pct > 0 else "📉"
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"{sign} **告警** {chain} {address[:8]}...\n"
                                 f"价值变化: {change_pct:+.1f}%\n"
                                 f"当前 ≈ ${current_value:.2f}"
                        )
                if current_value > 0:
                    update_last_value(chat_id, address, chain, current_value)
        except Exception as e:
            logger.error(f"Monitor error: {e}")
        await asyncio.sleep(MONITOR_INTERVAL)

# --------------------------- Main ---------------------------
async def main():
    if not BOT_TOKEN:
        logger.error("缺少 BOT_TOKEN 环境变量")
        return

    load_coingecko_coins()
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # 启动监控
    asyncio.create_task(monitor_task(app.context))

    logger.info("Bot started...")
    await app.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
