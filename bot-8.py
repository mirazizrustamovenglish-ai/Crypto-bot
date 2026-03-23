import asyncio
import aiohttp
import logging
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

BOT_TOKEN = os.environ.get("BOT_TOKEN")
COINMARKETCAL_KEY = os.environ.get("COINMARKETCAL_KEY", "")

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

# Binance symbol mapping
COIN_MAP = {
    "btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
    "bnb": "BNBUSDT", "xrp": "XRPUSDT", "ada": "ADAUSDT",
    "doge": "DOGEUSDT", "avax": "AVAXUSDT", "dot": "DOTUSDT",
    "matic": "MATICUSDT", "link": "LINKUSDT", "uni": "UNIUSDT",
    "ltc": "LTCUSDT", "atom": "ATOMUSDT", "near": "NEARUSDT",
    "ftm": "FTMUSDT", "algo": "ALGOUSDT", "xlm": "XLMUSDT",
    "trx": "TRXUSDT", "aave": "AAVEUSDT", "sand": "SANDUSDT",
    "apt": "APTUSDT", "sui": "SUIUSDT", "arb": "ARBUSDT",
    "op": "OPUSDT", "inj": "INJUSDT", "pepe": "PEPEUSDT",
    "shib": "SHIBUSDT", "floki": "FLOKIUSDT", "wld": "WLDUSDT",
    "sei": "SEIUSDT", "tia": "TIAUSDT", "jup": "JUPUSDT",
    "w": "WUSDT", "ena": "ENAUSDT", "io": "IOUSDT",
}

# CoinGecko ID mapping (fundamental uchun)
COINGECKO_MAP = {
    "BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana",
    "BNBUSDT": "binancecoin", "XRPUSDT": "ripple", "ADAUSDT": "cardano",
    "DOGEUSDT": "dogecoin", "AVAXUSDT": "avalanche-2", "DOTUSDT": "polkadot",
    "MATICUSDT": "matic-network", "LINKUSDT": "chainlink", "UNIUSDT": "uniswap",
}

# ============================================================
# BINANCE API
# ============================================================
BINANCE_BASE = "https://api.binance.com/api/v3"

async def binance_get(session, endpoint, params=None, retries=3):
    url = f"{BINANCE_BASE}/{endpoint}"
    for attempt in range(retries):
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
                elif r.status == 429:
                    await asyncio.sleep(10)
        except Exception as e:
            if attempt < retries - 1:
                await asyncio.sleep(2)
            logger.error(f"Binance API {endpoint}: {e}")
    return None

async def get_top200_symbols():
    """Binance dan top 200 USDT juftliklari"""
    async with aiohttp.ClientSession() as s:
        data = await binance_get(s, "ticker/24hr")
        if not data:
            return list(COIN_MAP.values())
        usdt_pairs = [d for d in data if d["symbol"].endswith("USDT")]
        usdt_pairs.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
        return [p["symbol"] for p in usdt_pairs[:200]]

async def get_ticker(symbol: str):
    """24 soatlik statistika"""
    async with aiohttp.ClientSession() as s:
        return await binance_get(s, "ticker/24hr", {"symbol": symbol})

async def get_ohlc(symbol: str, interval="4h", limit=100):
    """OHLC ma'lumotlari"""
    async with aiohttp.ClientSession() as s:
        data = await binance_get(s, "klines", {
            "symbol": symbol, "interval": interval, "limit": limit
        })
        if data:
            # [time, open, high, low, close, volume, ...]
            return [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
                    for c in data]
    return None

async def get_current_price(symbol: str):
    """Hozirgi narx"""
    async with aiohttp.ClientSession() as s:
        data = await binance_get(s, "ticker/price", {"symbol": symbol})
        if data:
            return float(data["price"])
    return None

