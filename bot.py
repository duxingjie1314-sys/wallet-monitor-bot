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
from telegram.error import RetryAfter

# ========================= 配置 =========================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "database.db"
ALERT_THRESHOLD = 10
MONITOR_INTERVAL = 90

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

# ========================= 链上查询（已优化）=========================
def get_native_balance(address, chain):
    """查询原生币余额"""
    try:
        if chain == "BSC":
            url = f"https://api.bscscan.com/api?module=account&action=balance&address={address}&apikey={BSCSCAN_API_KEY}"
            resp = requests.get(url, timeout=8).json()
            balance = int(resp.get("result", 0)) / 10**18
            return [{"symbol": "BNB", "balance": balance}] if balance > 0.001 else []
        
        elif chain == "ETH":
            url = f"https://api.etherscan.io/api?module=account&action=balance&address={address}&apikey={ETHERSCAN_API_KEY}"
            resp = requests.get(url, timeout=8).json()
            balance = int(resp.get("result", 0)) / 10**18
            return [{"symbol": "ETH", "balance": balance}] if balance > 0.001 else []
        
        elif chain == "SOL":
            payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]}
            resp = requests.post(SOLANA_RPC_URL, json=payload, timeout=10).json()
            balance = resp.get("result", {}).get("value", 0) / 10**9
            return [{"symbol": "SOL", "balance": balance}] if balance > 0.001 else []
    except Exception as e:
        logger.error(f"原生币查询失败 {chain}: {e}")
        return []


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
        url = f"{base_url}?module=account&action=tokentx&address={address}&page=1&offset=120&sort=desc&apikey={api_key}"
        resp = requests.get(url, timeout=10).json()
        
        token_dict = {}
        for tx in resp.get("result", []):
            symbol = tx.get("tokenSymbol")
            contract = tx.get("contractAddress")
            if symbol and contract:
                token_dict[symbol] = contract

        tokens = []
        for symbol, contract in list(token_dict.items())[:25]:
            try:
                bal_url = f"{base_url}?module=account&action=tokenbalance&contractaddress={contract}&address={address}&apikey={api_key}"
                bal_resp = requests.get(bal_url, timeout=8).json()
                balance = int(bal_resp.get("result", "0")) / 10**18
                if balance > 0.0001:
                    tokens.append({"symbol": symbol, "balance": balance})
            except:
                continue
        return tokens
    except Exception as e:
        logger.error(f"ERC20 查询失败: {e}")
        return []


def get_solana_tokens(address):
    tokens = get_native_balance(address, "SOL")
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [address, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}, {"encoding": "jsonParsed"}]
        }
        resp = requests.post(SOLANA_RPC_URL, json=payload, timeout=12).json()
        
        for acc in resp.get("result", {}).get("value", []):
            try:
                info = acc["account"]["data"]["parsed"]["info"]
                balance = int(info["tokenAmount"]["amount"]) / (10 ** int(info["tokenAmount"]["decimals"]))
                if balance > 0.0001:
                    mint = info.get("mint", "")[:8]
                    tokens.append({"symbol": f"Token({mint})", "balance": balance})
            except:
                continue
    except Exception as e:
        logger.error(f"SOL Token 查询失败: {e}")
    return tokens


def get_wallet_tokens(address, chain):
    chain = chain.upper()
    tokens = get_native_balance(address, chain)
    
    if chain in ["BSC", "ETH"]:
        tokens.extend(get_erc20_tokens(address, chain))
    elif chain == "SOL":
        tokens.extend(get_solana_tokens(address))   # 已包含 SOL
    
    return tokens


# ========================= 错误处理 =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}")
    if isinstance(context.error, RetryAfter):
        await asyncio.sleep(context.error.retry_after + 1)

