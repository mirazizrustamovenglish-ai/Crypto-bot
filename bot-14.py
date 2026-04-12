import asyncio
import aiohttp
import logging
import os
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
# ============================================================

CHAT_IDS = set()
WATCH_LIST = []
CACHE = {}
ACTIVE_SIGNALS = {}
ALERTED = set()

STATS = {
    "total": 0, "tp1": 0, "tp2": 0, "tp3": 0, "sl": 0,
    "log": []
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCLAIMER = "\n⚠️ _Moliyaviy maslahat emas._"
BITGET = "https://api.bitget.com"
CG = "https://api.coingecko.com/api/v3"

# CoinGecko ID map
CG_MAP = {
    "BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana",
    "BNBUSDT": "binancecoin", "XRPUSDT": "ripple", "ADAUSDT": "cardano",
    "DOGEUSDT": "dogecoin", "AVAXUSDT": "avalanche-2", "DOTUSDT": "polkadot",
    "LINKUSDT": "chainlink", "NEARUSDT": "near", "ATOMUSDT": "cosmos",
    "APTUSDT": "aptos", "SUIUSDT": "sui", "ARBUSDT": "arbitrum",
    "OPUSDT": "optimism", "INJUSDT": "injective-protocol",
    "PEPEUSDT": "pepe", "SHIBUSDT": "shiba-inu",
}

# ============================================================
# API
# ============================================================
async def bg(session, path, params=None):
    """Bitget GET"""
    try:
        async with session.get(f"{BITGET}{path}", params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                d = await r.json()
                if d.get("code") == "00000":
                    return d.get("data")
            elif r.status == 429:
                await asyncio.sleep(5)
    except Exception as e:
        logger.error(f"Bitget {path}: {e}")
    return None

async def cg(session, path, params=None):
    """CoinGecko GET"""
    try:
        async with session.get(f"{CG}{path}", params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        logger.error(f"CG {path}: {e}")
    return None

async def get_tickers():
    async with aiohttp.ClientSession() as s:
        data = await bg(s, "/api/v2/spot/market/tickers")
        if data:
            return {d["symbol"]: d for d in data if d.get("symbol", "").endswith("USDT")}
    return {}

async def get_klines(session, symbol, gran="4h", limit=100):
    data = await bg(session, "/api/v2/spot/market/candles", {
        "symbol": symbol, "granularity": gran, "limit": str(limit)
    })
    if data:
        r = []
        for c in data:
            try:
                r.append([float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])])
            except:
                pass
        return r if len(r) >= 10 else None
    return None

async def get_price(symbol):
    async with aiohttp.ClientSession() as s:
        d = await bg(s, "/api/v2/spot/market/tickers", {"symbol": symbol})
        if d:
            t = d[0] if isinstance(d, list) else d
            return float(t.get("lastPr", 0) or 0)
    return None

async def get_cg_info(symbol):
    """CoinGecko dan fundamental ma'lumot"""
    cg_id = CG_MAP.get(symbol)
    if not cg_id:
        return None
    async with aiohttp.ClientSession() as s:
        data = await cg(s, f"/coins/{cg_id}", {
            "localization": "false", "tickers": "false",
            "market_data": "true", "community_data": "false",
            "developer_data": "false"
        })
    return data

async def get_news(symbol):
    """CryptoPanic yangiliklari"""
    coin = symbol.replace("USDT", "")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://cryptopanic.com/api/v1/posts/", params={
                "auth_token": "free", "currencies": coin,
                "filter": "important", "public": "true"
            }, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    d = await r.json()
                    return [f"• {i['title'][:65]} ({i.get('published_at','')[:10]})"
                            for i in d.get("results", [])[:3]]
    except:
        pass
    return []

async def fetch_all(symbol):
    """Barcha ma'lumotlarni parallel olish"""
    now = datetime.now().timestamp()
    if symbol in CACHE and now - CACHE[symbol].get("ts", 0) < 180:
        return CACHE[symbol]

    async with aiohttp.ClientSession() as s:
        r4h, r1h = await asyncio.gather(
            get_klines(s, symbol, "4h", 100),
            get_klines(s, symbol, "1h", 100),
            return_exceptions=True
        )

    ohlc_4h = r4h if not isinstance(r4h, Exception) else None
    ohlc_1h = r1h if not isinstance(r1h, Exception) else None

    if not ohlc_4h:
        return None

    result = {"ohlc_4h": ohlc_4h, "ohlc_1h": ohlc_1h, "ts": now}
    CACHE[symbol] = result
    return result