async def fetch_coin_data(symbol: str):
    """Parallel ravishda barcha ma'lumotlarni olish"""
    now = datetime.now().timestamp()
    if symbol in CACHE and now - CACHE[symbol].get("ts", 0) < 300:
        return CACHE[symbol]

    async with aiohttp.ClientSession() as s:
        tasks = [
            binance_get(s, "ticker/24hr", {"symbol": symbol}),
            # 4H OHLC — 100 ta sham
            binance_get(s, "klines", {"symbol": symbol, "interval": "4h", "limit": 100}),
            # 1D OHLC — 50 ta sham
            binance_get(s, "klines", {"symbol": symbol, "interval": "1d", "limit": 50}),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    ticker = results[0] if not isinstance(results[0], Exception) else None
    klines_4h = results[1] if not isinstance(results[1], Exception) else None
    klines_1d = results[2] if not isinstance(results[2], Exception) else None

    if not ticker:
        return None

    def parse_klines(klines):
        if not klines:
            return None
        return [[float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
                for c in klines]

    result = {
        "ticker": ticker,
        "ohlc_4h": parse_klines(klines_4h),
        "ohlc_1d": parse_klines(klines_1d),
        "ts": now
    }
    CACHE[symbol] = result
    return result

# ============================================================
# YANGILIKLAR
# ============================================================
async def get_news(symbol: str):
    """CryptoPanic yangiliklari"""
    # Symbol dan coin nomini olish (BTCUSDT -> BTC)
    coin = symbol.replace("USDT", "").replace("BUSD", "")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://cryptopanic.com/api/v1/posts/", params={
                "auth_token": "free", "currencies": coin,
                "filter": "important", "public": "true"
            }, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    return [f"• {i.get('title','')[:70]} _({i.get('published_at','')[:10]})_"
                            for i in data.get("results", [])[:3]]
    except:
        pass
    return []

# ============================================================
# ECONOMIC CALENDAR
# ============================================================
async def get_economic_calendar():
    """CoinMarketCal dan real voqealar"""
    events = []

    if COINMARKETCAL_KEY:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.coinmarketcal.com/v1/events", params={
                    "x-api-key": COINMARKETCAL_KEY,
                    "max": 10,
                    "dateRangeStart": datetime.now().strftime("%Y-%m-%d"),
                    "sortBy": "hot_events",
                    "showOnly": "hot_events"
                }, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        for e in data.get("body", [])[:7]:
                            title = e.get("title", {}).get("en", "")[:55]
                            date = e.get("date_event", "")[:10]
                            coins = ", ".join([c.get("symbol","") for c in e.get("coins",[])[:2]])
                            percent = e.get("percent", 0)
                            hot = "🔥" if percent > 70 else "📅"
                            events.append(f"{hot} {title} | {coins} | {date}")
        except Exception as e:
            logger.error(f"CoinMarketCal: {e}")

    # Fallback — 2026 real sanalar
    if not events:
        today = datetime.now().strftime("%Y-%m-%d")

        fed_dates = [
            "2026-03-18", "2026-05-06", "2026-06-17",
            "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16"
        ]
        cpi_dates = [
            "2026-03-11", "2026-04-10", "2026-05-13",
            "2026-06-10", "2026-07-14", "2026-08-12",
            "2026-09-11", "2026-10-14", "2026-11-12", "2026-12-11"
        ]
        pce_dates = [
            "2026-03-27", "2026-04-30", "2026-05-29",
            "2026-06-26", "2026-07-31", "2026-08-28"
        ]

        upcoming_fed = [d for d in fed_dates if d >= today][:2]
        upcoming_cpi = [d for d in cpi_dates if d >= today][:2]
        upcoming_pce = [d for d in pce_dates if d >= today][:1]

        for d in upcoming_fed:
            days_left = (datetime.strptime(d, "%Y-%m-%d") - datetime.now()).days
            events.append(f"🏛 FED Meeting | {d} ({days_left} kun qoldi)")

        for d in upcoming_cpi:
            days_left = (datetime.strptime(d, "%Y-%m-%d") - datetime.now()).days
            events.append(f"📊 CPI Report | {d} ({days_left} kun qoldi)")

        for d in upcoming_pce:
            days_left = (datetime.strptime(d, "%Y-%m-%d") - datetime.now()).days
            events.append(f"📈 PCE Report | {d} ({days_left} kun qoldi)")

        events.append(f"₿ Bitcoin Halving | 2028-04 (taxminiy)")

    return events

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
        h, l, pc = ohlc[i][2], ohlc[i][3], ohlc[i-1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period if trs else None

def find_swing_highs(ohlc, lookback=5):
    if not ohlc or len(ohlc) < lookback * 2 + 1:
        return []
    highs = []
    for i in range(lookback, len(ohlc) - lookback):
        ch = ohlc[i][2]
        if ch > max(ohlc[j][2] for j in range(i-lookback, i)) and \
           ch > max(ohlc[j][2] for j in range(i+1, i+lookback+1)):
            highs.append(round(ch, 8))
    return sorted(set(highs))

def find_swing_lows(ohlc, lookback=5):
    if not ohlc or len(ohlc) < lookback * 2 + 1:
        return []
    lows = []
    for i in range(lookback, len(ohlc) - lookback):
        cl = ohlc[i][3]
        if cl < min(ohlc[j][3] for j in range(i-lookback, i)) and \
           cl < min(ohlc[j][3] for j in range(i+1, i+lookback+1)):
            lows.append(round(cl, 8))
    return sorted(set(lows))

def calc_ema(prices, period):
    if not prices or len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 8)

