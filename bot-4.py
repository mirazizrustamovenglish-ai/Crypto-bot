import asyncio
import aiohttp
import logging
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
# ============================================================

CHAT_IDS = set()
WATCH_LIST = []

STATS = {
    "total_signals": 0,
    "tp1_hit": 0, "tp2_hit": 0, "tp3_hit": 0, "sl_hit": 0,
    "signals_log": []
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCLAIMER = "\n⚠️ _Moliyaviy maslahat emas._"

COIN_IDS = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
    "bnb": "binancecoin", "xrp": "ripple", "ada": "cardano",
    "doge": "dogecoin", "avax": "avalanche-2", "dot": "polkadot",
    "matic": "matic-network", "link": "chainlink", "uni": "uniswap",
    "ltc": "litecoin", "atom": "cosmos", "near": "near",
}

# ============================================================
# API
# ============================================================
async def get_market_data(coin_id: str):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
    params = {"localization": "false", "tickers": "false",
              "market_data": "true", "community_data": "false", "developer_data": "false"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
    except Exception as e:
        logger.error(f"get_market_data {coin_id}: {e}")
    return None

async def get_ohlc(coin_id: str, days: int = 14):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": str(days)}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
    except Exception as e:
        logger.error(f"get_ohlc {coin_id}: {e}")
    return None

async def fetch_top100():
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {"vs_currency": "usd", "order": "market_cap_desc",
              "per_page": 100, "page": 1, "sparkline": False}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    data = await r.json()
                    return [c["id"] for c in data]
    except Exception as e:
        logger.error(f"fetch_top100: {e}")
    return list(COIN_IDS.values())

# ============================================================
# TEXNIK TAHLIL
# ============================================================
def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag / al)), 1)

def find_swing_highs(ohlc, lookback=3):
    highs = []
    for i in range(lookback, len(ohlc) - lookback):
        ch = ohlc[i][2]
        if ch > max(ohlc[j][2] for j in range(i-lookback, i)) and \
           ch > max(ohlc[j][2] for j in range(i+1, i+lookback+1)):
            highs.append(round(ch, 6))
    return sorted(set(highs))

def find_swing_lows(ohlc, lookback=3):
    lows = []
    for i in range(lookback, len(ohlc) - lookback):
        cl = ohlc[i][3]
        if cl < min(ohlc[j][3] for j in range(i-lookback, i)) and \
           cl < min(ohlc[j][3] for j in range(i+1, i+lookback+1)):
            lows.append(round(cl, 6))
    return sorted(set(lows))

def get_sl_tp(ohlc, price, is_long):
    s_highs = find_swing_highs(ohlc)
    s_lows = find_swing_lows(ohlc)
    if is_long:
        lows_below = [l for l in s_lows if l < price * 0.999]
        sl = max(lows_below) if lows_below else round(price * 0.95, 6)
        tps = sorted([h for h in s_highs if h > price * 1.005])[:3]
        if not tps:
            tps = [round(price*1.05,6), round(price*1.10,6), round(price*1.18,6)]
    else:
        highs_above = [h for h in s_highs if h > price * 1.001]
        sl = min(highs_above) if highs_above else round(price * 1.05, 6)
        tps = sorted([l for l in s_lows if l < price * 0.995], reverse=True)[:3]
        if not tps:
            tps = [round(price*0.95,6), round(price*0.90,6), round(price*0.82,6)]
    return sl, tps

def detect_trend(closes):
    if len(closes) < 6:
        return None
    r = closes[-6:]
    if r[-1] > r[0] * 1.02:
        return "bullish"
    elif r[-1] < r[0] * 0.98:
        return "bearish"
    return "sideways"

def is_near_support(price, ohlc):
    lows = find_swing_lows(ohlc)
    lows_below = [l for l in lows if l < price]
    if not lows_below:
        return False
    return price <= max(lows_below) * 1.025

