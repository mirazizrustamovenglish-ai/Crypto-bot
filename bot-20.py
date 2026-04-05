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
ALERTED = set()  # Takroriy signal oldini olish

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
# API
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
                result.append([float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
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

async def fetch_ohlc(symbol):
    now = datetime.now().timestamp()
    if symbol in CACHE and now - CACHE[symbol].get("ts", 0) < CACHE_TTL:
        return CACHE[symbol]

    async with aiohttp.ClientSession() as s:
        tasks = [
            get_klines(s, symbol, "4h", 100),
            get_klines(s, symbol, "1h", 100),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    ohlc_4h = results[0] if not isinstance(results[0], Exception) and results[0] else None
    ohlc_1h = results[1] if not isinstance(results[1], Exception) and results[1] else None

    if not ohlc_4h:
        return None

    result = {"ohlc_4h": ohlc_4h, "ohlc_1h": ohlc_1h, "ts": now}
    CACHE[symbol] = result
    return result

# ============================================================
# TEXNIK TAHLIL
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
    if not ohlc or len(ohlc) < period * 2:
        return None
    plus_dm_list, minus_dm_list, tr_list = [], [], []
    for i in range(1, len(ohlc)):
        h, l = ohlc[i][1], ohlc[i][2]
        ph, pl, pc = ohlc[i-1][1], ohlc[i-1][2], ohlc[i-1][3]
        plus_dm  = max(h - ph, 0) if (h - ph) > (pl - l) else 0
        minus_dm = max(pl - l, 0) if (pl - l) > (h - ph) else 0
        tr = max(h - l, abs(h - pc), abs(l - pc))
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr_list.append(tr)

    if len(tr_list) < period:
        return None

    atr_v = sum(tr_list[:period])
    pdm   = sum(plus_dm_list[:period])
    mdm   = sum(minus_dm_list[:period])
    dx_list = []
    for i in range(period, len(tr_list)):
        atr_v = atr_v - atr_v/period + tr_list[i]
        pdm   = pdm   - pdm/period   + plus_dm_list[i]
        mdm   = mdm   - mdm/period   + minus_dm_list[i]
        if atr_v == 0:
            continue
        pdi = 100 * pdm / atr_v
        mdi = 100 * mdm / atr_v
        if pdi + mdi == 0:
            continue
        dx_list.append(100 * abs(pdi - mdi) / (pdi + mdi))
    if not dx_list:
        return None
    return round(sum(dx_list[-period:]) / min(period, len(dx_list)), 1)

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

def mtf_trend(ohlc_4h, ohlc_1h):
    result = {"4h": "sideways", "1h": "sideways", "aligned": False, "direction": None}
    for tf_key, ohlc in [("4h", ohlc_4h), ("1h", ohlc_1h)]:
        if not ohlc or len(ohlc) < 50:
            continue
        closes = [c[3] for c in ohlc]
        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)
        price = closes[-1]
        if ema20 and ema50:
            if price > ema20 and ema20 > ema50:
                result[tf_key] = "bullish"
            elif price < ema20 and ema20 < ema50:
                result[tf_key] = "bearish"
    if result["4h"] == result["1h"] and result["4h"] != "sideways":
        result["aligned"] = True
        result["direction"] = result["4h"]
    return result

def fibonacci_ote(ohlc, price):
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
    fib_618 = swing_high - fib_range * 0.618
    fib_786 = swing_high - fib_range * 0.786
    ote_low  = min(fib_618, fib_786)
    ote_high = max(fib_618, fib_786)
    tol = fib_range * 0.05
    if ote_low - tol <= price <= ote_high + tol:
        return "bullish_ote", fib_618, fib_786
    fib_618b = swing_low + fib_range * 0.618
    fib_786b = swing_low + fib_range * 0.786
    ote_low_b  = min(fib_618b, fib_786b)
    ote_high_b = max(fib_618b, fib_786b)
    if ote_low_b - tol <= price <= ote_high_b + tol:
        return "bearish_ote", fib_618b, fib_786b
    return None, fib_618, fib_786

# ============================================================
# PUMP/DUMP OLDINDAN SEZISH
# ============================================================
def detect_early_pump(ohlc, price, ticker):
    """
    Pump bo'lishidan OLDIN xabar berish:
    1. Narx konsolidatsiya yuqori chegarasiga yaqin (95%+)
    2. Volume asta oshmoqda (accumulation)
    3. RSI 40-60 oralig'ida (haddan tashqari emas)
    """
    if not ohlc or len(ohlc) < 20:
        return None, 0

    # Konsolidatsiya diapazoni
    consol = ohlc[-20:-1]
    range_high = max(c[1] for c in consol)
    range_low  = min(c[2] for c in consol)

    if range_low == 0:
        return None, 0

    range_size = (range_high - range_low) / range_low * 100

    # Diapazon 20% dan kichik bo'lsin
    if range_size > 20:
        return None, 0

    # Narx diapazon ichida qayerda?
    position = (price - range_low) / (range_high - range_low) if (range_high - range_low) > 0 else 0

    # Volume accumulation
    vols = [c[4] for c in ohlc[-10:]]
    avg_vol = sum(vols[:5]) / 5 if len(vols) >= 5 else 0
    recent_avg = sum(vols[5:]) / 5 if len(vols) >= 5 else 0
    vol_growing = recent_avg > avg_vol * 1.2 if avg_vol > 0 else False

    rsi = calc_rsi([c[3] for c in ohlc])

    # EARLY PUMP: narx yuqori chegaraga yaqin + volume oshmoqda
    if position >= 0.85 and vol_growing and rsi and 40 <= rsi <= 65:
        proximity = round(position * 100, 1)
        return "EARLY_PUMP 🔔", proximity

    # EARLY DUMP: narx quyi chegaraga yaqin + volume oshmoqda
    if position <= 0.15 and vol_growing and rsi and 35 <= rsi <= 60:
        proximity = round((1 - position) * 100, 1)
        return "EARLY_DUMP 🔔", proximity

    return None, 0

# ============================================================
# YAXSHILANGAN SL/TP — ATR asosida
# ============================================================
def smart_sl_tp(ohlc, price, is_long, atr_multiplier_sl=1.5, min_rr=2.0):
    """
    ATR asosida dinamik SL/TP
    - SL: ATR * 1.5 (swing low/high ham tekshiriladi)
    - TP: kamida 1:2 R/R
    - TP1, TP2, TP3 swing darajalarda
    """
    atr = calc_atr(ohlc)
    if not atr:
        atr = price * 0.02

    s_highs = find_swing_highs(ohlc)
    s_lows  = find_swing_lows(ohlc)

    if is_long:
        # SL — ATR asosida, swing low bilan taqqoslash
        atr_sl = price - atr * atr_multiplier_sl
        swing_lows_below = [l for l in s_lows if l < price * 0.998]
        swing_sl = max(swing_lows_below) * 0.997 if swing_lows_below else None

        # Ikkinchi usuldan pastrog'ini ol (kengrog SL)
        if swing_sl and swing_sl < atr_sl:
            sl = swing_sl
        else:
            sl = atr_sl

        risk = price - sl
        if risk <= 0:
            return None, []

        # TP — swing highs dan, lekin kamida 1:2 R/R
        tps_raw = sorted([h for h in s_highs if h > price * 1.005])[:3]

        tps = []
        for tp in tps_raw:
            rr = (tp - price) / risk
            if rr >= min_rr:  # Kamida 1:2
                tps.append(round(tp, 8))

        # Agar yetarli TP yo'q bo'lsa — ATR asosida qo'shamiz
        if len(tps) < 3:
            for mult in [2.0, 3.5, 5.5]:
                tp_atr = round(price + atr * mult, 8)
                rr = (tp_atr - price) / risk
                if rr >= min_rr and tp_atr not in tps:
                    tps.append(tp_atr)
                if len(tps) >= 3:
                    break

        tps = sorted(tps)[:3]

    else:
        # SHORT
        atr_sl = price + atr * atr_multiplier_sl
        swing_highs_above = [h for h in s_highs if h > price * 1.002]
        swing_sl = min(swing_highs_above) * 1.003 if swing_highs_above else None

        if swing_sl and swing_sl > atr_sl:
            sl = swing_sl
        else:
            sl = atr_sl

        risk = sl - price
        if risk <= 0:
            return None, []

        tps_raw = sorted([l for l in s_lows if l < price * 0.995], reverse=True)[:3]

        tps = []
        for tp in tps_raw:
            rr = (price - tp) / risk
            if rr >= min_rr:
                tps.append(round(tp, 8))

        if len(tps) < 3:
            for mult in [2.0, 3.5, 5.5]:
                tp_atr = round(price - atr * mult, 8)
                rr = (price - tp_atr) / risk
                if rr >= min_rr and tp_atr not in tps:
                    tps.append(tp_atr)
                if len(tps) >= 3:
                    break

        tps = sorted(tps, reverse=True)[:3]

    return round(sl, 8), tps

# ============================================================
# ASOSIY SKAN
# ============================================================
async def scan_coin(symbol, ticker, min_score=3, check_early=True):
    try:
        price      = float(ticker.get("lastPr", 0) or 0)
        vol_24h    = float(ticker.get("usdtVolume", 0) or 0)
        change_24h = float(ticker.get("change24h", 0) or 0)

        if price == 0 or vol_24h < 50_000:
            return None

        all_data = await fetch_ohlc(symbol)
        if not all_data or not all_data["ohlc_4h"]:
            return None

        ohlc_4h = all_data["ohlc_4h"]
        ohlc_1h = all_data.get("ohlc_1h") or []

        closes_4h = [c[3] for c in ohlc_4h]
        rsi_4h  = calc_rsi(closes_4h)
        rsi_1h  = calc_rsi([c[3] for c in ohlc_1h]) if ohlc_1h else None
        adx_val = calc_adx(ohlc_4h)
        mtf     = mtf_trend(ohlc_4h, ohlc_1h)
        ote_type, fib_618, fib_786 = fibonacci_ote(ohlc_4h, price)

        # Volume surge
        vols    = [c[4] for c in ohlc_4h[-21:]]
        avg_vol = sum(vols[:-1]) / 20 if len(vols) >= 20 else 0
        last_vol = ohlc_4h[-1][4]
        vol_ratio = round(last_vol / avg_vol, 1) if avg_vol > 0 else 0
        vol_accum_list = [c[4] for c in ohlc_4h[-6:]]
        vol_accum = all(vol_accum_list[i] <= vol_accum_list[i+1]
                        for i in range(len(vol_accum_list)-1)) if len(vol_accum_list) >= 3 else False

        # Breakout (oldingi yopilgan sham)
        breakout_type = None
        range_size_bo = 0
        breakout_level = 0
        lookback_bo = 0
        last_close_prev = ohlc_4h[-2][3] if len(ohlc_4h) >= 2 else price

        for lb in [8, 12, 16, 20]:
            if len(ohlc_4h) < lb + 2:
                continue
            consol = ohlc_4h[-(lb+2):-2]
            rh = max(c[1] for c in consol)
            rl = min(c[2] for c in consol)
            if rl == 0:
                continue
            rs = (rh - rl) / rl * 100
            if rs > 25:
                continue
            if last_close_prev > rh:
                breakout_type = "PUMP 🚀"; range_size_bo = rs
                breakout_level = rh; lookback_bo = lb; break
            elif last_close_prev < rl:
                breakout_type = "DUMP 💥"; range_size_bo = rs
                breakout_level = rl; lookback_bo = lb; break

        # EARLY PUMP/DUMP
        early_type, early_prox = None, 0
        if check_early:
            early_type, early_prox = detect_early_pump(ohlc_4h, price, ticker)

        # Yo'nalish
        if mtf["aligned"]:
            is_long = mtf["direction"] == "bullish"
        elif breakout_type:
            is_long = "PUMP" in breakout_type
        elif ote_type:
            is_long = "bullish" in ote_type
        elif early_type:
            is_long = "PUMP" in early_type
        else:
            is_long = change_24h >= 0

        # SCORING
        score = 0
        factors = []

        if mtf["aligned"]:
            score += 3
            e = "📈" if mtf["direction"] == "bullish" else "📉"
            factors.append(f"{e}MTF")

        if ote_type:
            score += 3; factors.append("🌀OTE")

        if vol_ratio >= 2.5:
            score += 3; factors.append(f"⚡{vol_ratio}x")
        elif vol_ratio >= 1.8:
            score += 2; factors.append(f"⚡{vol_ratio}x")
        elif vol_accum:
            score += 1; factors.append("📊AccVol")

        if rsi_4h:
            if is_long and rsi_4h < 35:
                score += 2; factors.append(f"📊RSI{rsi_4h}↑")
            elif not is_long and rsi_4h > 65:
                score += 2; factors.append(f"📊RSI{rsi_4h}↓")
            elif 40 <= rsi_4h <= 60:
                score += 1; factors.append(f"📊RSI{rsi_4h}")

        if adx_val and adx_val >= 25:
            score += 2; factors.append(f"ADX{adx_val}")
        elif adx_val and adx_val >= 20:
            score += 1; factors.append(f"ADX{adx_val}")

        if breakout_type:
            score += 2; factors.append("📦BO")

        if early_type:
            score += 2; factors.append("🔔Early")

        if abs(change_24h) >= 5:
            score += 1; factors.append(f"{change_24h:+.0f}%")

        if score < min_score:
            return None

        # SL/TP — ATR asosida, min 1:2 R/R
        sl, tps = smart_sl_tp(ohlc_4h, price, is_long, min_rr=2.0)
        if sl is None or not tps:
            return None

        risk = abs(price - sl)
        sl_pct = (sl - price) / price * 100

        # R/R tekshirish — kamida 1:2 bo'lsin
        best_rr = abs(tps[0] - price) / risk if risk > 0 else 0
        if best_rr < 2.0:
            return None  # Yomon R/R — signal berilmasin

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

        # Early signal matni
        early_text = f"🔔 *Early signal:* Narx diapazonga `{early_prox}%` yaqin\n" if early_type else ""
        bo_text = f"📦 {breakout_type} `{lookback_bo}` sham | `{range_size_bo:.1f}%`\n" if breakout_type else ""
        fib_text = f"🌀 OTE: `${fib_618:,.6f}`\n" if ote_type and fib_618 else ""
        mtf_text = f"{'📈' if mtf['4h']=='bullish' else '📉' if mtf['4h']=='bearish' else '⬌'}4H {'📈' if mtf['1h']=='bullish' else '📉' if mtf['1h']=='bearish' else '⬌'}1H"

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

        # Takroriy signal oldini olish
        ALERTED.add(symbol)
        asyncio.get_event_loop().call_later(3600, lambda: ALERTED.discard(symbol))

        msg = (
            f"{emoji} *#{sid} — {coin}/USDT*\n"
            f"{strength} | {direction} | Score:`{score}/16`\n"
            f"{' | '.join(factors)}\n\n"
            f"{early_text}"
            f"{bo_text}"
            f"{fib_text}"
            f"📊 {mtf_text}\n"
            f"RSI 4H:`{rsi_4h or 'N/A'}` 1H:`{rsi_1h or 'N/A'}`\n"
            f"ADX:`{adx_val or 'N/A'}` Vol:`{vol_ratio}x`\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🎯 *Trade Setup*\n"
            f"• Entry: `${price:,.6f}`\n"
            f"• SL: `${sl:,.6f}` ({sl_pct:+.1f}%) [ATR]\n"
            f"{tp_text}"
            f"━━━━━━━━━━━━━━━\n"
            f"24h:`{change_24h:+.2f}%` Vol:`${vol_24h/1e6:.1f}M`\n"
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
    rsi_4h = calc_rsi([c[3] for c in ohlc_4h]) if ohlc_4h else None
    rsi_1h = calc_rsi([c[3] for c in ohlc_1h]) if ohlc_1h else None
    adx_val = calc_adx(ohlc_4h) if ohlc_4h else None
    ote_type, fib_618, _ = fibonacci_ote(ohlc_4h, price) if ohlc_4h else (None, None, None)
    early_type, early_prox = detect_early_pump(ohlc_4h, price, ticker) if ohlc_4h else (None, 0)

    vols = [c[4] for c in ohlc_4h[-21:]] if ohlc_4h else []
    avg_vol = sum(vols[:-1]) / 20 if len(vols) >= 20 else 0
    last_vol = ohlc_4h[-1][4] if ohlc_4h else 0
    vol_ratio = round(last_vol / avg_vol, 1) if avg_vol > 0 else 0

    c24 = "🟢" if change_24h >= 0 else "🔴"
    t4 = "📈" if mtf.get("4h") == "bullish" else "📉" if mtf.get("4h") == "bearish" else "⬌"
    t1 = "📈" if mtf.get("1h") == "bullish" else "📉" if mtf.get("1h") == "bearish" else "⬌"

    early_text = f"\n🔔 *Early signal:* Narx chegaraga `{early_prox}%` yaqin!" if early_type else ""

    msg = (
        f"📊 *{coin}/USDT* (Bitget)\n\n"
        f"💰 `${price:,.6f}`\n"
        f"{c24} 24h:`{change_24h:+.2f}%` | H:`${high_24h:,.4f}` L:`${low_24h:,.4f}`\n"
        f"Vol:`${vol_24h/1e6:.1f}M` | {vol_ratio}x\n\n"
        f"{t4}4H:`{mtf.get('4h','N/A')}` | {t1}1H:`{mtf.get('1h','N/A')}`\n"
        f"RSI 4H:`{rsi_4h or 'N/A'}` 1H:`{rsi_1h or 'N/A'}`\n"
        f"ADX:`{adx_val or 'N/A'}` | OTE:`{'Ha ✅' if ote_type else 'Yoq'}`"
        f"{early_text}"
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

    total_pnl = 0.0
    for s in STATS["signals_log"]:
        entry = s["entry"]
        if not entry:
            continue
        is_long = s["is_long"]
        status = s["status"]
        tp_key = "tp3" if "TP3" in status else "tp2" if "TP2" in status else "tp1" if "TP1" in status else "sl" if "SL" in status else None
        if tp_key and s.get(tp_key):
            pct = (s[tp_key] - entry) / entry * 100
            total_pnl += pct if is_long else -pct

    pnl_e = "✅" if total_pnl >= 0 else "❌"
    text = (
        f"📊 *Signal Hisoboti*\n\n"
        f"Jami:`{total}` | ✅TP:`{tp_t}` | ❌SL:`{sl_c}` | 🔄:`{ochiq}`\n"
        f"Win rate:`{wr}%` | {pnl_e} PNL:`{total_pnl:+.2f}%`\n\n"
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
        "🏦 Bitget Spot | 600+ token\n\n"
        "*5 omil asosida signal:*\n"
        "1️⃣ MTF Trend (4H+1H EMA)\n"
        "2️⃣ Fibonacci OTE (0.618)\n"
        "3️⃣ Volume Surge\n"
        "4️⃣ RSI Momentum\n"
        "5️⃣ ADX Trend kuchi\n"
        "📦 Breakout + 🔔 Early signal\n\n"
        "✅ Min R/R: 1:2\n"
        "🤖 TP/SL avtomatik | PNL hisobot\n\n"
        "👇 Coin nomi yozing: `BTC` `ETH` `SOL`",
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
            await msg_obj.edit_text(f"❌ *'{text}'* topilmadi.", parse_mode="Markdown", reply_markup=main_keyboard())
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
            result = await scan_coin(symbol, ticker, min_score=2, check_early=True)
            if result:
                await query.edit_message_text(result["msg"], parse_mode="Markdown", reply_markup=result["keyboard"])
                return
        msg, keyboard = await get_coin_info(symbol)
        if msg:
            await query.edit_message_text("📭 Signal yoq.\n\n" + msg, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "watch_on":
        CHAT_IDS.add(query.from_user.id)
        await query.edit_message_text(
            "✅ *Signallar yoqildi!*\n"
            "⏱ Har 2 daqiqada 600+ token\n"
            "🔴 Score 6+ | Min R/R 1:2",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

    elif data == "watch_off":
        CHAT_IDS.discard(query.from_user.id)
        await query.edit_message_text("🔕 *Signallar ochirildi.*", parse_mode="Markdown", reply_markup=main_keyboard())

    elif data == "hisobot":
        await query.edit_message_text(generate_report(), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="back_main")]]))

    elif data == "scan_now":
        await query.edit_message_text("⏳ 600+ token tekshirilmoqda...")
        tickers = await get_all_tickers()
        scan_list = sorted(list(tickers.keys()),
            key=lambda s: float(tickers.get(s, {}).get("usdtVolume", 0) or 0), reverse=True)
        found = 0
        for symbol in scan_list:
            result = await scan_coin(symbol, tickers[symbol], min_score=2, check_early=True)
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
            "👋 *Rustamov Crypto Bot*\n\n👇 Tugmalardan foydalaning:",
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
# AVTOMATIK TP/SL
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
                        sig["tp1_hit"] = True; STATS["tp1_hit"] += 1; _mark(sid, "TP1")
                        pct = abs(sig["tp1"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"✅ *#{sid} {coin} — TP1!* `${price:,.6f}` +`{pct:.1f}%`{DISCLAIMER}"

                elif sig["tp1_hit"] and not sig["tp2_hit"] and sig["tp2"]:
                    if (is_long and price >= sig["tp2"]) or (not is_long and price <= sig["tp2"]):
                        sig["tp2_hit"] = True; STATS["tp2_hit"] += 1; _mark(sid, "TP2")
                        pct = abs(sig["tp2"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"✅✅ *#{sid} {coin} — TP2!* `${price:,.6f}` +`{pct:.1f}%`{DISCLAIMER}"

                elif sig["tp2_hit"] and not sig["tp3_hit"] and sig["tp3"]:
                    if (is_long and price >= sig["tp3"]) or (not is_long and price <= sig["tp3"]):
                        sig["tp3_hit"] = True; sig["active"] = False
                        STATS["tp3_hit"] += 1; _mark(sid, "TP3")
                        pct = abs(sig["tp3"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"✅✅✅ *#{sid} {coin} — TP3 MUKAMMAL!* +`{pct:.1f}%`{DISCLAIMER}"

                if not sig["sl_hit"] and not sig.get("tp3_hit"):
                    if (is_long and price <= sig["sl"]) or (not is_long and price >= sig["sl"]):
                        sig["sl_hit"] = True; sig["active"] = False
                        STATS["sl_hit"] += 1; _mark(sid, "SL")
                        pct = abs(sig["sl"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"❌ *#{sid} {coin} — SL* `${price:,.6f}` -`{pct:.1f}%`{DISCLAIMER}"

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
    logger.info("Bitget yuklanmoqda...")
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
                if symbol in ALERTED:
                    continue
                result = await scan_coin(symbol, tickers.get(symbol, {}),
                                         min_score=6, check_early=True)
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

        logger.info(f"Skan #{scan_count} — {found} signal")

        if found == 0:
            no_signal_count += 1
            if no_signal_count % 5 == 0:
                for chat_id in list(CHAT_IDS):
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"📭 *Signal topilmadi*\n🕐 {datetime.now().strftime('%H:%M')} | {len(sorted_symbols)} token",
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
