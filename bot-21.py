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
CACHE_TTL = 300  # 5 daqiqa

# ============================================================
# BITGET API
# ============================================================
BITGET_BASE = "https://api.bitget.com"

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
            logger.error(f"Bitget API {endpoint}: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2)
    return None

async def get_all_symbols():
    """Bitget dan barcha USDT juftliklarini olish"""
    async with aiohttp.ClientSession() as s:
        data = await bitget_get(s, "/api/v2/spot/market/tickers")
        if data:
            usdt = [d for d in data if d.get("symbol", "").endswith("USDT")]
            usdt.sort(key=lambda x: float(x.get("usdtVolume", 0)), reverse=True)
            return [d["symbol"] for d in usdt[:300]]
    return []

async def get_ticker(symbol: str):
    """24 soatlik ticker"""
    async with aiohttp.ClientSession() as s:
        data = await bitget_get(s, "/api/v2/spot/market/tickers", {"symbol": symbol})
        if data and isinstance(data, list):
            return data[0]
        elif data and isinstance(data, dict):
            return data
    return None

async def get_klines(symbol: str, granularity="4h", limit=100):
    """OHLCV ma'lumotlari"""
    async with aiohttp.ClientSession() as s:
        data = await bitget_get(s, "/api/v2/spot/market/candles", {
            "symbol": symbol,
            "granularity": granularity,
            "limit": str(limit)
        })
        if data:
            # [timestamp, open, high, low, close, volume, quoteVolume]
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
            return result
    return None

async def get_current_price(symbol: str):
    """Hozirgi narx"""
    ticker = await get_ticker(symbol)
    if ticker:
        return float(ticker.get("lastPr", 0) or ticker.get("close", 0))
    return None

async def fetch_coin_data(symbol: str):
    """Cache bilan barcha ma'lumotlarni olish"""
    now = datetime.now().timestamp()
    if symbol in CACHE and now - CACHE[symbol].get("ts", 0) < CACHE_TTL:
        return CACHE[symbol]

    async with aiohttp.ClientSession() as s:
        tasks = [
            bitget_get(s, "/api/v2/spot/market/tickers", {"symbol": symbol}),
            bitget_get(s, "/api/v2/spot/market/candles", {
                "symbol": symbol, "granularity": "4h", "limit": "100"
            }),
            bitget_get(s, "/api/v2/spot/market/candles", {
                "symbol": symbol, "granularity": "1d", "limit": "50"
            }),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    ticker_data = results[0] if not isinstance(results[0], Exception) else None
    klines_4h = results[1] if not isinstance(results[1], Exception) else None
    klines_1d = results[2] if not isinstance(results[2], Exception) else None

    if not ticker_data:
        return None

    ticker = ticker_data[0] if isinstance(ticker_data, list) else ticker_data

    def parse_klines(klines):
        if not klines:
            return None
        result = []
        for c in klines:
            try:
                result.append([float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
            except:
                pass
        return result if result else None

    data = {
        "ticker": ticker,
        "ohlc_4h": parse_klines(klines_4h),
        "ohlc_1d": parse_klines(klines_1d),
        "ts": now
    }
    CACHE[symbol] = data
    return data

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

def get_sl_tp(ohlc, price, is_long):
    s_highs = find_swing_highs(ohlc)
    s_lows = find_swing_lows(ohlc)
    atr = calc_atr(ohlc)
    a = atr or price * 0.02

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
# KONSOLIDATSIYA + BREAKOUT ANIQLASH
# ============================================================
def detect_consolidation_breakout(ohlc, vol_24h, change_24h):
    """
    Konsolidatsiyadan chiqishni aniqlash:
    1. So'nggi 20-50 sham tor diapazon
    2. Oxirgi sham diapazondan chiqdi
    3. Volume spike bor
    """
    if not ohlc or len(ohlc) < 25:
        return None, 0, 0, 0, 0

    # Konsolidatsiya shamlarini topish (oxirgi 5 sham bundan tashqari)
    best_result = None

    for lookback in [20, 30, 50]:
        if len(ohlc) < lookback + 5:
            continue

        consol = ohlc[-(lookback+5):-5]
        recent = ohlc[-5:]
        last   = ohlc[-1]

        highs = [c[1] for c in consol]
        lows  = [c[2] for c in consol]

        range_high = max(highs)
        range_low  = min(lows)

        if range_low == 0:
            continue

        range_size = (range_high - range_low) / range_low * 100

        # Konsolidatsiya: diapazon 10% dan kichik
        if range_size > 10:
            continue

        # Oxirgi narx
        last_close = last[3]
        last_high  = last[1]
        last_low   = last[2]

        # Volume spike
        avg_vol = vol_24h / (abs(change_24h) / 5 + 1) if change_24h else vol_24h * 0.6
        vol_ratio = round(vol_24h / avg_vol, 1) if avg_vol > 0 else 0

        if vol_ratio < 1.5:
            continue

        # Breakout yo'nalishi
        if last_close > range_high * 1.003:
            pct_move = (last_close - range_high) / range_high * 100
            best_result = ("PUMP 🚀", range_size, vol_ratio, range_high, lookback)
            break
        elif last_close < range_low * 0.997:
            pct_move = (range_low - last_close) / range_low * 100
            best_result = ("DUMP 💥", range_size, vol_ratio, range_low, lookback)
            break

    if best_result:
        return best_result
    return None, 0, 0, 0, 0

# ============================================================
# ASOSIY SIGNAL TAHLIL
# ============================================================
async def scan_coin(symbol: str):
    """Bitta coin uchun pump/dump signalini tekshirish"""
    all_data = await fetch_coin_data(symbol)
    if not all_data:
        return None

    ticker = all_data["ticker"]
    ohlc_4h = all_data["ohlc_4h"]
    ohlc_1d = all_data["ohlc_1d"]
    main_ohlc = ohlc_4h or ohlc_1d

    if not main_ohlc or len(main_ohlc) < 25:
        return None

    coin = symbol.replace("USDT", "").replace("usdt", "")
    price = float(ticker.get("lastPr", 0) or ticker.get("close", 0) or 0)
    vol_24h = float(ticker.get("usdtVolume", 0) or ticker.get("quoteVol", 0) or 0)
    change_24h = float(ticker.get("change24h", 0) or ticker.get("priceChangePercent", 0) or 0)
    high_24h = float(ticker.get("high24h", 0) or 0)
    low_24h = float(ticker.get("low24h", 0) or 0)

    if price == 0 or vol_24h == 0:
        return None

    # Konsolidatsiya breakout tekshirish
    breakout_type, range_size, vol_ratio, breakout_level, lookback_days = \
        detect_consolidation_breakout(main_ohlc, vol_24h, change_24h)

    if not breakout_type:
        return None

    is_long = "PUMP" in breakout_type
    trend = detect_trend(main_ohlc)
    closes = [c[3] for c in main_ohlc]
    rsi = calc_rsi(closes)
    atr = calc_atr(main_ohlc)

    # SL/TP hisoblash
    sl, tps = get_sl_tp(main_ohlc, price, is_long)
    risk = abs(price - sl)
    rr1 = round(abs(tps[0] - price) / risk, 1) if risk > 0 and tps else 0
    rr2 = round(abs(tps[1] - price) / risk, 1) if risk > 0 and len(tps) > 1 else 0
    rr3 = round(abs(tps[2] - price) / risk, 1) if risk > 0 and len(tps) > 2 else 0

    sl_pct = (sl - price) / price * 100

    # Signal kuchi
    score = 0
    if vol_ratio >= 3.0: score += 3
    elif vol_ratio >= 2.0: score += 2
    else: score += 1

    if (is_long and trend == "bullish") or (not is_long and trend == "bearish"):
        score += 2
    if rsi and ((is_long and rsi < 50) or (not is_long and rsi > 50)):
        score += 1

    strength = "🔴 KUCHLI" if score >= 5 else "🟡 O'RTA" if score >= 3 else "🟢 ZAIF"

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
    })

    ACTIVE_SIGNALS[sid] = {
        "coin_id": symbol, "symbol": coin, "entry": price,
        "sl": sl, "is_long": is_long,
        "tp1": tps[0] if tps else None,
        "tp2": tps[1] if len(tps) > 1 else None,
        "tp3": tps[2] if len(tps) > 2 else None,
        "tp1_hit": False, "tp2_hit": False, "tp3_hit": False, "sl_hit": False,
        "chat_ids": set(CHAT_IDS), "active": True,
    }

    trend_e = "📈" if trend == "bullish" else "📉" if trend == "bearish" else "⬌"
    emoji = "🚀" if is_long else "💥"

    tp_text = ""
    for i, tp in enumerate(tps[:3], 1):
        pct = (tp - price) / price * 100
        rr = [rr1, rr2, rr3][i-1]
        tp_text += f"• TP{i}: `${tp:,.6f}` ({pct:+.1f}%) R/R:`1:{rr}`\n"

    msg = (
        f"{emoji} *#{sid} — {coin}/USDT — {breakout_type}*\n"
        f"{strength}\n\n"
        f"📦 *Konsolidatsiya:* `{lookback_days}` sham | `{range_size:.1f}%` diapazon\n"
        f"⚡ *Volume:* `{vol_ratio}x` noodatiy\n"
        f"🔓 *Breakout:* `${breakout_level:,.6f}`\n"
        f"{trend_e} *Trend:* `{trend}`\n"
        f"📊 *RSI:* `{rsi or 'N/A'}`\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 *Trade Setup*\n"
        f"• Entry: `${price:,.6f}`\n"
        f"• SL: `${sl:,.6f}` ({sl_pct:+.1f}%)\n"
        f"{tp_text}"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 24h: `{change_24h:+.2f}%`\n"
        f"📊 Hajm: `${vol_24h/1e6:.1f}M` USDT\n"
        f"🤖 _Bot avtomatik kuzatib boradi_"
        f"{DISCLAIMER}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ TP1 #{sid}", callback_data=f"tp1_{sid}"),
        InlineKeyboardButton(f"✅ TP2 #{sid}", callback_data=f"tp2_{sid}"),
        InlineKeyboardButton(f"❌ SL #{sid}", callback_data=f"sl_{sid}"),
    ]])

    return {"msg": msg, "keyboard": keyboard, "symbol": coin,
            "sid": sid, "price": price, "is_long": is_long}

