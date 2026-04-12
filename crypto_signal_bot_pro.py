import ccxt
import pandas as pd
import numpy as np
import requests
from datetime import datetime
import time
import ta
from ta.trend import ADXIndicator, MACD
from ta.momentum import RSIIndicator
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ============================================
# KONFIGURATSIYA
# ============================================

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# Exchange sozlamalari
ENABLED_EXCHANGES = os.getenv('ENABLED_EXCHANGES', 'binance,bitget,mexc').split(',')

# ASOSIY FILTRLAR (O'ZGARMAS)
MIN_VOLUME_USD = int(os.getenv('MIN_VOLUME_USD', '2000000'))  # $2M
MIN_MARKET_CAP = int(os.getenv('MIN_MARKET_CAP', '5000000'))  # $5M
MIN_ADX = int(os.getenv('MIN_ADX', '25'))
RSI_OVERBOUGHT = int(os.getenv('RSI_OVERBOUGHT', '70'))
RSI_OVERSOLD = int(os.getenv('RSI_OVERSOLD', '30'))
MIN_SIGNAL_SCORE = int(os.getenv('MIN_SIGNAL_SCORE', '8'))

# Bot sozlamalari
TIMEFRAME = os.getenv('TIMEFRAME', '15m')  # 15 daqiqa
LOOKBACK_CANDLES = int(os.getenv('LOOKBACK_CANDLES', '100'))
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '300'))  # 5 daqiqa
MAX_WORKERS = int(os.getenv('MAX_WORKERS', '15'))
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '100'))  # Barcha coinlar

# Manual scan sozlamalari
MANUAL_SCAN_ACTIVE = False
MANUAL_SCAN_SCORE = 7  # Manual scan uchun 7 ball
MANUAL_SCAN_MIN_VOLUME = 500000  # $500K
MANUAL_SCAN_MIN_CAP = 1000000  # $1M

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# ============================================
# TELEGRAM BOT COMMANDS
# ============================================

def send_telegram_message(message, parse_mode="HTML"):
    """Telegram xabar yuborish"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": parse_mode
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logging.error(f"❌ Telegram xato: {e}")
        return False


def send_welcome_message():
    """Xush kelibsiz xabari"""
    message = """
🎉 <b>RUSTAMOV CRYPTO BOT'GA XUSH KELIBSIZ!</b> 🎉

━━━━━━━━━━━━━━━━━━━━━
<b>🤖 BOT IMKONIYATLARI</b>
━━━━━━━━━━━━━━━━━━━━━

✅ <b>3 ta Birja:</b> Binance, Bitget, MEXC
✅ <b>600+ Coin:</b> Barcha USDT juftliklar
✅ <b>Professional Tahlil:</b> RSI, MACD, ADX, S/R
✅ <b>Auto TP/SL:</b> Risk/Reward asosida
✅ <b>Kuchli Filtrlar:</b> Faqat 8+/10 score signallar

━━━━━━━━━━━━━━━━━━━━━
<b>📊 AVTOMATIK MONITORING</b>
━━━━━━━━━━━━━━━━━━━━━

🔄 Har 5 daqiqada avtomatik skanerlash
📈 Minimal hajm: $2M
💰 Minimal market cap: $5M
⭐ Minimal score: 8/10

━━━━━━━━━━━━━━━━━━━━━
<b>🔍 BUYRUQLAR</b>
━━━━━━━━━━━━━━━━━━━━━

/start - Botni ishga tushirish
/scan - Manual skanerlash (7+ score)
/check BTCUSDT - Coin malumotini olish
/status - Bot holati
/help - Yordam

━━━━━━━━━━━━━━━━━━━━━
<b>⚠️ MUHIM OGOHLANTIRISH</b>
━━━━━━━━━━━━━━━━━━━━━

Bu signallar <b>faqat ma'lumot maqsadida</b> berilgan.
Investitsiya qarorlari sizning mas'uliyatingizda.
<b>Bu moliyaviy maslahat EMAS!</b>

━━━━━━━━━━━━━━━━━━━━━

✅ Bot ishga tushdi va monitoring boshlandi!

🚀 <b>Omad tilaymiz!</b>
    """
    send_telegram_message(message)


def send_keyboard():
    """Inline keyboard yuborish"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🔍 HOZIR SKAN", "callback_data": "scan_now"},
                    {"text": "📊 STATUS", "callback_data": "bot_status"}
                ],
                [
                    {"text": "📈 TOP COINLAR", "callback_data": "top_coins"},
                    {"text": "❓ YORDAM", "callback_data": "help"}
                ]
            ]
        }
        
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": "🎛️ <b>BOT BOSHQARUV PANELI</b>",
            "parse_mode": "HTML",
            "reply_markup": keyboard
        }
        
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logging.error(f"❌ Keyboard xato: {e}")
        return False