def detect_trend(ohlc):
    if not ohlc or len(ohlc) < 50:
        return "sideways"
    closes = [c[4] for c in ohlc]
    ema50 = calc_ema(closes, 50)
    ema200 = calc_ema(closes, min(200, len(closes)))
    price = closes[-1]
    if not ema50 or not ema200:
        return "sideways"
    if price > ema50 and ema50 > ema200:
        return "bullish"
    elif price < ema50 and ema50 < ema200:
        return "bearish"
    return "sideways"

def detect_bos(ohlc):
    if not ohlc or len(ohlc) < 20:
        return None
    closes = [c[4] for c in ohlc]
    ph = max(closes[-20:-10])
    pl = min(closes[-20:-10])
    curr = closes[-1]
    if curr > ph * 1.001:
        return "bullish"
    elif curr < pl * 0.999:
        return "bearish"
    return None

def detect_fvg(ohlc):
    if not ohlc or len(ohlc) < 3:
        return None
    for i in range(len(ohlc)-3, max(0, len(ohlc)-10), -1):
        c1h, c1l = ohlc[i][2], ohlc[i][3]
        c3h, c3l = ohlc[i+2][2], ohlc[i+2][3]
        if c3l > c1h:
            return "Bullish"
        elif c3h < c1l:
            return "Bearish"
    return None

def detect_ob(ohlc, trend):
    if not ohlc or len(ohlc) < 5:
        return False
    price = ohlc[-1][4]
    if trend == "bullish":
        for i in range(len(ohlc)-4, max(0, len(ohlc)-15), -1):
            if ohlc[i][4] < ohlc[i][1]:
                ob_h, ob_l = ohlc[i][2], ohlc[i][3]
                if ob_l <= price <= ob_h * 1.02:
                    return True
    else:
        for i in range(len(ohlc)-4, max(0, len(ohlc)-15), -1):
            if ohlc[i][4] > ohlc[i][1]:
                ob_h, ob_l = ohlc[i][2], ohlc[i][3]
                if ob_l * 0.98 <= price <= ob_h:
                    return True
    return False

def check_volume_spike(vol_24h, avg_vol):
    if not vol_24h or not avg_vol or avg_vol <= 0:
        return False, 0
    ratio = float(vol_24h) / float(avg_vol)
    return ratio >= 2.0, round(ratio, 1)

def get_sl_tp(ohlc, price, is_long):
    s_highs = find_swing_highs(ohlc)
    s_lows = find_swing_lows(ohlc)
    atr = calc_atr(ohlc)
    a = atr or price * 0.02

    if is_long:
        lows_below = [l for l in s_lows if l < price * 0.998]
        sl = max(lows_below) * 0.998 if lows_below else round(price - a * 2, 8)
        tps = sorted([h for h in s_highs if h > price * 1.005])[:3]
        if not tps:
            tps = [round(price+a*1.5,8), round(price+a*3,8), round(price+a*5,8)]
    else:
        highs_above = [h for h in s_highs if h > price * 1.002]
        sl = min(highs_above) * 1.002 if highs_above else round(price + a * 2, 8)
        tps = sorted([l for l in s_lows if l < price * 0.995], reverse=True)[:3]
        if not tps:
            tps = [round(price-a*1.5,8), round(price-a*3,8), round(price-a*5,8)]
    return round(sl, 8), [round(t, 8) for t in tps]

def multi_tf(ohlc_4h, ohlc_1d):
    t4h = detect_trend(ohlc_4h) if ohlc_4h else "sideways"
    t1d = detect_trend(ohlc_1d) if ohlc_1d else "sideways"
    bull = sum(1 for t in [t4h, t1d] if t == "bullish")
    bear = sum(1 for t in [t4h, t1d] if t == "bearish")
    overall = "bullish" if bull >= 2 else "bearish" if bear >= 2 else \
              "bullish" if bull > bear else "bearish" if bear > bull else "sideways"
    return {"4H": t4h, "1D": t1d}, overall

