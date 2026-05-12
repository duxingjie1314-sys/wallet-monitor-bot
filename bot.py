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
MORALIS_API_KEY = os.environ.get("MORALIS_API_KEY")

COINGECKO_COINS = {}

# ====================== CoinGecko 价格（优化版） ======================
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
    
    s = symbol.lower().strip()
    variants = [s, s.replace(" ", ""), s.replace("-", ""), s.replace("_", "")]
    
    for variant in variants:
        coin_id = COINGECKO_COINS.get(variant)
        if coin_id:
            try:
                r = httpx.get(
                    f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd",
                    timeout=10
                )
                price = r.json().get(coin_id, {}).get("usd")
                if price is not None:
                    return price
            except:
                continue
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


# ====================== Moralis 查询持仓 ======================
def get_wallet_tokens(address: str, chain: str) -> List[Dict]:
    if not MORALIS_API_KEY:
        logger.warning("未配置 MORALIS_API_KEY")
        return []

    chain_map = {"BSC": "bsc", "ETH": "eth"}
    moralis_chain = chain_map.get(chain.upper())
    if not moralis_chain:
        return []

    try:
        url = f"https://deep-index.moralis.io/api/v2.2/{address}/erc20?chain={moralis_chain}"
        headers = {"accept": "application/json", "X-API-Key": MORALIS_API_KEY}
        
        r = httpx.get(url, headers=headers, timeout=25)
        if r.status_code != 200:
            logger.error(f"Moralis 返回 {r.status_code}")
            return []

        data = r.json()
        tokens = []
        for item in data:
            try:
                balance = int(item.get("balance", 0)) / (10 ** int(item.get("decimals", 18)))
                if balance > 0.0001:
                    tokens.append({
                        "symbol": item.get("symbol", "UNKNOWN"),
                        "balance": balance
                    })
            except:
                continue
        logger.info(f"✅ Moralis 查询到 {len(tokens)} 个代币")
        return tokens
    except Exception as e:
        logger.error(f"Moralis 查询异常: {e}")
        return []


# ====================== Telegram Handlers ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ 添加钱包", callback_data='add_wallet')],
        [InlineKeyboardButton("👀 查看我的钱包", callback_data='view_wallet')]
    ]
    await update.message.reply_text("🎉 **Wallet Monitor Bot** 已启动\n使用 Moralis 查询持仓", 
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
            await query.message.reply_text("⚠️ 该地址暂无 ERC20 持仓")
            return

        msg = f"**{chain} 持仓**\n`{addr}`\n\n"
        total = 0.0
        for t in tokens[:30]:
            price = get_token_price(t['symbol'])
            usd = t['balance'] * (price or 0)
            total += usd
            
            if price and price > 0.00001:
                msg += f"• {t['symbol']}: {t['balance']:.4f} ≈ ${usd:.2f}\n"
            else:
                msg += f"• {t['symbol']}: {t['balance']:.4f} (暂无价格)\n"
        
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

    logger.info("🚀 Bot 已成功启动（Moralis + 优化价格版）")
    application.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
