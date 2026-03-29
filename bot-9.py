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
CACHE_TTL = 180

BITGET_BASE = "https://api.bitget.com"

# ============================================================
# BITGET API
# ============================================================
async def bitget_get(session, endpoint, params=None, retries=3):
    url = f"{BITGET_BASE}{endpoint}"
    for attempt in range(retries):
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    data = await r.json()
                    if data.get("code") == "00000":
                        return data.get("data")
                elif r.status == 429:
                    await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Bitget {endpoint}: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2)
    return None

async def get_all_tickers():
    async with aiohttp.ClientSession() as s:
        data = await bitget_get(s, "/api/v2/spot/market/tickers")
        if data:
            return {d["symbol"]: d for d in data if d.get("symbol", "").endswith("USDT")}
    return {}

async def get_klines(session, symbol, granularity="4h", limit=60):
    data = await bitget_get(session, "/api/v2/spot/market/candles", {
        "symbol": symbol, "granularity": granularity, "limit": str(limit)
    })
    if data:
        result = []
        for c in data:
            try:
                result.append([
                    float(c[1]),  # open
                    float(c[2]),  # high
                    float(c[3]),  # low
                    float(c[4]),  # close
                    float(c[5]),  # volume
                ])
            except:
                pass
        return result if len(result) >= 10 else None
    return None

async def get_current_price(symbol):
    async with aiohttp.ClientSession() as s:
        data = await bitget_get(s, "/api/v2/spot/market/tickers", {"symbol": symbol})
        if data:
            d = data[0] if isinstance(data, list) else data
            return float(d.get("lastPr", 0) or 0)
    return None

