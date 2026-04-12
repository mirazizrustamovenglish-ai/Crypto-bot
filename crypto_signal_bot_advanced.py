import sys
import subprocess
import os

# Auto-install ccxt agar yo'q bo'lsa
try:
    import ccxt
    print(f"✅ ccxt version: {ccxt.__version__}")
except ImportError:
    print("⚠️ ccxt topilmadi, o'rnatilmoqda...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "ccxt==4.2.25"])
    import ccxt
    print(f"✅ ccxt o'rnatildi: {ccxt.__version__}")

# Auto-install boshqa kutubxonalar
packages = {
    'pandas': '2.1.4',
    'numpy': '1.26.3', 
    'requests': '2.31.0',
    'ta': '0.11.0'
}

for package, version in packages.items():
    try:
        __import__(package)
        print(f"✅ {package} mavjud")
    except ImportError:
        print(f"⚠️ {package} o'rnatilmoqda...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", f"{package}=={version}"])
        print(f"✅ {package} o'rnatildi")

# Endi import qilish
import pandas as pd
import numpy as np
import requests
from datetime import datetime
import time
import ta
from ta.trend import ADXIndicator, MACD
from ta.momentum import RSIIndicator
import logging

# Qolgan kod...
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
import json

# ============================================
# KONFIGURATSIYA
# ============================================

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# Filtr parametrlari
MIN_VOLUME_USD = int(os.getenv('MIN_VOLUME_USD', '2000000'))
MIN_MARKET_CAP = int(os.getenv('MIN_MARKET_CAP', '5000000'))
MIN_ADX = int(os.getenv('MIN_ADX', '25'))
RSI_OVERBOUGHT = int(os.getenv('RSI_OVERBOUGHT', '70'))
RSI_OVERSOLD = int(os.getenv('RSI_OVERSOLD', '30'))
MIN_SIGNAL_SCORE = int(os.getenv('MIN_SIGNAL_SCORE', '8'))

# Yangi parametrlar
TIMEFRAME = os.getenv('TIMEFRAME', '5m')  # 5 daqiqa (tezroq)
LOOKBACK_CANDLES = int(os.getenv('LOOKBACK_CANDLES', '50'))  # Kamroq
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '120'))  # 2 daqiqa
MAX_WORKERS = int(os.getenv('MAX_WORKERS', '10'))  # Parallel ishlov
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '50'))  # Batch processing

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# ============================================
# EXCHANGE VA API FUNKSIYALARI
# ============================================

def get_exchange_connection():
    """Binance bilan ulanish"""
    try:
        exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'},
            'timeout': 10000,
        })
        return exchange
    except Exception as e:
        logging.error(f"❌ Exchange xato: {str(e)}")
        return None


def get_all_usdt_pairs(exchange):
    """Barcha USDT juftliklarini olish"""
    try:
        markets = exchange.load_markets()
        usdt_pairs = [
            symbol for symbol in markets.keys()
            if symbol.endswith('/USDT') and markets[symbol]['active']
        ]
        logging.info(f"✅ {len(usdt_pairs)} ta USDT juftlik topildi")
        return usdt_pairs
    except Exception as e:
        logging.error(f"❌ Markets xato: {str(e)}")
        return []


def get_24h_tickers_batch(exchange):
    """Barcha 24h tickerlarni bir so'rovda olish"""
    try:
        tickers = exchange.fetch_tickers()
        return tickers
    except Exception as e:
        logging.error(f"❌ Tickers xato: {str(e)}")
        return {}


def filter_by_volume(tickers, min_volume=MIN_VOLUME_USD):
    """Hajm bo'yicha filtr"""
    filtered = {}
    for symbol, ticker in tickers.items():
        if symbol.endswith('/USDT'):
            volume_usd = ticker.get('quoteVolume', 0)
            if volume_usd >= min_volume:
                filtered[symbol] = ticker
    
    logging.info(f"✅ Hajm filtri: {len(filtered)} ta token qoldi")
    return filtered


# ============================================
# TEXNIK TAHLIL (OPTIMIZATSIYA)
# ============================================