def is_near_resistance(price, ohlc):
    highs = find_swing_highs(ohlc)
    highs_above = [h for h in highs if h > price]
    if not highs_above:
        return False
    return price >= min(highs_above) * 0.975

def check_volume_spike(vol_24h, avg_vol):
    if avg_vol <= 0:
        return False, 0
    ratio = vol_24h / avg_vol
    return ratio >= 2.0, round(ratio, 1)

# ============================================================
# SIGNAL TEKSHIRISH
# ============================================================
async def check_signal(coin_id: str):
    data = await get_market_data(coin_id)
    if not data:
        return None

    md = data.get("market_data", {})
    name = data.get("name", coin_id)
    symbol = data.get("symbol", "").upper()
    price = md.get("current_price", {}).get("usd", 0)
    vol_24h = md.get("total_volume", {}).get("usd", 0)
    change_24h = md.get("price_change_percentage_24h", 0) or 0

    avg_vol = vol_24h / (abs(change_24h) / 5 + 1) if change_24h else vol_24h * 0.6
    spike, ratio = check_volume_spike(vol_24h, avg_vol)
    if not spike:
        return None

    ohlc = await get_ohlc(coin_id)
    if not ohlc or len(ohlc) < 10:
        return None

    closes = [c[4] for c in ohlc]
    trend = detect_trend(closes)
    rsi = calc_rsi(closes)

    near_sup = is_near_support(price, ohlc)
    near_res = is_near_resistance(price, ohlc)

    if trend == "bullish":
        direction = "LONG 📈"
        is_long = True
    elif trend == "bearish":
        direction = "SHORT 📉"
        is_long = False
    else:
        return None

    sl, tps = get_sl_tp(ohlc, price, is_long)

    strength_count = 1
    if near_sup or near_res:
        strength_count += 1
    if rsi and ((is_long and rsi < 40) or (not is_long and rsi > 60)):
        strength_count += 1

    if strength_count >= 3:
        strength = "🔴 KUCHLI"
    elif strength_count == 2:
        strength = "🟡 O'RTA"
    else:
        strength = "🟢 ZAIF"

    STATS["total_signals"] += 1
    signal_id = STATS["total_signals"]
    STATS["signals_log"].append({
        "id": signal_id, "coin": symbol, "coin_id": coin_id,
        "entry": price, "sl": sl,
        "tp1": tps[0] if len(tps) > 0 else None,
        "tp2": tps[1] if len(tps) > 1 else None,
        "tp3": tps[2] if len(tps) > 2 else None,
        "direction": direction, "status": "🔄 Ochiq",
        "time": datetime.now().strftime('%Y-%m-%d %H:%M'),
    })

    sl_pct = (sl - price) / price * 100
    tp_text = ""
    for i, tp in enumerate(tps[:3], 1):
        pct = (tp - price) / price * 100
        tp_text += f"• TP{i}: `${tp:,.4f}` ({pct:+.1f}%)\n"

    extra = []
    if near_sup: extra.append("Support ✅")
    if near_res: extra.append("Resistance ✅")
    if rsi: extra.append(f"RSI: {rsi}")
    extra_text = " | ".join(extra)

    msg = f"""🚨 *{name} ({symbol}) — {strength}*

Yo'nalish: *{direction}*
⚡ Volume: *{ratio}x* noodatiy
{extra_text}

💰 Narx: `${price:,.4f}`
❌ SL: `${sl:,.4f}` ({sl_pct:+.1f}%)
{tp_text}{DISCLAIMER}"""

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Batafsil", callback_data=f"detail_{coin_id}"),
        InlineKeyboardButton(f"✅ TP1 #{signal_id}", callback_data=f"tp1_{signal_id}"),
        InlineKeyboardButton(f"❌ SL #{signal_id}", callback_data=f"sl_{signal_id}"),
    ]])

    return msg, keyboard

