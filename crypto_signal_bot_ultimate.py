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
ACTIVE_POSITIONS = {}
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
        logging.error(f"Telegram xato: {e}")
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
    
    message = (
        "🤖 <b>RUSTAMOV CRYPTO BOT</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>⚠️ ISHGA TUSHIRISH TASDIQ QILISH</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Bot quyidagi sozlamalar bilan ishlaydi:\n\n"
        "📊 <b>EXCHANGE:</b> Binance, Bitget, MEXC\n"
        "⏱️ <b>TIMEFRAME:</b> 15 daqiqa\n"
        "🔄 <b>TEKSHIRISH:</b> Har 5 daqiqada\n"
        "⭐ <b>MIN SCORE:</b> 8/10\n"
        "💰 <b>MIN HAJM:</b> $2M\n"
        "📈 <b>MIN MARKET CAP:</b> $5M\n\n"
        "🎯 <b>XUSUSIYATLAR:</b>\n"
        "✅ Auto TP/SL (Risk/Reward 1:2.5)\n"
        "✅ TP/SL Tracking (Real-time kuzatuv)\n"
        "✅ Signal sabablari\n"
        "✅ Manual scan (7+ score)\n"
        "✅ Coin tekshirish\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Botni ishga tushirishni tasdiqlaysizmi?"
    )
    
    send_telegram_message(message, reply_markup=keyboard)


def send_welcome_message():
    """Xush kelibsiz xabari"""
    message = (
        "🎉 <b>BOT ISHGA TUSHDI!</b> 🎉\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>✅ AKTIVLASHTIRILDI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 Monitoring: <b>YONIQ</b>\n"
        "🔍 Skanerlash: <b>BOSHLANDI</b>\n"
        "📊 TP/SL Tracking: <b>AKTIV</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🔍 BUYRUQLAR</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "/scan - Manual skanerlash\n"
        "/check BTCUSDT - Coin tekshirish\n"
        "/positions - Ochiq pozitsiyalar\n"
        "/status - Bot holati\n"
        "/stop - Botni to'xtatish\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏳ Birinchi signallar 5 daqiqada..."
    )
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


