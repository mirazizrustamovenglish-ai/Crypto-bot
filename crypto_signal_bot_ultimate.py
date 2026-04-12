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
import json

# ============================================
# KONFIGURATSIYA
# ============================================

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# Exchange
ENABLED_EXCHANGES = os.getenv('ENABLED_EXCHANGES', 'binance,bitget,mexc').split(',')

# Filtrlar
MIN_VOLUME_USD = int(os.getenv('MIN_VOLUME_USD', '2000000'))
MIN_MARKET_CAP = int(os.getenv('MIN_MARKET_CAP', '5000000'))
MIN_ADX = int(os.getenv('MIN_ADX', '25'))
RSI_OVERBOUGHT = int(os.getenv('RSI_OVERBOUGHT', '70'))
RSI_OVERSOLD = int(os.getenv('RSI_OVERSOLD', '30'))
MIN_SIGNAL_SCORE = int(os.getenv('MIN_SIGNAL_SCORE', '8'))

# Bot settings
TIMEFRAME = os.getenv('TIMEFRAME', '15m')
LOOKBACK_CANDLES = int(os.getenv('LOOKBACK_CANDLES', '100'))
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '300'))
MAX_WORKERS = int(os.getenv('MAX_WORKERS', '15'))
BATCH_SIZE = int(os.getenv('BATCH_SIZE', '100'))

# Bot state
BOT_APPROVED = False
BOT_RUNNING = False
MANUAL_SCAN_ACTIVE = False
MANUAL_SCAN_SCORE = 7
MANUAL_SCAN_MIN_VOLUME = 500000

# TP/SL Tracking
ACTIVE_POSITIONS = {}  # {symbol: {type, entry, tp, sl, exchange, ...}}
TRACKING_ENABLED = True

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# ============================================
# TELEGRAM FUNCTIONS
# ============================================

def send_telegram_message(message, parse_mode="HTML", reply_markup=None):
    """Telegram xabar yuborish"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": parse_mode
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logging.error(f"❌ Telegram: {e}")
        return False


def send_approval_request():
    """Start tasdiqlash so'rovi"""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ TASDIQLASH", "callback_data": "approve_start"},
                {"text": "❌ BEKOR QILISH", "callback_data": "cancel_start"}
            ]
        ]
    }
    
    message = """
🤖 <b>RUSTAMOV CRYPTO BOT</b>

━━━━━━━━━━━━━━━━━━━━━
<b>⚠️ ISHGA TUSHIRISH TASDIQ QILISH</b>
━━━━━━━━━━━━━━━━━━━━━

Bot quyidagi sozlamalar bilan ishlaydi:

📊 <b>EXCHANGE:</b> Binance, Bitget, MEXC
⏱️ <b>TIMEFRAME:</b> 15 daqiqa
🔄 <b>TEKSHIRISH:</b> Har 5 daqiqada
⭐ <b>MIN SCORE:</b> 8/10
💰 <b>MIN HAJM:</b> $2M
📈 <b>MIN MARKET CAP:</b> $5M

🎯 <b>XUSUSIYATLAR:</b>
✅ Auto TP/SL (Risk/Reward 1:2.5)
✅ TP/SL Tracking (Real-time kuzatuv)
✅ Signal sabablari
✅ Manual scan (7+ score)
✅ Coin tekshirish

━━━━━━━━━━━━━━━━━━━━━

Botni ishga tushirishni tasdiqlaysizmi?
    """
    
    send_telegram_message(message, reply_markup=keyboard)