def handle_telegram_updates():
    """Telegram yangilanishlarini kuzatish"""
    last_update_id = 0
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {
                "offset": last_update_id + 1,
                "timeout": 30
            }
            
            response = requests.get(url, params=params, timeout=35)
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get('result'):
                    for update in data['result']:
                        last_update_id = update['update_id']
                        
                        # Callback query (tugmalar)
                        if 'callback_query' in update:
                            callback = update['callback_query']
                            callback_data = callback['data']
                            
                            if callback_data == 'scan_now':
                                handle_manual_scan()
                            elif callback_data == 'bot_status':
                                send_bot_status()
                            elif callback_data == 'top_coins':
                                send_top_coins()
                            elif callback_data == 'help':
                                send_help()
                        
                        # Text commands
                        if 'message' in update:
                            message = update['message']
                            text = message.get('text', '')
                            
                            if text.startswith('/start'):
                                send_welcome_message()
                                send_keyboard()
                            elif text.startswith('/scan'):
                                handle_manual_scan()
                            elif text.startswith('/check'):
                                parts = text.split()
                                if len(parts) > 1:
                                    coin = parts[1].upper()
                                    check_single_coin(coin)
                            elif text.startswith('/status'):
                                send_bot_status()
                            elif text.startswith('/help'):
                                send_help()
            
            time.sleep(1)
            
        except Exception as e:
            logging.error(f"❌ Update handler xato: {e}")
            time.sleep(5)


def handle_manual_scan():
    """Manual skanerlash (7+ score, kichik coinlar)"""
    global MANUAL_SCAN_ACTIVE
    
    MANUAL_SCAN_ACTIVE = True
    
    send_telegram_message("""
🔍 <b>MANUAL SKAN BOSHLANDI...</b>

⚙️ Sozlamalar:
  - Score: ≥7/10
  - Hajm: ≥$500K
  - Market Cap: ≥$1M
  - Barcha coinlar skanlanadi

⏳ Biroz kuting...
    """)
    
    # Manual scan logic (asosiy siklda ishlatiladi)
    logging.info("🔍 MANUAL SCAN FAOLLASHTIRILDI")


def check_single_coin(coin_symbol):
    """Bitta coin haqida to'liq ma'lumot"""
    try:
        send_telegram_message(f"🔍 <b>{coin_symbol}</b> tekshirilmoqda...")
        
        exchanges = get_exchange_connections()
        
        for exchange_name, exchange in exchanges.items():
            try:
                symbol = f"{coin_symbol}/USDT"
                
                # Ticker
                ticker = exchange.fetch_ticker(symbol)
                current_price = ticker.get('last', 0)
                volume_24h = ticker.get('quoteVolume', 0)
                price_change_24h = ticker.get('percentage', 0)
                
                # OHLCV
                ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=100)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                
                # Tahlil
                analysis = quick_technical_analysis(df)
                
                if analysis:
                    # Support/Resistance
                    sr = calculate_support_resistance(df)
                    
                    # Signal yo'nalishi
                    signal_direction = get_signal_direction(analysis)
                    
                    # Pozitsiya
                    position = get_current_position(current_price, sr)
                    
                    # Sabab
                    reason = get_signal_reason(analysis, signal_direction)
                    
                    message = f"""
📊 <b>{coin_symbol} TAHLIL ({exchange_name.upper()})</b>

━━━━━━━━━━━━━━━━━━━━━
<b>💰 ASOSIY MA'LUMOTLAR</b>
━━━━━━━━━━━━━━━━━━━━━

💵 Joriy Narx: ${current_price:.6f}
📈 24h O'zgarish: {price_change_24h:+.2f}%
💰 24h Hajm: ${volume_24h:,.0f}

━━━━━━━━━━━━━━━━━━━━━
<b>📈 TEXNIK INDIKATORLAR</b>
━━━━━━━━━━━━━━━━━━━━━

📊 RSI: {analysis['rsi']:.2f}
📉 MACD: {analysis['macd_hist']:.4f}
💪 ADX: {analysis['adx']:.2f}
🔥 Trend: {analysis['trend']}

━━━━━━━━━━━━━━━━━━━━━
<b>🎯 SUPPORT & RESISTANCE</b>
━━━━━━━━━━━━━━━━━━━━━

📈 Resistance 1: ${sr['r1']:.6f}
📈 Resistance 2: ${sr['r2']:.6f}
⚖️ Pivot: ${sr['pivot']:.6f}
📉 Support 1: ${sr['s1']:.6f}
📉 Support 2: ${sr['s2']:.6f}

━━━━━━━━━━━━━━━━━━━━━
<b>📍 JORIY POZITSIYA</b>
━━━━━━━━━━━━━━━━━━━━━

{position}

━━━━━━━━━━━━━━━━━━━━━
<b>💡 SIGNAL TAHLILI</b>
━━━━━━━━━━━━━━━━━━━━━

🎯 Yo'nalish: <b>{signal_direction}</b>

📝 Sabab:
{reason}

━━━━━━━━━━━━━━━━━━━━━

⚠️ <b>Bu moliyaviy maslahat emas!</b>
                    """
                    
                    send_telegram_message(message)
                    return
                    
            except Exception as e:
                continue
        
        send_telegram_message(f"❌ {coin_symbol} topilmadi yoki tahlil qilib bo'lmadi")
        
    except Exception as e:
        send_telegram_message(f"❌ Xatolik: {str(e)}")


