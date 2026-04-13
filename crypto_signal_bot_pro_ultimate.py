import ccxt
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import time
import ta
from ta.trend import ADXIndicator, MACD, EMAIndicator
from ta.momentum import RSIIndicator
from ta.volume import OnBalanceVolumeIndicator
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf
from io import BytesIO

# ============================================
# KONFIGURATSIYA
# ============================================

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', '')  # Admin ID

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

# Volume spike settings
VOLUME_SPIKE_THRESHOLD = 3.0  # 3x volume
VOLUME_CHECK_ENABLED = True

# Bot state
BOT_RUNNING = False
MANUAL_SCAN_ACTIVE = False
MANUAL_SCAN_SCORE = 7
MANUAL_SCAN_MIN_VOLUME = 500000

# Multi-user support
AUTHORIZED_USERS = {}  # {chat_id: {'approved': True/False, 'username': '@user', 'joined': datetime}}
PENDING_APPROVALS = {}  # {chat_id: user_data}

# TP/SL Tracking
ACTIVE_POSITIONS = {}  # Per user: {chat_id: {symbol: position_data}}
TRACKING_ENABLED = True

# Trade History (Winrate)
TRADE_HISTORY = {}  # Per user: {chat_id: [trades]}

# Chart directory
CHART_DIR = 'charts'
if not os.path.exists(CHART_DIR):
    os.makedirs(CHART_DIR)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# ============================================
# TELEGRAM FUNCTIONS
# ============================================

def send_telegram_message(chat_id, message, parse_mode="HTML", reply_markup=None):
    """Telegram xabar yuborish"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
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


def send_telegram_photo(chat_id, photo_path, caption="", parse_mode="HTML"):
    """Telegram rasm yuborish"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        
        with open(photo_path, 'rb') as photo:
            files = {'photo': photo}
            data = {
                'chat_id': chat_id,
                'caption': caption,
                'parse_mode': parse_mode
            }
            response = requests.post(url, files=files, data=data, timeout=30)
        
        return response.status_code == 200
    except Exception as e:
        logging.error(f"Photo yuborish xato: {e}")
        return False


def broadcast_message(message, exclude_user=None):
    """Barcha foydalanuvchilarga xabar yuborish"""
    for chat_id, user_data in AUTHORIZED_USERS.items():
        if user_data.get('approved') and chat_id != exclude_user:
            send_telegram_message(chat_id, message)
            time.sleep(0.1)


def send_admin_approval_request(chat_id, user_data):
    """Adminga tasdiqlash so'rovi"""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ TASDIQLASH", "callback_data": f"admin_approve_{chat_id}"},
                {"text": "❌ RAD ETISH", "callback_data": f"admin_reject_{chat_id}"}
            ]
        ]
    }
    
    username = user_data.get('username', 'Unknown')
    first_name = user_data.get('first_name', 'Unknown')
    
    message = (
        f"🆕 <b>YANGI FOYDALANUVCHI SO'ROVI</b>\n\n"
        f"👤 Ism: {first_name}\n"
        f"📱 Username: {username}\n"
        f"🆔 Chat ID: <code>{chat_id}</code>\n"
        f"📅 Vaqt: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"Foydalanuvchini tasdiqlaysizmi?"
    )
    
    send_telegram_message(ADMIN_CHAT_ID, message, reply_markup=keyboard)


def send_welcome_message(chat_id):
    """Xush kelibsiz xabari"""
    message = (
        "🎉 <b>RUSTAMOV CRYPTO BOT'GA XUSH KELIBSIZ!</b> 🎉\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>✅ SIZ TASDIQLANGANSIZ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 Bot imkoniyatlari:\n\n"
        "✅ 3 ta birja: Binance, Bitget, MEXC\n"
        "✅ Professional tahlil (10+ indikator)\n"
        "✅ Auto TP/SL tracking\n"
        "✅ Pump/Dump detector\n"
        "✅ Winrate hisoboti\n"
        "✅ Chart rasmlari\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🔍 BUYRUQLAR</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "/start - Botni boshlash\n"
        "/menu - Asosiy menyu\n"
        "/scan - Manual skanerlash\n"
        "/positions - Ochiq pozitsiyalar\n"
        "/report - Hisobot (Winrate)\n"
        "/status - Bot holati\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ <b>BU MOLIYAVIY MASLAHAT EMAS!</b>\n"
        "Har doim o'z tadqiqotingizni o'tkazing (DYOR)."
    )
    
    send_telegram_message(chat_id, message)
    send_main_keyboard(chat_id)


def send_main_keyboard(chat_id):
    """Asosiy klaviatura"""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🔍 HOZIR SKAN", "callback_data": "scan_now"},
                {"text": "📊 POZITSIYALAR", "callback_data": "show_positions"}
            ],
            [
                {"text": "📈 HISOBOT", "callback_data": "show_report"},
                {"text": "📊 STATUS", "callback_data": "bot_status"}
            ],
            [
                {"text": "⚙️ SOZLAMALAR", "callback_data": "settings"},
                {"text": "❓ YORDAM", "callback_data": "help"}
            ]
        ]
    }
    
    send_telegram_message(chat_id, "🎛️ <b>ASOSIY MENYU</b>", reply_markup=keyboard)