async def fetch_all_ohlc(symbol):
    """4H, 1H, 15M parallel olish"""
    now = datetime.now().timestamp()
    if symbol in CACHE and now - CACHE[symbol].get("ts", 0) < CACHE_TTL:
        return CACHE[symbol]

    async with aiohttp.ClientSession() as s:
        tasks = [
            get_klines(s, symbol, "4h", 60),
            get_klines(s, symbol, "1h", 60),
            get_klines(s, symbol, "15min", 60),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    ohlc_4h  = results[0] if not isinstance(results[0], Exception) else None
    ohlc_1h  = results[1] if not isinstance(results[1], Exception) else None
    ohlc_15m = results[2] if not isinstance(results[2], Exception) else None

    if not ohlc_4h:
        return None

    result = {"ohlc_4h": ohlc_4h, "ohlc_1h": ohlc_1h, "ohlc_15m": ohlc_15m, "ts": now}
    CACHE[symbol] = result
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
        h, l, pc = ohlc[i][1], ohlc[i][2], ohlc[i-1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period if trs else None

def calc_ema(prices, period):
    if not prices or len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def find_swing_highs(ohlc, lookback=3):
    if not ohlc or len(ohlc) < lookback * 2 + 1:
        return []
    highs = []
    for i in range(lookback, len(ohlc) - lookback):
        ch = ohlc[i][1]
        if ch > max(ohlc[j][1] for j in range(i-lookback, i)) and \
           ch > max(ohlc[j][1] for j in range(i+1, i+lookback+1)):
            highs.append(round(ch, 8))
    return sorted(set(highs))

def find_swing_lows(ohlc, lookback=3):
    if not ohlc or len(ohlc) < lookback * 2 + 1:
        return []
    lows = []
    for i in range(lookback, len(ohlc) - lookback):
        cl = ohlc[i][2]
        if cl < min(ohlc[j][2] for j in range(i-lookback, i)) and \
           cl < min(ohlc[j][2] for j in range(i+1, i+lookback+1)):
            lows.append(round(cl, 8))
    return sorted(set(lows))

def detect_trend(ohlc):
    if not ohlc or len(ohlc) < 20:
        return "sideways"
    closes = [c[3] for c in ohlc]
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, min(50, len(closes)))
    if not ema20 or not ema50:
        return "sideways"
    price = closes[-1]
    if price > ema20 and ema20 > ema50:
        return "bullish"
    elif price < ema20 and ema20 < ema50:
        return "bearish"
    return "sideways"

def detect_bos(ohlc):
    if not ohlc or len(ohlc) < 15:
        return None
    closes = [c[3] for c in ohlc]
    ph = max(closes[-15:-5])
    pl = min(closes[-15:-5])
    curr = closes[-1]
    if curr > ph * 1.001:
        return "bullish"
    elif curr < pl * 0.999:
        return "bearish"
    return None

def detect_fvg(ohlc):
    if not ohlc or len(ohlc) < 3:
        return None
    for i in range(len(ohlc)-3, max(0, len(ohlc)-8), -1):
        c1h, c1l = ohlc[i][1], ohlc[i][2]
        c3h, c3l = ohlc[i+2][1], ohlc[i+2][2]
        if c3l > c1h:
            return "Bullish"
        elif c3h < c1l:
            return "Bearish"
    return None

def find_entry_15m(ohlc_15m, is_long):
    """15M da aniq kirish nuqtasi — pullback yoki retest"""
    if not ohlc_15m or len(ohlc_15m) < 10:
        return None
    closes = [c[3] for c in ohlc_15m[-10:]]
    rsi_15m = calc_rsi(closes)
    if not rsi_15m:
        return None
    if is_long and rsi_15m < 50:
        return f"RSI {rsi_15m} (pullback)"
    elif not is_long and rsi_15m > 50:
        return f"RSI {rsi_15m} (retest)"
    return f"RSI {rsi_15m}"

def get_sl_tp(ohlc, price, is_long):
    s_highs = find_swing_highs(ohlc)
    s_lows = find_swing_lows(ohlc)
    atr = calc_atr(ohlc)
    a = atr or price * 0.03

    if is_long:
        lows_below = [l for l in s_lows if l < price * 0.999]
        sl = max(lows_below) * 0.997 if lows_below else round(price - a*2, 8)
        tps = sorted([h for h in s_highs if h > price * 1.005])[:3]
        if not tps:
            tps = [round(price+a*1.5,8), round(price+a*3,8), round(price+a*5,8)]
    else:
        highs_above = [h for h in s_highs if h > price * 1.001]
        sl = min(highs_above) * 1.003 if highs_above else round(price + a*2, 8)
        tps = sorted([l for l in s_lows if l < price * 0.995], reverse=True)[:3]
        if not tps:
            tps = [round(price-a*1.5,8), round(price-a*3,8), round(price-a*5,8)]
    return round(sl, 8), [round(t, 8) for t in tps]

# ============================================================
# KONSOLIDATSIYA BREAKOUT — 4H
# ============================================================
def detect_consolidation_breakout(ohlc_4h):
    """
    4H da konsolidatsiya + breakout aniqlash
    Returns: (breakout_type, range_size, vol_ratio, breakout_level, lookback)
    """
    if not ohlc_4h or len(ohlc_4h) < 15:
        return None, 0, 0, 0, 0

    # Volume o'rtacha (so'nggi 20 sham)
    vols = [c[4] for c in ohlc_4h[-25:-5]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    last_vol = ohlc_4h[-1][4]
    vol_ratio = round(last_vol / avg_vol, 1) if avg_vol > 0 else 0

    for lookback in [10, 15, 20, 30]:
        if len(ohlc_4h) < lookback + 3:
            continue

        consol = ohlc_4h[-(lookback+3):-3]
        last = ohlc_4h[-1]

        range_high = max(c[1] for c in consol)
        range_low  = min(c[2] for c in consol)

        if range_low == 0:
            continue

        range_size = (range_high - range_low) / range_low * 100

        if range_size > 15:
            continue

        last_close = last[3]

        if last_close > range_high * 1.002:
            return "PUMP 🚀", range_size, vol_ratio, range_high, lookback
        elif last_close < range_low * 0.998:
            return "DUMP 💥", range_size, vol_ratio, range_low, lookback

    return None, 0, 0, 0, 0

# ============================================================
# ASOSIY SKAN — MULTI TIMEFRAME
# ============================================================
async def scan_coin(symbol, ticker):
    try:
        price     = float(ticker.get("lastPr", 0) or 0)
        vol_24h   = float(ticker.get("usdtVolume", 0) or 0)
        change_24h = float(ticker.get("change24h", 0) or 0)

        # Tez filtr
        if price == 0 or vol_24h < 100_000:
            return None
        if abs(change_24h) < 2.0:
            return None

        # 4H, 1H, 15M olish
        all_data = await fetch_all_ohlc(symbol)
        if not all_data:
            return None

        ohlc_4h  = all_data["ohlc_4h"]
        ohlc_1h  = all_data["ohlc_1h"]
        ohlc_15m = all_data["ohlc_15m"]

        # 1. 4H — Konsolidatsiya + Breakout
        breakout_type, range_size, vol_ratio, breakout_level, lookback = \
            detect_consolidation_breakout(ohlc_4h)

        if not breakout_type:
            return None

        is_long = "PUMP" in breakout_type

        # 2. 1H — Trend tasdiqlash
        trend_1h = detect_trend(ohlc_1h) if ohlc_1h else "sideways"
        trend_4h = detect_trend(ohlc_4h)
        bos_1h   = detect_bos(ohlc_1h) if ohlc_1h else None
        fvg_1h   = detect_fvg(ohlc_1h) if ohlc_1h else None
        rsi_1h   = calc_rsi([c[3] for c in ohlc_1h]) if ohlc_1h else None

        # 3. 15M — Kirish nuqtasi
        entry_15m = find_entry_15m(ohlc_15m, is_long)
        rsi_15m   = calc_rsi([c[3] for c in ohlc_15m]) if ohlc_15m else None

        # 4H trend breakout yo'nalishi bilan mos kelishi kerak
        if is_long and trend_4h == "bearish":
            return None
        if not is_long and trend_4h == "bullish":
            return None

        # SL/TP — 4H asosida
        sl, tps = get_sl_tp(ohlc_4h, price, is_long)
        risk = abs(price - sl)
        if risk == 0:
            return None
        sl_pct = (sl - price) / price * 100

        # Signal kuchi hisoblash
        score = 0
        factors = []

        # Volume
        if vol_ratio >= 3.0:
            score += 3; factors.append(f"⚡{vol_ratio}x")
        elif vol_ratio >= 2.0:
            score += 2; factors.append(f"⚡{vol_ratio}x")
        elif vol_ratio >= 1.3:
            score += 1; factors.append(f"⚡{vol_ratio}x")

        # Trend
        if (is_long and trend_1h == "bullish") or (not is_long and trend_1h == "bearish"):
            score += 2; factors.append("📈1H")
        if (is_long and trend_4h == "bullish") or (not is_long and trend_4h == "bearish"):
            score += 1; factors.append("📈4H")

        # BOS
        if bos_1h == ("bullish" if is_long else "bearish"):
            score += 2; factors.append("📐BOS")

        # RSI
        if rsi_1h and ((is_long and rsi_1h < 55) or (not is_long and rsi_1h > 45)):
            score += 1; factors.append(f"📊{rsi_1h}")

        # FVG
        if fvg_1h:
            score += 1; factors.append("🔲FVG")

        strength = "🔴 KUCHLI" if score >= 7 else "🟡 O'RTA" if score >= 4 else "🟢 ZAIF"
        coin = symbol.replace("USDT", "")

        # TP matni
        tp_text = ""
        for i, tp in enumerate(tps[:3], 1):
            pct = (tp - price) / price * 100
            rr  = round(abs(tp - price) / risk, 1)
            tp_text += f"• TP{i}: `${tp:,.6f}` ({pct:+.1f}%) R/R:`1:{rr}`\n"

        # Signal saqlash
        STATS["total_signals"] += 1
        sid = STATS["total_signals"]
        STATS["signals_log"].append({
            "id": sid, "coin": coin, "coin_id": symbol,
            "entry": price, "sl": sl,
            "tp1": tps[0] if tps else None,
            "tp2": tps[1] if len(tps) > 1 else None,
            "tp3": tps[2] if len(tps) > 2 else None,
            "direction": "LONG" if is_long else "SHORT",
            "score": score, "status": "🔄 Ochiq",
            "time": datetime.now().strftime('%Y-%m-%d %H:%M'),
            "is_long": is_long,
            "pnl": 0.0,
        })

        ACTIVE_SIGNALS[sid] = {
            "coin_id": symbol, "symbol": coin, "entry": price,
            "sl": sl, "is_long": is_long,
            "tp1": tps[0] if tps else None,
            "tp2": tps[1] if len(tps) > 1 else None,
            "tp3": tps[2] if len(tps) > 2 else None,
            "tp1_hit": False, "tp2_hit": False, "tp3_hit": False, "sl_hit": False,
            "chat_ids": set(CHAT_IDS), "active": True,
            "risk_pct": abs(sl_pct),
        }

        emoji = "🚀" if is_long else "💥"
        trend_e_4h = "📈" if trend_4h == "bullish" else "📉" if trend_4h == "bearish" else "⬌"
        trend_e_1h = "📈" if trend_1h == "bullish" else "📉" if trend_1h == "bearish" else "⬌"

        msg = (
            f"{emoji} *#{sid} — {coin}/USDT — {breakout_type}*\n"
            f"{strength} | {' | '.join(factors)}\n\n"
            f"📦 *4H Konsolidatsiya:* `{lookback}` sham | `{range_size:.1f}%`\n"
            f"🔓 *Breakout:* `${breakout_level:,.6f}`\n"
            f"{trend_e_4h} *4H Trend:* `{trend_4h}` | {trend_e_1h} *1H:* `{trend_1h}`\n"
            f"📊 *1H RSI:* `{rsi_1h or 'N/A'}` | *15M RSI:* `{rsi_15m or 'N/A'}`\n"
            f"📐 *BOS:* `{bos_1h or 'Yoq'}` | 🔲 *FVG:* `{fvg_1h or 'Yoq'}`\n"
            f"🎯 *15M Entry:* `{entry_15m or 'Kutilmoqda'}`\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🎯 *Trade Setup*\n"
            f"• Entry: `${price:,.6f}`\n"
            f"• SL: `${sl:,.6f}` ({sl_pct:+.1f}%)\n"
            f"{tp_text}"
            f"━━━━━━━━━━━━━━━\n"
            f"24h: `{change_24h:+.2f}%` | Vol: `${vol_24h/1e6:.1f}M`\n"
            f"🤖 _Bot avtomatik kuzatib boradi_"
            f"{DISCLAIMER}"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ TP1 #{sid}", callback_data=f"tp1_{sid}"),
            InlineKeyboardButton(f"✅ TP2 #{sid}", callback_data=f"tp2_{sid}"),
            InlineKeyboardButton(f"❌ SL #{sid}", callback_data=f"sl_{sid}"),
        ]])

        return {"msg": msg, "keyboard": keyboard, "symbol": coin, "sid": sid}

    except Exception as e:
        logger.error(f"scan_coin {symbol}: {e}")
        return None

