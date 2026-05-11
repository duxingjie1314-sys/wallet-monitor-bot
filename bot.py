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
        resp = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd", timeout=8).json()
        return resp.get(coin_id, {}).get("usd")
    except:
        return None

# ========================= 数据库（不变）=========================
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

# ========================= 链上查询（已修复）=========================
def safe_get_json(resp):
    """安全获取JSON"""
    try:
        data = resp.json()
        if isinstance(data, str):
            logger.error(f"API 返回字符串错误: {data[:200]}")
            return {"status": "0", "result": "0", "message": data}
        return data
    except:
        return {"status": "0", "result": "0"}

def get_native_balance(address, chain):
    logger.info(f"查询原生币 {chain} {address[:8]}...")
    try:
        if chain == "BSC" and BSCSCAN_API_KEY:
            url = f"https://api.bscscan.com/api?module=account&action=balance&address={address}&apikey={BSCSCAN_API_KEY}"
            resp = requests.get(url, timeout=10)
            data = safe_get_json(resp)
            balance = int(data.get("result", 0)) / 10**18
            logger.info(f"BSC 原生币余额: {balance:.4f}")
            return [{"symbol": "BNB", "balance": balance}] if balance > 0.001 else []

        elif chain == "ETH" and ETHERSCAN_API_KEY:
            url = f"https://api.etherscan.io/api?module=account&action=balance&address={address}&apikey={ETHERSCAN_API_KEY}"
            resp = requests.get(url, timeout=10)
            data = safe_get_json(resp)
            balance = int(data.get("result", 0)) / 10**18
            logger.info(f"ETH 原生币余额: {balance:.4f}")
            return [{"symbol": "ETH", "balance": balance}] if balance > 0.001 else []

        elif chain == "SOL":
            payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]}
            resp = requests.post(SOLANA_RPC_URL, json=payload, timeout=10).json()
            balance = resp.get("result", {}).get("value", 0) / 10**9
            logger.info(f"SOL 原生币余额: {balance:.4f}")
            return [{"symbol": "SOL", "balance": balance}] if balance > 0.001 else []
    except Exception as e:
        logger.error(f"原生币查询异常: {e}")
    return []


def get_erc20_tokens(address, chain):
    logger.info(f"查询 {chain} Token 持仓 {address[:8]}...")
    if chain == "BSC":
        base_url, api_key = "https://api.bscscan.com/api", BSCSCAN_API_KEY
    elif chain == "ETH":
        base_url, api_key = "https://api.etherscan.io/api", ETHERSCAN_API_KEY
    else:
        return []
    if not api_key:
        logger.warning(f"缺少 {chain} API Key")
        return []

    try:
        url = f"{base_url}?module=account&action=tokentx&address={address}&page=1&offset=100&sort=desc&apikey={api_key}"
        resp = requests.get(url, timeout=12)
        data = safe_get_json(resp)
        
        result = data.get("result", [])
        if isinstance(result, str):
            logger.error(f"tokentx 返回错误: {result}")
            result = []
            
        logger.info(f"tokentx 返回 {len(result)} 条记录")

        token_dict = {}
        for tx in result:
            symbol = tx.get("tokenSymbol")
            contract = tx.get("contractAddress")
            if symbol and contract:
                token_dict[symbol] = contract

        tokens = []
        for symbol, contract in list(token_dict.items())[:20]:
            try:
                bal_url = f"{base_url}?module=account&action=tokenbalance&contractaddress={contract}&address={address}&apikey={api_key}"
                bal_resp = requests.get(bal_url, timeout=8)
                bal_data = safe_get_json(bal_resp)
                balance_str = bal_data.get("result", "0")
                balance = int(balance_str) / 10**18
                if balance > 0.0001:
                    tokens.append({"symbol": symbol, "balance": balance})
                    logger.info(f"✅ 找到持仓: {symbol} = {balance:.4f}")
            except Exception as e:
                logger.warning(f"单个 Token 余额查询失败 {symbol}: {e}")
                continue
        return tokens
    except Exception as e:
        logger.error(f"ERC20 查询异常: {e}")
        return []