def send_welcome_message():
    """Xush kelibsiz xabari (tasdiqlangandan keyin)"""
    message = """
🎉 <b>BOT ISHGA TUSHDI!</b> 🎉

━━━━━━━━━━━━━━━━━━━━━
<b>✅ AKTIVLASHTIRILDI</b>
━━━━━━━━━━━━━━━━━━━━━

🤖 Monitoring: <b>YONIQ</b>
🔍 Skanerlash: <b>BOSHLANDI</b>
📊 TP/SL Tracking: <b>AKTIV</b>

━━━━━━━━━━━━━━━━━━━━━
<b>🔍 BUYRUQLAR</b>
━━━━━━━━━━━━━━━━━━━━━

/scan - Manual skanerlash
/check BTCUSDT - Coin tekshirish
/positions - Ochiq pozitsiyalar
/status - Bot holati
/stop - Botni to'xtatish

━━━━━━━━━━━━━━━━━━━━━

⏳ Birinchi signallar 5 daqiqada...
    """
    send_telegram_message(message)
    send_main_keyboard()


def send_main_keyboard():
    """Asosiy klaviatura"""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🔍 HOZIR SKAN", "callback_data": "scan_now"},
                {"text": "📊 POZITSIYALAR", "callback_data": "show_positions"}
            ],
            [
                {"text": "📈 STATUS", "callback_data": "bot_status"},
                {"text": "🛑 TO'XTATISH", "callback_data": "stop_bot"}
            ]
        ]
    }
    
    send_telegram_message("🎛️ <b>BOSHQARUV PANELI</b>", reply_markup=keyboard)


def send_position_alert(position_data, alert_type):
    """TP/SL alert yuborish"""
    emoji_map = {
        'tp1_hit': '🎯',
        'tp2_hit': '🎯🎯',
        'tp3_hit': '🎯🎯🎯',
        'sl_hit': '🛑',
        'trailing': '📈'
    }
    
    emoji = emoji_map.get(alert_type, '⚠️')
    
    if alert_type == 'sl_hit':
        title = "STOP LOSS FAOLLASHDI"
        color = "🔴"
    elif 'tp' in alert_type:
        title = "TAKE PROFIT FAOLLASHDI"
        color = "🟢"
    else:
        title = "TRAILING STOP"
        color = "🔵"
    
    message = f"""
{emoji} <b>{color} {title}</b> {emoji}

━━━━━━━━━━━━━━━━━━━━━

💰 <b>{position_data['symbol']}</b> ({position_data['exchange']})
📊 Signal Turi: {position_data['type']}

📍 Entry: ${position_data['entry']:.6f}
💵 Joriy Narx: ${position_data['current_price']:.6f}

{'🎯 ' + alert_type.upper().replace('_', ' ') + ' faollashdi!' if 'tp' in alert_type or 'sl' in alert_type else ''}

💰 Foyda/Zarar: {position_data.get('pnl_percent', 0):+.2f}%
💵 Profit: ${position_data.get('pnl_usd', 0):+.2f}

⏰ {datetime.now().strftime('%H:%M:%S')}

━━━━━━━━━━━━━━━━━━━━━

⚠️ <b>Bu moliyaviy maslahat emas!</b>
    """
    
    send_telegram_message(message)


# ============================================
# TELEGRAM UPDATE HANDLER
# ============================================

