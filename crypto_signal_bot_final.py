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
from PIL import Image, ImageDraw, ImageFont
import textwrap

# ============================================
# KONFIGURATSIYA
# ============================================

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', '')
GROQ_API_KEY = os.getenv('GROQ_API_KEY', '')

# Exchange
ENABLED_EXCHANGES = os.getenv('ENABLED_EXCHANGES', 'binance,bitget,mexc').split(',')

# Filtrlar (Sizning shartlaringiz)
MIN_VOLUME_USD = int(os.getenv('MIN_VOLUME_USD', '2000000'))  # $2M
MIN_MARKET_CAP = int(os.getenv('MIN_MARKET_CAP', '5000000'))  # $5M
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

# Volume spike
VOLUME_SPIKE_THRESHOLD = 3.0

# Bot state
BOT_RUNNING = False
MANUAL_SCAN_ACTIVE = False
MANUAL_SCAN_SCORE = 7
MANUAL_SCAN_MIN_VOLUME = 500000

# Multi-user
AUTHORIZED_USERS = {}
PENDING_APPROVALS = {}

# Tracking
ACTIVE_POSITIONS = {}
TRACKING_ENABLED = True

# Trade History
TRADE_HISTORY = {}

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
    """Barcha tasdiqlangan foydalanuvchilarga yuborish"""
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
    """Xush kelibsiz"""
    message = (
        "🎉 <b>RUSTAMOV CRYPTO BOT'GA XUSH KELIBSIZ!</b> 🎉\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>✅ SIZ TASDIQLANGANSIZ!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 Bot imkoniyatlari:\n\n"
        "✅ 3 ta birja (Binance, Bitget, MEXC)\n"
        "✅ Professional tahlil\n"
        "✅ Auto TP/SL tracking\n"
        "✅ Pump/Dump detector\n"
        "✅ AI tahlil (Groq)\n"
        "✅ Winrate hisoboti\n"
        "✅ Coin qidiruv\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🔍 BUYRUQLAR</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "/menu - Asosiy menyu\n"
        "/scan - Manual skanerlash\n"
        "BTC - Coin ma'lumot (faqat BTC yozing)\n"
        "/positions - Ochiq pozitsiyalar\n"
        "/report - Hisobot (Winrate)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ <b>BU MOLIYAVIY MASLAHAT EMAS!</b>"
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
# SIGNAL CARD GENERATOR (PIL)
# ============================================

def create_signal_card(signal_data):
    """
    Chiroyli signal karta yaratish
    Faqat matn, oq background
    """
    try:
        width = 900
        height = 1600
        
        bg_color = '#FFFFFF'
        header_color = '#2196F3' if signal_data['type'] == 'LONG' else '#F44336'
        text_color = '#212121'
        section_bg = '#F5F5F5'
        border_color = '#E0E0E0'
        green = '#4CAF50'
        red = '#F44336'
        orange = '#FF9800'
        
        img = Image.new('RGB', (width, height), bg_color)
        draw = ImageDraw.Draw(img)
        
        # Fontlar
        try:
            font_title = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 36)
            font_header = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 28)
            font_body = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 22)
            font_small = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 18)
        except:
            font_title = ImageFont.load_default()
            font_header = ImageFont.load_default()
            font_body = ImageFont.load_default()
            font_small = ImageFont.load_default()
        
        y_pos = 30
        padding = 40
        
        # === HEADER ===
        signal_emoji = '🟢' if signal_data['type'] == 'LONG' else '🔴'
        header_text = f"{signal_emoji} {signal_data['type']} SIGNAL | {signal_data['exchange']}"
        
        draw.rectangle([0, 0, width, 90], fill=header_color)
        
        try:
            header_bbox = draw.textbbox((0, 0), header_text, font=font_title)
            header_width = header_bbox[2] - header_bbox[0]
        except:
            header_width = len(header_text) * 15
        
        draw.text(((width - header_width) // 2, 28), header_text, 
                 fill='#FFFFFF', font=font_title)
        
        y_pos = 110
        
        # === COIN INFO ===
        draw.text((padding, y_pos), f"💰 {signal_data['symbol']}", 
                 fill=text_color, font=font_header)
        y_pos += 45
        
        draw.text((padding, y_pos), 
                 f"💵 Joriy Narx: ${signal_data['price']:,.2f}", 
                 fill=text_color, font=font_body)
        y_pos += 40
        
        price_change = signal_data.get('price_change_24h', 0)
        price_change_color = green if price_change > 0 else red
        draw.text((padding, y_pos), 
                 f"📈 24h O'zgarish: {price_change:+.2f}%", 
                 fill=price_change_color, font=font_body)
        y_pos += 40
        
        draw.text((padding, y_pos), 
                 f"⭐ Signal Kuchi: {signal_data['score']}/10", 
                 fill=orange, font=font_body)
        y_pos += 55
        
        # === SEPARATOR ===
        draw.line([(padding, y_pos), (width - padding, y_pos)], 
                 fill=border_color, width=3)
        y_pos += 35
        
        # === SIGNAL SABABLARI ===
        draw.text((padding, y_pos), "🎯 SIGNAL SABABLARI", 
                 fill=text_color, font=font_header)
        y_pos += 45
        
        reasons = signal_data.get('reasons', [])
        for reason in reasons[:6]:
            wrapped = textwrap.fill(reason, width=55)
            draw.text((padding + 15, y_pos), wrapped, 
                     fill=text_color, font=font_small)
            lines = wrapped.count('\n') + 1
            y_pos += 28 * lines
        
        y_pos += 25
        
        # === SEPARATOR ===
        draw.line([(padding, y_pos), (width - padding, y_pos)], 
                 fill=border_color, width=3)
        y_pos += 35
        
        # === SAVDO DARAJALARI ===
        draw.text((padding, y_pos), "💰 SAVDO DARAJALARI", 
                 fill=text_color, font=font_header)
        y_pos += 45
        
        # Entry
        draw.text((padding + 15, y_pos), 
                 f"📍 ENTRY: ${signal_data['entry']:,.2f}", 
                 fill=text_color, font=font_body)
        y_pos += 40
        
        # Stop Loss
        sl_percent = ((signal_data['stop_loss'] - signal_data['entry']) / signal_data['entry'] * 100)
        draw.text((padding + 15, y_pos), 
                 f"🛑 STOP LOSS: ${signal_data['stop_loss']:,.2f} ({sl_percent:+.2f}%)", 
                 fill=red, font=font_body)
        y_pos += 50
        
        # Take Profits
        tp1_percent = ((signal_data['tp1'] - signal_data['entry']) / signal_data['entry'] * 100)
        draw.text((padding + 15, y_pos), 
                 f"🎯 TP1: ${signal_data['tp1']:,.2f} ({tp1_percent:+.2f}%)", 
                 fill=green, font=font_body)
        y_pos += 38
        
        tp2_percent = ((signal_data['tp2'] - signal_data['entry']) / signal_data['entry'] * 100)
        draw.text((padding + 15, y_pos), 
                 f"🎯 TP2: ${signal_data['tp2']:,.2f} ({tp2_percent:+.2f}%)", 
                 fill=green, font=font_body)
        y_pos += 38
        
        tp3_percent = ((signal_data['tp3'] - signal_data['entry']) / signal_data['entry'] * 100)
        draw.text((padding + 15, y_pos), 
                 f"🎯 TP3: ${signal_data['tp3']:,.2f} ({tp3_percent:+.2f}%)", 
                 fill=green, font=font_body)
        y_pos += 50
        
        # Risk/Reward
        rr = signal_data.get('risk_reward', 0)
        draw.text((padding + 15, y_pos), 
                 f"⚖️ Risk/Reward: 1:{rr:.2f}", 
                 fill=text_color, font=font_body)
        y_pos += 38
        
        # Leverage
        leverage = signal_data.get('leverage', '3x-5x')
        draw.text((padding + 15, y_pos), 
                 f"🎚️ Tavsiya Leverage: {leverage}", 
                 fill=orange, font=font_body)
        y_pos += 55
        
        # === SEPARATOR ===
        draw.line([(padding, y_pos), (width - padding, y_pos)], 
                 fill=border_color, width=3)
        y_pos += 35
        
        # === AI TAHLIL (KATTA BOX) ===
        ai_box_top = y_pos
        ai_box_height = 380
        draw.rectangle([padding - 15, ai_box_top, width - padding + 15, 
                       ai_box_top + ai_box_height], 
                      fill=section_bg, outline=header_color, width=4)
        
        y_pos += 25
        
        draw.text((padding + 15, y_pos), "🤖 AI TAHLIL & XULOSA", 
                 fill=header_color, font=font_header)
        y_pos += 50
        
        # AI text
        ai_text = signal_data.get('ai_analysis', 'AI tahlil yuklanmoqda...')
        wrapped_ai = textwrap.fill(ai_text, width=52)
        
        for line in wrapped_ai.split('\n')[:8]:  # Max 8 qator
            draw.text((padding + 25, y_pos), line, 
                     fill=text_color, font=font_small)
            y_pos += 28
        
        y_pos += 25
        
        # AI metrikalar
        ai_score = signal_data.get('ai_score', 0)
        draw.text((padding + 25, y_pos), 
                 f"⭐ AI Score: {ai_score}/10", 
                 fill=text_color, font=font_body)
        y_pos += 40
        
        ai_target = signal_data.get('ai_target', 0)
        draw.text((padding + 25, y_pos), 
                 f"🎯 AI Target: ${ai_target:,.2f}", 
                 fill=green, font=font_body)
        y_pos += 40
        
        ai_risk = signal_data.get('ai_risk', 'O\'rtacha')
        risk_color = green if 'past' in ai_risk.lower() else (red if 'yuqori' in ai_risk.lower() else orange)
        draw.text((padding + 25, y_pos), 
                 f"🛑 Risk Darajasi: {ai_risk}", 
                 fill=risk_color, font=font_body)
        y_pos += 40
        
        ai_leverage = signal_data.get('ai_leverage', '3x-5x')
        draw.text((padding + 25, y_pos), 
                 f"🎚️ AI Leverage: {ai_leverage}", 
                 fill=orange, font=font_body)
        y_pos += 55
        
        # === FOOTER ===
        draw.line([(padding, y_pos), (width - padding, y_pos)], 
                 fill=border_color, width=3)
        y_pos += 25
        
        draw.text((padding + 15, y_pos), 
                 "⚠️ Bu moliyaviy maslahat emas! O'z tadqiqotingizni o'tkazing!", 
                 fill=red, font=font_small)
        y_pos += 35
        
        timestamp = signal_data.get('timestamp', datetime.now())
        draw.text((padding + 15, y_pos), 
                 f"📅 {timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC", 
                 fill=text_color, font=font_small)
        y_pos += 30
        
        draw.text((padding + 15, y_pos), 
                 "🔔 TP/SL Tracking: AKTIV", 
                 fill=green, font=font_small)
        
        # === BORDER ===
        draw.rectangle([8, 8, width - 8, height - 8], 
                      outline=border_color, width=5)
        
        # Save
        filename = f"{signal_data['symbol'].replace('/', '_')}_{int(time.time())}.jpg"
        filepath = os.path.join(CHART_DIR, filename)
        
        img.save(filepath, 'JPEG', quality=95, optimize=True)
        
        logging.info(f"✅ Signal karta: {filepath}")
        return filepath
        
    except Exception as e:
        logging.error(f"❌ Karta yaratish xato: {e}")
        return None


# ============================================
# AI ANALYSIS (GROQ)
# ============================================

def get_ai_analysis(signal_data):
    """Groq AI dan tahlil olish"""
    try:
        if not GROQ_API_KEY:
            return {
                'text': 'AI tahlil: API key o\'rnatilmagan',
                'score': signal_data['score'],
                'target': signal_data['tp2'],
                'risk': 'O\'rtacha',
                'leverage': '3x-5x'
            }
        
        # Prompt yaratish
        prompt = f"""
Sen professional kripto treyder va AI tahlilchisan.

SIGNAL MA'LUMOTLARI:
- Coin: {signal_data['symbol']}
- Type: {signal_data['type']}
- Narx: ${signal_data['price']}
- 24h: {signal_data.get('price_change_24h', 0)}%
- Score: {signal_data['score']}/10
- Trend: {signal_data['trend']}
- RSI: {signal_data['rsi']}
- ADX: {signal_data['adx']}
- Volume Ratio: {signal_data.get('volume_ratio', 1)}x
- Entry: ${signal_data['entry']}
- SL: ${signal_data['stop_loss']}
- TP2: ${signal_data['tp2']}
- R/R: 1:{signal_data.get('risk_reward', 0)}

SIGNAL SABABLARI:
{chr(10).join(signal_data.get('reasons', [])[:3])}

VAZIFA: Qisqa va aniq tahlil (3-4 jumla, O'zbek tilida)

FORMAT:
[Tahlil matni - 3-4 jumla]

⭐ AI Score: X/10
🎯 AI Target: $XXXXX
🛑 Risk: [Past/O'rtacha/Yuqori]
🎚️ AI Leverage: Xx-Xx

TAHLIL QIL:
"""
        
        url = "https://api.groq.com/openai/v1/chat/completions"
        
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": "llama-3.1-70b-versatile",
            "messages": [
                {
                    "role": "system",
                    "content": "Sen professional kripto treyder va texnik tahlilchisan. O'zbek tilida qisqa va aniq tahlil berasan."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.5,
            "max_tokens": 500
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=30)
        
        if response.status_code == 200:
            ai_response = response.json()['choices'][0]['message']['content'].strip()
            
            # Parse qilish
            lines = ai_response.split('\n')
            text_lines = []
            score = signal_data['score']
            target = signal_data['tp2']
            risk = "O'rtacha"
            leverage = "3x-5x"
            
            for line in lines:
                if 'AI Score:' in line or '⭐' in line:
                    try:
                        score = int(''.join(filter(str.isdigit, line.split('/')[0])))
                    except:
                        pass
                elif 'AI Target:' in line or '🎯' in line:
                    try:
                        target = float(''.join(filter(lambda x: x.isdigit() or x == '.', line.split('$')[1].split()[0])))
                    except:
                        pass
                elif 'Risk:' in line or '🛑' in line:
                    if 'Past' in line or 'past' in line:
                        risk = 'Past'
                    elif 'Yuqori' in line or 'yuqori' in line:
                        risk = 'Yuqori'
                    else:
                        risk = "O'rtacha"
                elif 'Leverage:' in line or '🎚️' in line:
                    try:
                        leverage = line.split(':')[-1].strip()
                    except:
                        pass
                elif line.strip() and not any(x in line for x in ['⭐', '🎯', '🛑', '🎚️']):
                    text_lines.append(line.strip())
            
            ai_text = '\n'.join(text_lines[:6])  # Max 6 qator
            
            return {
                'text': ai_text,
                'score': score,
                'target': target,
                'risk': risk,
                'leverage': leverage
            }
        else:
            logging.error(f"Groq API xato: {response.status_code}")
            return {
                'text': f"Signal kuchli. {signal_data['trend']} trend. Tavsiya: {signal_data['type']} pozitsiya.",
                'score': signal_data['score'],
                'target': signal_data['tp2'],
                'risk': 'O\'rtacha',
                'leverage': '3x-5x'
            }
    
    except Exception as e:
        logging.error(f"AI tahlil xato: {e}")
        return {
            'text': 'AI tahlil vaqtincha mavjud emas.',
            'score': signal_data['score'],
            'target': signal_data['tp2'],
            'risk': 'O\'rtacha',
            'leverage': '3x-5x'
        }


def calculate_recommended_leverage(signal_data):
    """Leverage hisoblash"""
    score = signal_data['score']
    rr = signal_data.get('risk_reward', 0)
    adx = signal_data.get('adx', 0)
    volume_surge = signal_data.get('volume_surge', False)
    
    leverage = 1
    
    if score >= 9:
        leverage = 5
    elif score >= 8:
        leverage = 3
    elif score >= 7:
        leverage = 2
    
    if rr < 2:
        leverage = max(1, leverage - 1)
    
    if adx < 25:
        leverage = max(1, leverage - 1)
    
    if volume_surge:
        leverage = min(10, leverage + 1)
    
    return f"{leverage}x-{leverage+2}x"


# ============================================
# EXCHANGE & ANALYSIS
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
            except Exception as e:
                logging.error(f"❌ {ex_name}: {e}")
    
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
        df['ema_50'] = df['close'].ewm(span=50).mean()
        
        ema_9 = df['ema_9'].iloc[-1]
        ema_21 = df['ema_21'].iloc[-1]
        ema_50 = df['ema_50'].iloc[-1]
        price = df['close'].iloc[-1]
        
        if price > ema_9 > ema_21 > ema_50:
            trend = "STRONG BULLISH"
        elif price > ema_21 > ema_50:
            trend = "BULLISH"
        elif price < ema_9 < ema_21 < ema_50:
            trend = "STRONG BEARISH"
        elif price < ema_21 < ema_50:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"
        
        # Support/Resistance
        high_20 = df['high'].tail(20).max()
        low_20 = df['low'].tail(20).min()
        
        pivot = (high_20 + low_20 + price) / 3
        resistance = 2 * pivot - low_20
        support = 2 * pivot - high_20
        
        return {
            'rsi': rsi,
            'macd_hist': macd_hist,
            'adx': adx,
            'trend': trend,
            'price': price,
            'ema_9': ema_9,
            'ema_21': ema_21,
            'ema_50': ema_50,
            'support': support,
            'resistance': resistance,
            'pivot': pivot
        }
    except Exception as e:
        logging.error(f"TA xato: {e}")
        return None


def calculate_volume_surge(df):
    """Volume surge"""
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


def analyze_price_action(df):
    """Price action tahlil"""
    try:
        last_candle = df.iloc[-1]
        prev_candle = df.iloc[-2]
        
        body = abs(last_candle['close'] - last_candle['open'])
        candle_range = last_candle['high'] - last_candle['low']
        body_ratio = body / candle_range if candle_range > 0 else 0
        
        strong_candle = body_ratio > 0.7
        
        recent_low = df['low'].tail(20).min()
        recent_high = df['high'].tail(20).max()
        
        sr_bounce = (
            abs(last_candle['low'] - recent_low) / recent_low < 0.01 or
            abs(last_candle['high'] - recent_high) / recent_high < 0.01
        )
        
        highs = df['high'].tail(5)
        lows = df['low'].tail(5)
        
        higher_highs = all(highs.iloc[i] >= highs.iloc[i-1] for i in range(1, len(highs)))
        lower_lows = all(lows.iloc[i] <= lows.iloc[i-1] for i in range(1, len(lows)))
        
        trend_structure = higher_highs or lower_lows
        
        return {
            'strong_candle': strong_candle,
            'sr_bounce': sr_bounce,
            'trend_structure': trend_structure,
            'body_ratio': body_ratio,
            'higher_highs': higher_highs,
            'lower_lows': lower_lows
        }
    except:
        return {
            'strong_candle': False,
            'sr_bounce': False,
            'trend_structure': False,
            'body_ratio': 0,
            'higher_highs': False,
            'lower_lows': False
        }


def calculate_advanced_score(analysis, volume_data, price_action):
    """Score hisoblash (Price Action + Indikatorlar)"""
    score = 0
    
    # Price Action (40%)
    if price_action.get('strong_candle'):
        score += 1.5
    
    if price_action.get('sr_bounce'):
        score += 1.5
    
    if price_action.get('trend_structure'):
        score += 1
    
    # Volume (20%)
    volume_ratio = volume_data.get('volume_ratio', 0)
    if volume_ratio > 3:
        score += 2
    elif volume_ratio > 2:
        score += 1
    
    # Texnik Indikatorlar (40%)
    rsi = analysis.get('rsi', 50)
    adx = analysis.get('adx', 0)
    macd_hist = analysis.get('macd_hist', 0)
    trend = analysis.get('trend', 'NEUTRAL')
    
    if 40 <= rsi <= 60:
        score += 1.5
    elif 30 <= rsi <= 70:
        score += 1
    
    if adx > 30:
        score += 1.5
    elif adx > 25:
        score += 1
    
    if macd_hist != 0:
        score += 1
    
    if 'STRONG' in trend:
        score += 1.5
    elif trend != 'NEUTRAL':
        score += 1
    
    return min(round(score), 10)


def get_signal_reasons(analysis, volume_data, price_action, signal_type):
    """Signal sabablari (Sizning shartlaringiz asosida)"""
    reasons = []
    
    # 1. Price Action
    if price_action.get('sr_bounce'):
        if signal_type == 'LONG':
            reasons.append(f"✅ Support ${analysis.get('support', 0):,.0f} dan rebound")
        else:
            reasons.append(f"✅ Resistance ${analysis.get('resistance', 0):,.0f} dan rebound")
    
    if price_action.get('strong_candle'):
        candle_type = 'Bullish' if signal_type == 'LONG' else 'Bearish'
        reasons.append(f"✅ Kuchli {candle_type} sham formatsiyasi")
    
    if price_action.get('higher_highs') and signal_type == 'LONG':
        reasons.append("✅ Higher highs - ko'tarilish strukturasi")
    elif price_action.get('lower_lows') and signal_type == 'SHORT':
        reasons.append("✅ Lower lows - pasayish strukturasi")
    
    # 2. Volume
    volume_ratio = volume_data.get('volume_ratio', 0)
    if volume_ratio > 3:
        reasons.append(f"🚀 Volume {volume_ratio:.1f}x oshgan - PUMP!")
    elif volume_ratio > 2:
        reasons.append(f"✅ Volume {volume_ratio:.1f}x oshdi - kuchli harakat")
    
    # 3. Trend
    trend = analysis.get('trend', 'NEUTRAL')
    if 'STRONG' in trend:
        reasons.append(f"✅ {trend} - juda kuchli trend")
    elif trend != 'NEUTRAL':
        reasons.append(f"✅ {trend} trend")
    
    # 4. EMAs
    ema_9 = analysis.get('ema_9', 0)
    ema_21 = analysis.get('ema_21', 0)
    ema_50 = analysis.get('ema_50', 0)
    price = analysis.get('price', 0)
    
    if signal_type == 'LONG' and ema_9 > ema_21 > ema_50:
        reasons.append("✅ EMA 9>21>50 - bullish alignment")
    elif signal_type == 'SHORT' and ema_9 < ema_21 < ema_50:
        reasons.append("✅ EMA 9<21<50 - bearish alignment")
    
    # 5. RSI optimal
    rsi = analysis.get('rsi', 50)
    if 40 <= rsi <= 60:
        reasons.append(f"✅ RSI optimal zonada ({rsi:.0f})")
    
    # 6. ADX kuchli
    adx = analysis.get('adx', 0)
    if adx > 30:
        reasons.append(f"✅ ADX {adx:.0f} - juda kuchli trend")
    elif adx > 25:
        reasons.append(f"✅ ADX {adx:.0f} - kuchli trend")
    
    # 7. MACD
    macd_hist = analysis.get('macd_hist', 0)
    if signal_type == 'LONG' and macd_hist > 0:
        reasons.append("✅ MACD ijobiy - ko'tarilish impulsi")
    elif signal_type == 'SHORT' and macd_hist < 0:
        reasons.append("✅ MACD salbiy - pasayish impulsi")
    
    return reasons[:6]  # Faqat 6 ta


def check_volume_spike(symbol, exchange_name, current_volume, avg_volume, price_change):
    """Volume spike detector (Pump/Dump)"""
    try:
        if not avg_volume or avg_volume == 0:
            return
        
        ratio = current_volume / avg_volume
        
        if ratio >= VOLUME_SPIKE_THRESHOLD:
            if price_change > 5:
                alert_type = "🚀 PUMP"
                emoji = "🟢"
            elif price_change < -5:
                alert_type = "📉 DUMP"
                emoji = "🔴"
            else:
                alert_type = "⚠️ VOLUME SPIKE"
                emoji = "🟡"
            
            message = (
                f"{emoji} <b>{alert_type} DETECTED!</b> {emoji}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💰 <b>{symbol}</b> ({exchange_name.upper()})\n\n"
                f"📊 Volume: <b>{ratio:.1f}x</b> oshgan!\n"
                f"📈 Narx: <b>{price_change:+.2f}%</b>\n"
                f"💵 Joriy hajm: ${current_volume:,.0f}\n"
                f"📊 O'rtacha: ${avg_volume:,.0f}\n\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚠️ Ehtiyot bo'ling! Katta harakat!\n"
                f"Bu <b>moliyaviy maslahat emas!</b>"
            )
            
            broadcast_message(message)
            logging.info(f"{alert_type}: {symbol} - {ratio:.1f}x volume")
    
    except Exception as e:
        logging.error(f"Volume spike xato: {e}")


def calculate_auto_tp_sl(price, signal_type, analysis):
    """TP/SL hisoblash (Support/Resistance asosida)"""
    support = analysis.get('support', price * 0.95)
    resistance = analysis.get('resistance', price * 1.05)
    
    if signal_type == 'LONG':
        entry = price
        sl = support * 0.995
        tp1 = price + (price - sl) * 1.5
        tp2 = price + (price - sl) * 2.5
        tp3 = price + (price - sl) * 4
        
        if tp1 < resistance:
            tp1 = resistance * 0.998
        
    else:  # SHORT
        entry = price
        sl = resistance * 1.005
        tp1 = price - (sl - price) * 1.5
        tp2 = price - (sl - price) * 2.5
        tp3 = price - (sl - price) * 4
        
        if tp1 > support:
            tp1 = support * 1.002
    
    risk = abs(entry - sl)
    reward = abs(tp2 - entry)
    rr = reward / risk if risk > 0 else 0
    
    return {
        'entry': entry,
        'stop_loss': sl,
        'tp1': tp1,
        'tp2': tp2,
        'tp3': tp3,
        'risk_reward': rr
    }


def analyze_single_symbol(symbol, exchange, exchange_name, ticker_data, use_manual=False):
    """Symbol tahlil (TO'LIQ)"""
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
        
        if rsi > RSI_OVERBOUGHT or rsi < RSI_OVERSOLD:
            return None
        
        if adx < MIN_ADX:
            return None
        
        trend = analysis['trend']
        macd_hist = analysis['macd_hist']
        
        if ('BULLISH' in trend) and macd_hist > 0 and rsi < 60:
            signal_type = 'LONG'
        elif ('BEARISH' in trend) and macd_hist < 0 and rsi > 40:
            signal_type = 'SHORT'
        else:
            return None
        
        levels = calculate_auto_tp_sl(analysis['price'], signal_type, analysis)
        
        # Volume spike check
        price_change_24h = ticker_data.get('percentage', 0)
        check_volume_spike(
            symbol,
            exchange_name,
            volume_data['current_volume'],
            volume_data['avg_volume'],
            price_change_24h
        )
        
        # Signal sabablari
        reasons = get_signal_reasons(analysis, volume_data, price_action, signal_type)
        
        # Leverage
        leverage = calculate_recommended_leverage({
            'score': score,
            'risk_reward': levels['risk_reward'],
            'adx': adx,
            'volume_surge': volume_data['volume_surge']
        })
        
        signal = {
            'exchange': exchange_name.upper(),
            'symbol': symbol,
            'type': signal_type,
            'price': analysis['price'],
            'price_change_24h': price_change_24h,
            'score': score,
            'rsi': rsi,
            'adx': adx,
            'macd_hist': macd_hist,
            'trend': trend,
            'support': analysis['support'],
            'resistance': analysis['resistance'],
            'entry': levels['entry'],
            'stop_loss': levels['stop_loss'],
            'tp1': levels['tp1'],
            'tp2': levels['tp2'],
            'tp3': levels['tp3'],
            'risk_reward': levels['risk_reward'],
            'leverage': leverage,
            'volume_24h': ticker_data.get('quoteVolume', 0),
            'volume_surge': volume_data['volume_surge'],
            'volume_ratio': volume_data['volume_ratio'],
            'reasons': reasons,
            'timestamp': datetime.now(),
            'analysis': analysis,
            'volume_data': volume_data,
            'price_action': price_action
        }
        
        return signal
    
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
                sig = future.result(timeout=8)
                if sig:
                    signals.append(sig)
            except:
                pass
    
    return signals


def send_signal(signal, chat_ids):
    """Signal yuborish (karta bilan)"""
    try:
        # AI tahlil olish
        ai_result = get_ai_analysis(signal)
        
        # Signal data tayyorlash
        signal_data = {
            **signal,
            'ai_analysis': ai_result['text'],
            'ai_score': ai_result['score'],
            'ai_target': ai_result['target'],
            'ai_risk': ai_result['risk'],
            'ai_leverage': ai_result['leverage']
        }
        
        # Karta yaratish
        card_path = create_signal_card(signal_data)
        
        if not card_path:
            logging.error("Karta yaratilmadi")
            return
        
        # Caption (qisqa)
        caption = (
            f"🔔 <b>YANGI SIGNAL!</b>\n\n"
            f"💰 {signal['symbol']}\n"
            f"📊 {signal['type']} | {signal['exchange']}\n"
            f"⭐ Score: {signal['score']}/10\n\n"
            f"📸 Signal kartasini ko'ring ⬇️"
        )
        
        # Barcha foydalanuvchilarga yuborish
        for chat_id in chat_ids:
            try:
                success = send_telegram_photo(chat_id, card_path, caption)
                
                if success:
                    # Tracking ga qo'shish
                    add_position_to_tracking(chat_id, signal)
                    logging.info(f"✅ Signal yuborildi: {signal['symbol']} -> {chat_id}")
                
                time.sleep(0.5)
            except Exception as e:
                logging.error(f"Signal yuborish xato ({chat_id}): {e}")
        
        # Rasmni o'chirish (xotira tozalash)
        try:
            os.remove(card_path)
        except:
            pass
    
    except Exception as e:
        logging.error(f"send_signal xato: {e}")


# ============================================
# COIN SEARCH (BTC -> coin ma'lumot)
# ============================================

def search_coin_info(chat_id, coin_symbol):
    """Coin ma'lumotini olish va signal check"""
    try:
        # Normalize
        coin_upper = coin_symbol.upper()
        if not coin_upper.endswith('USDT'):
            symbol = f"{coin_upper}/USDT"
        else:
            symbol = f"{coin_upper.replace('USDT', '')}/USDT"
        
        # Exchange dan ma'lumot olish
        exchanges = get_exchange_connections()
        
        for exchange_name, exchange in exchanges.items():
            try:
                ticker = exchange.fetch_ticker(symbol)
                
                if not ticker:
                    continue
                
                price = ticker.get('last', 0)
                change_24h = ticker.get('percentage', 0)
                volume_24h = ticker.get('quoteVolume', 0)
                high_24h = ticker.get('high', 0)
                low_24h = ticker.get('low', 0)
                
                # Keyboard
                keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "🔍 SIGNAL?", "callback_data": f"check_signal_{symbol}_{exchange_name}"}
                        ]
                    ]
                }
                
                message = (
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>💰 {symbol} MA'LUMOT</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📊 <b>Birja:</b> {exchange_name.upper()}\n\n"
                    f"💵 <b>Joriy Narx:</b> ${price:,.6f}\n"
                    f"📈 <b>24h O'zgarish:</b> {change_24h:+.2f}%\n"
                    f"📊 <b>24h High:</b> ${high_24h:,.6f}\n"
                    f"📉 <b>24h Low:</b> ${low_24h:,.6f}\n"
                    f"💰 <b>24h Volume:</b> ${volume_24h:,.0f}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Signal mavjudligini tekshirish uchun\n"
                    f"<b>🔍 SIGNAL?</b> tugmasini bosing"
                )
                
                send_telegram_message(chat_id, message, reply_markup=keyboard)
                return True
            
            except Exception as e:
                continue
        
        send_telegram_message(chat_id, f"❌ {symbol} topilmadi yoki ma'lumot olishda xatolik")
        return False
    
    except Exception as e:
        logging.error(f"Coin search xato: {e}")
        send_telegram_message(chat_id, "❌ Xatolik yuz berdi")
        return False


