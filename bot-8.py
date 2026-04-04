import asyncio
import aiohttp
import logging
import os
import math
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

BOT_TOKEN = os.environ.get("BOT_TOKEN")

CHAT_IDS = set()
WATCH_LIST = []
CACHE = {}
ACTIVE_SIGNALS = {}
PRICE_CACHE = {}  # Real vaqt narx kuzatish
STATS = {
    "total_signals": 0,
    "tp1_hit": 0, "tp2_hit": 0, "tp3_hit": 0, "sl_hit": 0,
    "signals_log": []
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCLAIMER = "\n⚠️ _Moliyaviy maslahat emas._"
CACHE_TTL = 240  # 4 daqiqa

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

async def get_klines(session, symbol, granularity="4h", limit=100):
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

async def fetch_ohlc_data(symbol):
    """4H va 1H parallel olish — cache bilan"""
    now = datetime.now().timestamp()
    if symbol in CACHE and now - CACHE[symbol].get("ts", 0) < CACHE_TTL:
        return CACHE[symbol]

    async with aiohttp.ClientSession() as s:
        tasks = [
            get_klines(s, symbol, "4h", 100),
            get_klines(s, symbol, "1h", 100),
            get_klines(s, symbol, "15min", 60),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    ohlc_4h  = results[0] if not isinstance(results[0], Exception) and results[0] else None
    ohlc_1h  = results[1] if not isinstance(results[1], Exception) and results[1] else None
    ohlc_15m = results[2] if not isinstance(results[2], Exception) and results[2] else None

    if not ohlc_4h:
        return None

    result = {"ohlc_4h": ohlc_4h, "ohlc_1h": ohlc_1h, "ohlc_15m": ohlc_15m, "ts": now}
    CACHE[symbol] = result
    return result

# ============================================================
# ASOSIY TEXNIK TAHLIL
# ============================================================
def calc_ema(prices, period):
    if not prices or len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema

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

def calc_adx(ohlc, period=14):
    """ADX — Trend kuchini o'lchash"""
    if not ohlc or len(ohlc) < period * 2:
        return None
    
    plus_dm_list, minus_dm_list, tr_list = [], [], []
    
    for i in range(1, len(ohlc)):
        h, l = ohlc[i][1], ohlc[i][2]
        ph, pl = ohlc[i-1][1], ohlc[i-1][2]
        pc = ohlc[i-1][3]
        
        plus_dm  = max(h - ph, 0) if (h - ph) > (pl - l) else 0
        minus_dm = max(pl - l, 0) if (pl - l) > (h - ph) else 0
        tr = max(h - l, abs(h - pc), abs(l - pc))
        
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr)
    
    if len(tr_list) < period:
        return None
    
    atr_val = sum(tr_list[:period])
    plus_di = sum(plus_dm_list[:period])
    minus_di = sum(minus_dm_list[:period])
    
    dx_list = []
    for i in range(period, len(tr_list)):
        atr_val = atr_val - atr_val/period + tr_list[i]
        plus_di = plus_di - plus_di/period + plus_dm_list[i]
        minus_di = minus_di - minus_di/period + minus_dm_list[i]
        
        if atr_val == 0:
            continue
        
        pdi = 100 * plus_di / atr_val
        mdi = 100 * minus_di / atr_val
        
        if pdi + mdi == 0:
            continue
        
        dx = 100 * abs(pdi - mdi) / (pdi + mdi)
        dx_list.append(dx)
    
    if not dx_list:
        return None
    
    adx = sum(dx_list[-period:]) / min(period, len(dx_list))
    return round(adx, 1)

def find_swing_highs(ohlc, lookback=5):
    if not ohlc or len(ohlc) < lookback * 2 + 1:
        return []
    highs = []
    for i in range(lookback, len(ohlc) - lookback):
        ch = ohlc[i][1]
        if ch > max(ohlc[j][1] for j in range(i-lookback, i)) and \
           ch > max(ohlc[j][1] for j in range(i+1, i+lookback+1)):
            highs.append(round(ch, 8))
    return sorted(set(highs))

def find_swing_lows(ohlc, lookback=5):
    if not ohlc or len(ohlc) < lookback * 2 + 1:
        return []
    lows = []
    for i in range(lookback, len(ohlc) - lookback):
        cl = ohlc[i][2]
        if cl < min(ohlc[j][2] for j in range(i-lookback, i)) and \
           cl < min(ohlc[j][2] for j in range(i+1, i+lookback+1)):
            lows.append(round(cl, 8))
    return sorted(set(lows))

# ============================================================
# 1. MULTI-TIMEFRAME TREND
# ============================================================
def mtf_trend(ohlc_4h, ohlc_1h):
    """4H va 1H EMA trendlari mosligi"""
    result = {"4h": "sideways", "1h": "sideways", "aligned": False, "direction": None}
    
    for tf, ohlc, key in [("4h", ohlc_4h, "4h"), ("1h", ohlc_1h, "1h")]:
        if not ohlc or len(ohlc) < 50:
            continue
        closes = [c[3] for c in ohlc]
        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)
        price = closes[-1]
        if ema20 and ema50:
            if price > ema20 and ema20 > ema50:
                result[key] = "bullish"
            elif price < ema20 and ema20 < ema50:
                result[key] = "bearish"
    
    if result["4h"] == result["1h"] and result["4h"] != "sideways":
        result["aligned"] = True
        result["direction"] = result["4h"]
    
    return result