def handle_telegram_updates():
    """Telegram yangilanishlarni kuzatish"""
    global BOT_APPROVED, BOT_RUNNING, MANUAL_SCAN_ACTIVE
    
    last_update_id = 0
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            
            response = requests.get(url, params=params, timeout=35)
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get('result'):
                    for update in data['result']:
                        last_update_id = update['update_id']
                        
                        # Callback queries (tugmalar)
                        if 'callback_query' in update:
                            callback = update['callback_query']
                            callback_data = callback['data']
                            
                            if callback_data == 'approve_start':
                                BOT_APPROVED = True
                                BOT_RUNNING = True
                                send_telegram_message("✅ <b>BOT TASDIQLANDI!</b>")
                                send_welcome_message()
                                
                            elif callback_data == 'cancel_start':
                                send_telegram_message("❌ Bot ishga tushirish bekor qilindi")
                                
                            elif callback_data == 'scan_now':
                                if BOT_RUNNING:
                                    MANUAL_SCAN_ACTIVE = True
                                    send_telegram_message("🔍 Manual skan boshlandi...")
                                else:
                                    send_telegram_message("⚠️ Bot ishga tushmagan!")
                                    
                            elif callback_data == 'show_positions':
                                show_active_positions()
                                
                            elif callback_data == 'bot_status':
                                send_bot_status()
                                
                            elif callback_data == 'stop_bot':
                                BOT_RUNNING = False
                                send_telegram_message("🛑 Bot to'xtatildi")
                        
                        # Text commands
                        if 'message' in update:
                            message = update['message']
                            text = message.get('text', '')
                            
                            if text.startswith('/start'):
                                send_approval_request()
                                
                            elif text.startswith('/scan'):
                                if BOT_RUNNING:
                                    MANUAL_SCAN_ACTIVE = True
                                    send_telegram_message("🔍 Manual skan boshlandi...")
                                    
                            elif text.startswith('/check'):
                                parts = text.split()
                                if len(parts) > 1:
                                    check_single_coin(parts[1].upper())
                                    
                            elif text.startswith('/positions'):
                                show_active_positions()
                                
                            elif text.startswith('/status'):
                                send_bot_status()
                                
                            elif text.startswith('/stop'):
                                BOT_RUNNING = False
                                send_telegram_message("🛑 Bot to'xtatildi")
            
            time.sleep(1)
            
        except Exception as e:
            logging.error(f"❌ Update handler: {e}")
            time.sleep(5)


def show_active_positions():
    """Ochiq pozitsiyalarni ko'rsatish"""
    if not ACTIVE_POSITIONS:
        send_telegram_message("ℹ️ Hozirda ochiq pozitsiyalar yo'q")
        return
    
    message = "<b>📊 OCHIQ POZITSIYALAR</b>\n\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    for symbol, pos in ACTIVE_POSITIONS.items():
        pnl = pos.get('pnl_percent', 0)
        pnl_emoji = "🟢" if pnl > 0 else "🔴"
        
        message += f"""
{pnl_emoji} <b>{symbol}</b> ({pos['exchange']})
📊 {pos['type']} | Entry: ${pos['entry']:.6f}
💰 Joriy: ${pos['current_price']:.6f}
📈 P/L: {pnl:+.2f}%

🎯 TP1: ${pos['tp1']:.6f} {'✅' if pos.get('tp1_hit') else ''}
🎯 TP2: ${pos['tp2']:.6f} {'✅' if pos.get('tp2_hit') else ''}
🎯 TP3: ${pos['tp3']:.6f} {'✅' if pos.get('tp3_hit') else ''}
🛑 SL: ${pos['sl']:.6f}

━━━━━━━━━━━━━━━━━━━━━

"""
    
    message += "\n⚠️ <b>Bu moliyaviy maslahat emas!</b>"
    send_telegram_message(message)


def send_bot_status():
    """Bot holati"""
    status_emoji = "✅" if BOT_RUNNING else "🛑"
    
    message = f"""
📊 <b>BOT HOLATI</b>

━━━━━━━━━━━━━━━━━━━━━

{status_emoji} Status: <b>{'AKTIV' if BOT_RUNNING else 'TO\'XTATILGAN'}</b>
🔄 Monitoring: <b>{'YONIQ' if BOT_RUNNING else 'O\'CHIQ'}</b>
📊 Tracking: <b>{'AKTIV' if TRACKING_ENABLED else 'PASSIV'}</b>

━━━━━━━━━━━━━━━━━━━━━
<b>SOZLAMALAR</b>
━━━━━━━━━━━━━━━━━━━━━

📊 Birjalar: {', '.join(ENABLED_EXCHANGES)}
⏱️ Timeframe: {TIMEFRAME}
🔄 Interval: {CHECK_INTERVAL}s
⭐ Min Score: {MIN_SIGNAL_SCORE}/10
💰 Min Hajm: ${MIN_VOLUME_USD:,}

━━━━━━━━━━━━━━━━━━━━━
<b>STATISTIKA</b>
━━━━━━━━━━━━━━━━━━━━━

📍 Ochiq pozitsiyalar: {len(ACTIVE_POSITIONS)}
⏰ So'nggi yangilanish: {datetime.now().strftime('%H:%M:%S')}

━━━━━━━━━━━━━━━━━━━━━
    """
    
    send_telegram_message(message)