# ============================================================
# ASOSIY TAHLIL
# ============================================================
async def analyze_coin(symbol: str, force=False):
    all_data = await fetch_coin_data(symbol)
    if not all_data:
        return None

    ticker = all_data["ticker"]
    ohlc_4h = all_data["ohlc_4h"]
    ohlc_1d = all_data["ohlc_1d"]
    main_ohlc = ohlc_4h or ohlc_1d
    if not main_ohlc or len(main_ohlc) < 20:
        return None

    coin = symbol.replace("USDT", "")
    price = float(ticker.get("lastPrice", 0))
    vol_24h = float(ticker.get("quoteVolume", 0))
    avg_vol = float(ticker.get("quoteVolume", 0)) * 0.6
    change_24h = float(ticker.get("priceChangePercent", 0))
    high_24h = float(ticker.get("highPrice", 0))
    low_24h = float(ticker.get("lowPrice", 0))

    if price == 0:
        return None

    # Tahlil
    spike, ratio = check_volume_spike(vol_24h, avg_vol)
    tf_trends, overall = multi_tf(ohlc_4h, ohlc_1d)
    closes = [c[4] for c in main_ohlc]
    rsi = calc_rsi(closes)
    bos = detect_bos(main_ohlc)
    fvg = detect_fvg(main_ohlc)
    ob = detect_ob(main_ohlc, overall)

    s_highs = find_swing_highs(main_ohlc)
    s_lows = find_swing_lows(main_ohlc)
    near_sup = any(price <= l * 1.02 and price >= l * 0.98 for l in s_lows if l < price)
    near_res = any(price >= h * 0.98 and price <= h * 1.02 for h in s_highs if h > price)

    if overall == "bullish":
        is_long = True
    elif overall == "bearish":
        is_long = False
    else:
        if not force:
            return None
        is_long = change_24h >= 0

    # Scoring
    score = 0
    factors = []

    if spike:
        score += 3; factors.append(f"⚡{ratio}x Vol")
    if near_sup and is_long:
        score += 2; factors.append("🛡Sup")
    if near_res and not is_long:
        score += 2; factors.append("🛡Res")
    if bos == ("bullish" if is_long else "bearish"):
        score += 2; factors.append("📐BOS")
    if ob:
        score += 2; factors.append("📦OB")
    if rsi and ((is_long and rsi < 35) or (not is_long and rsi > 65)):
        score += 2; factors.append(f"📊RSI{rsi}")
    if fvg:
        score += 1; factors.append("🔲FVG")

    if not force and (score < 5 or not spike):
        return None

    strength = "🔴 KUCHLI" if score >= 8 else "🟡 ORTA" if score >= 5 else "🟢 ZAIF"
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
        "id": sid, "coin": coin, "symbol": symbol,
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
        "symbol": symbol, "coin": coin, "entry": price,
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

    signal_msg = (
        f"🚨 *#{sid} — {coin}/USDT — {strength}*\n\n"
        f"{direction} | R/R: `1:{rr}`\n"
        f"{tf_text}\n"
        f"{' | '.join(factors)}\n\n"
        f"💰 `${price:,.4f}`\n"
        f"24h: `{change_24h:+.2f}%`\n"
        f"❌ SL: `${sl:,.4f}` ({sl_pct:+.1f}%)\n"
        f"{tp_text}"
        f"🤖 _Bot avtomatik kuzatib boradi_"
        f"{DISCLAIMER}"
    )

    detail_msg = (
        f"📊 *{coin}/USDT — Batafsil*\n\n"
        f"💰 Narx: `${price:,.4f}`\n"
        f"24h: `{change_24h:+.2f}%`\n"
        f"24h High: `${high_24h:,.4f}`\n"
        f"24h Low: `${low_24h:,.4f}`\n"
        f"Hajm: `${vol_24h/1e6:.1f}M`\n\n"
        f"📈 Multi-TF: {tf_text}\n"
        f"📊 RSI: `{rsi or 'N/A'}`\n"
        f"📐 BOS: `{bos or 'Yoq'}`\n"
        f"🔲 FVG: `{fvg or 'Yoq'}`\n"
        f"📦 OB: `{'Ha' if ob else 'Yoq'}`\n"
        f"🎯 Score: `{score}/12`"
        f"{DISCLAIMER}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Batafsil", callback_data=f"det_{symbol}_{sid}"),
        InlineKeyboardButton("📰 Yangiliklar", callback_data=f"news_{coin}"),
    ]])

    return {
        "signal_msg": signal_msg, "detail_msg": detail_msg,
        "keyboard": keyboard, "symbol": symbol, "coin": coin,
        "sid": sid, "price": price,
        "change_24h": change_24h, "high_24h": high_24h,
        "low_24h": low_24h, "vol_24h": vol_24h,
        "rsi": rsi, "tf_text": tf_text,
    }