# ========================= Handlers =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text(
        "🎉 **欢迎使用钱包监控 Bot**\n支持 BSC • ETH • SOL", 
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data

    if data == 'add_wallet':
        context.user_data['action'] = 'adding'
        await query.message.reply_text("📍 请发送钱包地址：")

    elif data == 'view_wallet':
        addresses = get_user_wallets(chat_id)
        if not addresses:
            await query.message.reply_text("你还没有添加任何钱包。")
            return
        keyboard = [[InlineKeyboardButton(addr[:12] + "...", callback_data=f"addr|{addr}")] for addr in addresses]
        await query.message.reply_text("选择地址查看持仓：", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("addr|"):
        address = data.split("|")[1]
        context.user_data['selected_address'] = address
        chains = get_address_chains(chat_id, address)
        keyboard = [[InlineKeyboardButton(chain, callback_data=f"chain|{chain}")] for chain in chains]
        await query.message.reply_text(f"地址：`{address}`\n请选择链：", 
                                     reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif data.startswith("chain|"):
        chain = data.split("|")[1]
        address = context.user_data.get('selected_address')
        tokens = get_wallet_tokens(address, chain)
        
        if not tokens:
            await query.message.reply_text("⚠️ 未查询到持仓或查询失败。\n请确认地址正确且有资产。")
            return

        msg = f"**{chain} 持仓**\n`{address}`\n\n"
        total = 0
        for t in tokens[:15]:
            price = get_token_price(t['symbol'])
            usd = t['balance'] * (price or 0)
            total += usd
            price_str = f"(${price:.4f})" if price else ""
            msg += f"{t['symbol']}: {t['balance']:.4f} {price_str} ≈ ${usd:.2f}\n"
        
        msg += f"\n**总价值 ≈ ${total:.2f} USD**"
        await query.message.reply_text(msg, parse_mode='Markdown')

    elif data.startswith("addchain|"):
        chain = data.split("|")[1]
        address = context.user_data.get('pending_address')
        if address and add_wallet(chat_id, address, chain):
            await query.message.reply_text(f"✅ 添加成功！\n{address} ({chain})")
        else:
            await query.message.reply_text("❌ 添加失败（可能已存在）")
        context.user_data.clear()

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('action') == 'adding':
        context.user_data['pending_address'] = update.message.text.strip()
        context.user_data['action'] = 'choosing_chain'
        keyboard = [
            [InlineKeyboardButton("BSC", callback_data='addchain|BSC')],
            [InlineKeyboardButton("ETH", callback_data='addchain|ETH')],
            [InlineKeyboardButton("SOL", callback_data='addchain|SOL')]
        ]
        await update.message.reply_text("请选择链：", reply_markup=InlineKeyboardMarkup(keyboard))

# ========================= 监控任务 =========================
async def monitor_task(bot):
    while True:
        try:
            for chat_id, address, chain, last_value in get_wallets_all():
                tokens = get_wallet_tokens(address, chain)
                current_value = sum(t['balance'] * (get_token_price(t.get('symbol', '')) or 0) for t in tokens)

                if last_value > 5 and current_value > 5:
                    change_pct = (current_value - last_value) / last_value * 100
                    if abs(change_pct) >= ALERT_THRESHOLD:
                        sign = "📈" if change_pct > 0 else "📉"
                        try:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=f"{sign} **告警** {chain} `{address[:8]}...`\n"
                                     f"变化: {change_pct:+.2f}%\n当前 ≈ ${current_value:.2f}",
                                parse_mode='Markdown'
                            )
                        except RetryAfter as e:
                            await asyncio.sleep(e.retry_after + 1)
                if current_value > 0.01:
                    update_last_value(chat_id, address, chain, current_value)
        except Exception as e:
            logger.error(f"监控任务出错: {e}")
        await asyncio.sleep(MONITOR_INTERVAL)

# ========================= Post Init =========================
async def post_init(application):
    logger.info("🔄 启动后台监控任务...")
    asyncio.create_task(monitor_task(application.bot))

# ========================= 主函数 =========================
def main():
    if not BOT_TOKEN:
        logger.error("❌ 请设置 BOT_TOKEN 环境变量")
        return

    load_coingecko_coins()
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("🚀 钱包监控 Bot 已启动，正在运行...")
    app.run_polling()

if __name__ == '__main__':
    main()