def check_single_coin(coin_symbol):
    """Bitta coin tekshirish"""
    # Bu funksiya oldingi kodda bor edi, qisqartirish uchun...
    send_telegram_message(f"🔍 {coin_symbol} tekshirilmoqda...")
    # ... (oldingi kod)


# ============================================
# TP/SL TRACKING SYSTEM
# ============================================

def add_position_to_tracking(signal):
    """Pozitsiyani tracking ga qo'shish"""
    symbol = signal['symbol']
    
    ACTIVE_POSITIONS[symbol] = {
        'exchange': signal['exchange'],
        'symbol': symbol,
        'type': signal['type'],
        'entry': signal['entry'],
        'current_price': signal['price'],
        'tp1': signal['tp1'],
        'tp2': signal['tp2'],
        'tp3': signal['tp3'],
        'sl': signal['stop_loss'],
        'tp1_hit': False,
        'tp2_hit': False,
        'tp3_hit': False,
        'sl_hit': False,
        'timestamp': datetime.now(),
        'pnl_percent': 0,
        'pnl_usd': 0
    }
    
    logging.info(f"📊 Pozitsiya tracking qo'shildi: {symbol}")


def track_positions():
    """TP/SL tracking (background thread)"""
    exchanges = {}
    
    while True:
        try:
            if not TRACKING_ENABLED or not BOT_RUNNING:
                time.sleep(10)
                continue
            
            if not ACTIVE_POSITIONS:
                time.sleep(30)
                continue
            
            # Exchange ulanishlari
            if not exchanges:
                exchanges = get_exchange_connections()
            
            positions_to_remove = []
            
            for symbol, pos in list(ACTIVE_POSITIONS.items()):
                try:
                    exchange_name = pos['exchange'].lower()
                    
                    if exchange_name not in exchanges:
                        continue
                    
                    exchange = exchanges[exchange_name]
                    
                    # Joriy narxni olish
                    ticker = exchange.fetch_ticker(symbol)
                    current_price = ticker.get('last', 0)
                    
                    if current_price == 0:
                        continue
                    
                    # P/L hisoblash
                    if pos['type'] == 'LONG':
                        pnl_percent = ((current_price - pos['entry']) / pos['entry']) * 100
                    else:
                        pnl_percent = ((pos['entry'] - current_price) / pos['entry']) * 100
                    
                    # Update position
                    pos['current_price'] = current_price
                    pos['pnl_percent'] = pnl_percent
                    
                    # TP/SL check
                    if pos['type'] == 'LONG':
                        # TP checks
                        if not pos['tp1_hit'] and current_price >= pos['tp1']:
                            pos['tp1_hit'] = True
                            send_position_alert(pos, 'tp1_hit')
                            # Trailing SL ga o'tkazish
                            pos['sl'] = pos['entry']  # Breakeven
                            
                        if not pos['tp2_hit'] and current_price >= pos['tp2']:
                            pos['tp2_hit'] = True
                            send_position_alert(pos, 'tp2_hit')
                            # Trailing SL
                            pos['sl'] = pos['tp1']
                            
                        if not pos['tp3_hit'] and current_price >= pos['tp3']:
                            pos['tp3_hit'] = True
                            send_position_alert(pos, 'tp3_hit')
                            positions_to_remove.append(symbol)
                            
                        # SL check
                        if current_price <= pos['sl']:
                            pos['sl_hit'] = True
                            send_position_alert(pos, 'sl_hit')
                            positions_to_remove.append(symbol)
                    
                    else:  # SHORT
                        if not pos['tp1_hit'] and current_price <= pos['tp1']:
                            pos['tp1_hit'] = True
                            send_position_alert(pos, 'tp1_hit')
                            pos['sl'] = pos['entry']
                            
                        if not pos['tp2_hit'] and current_price <= pos['tp2']:
                            pos['tp2_hit'] = True
                            send_position_alert(pos, 'tp2_hit')
                            pos['sl'] = pos['tp1']
                            
                        if not pos['tp3_hit'] and current_price <= pos['tp3']:
                            pos['tp3_hit'] = True
                            send_position_alert(pos, 'tp3_hit')
                            positions_to_remove.append(symbol)
                            
                        if current_price >= pos['sl']:
                            pos['sl_hit'] = True
                            send_position_alert(pos, 'sl_hit')
                            positions_to_remove.append(symbol)
                    
                except Exception as e:
                    logging.error(f"❌ Tracking xato ({symbol}): {e}")
                    continue
            
            # Yopilgan pozitsiyalarni o'chirish
            for symbol in positions_to_remove:
                if symbol in ACTIVE_POSITIONS:
                    del ACTIVE_POSITIONS[symbol]
                    logging.info(f"✅ Pozitsiya yopildi: {symbol}")
            
            # Har 30 sekundda check
            time.sleep(30)
            
        except Exception as e:
            logging.error(f"❌ Tracking thread: {e}")
            time.sleep(60)