# ============================================================
# TEXNIK TAHLIL
# ============================================================
def ema(prices, n):
    if not prices or len(prices) < n:
        return None
    k = 2 / (n + 1)
    e = sum(prices[:n]) / n
    for p in prices[n:]:
        e = p * k + e * (1 - k)
    return e

def rsi(prices, n=14):
    if not prices or len(prices) < n + 1:
        return None
    g, l = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        g.append(max(d, 0)); l.append(max(-d, 0))
    ag = sum(g[-n:]) / n; al = sum(l[-n:]) / n
    return round(100 - 100 / (1 + ag / al), 1) if al else 100.0

def macd(prices):
    if not prices or len(prices) < 26:
        return None, None, None
    e12 = ema(prices, 12)
    e26 = ema(prices, 26)
    if not e12 or not e26:
        return None, None, None
    macd_line = e12 - e26
    # Signal line (9-period EMA of MACD)
    macd_vals = []
    for i in range(26, len(prices)):
        e12i = ema(prices[i-11:i+1], 12)
        e26i = ema(prices[i-25:i+1], 26)
        if e12i and e26i:
            macd_vals.append(e12i - e26i)
    signal_line = ema(macd_vals, 9) if len(macd_vals) >= 9 else None
    hist = round(macd_line - signal_line, 6) if signal_line else None
    return round(macd_line, 6), round(signal_line, 6) if signal_line else None, hist

def adx(ohlc, n=14):
    if not ohlc or len(ohlc) < n * 2:
        return None
    pdm_l, mdm_l, tr_l = [], [], []
    for i in range(1, len(ohlc)):
        h, l, ph, pl, pc = ohlc[i][1], ohlc[i][2], ohlc[i-1][1], ohlc[i-1][2], ohlc[i-1][3]
        pdm_l.append(max(h - ph, 0) if (h - ph) > (pl - l) else 0)
        mdm_l.append(max(pl - l, 0) if (pl - l) > (h - ph) else 0)
        tr_l.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(tr_l) < n:
        return None
    av = sum(tr_l[:n]); pd = sum(pdm_l[:n]); md = sum(mdm_l[:n])
    dx_l = []
    for i in range(n, len(tr_l)):
        av = av - av/n + tr_l[i]
        pd = pd - pd/n + pdm_l[i]
        md = md - md/n + mdm_l[i]
        if av == 0: continue
        pi = 100 * pd / av; mi = 100 * md / av
        if pi + mi == 0: continue
        dx_l.append(100 * abs(pi - mi) / (pi + mi))
    return round(sum(dx_l[-n:]) / min(n, len(dx_l)), 1) if dx_l else None

def atr(ohlc, n=14):
    if not ohlc or len(ohlc) < n + 1:
        return None
    trs = [max(ohlc[i][1]-ohlc[i][2], abs(ohlc[i][1]-ohlc[i-1][3]), abs(ohlc[i][2]-ohlc[i-1][3]))
           for i in range(1, len(ohlc))]
    return sum(trs[-n:]) / n if trs else None

def swing_highs(ohlc, lb=5):
    if not ohlc or len(ohlc) < lb*2+1: return []
    return sorted(set(round(ohlc[i][1], 8) for i in range(lb, len(ohlc)-lb)
                      if ohlc[i][1] > max(ohlc[j][1] for j in range(i-lb, i)) and
                         ohlc[i][1] > max(ohlc[j][1] for j in range(i+1, i+lb+1))))

def swing_lows(ohlc, lb=5):
    if not ohlc or len(ohlc) < lb*2+1: return []
    return sorted(set(round(ohlc[i][2], 8) for i in range(lb, len(ohlc)-lb)
                      if ohlc[i][2] < min(ohlc[j][2] for j in range(i-lb, i)) and
                         ohlc[i][2] < min(ohlc[j][2] for j in range(i+1, i+lb+1))))

def trend(ohlc):
    if not ohlc or len(ohlc) < 50: return "sideways"
    closes = [c[3] for c in ohlc]
    e20 = ema(closes, 20); e50 = ema(closes, 50)
    p = closes[-1]
    if e20 and e50:
        if p > e20 and e20 > e50: return "bullish"
        if p < e20 and e20 < e50: return "bearish"
    return "sideways"