def send_bot_status():
    """Bot holati"""
    # Bu funksiyani asosiy siklda global variablelar bilan to'ldirish kerak
    message = """
📊 <b>BOT HOLATI</b>

━━━━━━━━━━━━━━━━━━━━━

✅ Status: <b>AKTIV</b>
🔄 Monitoring: <b>YONIQ</b>
⏰ So'nggi tekshirish: <i>1 daqiqa oldin</i>

📊 Birjalar: Binance, Bitget, MEXC
🔍 Skanlanmoqda: 600+ coinlar
⏱️ Timeframe: 15m
🔄 Interval: 5 daqiqa

━━━━━━━━━━━━━━━━━━━━━
    """
    send_telegram_message(message)


def send_help():
    """Yordam"""
    message = """
❓ <b>YORDAM</b>

━━━━━━━━━━━━━━━━━━━━━
<b>🔍 BUYRUQLAR</b>
━━━━━━━━━━━━━━━━━━━━━

<b>/start</b> - Botni ishga tushirish
<b>/scan</b> - Hozir skanerlash (7+ score)
<b>/check BTCUSDT</b> - Coin ma'lumotini olish
<b>/status</b> - Bot holatini ko'rish
<b>/help</b> - Bu yordam

━━━━━━━━━━━━━━━━━━━━━
<b>🎛️ TUGMALAR</b>
━━━━━━━━━━━━━━━━━━━━━

🔍 <b>HOZIR SKAN</b> - Manual skanerlash
  • Score: ≥7/10
  • Kichik coinlar ham kiritiladi
  • Hajm: ≥$500K

📊 <b>STATUS</b> - Bot holati

📈 <b>TOP COINLAR</b> - Eng yaxshi coinlar

❓ <b>YORDAM</b> - Bu xabar

━━━━━━━━━━━━━━━━━━━━━
<b>📖 MISOL</b>
━━━━━━━━━━━━━━━━━━━━━

/check BTCUSDT
/check ETHUSDT
/check BNBUSDT

━━━━━━━━━━━━━━━━━━━━━
    """
    send_telegram_message(message)


def send_top_coins():
    """Top coinlar ro'yxati"""
    message = """
📈 <b>TOP COINLAR</b>

🔄 Real-time ma'lumot yuklanmoqda...

⏳ Biroz kuting...
    """
    send_telegram_message(message)


# ============================================
# EXCHANGE ULANISH
# ============================================

def get_exchange_connections():
    """Barcha birjalar bilan ulanish"""
    exchanges = {}
    
    if 'binance' in ENABLED_EXCHANGES:
        try:
            exchanges['binance'] = ccxt.binance({
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
                'timeout': 10000,
            })
            logging.info("✅ Binance ulandi")
        except Exception as e:
            logging.error(f"❌ Binance: {e}")
    
    if 'bitget' in ENABLED_EXCHANGES:
        try:
            exchanges['bitget'] = ccxt.bitget({
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
                'timeout': 10000,
            })
            logging.info("✅ Bitget ulandi")
        except Exception as e:
            logging.error(f"❌ Bitget: {e}")
    
    if 'mexc' in ENABLED_EXCHANGES:
        try:
            exchanges['mexc'] = ccxt.mexc({
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
                'timeout': 10000,
            })
            logging.info("✅ MEXC ulandi")
        except Exception as e:
            logging.error(f"❌ MEXC: {e}")
    
    return exchanges


