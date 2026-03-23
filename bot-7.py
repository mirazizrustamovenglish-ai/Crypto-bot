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
CACHE = {}
ACTIVE_SIGNALS = {}

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
    "trx": "tron", "aave": "aave", "sand": "the-sandbox",
    "apt": "aptos", "sui": "sui", "arb": "arbitrum",
    "op": "optimism", "inj": "injective-protocol",
    "pepe": "pepe", "shib": "shiba-inu",
}

async def api_get(session, url, params=None, retries=2):
    for attempt in range(retries):
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    return await r.json()
                elif r.status == 429:
                    await asyncio.sleep(15)
                elif r.status == 404:
                    return None
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(3)
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
            await asyncio.sleep(2)
    return results or list(COIN_MAP.values())

async def fetch_coin_data(coin_id):
    now = datetime.now().timestamp()
    if coin_id in CACHE and now - CACHE[coin_id].get("ts", 0) < 300:
        return CACHE[coin_id]
    async with aiohttp.ClientSession() as s:
        tasks = [
            api_get(s, f"https://api.coingecko.com/api/v3/coins/{coin_id}", {
                "localization": "false", "tickers": "false",
                "market_data": "true", "community_data": "false", "developer_data": "true"
            }),
            api_get(s, f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
                    {"vs_currency": "usd", "days": "30"}),
            api_get(s, f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
                    {"vs_currency": "usd", "days": "7"}),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    market = results[0] if not isinstance(results[0], Exception) else None
    ohlc_1d = results[1] if not isinstance(results[1], Exception) else None
    ohlc_4h = results[2] if not isinstance(results[2], Exception) else None
    if not market:
        return None
    result = {"market": market, "ohlc_1d": ohlc_1d, "ohlc_4h": ohlc_4h, "ts": now}
    CACHE[coin_id] = result
    return result

async def get_current_price(coin_id):
    url = "https://api.coingecko.com/api/v3/simple/price"
    async with aiohttp.ClientSession() as s:
        data = await api_get(s, url, {"ids": coin_id, "vs_currencies": "usd"})
        if data and coin_id in data:
            return data[coin_id]["usd"]
    return None

async def get_news(symbol):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://cryptopanic.com/api/v1/posts/", params={
                "auth_token": "free", "currencies": symbol.upper(),
                "filter": "important", "public": "true"
            }, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    return [f"• {i.get('title','')[:70]} _({i.get('published_at','')[:10]})_"
                            for i in data.get("results", [])[:3]]
    except:
        pass
    return []

async def get_economic_calendar():
    events = []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.coinmarketcal.com/v1/events", params={
                "max": 5, "dateRangeStart": datetime.now().strftime("%Y-%m-%d"),
                "sortBy": "created_desc", "showOnly": "hot_events"
            }, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    for e in data.get("body", [])[:5]:
                        title = e.get("title", {}).get("en", "")[:60]
                        date = e.get("date_event", "")[:10]
                        coins = ", ".join([c.get("symbol", "") for c in e.get("coins", [])[:2]])
                        events.append(f"• {title} | {coins} | {date}")
    except:
        pass
    if not events:
        events = [
            "• FED Meeting — har 6 haftada",
            "• CPI Report — har oy",
            "• Bitcoin Halving — Apr 2028 (taxminiy)",
        ]
    return events

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
    if not closes or len(closes) < 10:
        return None
    ph, pl = max(closes[-10:-5]), min(closes[-10:-5])
    curr = closes[-1]
    if curr > ph:
        return "bullish"
    elif curr < pl:
        return "bearish"
    return None

def detect_fvg(ohlc):
    if not ohlc or len(ohlc) < 3:
        return None
    for i in range(len(ohlc)-3, 0, -1):
        c1h, c1l = ohlc[i][2], ohlc[i][3]
        c3h, c3l = ohlc[i+2][2], ohlc[i+2][3]
        if c3l > c1h:
            return "Bullish"
        elif c3h < c1l:
            return "Bearish"
    return None