def support_resistance(ohlc, price):
    sh = swing_highs(ohlc)
    sl = swing_lows(ohlc)
    sup = max([l for l in sl if l < price], default=None)
    res = min([h for h in sh if h > price], default=None)
    return sup, res

def fib_ote(ohlc, price):
    sh = swing_highs(ohlc); sl = swing_lows(ohlc)
    if not sh or not sl: return None
    hi = max(sh[-3:]) if len(sh) >= 3 else max(sh)
    lo = min(sl[-3:]) if len(sl) >= 3 else min(sl)
    if hi <= lo: return None
    rng = hi - lo
    f618 = hi - rng * 0.618; f786 = hi - rng * 0.786
    tol = rng * 0.05
    if min(f618, f786) - tol <= price <= max(f618, f786) + tol:
        return f"Bullish OTE ${f618:,.4f}-${f786:,.4f}"
    f618b = lo + rng * 0.618; f786b = lo + rng * 0.786
    if min(f618b, f786b) - tol <= price <= max(f618b, f786b) + tol:
        return f"Bearish OTE ${f618b:,.4f}-${f786b:,.4f}"
    return None

def smart_sl_tp(ohlc, price, is_long):
    """ATR asosida SL, swing asosida TP, min R/R 1:2"""
    a = atr(ohlc) or price * 0.02
    sh = swing_highs(ohlc); sl_list = swing_lows(ohlc)

    if is_long:
        lows = [l for l in sl_list if l < price * 0.999]
        sl_price = max(lows) * 0.997 if lows else price - a * 2
        sl_price = min(sl_price, price - a * 1.5)  # Kamida 1.5 ATR pastda
        risk = price - sl_price
        if risk <= 0: return None, []
        tps_raw = sorted([h for h in sh if h > price * 1.005])[:5]
        tps = [t for t in tps_raw if (t - price) / risk >= 2.0][:3]
        if len(tps) < 3:
            for m in [2.0, 3.5, 5.5]:
                t = round(price + a * m, 8)
                if (t - price) / risk >= 2.0 and t not in tps:
                    tps.append(t)
                if len(tps) >= 3: break
        tps = sorted(tps)[:3]
    else:
        highs = [h for h in sh if h > price * 1.001]
        sl_price = min(highs) * 1.003 if highs else price + a * 2
        sl_price = max(sl_price, price + a * 1.5)
        risk = sl_price - price
        if risk <= 0: return None, []
        tps_raw = sorted([l for l in sl_list if l < price * 0.995], reverse=True)[:5]
        tps = [t for t in tps_raw if (price - t) / risk >= 2.0][:3]
        if len(tps) < 3:
            for m in [2.0, 3.5, 5.5]:
                t = round(price - a * m, 8)
                if (price - t) / risk >= 2.0 and t not in tps:
                    tps.append(t)
                if len(tps) >= 3: break
        tps = sorted(tps, reverse=True)[:3]

    return round(sl_price, 8), [round(t, 8) for t in tps]

# ============================================================
# SIGNAL SCORING
# ============================================================
def score_signal(ohlc_4h, ohlc_1h, price, change_24h, vol_ratio):
    score = 0
    factors = []

    # Trend MTF
    t4 = trend(ohlc_4h); t1 = trend(ohlc_1h) if ohlc_1h else "sideways"
    if t4 == t1 and t4 != "sideways":
        score += 3
        factors.append(f"{'📈' if t4=='bullish' else '📉'}MTF")

    # Volume
    if vol_ratio >= 2.5: score += 3; factors.append(f"⚡{vol_ratio}x")
    elif vol_ratio >= 1.8: score += 2; factors.append(f"⚡{vol_ratio}x")

    # RSI
    rsi_val = rsi([c[3] for c in ohlc_4h])
    if rsi_val:
        if rsi_val < 30: score += 2; factors.append(f"📊RSI{rsi_val}↑")
        elif rsi_val > 70: score += 2; factors.append(f"📊RSI{rsi_val}↓")
        elif 40 <= rsi_val <= 60: score += 1; factors.append(f"📊RSI{rsi_val}")

    # MACD
    m_line, m_sig, m_hist = macd([c[3] for c in ohlc_4h])
    if m_hist:
        if m_hist > 0: score += 1; factors.append("MACD↑")
        elif m_hist < 0: score += 1; factors.append("MACD↓")

    # ADX
    adx_val = adx(ohlc_4h)
    if adx_val and adx_val >= 25: score += 2; factors.append(f"ADX{adx_val}")
    elif adx_val and adx_val >= 20: score += 1; factors.append(f"ADX{adx_val}")

    # OTE
    ote = fib_ote(ohlc_4h, price)
    if ote: score += 2; factors.append("🌀OTE")

    # 24h o'zgarish
    if abs(change_24h) >= 5: score += 1; factors.append(f"{change_24h:+.0f}%")

    # Yo'nalish
    is_long = t4 == "bullish" or (t4 == "sideways" and change_24h >= 0)
    if t4 == "bearish": is_long = False

    return score, factors, is_long, rsi_val, adx_val, m_line, m_sig, m_hist, ote