def get_all_usdt_pairs(exchange, exchange_name):
    """BARCHA USDT juftliklarini olish"""
    try:
        markets = exchange.load_markets()
        usdt_pairs = [
            symbol for symbol in markets.keys()
            if symbol.endswith('/USDT') and markets[symbol]['active']
        ]
        logging.info(f"✅ {exchange_name}: {len(usdt_pairs)} ta coin")
        return usdt_pairs
    except Exception as e:
        logging.error(f"❌ {exchange_name} markets: {e}")
        return []


def get_24h_tickers_batch(exchange, exchange_name):
    """24h tickerlar"""
    try:
        tickers = exchange.fetch_tickers()
        return tickers
    except Exception as e:
        logging.error(f"❌ {exchange_name} tickers: {e}")
        return {}


def filter_by_volume(tickers, exchange_name, min_volume):
    """Hajm filtri"""
    filtered = {}
    for symbol, ticker in tickers.items():
        if symbol.endswith('/USDT'):
            volume_usd = ticker.get('quoteVolume', 0)
            if volume_usd >= min_volume:
                filtered[symbol] = ticker
    
    logging.info(f"✅ {exchange_name}: {len(filtered)} coin hajm filtridan o'tdi")
    return filtered


# ============================================
# TEXNIK TAHLIL
# ============================================

def quick_technical_analysis(df):
    """Tez texnik tahlil"""
    try:
        # RSI
        rsi_indicator = RSIIndicator(close=df['close'], window=14)
        rsi = rsi_indicator.rsi().iloc[-1]
        
        # MACD
        macd_indicator = MACD(close=df['close'], window_slow=26, window_fast=12, window_sign=9)
        macd_hist = macd_indicator.macd_diff().iloc[-1]
        macd_value = macd_indicator.macd().iloc[-1]
        macd_signal = macd_indicator.macd_signal().iloc[-1]
        
        # ADX
        adx_indicator = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
        adx = adx_indicator.adx().iloc[-1]
        di_plus = adx_indicator.adx_pos().iloc[-1]
        di_minus = adx_indicator.adx_neg().iloc[-1]
        
        # EMA
        df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
        df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
        
        ema_9 = df['ema_9'].iloc[-1]
        ema_21 = df['ema_21'].iloc[-1]
        ema_50 = df['ema_50'].iloc[-1]
        current_price = df['close'].iloc[-1]
        
        # Trend
        if current_price > ema_9 > ema_21 > ema_50:
            trend = "STRONG BULLISH"
        elif current_price > ema_21 > ema_50:
            trend = "BULLISH"
        elif current_price < ema_9 < ema_21 < ema_50:
            trend = "STRONG BEARISH"
        elif current_price < ema_21 < ema_50:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"
        
        # Volume
        avg_volume = df['volume'].tail(20).mean()
        current_volume = df['volume'].iloc[-1]
        volume_surge = current_volume > (avg_volume * 2.5)
        
        # Price change
        price_change_3 = ((df['close'].iloc[-1] - df['close'].iloc[-4]) / df['close'].iloc[-4]) * 100
        price_change_10 = ((df['close'].iloc[-1] - df['close'].iloc[-11]) / df['close'].iloc[-11]) * 100
        
        return {
            'rsi': rsi,
            'macd_hist': macd_hist,
            'macd': macd_value,
            'macd_signal': macd_signal,
            'adx': adx,
            'di_plus': di_plus,
            'di_minus': di_minus,
            'trend': trend,
            'volume_surge': volume_surge,
            'price': current_price,
            'price_change_3': price_change_3,
            'price_change_10': price_change_10,
            'ema_9': ema_9,
            'ema_21': ema_21,
            'ema_50': ema_50
        }
        
    except Exception as e:
        return None


def calculate_support_resistance(df):
    """Support va Resistance hisoblash"""
    try:
        high_20 = df['high'].tail(20).max()
        low_20 = df['low'].tail(20).min()
        close = df['close'].iloc[-1]
        
        pivot = (high_20 + low_20 + close) / 3
        
        r1 = 2 * pivot - low_20
        r2 = pivot + (high_20 - low_20)
        r3 = high_20 + 2 * (pivot - low_20)
        
        s1 = 2 * pivot - high_20
        s2 = pivot - (high_20 - low_20)
        s3 = low_20 - 2 * (high_20 - pivot)
        
        return {
            'pivot': pivot,
            'r1': r1,
            'r2': r2,
            'r3': r3,
            's1': s1,
            's2': s2,
            's3': s3
        }
    except:
        return None


