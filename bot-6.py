import asyncio
import aiohttp
import logging
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

BOT_TOKEN = os.environ.get("BOT_TOKEN")

CHAT_IDS = set()
WATCH_LIST = []
CACHE = {}  # {coin_id: {data, ohlc_1d, ohlc_4h, ohlc_1h, timestamp}}

STATS = {
    "total_signals": 0,
    "tp1_hit": 0, "tp2_hit": 0, "tp3_hit": 0, "sl_hit": 0,
    "signals_log": []
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCLAIMER = "\n⚠️ _Moliyaviy maslahat emas._"

COIN_MAP = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
    "bnb": "binancecoin", "xrp": "ripple", "ada": "cardano",
    "doge": "dogecoin", "avax": "avalanche-2", "dot": "polkadot",
    "matic": "matic-network", "link": "chainlink", "uni": "uniswap",
    "ltc": "litecoin", "atom": "cosmos", "near": "near",
    "ftm": "fantom", "algo": "algorand", "xlm": "stellar",
    "vet": "vechain", "trx": "tron", "fil": "filecoin",
    "sand": "the-sandbox", "mana": "decentraland", "axs": "axie-infinity",
    "aave": "aave", "comp": "compound-governance-token", "snx": "synthetix-network-token",
}

# ============================================================
# API — parallel so'rovlar
# ============================================================
async def api_get(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status == 200:
                return await r.json()
            elif r.status == 429:
                await asyncio.sleep(10)
    except Exception as e:
        logger.error(f"API xato {url}: {e}")
    return None

async def fetch_top200():
    url = "https://api.coingecko.com/api/v3/coins/markets"
    results = []
    async with aiohttp.ClientSession() as s:
        for page in [1, 2]:
            data = await api_get(s, url, {
                "vs_currency": "usd", "order": "market_cap_desc",
                "per_page": 100, "page": page, "sparkline": False
            })
            if data:
                results.extend([c["id"] for c in data])
            await asyncio.sleep(1.5)
    return results if results else list(COIN_MAP.values())

async def fetch_coin_all_data(coin_id: str):
    """Bir coin uchun barcha ma'lumotlarni parallel olish"""
    now = datetime.now().timestamp()
    if coin_id in CACHE and now - CACHE[coin_id].get("ts", 0) < 300:
        return CACHE[coin_id]

    async with aiohttp.ClientSession() as s:
        # Parallel ravishda 4 ta so'rov
        tasks = [
            api_get(s, f"https://api.coingecko.com/api/v3/coins/{coin_id}", {
                "localization": "false", "tickers": "false",
                "market_data": "true", "community_data": "false", "developer_data": "true"
            }),
            api_get(s, f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
                    {"vs_currency": "usd", "days": "30"}),   # 1D uchun
            api_get(s, f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
                    {"vs_currency": "usd", "days": "7"}),    # 4H uchun
            api_get(s, f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
                    {"vs_currency": "usd", "days": "1"}),    # 1H uchun
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    market_data = results[0] if not isinstance(results[0], Exception) else None
    ohlc_1d = results[1] if not isinstance(results[1], Exception) else None
    ohlc_4h = results[2] if not isinstance(results[2], Exception) else None
    ohlc_1h = results[3] if not isinstance(results[3], Exception) else None

    if not market_data:
        return None

    result = {
        "market": market_data,
        "ohlc_1d": ohlc_1d,
        "ohlc_4h": ohlc_4h,
        "ohlc_1h": ohlc_1h,
        "ts": now
    }
    CACHE[coin_id] = result
    return result

# ============================================================
# TEXNIK TAHLIL
# ============================================================
def calc_rsi(prices, period=14):
    if not prices or len(prices) < period + 1:
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

def calc_atr(ohlc, period=14):
    if not ohlc or len(ohlc) < period + 1:
        return None
    trs = []
    for i in range(1, len(ohlc)):
        h, l, pc = ohlc[i][2], ohlc[i][3], ohlc[i-1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period if trs else None

def find_swing_highs(ohlc, lookback=3):
    if not ohlc or len(ohlc) < lookback * 2 + 1:
        return []
    highs = []
    for i in range(lookback, len(ohlc) - lookback):
        ch = ohlc[i][2]
        if ch > max(ohlc[j][2] for j in range(i-lookback, i)) and \
           ch > max(ohlc[j][2] for j in range(i+1, i+lookback+1)):
            highs.append(round(ch, 8))
    return sorted(set(highs))

def find_swing_lows(ohlc, lookback=3):
    if not ohlc or len(ohlc) < lookback * 2 + 1:
        return []
    lows = []
    for i in range(lookback, len(ohlc) - lookback):
        cl = ohlc[i][3]
        if cl < min(ohlc[j][3] for j in range(i-lookback, i)) and \
           cl < min(ohlc[j][3] for j in range(i+1, i+lookback+1)):
            lows.append(round(cl, 8))
    return sorted(set(lows))

def detect_trend(closes):
    if not closes or len(closes) < 6:
        return "sideways"
    r = closes[-6:]
    if r[-1] > r[0] * 1.02:
        return "bullish"
    elif r[-1] < r[0] * 0.98:
        return "bearish"
    return "sideways"

def detect_bos(closes):
    """Break of Structure"""
    if not closes or len(closes) < 10:
        return None
    ph = max(closes[-10:-5])
    pl = min(closes[-10:-5])
    curr = closes[-1]
    if curr > ph:
        return "bullish"
    elif curr < pl:
        return "bearish"
    return None

def detect_order_block(ohlc, trend):
    """Order Block — katta harakatdan oldingi oxirgi qarshi sham"""
    if not ohlc or len(ohlc) < 5:
        return None, None
    if trend == "bullish":
        # Bullish OB — pastga ketayotgan shamlar orasida eng kuchli
        for i in range(len(ohlc)-3, 0, -1):
            if ohlc[i][4] < ohlc[i][1]:  # Bearish sham
                ob_high = ohlc[i][2]
                ob_low = ohlc[i][3]
                curr_price = ohlc[-1][4]
                if ob_low <= curr_price <= ob_high * 1.01:
                    return ob_low, ob_high
    else:
        # Bearish OB
        for i in range(len(ohlc)-3, 0, -1):
            if ohlc[i][4] > ohlc[i][1]:  # Bullish sham
                ob_high = ohlc[i][2]
                ob_low = ohlc[i][3]
                curr_price = ohlc[-1][4]
                if ob_low * 0.99 <= curr_price <= ob_high:
                    return ob_low, ob_high
    return None, None

def detect_fvg(ohlc):
    """Fair Value Gap"""
    if not ohlc or len(ohlc) < 3:
        return None
    for i in range(len(ohlc)-3, 0, -1):
        c1h, c1l = ohlc[i][2], ohlc[i][3]
        c3h, c3l = ohlc[i+2][2], ohlc[i+2][3]
        if c3l > c1h:
            return f"Bullish FVG: ${c1h:,.4f}—${c3l:,.4f}"
        elif c3h < c1l:
            return f"Bearish FVG: ${c3h:,.4f}—${c1l:,.4f}"
    return None

def detect_liquidity(ohlc, price):
    """Liquidity zones — equal highs/lows"""
    if not ohlc or len(ohlc) < 10:
        return None
    highs = [c[2] for c in ohlc[-20:]]
    lows = [c[3] for c in ohlc[-20:]]
    tolerance = price * 0.003

    # Equal highs (sell-side liquidity)
    eq_highs = [h for h in highs if abs(h - max(highs)) < tolerance]
    if len(eq_highs) >= 2 and abs(price - max(highs)) / price < 0.02:
        return f"Sell liquidity: ${max(highs):,.4f}"

    # Equal lows (buy-side liquidity)
    eq_lows = [l for l in lows if abs(l - min(lows)) < tolerance]
    if len(eq_lows) >= 2 and abs(price - min(lows)) / price < 0.02:
        return f"Buy liquidity: ${min(lows):,.4f}"

    return None

def get_sl_tp(ohlc, price, is_long):
    s_highs = find_swing_highs(ohlc)
    s_lows = find_swing_lows(ohlc)

    if is_long:
        lows_below = [l for l in s_lows if l < price * 0.999]
        sl = max(lows_below) if lows_below else round(price * 0.95, 8)
        tps = sorted([h for h in s_highs if h > price * 1.005])[:3]
        if not tps:
            atr = calc_atr(ohlc)
            if atr:
                tps = [round(price+atr*1.5,8), round(price+atr*3,8), round(price+atr*5,8)]
            else:
                tps = [round(price*1.05,8), round(price*1.10,8), round(price*1.18,8)]
    else:
        highs_above = [h for h in s_highs if h > price * 1.001]
        sl = min(highs_above) if highs_above else round(price * 1.05, 8)
        tps = sorted([l for l in s_lows if l < price * 0.995], reverse=True)[:3]
        if not tps:
            atr = calc_atr(ohlc)
            if atr:
                tps = [round(price-atr*1.5,8), round(price-atr*3,8), round(price-atr*5,8)]
            else:
                tps = [round(price*0.95,8), round(price*0.90,8), round(price*0.82,8)]
    return sl, tps

def check_volume_spike(vol_24h, avg_vol):
    if avg_vol <= 0:
        return False, 0
    ratio = vol_24h / avg_vol
    return ratio >= 2.0, round(ratio, 1)

# ============================================================
# FUNDAMENTAL SKOR
# ============================================================
def fundamental_score(data):
    score = 0
    md = data.get("market_data", {})

    mcap = md.get("market_cap", {}).get("usd", 0)
    if mcap > 100_000_000_000: score += 3
    elif mcap > 10_000_000_000: score += 2
    elif mcap > 1_000_000_000: score += 1

    vol = md.get("total_volume", {}).get("usd", 0)
    if mcap > 0:
        r = vol / mcap
        if r > 0.1: score += 2
        elif r > 0.03: score += 1

    circ = md.get("circulating_supply", 0)
    total = md.get("total_supply", 1) or 1
    pct = circ / total
    if pct > 0.7: score += 2
    elif pct > 0.4: score += 1

    ath_chg = md.get("ath_change_percentage", {}).get("usd", 0) or 0
    if ath_chg < -70: score += 2
    elif ath_chg < -30: score += 1

    commits = data.get("developer_data", {}).get("commit_count_4_weeks", 0) or 0
    if commits > 10: score += 1

    return round((score / 10) * 10, 1)

# ============================================================
# MULTI-TIMEFRAME TAHLIL
# ============================================================
def multi_tf_analysis(ohlc_1d, ohlc_4h, ohlc_1h):
    """1D, 4H, 1H trendlarini aniqlash"""
    results = {}

    for tf, ohlc in [("1D", ohlc_1d), ("4H", ohlc_4h), ("1H", ohlc_1h)]:
        if ohlc:
            closes = [c[4] for c in ohlc]
            results[tf] = detect_trend(closes)
        else:
            results[tf] = "sideways"

    # Umumiy yo'nalish
    bullish_count = sum(1 for t in results.values() if t == "bullish")
    bearish_count = sum(1 for t in results.values() if t == "bearish")

    if bullish_count >= 2:
        overall = "bullish"
    elif bearish_count >= 2:
        overall = "bearish"
    else:
        overall = "sideways"

    return results, overall

# ============================================================
# ASOSIY SIGNAL TEKSHIRISH
# ============================================================
async def analyze_coin(coin_id: str, force_signal=False):
    all_data = await fetch_coin_all_data(coin_id)
    if not all_data:
        return None

    market = all_data["market"]
    ohlc_1d = all_data["ohlc_1d"]
    ohlc_4h = all_data["ohlc_4h"]
    ohlc_1h = all_data["ohlc_1h"]

    md = market.get("market_data", {})
    name = market.get("name", coin_id)
    symbol = market.get("symbol", "").upper()
    price = md.get("current_price", {}).get("usd", 0)
    vol_24h = md.get("total_volume", {}).get("usd", 0)
    change_24h = md.get("price_change_percentage_24h", 0) or 0
    change_7d = md.get("price_change_percentage_7d", 0) or 0
    ath = md.get("ath", {}).get("usd", 0)
    ath_change = md.get("ath_change_percentage", {}).get("usd", 0) or 0
    market_cap = md.get("market_cap", {}).get("usd", 0)

    # Volume spike
    avg_vol = vol_24h / (abs(change_24h) / 5 + 1) if change_24h else vol_24h * 0.6
    spike, ratio = check_volume_spike(vol_24h, avg_vol)

    # Multi-timeframe
    tf_trends, overall_trend = multi_tf_analysis(ohlc_1d, ohlc_4h, ohlc_1h)

    # Asosiy OHLC — 4H
    main_ohlc = ohlc_4h or ohlc_1d or ohlc_1h
    if not main_ohlc:
        return None

    closes = [c[4] for c in main_ohlc]
    rsi = calc_rsi(closes)
    bos = detect_bos(closes)
    fvg = detect_fvg(main_ohlc)
    ob_low, ob_high = detect_order_block(main_ohlc, overall_trend)
    liquidity = detect_liquidity(main_ohlc, price)
    fund_score = fundamental_score(market)

    # Swing support/resistance
    s_highs = find_swing_highs(main_ohlc)
    s_lows = find_swing_lows(main_ohlc)
    near_sup = any(price <= l * 1.025 for l in s_lows if l < price)
    near_res = any(price >= h * 0.975 for h in s_highs if h > price)

    # Yo'nalish
    if overall_trend == "bullish":
        is_long = True
    elif overall_trend == "bearish":
        is_long = False
    else:
        if not force_signal:
            return None
        is_long = change_24h >= 0

    # Signal kuchi hisoblash
    score = 0
    factors = []

    if spike:
        score += 3
        factors.append(f"⚡ Volume {ratio}x")
    if near_sup and is_long:
        score += 2
        factors.append("🛡 Support")
    if near_res and not is_long:
        score += 2
        factors.append("🛡 Resistance")
    if bos == ("bullish" if is_long else "bearish"):
        score += 2
        factors.append("📐 BOS")
    if ob_low and ob_high:
        score += 2
        factors.append("📦 Order Block")
    if rsi and ((is_long and rsi < 35) or (not is_long and rsi > 65)):
        score += 2
        factors.append(f"📊 RSI {rsi}")
    if fvg:
        score += 1
        factors.append("🔲 FVG")
    if liquidity:
        score += 1
        factors.append("💧 Liquidity")

    # Signal yo'q — agar force emas va score past
    if not force_signal and (score < 4 or not spike):
        return None

    if score >= 8:
        strength = "🔴 KUCHLI"
    elif score >= 5:
        strength = "🟡 O'RTA"
    else:
        strength = "🟢 ZAIF"

    direction = "LONG 📈" if is_long else "SHORT 📉"
    sl, tps = get_sl_tp(main_ohlc, price, is_long)

    # Risk/Reward
    risk = abs(price - sl)
    rr = round(abs(tps[0] - price) / risk, 1) if risk > 0 and tps else 0

    # TF matni
    tf_text = ""
    for tf, t in tf_trends.items():
        e = "📈" if t == "bullish" else ("📉" if t == "bearish" else "↔️")
        tf_text += f"{e}{tf} "

    # Signal saqlash
    STATS["total_signals"] += 1
    sid = STATS["total_signals"]
    STATS["signals_log"].append({
        "id": sid, "coin": symbol, "coin_id": coin_id,
        "entry": price, "sl": sl,
        "tp1": tps[0] if len(tps) > 0 else None,
        "tp2": tps[1] if len(tps) > 1 else None,
        "tp3": tps[2] if len(tps) > 2 else None,
        "direction": "LONG" if is_long else "SHORT",
        "score": score, "status": "🔄 Ochiq",
        "time": datetime.now().strftime('%Y-%m-%d %H:%M'),
    })

    sl_pct = (sl - price) / price * 100
    tp_text = ""
    for i, tp in enumerate(tps[:3], 1):
        pct = (tp - price) / price * 100
        tp_text += f"• TP{i}: `${tp:,.4f}` ({pct:+.1f}%)\n"

    # SIGNAL XABAR — qisqa
    signal_msg = f"""🚨 *#{sid} — {name} ({symbol}) — {strength}*

{direction} | R/R: `1:{rr}`
{tf_text}
{" | ".join(factors)}

💰 `${price:,.4f}`
❌ SL: `${sl:,.4f}` ({sl_pct:+.1f}%)
{tp_text}{DISCLAIMER}"""

    # BATAFSIL XABAR
    detail_msg = f"""📊 *{name} ({symbol}) — Batafsil*

💰 Narx: `${price:,.4f}`
24h: `{change_24h:+.2f}%` | 7d: `{change_7d:+.2f}%`
ATH: `${ath:,.2f}` ({ath_change:.1f}%)
Market Cap: `${market_cap/1e9:.2f}B`

📈 *Multi-Timeframe:*
{tf_text}

🔬 *SMC Tahlil:*
• RSI: `{rsi or 'N/A'}`
• BOS: `{bos or "Yoq"}`
• FVG: `{fvg or "Yoq"}`
• Order Block: `{("${:.4f}—${:.4f}".format(ob_low, ob_high)) if ob_low else "Yoq"}`
• Liquidity: `{liquidity or "Yoq"}`

⭐ Fundamental: `{fund_score}/10`
🎯 Signal kuchi: `{score}/13`{DISCLAIMER}"""

    signal_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Batafsil", callback_data=f"det_{coin_id}_{sid}"),
        InlineKeyboardButton("✅ TP1", callback_data=f"tp1_{sid}"),
        InlineKeyboardButton("✅ TP2", callback_data=f"tp2_{sid}"),
    ], [
        InlineKeyboardButton("✅ TP3", callback_data=f"tp3_{sid}"),
        InlineKeyboardButton("❌ SL", callback_data=f"sl_{sid}"),
    ]])

    return {
        "signal_msg": signal_msg,
        "detail_msg": detail_msg,
        "keyboard": signal_keyboard,
        "coin_id": coin_id,
        "symbol": symbol,
        "name": name,
        "price": price,
        "score": score,
        "sid": sid,
    }