# ============================================================
# ASOSIY SKAN
# ============================================================
async def scan_coin(symbol, ticker, min_score=3):
    try:
        price      = float(ticker.get("lastPr", 0) or 0)
        vol_24h    = float(ticker.get("usdtVolume", 0) or 0)
        change_24h = float(ticker.get("change24h", 0) or 0)
        high_24h   = float(ticker.get("high24h", 0) or 0)
        low_24h    = float(ticker.get("low24h", 0) or 0)

        if price == 0 or vol_24h < 50_000:
            return None

        data = await fetch_all(symbol)
        if not data:
            return None

        ohlc_4h = data["ohlc_4h"]
        ohlc_1h = data.get("ohlc_1h")

        # Volume ratio
        vols = [c[4] for c in ohlc_4h[-21:]]
        avg_vol = sum(vols[:-1]) / 20 if len(vols) >= 20 else 0
        vol_ratio = round(ohlc_4h[-1][4] / avg_vol, 1) if avg_vol > 0 else 0

        score, factors, is_long, rsi_val, adx_val, m_line, m_sig, m_hist, ote = \
            score_signal(ohlc_4h, ohlc_1h, price, change_24h, vol_ratio)

        if score < min_score:
            return None

        # Support / Resistance
        sup, res = support_resistance(ohlc_4h, price)

        # SL / TP
        sl_price, tps = smart_sl_tp(ohlc_4h, price, is_long)
        if sl_price is None or not tps:
            return None

        risk = abs(price - sl_price)
        if risk == 0:
            return None

        # Min R/R 1:2
        if abs(tps[0] - price) / risk < 2.0:
            return None

        sl_pct = (sl_price - price) / price * 100
        strength = "🔴 KUCHLI" if score >= 10 else "🟡 O'RTA" if score >= 6 else "🟢 ZAIF"
        coin = symbol.replace("USDT", "")
        direction = "LONG 📈" if is_long else "SHORT 📉"
        emoji = "🚀" if is_long else "💥"
        t4_e = "📈" if trend(ohlc_4h)=="bullish" else "📉" if trend(ohlc_4h)=="bearish" else "⬌"
        t1_e = "📈" if (ohlc_1h and trend(ohlc_1h)=="bullish") else "📉" if (ohlc_1h and trend(ohlc_1h)=="bearish") else "⬌"

        # TP matni
        tp_text = ""
        for i, tp in enumerate(tps[:3], 1):
            pct = (tp - price) / price * 100
            rr  = round(abs(tp - price) / risk, 1)
            tp_text += f"• TP{i}: `${tp:,.6f}` ({pct:+.1f}%) R/R`1:{rr}`\n"

        # Signal saqlash
        STATS["total"] += 1
        sid = STATS["total"]
        STATS["log"].append({
            "id": sid, "coin": coin, "coin_id": symbol,
            "entry": price, "sl": sl_price, "is_long": is_long,
            "tp1": tps[0] if tps else None,
            "tp2": tps[1] if len(tps) > 1 else None,
            "tp3": tps[2] if len(tps) > 2 else None,
            "direction": "LONG" if is_long else "SHORT",
            "score": score, "status": "🔄 Ochiq",
            "time": datetime.now().strftime("%H:%M %d.%m"),
        })

        ACTIVE_SIGNALS[sid] = {
            "coin_id": symbol, "symbol": coin, "entry": price,
            "sl": sl_price, "is_long": is_long,
            "tp1": tps[0] if tps else None,
            "tp2": tps[1] if len(tps) > 1 else None,
            "tp3": tps[2] if len(tps) > 2 else None,
            "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
            "sl_hit": False, "chat_ids": set(CHAT_IDS), "active": True,
        }

        ALERTED.add(symbol)
        asyncio.get_event_loop().call_later(3600, lambda: ALERTED.discard(symbol))

        msg = (
            f"{emoji} *#{sid} {coin}/USDT — {strength}*\n"
            f"{direction} | Score:`{score}`\n"
            f"{' | '.join(factors)}\n\n"
            f"💰 `${price:,.6f}` | 24h:`{change_24h:+.2f}%`\n"
            f"📊 {t4_e}4H {t1_e}1H | RSI:`{rsi_val or 'N/A'}` ADX:`{adx_val or 'N/A'}`\n"
            f"MACD:`{m_line or 'N/A'}` | Vol:`{vol_ratio}x`\n"
            f"🛡 Sup:`${sup:,.4f}` Res:`${res:,.4f}`\n"
            f"{('🌀 OTE: ' + ote + chr(10)) if ote else ''}"
            f"\n━━━━━━━━━━━━━━━\n"
            f"🎯 *Trade Setup*\n"
            f"• Entry: `${price:,.6f}`\n"
            f"• SL: `${sl_price:,.6f}` ({sl_pct:+.1f}%)\n"
            f"{tp_text}"
            f"━━━━━━━━━━━━━━━\n"
            f"Vol:`${vol_24h/1e6:.1f}M` H:`${high_24h:,.4f}` L:`${low_24h:,.4f}`\n"
            f"🤖 _Avtomatik kuzatiladi_"
            f"{DISCLAIMER}"
        )

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅TP1 #{sid}", callback_data=f"tp1_{sid}"),
            InlineKeyboardButton(f"✅TP2 #{sid}", callback_data=f"tp2_{sid}"),
            InlineKeyboardButton(f"❌SL #{sid}", callback_data=f"sl_{sid}"),
        ]])

        return {"msg": msg, "kb": kb, "coin": coin, "sid": sid}

    except Exception as e:
        logger.error(f"scan {symbol}: {e}")
        return None