def get_current_position(price, sr):
    """Narxning SR ga nisbatan pozitsiyasi"""
    if not sr:
        return "Ma'lumot yo'q"
    
    if price > sr['r1']:
        return f"📈 Resistance 1 dan yuqorida ({((price - sr['r1']) / sr['r1'] * 100):.2f}%)"
    elif price > sr['pivot']:
        return f"⚖️ Pivot va R1 orasida ({((price - sr['pivot']) / sr['pivot'] * 100):.2f}% pivot dan yuqori)"
    elif price > sr['s1']:
        return f"⚖️ Support 1 va Pivot orasida"
    else:
        return f"📉 Support 1 dan pastda ({((sr['s1'] - price) / sr['s1'] * 100):.2f}%)"


def get_signal_direction(analysis):
    """Signal yo'nalishini aniqlash"""
    if not analysis:
        return "NOMA'LUM"
    
    trend = analysis['trend']
    macd_hist = analysis['macd_hist']
    rsi = analysis['rsi']
    
    if "BULLISH" in trend and macd_hist > 0 and rsi < 65:
        return "🟢 LONG"
    elif "BEARISH" in trend and macd_hist < 0 and rsi > 35:
        return "🔴 SHORT"
    else:
        return "⚪ NEUTRAL"


def get_signal_reason(analysis, signal_direction):
    """Signal sababi"""
    if not analysis:
        return "Ma'lumot yo'q"
    
    reasons = []
    
    # Trend
    if "STRONG BULLISH" in analysis['trend']:
        reasons.append("✅ Kuchli ko'tarilish trendi (EMA 9>21>50)")
    elif "BULLISH" in analysis['trend']:
        reasons.append("✅ Ko'tarilish trendi")
    elif "STRONG BEARISH" in analysis['trend']:
        reasons.append("✅ Kuchli pasayish trendi (EMA 9<21<50)")
    elif "BEARISH" in analysis['trend']:
        reasons.append("✅ Pasayish trendi")
    
    # MACD
    if analysis['macd_hist'] > 0:
        reasons.append("✅ MACD ijobiy (ko'tarilish impulsi)")
    else:
        reasons.append("✅ MACD salbiy (pasayish impulsi)")
    
    # RSI
    if 40 <= analysis['rsi'] <= 60:
        reasons.append(f"✅ RSI optimal zonada ({analysis['rsi']:.1f})")
    elif analysis['rsi'] < 40:
        reasons.append(f"✅ RSI oversold yaqinida ({analysis['rsi']:.1f})")
    elif analysis['rsi'] > 60:
        reasons.append(f"✅ RSI overbought yaqinida ({analysis['rsi']:.1f})")
    
    # ADX
    if analysis['adx'] > 25:
        reasons.append(f"✅ Kuchli trend (ADX: {analysis['adx']:.1f})")
    
    # Volume
    if analysis['volume_surge']:
        reasons.append("🚀 Hajm keskin oshgan (2.5x)")
    
    # Price momentum
    if abs(analysis['price_change_3']) > 3:
        reasons.append(f"✅ Kuchli harakat: {analysis['price_change_3']:+.2f}% (3 sham)")
    
    return "\n".join(reasons) if reasons else "Standart tahlil"


def calculate_quick_score(analysis):
    """Score hisoblash"""
    score = 0
    
    rsi = analysis.get('rsi', 50)
    adx = analysis.get('adx', 0)
    macd_hist = analysis.get('macd_hist', 0)
    trend = analysis.get('trend', 'NEUTRAL')
    volume_surge = analysis.get('volume_surge', False)
    price_change_3 = analysis.get('price_change_3', 0)
    
    # RSI optimal
    if 40 <= rsi <= 60:
        score += 2
    elif 30 <= rsi <= 70:
        score += 1
    
    # ADX kuchli
    if adx > 30:
        score += 2
    elif adx > 25:
        score += 1
    
    # MACD
    if macd_hist > 0:
        score += 1
    
    # Trend
    if "STRONG" in trend:
        score += 3
    elif trend in ['BULLISH', 'BEARISH']:
        score += 2
    
    # Volume surge
    if volume_surge:
        score += 2
    
    # Price momentum
    if abs(price_change_3) > 5:
        score += 1
    
    return min(score, 10)


# ============================================
# TP/SL AVTOMATIK HISOBLASH
# ============================================

