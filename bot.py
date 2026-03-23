import asyncio
import aiohttp
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# TOKENINGIZNI SHU YERGA YOZING
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
# ============================================================

WATCH_LIST = ["bitcoin", "ethereum", "solana"]
CHAT_IDS = set()

# Hisobot statistikasi
STATS = {
    "total_signals": 0,
    "tp1_hit": 0,
    "tp2_hit": 0,
    "tp3_hit": 0,
    "sl_hit": 0,
    "signals_log": []
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCLAIMER = "\n\n⚠️ _Bu tahlil faqat ma'lumot uchun. Moliyaviy maslahat emas. Investitsiya qarorini o'zingiz qabul qiling._"

# ============================================================
# API FUNKSIYALAR
# ============================================================
async def get_coin_data(coin_id: str):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
    params = {"localization": "false", "tickers": "false", "market_data": "true",
               "community_data": "false", "developer_data": "true"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
    except Exception as e:
        logger.error(f"get_coin_data: {e}")
    return None

async def get_ohlc_data(coin_id: str, days: int = 30):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
    params = {"vs_currency": "usd", "days": str(days)}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
    except Exception as e:
        logger.error(f"get_ohlc_data: {e}")
    return None

async def get_news(symbol: str):
    url = "https://cryptopanic.com/api/v1/posts/"
    params = {"auth_token": "free", "currencies": symbol.upper(),
               "filter": "important", "public": "true"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    data = await r.json()
                    results = data.get("results", [])
                    return [f"• {item.get('title','')[:80]} _({item.get('published_at','')[:10]})_"
                            for item in results[:3]]
    except Exception as e:
        logger.error(f"get_news: {e}")
    return []

# ============================================================
# TEXNIK TAHLIL
# ============================================================
def calculate_rsi(prices: list, period: int = 14):
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

def calculate_atr(ohlc: list, period: int = 14):
    if not ohlc or len(ohlc) < period + 1:
        return None
    trs = []
    for i in range(1, len(ohlc)):
        h, l, pc = ohlc[i][2], ohlc[i][3], ohlc[i-1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period

def find_swing_highs(ohlc: list, lookback: int = 3):
    """Swing high nuqtalar — TP darajalari"""
    swing_highs = []
    for i in range(lookback, len(ohlc) - lookback):
        ch = ohlc[i][2]
        if ch > max(ohlc[j][2] for j in range(i-lookback, i)) and \
           ch > max(ohlc[j][2] for j in range(i+1, i+lookback+1)):
            swing_highs.append(round(ch, 4))
    return sorted(set(swing_highs))

def find_swing_lows(ohlc: list, lookback: int = 3):
    """Swing low nuqtalar — SL daraja"""
    swing_lows = []
    for i in range(lookback, len(ohlc) - lookback):
        cl = ohlc[i][3]
        if cl < min(ohlc[j][3] for j in range(i-lookback, i)) and \
           cl < min(ohlc[j][3] for j in range(i+1, i+lookback+1)):
            swing_lows.append(round(cl, 4))
    return sorted(set(swing_lows))

def find_sl_tp(ohlc: list, price: float, trend: str):
    """
    Swing asosida SL va TP:
    Bullish → SL: eng yaqin swing low (pastda), TP: yuqoridagi swing highlar
    Bearish → SL: eng yaqin swing high (yuqorida), TP: pastdagi swing lowlar
    """
    swing_highs = find_swing_highs(ohlc)
    swing_lows = find_swing_lows(ohlc)
    sl = None
    tps = []

    if "Bullish" in trend or "Yuqori" in trend:
        lows_below = [l for l in swing_lows if l < price * 0.999]
        sl = max(lows_below) if lows_below else (min(swing_lows) if swing_lows else round(price * 0.95, 4))
        tps = sorted([h for h in swing_highs if h > price * 1.005])[:3]
    else:
        highs_above = [h for h in swing_highs if h > price * 1.001]
        sl = min(highs_above) if highs_above else (max(swing_highs) if swing_highs else round(price * 1.05, 4))
        tps = sorted([l for l in swing_lows if l < price * 0.995], reverse=True)[:3]

    # Fallback — ATR asosida
    if not tps:
        atr = calculate_atr(ohlc)
        if atr:
            tps = [round(price + atr * 1.5, 4),
                   round(price + atr * 3.0, 4),
                   round(price + atr * 5.0, 4)]
    return sl, tps

def detect_trend(closes: list):
    if len(closes) < 6:
        return "Noma'lum"
    r = closes[-6:]
    if r[-1] > r[0] * 1.02:
        return "📈 Yuqoriga (Bullish)"
    elif r[-1] < r[0] * 0.98:
        return "📉 Pastga (Bearish)"
    return "↔️ Yon (Sideways)"

def detect_bos(closes: list):
    if len(closes) < 10:
        return None
    ph, pl = max(closes[-10:-5]), min(closes[-10:-5])
    curr = closes[-1]
    if curr > ph:
        return "🔼 BOS Bullish — yuqori struktura buzildi"
    elif curr < pl:
        return "🔽 BOS Bearish — quyi struktura buzildi"
    return None

def detect_fvg(ohlc: list):
    if not ohlc or len(ohlc) < 3:
        return None
    for i in range(len(ohlc) - 3, 0, -1):
        c1h, c1l = ohlc[i][2], ohlc[i][3]
        c3h, c3l = ohlc[i+2][2], ohlc[i+2][3]
        if c3l > c1h:
            return f"📊 FVG Bullish: ${c1h:,.4f} — ${c3l:,.4f}"
        elif c3h < c1l:
            return f"📊 FVG Bearish: ${c3h:,.4f} — ${c1l:,.4f}"
    return None

def check_volume_spike(vol: float, avg: float):
    if avg <= 0:
        return False, 0
    ratio = vol / avg
    return ratio >= 2.0, round(ratio, 2)

def calculate_fundamental_score(data: dict):
    score = 0
    details = []
    md = data.get("market_data", {})

    # Market cap (0-3)
    mcap = md.get("market_cap", {}).get("usd", 0)
    if mcap > 100_000_000_000:
        score += 3; details.append("✅ Market cap: Katta ($100B+)")
    elif mcap > 10_000_000_000:
        score += 2; details.append("✅ Market cap: O'rta ($10B+)")
    elif mcap > 1_000_000_000:
        score += 1; details.append("⚠️ Market cap: Kichik ($1B+)")
    else:
        details.append("❌ Market cap: Micro-cap")

    # Likvidlik (0-2)
    vol = md.get("total_volume", {}).get("usd", 0)
    if mcap > 0:
        ratio = vol / mcap
        if ratio > 0.1:
            score += 2; details.append("✅ Likvidlik: Yuqori")
        elif ratio > 0.03:
            score += 1; details.append("⚠️ Likvidlik: O'rta")
        else:
            details.append("❌ Likvidlik: Past")

    # Supply foizi (0-2)
    circ = md.get("circulating_supply", 0)
    total = md.get("total_supply", 1)
    if total:
        pct = circ / total
        if pct > 0.7:
            score += 2; details.append("✅ Supply: Ko'p muomalada (70%+)")
        elif pct > 0.4:
            score += 1; details.append("⚠️ Supply: O'rta muomalada")
        else:
            details.append("❌ Supply: Inflyatsiya xavfi")

    # ATH dan farq (0-2)
    ath_chg = md.get("ath_change_percentage", {}).get("usd", 0) or 0
    if ath_chg < -70:
        score += 2; details.append("✅ ATH dan uzoq — yuqori potensial")
    elif ath_chg < -30:
        score += 1; details.append("⚠️ ATH dan biroz uzoq")
    else:
        details.append("❌ ATH ga yaqin — cheklangan o'sish")

    # Developer (0-1)
    commits = data.get("developer_data", {}).get("commit_count_4_weeks", 0)
    if commits and commits > 10:
        score += 1; details.append("✅ Developer: Faol")
    else:
        details.append("⚠️ Developer: Past faollik")

    return round((score / 10) * 10, 1), details

def signal_strength(signals: dict):
    count = 0
    if signals.get("volume_spike"): count += 2
    if signals.get("rsi_extreme"): count += 2
    if signals.get("bos"): count += 2
    if signals.get("fvg"): count += 1
    if signals.get("near_support"): count += 1
    if count >= 6: return "🔴 KUCHLI SIGNAL", count
    if count >= 3: return "🟡 O'RTA SIGNAL", count
    if count >= 1: return "🟢 ZAIF SIGNAL", count
    return None, count

# ============================================================
# ASOSIY TAHLIL
# ============================================================
async def analyze_coin(coin_id: str):
    data = await get_coin_data(coin_id)
    if not data:
        return None, None

    ohlc = await get_ohlc_data(coin_id)
    md = data.get("market_data", {})

    name = data.get("name", coin_id)
    symbol = data.get("symbol", "").upper()
    price = md.get("current_price", {}).get("usd", 0)
    market_cap = md.get("market_cap", {}).get("usd", 0)
    volume_24h = md.get("total_volume", {}).get("usd", 0)
    change_24h = md.get("price_change_percentage_24h", 0) or 0
    change_7d = md.get("price_change_percentage_7d", 0) or 0
    ath = md.get("ath", {}).get("usd", 0)
    ath_change = md.get("ath_change_percentage", {}).get("usd", 0) or 0
    supply = md.get("circulating_supply", 0)

    closes = [c[4] for c in ohlc] if ohlc else []
    rsi = calculate_rsi(closes) if len(closes) > 14 else None
    trend = detect_trend(closes) if closes else "Noma'lum"
    bos = detect_bos(closes) if closes else None
    fvg = detect_fvg(ohlc) if ohlc else None
    sl, tps = find_sl_tp(ohlc, price, trend) if ohlc else (None, [])

    spike, spike_ratio = check_volume_spike(volume_24h, volume_24h * 0.75)
    fund_score, fund_details = calculate_fundamental_score(data)
    news = await get_news(symbol)

    signals = {
        "volume_spike": spike,
        "rsi_extreme": rsi and (rsi < 30 or rsi > 70),
        "bos": bos is not None,
        "fvg": fvg is not None,
        "near_support": sl and price <= sl * 1.03 if sl else False,
    }
    strength_label, sig_count = signal_strength(signals)

    # Signal log
    if strength_label and sl and tps:
        STATS["total_signals"] += 1
        STATS["signals_log"].append({
            "id": STATS["total_signals"],
            "coin": symbol,
            "entry": price,
            "sl": sl,
            "tp1": tps[0] if len(tps) > 0 else None,
            "tp2": tps[1] if len(tps) > 1 else None,
            "tp3": tps[2] if len(tps) > 2 else None,
            "status": "🔄 Ochiq",
            "time": datetime.now().strftime('%Y-%m-%d %H:%M'),
        })

    # RSI matni
    rsi_text = ""
    if rsi:
        if rsi < 30: rsi_text = f"🟢 Oversold ({rsi})"
        elif rsi > 70: rsi_text = f"🔴 Overbought ({rsi})"
        else: rsi_text = f"⚪ Neytral ({rsi})"

    # TP matni
    tp_text = ""
    if tps:
        for i, tp in enumerate(tps[:3], 1):
            pct = (tp - price) / price * 100
            tp_text += f"• TP{i}: `${tp:,.4f}` ({pct:+.1f}%) — _swing {'high' if 'Bullish' in trend else 'low'}_\n"

    sl_pct = ((sl - price) / price * 100) if sl else 0

    # Risk/Reward
    rr_text = ""
    if sl and tps:
        risk = abs(price - sl)
        if risk > 0:
            reward = abs(tps[0] - price)
            rr_text = f"1:{round(reward/risk, 2)}"

    change_emoji = "🟢" if change_24h >= 0 else "🔴"

    msg = f"""
📊 *{name} ({symbol}/USDT)*
🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC

━━━━━━━━━━━━━━━
💰 *Bozor ma'lumotlari*
• Narx: `${price:,.4f}`
• 24h: {change_emoji} `{change_24h:+.2f}%` | 7d: `{change_7d:+.2f}%`
• Market Cap: `${market_cap/1e9:.2f}B`
• 24h Hajm: `${volume_24h/1e6:.1f}M`
• ATH: `${ath:,.2f}` ({ath_change:.1f}%)
• Ta'minot: `{supply:,.0f} {symbol}`

━━━━━━━━━━━━━━━
📈 *Texnik tahlil*
• Trend: {trend}
• RSI (14): {rsi_text or "Ma'lumot yo'q"}"""

    if spike:
        msg += f"\n• ⚡ *Volume spike!* Oddiy hajmdan {spike_ratio}x ko'p"
    if bos:
        msg += f"\n• {bos}"
    if fvg:
        msg += f"\n• {fvg}"

    if sl and tps:
        msg += f"""

━━━━━━━━━━━━━━━
🎯 *Trade Setup*
• Entry: `${price:,.4f}`
• SL: `${sl:,.4f}` ({sl_pct:.1f}%) — _swing {'low' if 'Bullish' in trend else 'high'}_
{tp_text}"""
        if rr_text:
            msg += f"• Risk/Reward: `{rr_text}`"

    msg += f"""

━━━━━━━━━━━━━━━
⭐ *Fundamental skor: {fund_score}/10*
"""
    for d in fund_details:
        msg += f"{d}\n"

    if news:
        msg += "\n━━━━━━━━━━━━━━━\n📰 *Muhim yangiliklar*\n"
        for n in news:
            msg += f"{n}\n"

    if strength_label:
        msg += f"\n━━━━━━━━━━━━━━━\n🚨 *{strength_label}* — {sig_count} faktor to'g'ri keldi"

    msg += DISCLAIMER
    return msg, strength_label

# ============================================================
# HISOBOT
# ============================================================
def generate_report():
    total = STATS["total_signals"]
    if total == 0:
        return "📊 *Hisobot*\n\nHali signal berilmagan.\n\nSignal olish uchun /watch yoqing." + DISCLAIMER

    tp_total = STATS["tp1_hit"] + STATS["tp2_hit"] + STATS["tp3_hit"]
    sl = STATS["sl_hit"]
    ochiq = total - tp_total - sl
    win_rate = round(tp_total / (tp_total + sl) * 100, 1) if (tp_total + sl) > 0 else 0

    msg = f"""📊 *Signal Hisoboti*
━━━━━━━━━━━━━━━
• Jami signallar: `{total}`
• ✅ TP1 urdi: `{STATS['tp1_hit']}`
• ✅ ✅ TP2 urdi: `{STATS['tp2_hit']}`
• ✅ ✅ ✅ TP3 urdi: `{STATS['tp3_hit']}`
• ❌ SL urdi: `{sl}`
• 🔄 Ochiq: `{ochiq}`
━━━━━━━━━━━━━━━
• Win rate: `{win_rate}%`

*So'nggi 5 signal:*
"""
    for s in reversed(STATS["signals_log"][-5:]):
        msg += f"{s['status']} #{s['id']} *{s['coin']}* | `${s['entry']:,.4f}` | {s['time']}\n"

    msg += DISCLAIMER
    return msg

def mark_result(signal_id: int, status: str):
    for s in STATS["signals_log"]:
        if s["id"] == signal_id:
            s["status"] = f"{'✅' if 'TP' in status else '❌'} {status}"
            if status == "TP1": STATS["tp1_hit"] += 1
            elif status == "TP2": STATS["tp2_hit"] += 1
            elif status == "TP3": STATS["tp3_hit"] += 1
            elif status == "SL": STATS["sl_hit"] += 1
            return True
    return False

# ============================================================
# TELEGRAM KOMANDALAR
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text(
        "👋 *Crypto Tahlil Botiga xush kelibsiz!*\n\n"
        "📌 *Komandalar:*\n"
        "• /btc — Bitcoin tahlili\n"
        "• /eth — Ethereum tahlili\n"
        "• /sol — Solana tahlili\n"
        "• /coin dogecoin — Istalgan token\n"
        "• /watch — Avtomatik signallar (har soat)\n"
        "• /stop — Kuzatishni to'xtatish\n"
        "• /hisobot — Signal statistikasi\n\n"
        "📝 *Natija belgilash:*\n"
        "• /tp1 5 — 5-signal TP1 urdi\n"
        "• /tp2 5 — 5-signal TP2 urdi\n"
        "• /tp3 5 — 5-signal TP3 urdi\n"
        "• /sl 5 — 5-signal SL urdi",
        parse_mode="Markdown"
    )

async def cmd_btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("⏳ Bitcoin tahlil qilinmoqda...")
    result, _ = await analyze_coin("bitcoin")
    await m.edit_text(result or "❌ Xato", parse_mode="Markdown")

async def cmd_eth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("⏳ Ethereum tahlil qilinmoqda...")
    result, _ = await analyze_coin("ethereum")
    await m.edit_text(result or "❌ Xato", parse_mode="Markdown")

async def cmd_sol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("⏳ Solana tahlil qilinmoqda...")
    result, _ = await analyze_coin("solana")
    await m.edit_text(result or "❌ Xato", parse_mode="Markdown")

async def cmd_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❓ Misol: /coin dogecoin")
        return
    coin_id = context.args[0].lower()
    m = await update.message.reply_text(f"⏳ {coin_id} tahlil qilinmoqda...")
    result, _ = await analyze_coin(coin_id)
    await m.edit_text(result or f"❌ '{coin_id}' topilmadi.", parse_mode="Markdown")

async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)
    await update.message.reply_text(
        "✅ *Avtomatik kuzatish yoqildi!*\n"
        "Har soatda BTC, ETH, SOL tekshiriladi.\n"
        "Faqat signal bo'lganda xabar keladi! 🚨",
        parse_mode="Markdown"
    )

async def cmd_stop_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.discard(update.effective_chat.id)
    await update.message.reply_text("🔕 Avtomatik kuzatish to'xtatildi.")

async def cmd_hisobot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(generate_report(), parse_mode="Markdown")

async def cmd_mark(update: Update, context: ContextTypes.DEFAULT_TYPE, status: str):
    if not context.args:
        await update.message.reply_text(f"❓ Misol: /{status.lower()} 5")
        return
    try:
        sid = int(context.args[0])
        if mark_result(sid, status.upper()):
            emoji = "✅" if "TP" in status else "❌"
            await update.message.reply_text(f"{emoji} #{sid} signal *{status.upper()}* deb belgilandi!", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ #{sid} signal topilmadi.")
    except ValueError:
        await update.message.reply_text("❓ Raqam kiriting. Misol: /tp1 5")

async def cmd_tp1(u, c): await cmd_mark(u, c, "TP1")
async def cmd_tp2(u, c): await cmd_mark(u, c, "TP2")
async def cmd_tp3(u, c): await cmd_mark(u, c, "TP3")
async def cmd_sl(u, c): await cmd_mark(u, c, "SL")

# ============================================================
# HAR SOATLIK SKAN
# ============================================================
async def hourly_scan(bot):
    while True:
        await asyncio.sleep(3600)
        if not CHAT_IDS:
            continue
        logger.info("Avtomatik skan...")
        for coin_id in WATCH_LIST:
            try:
                result, strength = await analyze_coin(coin_id)
                if result and strength:
                    for chat_id in list(CHAT_IDS):
                        try:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=f"🚨 *AVTOMATIK SIGNAL*\n{result}",
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            logger.error(f"Yuborishda xato: {e}")
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"{coin_id} xato: {e}")

# ============================================================
# MAIN
# ============================================================
async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("btc", cmd_btc))
    app.add_handler(CommandHandler("eth", cmd_eth))
    app.add_handler(CommandHandler("sol", cmd_sol))
    app.add_handler(CommandHandler("coin", cmd_coin))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("stop", cmd_stop_watch))
    app.add_handler(CommandHandler("hisobot", cmd_hisobot))
    app.add_handler(CommandHandler("tp1", cmd_tp1))
    app.add_handler(CommandHandler("tp2", cmd_tp2))
    app.add_handler(CommandHandler("tp3", cmd_tp3))
    app.add_handler(CommandHandler("sl", cmd_sl))
    asyncio.create_task(hourly_scan(app.bot))
    print("✅ Bot ishga tushdi!")
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