# ============================================================
# COIN INFO — TO'LIQ TAHLIL
# ============================================================
async def coin_info(symbol):
    async with aiohttp.ClientSession() as s:
        td = await bg(s, "/api/v2/spot/market/tickers", {"symbol": symbol})
        if not td:
            return None, None
        ticker = td[0] if isinstance(td, list) else td
        o4h = await get_klines(s, symbol, "4h", 100)
        o1h = await get_klines(s, symbol, "1h", 60)

    coin = symbol.replace("USDT", "")
    price = float(ticker.get("lastPr", 0) or 0)
    c24   = float(ticker.get("change24h", 0) or 0)
    hi    = float(ticker.get("high24h", 0) or 0)
    lo    = float(ticker.get("low24h", 0) or 0)
    vol   = float(ticker.get("usdtVolume", 0) or 0)

    if not o4h:
        return None, None

    closes = [c[3] for c in o4h]
    rsi_v  = rsi(closes)
    adx_v  = adx(o4h)
    ml, ms, mh = macd(closes)
    t4     = trend(o4h)
    t1     = trend(o1h) if o1h else "N/A"
    sup, res = support_resistance(o4h, price)
    ote    = fib_ote(o4h, price)
    atr_v  = atr(o4h)

    vols   = [c[4] for c in o4h[-21:]]
    avg_v  = sum(vols[:-1]) / 20 if len(vols) >= 20 else 0
    vr     = round(o4h[-1][4] / avg_v, 1) if avg_v > 0 else 0

    # Fundamental (CoinGecko)
    cg_data = await get_cg_info(symbol)
    fund_text = ""
    if cg_data:
        md = cg_data.get("market_data", {})
        mcap = md.get("market_cap", {}).get("usd", 0)
        ath  = md.get("ath", {}).get("usd", 0)
        ath_c = md.get("ath_change_percentage", {}).get("usd", 0) or 0
        circ = md.get("circulating_supply", 0)
        total_s = md.get("total_supply", 0)
        fund_text = (
            f"\n━━━━━━━━━━━━━━━\n"
            f"📦 *Tokenomics*\n"
            f"Market Cap: `${mcap/1e9:.2f}B`\n"
            f"ATH: `${ath:,.2f}` ({ath_c:.1f}%)\n"
            f"Muomala: `{circ:,.0f}` | Jami: `{total_s:,.0f}`\n"
        )

    # Yangiliklar
    news = await get_news(symbol)
    news_text = ""
    if news:
        news_text = "\n━━━━━━━━━━━━━━━\n📰 *Yangiliklar*\n" + "\n".join(news)

    ce = "🟢" if c24 >= 0 else "🔴"
    t4e = "📈" if t4=="bullish" else "📉" if t4=="bearish" else "⬌"
    t1e = "📈" if t1=="bullish" else "📉" if t1=="bearish" else "⬌"
    macd_e = "↑" if mh and mh > 0 else "↓" if mh and mh < 0 else "-"

    msg = (
        f"📊 *{coin}/USDT* (Bitget)\n\n"
        f"💰 `${price:,.6f}`\n"
        f"{ce} 24h:`{c24:+.2f}%` | H:`${hi:,.4f}` L:`${lo:,.4f}`\n"
        f"Vol:`${vol/1e6:.1f}M` ({vr}x) | ATR:`{atr_v:.4f}`\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 *Texnik Tahlil*\n"
        f"{t4e}4H Trend:`{t4}` | {t1e}1H:`{t1}`\n"
        f"RSI 4H:`{rsi_v or 'N/A'}` | ADX:`{adx_v or 'N/A'}`\n"
        f"MACD:`{ml or 'N/A'}` ({macd_e})\n"
        f"🛡 Support:`${sup:,.4f}` Resistance:`${res:,.4f}`\n"
        f"{('🌀 OTE: ' + ote + chr(10)) if ote else ''}"
        f"{fund_text}"
        f"{news_text}"
        f"{DISCLAIMER}"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Yangilash", callback_data=f"info_{symbol}"),
        InlineKeyboardButton("🔍 Signal?", callback_data=f"chk_{symbol}"),
    ], [
        InlineKeyboardButton("🔙 Orqaga", callback_data="back"),
    ]])

    return msg, kb