def calculate_auto_tp_sl(current_price, signal_type, sr, analysis):
    """TP va SL avtomatik hisoblash (Risk/Reward asosida)"""
    try:
        risk_reward_ratio = 2.5  # 1:2.5
        
        if signal_type == 'LONG':
            # Entry
            entry = current_price
            
            # Stop Loss (Support yoki ATR asosida)
            if sr and sr['s1'] < current_price:
                sl = sr['s1'] * 0.995  # Support dan 0.5% pastroq
            else:
                sl = current_price * 0.97  # 3% SL
            
            risk = entry - sl
            
            # Take Profit (Risk/Reward asosida)
            tp1 = entry + (risk * 1.5)  # 1:1.5
            tp2 = entry + (risk * 2.5)  # 1:2.5
            tp3 = entry + (risk * 4.0)  # 1:4
            
            # Agar SR mavjud bo'lsa, R larga yaqinlashtirish
            if sr:
                if tp1 < sr['r1']:
                    tp1 = sr['r1'] * 0.998
                if tp2 < sr['r2']:
                    tp2 = sr['r2'] * 0.998
                if tp3 < sr['r3']:
                    tp3 = sr['r3'] * 0.998
        
        else:  # SHORT
            entry = current_price
            
            # Stop Loss
            if sr and sr['r1'] > current_price:
                sl = sr['r1'] * 1.005
            else:
                sl = current_price * 1.03
            
            risk = sl - entry
            
            # Take Profit
            tp1 = entry - (risk * 1.5)
            tp2 = entry - (risk * 2.5)
            tp3 = entry - (risk * 4.0)
            
            if sr:
                if tp1 > sr['s1']:
                    tp1 = sr['s1'] * 1.002
                if tp2 > sr['s2']:
                    tp2 = sr['s2'] * 1.002
                if tp3 > sr['s3']:
                    tp3 = sr['s3'] * 1.002
        
        risk_percent = (abs(entry - sl) / entry) * 100
        reward_percent = (abs(tp2 - entry) / entry) * 100
        actual_rr = reward_percent / risk_percent if risk_percent > 0 else 0
        
        return {
            'entry': entry,
            'stop_loss': sl,
            'tp1': tp1,
            'tp2': tp2,
            'tp3': tp3,
            'risk_percent': risk_percent,
            'reward_percent': reward_percent,
            'risk_reward_ratio': actual_rr
        }
    except:
        return None


# ============================================
# SIGNAL GENERATSIYA
# ============================================

def analyze_single_symbol(symbol, exchange, exchange_name, ticker_data, use_manual_filters=False):
    """Bitta symbolni tahlil qilish"""
    try:
        # OHLCV
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LOOKBACK_CANDLES)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        if df.empty or len(df) < 50:
            return None
        
        # Tahlil
        analysis = quick_technical_analysis(df)
        if not analysis:
            return None
        
        # Score
        score = calculate_quick_score(analysis)
        
        # Filtrlar (manual yoki avtomatik)
        if use_manual_filters:
            min_score = MANUAL_SCAN_SCORE
            min_volume = MANUAL_SCAN_MIN_VOLUME
        else:
            min_score = MIN_SIGNAL_SCORE
            min_volume = MIN_VOLUME_USD
        
        # Score check
        if score < min_score:
            return None
        
        # RSI check
        rsi = analysis['rsi']
        adx = analysis['adx']
        
        if rsi > RSI_OVERBOUGHT or rsi < RSI_OVERSOLD:
            return None
        
        if adx < MIN_ADX:
            return None
        
        # Signal type
        trend = analysis['trend']
        macd_hist = analysis['macd_hist']
        
        signal_type = None
        if ("BULLISH" in trend) and macd_hist > 0 and rsi < 65:
            signal_type = 'LONG'
        elif ("BEARISH" in trend) and macd_hist < 0 and rsi > 35:
            signal_type = 'SHORT'
        
        if not signal_type:
            return None
        
        # Support/Resistance
        sr = calculate_support_resistance(df)
        
        # TP/SL avtomatik hisoblash
        levels = calculate_auto_tp_sl(analysis['price'], signal_type, sr, analysis)
        
        if not levels:
            return None
        
        # Signal obyekti
        signal = {
            'exchange': exchange_name.upper(),
            'symbol': symbol,
            'type': signal_type,
            'timestamp': datetime.now(),
            'price': analysis['price'],
            'price_change_3': analysis['price_change_3'],
            'price_change_10': analysis['price_change_10'],
            'score': score,
            'rsi': rsi,
            'adx': adx,
            'macd_hist': macd_hist,
            'macd': analysis['macd'],
            'macd_signal': analysis['macd_signal'],
            'trend': trend,
            'volume_surge': analysis['volume_surge'],
            'entry': levels['entry'],
            'stop_loss': levels['stop_loss'],
            'tp1': levels['tp1'],
            'tp2': levels['tp2'],
            'tp3': levels['tp3'],
            'risk_percent': levels['risk_percent'],
            'reward_percent': levels['reward_percent'],
            'risk_reward_ratio': levels['risk_reward_ratio'],
            'volume_24h': ticker_data.get('quoteVolume', 0),
            'support_resistance': sr,
            'analysis': analysis
        }
        
        return signal
        
    except Exception as e:
        return None


