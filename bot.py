import os
import asyncio
import sqlite3
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)

# ---------------------------
# 配置
# ---------------------------
DB_FILE = "database.db"
ALERT_THRESHOLD = 10  # 涨幅百分比
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BSCSCAN_API_KEY = os.environ.get("BSCSCAN_API_KEY")
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
MONITOR_INTERVAL = 20  # 秒

# ---------------------------
# CoinGecko 缓存
COINGECKO_COINS = {}

def load_coingecko_coins():
    global COINGECKO_COINS
    try:
        resp = requests.get("https://api.coingecko.com/api/v3/coins/list", timeout=10).json()
        for coin in resp:
            COINGECKO_COINS[coin['symbol'].lower()] = coin['id']
    except Exception as e:
        print("加载 CoinGecko 代币列表失败:", e)

def get_token_price(symbol):
    symbol_lower = symbol.lower()
    coin_id = COINGECKO_COINS.get(symbol_lower)
    if not coin_id:
        return None, None
    try:
        resp = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd", timeout=5).json()
        price = resp.get(coin_id, {}).get("usd")
        return price, coin_id if price else (None, coin_id)
    except:
        return None, coin_id

# ---------------------------
# 数据库
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS wallets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    address TEXT,
                    chain TEXT,
                    last_value REAL DEFAULT 0
                )""")
    conn.commit()
    conn.close()

def add_wallet(chat_id, address, chain):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO wallets (chat_id, address, chain) VALUES (?, ?, ?)", (chat_id, address, chain))
    conn.commit()
    conn.close()

def get_user_wallets(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT DISTINCT address FROM wallets WHERE chat_id=?", (chat_id,))
    addresses = [row[0] for row in c.fetchall()]
    conn.close()
    return addresses

def get_address_chains(chat_id, address):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chain FROM wallets WHERE chat_id=? AND address=?", (chat_id, address))
    chains = [row[0] for row in c.fetchall()]
    conn.close()
    return chains

def update_last_value(chat_id, address, chain, value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE wallets SET last_value=? WHERE chat_id=? AND address=? AND chain=?", (value, chat_id, address, chain))
    conn.commit()
    conn.close()

def get_wallets_all():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chat_id, address, chain, last_value FROM wallets")
    rows = c.fetchall()
    conn.close()
    return rows

# ---------------------------
# 链上查询
def get_erc20_tokens(address, chain):
    if chain.upper() == "BSC":
        api_key = BSCSCAN_API_KEY
        base_url = "https://api.bscscan.com/api"
    elif chain.upper() == "ETH":
        api_key = ETHERSCAN_API_KEY
        base_url = "https://api.etherscan.io/api"
    else:
        return []

    try:
        url = f"{base_url}?module=account&action=tokentx&address={address}&page=1&offset=100&sort=asc&apikey={api_key}"
        resp = requests.get(url, timeout=5).json()
        tokens = {}
        for tx in resp.get("result", []):
            tokens[tx["tokenSymbol"]] = tx["contractAddress"]

        balances = []
        for symbol, contract in tokens.items():
            token_url = f"{base_url}?module=account&action=tokenbalance&contractaddress={contract}&address={address}&tag=latest&apikey={api_key}"
            balance_resp = requests.get(token_url, timeout=5).json()
            balance = int(balance_resp.get("result", 0)) / (10 ** 18)
            balances.append({"symbol": symbol, "balance": balance, "contract": contract})
        return balances
    except:
        return []

def get_solana_tokens(address):
    try:
        headers = {"Content-Type": "application/json"}
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                address,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed"}
            ]
        }
        resp = requests.post(SOLANA_RPC_URL, json=payload, headers=headers, timeout=5).json()
        tokens = []
        for account in resp.get("result", {}).get("value", []):
            info = account["account"]["data"]["parsed"]["info"]
            mint = info["mint"]
            amount = int(info["tokenAmount"]["amount"])
            decimals = int(info["tokenAmount"]["decimals"])
            balance = amount / (10 ** decimals)
            tokens.append({"symbol": mint[:6], "balance": balance, "contract": mint})
        return tokens
    except:
        return []

def get_wallet_tokens(address, chain):
    chain = chain.upper()
    if chain in ["BSC", "ETH"]:
        return get_erc20_tokens(address, chain)
    elif chain == "SOL":
        return get_solana_tokens(address)
    else:
        return []

# ---------------------------
# Telegram Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("添加钱包地址", callback_data='add_wallet')],
        [InlineKeyboardButton("查看我的钱包", callback_data='view_wallet')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("欢迎使用代币监控Bot！请选择操作：", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id

    if query.data == 'add_wallet':
        context.user_data['action'] = 'adding_wallet'
        await query.message.reply_text("请输入你的钱包地址：")
    elif query.data == 'view_wallet':
        addresses = get_user_wallets(chat_id)
        if not addresses:
            await query.message.reply_text("你还没有添加任何钱包地址。")
            return
        keyboard = [[InlineKeyboardButton(addr, callback_data=f"addr|{addr}")] for addr in addresses]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("请选择要查看的地址：", reply_markup=reply_markup)
    elif query.data.startswith("addr|"):
        address = query.data.split("|")[1]
        context.user_data['selected_address'] = address
        chains = get_address_chains(chat_id, address)
        keyboard = [[InlineKeyboardButton(chain, callback_data=f"chain|{chain}")] for chain in chains]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(f"地址 {address} 的链，请选择：", reply_markup=reply_markup)
    elif query.data.startswith("chain|"):
        chain = query.data.split("|")[1]
        address = context.user_data.get('selected_address')
        tokens = get_wallet_tokens(address, chain)
        msg = f"钱包: {address}\n链: {chain}\n\n持仓代币:\n"
        for t in tokens:
            price, _ = get_token_price(t['symbol'])
            price_display = f"${price:.4f}" if price else "未知"
            msg += f"{t['symbol']}: {t['balance']} 价格: {price_display} 合约: {t['contract']}\n"
        await query.message.reply_text(msg)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('action') == 'adding_wallet':
        address = update.message.text
        context.user_data['pending_address'] = address
        keyboard =