# ============================================================
# COIN INFO (signal emas)
# ============================================================
async def get_coin_info(coin_id: str):
    all_data = await fetch_coin_all_data(coin_id)
    if not all_data:
        return "❌ Ma'lumot topilmadi. Keyinroq urinib ko'ring.", None

    market = all_data["market"]
    md = market.get("market_data", {})
    name = market.get("name", coin_id)
    symbol = market.get("symbol", "").upper()
    price = md.get("current_price", {}).get("usd", 0)
    change_24h = md.get("price_change_percentage_24h", 0) or 0
    change_7d = md.get("price_change_percentage_7d", 0) or 0
    ath = md.get("ath", {}).get("usd", 0)
    ath_change = md.get("ath_change_percentage", {}).get("usd", 0) or 0
    market_cap = md.get("market_cap", {}).get("usd", 0)
    vol_24h = md.get("total_volume", {}).get("usd", 0)
    supply = md.get("circulating_supply", 0)

    main_ohlc = all_data["ohlc_4h"] or all_data["ohlc_1d"]
    rsi = None
    tf_trends = {}
    if main_ohlc:
        closes = [c[4] for c in main_ohlc]
        rsi = calc_rsi(closes)
        tf_trends, overall = multi_tf_analysis(
            all_data["ohlc_1d"], all_data["ohlc_4h"], all_data["ohlc_1h"]
        )

    fund_score = fundamental_score(market)

    c24 = "🟢" if change_24h >= 0 else "🔴"
    c7 = "🟢" if change_7d >= 0 else "🔴"
    tf_text = ""
    for tf, t in tf_trends.items():
        e = "📈" if t == "bullish" else ("📉" if t == "bearish" else "↔️")
        tf_text += f"{e}{tf} "

    msg = f"""📊 *{name} ({symbol})*

💰 Narx: `${price:,.4f}`
{c24} 24h: `{change_24h:+.2f}%` | {c7} 7d: `{change_7d:+.2f}%`
📈 ATH: `${ath:,.2f}` ({ath_change:.1f}%)
💎 Market Cap: `${market_cap/1e9:.2f}B`
📊 Hajm: `${vol_24h/1e6:.1f}M`
🔄 Ta'minot: `{supply:,.0f}`

📐 Trend: {tf_text}
📊 RSI: `{rsi or 'N/A'}`
⭐ Fundamental: `{fund_score}/10`{DISCLAIMER}"""

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yangilash", callback_data=f"info_{coin_id}"),
        InlineKeyboardButton("🚨 Signal tekshir", callback_data=f"sig_{coin_id}"),
    ], [
        InlineKeyboardButton("🔙 Orqaga", callback_data="back_main"),
    ]])

    return msg, keyboard