# ============================================================
# 2. FIBONACCI OTE (0.618 Oltin Hudud)
# ============================================================
def fibonacci_ote(ohlc, price):
    """
    Narx Fibonacci 0.618 (Optimal Trade Entry) darajasidami?
    Swing Low → Swing High oralig'ida 0.618 daraja
    """
    if not ohlc or len(ohlc) < 20:
        return None, None, None
    
    highs = find_swing_highs(ohlc, lookback=5)
    lows  = find_swing_lows(ohlc, lookback=5)
    
    if not highs or not lows:
        return None, None, None
    
    swing_high = max(highs[-3:]) if len(highs) >= 3 else max(highs)
    swing_low  = min(lows[-3:])  if len(lows) >= 3  else min(lows)
    
    if swing_high <= swing_low:
        return None, None, None
    
    fib_range = swing_high - swing_low
    
    # Fibonacci darajalari
    fib_618 = swing_high - fib_range * 0.618  # Bullish OTE
    fib_786 = swing_high - fib_range * 0.786
    fib_382 = swing_high - fib_range * 0.382
    
    # Narx OTE zonasidami (0.618 - 0.786 orasida)?
    ote_low  = min(fib_618, fib_786)
    ote_high = max(fib_618, fib_786)
    
    tolerance = fib_range * 0.05  # 5% tolerans
    
    if ote_low - tolerance <= price <= ote_high + tolerance:
        return "bullish_ote", fib_618, fib_786
    
    # Bearish OTE — narx pastga tushib keyin tepaga qaytsa
    fib_618_bear = swing_low + fib_range * 0.618
    fib_786_bear = swing_low + fib_range * 0.786
    ote_low_bear  = min(fib_618_bear, fib_786_bear)
    ote_high_bear = max(fib_618_bear, fib_786_bear)
    
    if ote_low_bear - tolerance <= price <= ote_high_bear + tolerance:
        return "bearish_ote", fib_618_bear, fib_786_bear
    
    return None, fib_618, fib_786

# ============================================================
# 3. VOLUME SURGE
# ============================================================
def volume_surge(ohlc, window=20):
    """Hajm birdan oshishi — real vaqtda"""
    if not ohlc or len(ohlc) < window + 2:
        return False, 0, False
    
    # O'tgan shamlar volume o'rtacha
    avg_vol = sum(c[4] for c in ohlc[-(window+2):-2]) / window
    
    # Oldingi sham (yopilgan)
    prev_vol = ohlc[-2][4]
    # Hozirgi sham (yopilmagan)
    curr_vol = ohlc[-1][4]
    
    prev_ratio = round(prev_vol / avg_vol, 1) if avg_vol > 0 else 0
    curr_ratio = round(curr_vol / avg_vol, 1) if avg_vol > 0 else 0
    
    # Volume accumulation — 5 shamda asta oshyaptimi
    recent_vols = [c[4] for c in ohlc[-6:-1]]
    accumulation = all(recent_vols[i] <= recent_vols[i+1] for i in range(len(recent_vols)-1))
    
    surge = prev_ratio >= 2.0 or curr_ratio >= 1.5
    ratio = max(prev_ratio, curr_ratio)
    
    return surge, ratio, accumulation