# ============================================================
# COIN MA'LUMOTI
# ============================================================
async def get_coin_info(coin_id: str):
    data = await get_market_data(coin_id)
    if not data:
        return "❌ Ma'lumot topilmadi.", None

    md = data.get("market_data", {})
    name = data.get("name", coin_id)
    symbol = data.get("symbol", "").upper()
    price = md.get("current_price", {}).get("usd", 0)
    change_24h = md.get("price_change_percentage_24h", 0) or 0
    change_7d = md.get("price_change_percentage_7d", 0) or 0
    market_cap = md.get("market_cap", {}).get("usd", 0)
    vol_24h = md.get("total_volume", {}).get("usd", 0)
    ath = md.get("ath", {}).get("usd", 0)
    ath_change = md.get("ath_change_percentage", {}).get("usd", 0) or 0
    supply = md.get("circulating_supply", 0)

    c24 = "🟢" if change_24h >= 0 else "🔴"
    c7 = "🟢" if change_7d >= 0 else "🔴"

    msg = f"""📊 *{name} ({symbol})*

💰 Narx: `${price:,.4f}`
{c24} 24h: `{change_24h:+.2f}%`
{c7} 7d: `{change_7d:+.2f}%`
📈 ATH: `${ath:,.2f}` ({ath_change:.1f}%)
💎 Market Cap: `${market_cap/1e9:.2f}B`
📊 Hajm: `${vol_24h/1e6:.1f}M`
🔄 Ta'minot: `{supply:,.0f} {symbol}`{DISCLAIMER}"""

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yangilash", callback_data=f"detail_{coin_id}"),
        InlineKeyboardButton("📈 Signal tekshir", callback_data=f"signal_{coin_id}"),
    ]])

    return msg, keyboard

# ============================================================
# TUGMALAR (KEYBOARD)
# ============================================================
def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("₿ BTC", callback_data="detail_bitcoin"),
            InlineKeyboardButton("Ξ ETH", callback_data="detail_ethereum"),
            InlineKeyboardButton("◎ SOL", callback_data="detail_solana"),
        ],
        [
            InlineKeyboardButton("🔔 Signallarni yoqish", callback_data="watch_on"),
            InlineKeyboardButton("🔕 O'chirish", callback_data="watch_off"),
        ],
        [
            InlineKeyboardButton("📊 Hisobot", callback_data="hisobot"),
            InlineKeyboardButton("🔍 Hozir tekshir", callback_data="scan_now"),
        ]
    ])