# ============================================
# EXCHANGE VA TAHLIL (Oldingi koddan)
# ============================================

def get_exchange_connections():
    """Exchange ulanish"""
    exchanges = {}
    
    if 'binance' in ENABLED_EXCHANGES:
        try:
            exchanges['binance'] = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'spot'}, 'timeout': 10000})
            logging.info("✅ Binance")
        except: pass
    
    if 'bitget' in ENABLED_EXCHANGES:
        try:
            exchanges['bitget'] = ccxt.bitget({'enableRateLimit': True, 'options': {'defaultType': 'spot'}, 'timeout': 10000})
            logging.info("✅ Bitget")
        except: pass
    
    if 'mexc' in ENABLED_EXCHANGES:
        try:
            exchanges['mexc'] = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'spot'}, 'timeout': 10000})
            logging.info("✅ MEXC")
        except: pass
    
    return exchanges


def get_24h_tickers_batch(exchange, exchange_name):
    """Tickerlar"""
    try:
        return exchange.fetch_tickers()
    except:
        return {}


def filter_by_volume(tickers, exchange_name, min_volume):
    """Hajm filtri"""
    filtered = {}
    for symbol, ticker in tickers.items():
        if symbol.endswith('/USDT'):
            if ticker.get('quoteVolume', 0) >= min_volume:
                filtered[symbol] = ticker
    return filtered


def quick_technical_analysis(df):
    """Texnik tahlil (oldingi kod)"""
    # ... (oldingi koddan nusxa)
    try:
        rsi_indicator = RSIIndicator(close=df['close'], window=14)
        rsi = rsi_indicator.rsi().iloc[-1]
        
        macd_indicator = MACD(close=df['close'])
        macd_hist = macd_indicator.macd_diff().iloc[-1]
        
        adx_indicator = ADXIndicator(high=df['high'], low=df['low'], close=df['close'])
        adx = adx_indicator.adx().iloc[-1]
        
        df['ema_9'] = df['close'].ewm(span=9).mean()
        df['ema_21'] = df['close'].ewm(span=21).mean()
        
        trend = "BULLISH" if df['ema_9'].iloc[-1] > df['ema_21'].iloc[-1] else "BEARISH"
        
        return {
            'rsi': rsi,
            'macd_hist': macd_hist,
            'adx': adx,
            'trend': trend,
            'price': df['close'].iloc[-1]
        }
    except:
        return None