# ============================================================
# HISOBOT
# ============================================================
def report():
    total = STATS["total"]
    if total == 0:
        return "📊 *Hisobot*\n\nHali signal berilmagan." + DISCLAIMER

    tp_t = STATS["tp1"] + STATS["tp2"] + STATS["tp3"]
    sl_c = STATS["sl"]
    ochiq = total - tp_t - sl_c
    wr = round(tp_t / (tp_t + sl_c) * 100, 1) if (tp_t + sl_c) > 0 else 0

    pnl = 0.0
    for s in STATS["log"]:
        e = s["entry"]
        if not e: continue
        st = s["status"]
        key = "tp3" if "TP3" in st else "tp2" if "TP2" in st else "tp1" if "TP1" in st else "sl" if "SL" in st else None
        if key and s.get(key):
            p = (s[key] - e) / e * 100
            pnl += p if s["is_long"] else -p

    pe = "✅" if pnl >= 0 else "❌"
    txt = (
        f"📊 *Hisobot*\n\n"
        f"Jami:`{total}` ✅:`{tp_t}` ❌:`{sl_c}` 🔄:`{ochiq}`\n"
        f"Win rate:`{wr}%` | {pe} PNL:`{pnl:+.2f}%`\n\n"
        f"*So'nggi 5:*\n"
    )
    for s in reversed(STATS["log"][-5:]):
        txt += f"{s['status']} #{s['id']} *{s['coin']}* {s['direction']} `${s['entry']:,.4f}` {s['time']}\n"
    txt += DISCLAIMER
    return txt

def mark(sid, st):
    for s in STATS["log"]:
        if s["id"] == sid:
            s["status"] = f"{'✅' if 'TP' in st else '❌'} {st}"
            if st == "TP1": STATS["tp1"] += 1
            elif st == "TP2": STATS["tp2"] += 1
            elif st == "TP3": STATS["tp3"] += 1
            elif st == "SL": STATS["sl"] += 1

# ============================================================
# KLAVIATURA
# ============================================================
def mkb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("₿ BTC", callback_data="info_BTCUSDT"),
            InlineKeyboardButton("Ξ ETH", callback_data="info_ETHUSDT"),
            InlineKeyboardButton("◎ SOL", callback_data="info_SOLUSDT"),
        ],
        [
            InlineKeyboardButton("🔔 Yoqish", callback_data="watch_on"),
            InlineKeyboardButton("🔕 Ochirish", callback_data="watch_off"),
        ],
        [
            InlineKeyboardButton("📊 Hisobot", callback_data="hisobot"),
            InlineKeyboardButton("🔍 Skan", callback_data="scan_now"),
        ],
    ])