# ============================================================
# 4. RSI MOMENTUM
# ============================================================
def rsi_momentum(ohlc_4h, ohlc_1h):
    """RSI haddan tashqari qizimagan va barqaror hududda"""
    rsi_4h = calc_rsi([c[3] for c in ohlc_4h]) if ohlc_4h else None
    rsi_1h = calc_rsi([c[3] for c in ohlc_1h]) if ohlc_1h else None
    
    result = {
        "rsi_4h": rsi_4h,
        "rsi_1h": rsi_1h,
        "bullish": False,
        "bearish": False,
        "neutral": False
    }
    
    if rsi_4h:
        if 40 <= rsi_4h <= 60:
            result["neutral"] = True
        elif rsi_4h < 35:
            result["bullish"] = True  # Oversold — potensial ko'tarilish
        elif rsi_4h > 65:
            result["bearish"] = True  # Overbought — potensial tushish
    
    return result

# ============================================================
# 5. ADX STRENGTH
# ============================================================
def adx_strength(ohlc):
    """Trend kuchi ADX bilan"""
    adx = calc_adx(ohlc)
    if not adx:
        return None, "N/A"
    
    if adx >= 40:
        return adx, "Juda kuchli 🔥"
    elif adx >= 25:
        return adx, "Kuchli 💪"
    elif adx >= 20:
        return adx, "O'rta"
    else:
        return adx, "Zaif"

# ============================================================
# KONSOLIDATSIYA BREAKOUT
# ============================================================
def detect_breakout(ohlc):
    """Konsolidatsiyadan chiqish — oldingi sham asosida"""
    if not ohlc or len(ohlc) < 12:
        return None, 0, 0, 0
    
    # Oxirgi yopilgan sham (index -2)
    last_closed = ohlc[-2]
    last_close = last_closed[3]
    
    for lookback in [8, 12, 16, 20]:
        if len(ohlc) < lookback + 2:
            continue
        
        # Konsolidatsiya shamlar — oxirgi yopilgan shamdan oldin
        consol = ohlc[-(lookback+2):-2]
        
        range_high = max(c[1] for c in consol)
        range_low  = min(c[2] for c in consol)
        
        if range_low == 0:
            continue
        
        range_size = (range_high - range_low) / range_low * 100
        
        if range_size > 25:
            continue
        
        if last_close > range_high:
            return "PUMP 🚀", range_size, range_high, lookback
        elif last_close < range_low:
            return "DUMP 💥", range_size, range_low, lookback
    
    return None, 0, 0, 0