def quick_technical_analysis(df):
    """Tez texnik tahlil (faqat zarur indikatorlar)"""
    try:
        # RSI
        rsi_indicator = RSIIndicator(close=df['close'], window=14)
        rsi = rsi_indicator.rsi().iloc[-1]
        
        # MACD
        macd_indicator = MACD(close=df['close'], window_slow=26, window_fast=12, window_sign=9)
        macd_hist = macd_indicator.macd_diff().iloc[-1]
        
        # ADX
        adx_indicator = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
        adx = adx_indicator.adx().iloc[-1]
        
        # EMA Trend
        df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
        
        ema_20 = df['ema_20'].iloc[-1]
        ema_50 = df['ema_50'].iloc[-1]
        current_price = df['close'].iloc[-1]
        
        # Trend
        if current_price > ema_20 > ema_50:
            trend = "BULLISH"
        elif current_price < ema_20 < ema_50:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"
        
        # Volume surge
        avg_volume = df['volume'].tail(20).mean()
        current_volume = df['volume'].iloc[-1]
        volume_surge = current_volume > (avg_volume * 2)
        
        return {
            'rsi': rsi,
            'macd_hist': macd_hist,
            'adx': adx,
            'trend': trend,
            'volume_surge': volume_surge,
            'price': current_price
        }
        
    except Exception as e:
        logging.error(f"❌ TA xato: {str(e)}")
        return None


def calculate_quick_score(analysis):
    """Tez scoring (sodda)"""
    score = 0
    
    rsi = analysis.get('rsi', 50)
    adx = analysis.get('adx', 0)
    macd_hist = analysis.get('macd_hist', 0)
    trend = analysis.get('trend', 'NEUTRAL')
    volume_surge = analysis.get('volume_surge', False)
    
    # RSI optimal: 40-60
    if 40 <= rsi <= 60:
        score += 2
    
    # ADX kuchli: >25
    if adx > 25:
        score += 2
    
    # MACD ijobiy
    if macd_hist > 0:
        score += 2
    
    # Trend kuchli
    if trend in ['BULLISH', 'BEARISH']:
        score += 2
    
    # Volume surge
    if volume_surge:
        score += 2
    
    return score


# ============================================
# SIGNAL GENERATSIYA (PARALLEL)
# ============================================

def analyze_single_symbol(symbol, exchange, ticker_data):
    """Bitta symbolni tahlil qilish"""
    try:
        # OHLCV olish
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LOOKBACK_CANDLES)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        if df.empty or len(df) < 30:
            return None
        
        # Tez tahlil
        analysis = quick_technical_analysis(df)
        if not analysis:
            return None
        
        # Score
        score = calculate_quick_score(analysis)
        
        # Filtrlar
        rsi = analysis['rsi']
        adx = analysis['adx']
        
        # RSI check
        if rsi > RSI_OVERBOUGHT or rsi < RSI_OVERSOLD:
            return None
        
        # ADX check
        if adx < MIN_ADX:
            return None
        
        # Score check
        if score < MIN_SIGNAL_SCORE:
            return None
        
        # Signal type
        trend = analysis['trend']
        macd_hist = analysis['macd_hist']
        
        signal_type = None
        if trend == 'BULLISH' and macd_hist > 0 and rsi < 60:
            signal_type = 'LONG'
        elif trend == 'BEARISH' and macd_hist < 0 and rsi > 40:
            signal_type = 'SHORT'
        
        if not signal_type:
            return None
        
        # Support/Resistance (sodda)
        high_20 = df['high'].tail(20).max()
        low_20 = df['low'].tail(20).min()
        current_price = analysis['price']
        
        if signal_type == 'LONG':
            entry = current_price
            stop_loss = low_20 * 0.98
            take_profit = high_20 * 1.02
        else:
            entry = current_price
            stop_loss = high_20 * 1.02
            take_profit = low_20 * 0.98
        
        # Signal obyekti
        signal = {
            'symbol': symbol,
            'type': signal_type,
            'timestamp': datetime.now(),
            'price': current_price,
            'score': score,
            'rsi': rsi,
            'adx': adx,
            'macd_hist': macd_hist,
            'trend': trend,
            'volume_surge': analysis['volume_surge'],
            'entry': entry,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'volume_24h': ticker_data.get('quoteVolume', 0)
        }
        
        return signal
        
    except Exception as e:
        # Silent fail (loglarni kamaytirish)
        return None


def analyze_batch_parallel(symbols_batch, exchange, tickers):
    """Batch symbollarni parallel tahlil qilish"""
    signals = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                analyze_single_symbol,
                symbol,
                exchange,
                tickers.get(symbol, {})
            ): symbol
            for symbol in symbols_batch
        }
        
        for future in as_completed(futures):
            try:
                signal = future.result(timeout=5)
                if signal:
                    signals.append(signal)
            except Exception:
                pass
    
    return signals


# ============================================
# TELEGRAM
# ============================================