# ============================================================
# HANDLERS
# ============================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text(
        "👋 *Rustamov Crypto Bot*\n\n"
        "🏦 Bitget | 600+ token\n"
        "📊 RSI · MACD · ADX · MTF · OTE\n"
        "🛡 Support/Resistance · Volume\n"
        "📰 Yangiliklar · Tokenomics\n"
        "✅ Min R/R 1:2 | ATR SL\n\n"
        "Coin nomi yozing: `BTC` `ETH` `SOL`",
        parse_mode="Markdown", reply_markup=mkb()
    )

async def cmd_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    symbol = text if text.endswith("USDT") else text + "USDT"
    m = await update.message.reply_text("⏳ Tahlil qilinmoqda...")
    try:
        msg, kb = await coin_info(symbol)
        if msg:
            await m.edit_text(msg, parse_mode="Markdown", reply_markup=kb)
        else:
            await m.edit_text(f"❌ '{text}' topilmadi.", reply_markup=mkb())
    except Exception as e:
        logger.error(f"text: {e}")
        await m.edit_text("❌ Xato.", reply_markup=mkb())

async def cmd_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    if d.startswith("info_"):
        sym = d[5:]
        await q.edit_message_text("⏳...", parse_mode="Markdown")
        msg, kb = await coin_info(sym)
        if msg: await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
        else: await q.edit_message_text("❌ Topilmadi.", reply_markup=mkb())

    elif d.startswith("chk_"):
        sym = d[4:]
        await q.edit_message_text("⏳ Signal tekshirilmoqda...")
        async with aiohttp.ClientSession() as s:
            td = await bg(s, "/api/v2/spot/market/tickers", {"symbol": sym})
        if td:
            t = td[0] if isinstance(td, list) else td
            r = await scan_coin(sym, t, min_score=2)
            if r:
                await q.edit_message_text(r["msg"], parse_mode="Markdown", reply_markup=r["kb"])
                return
        msg, kb = await coin_info(sym)
        if msg: await q.edit_message_text("📭 Signal yoq.\n\n" + msg, parse_mode="Markdown", reply_markup=kb)

    elif d == "watch_on":
        CHAT_IDS.add(q.from_user.id)
        await q.edit_message_text("✅ *Signallar yoqildi!*\nHar 2 daqiqada skan. Score 6+ signallar keladi.",
                                   parse_mode="Markdown", reply_markup=mkb())

    elif d == "watch_off":
        CHAT_IDS.discard(q.from_user.id)
        await q.edit_message_text("🔕 *Ochirildi.*", parse_mode="Markdown", reply_markup=mkb())

    elif d == "hisobot":
        await q.edit_message_text(report(), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="back")]]))

    elif d == "scan_now":
        await q.edit_message_text("⏳ 600+ token tekshirilmoqda...")
        tickers = await get_tickers()
        lst = sorted(tickers.keys(), key=lambda s: float(tickers[s].get("usdtVolume", 0) or 0), reverse=True)
        found = 0
        for sym in lst:
            r = await scan_coin(sym, tickers[sym], min_score=2)
            if r:
                found += 1
                await ctx.bot.send_message(q.message.chat_id, r["msg"],
                                           parse_mode="Markdown", reply_markup=r["kb"])
            await asyncio.sleep(0.2)
        await ctx.bot.send_message(q.message.chat_id,
            f"✅ {found} signal" if found else "📭 Signal topilmadi.", reply_markup=mkb())

    elif d == "back":
        await q.edit_message_text("👋 *Rustamov Crypto Bot*\n\n👇",
                                   parse_mode="Markdown", reply_markup=mkb())

    elif any(d.startswith(x) for x in ["tp1_", "tp2_", "tp3_", "sl_"]):
        p = d.split("_"); st = p[0].upper(); sid = int(p[1])
        mark(sid, st)
        await q.answer(f"{'✅' if 'TP' in st else '❌'} #{sid} {st}", show_alert=True)