def get_solana_tokens(address):
    tokens = get_native_balance(address, "SOL")
    # SPL Token 查询保持不变...
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [address, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}, {"encoding": "jsonParsed"}]
        }
        resp = requests.post(SOLANA_RPC_URL, json=payload, timeout=12).json()
        count = 0
        for acc in resp.get("result", {}).get("value", []):
            try:
                info = acc["account"]["data"]["parsed"]["info"]
                balance = int(info["tokenAmount"]["amount"]) / (10 ** int(info["tokenAmount"]["decimals"]))
                if balance > 0.0001:
                    tokens.append({"symbol": "SPL-Token", "balance": balance})
                    count += 1
            except:
                continue
        logger.info(f"SOL 找到 {count} 个 SPL Token")
    except Exception as e:
        logger.error(f"SOL Token 查询异常: {e}")
    return tokens


def get_wallet_tokens(address, chain):
    chain = chain.upper()
    tokens = get_native_balance(address, chain)
    
    if chain in ["BSC", "ETH"]:
        tokens.extend(get_erc20_tokens(address, chain))
    elif chain == "SOL":
        tokens.extend(get_solana_tokens(address))
    
    logger.info(f"{chain} 最终找到 {len(tokens)} 个资产")
    return tokens


# ========================= 其他部分（Handlers + Main）=========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"错误: {context.error}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text("🎉 **钱包监控 Bot** 已就绪", 
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

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
            await query.message.reply_text("暂无钱包")
            return
        kb = [[InlineKeyboardButton(a[:12]+"...", callback_data=f"addr|{a}")] for a in addresses]
        await query.message.reply_text("选择地址：", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("addr|"):
        addr = data.split("|")[1]
        context.user_data['selected_address'] = addr
        chains = get_address_chains(chat_id, addr)
        kb = [[InlineKeyboardButton(c, callback_data=f"chain|{c}")] for c in chains]
        await query.message.reply_text(f"地址：`{addr}`\n选择链：", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

    elif data.startswith("chain|"):
        chain = data.split("|")[1]
        address = context.user_data.get('selected_address')
        tokens = get_wallet_tokens(address, chain)
        
        if not tokens:
            await query.message.reply_text("⚠️ 未查询到持仓。\n建议使用有较多交易记录的地址测试。")
            return

        msg = f"**{chain} 持仓**\n`{address}`\n\n"
        total = 0
        for t in tokens[:15]:
            price = get_token_price(t['symbol'])
            usd = t['balance'] * (price or 0)
            total += usd
            msg += f"{t['symbol']}: {t['balance']:.4f} ≈ ${usd:.2f}\n"
        msg += f"\n**总价值 ≈ ${total:.2f}**"
        await query.message.reply_text(msg, parse_mode='Markdown')

    elif data.startswith("addchain|"):
        chain = data.split("|")[1]
        addr = context.user_data.get('pending_address')
        if addr and add_wallet(chat_id, addr, chain):
            await query.message.reply_text(f"✅ 添加成功\n{addr} ({chain})")
        else:
            await query.message.reply_text("❌ 添加失败（可能已存在）")
        context.user_data.clear()

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('action') == 'adding':
        context.user_data['pending_address'] = update.message.text.strip()
        context.user_data['action'] = 'choosing_chain'
        kb = [[InlineKeyboardButton(c, callback_data=f'addchain|{c}')] for c in ["BSC","ETH","SOL"]]
        await update.message.reply_text("请选择链：", reply_markup=InlineKeyboardMarkup(kb))

async def monitor_task(bot):
    while True:
        try:
            for chat_id, address, chain, last_value in get_wallets_all():
                tokens = get_wallet_tokens(address, chain)
                current = sum(t['balance'] * (get_token_price(t.get('symbol','')) or 0) for t in tokens)
                if current > 0.01:
                    update_last_value(chat_id, address, chain, current)
        except Exception as e:
            logger.error(f"监控出错: {e}")
        await asyncio.sleep(MONITOR_INTERVAL)

async def post_init(application):
    logger.info("🔄 启动监控任务...")
    asyncio.create_task(monitor_task(application.bot))

def main():
    if not BOT_TOKEN:
        logger.error("缺少 BOT_TOKEN")
        return
    load_coingecko_coins()
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    logger.info("🚀 Bot 已启动")
    app.run_polling()

if __name__ == '__main__':
    main()