def detect_ob(ohlc, trend):
    if not ohlc or len(ohlc) < 5:
        return None, None
    if trend == "bullish":
        for i in range(len(ohlc)-3, 0, -1):
            if ohlc[i][4] < ohlc[i][1]:
                ob_h, ob_l = ohlc[i][2], ohlc[i][3]
                if ob_l <= ohlc[-1][4] <= ob_h * 1.01:
                    return ob_l, ob_h
    else:
        for i in range(len(ohlc)-3, 0, -1):
            if ohlc[i][4] > ohlc[i][1]:
                ob_h, ob_l = ohlc[i][2], ohlc[i][3]
                if ob_l * 0.99 <= ohlc[-1][4] <= ob_h:
                    return ob_l, ob_h
    return None, None

def check_volume_spike(vol_24h, change_24h):
    avg_vol = vol_24h / (abs(change_24h) / 5 + 1) if change_24h else vol_24h * 0.6
    if avg_vol <= 0:
        return False, 0
    ratio = vol_24h / avg_vol
    return ratio >= 2.0, round(ratio, 1)

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
    if circ / total > 0.7: score += 2
    elif circ / total > 0.4: score += 1
    ath_chg = md.get("ath_change_percentage", {}).get("usd", 0) or 0
    if ath_chg < -70: score += 2
    elif ath_chg < -30: score += 1
    commits = data.get("developer_data", {}).get("commit_count_4_weeks", 0) or 0
    if commits > 10: score += 1
    return round((score / 10) * 10, 1)

def multi_tf(ohlc_1d, ohlc_4h):
    results = {}
    for tf, ohlc in [("1D", ohlc_1d), ("4H", ohlc_4h)]:
        results[tf] = detect_trend([c[4] for c in ohlc]) if ohlc else "sideways"
    bull = sum(1 for t in results.values() if t == "bullish")
    bear = sum(1 for t in results.values() if t == "bearish")
    overall = "bullish" if bull >= 2 else "bearish" if bear >= 2 else \
              "bullish" if bull > bear else "bearish" if bear > bull else "sideways"
    return results, overall

def get_sl_tp(ohlc, price, is_long):
    s_highs = find_swing_highs(ohlc)
    s_lows = find_swing_lows(ohlc)
    atr = calc_atr(ohlc)
    a = atr or price * 0.02
    if is_long:
        lows_below = [l for l in s_lows if l < price * 0.999]
        sl = max(lows_below) if lows_below else round(price - a * 2, 8)
        tps = sorted([h for h in s_highs if h > price * 1.005])[:3]
        if not tps:
            tps = [round(price+a*1.5,8), round(price+a*3,8), round(price+a*5,8)]
    else:
        highs_above = [h for h in s_highs if h > price * 1.001]
        sl = min(highs_above) if highs_above else round(price + a * 2, 8)
        tps = sorted([l for l in s_lows if l < price * 0.995], reverse=True)[:3]
        if not tps:
            tps = [round(price-a*1.5,8), round(price-a*3,8), round(price-a*5,8)]
    return sl, tps