def check_signal_for_coin(chat_id, symbol, exchange_name):
    """Coin uchun signal tekshirish"""
    try:
        send_telegram_message(chat_id, f"🔍 {symbol} tahlil qilinmoqda...")
        
        exchanges = get_exchange_connections()
        
        if exchange_name not in exchanges:
            send_telegram_message(chat_id, "❌ Exchange topilmadi")
            return
        
        exchange = exchanges[exchange_name]
        
        # Tahlil
        ticker = exchange.fetch_ticker(symbol)
        signal = analyze_single_symbol(symbol, exchange, exchange_name, ticker, use_manual=True)
        
        if signal:
            # Signal topildi - yuborish
            send_signal(signal, [chat_id])
        else:
            send_telegram_message(chat_id, f"❌ {symbol} uchun hozirda signal yo'q")
    
    except Exception as e:
        logging.error(f"Signal check xato: {e}")
        send_telegram_message(chat_id, "❌ Tahlil xatosi")


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


def show_active_positions(chat_id):
    """Ochiq pozitsiyalar"""
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
    """TP/SL alert"""
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
        return
    
    alert_description = alert_type.upper().replace('_', ' ')
    
    message_parts = [
        f"{emoji} <b>{color} {title}</b> {emoji}\n\n",
        "━━━━━━━━━━━━━━━━━━━━━\n\n",
        f"💰 <b>{position_data['symbol']}</b> ({position_data['exchange']})\n",
        f"📊 {position_data['type']}\n\n",
        f"📍 Entry: ${position_data['entry']:.6f}\n",
        f"💵 Exit: ${position_data['current_price']:.6f}\n\n",
        f"🎯 {alert_description} faollashdi!\n\n",
        f"💰 P/L: {position_data.get('pnl_percent', 0):+.2f}%\n",
        f"💵 Profit: ${position_data.get('pnl_usd', 0):+.2f}\n\n",
        f"⏰ {datetime.now().strftime('%H:%M:%S')}\n\n",
        "━━━━━━━━━━━━━━━━━━━━━\n\n",
        "⚠️ <b>Bu moliyaviy maslahat emas!</b>"
    ]
    
    send_telegram_message(chat_id, ''.join(message_parts))
    
    # Trade tarixiga qo'shish
    if result:
        add_trade_to_history(chat_id, {
            'symbol': position_data['symbol'],
            'type': position_data['type'],
            'entry': position_data['entry'],
            'exit': position_data['current_price'],
            'pnl': position_data.get('pnl_usd', 0),
            'pnl_percent': position_data.get('pnl_percent', 0),
            'result': result,
            'exit_time': datetime.now()
        })


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
                            pnl_usd = (current_price - pos['entry']) * 100
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
                                pos['sl'] = pos['entry']
                            
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
                
                for symbol in positions_to_remove:
                    if symbol in ACTIVE_POSITIONS[chat_id]:
                        del ACTIVE_POSITIONS[chat_id][symbol]
            
            time.sleep(30)
        
        except Exception as e:
            logging.error(f"Tracking thread xato: {e}")
            time.sleep(60)