# ============================================================
# KLAVIATURA
# ============================================================
def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("₿ BTC", callback_data="info_bitcoin"),
            InlineKeyboardButton("Ξ ETH", callback_data="info_ethereum"),
            InlineKeyboardButton("◎ SOL", callback_data="info_solana"),
        ],
        [
            InlineKeyboardButton("🔔 Signallar yoqish", callback_data="watch_on"),
            InlineKeyboardButton("🔕 O'chirish", callback_data="watch_off"),
        ],
        [
            InlineKeyboardButton("📊 Hisobot", callback_data="hisobot"),
            InlineKeyboardButton("🔍 Hozir skan", callback_data="scan_now"),
        ]
    ])

# ============================================================
# HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text(
        "👋 *Rustamov Crypto Botiga xush kelibsiz!*\n\n"
        "🔍 Top 200 token kuzatiladi\n"
        "⚡ Volume + SMC + Multi-TF tahlil\n"
        "📩 Faqat kuchli signal bo'lganda xabar\n\n"
        "👇 Tugmalardan foydalaning yoki coin nomini yozing:",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchi matn yozsa — coin tahlili"""
    text = update.message.text.strip().lower()

    # COIN_MAP dan qidirish
    coin_id = COIN_MAP.get(text)

    # Agar topilmasa — to'g'ridan CoinGecko ID sifatida sinash
    if not coin_id:
        coin_id = text

    msg_obj = await update.message.reply_text("⏳ Tahlil qilinmoqda...")
    result, keyboard = await get_coin_info(coin_id)
    await msg_obj.edit_text(result, parse_mode="Markdown", reply_markup=keyboard)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("info_"):
        coin_id = data[5:]
        await query.edit_message_text("⏳ Yuklanmoqda...", parse_mode="Markdown")
        msg, keyboard = await get_coin_info(coin_id)
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=keyboard)

    elif data.startswith("sig_"):
        coin_id = data[4:]
        await query.edit_message_text("⏳ Signal tekshirilmoqda...", parse_mode="Markdown")
        result = await analyze_coin(coin_id, force_signal=True)
        if result:
            await query.edit_message_text(
                result["signal_msg"], parse_mode="Markdown",
                reply_markup=result["keyboard"]
            )
        else:
            msg, keyboard = await get_coin_info(coin_id)
            await query.edit_message_text(
                "📭 Hozircha signal yo'q.\n\n" + msg,
                parse_mode="Markdown", reply_markup=keyboard
            )

    elif data.startswith("det_"):
        parts = data.split("_")
        coin_id = parts[1]
        await query.edit_message_text("⏳ Yuklanmoqda...", parse_mode="Markdown")
        result = await analyze_coin(coin_id, force_signal=True)
        if result:
            back_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Orqaga", callback_data=f"info_{coin_id}")
            ]])
            await query.edit_message_text(
                result["detail_msg"], parse_mode="Markdown", reply_markup=back_kb
            )

    elif data == "watch_on":
        CHAT_IDS.add(query.from_user.id)
        await query.edit_message_text(
            "✅ *Signallar yoqildi!*\n"
            "Top 200 token har soatda tekshiriladi.\n"
            "Faqat kuchli signal bo'lganda xabar keladi! 🚨\n\n"
            "👇 Tugmalardan foydalaning:",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    elif data == "watch_off":
        CHAT_IDS.discard(query.from_user.id)
        await query.edit_message_text(
            "🔕 *Signallar o'chirildi.*\n\n"
            "👇 Tugmalardan foydalaning:",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    elif data == "hisobot":
        total = STATS["total_signals"]
        if total == 0:
            text = "📊 *Hisobot*\n\nHali signal berilmagan."
        else:
            tp_t = STATS["tp1_hit"] + STATS["tp2_hit"] + STATS["tp3_hit"]
            sl = STATS["sl_hit"]
            ochiq = total - tp_t - sl
            wr = round(tp_t / (tp_t + sl) * 100, 1) if (tp_t + sl) > 0 else 0
            text = (f"📊 *Signal Hisoboti*\n\n"
                    f"Jami: `{total}` | ✅ TP: `{tp_t}` | ❌ SL: `{sl}` | 🔄 Ochiq: `{ochiq}`\n"
                    f"Win rate: `{wr}%`\n\n*So'nggi signallar:*\n")
            for s in reversed(STATS["signals_log"][-5:]):
                text += f"{s['status']} #{s['id']} *{s['coin']}* {s['direction']} | `${s['entry']:,.4f}` | {s['time']}\n"
        text += DISCLAIMER
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="back_main")]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif data == "scan_now":
        await query.edit_message_text("⏳ Top 50 token tekshirilmoqda... (1-2 daqiqa)")
        found = 0
        scan_list = WATCH_LIST[:50] if WATCH_LIST else list(COIN_MAP.values())
        for coin_id in scan_list:
            result = await analyze_coin(coin_id)
            if result:
                found += 1
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=result["signal_msg"],
                    parse_mode="Markdown",
                    reply_markup=result["keyboard"]
                )
            await asyncio.sleep(1.5)
        final_text = f"✅ Skan tugadi — {found} signal topildi!" if found > 0 else "📭 Signal topilmadi."
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=final_text, reply_markup=main_keyboard()
        )

    elif data == "back_main":
        await query.edit_message_text(
            "👋 *Rustamov Crypto Bot*\n\n👇 Tugmalardan foydalaning:",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    elif data.startswith("tp1_") or data.startswith("tp2_") or \
         data.startswith("tp3_") or data.startswith("sl_"):
        parts = data.split("_")
        status = parts[0].upper()
        sid = int(parts[1])
        _mark(sid, status)
        e = "✅" if "TP" in status else "❌"
        await query.answer(f"{e} #{sid} — {status} belgilandi!", show_alert=True)

def _mark(sid, status):
    for s in STATS["signals_log"]:
        if s["id"] == sid:
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
    logger.info("Top 200 yuklanmoqda...")
    WATCH_LIST = await fetch_top200()
    logger.info(f"{len(WATCH_LIST)} token yuklandi")

    while True:
        await asyncio.sleep(3600)
        if not CHAT_IDS:
            continue

        new_list = await fetch_top200()
        if new_list:
            WATCH_LIST = new_list
        CACHE.clear()

        logger.info(f"Skan — {len(WATCH_LIST)} token")
        found = 0

        for coin_id in WATCH_LIST:
            try:
                result = await analyze_coin(coin_id)
                if result:
                    found += 1
                    for chat_id in list(CHAT_IDS):
                        try:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=result["signal_msg"],
                                parse_mode="Markdown",
                                reply_markup=result["keyboard"]
                            )
                        except Exception as e:
                            logger.error(f"Yuborishda xato: {e}")
                await asyncio.sleep(1.2)
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