async def analyze_coin(coin_id, force=False):
    all_data = await fetch_coin_data(coin_id)
    if not all_data:
        return None
    market = all_data["market"]
    ohlc_1d = all_data["ohlc_1d"]
    ohlc_4h = all_data["ohlc_4h"]
    main_ohlc = ohlc_4h or ohlc_1d
    if not main_ohlc:
        return None
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

    spike, ratio = check_volume_spike(vol_24h, change_24h)
    tf_trends, overall = multi_tf(ohlc_1d, ohlc_4h)
    closes = [c[4] for c in main_ohlc]
    rsi = calc_rsi(closes)
    bos = detect_bos(closes)
    fvg = detect_fvg(main_ohlc)
    ob_low, ob_high = detect_ob(main_ohlc, overall)
    fund_score = fundamental_score(market)

    s_highs = find_swing_highs(main_ohlc)
    s_lows = find_swing_lows(main_ohlc)
    near_sup = any(price <= l * 1.025 for l in s_lows if l < price)
    near_res = any(price >= h * 0.975 for h in s_highs if h > price)

    if overall == "bullish":
        is_long = True
    elif overall == "bearish":
        is_long = False
    else:
        if not force:
            return None
        is_long = change_24h >= 0

    score = 0
    factors = []
    if spike: score += 3; factors.append(f"⚡{ratio}x Vol")
    if near_sup and is_long: score += 2; factors.append("🛡Support")
    if near_res and not is_long: score += 2; factors.append("🛡Resistance")
    if bos == ("bullish" if is_long else "bearish"): score += 2; factors.append("📐BOS")
    if ob_low and ob_high: score += 2; factors.append("📦OB")
    if rsi and ((is_long and rsi < 35) or (not is_long and rsi > 65)):
        score += 2; factors.append(f"📊RSI {rsi}")
    if fvg: score += 1; factors.append("🔲FVG")

    if not force and (score < 4 or not spike):
        return None

    strength = "🔴 KUCHLI" if score >= 7 else "🟡 ORTA" if score >= 5 else "🟢 ZAIF"
    direction = "LONG 📈" if is_long else "SHORT 📉"
    sl, tps = get_sl_tp(main_ohlc, price, is_long)
    risk = abs(price - sl)
    rr = round(abs(tps[0] - price) / risk, 1) if risk > 0 and tps else 0

    tf_text = ""
    for tf, t in tf_trends.items():
        e = "📈" if t == "bullish" else "📉" if t == "bearish" else "⬌"
        tf_text += f"{e}{tf} "

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
        "is_long": is_long,
    })

    ACTIVE_SIGNALS[sid] = {
        "coin_id": coin_id, "symbol": symbol, "entry": price,
        "sl": sl, "is_long": is_long,
        "tp1": tps[0] if len(tps) > 0 else None,
        "tp2": tps[1] if len(tps) > 1 else None,
        "tp3": tps[2] if len(tps) > 2 else None,
        "tp1_hit": False, "tp2_hit": False, "tp3_hit": False, "sl_hit": False,
        "chat_ids": set(CHAT_IDS), "active": True,
    }

    sl_pct = (sl - price) / price * 100
    tp_text = ""
    for i, tp in enumerate(tps[:3], 1):
        pct = (tp - price) / price * 100
        tp_text += f"TP{i}: `${tp:,.4f}` ({pct:+.1f}%)\n"

    detail_msg = (
        f"📊 *{name} ({symbol}) — Batafsil*\n\n"
        f"💰 Narx: `${price:,.4f}`\n"
        f"24h: `{change_24h:+.2f}%` | 7d: `{change_7d:+.2f}%`\n"
        f"ATH: `${ath:,.2f}` ({ath_change:.1f}%)\n"
        f"Market Cap: `${market_cap/1e9:.2f}B`\n\n"
        f"📈 Multi-TF: {tf_text}\n"
        f"📊 RSI: `{rsi or 'N/A'}`\n"
        f"📐 BOS: `{bos or 'Yoq'}`\n"
        f"🔲 FVG: `{fvg or 'Yoq'}`\n"
        f"📦 OB: `{'Ha' if ob_low else 'Yoq'}`\n"
        f"⭐ Fundamental: `{fund_score}/10`\n"
        f"🎯 Signal kuchi: `{score}/12`"
        f"{DISCLAIMER}"
    )

    signal_msg = (
        f"🚨 *#{sid} — {name} ({symbol}) — {strength}*\n\n"
        f"{direction} | R/R: `1:{rr}`\n"
        f"{tf_text}\n"
        f"{' | '.join(factors)}\n\n"
        f"💰 `${price:,.4f}`\n"
        f"❌ SL: `${sl:,.4f}` ({sl_pct:+.1f}%)\n"
        f"{tp_text}"
        f"🤖 _Bot avtomatik kuzatib boradi_"
        f"{DISCLAIMER}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Batafsil", callback_data=f"det_{coin_id}_{sid}"),
        InlineKeyboardButton("📰 Yangiliklar", callback_data=f"news_{symbol}"),
    ]])

    return {
        "signal_msg": signal_msg, "detail_msg": detail_msg,
        "keyboard": keyboard, "coin_id": coin_id,
        "symbol": symbol, "name": name, "sid": sid,
        "price": price, "change_24h": change_24h,
        "change_7d": change_7d, "ath": ath, "ath_change": ath_change,
        "market_cap": market_cap, "rsi": rsi,
        "fund_score": fund_score, "tf_text": tf_text,
    }