def get_sl_tp(ohlc, price, is_long):
    s_highs = find_swing_highs(ohlc)
    s_lows  = find_swing_lows(ohlc)
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
# ASOSIY SKAN
# ============================================================
async def scan_coin(symbol, ticker, min_score=3):
    try:
        price      = float(ticker.get("lastPr", 0) or 0)
        vol_24h    = float(ticker.get("usdtVolume", 0) or 0)
        change_24h = float(ticker.get("change24h", 0) or 0)

        if price == 0 or vol_24h < 50_000:
            return None

        # OHLC olish
        all_data = await fetch_ohlc_data(symbol)
        if not all_data or not all_data["ohlc_4h"]:
            return None

        ohlc_4h  = all_data["ohlc_4h"]
        ohlc_1h  = all_data.get("ohlc_1h") or []
        ohlc_15m = all_data.get("ohlc_15m") or []

        # ============================================================
        # 1. MULTI-TIMEFRAME TREND
        # ============================================================
        mtf = mtf_trend(ohlc_4h, ohlc_1h)

        # ============================================================
        # 2. FIBONACCI OTE
        # ============================================================
        ote_type, fib_618, fib_786 = fibonacci_ote(ohlc_4h, price)

        # ============================================================
        # 3. VOLUME SURGE
        # ============================================================
        vol_spike, vol_ratio, vol_accum = volume_surge(ohlc_4h)

        # ============================================================
        # 4. RSI MOMENTUM
        # ============================================================
        rsi = rsi_momentum(ohlc_4h, ohlc_1h if ohlc_1h else None)

        # ============================================================
        # 5. ADX STRENGTH
        # ============================================================
        adx_val, adx_label = adx_strength(ohlc_4h)

        # ============================================================
        # BREAKOUT
        # ============================================================
        breakout_type, range_size, breakout_level, lookback = detect_breakout(ohlc_4h)

        # ============================================================
        # YO'NALISH ANIQLASH
        # ============================================================
        if mtf["aligned"]:
            is_long = mtf["direction"] == "bullish"
        elif breakout_type:
            is_long = "PUMP" in breakout_type
        elif ote_type:
            is_long = "bullish" in ote_type
        else:
            is_long = change_24h >= 0

        # ============================================================
        # SCORING
        # ============================================================
        score = 0
        factors = []

        # 1. MTF Trend
        if mtf["aligned"]:
            score += 3
            emoji_dir = "📈" if mtf["direction"] == "bullish" else "📉"
            factors.append(f"{emoji_dir}MTF")

        # 2. Fibonacci OTE
        if ote_type:
            score += 3
            factors.append("🌀OTE")

        # 3. Volume Surge
        if vol_spike:
            score += 3 if vol_ratio >= 3 else 2
            factors.append(f"⚡{vol_ratio}x")
        elif vol_accum:
            score += 1
            factors.append("📊AccVol")

        # 4. RSI Momentum
        if is_long and rsi["bullish"]:
            score += 2; factors.append(f"📊RSI{rsi['rsi_4h']}")
        elif not is_long and rsi["bearish"]:
            score += 2; factors.append(f"📊RSI{rsi['rsi_4h']}")
        elif rsi["neutral"]:
            score += 1; factors.append(f"📊RSI{rsi['rsi_4h']}")

        # 5. ADX Strength
        if adx_val and adx_val >= 25:
            score += 2; factors.append(f"ADX{adx_val}")
        elif adx_val and adx_val >= 20:
            score += 1; factors.append(f"ADX{adx_val}")

        # Breakout bonus
        if breakout_type:
            score += 2; factors.append("📦BO")

        # 24h o'zgarish
        if abs(change_24h) >= 5:
            score += 1; factors.append(f"{change_24h:+.0f}%")

        if score < min_score:
            return None

        # SL/TP
        sl, tps = get_sl_tp(ohlc_4h, price, is_long)
        risk = abs(price - sl)
        if risk == 0:
            return None
        sl_pct = (sl - price) / price * 100

        strength = "🔴 KUCHLI" if score >= 9 else "🟡 O'RTA" if score >= 6 else "🟢 ZAIF"
        coin = symbol.replace("USDT", "")
        direction = "LONG 📈" if is_long else "SHORT 📉"
        emoji = "🚀" if is_long else "💥"

        # TP matni
        tp_text = ""
        for i, tp in enumerate(tps[:3], 1):
            pct = (tp - price) / price * 100
            rr  = round(abs(tp - price) / risk, 1)
            tp_text += f"• TP{i}: `${tp:,.6f}` ({pct:+.1f}%) R/R:`1:{rr}`\n"

        # Fibonacci matni
        fib_text = ""
        if ote_type and fib_618:
            fib_text = f"🌀 OTE: `${fib_618:,.6f}` — `${fib_786:,.6f}`\n"

        # Breakout matni
        bo_text = ""
        if breakout_type:
            bo_text = f"📦 {breakout_type} `{lookback}` sham | `{range_size:.1f}%`\n"

        # MTF matni
        mtf_text = f"{'📈' if mtf['4h']=='bullish' else '📉' if mtf['4h']=='bearish' else '⬌'}4H | {'📈' if mtf['1h']=='bullish' else '📉' if mtf['1h']=='bearish' else '⬌'}1H"

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

        msg = (
            f"{emoji} *#{sid} — {coin}/USDT*\n"
            f"{strength} | {direction}\n"
            f"{' | '.join(factors)}\n\n"
            f"{bo_text}"
            f"{fib_text}"
            f"📊 {mtf_text}\n"
            f"📈 RSI 4H: `{rsi['rsi_4h'] or 'N/A'}` | 1H: `{rsi['rsi_1h'] or 'N/A'}`\n"
            f"💪 ADX: `{adx_val or 'N/A'}` ({adx_label})\n"
            f"⚡ Vol: `{vol_ratio}x` {'📈accumulation' if vol_accum else ''}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🎯 *Trade Setup*\n"
            f"• Entry: `${price:,.6f}`\n"
            f"• SL: `${sl:,.6f}` ({sl_pct:+.1f}%)\n"
            f"{tp_text}"
            f"━━━━━━━━━━━━━━━\n"
            f"24h: `{change_24h:+.2f}%` | Vol: `${vol_24h/1e6:.1f}M`\n"
            f"🎯 Score: `{score}/16`\n"
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
        ohlc_4h = await get_klines(s, symbol, "4h", 100)
        ohlc_1h = await get_klines(s, symbol, "1h", 100)

    coin = symbol.replace("USDT", "")
    price = float(ticker.get("lastPr", 0) or 0)
    change_24h = float(ticker.get("change24h", 0) or 0)
    high_24h = float(ticker.get("high24h", 0) or 0)
    low_24h = float(ticker.get("low24h", 0) or 0)
    vol_24h = float(ticker.get("usdtVolume", 0) or 0)

    mtf = mtf_trend(ohlc_4h, ohlc_1h) if ohlc_4h else {}
    rsi = rsi_momentum(ohlc_4h, ohlc_1h) if ohlc_4h else {}
    adx_val, adx_label = adx_strength(ohlc_4h) if ohlc_4h else (None, "N/A")
    ote_type, fib_618, _ = fibonacci_ote(ohlc_4h, price) if ohlc_4h else (None, None, None)
    _, vol_ratio, vol_accum = volume_surge(ohlc_4h) if ohlc_4h else (False, 0, False)

    c24 = "🟢" if change_24h >= 0 else "🔴"

    msg = (
        f"📊 *{coin}/USDT* (Bitget)\n\n"
        f"💰 Narx: `${price:,.6f}`\n"
        f"{c24} 24h: `{change_24h:+.2f}%`\n"
        f"📈 High: `${high_24h:,.6f}` | 📉 Low: `${low_24h:,.6f}`\n"
        f"📊 Hajm: `${vol_24h/1e6:.1f}M`\n\n"
        f"{'📈' if mtf.get('4h')=='bullish' else '📉' if mtf.get('4h')=='bearish' else '⬌'} 4H: `{mtf.get('4h','N/A')}`\n"
        f"{'📈' if mtf.get('1h')=='bullish' else '📉' if mtf.get('1h')=='bearish' else '⬌'} 1H: `{mtf.get('1h','N/A')}`\n"
        f"📊 RSI 4H: `{rsi.get('rsi_4h','N/A')}` | 1H: `{rsi.get('rsi_1h','N/A')}`\n"
        f"💪 ADX: `{adx_val or 'N/A'}` ({adx_label})\n"
        f"⚡ Vol: `{vol_ratio}x` {'📈 Accumulation!' if vol_accum else ''}\n"
        f"🌀 OTE: `{'Ha' if ote_type else 'Yoq'}`"
        f"{DISCLAIMER}"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yangilash", callback_data=f"info_{symbol}"),
        InlineKeyboardButton("🔍 Signal?", callback_data=f"chk_{symbol}"),
    ], [
        InlineKeyboardButton("🔙 Orqaga", callback_data="back_main"),
    ]])

    return msg, keyboard