def show_active_positions():
    """Ochiq pozitsiyalarni ko'rsatish"""
    if not ACTIVE_POSITIONS:
        send_telegram_message("ℹ️ Hozirda ochiq pozitsiyalar yo'q")
        return
    
    message_parts = ["<b>📊 OCHIQ POZITSIYALAR</b>\n\n", "━━━━━━━━━━━━━━━━━━━━━\n\n"]
    
    for symbol, pos in ACTIVE_POSITIONS.items():
        pnl = pos.get('pnl_percent', 0)
        pnl_emoji = "🟢" if pnl > 0 else "🔴"
        
        tp1_status = '✅' if pos.get('tp1_hit') else ''
        tp2_status = '✅' if pos.get('tp2_hit') else ''
        tp3_status = '✅' if pos.get('tp3_hit') else ''
        
        position_text = (
            f"{pnl_emoji} <b>{symbol}</b> ({pos['exchange']})\n"
            f"📊 {pos['type']} | Entry: ${pos['entry']:.6f}\n"
            f"💰 Joriy: ${pos['current_price']:.6f}\n"
            f"📈 P/L: {pnl:+.2f}%\n\n"
            f"🎯 TP1: ${pos['tp1']:.6f} {tp1_status}\n"
            f"🎯 TP2: ${pos['tp2']:.6f} {tp2_status}\n"
            f"🎯 TP3: ${pos['tp3']:.6f} {tp3_status}\n"
            f"🛑 SL: ${pos['sl']:.6f}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        
        message_parts.append(position_text)
    
    message_parts.append("⚠️ <b>Bu moliyaviy maslahat emas!</b>")
    send_telegram_message(''.join(message_parts))


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
    
    alert_description = alert_type.upper().replace('_', ' ')
    
    message_parts = [
        f"{emoji} <b>{color} {title}</b> {emoji}\n\n",
        "━━━━━━━━━━━━━━━━━━━━━\n\n",
        f"💰 <b>{position_data['symbol']}</b> ({position_data['exchange']})\n",
        f"📊 Signal Turi: {position_data['type']}\n\n",
        f"📍 Entry: ${position_data['entry']:.6f}\n",
        f"💵 Joriy Narx: ${position_data['current_price']:.6f}\n\n"
    ]
    
    if 'tp' in alert_type or 'sl' in alert_type:
        message_parts.append(f"🎯 {alert_description} faollashdi!\n\n")
    
    message_parts.extend([
        f"💰 Foyda/Zarar: {position_data.get('pnl_percent', 0):+.2f}%\n",
        f"💵 Profit: ${position_data.get('pnl_usd', 0):+.2f}\n\n",
        f"⏰ {datetime.now().strftime('%H:%M:%S')}\n\n",
        "━━━━━━━━━━━━━━━━━━━━━\n\n",
        "⚠️ <b>Bu moliyaviy maslahat emas!</b>"
    ])
    
    send_telegram_message(''.join(message_parts))


def send_bot_status():
    """Bot holati"""
    status_emoji = "✅" if BOT_RUNNING else "🛑"
    status_text = "AKTIV" if BOT_RUNNING else "TO'XTATILGAN"
    monitoring = "YONIQ" if BOT_RUNNING else "O'CHIQ"
    tracking = "AKTIV" if TRACKING_ENABLED else "PASSIV"
    
    exchanges_text = ', '.join(ENABLED_EXCHANGES)
    
    message = (
        "📊 <b>BOT HOLATI</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{status_emoji} Status: <b>{status_text}</b>\n"
        f"🔄 Monitoring: <b>{monitoring}</b>\n"
        f"📊 Tracking: <b>{tracking}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>SOZLAMALAR</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Birjalar: {exchanges_text}\n"
        f"⏱️ Timeframe: {TIMEFRAME}\n"
        f"🔄 Interval: {CHECK_INTERVAL}s\n"
        f"⭐ Min Score: {MIN_SIGNAL_SCORE}/10\n"
        f"💰 Min Hajm: ${MIN_VOLUME_USD:,}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>STATISTIKA</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📍 Ochiq pozitsiyalar: {len(ACTIVE_POSITIONS)}\n"
        f"⏰ So'nggi yangilanish: {datetime.now().strftime('%H:%M:%S')}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )
    
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
                        
                        # Callback queries
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
                                    
                            elif text.startswith('/positions'):
                                show_active_positions()
                                
                            elif text.startswith('/status'):
                                send_bot_status()
                                
                            elif text.startswith('/stop'):
                                BOT_RUNNING = False
                                send_telegram_message("🛑 Bot to'xtatildi")
            
            time.sleep(1)
            
        except Exception as e:
            logging.error(f"Update handler xato: {e}")
            time.sleep(5)


# ============================================
# TP/SL TRACKING
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
    
    logging.info(f"Pozitsiya qo'shildi: {symbol}")


def track_positions():
    """TP/SL tracking thread"""
    exchanges = {}
    
    while True:
        try:
            if not TRACKING_ENABLED or not BOT_RUNNING:
                time.sleep(10)
                continue
            
            if not ACTIVE_POSITIONS:
                time.sleep(30)
                continue
            
            if not exchanges:
                exchanges = get_exchange_connections()
            
            positions_to_remove = []
            
            for symbol, pos in list(ACTIVE_POSITIONS.items()):
                try:
                    exchange_name = pos['exchange'].lower()
                    
                    if exchange_name not in exchanges:
                        continue
                    
                    exchange = exchanges[exchange_name]
                    
                    ticker = exchange.fetch_ticker(symbol)
                    current_price = ticker.get('last', 0)
                    
                    if current_price == 0:
                        continue
                    
                    # P/L
                    if pos['type'] == 'LONG':
                        pnl_percent = ((current_price - pos['entry']) / pos['entry']) * 100
                    else:
                        pnl_percent = ((pos['entry'] - current_price) / pos['entry']) * 100
                    
                    pos['current_price'] = current_price
                    pos['pnl_percent'] = pnl_percent
                    
                    # TP/SL checks
                    if pos['type'] == 'LONG':
                        if not pos['tp1_hit'] and current_price >= pos['tp1']:
                            pos['tp1_hit'] = True
                            send_position_alert(pos, 'tp1_hit')
                            pos['sl'] = pos['entry']
                            
                        if not pos['tp2_hit'] and current_price >= pos['tp2']:
                            pos['tp2_hit'] = True
                            send_position_alert(pos, 'tp2_hit')
                            pos['sl'] = pos['tp1']
                            
                        if not pos['tp3_hit'] and current_price >= pos['tp3']:
                            pos['tp3_hit'] = True
                            send_position_alert(pos, 'tp3_hit')
                            positions_to_remove.append(symbol)
                            
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
                    logging.error(f"Tracking xato ({symbol}): {e}")
            
            for symbol in positions_to_remove:
                if symbol in ACTIVE_POSITIONS:
                    del ACTIVE_POSITIONS[symbol]
                    logging.info(f"Pozitsiya yopildi: {symbol}")
            
            time.sleep(30)
            
        except Exception as e:
            logging.error(f"Tracking thread xato: {e}")
            time.sleep(60)


# ============================================
# EXCHANGE VA TAHLIL
# ============================================

def get_exchange_connections():
    """Exchange ulanish"""
    exchanges = {}
    
    if 'binance' in ENABLED_EXCHANGES:
        try:
            exchanges['binance'] = ccxt.binance({
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
                'timeout': 10000
            })
            logging.info("✅ Binance")
        except Exception as e:
            logging.error(f"Binance xato: {e}")
    
    if 'bitget' in ENABLED_EXCHANGES:
        try:
            exchanges['bitget'] = ccxt.bitget({
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
                'timeout': 10000
            })
            logging.info("✅ Bitget")
        except Exception as e:
            logging.error(f"Bitget xato: {e}")
    
    if 'mexc' in ENABLED_EXCHANGES:
        try:
            exchanges['mexc'] = ccxt.mexc({
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
                'timeout': 10000
            })
            logging.info("✅ MEXC")
        except Exception as e:
            logging.error(f"MEXC xato: {e}")
    
    return exchanges


def get_24h_tickers_batch(exchange, exchange_name):
    """Tickerlar"""
    try:
        tickers = exchange.fetch_tickers()
        logging.info(f"{exchange_name}: {len(tickers)} ticker")
        return tickers
    except Exception as e:
        logging.error(f"{exchange_name} ticker xato: {e}")
        return {}


def filter_by_volume(tickers, exchange_name, min_volume):
    """Hajm filtri"""
    filtered = {}
    for symbol, ticker in tickers.items():
        if symbol.endswith('/USDT'):
            if ticker.get('quoteVolume', 0) >= min_volume:
                filtered[symbol] = ticker
    
    logging.info(f"{exchange_name}: {len(filtered)} coin hajm filtridan o'tdi")
    return filtered


def quick_technical_analysis(df):
    """Texnik tahlil"""
    try:
        rsi_indicator = RSIIndicator(close=df['close'], window=14)
        rsi = rsi_indicator.rsi().iloc[-1]
        
        macd_indicator = MACD(close=df['close'], window_slow=26, window_fast=12, window_sign=9)
        macd_hist = macd_indicator.macd_diff().iloc[-1]
        
        adx_indicator = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
        adx = adx_indicator.adx().iloc[-1]
        
        df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
        
        ema_9 = df['ema_9'].iloc[-1]
        ema_21 = df['ema_21'].iloc[-1]
        current_price = df['close'].iloc[-1]
        
        if current_price > ema_9 > ema_21:
            trend = "BULLISH"
        elif current_price < ema_9 < ema_21:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"
        
        return {
            'rsi': rsi,
            'macd_hist': macd_hist,
            'adx': adx,
            'trend': trend,
            'price': current_price
        }
    except Exception as e:
        logging.error(f"TA xato: {e}")
        return None


def calculate_quick_score(analysis):
    """Score"""
    score = 5
    
    rsi = analysis.get('rsi', 50)
    adx = analysis.get('adx', 0)
    macd_hist = analysis.get('macd_hist', 0)
    trend = analysis.get('trend', 'NEUTRAL')
    
    if 40 <= rsi <= 60:
        score += 2
    if adx > 25:
        score += 2
    if macd_hist > 0:
        score += 1
    
    return min(score, 10)


def calculate_auto_tp_sl(price, signal_type):
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
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LOOKBACK_CANDLES)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        if df.empty or len(df) < 50:
            return None
        
        analysis = quick_technical_analysis(df)
        if not analysis:
            return None
        
        score = calculate_quick_score(analysis)
        
        min_score = MANUAL_SCAN_SCORE if use_manual else MIN_SIGNAL_SCORE
        
        if score < min_score:
            return None
        
        rsi = analysis['rsi']
        adx = analysis['adx']
        
        if rsi > RSI_OVERBOUGHT or rsi < RSI_OVERSOLD:
            return None
        
        if adx < MIN_ADX:
            return None
        
        trend = analysis['trend']
        macd_hist = analysis['macd_hist']
        
        if trend == 'BULLISH' and macd_hist > 0:
            signal_type = 'LONG'
        elif trend == 'BEARISH' and macd_hist < 0:
            signal_type = 'SHORT'
        else:
            return None
        
        levels = calculate_auto_tp_sl(analysis['price'], signal_type)
        
        return {
            'exchange': exchange_name.upper(),
            'symbol': symbol,
            'type': signal_type,
            'price': analysis['price'],
            'score': score,
            'rsi': rsi,
            'adx': adx,
            'macd_hist': macd_hist,
            'trend': trend,
            'entry': levels['entry'],
            'stop_loss': levels['stop_loss'],
            'tp1': levels['tp1'],
            'tp2': levels['tp2'],
            'tp3': levels['tp3'],
            'volume_24h': ticker_data.get('quoteVolume', 0),
            'timestamp': datetime.now()
        }
    except Exception as e:
        return None


def analyze_batch_parallel(symbols, exchange, exchange_name, tickers, use_manual=False):
    """Batch parallel tahlil"""
    signals = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(analyze_single_symbol, s, exchange, exchange_name, tickers.get(s, {}), use_manual): s 
            for s in symbols
        }
        
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
    
    message_lines = [
        f"{emoji} <b>{signal['type']} SIGNAL</b> | <b>{signal['exchange']}</b>\n\n",
        "━━━━━━━━━━━━━━━━━━━━━\n",
        f"<b>💰 {signal['symbol']}</b>\n",
        "━━━━━━━━━━━━━━━━━━━━━\n\n",
        f"💵 Narx: <b>${signal['price']:.6f}</b>\n",
        f"⭐ Score: <b>{signal['score']}/10</b>\n\n",
        "━━━━━━━━━━━━━━━━━━━━━\n",
        "<b>📈 TAHLIL</b>\n",
        "━━━━━━━━━━━━━━━━━━━━━\n\n",
        f"📊 RSI: {signal['rsi']:.2f}\n",
        f"💪 ADX: {signal['adx']:.2f}\n",
        f"🔥 Trend: {signal['trend']}\n\n",
        "━━━━━━━━━━━━━━━━━━━━━\n",
        "<b>💰 SAVDO DARAJALARI</b>\n",
        "━━━━━━━━━━━━━━━━━━━━━\n\n",
        f"📍 Entry: ${signal['entry']:.6f}\n",
        f"🛑 Stop Loss: ${signal['stop_loss']:.6f}\n\n",
        f"🎯 TP1: ${signal['tp1']:.6f}\n",
        f"🎯 TP2: ${signal['tp2']:.6f}\n",
        f"🎯 TP3: ${signal['tp3']:.6f}\n\n",
        "━━━━━━━━━━━━━━━━━━━━━\n\n",
        f"💰 24h Hajm: ${signal['volume_24h']:,.0f}\n",
        f"⏰ {signal['timestamp'].strftime('%H:%M:%S')}\n\n",
        "🔔 <b>TP/SL TRACKING AKTIV!</b>\n\n",
        "━━━━━━━━━━━━━━━━━━━━━\n\n",
        "⚠️ <b>BU MOLIYAVIY MASLAHAT EMAS!</b>"
    ]
    
    return ''.join(message_lines)