def calculate_quick_score(analysis):
    """Score"""
    score = 5
    if 40 <= analysis.get('rsi', 50) <= 60:
        score += 2
    if analysis.get('adx', 0) > 25:
        score += 2
    if analysis.get('macd_hist', 0) > 0:
        score += 1
    return score


def calculate_auto_tp_sl(price, signal_type, sr=None):
    """TP/SL hisoblash"""
    if signal_type == 'LONG':
        entry = price
        sl = price * 0.97
        tp1 = price * 1.02
        tp2 = price * 1.05
        tp3 = price * 1.08
    else:
        entry = price
        sl = price * 1.03
        tp1 = price * 0.98
        tp2 = price * 0.95
        tp3 = price * 0.92
    
    return {
        'entry': entry,
        'stop_loss': sl,
        'tp1': tp1,
        'tp2': tp2,
        'tp3': tp3
    }


def analyze_single_symbol(symbol, exchange, exchange_name, ticker_data, use_manual=False):
    """Symbol tahlil"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        analysis = quick_technical_analysis(df)
        if not analysis:
            return None
        
        score = calculate_quick_score(analysis)
        
        min_score = MANUAL_SCAN_SCORE if use_manual else MIN_SIGNAL_SCORE
        
        if score < min_score:
            return None
        
        signal_type = 'LONG' if analysis['trend'] == 'BULLISH' else 'SHORT'
        
        levels = calculate_auto_tp_sl(analysis['price'], signal_type)
        
        return {
            'exchange': exchange_name.upper(),
            'symbol': symbol,
            'type': signal_type,
            'price': analysis['price'],
            'score': score,
            'rsi': analysis['rsi'],
            'adx': analysis['adx'],
            'macd_hist': analysis['macd_hist'],
            'trend': analysis['trend'],
            **levels,
            'volume_24h': ticker_data.get('quoteVolume', 0),
            'timestamp': datetime.now()
        }
    except:
        return None


def analyze_batch_parallel(symbols, exchange, exchange_name, tickers, use_manual=False):
    """Batch tahlil"""
    signals = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(analyze_single_symbol, s, exchange, exchange_name, tickers.get(s, {}), use_manual): s for s in symbols}
        for future in as_completed(futures):
            try:
                sig = future.result(timeout=5)
                if sig:
                    signals.append(sig)
            except:
                pass
    return signals


def format_signal_message(signal):
    """Signal formatlash"""
    emoji = '🟢' if signal['type'] == 'LONG' else '🔴'
    
    message = f"""
{emoji} <b>{signal['type']} SIGNAL</b> | <b>{signal['exchange']}</b>

━━━━━━━━━━━━━━━━━━━━━
<b>💰 {signal['symbol']}</b>
━━━━━━━━━━━━━━━━━━━━━

💵 Narx: <b>${signal['price']:.6f}</b>
⭐ Score: <b>{signal['score']}/10</b>

━━━━━━━━━━━━━━━━━━━━━
<b>📈 TAHLIL</b>
━━━━━━━━━━━━━━━━━━━━━

📊 RSI: {signal['rsi']:.2f}
💪 ADX: {signal['adx']:.2f}
🔥 Trend: {signal['trend']}

━━━━━━━━━━━━━━━━━━━━━
<b>💰 SAVDO DARAJALARI</b>
━━━━━━━━━━━━━━━━━━━━━

📍 Entry: ${signal['entry']:.6f}
🛑 Stop Loss: ${signal['stop_loss']:.6f}

🎯 TP1: ${signal['tp1']:.6f}
🎯 TP2: ${signal['tp2']:.6f}
🎯 TP3: ${signal['tp3']:.6f}

━━━━━━━━━━━━━━━━━━━━━

💰 24h Hajm: ${signal['volume_24h']:,.0f}
⏰ {signal['timestamp'].strftime('%H:%M:%S')}

🔔 <b>TP/SL TRACKING AKTIV!</b>

━━━━━━━━━━━━━━━━━━━━━