# ============================================================
# TP/SL KUZATISH
# ============================================================
async def track(bot):
    while True:
        await asyncio.sleep(60)
        if not ACTIVE_SIGNALS: continue
        done = []
        for sid, sig in list(ACTIVE_SIGNALS.items()):
            if not sig["active"]: done.append(sid); continue
            try:
                p = await get_price(sig["coin_id"])
                if not p: continue
                il = sig["is_long"]; coin = sig["symbol"]; msg = None

                if not sig["tp1_hit"] and sig["tp1"]:
                    if (il and p >= sig["tp1"]) or (not il and p <= sig["tp1"]):
                        sig["tp1_hit"] = True; STATS["tp1"] += 1; mark(sid, "TP1")
                        pct = abs(sig["tp1"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"✅ *#{sid} {coin} — TP1!* +`{pct:.1f}%`{DISCLAIMER}"

                elif sig["tp1_hit"] and not sig["tp2_hit"] and sig["tp2"]:
                    if (il and p >= sig["tp2"]) or (not il and p <= sig["tp2"]):
                        sig["tp2_hit"] = True; STATS["tp2"] += 1; mark(sid, "TP2")
                        pct = abs(sig["tp2"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"✅✅ *#{sid} {coin} — TP2!* +`{pct:.1f}%`{DISCLAIMER}"

                elif sig["tp2_hit"] and not sig["tp3_hit"] and sig["tp3"]:
                    if (il and p >= sig["tp3"]) or (not il and p <= sig["tp3"]):
                        sig["tp3_hit"] = True; sig["active"] = False
                        STATS["tp3"] += 1; mark(sid, "TP3")
                        pct = abs(sig["tp3"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"✅✅✅ *#{sid} {coin} — TP3 MUKAMMAL!* +`{pct:.1f}%`{DISCLAIMER}"

                if not sig["sl_hit"] and not sig.get("tp3_hit"):
                    if (il and p <= sig["sl"]) or (not il and p >= sig["sl"]):
                        sig["sl_hit"] = True; sig["active"] = False
                        STATS["sl"] += 1; mark(sid, "SL")
                        pct = abs(sig["sl"] - sig["entry"]) / sig["entry"] * 100
                        msg = f"❌ *#{sid} {coin} — SL* -{pct:.1f}%{DISCLAIMER}"

                if msg:
                    for cid in sig["chat_ids"]:
                        try: await bot.send_message(cid, msg, parse_mode="Markdown")
                        except: pass
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.error(f"track {sid}: {e}")
        for sid in done: ACTIVE_SIGNALS.pop(sid, None)

# ============================================================
# HAR 2 DAQIQALIK SKAN
# ============================================================
async def scanner(bot):
    global WATCH_LIST
    logger.info("Yuklanmoqda...")
    tickers = await get_tickers()
    WATCH_LIST = list(tickers.keys())
    logger.info(f"{len(WATCH_LIST)} token")

    count = 0; no_sig = 0

    while True:
        await asyncio.sleep(120)
        if not CHAT_IDS: continue

        new_t = await get_tickers()
        if new_t: tickers = new_t; WATCH_LIST = list(tickers.keys())
        CACHE.clear()

        count += 1
        lst = sorted(WATCH_LIST, key=lambda s: float(tickers.get(s, {}).get("usdtVolume", 0) or 0), reverse=True)
        logger.info(f"Skan #{count} — {len(lst)} token")
        found = 0

        for sym in lst:
            try:
                if sym in ALERTED: continue
                r = await scan_coin(sym, tickers.get(sym, {}), min_score=6)
                if r:
                    found += 1
                    for cid in list(CHAT_IDS):
                        try: await bot.send_message(cid, r["msg"], parse_mode="Markdown", reply_markup=r["kb"])
                        except: pass
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.error(f"{sym}: {e}")

        logger.info(f"Skan #{count} — {found} signal")
        if found == 0:
            no_sig += 1
            if no_sig % 5 == 0:
                for cid in list(CHAT_IDS):
                    try:
                        await bot.send_message(cid,
                            f"📭 *Signal topilmadi*\n🕐 {datetime.now().strftime('%H:%M')} | {len(lst)} token",
                            parse_mode="Markdown")
                    except: pass
        else:
            no_sig = 0

# ============================================================
# MAIN
# ============================================================
async def post_init(app):
    loop = asyncio.get_event_loop()
    loop.create_task(scanner(app.bot))
    loop.create_task(track(app.bot))
    logger.info("✅ Bot ishga tushdi!")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cmd_btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_text))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