# ============================================================
# COIN INFO
# ============================================================
async def get_coin_info(symbol):
    async with aiohttp.ClientSession() as s:
        ticker_data = await bitget_get(s, "/api/v2/spot/market/tickers", {"symbol": symbol})
        if not ticker_data:
            return None, None
        ticker = ticker_data[0] if isinstance(ticker_data, list) else ticker_data
        ohlc_4h = await get_klines(s, symbol, "4h", 60)
        ohlc_1h = await get_klines(s, symbol, "1h", 30)

    coin = symbol.replace("USDT", "")
    price = float(ticker.get("lastPr", 0) or 0)
    change_24h = float(ticker.get("change24h", 0) or 0)
    high_24h = float(ticker.get("high24h", 0) or 0)
    low_24h = float(ticker.get("low24h", 0) or 0)
    vol_24h = float(ticker.get("usdtVolume", 0) or 0)

    rsi_4h = calc_rsi([c[3] for c in ohlc_4h]) if ohlc_4h else None
    rsi_1h = calc_rsi([c[3] for c in ohlc_1h]) if ohlc_1h else None
    trend_4h = detect_trend(ohlc_4h) if ohlc_4h else "N/A"
    trend_1h = detect_trend(ohlc_1h) if ohlc_1h else "N/A"

    c24 = "🟢" if change_24h >= 0 else "🔴"
    t4 = "📈" if trend_4h == "bullish" else "📉" if trend_4h == "bearish" else "⬌"
    t1 = "📈" if trend_1h == "bullish" else "📉" if trend_1h == "bearish" else "⬌"

    msg = (
        f"📊 *{coin}/USDT* (Bitget)\n\n"
        f"💰 Narx: `${price:,.6f}`\n"
        f"{c24} 24h: `{change_24h:+.2f}%`\n"
        f"📈 High: `${high_24h:,.6f}` | 📉 Low: `${low_24h:,.6f}`\n"
        f"📊 Hajm: `${vol_24h/1e6:.1f}M`\n\n"
        f"{t4} 4H Trend: `{trend_4h}` | RSI: `{rsi_4h or 'N/A'}`\n"
        f"{t1} 1H Trend: `{trend_1h}` | RSI: `{rsi_1h or 'N/A'}`"
        f"{DISCLAIMER}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yangilash", callback_data=f"info_{symbol}"),
        InlineKeyboardButton("🔍 Breakout?", callback_data=f"chk_{symbol}"),
    ], [
        InlineKeyboardButton("🔙 Orqaga", callback_data="back_main"),
    ]])

    return msg, keyboard