def send_report_keyboard(chat_id):
    """Hisobot klaviaturasi"""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "📅 BUGUN", "callback_data": "report_today"},
                {"text": "📅 HAFTA", "callback_data": "report_week"}
            ],
            [
                {"text": "📅 OY", "callback_data": "report_month"},
                {"text": "📅 BARCHASI", "callback_data": "report_all"}
            ],
            [
                {"text": "🔙 ORQAGA", "callback_data": "back_to_menu"}
            ]
        ]
    }
    
    send_telegram_message(chat_id, "📊 <b>HISOBOT DAVRI</b>\n\nQaysi davrni ko'rmoqchisiz?", reply_markup=keyboard)


# ============================================
# CHART GENERATION (CANDLESTICK)
# ============================================

def generate_signal_chart(symbol, df, entry, tp1, tp2, tp3, sl, signal_type):
    """Signal chart yaratish (Candlestick + TP/SL)"""
    try:
        # DataFrame tayyorlash
        df_chart = df.tail(50).copy()
        df_chart.index = pd.to_datetime(df_chart['timestamp'], unit='ms')
        df_chart = df_chart[['open', 'high', 'low', 'close', 'volume']]
        
        # TP/SL chiziqlar
        apds = []
        
        # Entry
        apds.append(mpf.make_addplot([entry]*len(df_chart), color='yellow', linestyle='--', width=1.5))
        
        # TP levels
        apds.append(mpf.make_addplot([tp1]*len(df_chart), color='lime', linestyle='--', width=1))
        apds.append(mpf.make_addplot([tp2]*len(df_chart), color='green', linestyle='--', width=1))
        apds.append(mpf.make_addplot([tp3]*len(df_chart), color='darkgreen', linestyle='--', width=1))
        
        # SL
        apds.append(mpf.make_addplot([sl]*len(df_chart), color='red', linestyle='--', width=1.5))
        
        # Chart style
        mc = mpf.make_marketcolors(
            up='#00ff00', down='#ff0000',
            edge='inherit',
            wick={'up':'#00ff00', 'down':'#ff0000'},
            volume='in'
        )
        
        s = mpf.make_mpf_style(
            marketcolors=mc,
            gridstyle='-',
            y_on_right=False
        )
        
        # Save path
        filename = f"{symbol.replace('/', '_')}_{int(time.time())}.jpg"
        filepath = os.path.join(CHART_DIR, filename)
        
        # Plot
        mpf.plot(
            df_chart,
            type='candle',
            style=s,
            volume=True,
            addplot=apds,
            title=f"{symbol} - {signal_type} SIGNAL",
            ylabel='Price (USDT)',
            savefig=dict(fname=filepath, dpi=150, bbox_inches='tight')
        )
        
        logging.info(f"Chart yaratildi: {filepath}")
        return filepath
        
    except Exception as e:
        logging.error(f"Chart yaratish xato: {e}")
        return None


# ============================================
# VOLUME SPIKE DETECTOR (PUMP/DUMP)
# ============================================

def check_volume_spike(symbol, exchange_name, current_volume, avg_volume, price_change):
    """Volume spike va pump/dump detector"""
    try:
        if not VOLUME_CHECK_ENABLED:
            return
        
        ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        if ratio >= VOLUME_SPIKE_THRESHOLD:
            # PUMP yoki DUMP aniqlash
            if price_change > 5:  # 5% dan ko'p oshgan
                alert_type = "🚀 PUMP"
                emoji = "🟢"
            elif price_change < -5:  # 5% dan ko'p tushgan
                alert_type = "📉 DUMP"
                emoji = "🔴"
            else:
                alert_type = "⚠️ VOLUME SPIKE"
                emoji = "🟡"
            
            message = (
                f"{emoji} <b>{alert_type} DETECTED!</b> {emoji}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💰 <b>{symbol}</b> ({exchange_name.upper()})\n\n"
                f"📊 Volume: <b>{ratio:.1f}x</b> o'sgan!\n"
                f"📈 Narx o'zgarish: <b>{price_change:+.2f}%</b>\n"
                f"💵 Joriy hajm: ${current_volume:,.0f}\n"
                f"📊 O'rtacha hajm: ${avg_volume:,.0f}\n\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚠️ Ehtiyot bo'ling! Kitlar harakatlanmoqda!\n"
                f"Bu <b>moliyaviy maslahat emas!</b>"
            )
            
            # Barcha foydalanuvchilarga yuborish
            broadcast_message(message)
            
            logging.info(f"{alert_type}: {symbol} - {ratio:.1f}x volume")
    
    except Exception as e:
        logging.error(f"Volume spike check xato: {e}")


# ============================================
# SIGNAL REASON GENERATOR
# ============================================