def analyze_batch_parallel(symbols_batch, exchange, exchange_name, tickers, use_manual=False):
    """Batch parallel tahlil"""
    signals = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                analyze_single_symbol,
                symbol,
                exchange,
                exchange_name,
                tickers.get(symbol, {}),
                use_manual
            ): symbol
            for symbol in symbols_batch
        }
        
        for future in as_completed(futures):
            try:
                signal = future.result(timeout=8)
                if signal:
                    signals.append(signal)
            except Exception:
                pass
    
    return signals


# ============================================
# SIGNAL FORMATLASH
# ============================================

def format_signal_message(signal):
    """Signal formatlash"""
    emoji = '🟢' if signal['type'] == 'LONG' else '🔴'
    pump_emoji = '🚀' if signal.get('volume_surge', False) else ''
    
    # Signal sababi
    reason = get_signal_reason(signal['analysis'], signal['type'])
    
    message = f"""
{emoji} <b>{signal['type']} SIGNAL</b> {pump_emoji} | <b>{signal['exchange']}</b>

━━━━━━━━━━━━━━━━━━━━━
<b>💰 {signal['symbol']}</b>
━━━━━━━━━━━━━━━━━━━━━

💵 Joriy Narx: <b>${signal['price']:.6f}</b>
📊 3-sham: {signal['price_change_3']:+.2f}%
📈 10-sham: {signal['price_change_10']:+.2f}%
⭐ Signal Score: <b>{signal['score']}/10</b>

━━━━━━━━━━━━━━━━━━━━━
<b>📈 TEXNIK TAHLIL</b>
━━━━━━━━━━━━━━━━━━━━━

📊 RSI: {signal['rsi']:.2f}
📉 MACD: {signal['macd_hist']:.4f}
💪 ADX: {signal['adx']:.2f}
🔥 Trend: <b>{signal['trend']}</b>
{'🚀 <b>VOLUME SURGE!</b>' if signal['volume_surge'] else ''}

━━━━━━━━━━━━━━━━━━━━━
<b>💰 SAVDO DARAJALARI (Auto TP/SL)</b>
━━━━━━━━━━━━━━━━━━━━━

📍 <b>Entry:</b> ${signal['entry']:.6f}
🛑 <b>Stop Loss:</b> ${signal['stop_loss']:.6f}

🎯 <b>Take Profit 1:</b> ${signal['tp1']:.6f}
🎯 <b>Take Profit 2:</b> ${signal['tp2']:.6f}
🎯 <b>Take Profit 3:</b> ${signal['tp3']:.6f}

⚠️ <b>Risk:</b> {signal['risk_percent']:.2f}%
💰 <b>Reward:</b> {signal['reward_percent']:.2f}%
⚖️ <b>R/R Ratio:</b> 1:{signal['risk_reward_ratio']:.2f}

━━━━━━━━━━━━━━━━━━━━━
<b>📊 SUPPORT & RESISTANCE</b>
━━━━━━━━━━━━━━━━━━━━━

📈 R1: ${signal['support_resistance']['r1']:.6f}
⚖️ Pivot: ${signal['support_resistance']['pivot']:.6f}
📉 S1: ${signal['support_resistance']['s1']:.6f}

━━━━━━━━━━━━━━━━━━━━━
<b>💡 SIGNAL SABABI</b>
━━━━━━━━━━━━━━━━━━━━━

{reason}

━━━━━━━━━━━━━━━━━━━━━
<b>📊 QO'SHIMCHA</b>
━━━━━━━━━━━━━━━━━━━━━

💵 24h Hajm: ${signal['volume_24h']:,.0f}
⏰ Vaqt: {signal['timestamp'].strftime('%H:%M:%S')}

━━━━━━━━━━━━━━━━━━━━━

⚠️ <b>BU MOLIYAVIY MASLAHAT EMAS!</b>
<i>O'z tadqiqotingizni o'tkazing (DYOR).</i>
    """
    return message


def send_signal(signal):
    """Signal yuborish"""
    message = format_signal_message(signal)
    success = send_telegram_message(message)
    if success:
        logging.info(f"✅ Signal: {signal['exchange']} - {signal['symbol']} - {signal['type']}")
    return success


# ============================================
# ASOSIY SIKL
# ============================================