# ============================================================
# HISOBOT — PNL bilan
# ============================================================
def generate_report():
    total = STATS["total_signals"]
    if total == 0:
        return "📊 *Hisobot*\n\nHali signal berilmagan." + DISCLAIMER

    tp_t = STATS["tp1_hit"] + STATS["tp2_hit"] + STATS["tp3_hit"]
    sl_c = STATS["sl_hit"]
    ochiq = total - tp_t - sl_c
    wr = round(tp_t / (tp_t + sl_c) * 100, 1) if (tp_t + sl_c) > 0 else 0

    # PNL hisoblash
    total_pnl = 0.0
    for s in STATS["signals_log"]:
        entry = s["entry"]
        if entry == 0:
            continue
        is_long = s["is_long"]
        status = s["status"]

        if "TP1" in status and s["tp1"]:
            pct = (s["tp1"] - entry) / entry * 100
            total_pnl += pct if is_long else -pct
        elif "TP2" in status and s["tp2"]:
            pct = (s["tp2"] - entry) / entry * 100
            total_pnl += pct if is_long else -pct
        elif "TP3" in status and s["tp3"]:
            pct = (s["tp3"] - entry) / entry * 100
            total_pnl += pct if is_long else -pct
        elif "SL" in status and s["sl"]:
            pct = (s["sl"] - entry) / entry * 100
            total_pnl += pct if is_long else -pct

    pnl_emoji = "✅" if total_pnl >= 0 else "❌"

    text = (
        f"📊 *Signal Hisoboti* (Bitget)\n\n"
        f"Jami: `{total}` | ✅TP: `{tp_t}` | ❌SL: `{sl_c}` | 🔄: `{ochiq}`\n"
        f"Win rate: `{wr}%`\n"
        f"{pnl_emoji} Jami PNL: `{total_pnl:+.2f}%`\n\n"
        f"*So'nggi signallar:*\n"
    )
    for s in reversed(STATS["signals_log"][-5:]):
        text += f"{s['status']} #{s['id']} *{s['coin']}* {s['direction']} `${s['entry']:,.6f}` {s['time']}\n"

    text += DISCLAIMER
    return text

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
    ])