def get_signal_reason(analysis, signal_type, volume_surge=False):
    """Signal sabablari"""
    reasons = []
    
    trend = analysis.get('trend', 'NEUTRAL')
    rsi = analysis.get('rsi', 50)
    adx = analysis.get('adx', 0)
    macd_hist = analysis.get('macd_hist', 0)
    ema_9 = analysis.get('ema_9', 0)
    ema_21 = analysis.get('ema_21', 0)
    price = analysis.get('price', 0)
    
    # 1. Trend
    if signal_type == 'LONG':
        if price > ema_9 > ema_21:
            reasons.append("✅ Kuchli ko'tarilish trendi (Price > EMA9 > EMA21)")
        else:
            reasons.append("✅ Ko'tarilish boshlanmoqda")
    else:
        if price < ema_9 < ema_21:
            reasons.append("✅ Kuchli pasayish trendi (Price < EMA9 < EMA21)")
        else:
            reasons.append("✅ Pasayish boshlanmoqda")
    
    # 2. MACD
    if signal_type == 'LONG' and macd_hist > 0:
        reasons.append(f"✅ MACD ijobiy - ko'tarilish impulsi ({macd_hist:.4f})")
    elif signal_type == 'SHORT' and macd_hist < 0:
        reasons.append(f"✅ MACD salbiy - pasayish impulsi ({macd_hist:.4f})")
    
    # 3. RSI
    if 40 <= rsi <= 60:
        reasons.append(f"✅ RSI optimal zonada ({rsi:.1f}) - balansli")
    elif signal_type == 'LONG' and rsi < 40:
        reasons.append(f"✅ RSI oversold yaqinida ({rsi:.1f}) - rebound imkoniyati")
    elif signal_type == 'SHORT' and rsi > 60:
        reasons.append(f"✅ RSI overbought yaqinida ({rsi:.1f}) - tuzatish kutilmoqda")
    
    # 4. ADX
    if adx > 30:
        reasons.append(f"✅ Juda kuchli trend - ADX {adx:.1f}")
    elif adx > 25:
        reasons.append(f"✅ Kuchli trend - ADX {adx:.1f}")
    
    # 5. Volume
    if volume_surge:
        reasons.append("🚀 HAJM KESKIN OSHGAN - Kitlar harakatlanmoqda!")
    
    # 6. EMA crossing
    if signal_type == 'LONG' and ema_9 > ema_21:
        reasons.append("✅ EMA9 EMA21 dan yuqori - bullish signal")
    elif signal_type == 'SHORT' and ema_9 < ema_21:
        reasons.append("✅ EMA9 EMA21 dan past - bearish signal")
    
    return "\n".join(reasons) if reasons else "Standart texnik tahlil"


# ============================================
# ADVANCED SCORE CALCULATION
# ============================================

def calculate_advanced_score(analysis, volume_data, price_action):
    """Kengaytirilgan score (indikatorga tayanmasdan)"""
    score = 0
    
    # === PRICE ACTION (3 ball) ===
    
    # 1. Candlestick patterns (1 ball)
    if price_action.get('strong_candle', False):
        score += 1
    
    # 2. Support/Resistance bounce (1 ball)
    if price_action.get('sr_bounce', False):
        score += 1
    
    # 3. Higher highs / Lower lows (1 ball)
    if price_action.get('trend_structure', False):
        score += 1
    
    # === VOLUME (2 ball) ===
    
    volume_ratio = volume_data.get('volume_ratio', 0)
    if volume_ratio > 2.5:
        score += 2  # Juda kuchli hajm
    elif volume_ratio > 1.5:
        score += 1  # Yuqori hajm
    
    # === TEXNIK INDIKATORLAR (3 ball) ===
    
    rsi = analysis.get('rsi', 50)
    adx = analysis.get('adx', 0)
    macd_hist = analysis.get('macd_hist', 0)
    
    if 40 <= rsi <= 60:
        score += 1
    
    if adx > 25:
        score += 1
    
    if macd_hist != 0:
        score += 1
    
    # === MARKET STRUCTURE (2 ball) ===
    
    trend = analysis.get('trend', 'NEUTRAL')
    if 'STRONG' in trend:
        score += 2
    elif trend != 'NEUTRAL':
        score += 1
    
    return min(score, 10)


def analyze_price_action(df):
    """Price action tahlil"""
    try:
        # So'nggi sham
        last_candle = df.iloc[-1]
        prev_candle = df.iloc[-2]
        
        # Candle body
        body = abs(last_candle['close'] - last_candle['open'])
        candle_range = last_candle['high'] - last_candle['low']
        body_ratio = body / candle_range if candle_range > 0 else 0
        
        # Kuchli sham (body > 70% of range)
        strong_candle = body_ratio > 0.7
        
        # Support/Resistance bounce
        # (Soddalashtirilgan)
        recent_low = df['low'].tail(20).min()
        recent_high = df['high'].tail(20).max()
        
        sr_bounce = (
            abs(last_candle['low'] - recent_low) / recent_low < 0.01 or
            abs(last_candle['high'] - recent_high) / recent_high < 0.01
        )
        
        # Trend structure (Higher highs yoki Lower lows)
        highs = df['high'].tail(5)
        lows = df['low'].tail(5)
        
        higher_highs = all(highs.iloc[i] > highs.iloc[i-1] for i in range(1, len(highs)))
        lower_lows = all(lows.iloc[i] < lows.iloc[i-1] for i in range(1, len(lows)))
        
        trend_structure = higher_highs or lower_lows
        
        return {
            'strong_candle': strong_candle,
            'sr_bounce': sr_bounce,
            'trend_structure': trend_structure,
            'body_ratio': body_ratio
        }
    
    except:
        return {
            'strong_candle': False,
            'sr_bounce': False,
            'trend_structure': False,
            'body_ratio': 0
        }


# ============================================
# WINRATE & PnL CALCULATION
# ============================================

def add_trade_to_history(chat_id, trade_data):
    """Trade tarixiga qo'shish"""
    if chat_id not in TRADE_HISTORY:
        TRADE_HISTORY[chat_id] = []
    
    TRADE_HISTORY[chat_id].append(trade_data)
    
    # Faqat so'nggi 1000 ta trade ni saqlash
    if len(TRADE_HISTORY[chat_id]) > 1000:
        TRADE_HISTORY[chat_id] = TRADE_HISTORY[chat_id][-1000:]