def send_signal(signal):
    """Signal yuborish"""
    message = format_signal_message(signal)
    success = send_telegram_message(message)
    
    if success:
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
        logging.info("=" * 60)
        logging.info("🚀 RUSTAMOV CRYPTO BOT ULTIMATE")
        logging.info("=" * 60)
        
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logging.error("❌ Telegram sozlanmagan!")
            return
        
        # Update handler
        update_thread = threading.Thread(target=handle_telegram_updates, daemon=True)
        update_thread.start()
        logging.info("✅ Telegram handler")
        
        # Tracking thread
        tracking_thread = threading.Thread(target=track_positions, daemon=True)
        tracking_thread.start()
        logging.info("✅ TP/SL tracking")
        
        # Start so'rovi
        send_approval_request()
        logging.info("📤 Start so'rovi yuborildi")
        
        # Tasdiqlashni kutish
        while not BOT_APPROVED:
            time.sleep(2)
        
        logging.info("✅ Bot tasdiqlandi!")
        
        # Exchange ulanish
        exchanges = get_exchange_connections()
        
        if not exchanges:
            send_telegram_message("❌ Exchangelarga ulanib bo'lmadi!")
            return
        
        # Asosiy sikl
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
                        send_telegram_message(f"✅ Manual scan: {len(all_signals)} signal")
                
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