# ============================================================
# COIN INFO
# ============================================================
async def get_coin_info(symbol: str):
    all_data = await fetch_coin_data(symbol)
    if not all_data:
        return None, None

    ticker = all_data["ticker"]
    ohlc = all_data["ohlc_4h"] or all_data["ohlc_1d"]

    coin = symbol.replace("USDT", "")
    price = float(ticker.get("lastPr", 0) or ticker.get("close", 0) or 0)
    change_24h = float(ticker.get("change24h", 0) or 0)
    high_24h = float(ticker.get("high24h", 0) or 0)
    low_24h = float(ticker.get("low24h", 0) or 0)
    vol_24h = float(ticker.get("usdtVolume", 0) or ticker.get("quoteVol", 0) or 0)

    rsi = None
    trend = "N/A"
    if ohlc and len(ohlc) > 14:
        closes = [c[3] for c in ohlc]
        rsi = calc_rsi(closes)
        trend = detect_trend(ohlc)

    c24 = "🟢" if change_24h >= 0 else "🔴"
    trend_e = "📈" if trend == "bullish" else "📉" if trend == "bearish" else "⬌"

    msg = (
        f"📊 *{coin}/USDT* (Bitget)\n\n"
        f"💰 Narx: `${price:,.6f}`\n"
        f"{c24} 24h: `{change_24h:+.2f}%`\n"
        f"📈 24h Yuqori: `${high_24h:,.6f}`\n"
        f"📉 24h Past: `${low_24h:,.6f}`\n"
        f"📊 Hajm: `${vol_24h/1e6:.1f}M` USDT\n\n"
        f"{trend_e} Trend: `{trend}`\n"
        f"📊 RSI: `{rsi or 'N/A'}`"
        f"{DISCLAIMER}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yangilash", callback_data=f"info_{symbol}"),
        InlineKeyboardButton("🚨 Breakout?", callback_data=f"scan_{symbol}"),
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
        "🏦 Birja: *Bitget*\n"
        "🔍 300 token kuzatiladi\n"
        "📦 Konsolidatsiya + Breakout aniqlash\n"
        "🚀 Pump / 💥 Dump signallari\n"
        "🤖 TP/SL avtomatik kuzatiladi\n\n"
        "👇 Tugma bosing yoki coin nomi yozing:\n"
        "`BTC` `ETH` `SOL` `BNB` `DOGE` ...",
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
                f"❌ *'{text}'* topilmadi.\n`BTC` `ETH` `SOL` `BNB` `DOGE`",
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

    elif data.startswith("scan_"):
        symbol = data[5:]
        coin = symbol.replace("USDT", "")
        await query.edit_message_text(f"⏳ {coin} breakout tekshirilmoqda...", parse_mode="Markdown")
        result = await scan_coin(symbol)
        if result:
            await query.edit_message_text(result["msg"], parse_mode="Markdown", reply_markup=result["keyboard"])
        else:
            msg, keyboard = await get_coin_info(symbol)
            if msg:
                await query.edit_message_text("📭 Hozircha breakout yoq.\n\n" + msg, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "watch_on":
        CHAT_IDS.add(query.from_user.id)
        await query.edit_message_text(
            "✅ *Signallar yoqildi!*\n"
            "300 token har 30 daqiqada tekshiriladi.\n"
            "Pump/Dump bo'lganda darhol xabar! 🚀💥",
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
            text = (
                f"📊 *Signal Hisoboti* (Bitget)\n\n"
                f"Jami: `{total}` | ✅TP: `{tp_t}` | ❌SL: `{sl_c}` | 🔄: `{ochiq}`\n"
                f"Win rate: `{wr}%`\n\n*So'nggi signallar:*\n"
            )
            for s in reversed(STATS["signals_log"][-5:]):
                text += f"{s['status']} #{s['id']} *{s['coin']}* {s['direction']} `${s['entry']:,.6f}` {s['time']}\n"
        text += DISCLAIMER
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="back_main")]])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    elif data == "scan_now":
        await query.edit_message_text("⏳ Top 50 token tekshirilmoqda... (1-2 daqiqa)")
        found = 0
        scan_list = WATCH_LIST[:50] if WATCH_LIST else ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","DOGEUSDT"]
        for symbol in scan_list:
            result = await scan_coin(symbol)
            if result:
                found += 1
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=result["msg"],
                    parse_mode="Markdown",
                    reply_markup=result["keyboard"]
                )
            await asyncio.sleep(0.5)
        txt = f"✅ {found} signal topildi!" if found else "📭 Hozircha breakout topilmadi."
        await context.bot.send_message(chat_id=query.message.chat_id, text=txt, reply_markup=main_keyboard())

    elif data == "back_main":
        await query.edit_message_text(
            "👋 *Rustamov Crypto Bot* | Bitget\n\n👇 Tugmalardan foydalaning:",
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
# AVTOMATIK TP/SL KUZATISH
# ============================================================
async def track_signals(bot):
    while True:
        await asyncio.sleep(120)  # 2 daqiqada bir
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
                        msg = f"✅ *#{sid} {coin} — TP1 URDI!*\n💰 `${price:,.6f}` | TP1: `${sig['tp1']:,.6f}`{DISCLAIMER}"

                elif sig["tp1_hit"] and not sig["tp2_hit"] and sig["tp2"]:
                    if (is_long and price >= sig["tp2"]) or (not is_long and price <= sig["tp2"]):
                        sig["tp2_hit"] = True
                        STATS["tp2_hit"] += 1
                        _mark(sid, "TP2")
                        msg = f"✅✅ *#{sid} {coin} — TP2 URDI!*\n💰 `${price:,.6f}` | TP2: `${sig['tp2']:,.6f}`{DISCLAIMER}"

                elif sig["tp2_hit"] and not sig["tp3_hit"] and sig["tp3"]:
                    if (is_long and price >= sig["tp3"]) or (not is_long and price <= sig["tp3"]):
                        sig["tp3_hit"] = True
                        sig["active"] = False
                        STATS["tp3_hit"] += 1
                        _mark(sid, "TP3")
                        msg = f"✅✅✅ *#{sid} {coin} — TP3 URDI! MUKAMMAL!*\n💰 `${price:,.6f}`{DISCLAIMER}"

                if not sig["sl_hit"] and not sig.get("tp3_hit"):
                    if (is_long and price <= sig["sl"]) or (not is_long and price >= sig["sl"]):
                        sig["sl_hit"] = True
                        sig["active"] = False
                        STATS["sl_hit"] += 1
                        _mark(sid, "SL")
                        msg = f"❌ *#{sid} {coin} — SL URDI*\n💰 `${price:,.6f}` | SL: `${sig['sl']:,.6f}`{DISCLAIMER}"

                if msg:
                    for chat_id in sig["chat_ids"]:
                        try:
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                        except Exception as e:
                            logger.error(f"Track xabar: {e}")
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"Track {sid}: {e}")
        for sid in closed:
            ACTIVE_SIGNALS.pop(sid, None)