def send_telegram_message(message):
    """Telegram xabar"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, json=payload, timeout=5)
        return response.status_code == 200
    except:
        return False


def format_short_signal(signal):
    """Qisqa format (ko'p signallar uchun)"""
    emoji = '🟢' if signal['type'] == 'LONG' else '🔴'
    
    message = f"""
{emoji} <b>{signal['type']}</b> | {signal['symbol']}

💰 Narx: ${signal['price']:.4f}
⭐ Score: {signal['score']}/10
📊 RSI: {signal['rsi']:.1f} | ADX: {signal['adx']:.1f}
📈 Trend: {signal['trend']}

📍 Entry: ${signal['entry']:.4f}
🛑 Stop: ${signal['stop_loss']:.4f}
🎯 Target: ${signal['take_profit']:.4f}

💵 24h Vol: ${signal['volume_24h']:,.0f}
⏰ {signal['timestamp'].strftime('%H:%M:%S')}
"""
    return message


def send_signal(signal):
    """Signal yuborish"""
    message = format_short_signal(signal)
    success = send_telegram_message(message)
    if success:
        logging.info(f"✅ Signal: {signal['symbol']} - {signal['type']}")
    return success


# ============================================
# ASOSIY SIKL
# ============================================

def main():
    """Asosiy dastur"""
    try:
        logging.info("="*60)
        logging.info("🚀 600+ TOKEN SIGNAL BOT ISHGA TUSHDI")
        logging.info("="*60)
        
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("❌ Telegram sozlanmagan!")
            return
        
        exchange = get_exchange_connection()
        if not exchange:
            return
        
        send_telegram_message("✅ Bot 600+ token monitoring boshlanmoqda...")
        
        # Asosiy sikl
        while True:
            try:
                cycle_start = time.time()
                logging.info(f"\n{'='*60}")
                logging.info(f"🔄 Yangi sikl: {datetime.now().strftime('%H:%M:%S')}")
                logging.info(f"{'='*60}")
                
                # 1. Barcha tickerlarni olish
                logging.info("📊 Tickerlar yuklanmoqda...")
                tickers = get_24h_tickers_batch(exchange)
                
                if not tickers:
                    logging.warning("⚠️ Tickerlar yuklanmadi")
                    time.sleep(60)
                    continue
                
                # 2. Hajm bo'yicha filtr
                filtered_tickers = filter_by_volume(tickers, MIN_VOLUME_USD)
                symbols_to_analyze = list(filtered_tickers.keys())
                
                if not symbols_to_analyze:
                    logging.warning("⚠️ Hajm filtridan token o'tmadi")
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                logging.info(f"🔍 {len(symbols_to_analyze)} ta token tahlil qilinadi")
                
                # 3. Batch processing
                all_signals = []
                
                for i in range(0, len(symbols_to_analyze), BATCH_SIZE):
                    batch = symbols_to_analyze[i:i+BATCH_SIZE]
                    batch_num = i // BATCH_SIZE + 1
                    total_batches = (len(symbols_to_analyze) + BATCH_SIZE - 1) // BATCH_SIZE
                    
                    logging.info(f"⚡ Batch {batch_num}/{total_batches}: {len(batch)} token")
                    
                    batch_signals = analyze_batch_parallel(batch, exchange, filtered_tickers)
                    all_signals.extend(batch_signals)
                    
                    time.sleep(1)  # Rate limit
                
                # 4. Signallarni yuborish
                if all_signals:
                    logging.info(f"🎯 {len(all_signals)} ta signal topildi!")
                    
                    # Score bo'yicha saralash (eng kuchlisi birinchi)
                    all_signals.sort(key=lambda x: x['score'], reverse=True)
                    
                    # Faqat top 10 ni yuborish (spam oldini olish)
                    top_signals = all_signals[:10]
                    
                    for signal in top_signals:
                        send_signal(signal)
                        time.sleep(1)
                    
                    if len(all_signals) > 10:
                        summary = f"📊 Jami {len(all_signals)} signal, top 10 yuborildi"
                        send_telegram_message(summary)
                else:
                    logging.info("ℹ️ Hech qanday signal topilmadi")
                
                # 5. Statistika
                cycle_time = time.time() - cycle_start
                logging.info(f"\n⏱️ Sikl vaqti: {cycle_time:.1f}s")
                logging.info(f"📊 Tahlil: {len(symbols_to_analyze)} token")
                logging.info(f"✅ Signallar: {len(all_signals)}")
                
                # 6. Keyingi sikl
                wait_time = max(CHECK_INTERVAL - cycle_time, 10)
                logging.info(f"⏳ Keyingi sikl: {wait_time:.0f}s\n")
                time.sleep(wait_time)
                
            except KeyboardInterrupt:
                logging.info("\n👋 To'xtatildi")
                send_telegram_message("⛔ Bot to'xtatildi")
                break
            except Exception as e:
                logging.error(f"❌ Sikl xato: {str(e)}")
                time.sleep(30)
                
    except Exception as e:
        logging.error(f"❌ Fatal xato: {str(e)}")


if __name__ == "__main__":
    main()