# ============================================
# WINRATE REPORTING
# ============================================

def add_trade_to_history(chat_id, trade_data):
    """Trade qo'shish"""
    if chat_id not in TRADE_HISTORY:
        TRADE_HISTORY[chat_id] = []
    
    TRADE_HISTORY[chat_id].append(trade_data)
    
    if len(TRADE_HISTORY[chat_id]) > 1000:
        TRADE_HISTORY[chat_id] = TRADE_HISTORY[chat_id][-1000:]


def calculate_winrate(chat_id, period='all'):
    """Winrate hisoblash"""
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
        
        now = datetime.now()
        
        if period == 'today':
            start_date = now.replace(hour=0, minute=0, second=0)
        elif period == 'week':
            start_date = now - timedelta(days=7)
        elif period == 'month':
            start_date = now - timedelta(days=30)
        else:
            start_date = datetime.min
        
        trades = [
            t for t in TRADE_HISTORY[chat_id]
            if t.get('exit_time', now) >= start_date
        ]
        
        if not trades:
            return calculate_winrate(chat_id, 'all')
        
        total_trades = len(trades)
        wins = sum(1 for t in trades if t.get('result') == 'WIN')
        losses = total_trades - wins
        
        winrate = (wins / total_trades * 100) if total_trades > 0 else 0
        
        total_pnl = sum(t.get('pnl', 0) for t in trades)
        
        win_trades = [t.get('pnl', 0) for t in trades if t.get('result') == 'WIN']
        loss_trades = [t.get('pnl', 0) for t in trades if t.get('result') == 'LOSS']
        
        avg_win = sum(win_trades) / len(win_trades) if win_trades else 0
        avg_loss = abs(sum(loss_trades) / len(loss_trades)) if loss_trades else 0
        
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
        logging.error(f"Winrate xato: {e}")
        return {}