# ============================================================
# HAR 2 DAQIQALIK SKAN
# ============================================================
async def periodic_scan(bot):
    global WATCH_LIST
    logger.info("Top 300 yuklanmoqda (Bitget)...")
    WATCH_LIST = await get_all_symbols()
    logger.info(f"{len(WATCH_LIST)} token yuklandi")

    no_signal_count = 0  # Signal topilmagan skanlar soni

    while True:
        await asyncio.sleep(120)  # 2 daqiqa
        if not CHAT_IDS:
            continue

        # Har soatda WATCH_LIST yangilash
        if no_signal_count % 30 == 0:
            new_list = await get_all_symbols()
            if new_list:
                WATCH_LIST = new_list
            CACHE.clear()

        logger.info(f"Skan — {len(WATCH_LIST)} token")
        found = 0

        for symbol in WATCH_LIST:
            try:
                result = await scan_coin(symbol)
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
                            logger.error(f"Yuborishda xato: {e}")
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.error(f"{symbol}: {e}")

        logger.info(f"Skan tugadi — {found} signal")

        if found == 0:
            no_signal_count += 1
            # Har 10 daqiqada (5 skan) signal topilmadi xabari
            if no_signal_count % 5 == 0:
                for chat_id in list(CHAT_IDS):
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"📭 *Signal topilmadi*\n"
                                 f"🕐 {datetime.now().strftime('%H:%M')} | "
                                 f"300 token tekshirildi\n"
                                 f"_Keyingi skan 2 daqiqadan so'ng..._",
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.error(f"No-signal xabar: {e}")
        else:
            no_signal_count = 0
            continue

        new_list = await get_all_symbols()
        if new_list:
            WATCH_LIST = new_list
        CACHE.clear()

        logger.info(f"Skan — {len(WATCH_LIST)} token")
        found = 0

        for symbol in WATCH_LIST:
            try:
                result = await scan_coin(symbol)
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
                            logger.error(f"Yuborishda xato: {e}")
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"{symbol}: {e}")

        logger.info(f"Skan tugadi — {found} signal")

# ============================================================
# MAIN
# ============================================================
async def post_init(app):
    asyncio.create_task(periodic_scan(app.bot))
    asyncio.create_task(track_signals(app.bot))
    logger.info("✅ Bot ishga tushdi! (Bitget)")

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