def calculate_winrate(chat_id, period='all'):
    """Winrate va PnL hisoblash"""
    try:
        if chat_id not in TRADE_HISTORY or not TRADE_HISTORY[chat_id]:
            return {
                'winrate': 0,
                'total_trades': 0,
                'wins': 0,
                'losses': 0,
                'total_pnl': 0,
                'avg_win': 0,
                'avg_loss': 0,
                'profit_factor': 0
            }
        
        # Period filter
        now = datetime.now()
        
        if period == 'today':
            start_date = now.replace(hour=0, minute=0, second=0)
        elif period == 'week':
            start_date = now - timedelta(days=7)
        elif period == 'month':
            start_date = now - timedelta(days=30)
        else:  # all
            start_date = datetime.min
        
        # Filter trades
        trades = [
            t for t in TRADE_HISTORY[chat_id]
            if t.get('exit_time', now) >= start_date
        ]
        
        if not trades:
            return calculate_winrate(chat_id, 'all')  # Fallback to all
        
        total_trades = len(trades)
        wins = sum(1 for t in trades if t.get('result') == 'WIN')
        losses = total_trades - wins
        
        winrate = (wins / total_trades * 100) if total_trades > 0 else 0
        
        # PnL
        total_pnl = sum(t.get('pnl', 0) for t in trades)
        
        # Average win/loss
        win_trades = [t.get('pnl', 0) for t in trades if t.get('result') == 'WIN']
        loss_trades = [t.get('pnl', 0) for t in trades if t.get('result') == 'LOSS']
        
        avg_win = sum(win_trades) / len(win_trades) if win_trades else 0
        avg_loss = abs(sum(loss_trades) / len(loss_trades)) if loss_trades else 0
        
        # Profit factor
        total_wins = sum(win_trades) if win_trades else 0
        total_losses = abs(sum(loss_trades)) if loss_trades else 0
        profit_factor = total_wins / total_losses if total_losses > 0 else 0
        
        return {
            'winrate': winrate,
            'total_trades': total_trades,
            'wins': wins,
            'losses': losses,
            'total_pnl': total_pnl,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': profit_factor
        }
    
    except Exception as e:
        logging.error(f"Winrate hisoblash xato: {e}")
        return {}


def send_trade_report(chat_id, period='all'):
    """Trade hisobotini yuborish"""
    try:
        stats = calculate_winrate(chat_id, period)
        
        period_names = {
            'today': 'BUGUN',
            'week': 'HAFTA',
            'month': 'OY',
            'all': 'BARCHA VAQT'
        }
        
        period_name = period_names.get(period, 'BARCHA VAQT')
        
        # Winrate emoji
        winrate = stats.get('winrate', 0)
        if winrate >= 70:
            wr_emoji = "🟢"
        elif winrate >= 50:
            wr_emoji = "🟡"
        else:
            wr_emoji = "🔴"
        
        # PnL emoji
        pnl = stats.get('total_pnl', 0)
        pnl_emoji = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
        
        message = (
            f"📊 <b>TRADE HISOBOTI</b>\n"
            f"📅 Davr: <b>{period_name}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>📈 ASOSIY KO'RSATKICHLAR</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{wr_emoji} <b>Winrate:</b> {winrate:.1f}%\n"
            f"📊 Jami tradelar: {stats.get('total_trades', 0)}\n"
            f"✅ Yutganlar: {stats.get('wins', 0)}\n"
            f"❌ Yutqazganlar: {stats.get('losses', 0)}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>💰 PnL TAHLILI</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{pnl_emoji} <b>Umumiy PnL:</b> ${pnl:+,.2f}\n"
            f"📈 O'rtacha yutish: ${stats.get('avg_win', 0):.2f}\n"
            f"📉 O'rtacha yutqazish: ${stats.get('avg_loss', 0):.2f}\n"
            f"⚖️ Profit Factor: {stats.get('profit_factor', 0):.2f}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⚠️ <b>Bu moliyaviy maslahat emas!</b>"
        )
        
        send_telegram_message(chat_id, message)
    
    except Exception as e:
        logging.error(f"Report yuborish xato: {e}")


# ============================================
# POSITION TRACKING
# ============================================