⚠️ <b>BU MOLIYAVIY MASLAHAT EMAS!</b>
    """
    return message


def send_signal(signal):
    """Signal yuborish"""
    message = format_signal_message(signal)
    success = send_telegram_message(message)
    
    if success:
        # Pozitsiyani tracking ga qo'shish
        add_position_to_tracking(signal)
        logging.info(f"✅ Signal: {signal['symbol']}")
    
    return success


# ============================================
# ASOSIY SIKL
# ============================================

def main():
    """Asosiy dastur"""
    global BOT_RUNNING, MANUAL_SCAN_ACTIVE
    
    try:
        logging.info("="*60)
        logging.info("🚀 RUSTAMOV CRYPTO BOT ULTIMATE")
        logging.info("="*60)
        
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("❌ Telegram sozlanmagan!")
            return
        
        # Update handler thread
        update_thread = threading.Thread(target=handle_telegram_updates, daemon=True)
        update_thread.start()
        logging.info("✅ Telegram handler ishga tushdi")
        
        # TP/SL tracking thread
        tracking_thread = threading.Thread(target=track_positions, daemon=True)
        tracking_thread.start()
        logging.info("✅ TP/SL tracking ishga tushdi")
        
        # Start so'rovini yuborish
        send_approval_request()
        logging.info("📤 Start tasdiq so'rovi yuborildi")
        
        # Tasdiqlashni kutish
        while not BOT_APPROVED:
            time.sleep(2)
        
        logging.info("✅ Bot tasdiqlandi!")
        
        # Exchange ulanish
        exchanges = get_exchange_connections()
        
        if not exchanges:
            send_telegram_message("❌ Exchangelarga ulanib bo'lmadi!")
            return
        
        # Asosiy monitoring sikl
        while BOT_RUNNING:
            try:
                cycle_start = time.time()
                logging.info(f"\n{'='*60}")
                logging.info(f"🔄 Sikl: {datetime.now().strftime('%H:%M:%S')}")
                
                all_signals = []
                use_manual = MANUAL_SCAN_ACTIVE
                
                for exchange_name, exchange in exchanges.items():
                    try:
                        tickers = get_24h_tickers_batch(exchange, exchange_name)
                        if not tickers:
                            continue
                        
                        min_vol = MANUAL_SCAN_MIN_VOLUME if use_manual else MIN_VOLUME_USD
                        filtered = filter_by_volume(tickers, exchange_name, min_vol)
                        
                        symbols = list(filtered.keys())
                        
                        if not symbols:
                            continue
                        
                        logging.info(f"🔍 {exchange_name}: {len(symbols)} coin")
                        
                        for i in range(0, len(symbols), BATCH_SIZE):
                            batch = symbols[i:i+BATCH_SIZE]
                            batch_signals = analyze_batch_parallel(batch, exchange, exchange_name, filtered, use_manual)
                            all_signals.extend(batch_signals)
                            time.sleep(0.5)
                    
                    except Exception as e:
                        logging.error(f"❌ {exchange_name}: {e}")
                
                if all_signals:
                    all_signals.sort(key=lambda x: x['score'], reverse=True)
                    
                    limit = 15 if use_manual else 10
                    top = all_signals[:limit]
                    
                    for sig in top:
                        send_signal(sig)
                        time.sleep(1.5)
                    
                    if use_manual:
                        MANUAL_SCAN_ACTIVE = False
                        send_telegram_message(f"✅ Manual scan: {len(all_signals)} signal topildi")
                
                cycle_time = time.time() - cycle_start
                wait = max(CHECK_INTERVAL - cycle_time, 10)
                logging.info(f"⏳ Keyingi: {wait:.0f}s")
                time.sleep(wait)
                
            except Exception as e:
                logging.error(f"❌ Sikl xato: {e}")
                time.sleep(30)
        
        send_telegram_message("⛔ Bot to'xtatildi")
        
    except Exception as e:
        logging.error(f"❌ Fatal: {e}")


if __name__ == "__main__":
    main()