# ============================================================
# HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text(
        "👋 *Rustamov Crypto Botiga xush kelibsiz!*\n\n"
        "🔍 Top 100 token kuzatiladi\n"
        "⚡ Noodatiy volume + Trend + Support/Resistance\n"
        "📩 Signal bo'lganda darhol xabar keladi\n\n"
        "Quyidagi tugmalardan foydalaning 👇",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("detail_"):
        coin_id = data.replace("detail_", "")
        msg, keyboard = await get_coin_info(coin_id)
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=keyboard)

    elif data.startswith("signal_"):
        coin_id = data.replace("signal_", "")
        await query.edit_message_text("⏳ Tekshirilmoqda...", parse_mode="Markdown")
        result = await check_signal(coin_id)
        if result:
            msg, keyboard = result
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=keyboard)
        else:
            msg, keyboard = await get_coin_info(coin_id)
            await query.edit_message_text(
                "📭 Hozircha signal yo'q.\n\n" + msg,
                parse_mode="Markdown", reply_markup=keyboard
            )

    elif data == "watch_on":
        CHAT_IDS.add(query.from_user.id)
        await query.edit_message_text(
            "✅ *Signallar yoqildi!*\n"
            "Top 100 token har soatda tekshiriladi.\n"
            "Faqat signal bo'lganda xabar keladi! 🚨\n\n"
            "Quyidagi tugmalardan foydalaning 👇",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    elif data == "watch_off":
        CHAT_IDS.discard(query.from_user.id)
        await query.edit_message_text(
            "🔕 *Signallar o'chirildi.*\n\n"
            "Quyidagi tugmalardan foydalaning 👇",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    elif data == "hisobot":
        total = STATS["total_signals"]
        if total == 0:
            text = "📊 *Hisobot*\n\nHali signal berilmagan."
        else:
            tp_total = STATS["tp1_hit"] + STATS["tp2_hit"] + STATS["tp3_hit"]
            sl = STATS["sl_hit"]
            ochiq = total - tp_total - sl
            win_rate = round(tp_total / (tp_total + sl) * 100, 1) if (tp_total + sl) > 0 else 0
            text = f"📊 *Hisobot*\n\nJami: `{total}` | TP: `{tp_total}` | SL: `{sl}` | Ochiq: `{ochiq}`\nWin rate: `{win_rate}%`"
            if STATS["signals_log"]:
                text += "\n\n*So'nggi signallar:*\n"
                for s in reversed(STATS["signals_log"][-5:]):
                    text += f"{s['status']} #{s['id']} *{s['coin']}* | `${s['entry']:,.4f}` | {s['time']}\n"
        text += DISCLAIMER
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Orqaga", callback_data="back_main")
        ]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "scan_now":
        await query.edit_message_text("⏳ Top 20 token tekshirilmoqda...")
        found = 0
        for coin_id in WATCH_LIST[:20]:
            result = await check_signal(coin_id)
            if result:
                msg, keyboard = result
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=msg, parse_mode="Markdown", reply_markup=keyboard
                )
                found += 1
            await asyncio.sleep(1.5)
        if found == 0:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="📭 Hozircha signal topilmadi.",
                reply_markup=main_keyboard()
            )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"✅ {found} ta signal topildi!",
                reply_markup=main_keyboard()
            )

    elif data == "back_main":
        await query.edit_message_text(
            "👋 *Rustamov Crypto Bot*\n\nQuyidagi tugmalardan foydalaning 👇",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    elif data.startswith("tp1_"):
        sid = int(data.replace("tp1_", ""))
        _mark(sid, "TP1")
        await query.answer("✅ TP1 belgilandi!", show_alert=True)

    elif data.startswith("sl_"):
        sid = int(data.replace("sl_", ""))
        _mark(sid, "SL")
        await query.answer("❌ SL belgilandi!", show_alert=True)

def _mark(signal_id, status):
    for s in STATS["signals_log"]:
        if s["id"] == signal_id:
            s["status"] = f"{'✅' if 'TP' in status else '❌'} {status}"
            if status == "TP1": STATS["tp1_hit"] += 1
            elif status == "TP2": STATS["tp2_hit"] += 1
            elif status == "TP3": STATS["tp3_hit"] += 1
            elif status == "SL": STATS["sl_hit"] += 1

# ============================================================
# HAR SOATLIK SKAN
# ============================================================
async def hourly_scan(bot):
    global WATCH_LIST
    logger.info("Top 100 yuklanmoqda...")
    WATCH_LIST = await fetch_top100()
    logger.info(f"{len(WATCH_LIST)} token yuklandi")

    while True:
        await asyncio.sleep(3600)
        if not CHAT_IDS:
            continue

        new_list = await fetch_top100()
        if new_list:
            WATCH_LIST = new_list

        logger.info(f"Skan — {len(WATCH_LIST)} token")
        found = 0

        for coin_id in WATCH_LIST:
            try:
                result = await check_signal(coin_id)
                if result:
                    msg, keyboard = result
                    found += 1
                    for chat_id in list(CHAT_IDS):
                        try:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=msg,
                                parse_mode="Markdown",
                                reply_markup=keyboard
                            )
                        except Exception as e:
                            logger.error(f"Yuborishda xato: {e}")
                await asyncio.sleep(1.5)
            except Exception as e:
                logger.error(f"{coin_id}: {e}")

        logger.info(f"Skan tugadi — {found} signal")

# ============================================================
# MAIN
# ============================================================
async def post_init(app):
    asyncio.create_task(hourly_scan(app.bot))
    logger.info("✅ Bot ishga tushdi!")

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