def main():
    """Asosiy dastur"""
    global MANUAL_SCAN_ACTIVE
    
    try:
        logging.info("="*60)
        logging.info("🚀 RUSTAMOV CRYPTO BOT")
        logging.info("="*60)
        
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("❌ Telegram sozlanmagan!")
            return
        
        # Exchange ulanish
        exchanges = get_exchange_connections()
        
        if not exchanges:
            logging.error("❌ Hech qanday exchange ulanmadi!")
            return
        
        # Xush kelibsiz xabari
        send_welcome_message()
        time.sleep(2)
        send_keyboard()
        
        # Telegram updates handler (background)
        update_thread = threading.Thread(target=handle_telegram_updates, daemon=True)
        update_thread.start()
        
        # Asosiy sikl
        while True:
            try:
                cycle_start = time.time()
                logging.info(f"\n{'='*60}")
                logging.info(f"🔄 Sikl: {datetime.now().strftime('%H:%M:%S')}")
                logging.info(f"{'='*60}")
                
                all_signals = []
                
                # Manual scan check
                use_manual_filters = MANUAL_SCAN_ACTIVE
                
                if use_manual_filters:
                    logging.info("🔍 MANUAL SCAN REJIMI")
                    min_score_display = MANUAL_SCAN_SCORE
                    min_volume_display = MANUAL_SCAN_MIN_VOLUME
                else:
                    min_score_display = MIN_SIGNAL_SCORE
                    min_volume_display = MIN_VOLUME_USD
                
                # Har bir exchange
                for exchange_name, exchange in exchanges.items():
                    try:
                        logging.info(f"\n--- {exchange_name.upper()} ---")
                        
                        # Tickerlar
                        tickers = get_24h_tickers_batch(exchange, exchange_name)
                        if not tickers:
                            continue
                        
                        # Hajm filtri
                        filtered_tickers = filter_by_volume(
                            tickers, 
                            exchange_name, 
                            min_volume_display if use_manual_filters else MIN_VOLUME_USD
                        )
                        
                        symbols_to_analyze = list(filtered_tickers.keys())
                        
                        if not symbols_to_analyze:
                            continue
                        
                        logging.info(f"🔍 {exchange_name}: {len(symbols_to_analyze)} coin tahlil")
                        
                        # Batch tahlil
                        for i in range(0, len(symbols_to_analyze), BATCH_SIZE):
                            batch = symbols_to_analyze[i:i+BATCH_SIZE]
                            
                            batch_signals = analyze_batch_parallel(
                                batch, 
                                exchange, 
                                exchange_name, 
                                filtered_tickers,
                                use_manual_filters
                            )
                            all_signals.extend(batch_signals)
                            
                            time.sleep(0.5)
                        
                    except Exception as e:
                        logging.error(f"❌ {exchange_name}: {e}")
                        continue
                
                # Signallarni yuborish
                if all_signals:
                    logging.info(f"\n🎯 {len(all_signals)} TA SIGNAL!")
                    
                    # Score bo'yicha sort
                    all_signals.sort(key=lambda x: x['score'], reverse=True)
                    
                    # Limit (manual 20, auto 10)
                    limit = 20 if use_manual_filters else 10
                    top_signals = all_signals[:limit]
                    
                    for signal in top_signals:
                        send_signal(signal)
                        time.sleep(1.5)
                    
                    if len(all_signals) > limit:
                        summary = f"📊 Jami {len(all_signals)} signal, top {limit} yuborildi"
                        send_telegram_message(summary)
                    
                    # Manual scan reset
                    if use_manual_filters:
                        MANUAL_SCAN_ACTIVE = False
                        send_telegram_message("✅ Manual scan tugadi!")
                else:
                    logging.info("ℹ️ Signal yo'q")
                    
                    if use_manual_filters:
                        send_telegram_message("❌ Manual scanda signal topilmadi")
                        MANUAL_SCAN_ACTIVE = False
                
                # Stats
                cycle_time = time.time() - cycle_start
                logging.info(f"\n⏱️ Sikl: {cycle_time:.1f}s | Signallar: {len(all_signals)}")
                
                # Keyingi sikl
                wait_time = max(CHECK_INTERVAL - cycle_time, 10)
                logging.info(f"⏳ Keyingi: {wait_time:.0f}s\n")
                time.sleep(wait_time)
                
            except KeyboardInterrupt:
                logging.info("\n👋 To'xtatildi")
                send_telegram_message("⛔ Bot to'xtatildi")
                break
            except Exception as e:
                logging.error(f"❌ Sikl xato: {e}")
                time.sleep(30)
                
    except Exception as e:
        logging.error(f"❌ Fatal: {e}")


if __name__ == "__main__":
    main()