def send_trade_report(chat_id, period='all'):
    """Hisobot yuborish"""
    try:
        stats = calculate_winrate(chat_id, period)
        
        period_names = {
            'today': 'BUGUN',
            'week': 'HAFTA',
            'month': 'OY',
            'all': 'BARCHA VAQT'
        }
        
        period_name = period_names.get(period, 'BARCHA VAQT')
        
        winrate = stats.get('winrate', 0)
        if winrate >= 70:
            wr_emoji = "🟢"
        elif winrate >= 50:
            wr_emoji = "🟡"
        else:
            wr_emoji = "🔴"
        
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
        logging.error(f"Report xato: {e}")


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


# ============================================
# TELEGRAM UPDATE HANDLER
# ============================================

def handle_telegram_updates():
    """Telegram updates"""
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
                            
                            # Admin approvals
                            if callback_data.startswith('admin_approve_'):
                                target_chat_id = int(callback_data.split('_')[2])
                                if target_chat_id in PENDING_APPROVALS:
                                    AUTHORIZED_USERS[target_chat_id] = PENDING_APPROVALS[target_chat_id]
                                    AUTHORIZED_USERS[target_chat_id]['approved'] = True
                                    del PENDING_APPROVALS[target_chat_id]
                                    
                                    send_telegram_message(ADMIN_CHAT_ID, f"✅ Tasdiqlandi: {target_chat_id}")
                                    send_welcome_message(target_chat_id)
                            
                            elif callback_data.startswith('admin_reject_'):
                                target_chat_id = int(callback_data.split('_')[2])
                                if target_chat_id in PENDING_APPROVALS:
                                    del PENDING_APPROVALS[target_chat_id]
                                    send_telegram_message(target_chat_id, "❌ So'rovingiz rad etildi")
                                    send_telegram_message(ADMIN_CHAT_ID, f"❌ Rad etildi: {target_chat_id}")
                            
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
                                    send_telegram_message(chat_id, "❓ Yordam bo'limi ishlab chiqilmoqda...")
                                
                                elif callback_data.startswith('check_signal_'):
                                    parts = callback_data.split('_')
                                    symbol = '_'.join(parts[2:-1]).replace('_', '/')
                                    exchange_name = parts[-1]
                                    check_signal_for_coin(chat_id, symbol, exchange_name)
                        
                        # Text messages
                        if 'message' in update:
                            message = update['message']
                            chat_id = message['chat']['id']
                            text = message.get('text', '')
                            
                            username = message['from'].get('username', 'Unknown')
                            first_name = message['from'].get('first_name', 'Unknown')
                            
                            if text.startswith('/start'):
                                if chat_id not in AUTHORIZED_USERS:
                                    user_data = {
                                        'approved': False,
                                        'username': f"@{username}",
                                        'first_name': first_name,
                                        'joined': datetime.now()
                                    }
                                    PENDING_APPROVALS[chat_id] = user_data
                                    
                                    send_telegram_message(chat_id, "⏳ So'rovingiz adminga yuborildi...")
                                    send_admin_approval_request(chat_id, user_data)
                                
                                elif AUTHORIZED_USERS[chat_id].get('approved'):
                                    send_welcome_message(chat_id)
                                else:
                                    send_telegram_message(chat_id, "⏳ Tasdiqlashni kuting...")
                            
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
                                
                                # Coin search (faqat BTC yozsa)
                                elif len(text) <= 10 and text.isupper() and not text.startswith('/'):
                                    search_coin_info(chat_id, text)
            
            time.sleep(1)
        
        except Exception as e:
            logging.error(f"Update handler xato: {e}")
            time.sleep(5)