# ============================================================
# HISOBOT
# ============================================================
def generate_report():
    total = STATS["total_signals"]
    if total == 0:
        return "📊 *Hisobot*\n\nHali signal berilmagan." + DISCLAIMER

    tp_t = STATS["tp1_hit"] + STATS["tp2_hit"] + STATS["tp3_hit"]
    sl_c = STATS["sl_hit"]
    ochiq = total - tp_t - sl_c
    wr = round(tp_t / (tp_t + sl_c) * 100, 1) if (tp_t + sl_c) > 0 else 0

    # PNL
    total_pnl = 0.0
    for s in STATS["signals_log"]:
        entry = s["entry"]
        if not entry: continue
        is_long = s["is_long"]
        status = s["status"]
        tp_key = None
        if "TP3" in status: tp_key = "tp3"
        elif "TP2" in status: tp_key = "tp2"
        elif "TP1" in status: tp_key = "tp1"
        elif "SL" in status: tp_key = "sl"
        if tp_key and s.get(tp_key):
            pct = (s[tp_key] - entry) / entry * 100
            total_pnl += pct if is_long else -pct

    pnl_e = "✅" if total_pnl >= 0 else "❌"

    text = (
        f"📊 *Signal Hisoboti* (Bitget)\n\n"
        f"Jami: `{total}` | ✅TP: `{tp_t}` | ❌SL: `{sl_c}` | 🔄: `{ochiq}`\n"
        f"Win rate: `{wr}%`\n"
        f"{pnl_e} Jami PNL: `{total_pnl:+.2f}%`\n\n"
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
        "🔍 600+ token kuzatiladi\n\n"
        "*Signal omillari:*\n"
        "1️⃣ Multi-TF Trend (4H + 1H EMA)\n"
        "2️⃣ Fibonacci OTE (0.618 Oltin hudud)\n"
        "3️⃣ Volume Surge (hajm oshishi)\n"
        "4️⃣ RSI Momentum\n"
        "5️⃣ ADX Trend kuchi\n"
        "📦 Konsolidatsiya Breakout\n\n"
        "⏱ Har 2 daqiqada skan\n"
        "🤖 TP/SL avtomatik | PNL hisobot\n\n"
        "👇 Tugma bosing yoki coin yozing:\n"
        "`BTC` `ETH` `SOL` `PEPE` ...",
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
        await msg_obj.edit_text("❌ Xato.", reply_markup=main_keyboard())

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
            result = await scan_coin(symbol, ticker, min_score=2)
            if result:
                await query.edit_message_text(result["msg"], parse_mode="Markdown", reply_markup=result["keyboard"])
                return
        msg, keyboard = await get_coin_info(symbol)
        if msg:
            await query.edit_message_text("📭 Hozircha signal yoq.\n\n" + msg, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "watch_on":
        CHAT_IDS.add(query.from_user.id)
        await query.edit_message_text(
            "✅ *Signallar yoqildi!*\n"
            "⏱ Har 2 daqiqada 600+ token\n"
            "🔴 Faqat kuchli signallar (score 6+)",
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
        scan_list = sorted(list(tickers.keys()),
            key=lambda s: float(tickers.get(s, {}).get("usdtVolume", 0) or 0), reverse=True)
        found = 0
        for symbol in scan_list:
            result = await scan_coin(symbol, tickers[symbol], min_score=2)
            if result:
                found += 1
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=result["msg"], parse_mode="Markdown",
                    reply_markup=result["keyboard"]
                )
            await asyncio.sleep(0.2)
        txt = f"✅ {found} signal topildi!" if found else "📭 Signal topilmadi."
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

        new_tickers = await get_all_tickers()
        if new_tickers:
            tickers = new_tickers
            WATCH_LIST = list(tickers.keys())
        CACHE.clear()

        scan_count += 1
        sorted_symbols = sorted(WATCH_LIST,
            key=lambda s: float(tickers.get(s, {}).get("usdtVolume", 0) or 0),
            reverse=True)

        logger.info(f"Skan #{scan_count} — {len(sorted_symbols)} token")
        found = 0

        for symbol in sorted_symbols:
            try:
                # Avtomatik — faqat kuchli (score 6+)
                result = await scan_coin(symbol, tickers.get(symbol, {}), min_score=6)
                if result:
                    found += 1
                    for chat_id in list(CHAT_IDS):
                        try:
                            await bot.send_message(
                                chat_id=chat_id, text=result["msg"],
                                parse_mode="Markdown", reply_markup=result["keyboard"]
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
    logger.info("✅ Bot ishga tushdi! (Bitget Multi-Factor)")

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