async def get_coin_info(coin_id):
    all_data = await fetch_coin_data(coin_id)
    if not all_data:
        return None, None
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
    fund_score = fundamental_score(market)

    main_ohlc = all_data["ohlc_4h"] or all_data["ohlc_1d"]
    rsi = None
    tf_text = ""
    if main_ohlc:
        rsi = calc_rsi([c[4] for c in main_ohlc])
        tf_trends, _ = multi_tf(all_data["ohlc_1d"], all_data["ohlc_4h"])
        for tf, t in tf_trends.items():
            e = "📈" if t == "bullish" else "📉" if t == "bearish" else "⬌"
            tf_text += f"{e}{tf} "

    c24 = "🟢" if change_24h >= 0 else "🔴"
    c7 = "🟢" if change_7d >= 0 else "🔴"

    msg = (
        f"📊 *{name} ({symbol})*\n\n"
        f"💰 Narx: `${price:,.4f}`\n"
        f"{c24} 24h: `{change_24h:+.2f}%` | {c7} 7d: `{change_7d:+.2f}%`\n"
        f"📈 ATH: `${ath:,.2f}` ({ath_change:.1f}%)\n"
        f"💎 Market Cap: `${market_cap/1e9:.2f}B`\n"
        f"📊 Hajm: `${vol_24h/1e6:.1f}M`\n"
        f"🔄 Muomala: `{supply:,.0f}`\n\n"
        f"📐 Trend: {tf_text}\n"
        f"📊 RSI: `{rsi or 'N/A'}`\n"
        f"⭐ Fundamental: `{fund_score}/10`"
        f"{DISCLAIMER}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yangilash", callback_data=f"info_{coin_id}"),
        InlineKeyboardButton("🚨 Signal", callback_data=f"sig_{coin_id}"),
    ], [
        InlineKeyboardButton("📰 Yangiliklar", callback_data=f"news_{symbol}"),
        InlineKeyboardButton("🔙 Orqaga", callback_data="back_main"),
    ]])

    return msg, keyboard

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("₿ BTC", callback_data="info_bitcoin"),
            InlineKeyboardButton("Ξ ETH", callback_data="info_ethereum"),
            InlineKeyboardButton("◎ SOL", callback_data="info_solana"),
        ],
        [
            InlineKeyboardButton("🔔 Signallar yoqish", callback_data="watch_on"),
            InlineKeyboardButton("🔕 Ochirish", callback_data="watch_off"),
        ],
        [
            InlineKeyboardButton("📊 Hisobot", callback_data="hisobot"),
            InlineKeyboardButton("🔍 Hozir skan", callback_data="scan_now"),
        ],
        [
            InlineKeyboardButton("📅 Economic Calendar", callback_data="calendar"),
        ]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text(
        "👋 *Rustamov Crypto Botiga xush kelibsiz!*\n\n"
        "🔍 Top 200 token kuzatiladi\n"
        "⚡ Volume + SMC + Multi-TF + RSI\n"
        "🤖 TP/SL avtomatik kuzatiladi\n"
        "📅 Economic Calendar\n"
        "📰 Kripto yangiliklari\n\n"
        "👇 Tugma bosing yoki coin nomi yozing:\n"
        "`btc` `eth` `sol` `bnb` `doge` ...",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    coin_id = COIN_MAP.get(text, text)
    msg_obj = await update.message.reply_text("⏳ Tahlil qilinmoqda...")
    try:
        msg, keyboard = await get_coin_info(coin_id)
        if msg:
            await msg_obj.edit_text(msg, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg_obj.edit_text(
                f"❌ *'{text}'* topilmadi.\n\nMisol: `btc` `eth` `sol` `bnb`",
                parse_mode="Markdown", reply_markup=main_keyboard()
            )
    except Exception as e:
        logger.error(f"text_handler: {e}")
        await msg_obj.edit_text("❌ Xato. Keyinroq urinib koring.", reply_markup=main_keyboard())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("info_"):
        coin_id = data[5:]
        await query.edit_message_text("⏳ Yuklanmoqda...", parse_mode="Markdown")
        msg, keyboard = await get_coin_info(coin_id)
        if msg:
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await query.edit_message_text("❌ Malumot topilmadi.", reply_markup=main_keyboard())

    elif data.startswith("sig_"):
        coin_id = data[4:]
        await query.edit_message_text("⏳ Signal tekshirilmoqda...", parse_mode="Markdown")
        result = await analyze_coin(coin_id, force=True)
        if result:
            await query.edit_message_text(result["signal_msg"], parse_mode="Markdown", reply_markup=result["keyboard"])
        else:
            msg, keyboard = await get_coin_info(coin_id)
            if msg:
                await query.edit_message_text("📭 Hozircha signal yoq.\n\n" + msg, parse_mode="Markdown", reply_markup=keyboard)

    elif data.startswith("det_"):
        parts = data.split("_")
        coin_id = parts[1]
        await query.edit_message_text("⏳ Yuklanmoqda...", parse_mode="Markdown")
        result = await analyze_coin(coin_id, force=True)
        if result:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data=f"info_{coin_id}")]])
            await query.edit_message_text(result["detail_msg"], parse_mode="Markdown", reply_markup=kb)

    elif data.startswith("news_"):
        symbol = data[5:]
        await query.edit_message_text("⏳ Yangiliklar yuklanmoqda...", parse_mode="Markdown")
        news = await get_news(symbol)
        msg = f"📰 *{symbol} Yangiliklari*\n\n" + ("\n".join(news) if news else "Yangilik topilmadi.")
        msg += DISCLAIMER
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="back_main")]])
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)

    elif data == "calendar":
        await query.edit_message_text("⏳ Yuklanmoqda...", parse_mode="Markdown")
        events = await get_economic_calendar()
        msg = "📅 *Economic Calendar*\n\n" + "\n".join(events) + "\n\n_Manba: CoinMarketCal_" + DISCLAIMER
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Yangilash", callback_data="calendar"),
            InlineKeyboardButton("🔙 Orqaga", callback_data="back_main"),
        ]])
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)

    elif data == "watch_on":
        CHAT_IDS.add(query.from_user.id)
        await query.edit_message_text(
            "✅ *Signallar yoqildi!*\nTP/SL avtomatik kuzatiladi! 🤖",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    elif data == "watch_off":
        CHAT_IDS.discard(query.from_user.id)
        await query.edit_message_text("🔕 *Signallar ochirildi.*", parse_mode="Markdown", reply_markup=main_keyboard())

    elif data == "hisobot":
        total = STATS["total_signals"]
        if total == 0:
            text = "📊 *Hisobot*\n\nHali signal berilmagan."
        else:
            tp_t = STATS["tp1_hit"] + STATS["tp2_hit"] + STATS["tp3_hit"]
            sl_c = STATS["sl_hit"]
            ochiq = total - tp_t - sl_c
            wr = round(tp_t / (tp_t + sl_c) * 100, 1) if (tp_t + sl_c) > 0 else 0
            text = (f"📊 *Signal Hisoboti*\n\n"
                    f"Jami: `{total}` | ✅TP: `{tp_t}` | ❌SL: `{sl_c}` | 🔄: `{ochiq}`\n"
                    f"Win rate: `{wr}%`\n\n*Songi signallar:*\n")
            for s in reversed(STATS["signals_log"][-5:]):
                text += f"{s['status']} #{s['id']} *{s['coin']}* {s['direction']} `${s['entry']:,.4f}` {s['time']}\n"
        text += DISCLAIMER
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="back_main")]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif data == "scan_now":
        await query.edit_message_text("⏳ Top 50 token tekshirilmoqda...")
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
            await asyncio.sleep(1.2)
        txt = f"✅ {found} signal topildi!" if found else "📭 Signal topilmadi."
        await context.bot.send_message(chat_id=query.message.chat_id, text=txt, reply_markup=main_keyboard())

    elif data == "back_main":
        await query.edit_message_text(
            "👋 *Rustamov Crypto Bot*\n\n👇 Tugmalardan foydalaning:",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

async def track_signals(bot):
    """Har 5 daqiqada TP/SL avtomatik tekshirish"""
    while True:
        await asyncio.sleep(300)
        if not ACTIVE_SIGNALS:
            continue

        closed = []
        for sid, sig in list(ACTIVE_SIGNALS.items()):
            if not sig["active"]:
                closed.append(sid)
                continue
            try:
                price = await get_current_price(sig["coin_id"])
                if not price:
                    continue
                is_long = sig["is_long"]
                symbol = sig["symbol"]
                msg = None

                if not sig["tp1_hit"] and sig["tp1"]:
                    if (is_long and price >= sig["tp1"]) or (not is_long and price <= sig["tp1"]):
                        sig["tp1_hit"] = True
                        STATS["tp1_hit"] += 1
                        _update_status(sid, "TP1")
                        msg = f"✅ *#{sid} {symbol} — TP1 URDI!*\n💰 `${price:,.4f}` | TP1: `${sig['tp1']:,.4f}`{DISCLAIMER}"

                elif sig["tp1_hit"] and not sig["tp2_hit"] and sig["tp2"]:
                    if (is_long and price >= sig["tp2"]) or (not is_long and price <= sig["tp2"]):
                        sig["tp2_hit"] = True
                        STATS["tp2_hit"] += 1
                        _update_status(sid, "TP2")
                        msg = f"✅✅ *#{sid} {symbol} — TP2 URDI!*\n💰 `${price:,.4f}` | TP2: `${sig['tp2']:,.4f}`{DISCLAIMER}"

                elif sig["tp2_hit"] and not sig["tp3_hit"] and sig["tp3"]:
                    if (is_long and price >= sig["tp3"]) or (not is_long and price <= sig["tp3"]):
                        sig["tp3_hit"] = True
                        sig["active"] = False
                        STATS["tp3_hit"] += 1
                        _update_status(sid, "TP3")
                        msg = f"✅✅✅ *#{sid} {symbol} — TP3 URDI! MUKAMMAL!*\n💰 `${price:,.4f}` | TP3: `${sig['tp3']:,.4f}`{DISCLAIMER}"

                if not sig["sl_hit"] and not sig.get("tp3_hit"):
                    if (is_long and price <= sig["sl"]) or (not is_long and price >= sig["sl"]):
                        sig["sl_hit"] = True
                        sig["active"] = False
                        STATS["sl_hit"] += 1
                        _update_status(sid, "SL")
                        msg = f"❌ *#{sid} {symbol} — SL URDI*\n💰 `${price:,.4f}` | SL: `${sig['sl']:,.4f}`{DISCLAIMER}"

                if msg:
                    for chat_id in sig["chat_ids"]:
                        try:
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                        except Exception as e:
                            logger.error(f"Track xabar: {e}")
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Track {sid}: {e}")

        for sid in closed:
            ACTIVE_SIGNALS.pop(sid, None)

def _update_status(sid, status):
    for s in STATS["signals_log"]:
        if s["id"] == sid:
            s["status"] = f"{'✅' if 'TP' in status else '❌'} {status}"

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
                                chat_id=chat_id, text=result["signal_msg"],
                                parse_mode="Markdown", reply_markup=result["keyboard"]
                            )
                        except Exception as e:
                            logger.error(f"Yuborishda xato: {e}")
                await asyncio.sleep(1.2)
            except Exception as e:
                logger.error(f"{coin_id}: {e}")
        logger.info(f"Skan tugadi — {found} signal")

async def post_init(app):
    asyncio.create_task(hourly_scan(app.bot))
    asyncio.create_task(track_signals(app.bot))
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