# ============================================================
# COIN INFO
# ============================================================
async def get_coin_info(symbol: str):
    all_data = await fetch_coin_data(symbol)
    if not all_data:
        return None, None

    ticker = all_data["ticker"]
    ohlc_4h = all_data["ohlc_4h"]
    ohlc_1d = all_data["ohlc_1d"]

    coin = symbol.replace("USDT", "")
    price = float(ticker.get("lastPrice", 0))
    change_24h = float(ticker.get("priceChangePercent", 0))
    high_24h = float(ticker.get("highPrice", 0))
    low_24h = float(ticker.get("lowPrice", 0))
    vol_24h = float(ticker.get("quoteVolume", 0))
    vol_base = float(ticker.get("volume", 0))

    main_ohlc = ohlc_4h or ohlc_1d
    rsi = None
    tf_text = ""
    if main_ohlc:
        closes = [c[4] for c in main_ohlc]
        rsi = calc_rsi(closes)
        tf_trends, _ = multi_tf(ohlc_4h, ohlc_1d)
        for tf, t in tf_trends.items():
            e = "📈" if t == "bullish" else "📉" if t == "bearish" else "⬌"
            tf_text += f"{e}{tf} "

    c24 = "🟢" if change_24h >= 0 else "🔴"

    msg = (
        f"📊 *{coin}/USDT*\n\n"
        f"💰 Narx: `${price:,.6f}`\n"
        f"{c24} 24h: `{change_24h:+.2f}%`\n"
        f"📈 24h Yuqori: `${high_24h:,.6f}`\n"
        f"📉 24h Past: `${low_24h:,.6f}`\n"
        f"📊 Hajm: `${vol_24h/1e6:.1f}M` USDT\n"
        f"🔄 Hajm: `{vol_base:,.0f}` {coin}\n\n"
        f"📐 Trend: {tf_text}\n"
        f"📊 RSI (14): `{rsi or 'N/A'}`"
        f"{DISCLAIMER}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yangilash", callback_data=f"info_{symbol}"),
        InlineKeyboardButton("🚨 Signal", callback_data=f"sig_{symbol}"),
    ], [
        InlineKeyboardButton("📰 Yangiliklar", callback_data=f"news_{coin}"),
        InlineKeyboardButton("🔙 Orqaga", callback_data="back_main"),
    ]])

    return msg, keyboard

# ============================================================
# KLAVIATURA
# ============================================================
def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("₿ BTC", callback_data="info_BTCUSDT"),
            InlineKeyboardButton("Ξ ETH", callback_data="info_ETHUSDT"),
            InlineKeyboardButton("◎ SOL", callback_data="info_SOLUSDT"),
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