def add_position_to_tracking(chat_id, signal):
    """Pozitsiyani tracking ga qo'shish"""
    symbol = signal['symbol']
    
    if chat_id not in ACTIVE_POSITIONS:
        ACTIVE_POSITIONS[chat_id] = {}
    
    ACTIVE_POSITIONS[chat_id][symbol] = {
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
    
    logging.info(f"Pozitsiya qo'shildi: {symbol} (User: {chat_id})")


def show_active_positions(chat_id):
    """Ochiq pozitsiyalarni ko'rsatish"""
    user_positions = ACTIVE_POSITIONS.get(chat_id, {})
    
    if not user_positions:
        send_telegram_message(chat_id, "ℹ️ Hozirda ochiq pozitsiyalar yo'q")
        return
    
    message_parts = ["<b>📊 OCHIQ POZITSIYALAR</b>\n\n", "━━━━━━━━━━━━━━━━━━━━━\n\n"]
    
    for symbol, pos in user_positions.items():
        pnl = pos.get('pnl_percent', 0)
        pnl_emoji = "🟢" if pnl > 0 else "🔴"
        
        tp1_status = '✅' if pos.get('tp1_hit') else '⏳'
        tp2_status = '✅' if pos.get('tp2_hit') else '⏳'
        tp3_status = '✅' if pos.get('tp3_hit') else '⏳'
        
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
    send_telegram_message(chat_id, ''.join(message_parts))


def send_position_alert(chat_id, position_data, alert_type):
    """TP/SL alert yuborish"""
    emoji_map = {
        'tp1_hit': '🎯',
        'tp2_hit': '🎯🎯',
        'tp3_hit': '🎯🎯🎯',
        'sl_hit': '🛑',
    }
    
    emoji = emoji_map.get(alert_type, '⚠️')
    
    if alert_type == 'sl_hit':
        title = "STOP LOSS FAOLLASHDI"
        color = "🔴"
        result = "LOSS"
    elif 'tp' in alert_type:
        title = "TAKE PROFIT FAOLLASHDI"
        color = "🟢"
        result = "WIN"
    else:
        title = "POZITSIYA YANGILANDI"
        color = "🔵"
        result = None
    
    alert_description = alert_type.upper().replace('_', ' ')
    
    message_parts = [
        f"{emoji} <b>{color} {title}</b> {emoji}\n\n",
        "━━━━━━━━━━━━━━━━━━━━━\n\n",
        f"💰 <b>{position_data['symbol']}</b> ({position_data['exchange']})\n",
        f"📊 {position_data['type']}\n\n",
        f"📍 Entry: ${position_data['entry']:.6f}\n",
        f"💵 Exit: ${position_data['current_price']:.6f}\n\n"
    ]
    
    if 'tp' in alert_type or 'sl' in alert_type:
        message_parts.append(f"🎯 {alert_description} faollashdi!\n\n")
    
    pnl = position_data.get('pnl_percent', 0)
    pnl_usd = position_data.get('pnl_usd', 0)
    
    message_parts.extend([
        f"💰 P/L: {pnl:+.2f}%\n",
        f"💵 Profit: ${pnl_usd:+.2f}\n\n",
        f"⏰ {datetime.now().strftime('%H:%M:%S')}\n\n",
        "━━━━━━━━━━━━━━━━━━━━━\n\n",
        "⚠️ <b>Bu moliyaviy maslahat emas!</b>"
    ])
    
    send_telegram_message(chat_id, ''.join(message_parts))
    
    # Trade tarixiga qo'shish
    if result:
        trade_data = {
            'symbol': position_data['symbol'],
            'type': position_data['type'],
            'entry': position_data['entry'],
            'exit': position_data['current_price'],
            'pnl': pnl_usd,
            'pnl_percent': pnl,
            'result': result,
            'exit_time': datetime.now()
        }
        add_trade_to_history(chat_id, trade_data)


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
            
            for chat_id, user_positions in list(ACTIVE_POSITIONS.items()):
                positions_to_remove = []
                
                for symbol, pos in list(user_positions.items()):
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
                            pnl_usd = (current_price - pos['entry']) * 100  # Assume 100 units
                        else:
                            pnl_percent = ((pos['entry'] - current_price) / pos['entry']) * 100
                            pnl_usd = (pos['entry'] - current_price) * 100
                        
                        pos['current_price'] = current_price
                        pos['pnl_percent'] = pnl_percent
                        pos['pnl_usd'] = pnl_usd
                        
                        # TP/SL checks
                        if pos['type'] == 'LONG':
                            if not pos['tp1_hit'] and current_price >= pos['tp1']:
                                pos['tp1_hit'] = True
                                send_position_alert(chat_id, pos, 'tp1_hit')
                                pos['sl'] = pos['entry']  # Breakeven
                                
                            if not pos['tp2_hit'] and current_price >= pos['tp2']:
                                pos['tp2_hit'] = True
                                send_position_alert(chat_id, pos, 'tp2_hit')
                                pos['sl'] = pos['tp1']
                                
                            if not pos['tp3_hit'] and current_price >= pos['tp3']:
                                pos['tp3_hit'] = True
                                send_position_alert(chat_id, pos, 'tp3_hit')
                                positions_to_remove.append(symbol)
                                
                            if current_price <= pos['sl']:
                                pos['sl_hit'] = True
                                send_position_alert(chat_id, pos, 'sl_hit')
                                positions_to_remove.append(symbol)
                        
                        else:  # SHORT
                            if not pos['tp1_hit'] and current_price <= pos['tp1']:
                                pos['tp1_hit'] = True
                                send_position_alert(chat_id, pos, 'tp1_hit')
                                pos['sl'] = pos['entry']
                                
                            if not pos['tp2_hit'] and current_price <= pos['tp2']:
                                pos['tp2_hit'] = True
                                send_position_alert(chat_id, pos, 'tp2_hit')
                                pos['sl'] = pos['tp1']
                                
                            if not pos['tp3_hit'] and current_price <= pos['tp3']:
                                pos['tp3_hit'] = True
                                send_position_alert(chat_id, pos, 'tp3_hit')
                                positions_to_remove.append(symbol)
                                
                            if current_price >= pos['sl']:
                                pos['sl_hit'] = True
                                send_position_alert(chat_id, pos, 'sl_hit')
                                positions_to_remove.append(symbol)
                    
                    except Exception as e:
                        logging.error(f"Position tracking xato ({symbol}): {e}")
                
                # Remove closed positions
                for symbol in positions_to_remove:
                    if symbol in ACTIVE_POSITIONS[chat_id]:
                        del ACTIVE_POSITIONS[chat_id][symbol]
                        logging.info(f"Pozitsiya yopildi: {symbol} (User: {chat_id})")
            
            time.sleep(30)
            
        except Exception as e:
            logging.error(f"Tracking thread xato: {e}")
            time.sleep(60)


# ============================================
# TELEGRAM UPDATE HANDLER
# ============================================

def handle_telegram_updates():
    """Telegram yangilanishlar"""
    global BOT_RUNNING, MANUAL_SCAN_ACTIVE
    
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
                            chat_id = callback['message']['chat']['id']
                            
                            # Admin approval
                            if callback_data.startswith('admin_approve_'):
                                target_chat_id = int(callback_data.split('_')[2])
                                if target_chat_id in PENDING_APPROVALS:
                                    AUTHORIZED_USERS[target_chat_id] = PENDING_APPROVALS[target_chat_id]
                                    AUTHORIZED_USERS[target_chat_id]['approved'] = True
                                    del PENDING_APPROVALS[target_chat_id]
                                    
                                    send_telegram_message(ADMIN_CHAT_ID, f"✅ Foydalanuvchi tasdiqlandi: {target_chat_id}")
                                    send_welcome_message(target_chat_id)
                            
                            elif callback_data.startswith('admin_reject_'):
                                target_chat_id = int(callback_data.split('_')[2])
                                if target_chat_id in PENDING_APPROVALS:
                                    del PENDING_APPROVALS[target_chat_id]
                                    send_telegram_message(target_chat_id, "❌ Sizning so'rovingiz rad etildi.")
                                    send_telegram_message(ADMIN_CHAT_ID, f"❌ Foydalanuvchi rad etildi: {target_chat_id}")
                            
                            # User actions
                            elif chat_id in AUTHORIZED_USERS and AUTHORIZED_USERS[chat_id].get('approved'):
                                if callback_data == 'scan_now':
                                    MANUAL_SCAN_ACTIVE = True
                                    send_telegram_message(chat_id, "🔍 Manual skan boshlandi...")
                                    
                                elif callback_data == 'show_positions':
                                    show_active_positions(chat_id)
                                    
                                elif callback_data == 'show_report':
                                    send_report_keyboard(chat_id)
                                    
                                elif callback_data.startswith('report_'):
                                    period = callback_data.split('_')[1]
                                    send_trade_report(chat_id, period)
                                    
                                elif callback_data == 'bot_status':
                                    send_bot_status(chat_id)
                                    
                                elif callback_data == 'back_to_menu':
                                    send_main_keyboard(chat_id)
                                    
                                elif callback_data == 'help':
                                    send_help_message(chat_id)
                        
                        # Text commands
                        if 'message' in update:
                            message = update['message']
                            chat_id = message['chat']['id']
                            text = message.get('text', '')
                            
                            # User data
                            username = message['from'].get('username', 'Unknown')
                            first_name = message['from'].get('first_name', 'Unknown')
                            
                            if text.startswith('/start'):
                                if chat_id not in AUTHORIZED_USERS:
                                    # Yangi foydalanuvchi
                                    user_data = {
                                        'approved': False,
                                        'username': f"@{username}",
                                        'first_name': first_name,
                                        'joined': datetime.now()
                                    }
                                    PENDING_APPROVALS[chat_id] = user_data
                                    
                                    send_telegram_message(chat_id, "⏳ So'rovingiz adminga yuborildi. Tasdiqlashni kuting...")
                                    send_admin_approval_request(chat_id, user_data)
                                
                                elif AUTHORIZED_USERS[chat_id].get('approved'):
                                    send_welcome_message(chat_id)
                                else:
                                    send_telegram_message(chat_id, "⏳ Sizning so'rovingiz hali tasdiqlanmagan.")
                            
                            elif chat_id in AUTHORIZED_USERS and AUTHORIZED_USERS[chat_id].get('approved'):
                                if text.startswith('/menu'):
                                    send_main_keyboard(chat_id)
                                    
                                elif text.startswith('/scan'):
                                    MANUAL_SCAN_ACTIVE = True
                                    send_telegram_message(chat_id, "🔍 Manual skan boshlandi...")
                                    
                                elif text.startswith('/positions'):
                                    show_active_positions(chat_id)
                                    
                                elif text.startswith('/report'):
                                    send_report_keyboard(chat_id)
                                    
                                elif text.startswith('/status'):
                                    send_bot_status(chat_id)
            
            time.sleep(1)
            
        except Exception as e:
            logging.error(f"Update handler xato: {e}")
            time.sleep(5)


def send_bot_status(chat_id):
    """Bot holati"""
    status_emoji = "✅" if BOT_RUNNING else "🛑"
    status_text = "AKTIV" if BOT_RUNNING else "TO'XTATILGAN"
    
    total_users = len([u for u in AUTHORIZED_USERS.values() if u.get('approved')])
    
    message = (
        f"📊 <b>BOT HOLATI</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{status_emoji} Status: <b>{status_text}</b>\n"
        f"👥 Foydalanuvchilar: {total_users}\n"
        f"📊 Birjalar: {', '.join(ENABLED_EXCHANGES)}\n"
        f"⏱️ Timeframe: {TIMEFRAME}\n"
        f"🔄 Interval: {CHECK_INTERVAL}s\n"
        f"⭐ Min Score: {MIN_SIGNAL_SCORE}/10\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    send_telegram_message(chat_id, message)


def send_help_message(chat_id):
    """Yordam xabari"""
    message = (
        "❓ <b>YORDAM</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>📱 BUYRUQLAR:</b>\n\n"
        "/start - Botni boshlash\n"
        "/menu - Asosiy menyu\n"
        "/scan - Manual skanerlash\n"
        "/positions - Ochiq pozitsiyalar\n"
        "/report - Hisobot (Winrate)\n"
        "/status - Bot holati\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>🎯 XUSUSIYATLAR:</b>\n\n"
        "✅ 3 ta birja monitoring\n"
        "✅ Real-time TP/SL tracking\n"
        "✅ Pump/Dump detector\n"
        "✅ Chart rasmlari\n"
        "✅ Winrate hisoboti\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ <b>Bu moliyaviy maslahat emas!</b>"
    )
    
    send_telegram_message(chat_id, message)


# ============================================
# EXCHANGE & ANALYSIS (QISQARTIRILGAN)
# ============================================

def get_exchange_connections():
    """Exchange ulanish"""
    exchanges = {}
    
    for ex_name in ['binance', 'bitget', 'mexc']:
        if ex_name in ENABLED_EXCHANGES:
            try:
                if ex_name == 'binance':
                    exchanges['binance'] = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
                elif ex_name == 'bitget':
                    exchanges['bitget'] = ccxt.bitget({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
                elif ex_name == 'mexc':
                    exchanges['mexc'] = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
                
                logging.info(f"✅ {ex_name.capitalize()}")
            except:
                pass
    
    return exchanges


def get_24h_tickers_batch(exchange):
    """Tickerlar"""
    try:
        return exchange.fetch_tickers()
    except:
        return {}


def filter_by_volume(tickers, min_volume):
    """Hajm filtri"""
    filtered = {}
    for symbol, ticker in tickers.items():
        if symbol.endswith('/USDT') and ticker.get('quoteVolume', 0) >= min_volume:
            filtered[symbol] = ticker
    return filtered


def quick_technical_analysis(df):
    """Texnik tahlil"""
    try:
        rsi_indicator = RSIIndicator(close=df['close'], window=14)
        rsi = rsi_indicator.rsi().iloc[-1]
        
        macd_indicator = MACD(close=df['close'])
        macd_hist = macd_indicator.macd_diff().iloc[-1]
        
        adx_indicator = ADXIndicator(high=df['high'], low=df['low'], close=df['close'])
        adx = adx_indicator.adx().iloc[-1]
        
        df['ema_9'] = df['close'].ewm(span=9).mean()
        df['ema_21'] = df['close'].ewm(span=21).mean()
        
        ema_9 = df['ema_9'].iloc[-1]
        ema_21 = df['ema_21'].iloc[-1]
        price = df['close'].iloc[-1]
        
        if price > ema_9 > ema_21:
            trend = "BULLISH"
        elif price < ema_9 < ema_21:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"
        
        return {
            'rsi': rsi,
            'macd_hist': macd_hist,
            'adx': adx,
            'trend': trend,
            'price': price,
            'ema_9': ema_9,
            'ema_21': ema_21
        }
    except:
        return None


def calculate_volume_surge(df):
    """Hajm o'sishi"""
    try:
        avg_volume = df['volume'].tail(20).mean()
        current_volume = df['volume'].iloc[-1]
        volume_surge = current_volume > (avg_volume * 2.5)
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        return {
            'avg_volume': avg_volume,
            'current_volume': current_volume,
            'volume_surge': volume_surge,
            'volume_ratio': volume_ratio
        }
    except:
        return None


def calculate_auto_tp_sl(price, signal_type):
    """TP/SL"""
    if signal_type == 'LONG':
        return {
            'entry': price,
            'stop_loss': price * 0.97,
            'tp1': price * 1.02,
            'tp2': price * 1.05,
            'tp3': price * 1.08
        }
    else:
        return {
            'entry': price,
            'stop_loss': price * 1.03,
            'tp1': price * 0.98,
            'tp2': price * 0.95,
            'tp3': price * 0.92
        }


def analyze_single_symbol(symbol, exchange, exchange_name, ticker_data, use_manual=False):
    """Symbol tahlil"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LOOKBACK_CANDLES)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        if len(df) < 50:
            return None
        
        analysis = quick_technical_analysis(df)
        if not analysis:
            return None
        
        volume_data = calculate_volume_surge(df)
        price_action = analyze_price_action(df)
        
        score = calculate_advanced_score(analysis, volume_data, price_action)
        
        min_score = MANUAL_SCAN_SCORE if use_manual else MIN_SIGNAL_SCORE
        
        if score < min_score:
            return None
        
        rsi = analysis['rsi']
        adx = analysis['adx']
        
        if rsi > RSI_OVERBOUGHT or rsi < RSI_OVERSOLD or adx < MIN_ADX:
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
        
        # Volume spike check
        price_change = ticker_data.get('percentage', 0)
        check_volume_spike(
            symbol, 
            exchange_name, 
            volume_data['current_volume'],
            volume_data['avg_volume'],
            price_change
        )
        
        # Generate chart
        chart_path = generate_signal_chart(
            symbol, df,
            levels['entry'], levels['tp1'], levels['tp2'], levels['tp3'], levels['stop_loss'],
            signal_type
        )
        
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
            'volume_surge': volume_data['volume_surge'],
            'timestamp': datetime.now(),
            'chart_path': chart_path,
            'df': df,
            'analysis': analysis
        }
    except:
        return None


def analyze_batch_parallel(symbols, exchange, exchange_name, tickers, use_manual=False):
    """Batch tahlil"""
    signals = []
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(analyze_single_symbol, s, exchange, exchange_name, tickers.get(s, {}), use_manual): s 
            for s in symbols
        }
        
        for future in as_completed(futures):
            try:
                sig = future.result(timeout=8)
                if sig:
                    signals.append(sig)
            except:
                pass
    
    return signals


def format_signal_message(signal):
    """Signal formatlash"""
    emoji = '🟢' if signal['type'] == 'LONG' else '🔴'
    volume_emoji = '🚀' if signal.get('volume_surge') else ''
    
    # Signal sababi
    reason = get_signal_reason(signal['analysis'], signal['type'], signal.get('volume_surge', False))
    
    caption = (
        f"{emoji} <b>{signal['type']} SIGNAL</b> {volume_emoji} | <b>{signal['exchange']}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>💰 {signal['symbol']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💵 Narx: <b>${signal['price']:.6f}</b>\n"
        f"⭐ Score: <b>{signal['score']}/10</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>📈 TAHLIL</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 RSI: {signal['rsi']:.2f}\n"
        f"💪 ADX: {signal['adx']:.2f}\n"
        f"🔥 Trend: {signal['trend']}\n"
        f"{'🚀 VOLUME SURGE!' if signal.get('volume_surge') else ''}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>💰 SAVDO DARAJALARI</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📍 Entry: ${signal['entry']:.6f}\n"
        f"🛑 SL: ${signal['stop_loss']:.6f}\n\n"
        f"🎯 TP1: ${signal['tp1']:.6f}\n"
        f"🎯 TP2: ${signal['tp2']:.6f}\n"
        f"🎯 TP3: ${signal['tp3']:.6f}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>💡 SIGNAL SABABI</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{reason}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 24h Hajm: ${signal['volume_24h']:,.0f}\n"
        f"⏰ {signal['timestamp'].strftime('%H:%M:%S')}\n\n"
        f"🔔 <b>TP/SL AUTO TRACKING!</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚠️ <b>BU MOLIYAVIY MASLAHAT EMAS!</b>"
    )
    
    return caption


def send_signal(signal, chat_ids):
    """Signal yuborish (rasm bilan)"""
    caption = format_signal_message(signal)
    
    for chat_id in chat_ids:
        try:
            if signal.get('chart_path') and os.path.exists(signal['chart_path']):
                success = send_telegram_photo(chat_id, signal['chart_path'], caption)
            else:
                success = send_telegram_message(chat_id, caption)
            
            if success:
                add_position_to_tracking(chat_id, signal)
                logging.info(f"✅ Signal yuborildi: {signal['symbol']} -> {chat_id}")
            
            time.sleep(0.5)
        except Exception as e:
            logging.error(f"Signal yuborish xato ({chat_id}): {e}")


# ============================================
# ASOSIY SIKL
# ============================================

def main():
    """Asosiy dastur"""
    global BOT_RUNNING, MANUAL_SCAN_ACTIVE
    
    try:
        logging.info("="*60)
        logging.info("🚀 RUSTAMOV CRYPTO BOT ULTIMATE PRO")
        logging.info("="*60)
        
        if not TELEGRAM_BOT_TOKEN or not ADMIN_CHAT_ID:
            logging.error("❌ Telegram sozlanmagan!")
            return
        
        # Threads
        update_thread = threading.Thread(target=handle_telegram_updates, daemon=True)
        update_thread.start()
        
        tracking_thread = threading.Thread(target=track_positions, daemon=True)
        tracking_thread.start()
        
        logging.info("✅ Bot tayyor!")
        
        # Admin ga xabar
        send_telegram_message(ADMIN_CHAT_ID, "✅ Bot ishga tushdi!")
        
        BOT_RUNNING = True
        
        # Exchange ulanish
        exchanges = get_exchange_connections()
        
        if not exchanges:
            send_telegram_message(ADMIN_CHAT_ID, "❌ Exchangelarga ulanib bo'lmadi!")
            return
        
        # Asosiy sikl
        while BOT_RUNNING:
            try:
                cycle_start = time.time()
                
                all_signals = []
                use_manual = MANUAL_SCAN_ACTIVE
                
                for exchange_name, exchange in exchanges.items():
                    try:
                        tickers = get_24h_tickers_batch(exchange)
                        if not tickers:
                            continue
                        
                        min_vol = MANUAL_SCAN_MIN_VOLUME if use_manual else MIN_VOLUME_USD
                        filtered = filter_by_volume(tickers, min_vol)
                        
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
                    
                    # Barcha tasdiqlangan foydalanuvchilarga yuborish
                    approved_users = [cid for cid, data in AUTHORIZED_USERS.items() if data.get('approved')]
                    
                    for sig in top:
                        send_signal(sig, approved_users)
                        time.sleep(2)
                    
                    if use_manual:
                        MANUAL_SCAN_ACTIVE = False
                
                cycle_time = time.time() - cycle_start
                wait = max(CHECK_INTERVAL - cycle_time, 10)
                logging.info(f"⏳ Keyingi: {wait:.0f}s")
                time.sleep(wait)
                
            except Exception as e:
                logging.error(f"❌ Sikl xato: {e}")
                time.sleep(30)
        
    except Exception as e:
        logging.error(f"❌ Fatal: {e}")


if __name__ == "__main__":
    main()
