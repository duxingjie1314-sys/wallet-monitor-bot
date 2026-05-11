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

# ========================= 配置 =========================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "database.db"
ALERT_THRESHOLD = 10
MONITOR_INTERVAL = 45

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BSCSCAN_API_KEY = os.environ.get("BSCSCAN_API_KEY")
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY")
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

COINGECKO_COINS = {}

# ========================= CoinGecko =========================
def load_coingecko_coins():
    global COINGECKO_COINS
    try:
        resp = requests.get("https://api.coingecko.com/api/v3/coins/list", timeout=15)
        if resp.status_code == 200:
            COINGECKO_COINS = {coin['symbol'].lower(): coin['id'] for coin in resp.json()}
            logger.info(f"✅ 成功加载 {len(COINGECKO_COINS)} 个币种")
    except Exception as e:
        logger.error(f"CoinGecko 加载失败: {e}")

def get_token_price(symbol: str):
    if not symbol or not COINGECKO_COINS:
        return None
    coin_id = COINGECKO_COINS.get(symbol.lower())
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

# ========================= 数据库 =========================
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
    c.execute("UPDATE wallets SET last_value=? WHERE chat_id=? AND address=? AND chain=?", 
              (value, chat_id, address, chain))
    conn.commit()
    conn.close()

# ========================= 链上查询 =========================
def get_erc20_tokens(address, chain):
    if chain == "BSC":
        base_url, api_key = "https://api.bscscan.com/api", BSCSCAN_API_KEY
    elif chain == "ETH":
        base_url, api_key = "https://api.etherscan.io/api", ETHERSCAN_API_KEY
    else:
        return []
    if not api_key:
        return []

    try:
        url = f"{base_url}?module=account&action=tokentx&address={address}&page=1&offset=80&sort=desc&apikey={api_key}"
        resp = requests.get(url, timeout=10).json()
        tokens = {tx["tokenSymbol"]: tx["contractAddress"] for tx in resp.get("result", []) if tx.get("tokenSymbol")}
        
        balances = []
        for symbol, contract in list(tokens.items())[:20]:
            try:
                bal_url = f"{base_url}?module=account&action=tokenbalance&contractaddress={contract}&address={address}&apikey={api_key}"
                bal = int(requests.get(bal_url, timeout=8).json().get("result", "0")) / 10**18
                if bal > 0.0001:
                    balances.append({"symbol": symbol, "balance": bal})
            except:
                continue
        return balances
    except:
        return []

def get_solana_tokens(address):
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [address, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}, {"encoding": "jsonParsed"}]
        }
        resp = requests.post(SOLANA_RPC_URL, json=payload, timeout=12).json()
        tokens = []
        for acc in resp.get("result", {}).get("value", []):
            try:
                info = acc["account"]["data"]["parsed"]["info"]
                balance = int(info["tokenAmount"]["amount"]) / (10 ** int(info["tokenAmount"]["decimals"]))
                if balance > 0.0001:
                    tokens.append({"symbol": "Token", "balance": balance})
            except:
                continue
        return tokens
    except:
        return []

def get_wallet_tokens(address, chain):
    if chain in ["BSC", "ETH"]:
        return get_erc20_tokens(address, chain)
    elif chain == "SOL":
        return get_solana_tokens(address)
    return []

# ========================= Handlers =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text("🎉 **钱包监控 Bot 已启动**", 
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == 'add_wallet':
        context.user_data['action'] = 'adding'
        await query.message.reply_text("📍 请发送钱包地址：")
    # （其他 button_handler 逻辑保持不变，篇幅原因省略部分，实际请保留之前完整版本中的 button_handler）
    # ... 复制你上一个版本中 button_handler 的完整内容 ...

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('action') == 'adding':
        context.user_data['pending'] = update.message.text.strip()
        context.user_data['action'] = 'choosing'
        kb = [
            [InlineKeyboardButton("BSC", callback_data='addchain|BSC')],
            [InlineKeyboardButton("ETH", callback_data='addchain|ETH')],
            [InlineKeyboardButton("SOL", callback_data='addchain|SOL')]
        ]
        await update.message.reply_text("请选择链：", reply_markup=InlineKeyboardMarkup(kb))

# ========================= 监控任务 =========================
async def monitor_task(bot):
    while True:
        try:
            for chat_id, address, chain, last_value in get_wallets_all():
                tokens = get_wallet_tokens(address, chain)
                current = sum(t['balance'] * (get_token_price(t.get('symbol', '')) or 0) for t in tokens)

                if last_value > 5 and current > 5:
                    pct = (current - last_value) / last_value * 100
                    if abs(pct) >= ALERT_THRESHOLD:
                        sign = "📈" if pct > 0 else "📉"
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"{sign} **告警** {chain} `{address[:8]}...`\n变化 {pct:+.2f}%\n当前 ≈ ${current:.2f}",
                            parse_mode='Markdown'
                        )
                if current > 0.01:
                    update_last_value(chat_id, address, chain, current)
        except Exception as e:
            logger.error(f"监控错误: {e}")
        await asyncio.sleep(MONITOR_INTERVAL)

# ========================= Post Init（关键修复）=========================
async def post_init(application):
    logger.info("启动后台监控任务...")
    asyncio.create_task(monitor_task(application.bot))

# ========================= 主函数（重要修改）=========================
def main():
    if not BOT_TOKEN:
        logger.error("缺少 BOT_TOKEN 环境变量")
        return

    load_coingecko_coins()
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("🚀 Bot 启动成功...")
    app.run_polling()   # 注意：这里不要加 await

if __name__ == '__main__':
    main()