# ============================================================
# HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text(
        "👋 *Rustamov Crypto Botiga xush kelibsiz!*\n\n"
        "🔍 Top 200 token kuzatiladi (Binance)\n"
        "⚡ Volume + SMC + Multi-TF + RSI\n"
        "🤖 TP/SL avtomatik kuzatiladi\n"
        "📅 Economic Calendar (FED, CPI, PCE)\n"
        "📰 Kripto yangiliklari\n\n"
        "👇 Tugma bosing yoki coin nomi yozing:\n"
        "`btc` `eth` `sol` `bnb` `doge` ...",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    # Symbol topish
    symbol = COIN_MAP.get(text)
    if not symbol:
        # To'g'ridan USDT qo'shib sinash
        symbol = text.upper() + "USDT"

    msg_obj = await update.message.reply_text("⏳ Tahlil qilinmoqda...")
    try:
        msg, keyboard = await get_coin_info(symbol)
        if msg:
            await msg_obj.edit_text(msg, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg_obj.edit_text(
                f"❌ *'{text}'* topilmadi.\n\nMisol: `btc` `eth` `sol` `bnb` `doge`",
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
        symbol = data[5:]
        await query.edit_message_text("⏳ Yuklanmoqda...", parse_mode="Markdown")
        msg, keyboard = await get_coin_info(symbol)
        if msg:
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await query.edit_message_text("❌ Malumot topilmadi.", reply_markup=main_keyboard())

    elif data.startswith("sig_"):
        symbol = data[4:]
        await query.edit_message_text("⏳ Signal tekshirilmoqda...", parse_mode="Markdown")
        result = await analyze_coin(symbol, force=True)
        if result:
            await query.edit_message_text(
                result["signal_msg"], parse_mode="Markdown",
                reply_markup=result["keyboard"]
            )
        else:
            msg, keyboard = await get_coin_info(symbol)
            if msg:
                await query.edit_message_text(
                    "📭 Hozircha signal yoq.\n\n" + msg,
                    parse_mode="Markdown", reply_markup=keyboard
                )

    elif data.startswith("det_"):
        parts = data.split("_")
        symbol = parts[1]
        await query.edit_message_text("⏳ Yuklanmoqda...", parse_mode="Markdown")
        result = await analyze_coin(symbol, force=True)
        if result:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Orqaga", callback_data=f"info_{symbol}")
            ]])
            await query.edit_message_text(
                result["detail_msg"], parse_mode="Markdown", reply_markup=kb
            )

    elif data.startswith("news_"):
        coin = data[5:]
        await query.edit_message_text("⏳ Yuklanmoqda...", parse_mode="Markdown")
        news = await get_news(coin)
        msg = f"📰 *{coin} Yangiliklari*\n\n"
        msg += "\n".join(news) if news else "Yangilik topilmadi."
        msg += DISCLAIMER
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Orqaga", callback_data="back_main")
        ]])
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)

    elif data == "calendar":
        await query.edit_message_text("⏳ Yuklanmoqda...", parse_mode="Markdown")
        events = await get_economic_calendar()
        msg = "📅 *Economic Calendar*\n\n"
        msg += "\n".join(events)
        msg += "\n\n_Manba: CoinMarketCal + rasmiy manba_" + DISCLAIMER
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Yangilash", callback_data="calendar"),
            InlineKeyboardButton("🔙 Orqaga", callback_data="back_main"),
        ]])
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)

    elif data == "watch_on":
        CHAT_IDS.add(query.from_user.id)
        await query.edit_message_text(
            "✅ *Signallar yoqildi!*\n"
            "Top 200 token har soatda tekshiriladi.\n"
            "TP/SL avtomatik kuzatiladi! 🤖",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    elif data == "watch_off":
        CHAT_IDS.discard(query.from_user.id)
        await query.edit_message_text(
            "🔕 *Signallar ochirildi.*",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    elif data == "hisobot":
        total = STATS["total_signals"]
        if total == 0:
            text = "📊 *Hisobot*\n\nHali signal berilmagan."
        else:
            tp_t = STATS["tp1_hit"] + STATS["tp2_hit"] + STATS["tp3_hit"]
            sl_c = STATS["sl_hit"]
            ochiq = total - tp_t - sl_c
            wr = round(tp_t / (tp_t + sl_c) * 100, 1) if (tp_t + sl_c) > 0 else 0
            text = (
                f"📊 *Signal Hisoboti*\n\n"
                f"Jami: `{total}` | ✅TP: `{tp_t}` | ❌SL: `{sl_c}` | 🔄: `{ochiq}`\n"
                f"Win rate: `{wr}%`\n\n*Songi signallar:*\n"
            )
            for s in reversed(STATS["signals_log"][-5:]):
                text += f"{s['status']} #{s['id']} *{s['coin']}* {s['direction']} `${s['entry']:,.4f}` {s['time']}\n"
        text += DISCLAIMER
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Orqaga", callback_data="back_main")
        ]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif data == "scan_now":
        await query.edit_message_text("⏳ Top 50 token tekshirilmoqda...")
        found = 0
        scan_list = WATCH_LIST[:50] if WATCH_LIST else list(COIN_MAP.values())
        for symbol in scan_list:
            result = await analyze_coin(symbol)
            if result:
                found += 1
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=result["signal_msg"],
                    parse_mode="Markdown",
                    reply_markup=result["keyboard"]
                )
            await asyncio.sleep(0.5)
        txt = f"✅ {found} signal topildi!" if found else "📭 Signal topilmadi."
        await context.bot.send_message(
            chat_id=query.message.chat_id, text=txt,
            reply_markup=main_keyboard()
        )

    elif data == "back_main":
        await query.edit_message_text(
            "👋 *Rustamov Crypto Bot*\n\n👇 Tugmalardan foydalaning:",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

# ============================================================
# AVTOMATIK TP/SL KUZATISH
# ============================================================
async def track_signals(bot):
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
                price = await get_current_price(sig["symbol"])
                if not price:
                    continue

                is_long = sig["is_long"]
                coin = sig["coin"]
                msg = None

                if not sig["tp1_hit"] and sig["tp1"]:
                    if (is_long and price >= sig["tp1"]) or (not is_long and price <= sig["tp1"]):
                        sig["tp1_hit"] = True
                        STATS["tp1_hit"] += 1
                        _update_status(sid, "TP1")
                        msg = (f"✅ *#{sid} {coin} — TP1 URDI!*\n"
                               f"💰 `${price:,.4f}` | TP1: `${sig['tp1']:,.4f}`{DISCLAIMER}")

                elif sig["tp1_hit"] and not sig["tp2_hit"] and sig["tp2"]:
                    if (is_long and price >= sig["tp2"]) or (not is_long and price <= sig["tp2"]):
                        sig["tp2_hit"] = True
                        STATS["tp2_hit"] += 1
                        _update_status(sid, "TP2")
                        msg = (f"✅✅ *#{sid} {coin} — TP2 URDI!*\n"
                               f"💰 `${price:,.4f}` | TP2: `${sig['tp2']:,.4f}`{DISCLAIMER}")

                elif sig["tp2_hit"] and not sig["tp3_hit"] and sig["tp3"]:
                    if (is_long and price >= sig["tp3"]) or (not is_long and price <= sig["tp3"]):
                        sig["tp3_hit"] = True
                        sig["active"] = False
                        STATS["tp3_hit"] += 1
                        _update_status(sid, "TP3")
                        msg = (f"✅✅✅ *#{sid} {coin} — TP3 URDI! MUKAMMAL!*\n"
                               f"💰 `${price:,.4f}` | TP3: `${sig['tp3']:,.4f}`{DISCLAIMER}")

                if not sig["sl_hit"] and not sig.get("tp3_hit"):
                    if (is_long and price <= sig["sl"]) or (not is_long and price >= sig["sl"]):
                        sig["sl_hit"] = True
                        sig["active"] = False
                        STATS["sl_hit"] += 1
                        _update_status(sid, "SL")
                        msg = (f"❌ *#{sid} {coin} — SL URDI*\n"
                               f"💰 `${price:,.4f}` | SL: `${sig['sl']:,.4f}`{DISCLAIMER}")

                if msg:
                    for chat_id in sig["chat_ids"]:
                        try:
                            await bot.send_message(
                                chat_id=chat_id, text=msg, parse_mode="Markdown"
                            )
                        except Exception as e:
                            logger.error(f"Track xabar: {e}")

                await asyncio.sleep(0.3)

            except Exception as e:
                logger.error(f"Track {sid}: {e}")

        for sid in closed:
            ACTIVE_SIGNALS.pop(sid, None)

def _update_status(sid, status):
    for s in STATS["signals_log"]:
        if s["id"] == sid:
            s["status"] = f"{'✅' if 'TP' in status else '❌'} {status}"

# ============================================================
# HAR SOATLIK SKAN
# ============================================================
async def hourly_scan(bot):
    global WATCH_LIST
    logger.info("Top 200 yuklanmoqda...")
    WATCH_LIST = await get_top200_symbols()
    logger.info(f"{len(WATCH_LIST)} token yuklandi")

    while True:
        await asyncio.sleep(3600)
        if not CHAT_IDS:
            continue

        new_list = await get_top200_symbols()
        if new_list:
            WATCH_LIST = new_list
        CACHE.clear()

        logger.info(f"Skan boshlandi — {len(WATCH_LIST)} token")
        found = 0

        for symbol in WATCH_LIST:
            try:
                result = await analyze_coin(symbol)
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
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"{symbol}: {e}")

        logger.info(f"Skan tugadi — {found} signal")

# ============================================================
# MAIN
# ============================================================
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