# ============================================
# MAIN LOOP
# ============================================

def main():
    """Asosiy dastur"""
    global BOT_RUNNING, MANUAL_SCAN_ACTIVE
    
    try:
        logging.info("="*60)
        logging.info("🚀 RUSTAMOV CRYPTO BOT - FINAL VERSION")
        logging.info("="*60)
        
        if not TELEGRAM_BOT_TOKEN or not ADMIN_CHAT_ID:
            logging.error("❌ Telegram sozlanmagan!")
            return
        
        # Threads
        update_thread = threading.Thread(target=handle_telegram_updates, daemon=True)
        update_thread.start()
        logging.info("✅ Telegram handler")
        
        tracking_thread = threading.Thread(target=track_positions, daemon=True)
        tracking_thread.start()
        logging.info("✅ TP/SL tracking")
        
        send_telegram_message(ADMIN_CHAT_ID, "✅ Bot ishga tushdi!")
        
        BOT_RUNNING = True
        
        # Exchanges
        exchanges = get_exchange_connections()
        
        if not exchanges:
            send_telegram_message(ADMIN_CHAT_ID, "❌ Exchange xatosi!")
            return
        
        # Main loop
        while BOT_RUNNING:
            try:
                cycle_start = time.time()
                logging.info(f"\n{'='*60}")
                logging.info(f"🔄 Sikl: {datetime.now().strftime('%H:%M:%S')}")
                
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