# ============================================================
# HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text(
        "👋 *Rustamov Crypto Botiga xush kelibsiz!*\n\n"
        "🏦 Birja: *Bitget Spot*\n"
        "🔍 600+ token kuzatiladi\n"
        "📊 Multi-TF: 4H → 1H → 15M\n"
        "🚀 Pump / 💥 Dump signallari\n"
        "⏱ Har 2 daqiqada skan\n"
        "🤖 TP/SL avtomatik | PNL hisobot\n\n"
        "👇 Tugma bosing yoki coin yozing:\n"
        "`BTC` `ETH` `SOL` `PEPE` `SHIB` ...",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    symbol = text if text.endswith("USDT") else text + "USDT"
    msg_obj = await update.message.reply_text("⏳ Tahlil qilinmoqda...")
    try:
        msg, keyboard = await get_coin_info(symbol)
        if msg:
            await msg_obj.edit_text(msg, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await msg_obj.edit_text(
                f"❌ *'{text}'* topilmadi.\n`BTC` `ETH` `SOL` `PEPE`",
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
            await query.edit_message_text("❌ Topilmadi.", reply_markup=main_keyboard())

    elif data.startswith("chk_"):
        symbol = data[4:]
        coin = symbol.replace("USDT", "")
        await query.edit_message_text(f"⏳ {coin} tekshirilmoqda...", parse_mode="Markdown")
        async with aiohttp.ClientSession() as s:
            ticker_data = await bitget_get(s, "/api/v2/spot/market/tickers", {"symbol": symbol})
        if ticker_data:
            ticker = ticker_data[0] if isinstance(ticker_data, list) else ticker_data
            result = await scan_coin(symbol, ticker)
            if result:
                await query.edit_message_text(result["msg"], parse_mode="Markdown", reply_markup=result["keyboard"])
                return
        msg, keyboard = await get_coin_info(symbol)
        if msg:
            await query.edit_message_text("📭 Hozircha breakout yoq.\n\n" + msg, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "watch_on":
        CHAT_IDS.add(query.from_user.id)
        await query.edit_message_text(
            "✅ *Signallar yoqildi!*\n"
            "📊 4H → 1H → 15M tahlil\n"
            "⏱ Har 2 daqiqada 600+ token\n"
            "🚀 Pump/Dump bo'lganda darhol signal!",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    elif data == "watch_off":
        CHAT_IDS.discard(query.from_user.id)
        await query.edit_message_text("🔕 *Signallar ochirildi.*", parse_mode="Markdown", reply_markup=main_keyboard())

    elif data == "hisobot":
        await query.edit_message_text(generate_report(), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="back_main")]]))

    elif data == "scan_now":
        await query.edit_message_text("⏳ 600+ token tekshirilmoqda... (2-3 daqiqa)")
        tickers = await get_all_tickers()
        scan_list = sorted(
            list(tickers.keys()),
            key=lambda s: float(tickers.get(s, {}).get("usdtVolume", 0) or 0),
            reverse=True
        )
        found = 0
        for symbol in scan_list:
            result = await scan_coin(symbol, tickers[symbol])
            if result:
                found += 1
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=result["msg"], parse_mode="Markdown",
                    reply_markup=result["keyboard"]
                )
            await asyncio.sleep(0.2)
        txt = f"✅ {found} signal topildi!" if found else "📭 Hozircha breakout topilmadi."
        await context.bot.send_message(chat_id=query.message.chat_id, text=txt, reply_markup=main_keyboard())

    elif data == "back_main":
        await query.edit_message_text(
            "👋 *Rustamov Crypto Bot* | Bitget\n\n👇 Tugmalardan foydalaning:",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    elif any(data.startswith(x) for x in ["tp1_", "tp2_", "tp3_", "sl_"]):
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
# AVTOMATIK TP/SL KUZATISH
# ============================================================
async def track_signals(bot):
    while True:
        await asyncio.sleep(60)
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
                coin = sig["symbol"]
                msg = None

                if not sig["tp1_hit"] and sig["tp1"]:
                    if (is_long and price >= sig["tp1"]) or (not is_long and price <= sig["tp1"]):
                        sig["tp1_hit"] = True
                        STATS["tp1_hit"] += 1
                        _mark(sid, "TP1")
                        pct = abs(sig["tp1"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"✅ *#{sid} {coin} — TP1 URDI!*\n💰 `${price:,.6f}` | +`{pct:.1f}%`{DISCLAIMER}"

                elif sig["tp1_hit"] and not sig["tp2_hit"] and sig["tp2"]:
                    if (is_long and price >= sig["tp2"]) or (not is_long and price <= sig["tp2"]):
                        sig["tp2_hit"] = True
                        STATS["tp2_hit"] += 1
                        _mark(sid, "TP2")
                        pct = abs(sig["tp2"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"✅✅ *#{sid} {coin} — TP2 URDI!*\n💰 `${price:,.6f}` | +`{pct:.1f}%`{DISCLAIMER}"

                elif sig["tp2_hit"] and not sig["tp3_hit"] and sig["tp3"]:
                    if (is_long and price >= sig["tp3"]) or (not is_long and price <= sig["tp3"]):
                        sig["tp3_hit"] = True
                        sig["active"] = False
                        STATS["tp3_hit"] += 1
                        _mark(sid, "TP3")
                        pct = abs(sig["tp3"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"✅✅✅ *#{sid} {coin} — TP3 URDI! MUKAMMAL!*\n💰 `${price:,.6f}` | +`{pct:.1f}%`{DISCLAIMER}"

                if not sig["sl_hit"] and not sig.get("tp3_hit"):
                    if (is_long and price <= sig["sl"]) or (not is_long and price >= sig["sl"]):
                        sig["sl_hit"] = True
                        sig["active"] = False
                        STATS["sl_hit"] += 1
                        _mark(sid, "SL")
                        pct = abs(sig["sl"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"❌ *#{sid} {coin} — SL URDI*\n💰 `${price:,.6f}` | -`{pct:.1f}%`{DISCLAIMER}"

                if msg:
                    for chat_id in sig["chat_ids"]:
                        try:
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                        except Exception as e:
                            logger.error(f"Track: {e}")
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.error(f"Track {sid}: {e}")
        for sid in closed:
            ACTIVE_SIGNALS.pop(sid, None)

# ============================================================
# HAR 2 DAQIQALIK SKAN
# ============================================================
async def periodic_scan(bot):
    global WATCH_LIST

    logger.info("Bitget tickerlar yuklanmoqda...")
    tickers = await get_all_tickers()
    WATCH_LIST = list(tickers.keys())
    logger.info(f"{len(WATCH_LIST)} token yuklandi")

    scan_count = 0
    no_signal_count = 0

    while True:
        await asyncio.sleep(120)
        if not CHAT_IDS:
            continue

        # Har skanda yangi tickerlar
        new_tickers = await get_all_tickers()
        if new_tickers:
            tickers = new_tickers
            WATCH_LIST = list(tickers.keys())
        CACHE.clear()

        scan_count += 1
        logger.info(f"Skan #{scan_count} — {len(WATCH_LIST)} token")
        found = 0

        sorted_symbols = sorted(
            WATCH_LIST,
            key=lambda s: float(tickers.get(s, {}).get("usdtVolume", 0) or 0),
            reverse=True
        )

        for symbol in sorted_symbols:
            try:
                result = await scan_coin(symbol, tickers.get(symbol, {}))
                if result:
                    found += 1
                    for chat_id in list(CHAT_IDS):
                        try:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=result["msg"],
                                parse_mode="Markdown",
                                reply_markup=result["keyboard"]
                            )
                        except Exception as e:
                            logger.error(f"Yuborish: {e}")
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.error(f"{symbol}: {e}")

        logger.info(f"Skan #{scan_count} tugadi — {found} signal")

        if found == 0:
            no_signal_count += 1
            if no_signal_count % 5 == 0:
                for chat_id in list(CHAT_IDS):
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"📭 *Signal topilmadi*\n"
                                 f"🕐 {datetime.now().strftime('%H:%M')} | "
                                 f"{len(sorted_symbols)} token tekshirildi",
                            parse_mode="Markdown"
                        )
                    except:
                        pass
        else:
            no_signal_count = 0

# ============================================================
# MAIN
# ============================================================
async def post_init(app):
    loop = asyncio.get_event_loop()
    loop.create_task(periodic_scan(app.bot))
    loop.create_task(track_signals(app.bot))
    logger.info("✅ Bot ishga tushdi! (Bitget Multi-TF)")

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
