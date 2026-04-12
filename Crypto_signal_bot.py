import ccxt
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
import time
import ta
from ta.trend import ADXIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volume import VolumeWeightedAveragePrice
import logging
import json

# ============================================
# KONFIGURATSIYA
# ============================================

# Telegram Bot sozlamalari
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"  # O'z tokeningizni kiriting
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID_HERE"  # O'z chat ID ingizni kiriting

# API kalitlari (BEPUL)
ETHERSCAN_API_KEY = "YOUR_ETHERSCAN_API_KEY"  # Bepul: https://etherscan.io/apis
BSCSCAN_API_KEY = "YOUR_BSCSCAN_API_KEY"  # Bepul: https://bscscan.com/apis

# Filtr parametrlari
MIN_VOLUME_USD = 2_000_000  # Minimal 24h hajm: $2M
MIN_MARKET_CAP = 5_000_000  # Minimal market cap: $5M
MIN_ADX = 25  # Minimal ADX qiymati
RSI_OVERBOUGHT = 70  # RSI yuqori chegara
RSI_OVERSOLD = 30  # RSI pastki chegara
MIN_SIGNAL_SCORE = 8  # Minimal signal bali (10 dan)

# Boshqa sozlamalar
TIMEFRAME = '1h'  # Tahlil vaqt oralig'i
LOOKBACK_CANDLES = 100  # Orqaga qarab olinadigan shamlar soni
CHECK_INTERVAL = 300  # Tekshirish intervali (sekundlarda)

# Logging sozlash
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('crypto_signal_bot.log'),
        logging.StreamHandler()
    ]
)

# ============================================
# YORDAMCHI FUNKSIYALAR
# ============================================

def send_telegram_message(message):
    """Telegram orqali xabar yuborish"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logging.info("Telegram xabar muvaffaqiyatli yuborildi")
            return True
        else:
            logging.error(f"Telegram xabar yuborishda xato: {response.text}")
            return False
    except Exception as e:
        logging.error(f"Telegram xabar yuborishda xatolik: {str(e)}")
        return False


def get_exchange_connection():
    """Binance exchange bilan ulanish"""
    try:
        exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
            },
            'timeout': 30000,
        })
        return exchange
    except Exception as e:
        logging.error(f"Exchange bilan ulanishda xatolik: {str(e)}")
        return None


def fetch_ohlcv_data(exchange, symbol, timeframe=TIMEFRAME, limit=LOOKBACK_CANDLES):
    """OHLCV ma'lumotlarini olish"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        logging.error(f"{symbol} uchun OHLCV ma'lumot olishda xato: {str(e)}")
        return None


def fetch_ticker_data(exchange, symbol):
    """Ticker ma'lumotlarini olish"""
    try:
        ticker = exchange.fetch_ticker(symbol)
        return ticker
    except Exception as e:
        logging.error(f"{symbol} uchun ticker ma'lumot olishda xato: {str(e)}")
        return None


def fetch_order_book(exchange, symbol, limit=100):
    """Order book ma'lumotlarini olish"""
    try:
        order_book = exchange.fetch_order_book(symbol, limit=limit)
        return order_book
    except Exception as e:
        logging.error(f"{symbol} uchun order book olishda xato: {str(e)}")
        return None


# ============================================
# TEXNIK INDIKATORLAR
# ============================================

def calculate_rsi(df, period=14):
    """RSI (Relative Strength Index) hisoblash"""
    try:
        rsi_indicator = RSIIndicator(close=df['close'], window=period)
        df['rsi'] = rsi_indicator.rsi()
        return df['rsi'].iloc[-1]
    except Exception as e:
        logging.error(f"RSI hisoblashda xato: {str(e)}")
        return None


def calculate_macd(df):
    """MACD (Moving Average Convergence Divergence) hisoblash"""
    try:
        macd_indicator = MACD(
            close=df['close'],
            window_slow=26,
            window_fast=12,
            window_sign=9
        )
        df['macd'] = macd_indicator.macd()
        df['macd_signal'] = macd_indicator.macd_signal()
        df['macd_diff'] = macd_indicator.macd_diff()
        
        macd_value = df['macd'].iloc[-1]
        macd_signal = df['macd_signal'].iloc[-1]
        macd_diff = df['macd_diff'].iloc[-1]
        
        return {
            'macd': macd_value,
            'signal': macd_signal,
            'histogram': macd_diff
        }
    except Exception as e:
        logging.error(f"MACD hisoblashda xato: {str(e)}")
        return None


def calculate_adx(df, period=14):
    """ADX (Average Directional Index) hisoblash"""
    try:
        adx_indicator = ADXIndicator(
            high=df['high'],
            low=df['low'],
            close=df['close'],
            window=period
        )
        df['adx'] = adx_indicator.adx()
        df['adx_pos'] = adx_indicator.adx_pos()
        df['adx_neg'] = adx_indicator.adx_neg()
        
        return {
            'adx': df['adx'].iloc[-1],
            'di_plus': df['adx_pos'].iloc[-1],
            'di_minus': df['adx_neg'].iloc[-1]
        }
    except Exception as e:
        logging.error(f"ADX hisoblashda xato: {str(e)}")
        return None


def calculate_support_resistance(df, window=20):
    """Support va Resistance darajalarini aniqlash"""
    try:
        # Pivot Points usuli
        high = df['high'].rolling(window=window).max()
        low = df['low'].rolling(window=window).min()
        close = df['close']
        
        pivot = (high.iloc[-1] + low.iloc[-1] + close.iloc[-1]) / 3
        
        resistance_1 = 2 * pivot - low.iloc[-1]
        resistance_2 = pivot + (high.iloc[-1] - low.iloc[-1])
        resistance_3 = high.iloc[-1] + 2 * (pivot - low.iloc[-1])
        
        support_1 = 2 * pivot - high.iloc[-1]
        support_2 = pivot - (high.iloc[-1] - low.iloc[-1])
        support_3 = low.iloc[-1] - 2 * (high.iloc[-1] - pivot)
        
        return {
            'pivot': pivot,
            'resistance_1': resistance_1,
            'resistance_2': resistance_2,
            'resistance_3': resistance_3,
            'support_1': support_1,
            'support_2': support_2,
            'support_3': support_3
        }
    except Exception as e:
        logging.error(f"Support/Resistance hisoblashda xato: {str(e)}")
        return None


def calculate_ote_zone(df):
    """OTE (Optimal Trade Entry) zonasini hisoblash"""
    try:
        # So'nggi swing high va swing low topish
        high_max = df['high'].tail(50).max()
        low_min = df['low'].tail(50).min()
        
        range_size = high_max - low_min
        
        # Fibonacci retracement darajalari
        fib_0618 = high_max - (range_size * 0.618)
        fib_0786 = high_max - (range_size * 0.786)
        fib_0382 = high_max - (range_size * 0.382)
        
        current_price = df['close'].iloc[-1]
        
        # OTE zonasi: 0.62 - 0.79 orasida
        ote_upper = fib_0618
        ote_lower = fib_0786
        
        # Narx OTE zonasida ekanligini tekshirish
        in_bullish_ote = ote_lower <= current_price <= ote_upper
        in_bearish_ote = current_price >= ote_upper
        
        return {
            'ote_upper': ote_upper,
            'ote_lower': ote_lower,
            'in_bullish_ote': in_bullish_ote,
            'in_bearish_ote': in_bearish_ote,
            'fib_0382': fib_0382,
            'fib_0618': fib_0618,
            'fib_0786': fib_0786
        }
    except Exception as e:
        logging.error(f"OTE zona hisoblashda xato: {str(e)}")
        return None


def calculate_volume_surge(df, multiplier=2.0):
    """Hajm o'sishini aniqlash"""
    try:
        avg_volume = df['volume'].tail(20).mean()
        current_volume = df['volume'].iloc[-1]
        
        volume_surge = current_volume > (avg_volume * multiplier)
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        return {
            'avg_volume': avg_volume,
            'current_volume': current_volume,
            'volume_surge': volume_surge,
            'volume_ratio': volume_ratio
        }
    except Exception as e:
        logging.error(f"Volume surge hisoblashda xato: {str(e)}")
        return None


def detect_trend(df):
    """Trend yo'nalishini aniqlash"""
    try:
        # EMA 20, 50 dan foydalanish
        df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
        
        ema_20 = df['ema_20'].iloc[-1]
        ema_50 = df['ema_50'].iloc[-1]
        current_price = df['close'].iloc[-1]
        
        # Trend aniqlash
        if current_price > ema_20 > ema_50:
            trend = "BULLISH"
            strength = "STRONG"
        elif current_price > ema_20 and ema_20 < ema_50:
            trend = "BULLISH"
            strength = "WEAK"
        elif current_price < ema_20 < ema_50:
            trend = "BEARISH"
            strength = "STRONG"
        elif current_price < ema_20 and ema_20 > ema_50:
            trend = "BEARISH"
            strength = "WEAK"
        else:
            trend = "NEUTRAL"
            strength = "NEUTRAL"
        
        return {
            'trend': trend,
            'strength': strength,
            'ema_20': ema_20,
            'ema_50': ema_50
        }
    except Exception as e:
        logging.error(f"Trend aniqlashda xato: {str(e)}")
        return None


# ============================================
# FUNDAMENTAL TAHLIL (BEPUL API LAR)
# ============================================

def get_coingecko_data_free(coin_id):
    """CoinGecko BEPUL API dan coin ma'lumotlarini olish"""
    try:
        # BEPUL API endpoint
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        params = {
            'localization': 'false',
            'tickers': 'false',
            'market_data': 'true',
            'community_data': 'true',
            'developer_data': 'true',
            'sparkline': 'false'
        }
        
        response = requests.get(url, params=params, timeout=10)
        time.sleep(1.5)  # BEPUL API uchun rate limit: 10-50 calls/minute
        
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 429:
            logging.warning("CoinGecko rate limit! 60 sekund kutilmoqda...")
            time.sleep(60)
            return None
        else:
            logging.warning(f"CoinGecko API xato: {response.status_code}")
            return None
    except Exception as e:
        logging.error(f"CoinGecko ma'lumot olishda xato: {str(e)}")
        return None


def get_coinmarketcap_data_free(symbol):
    """CoinMarketCap BEPUL API (alternativa)"""
    try:
        # Agar CoinMarketCap API key bo'lsa
        # Bu optional, chunki limited free tier bor
        return None
    except Exception as e:
        logging.error(f"CoinMarketCap ma'lumot olishda xato: {str(e)}")
        return None


def get_binance_market_data(exchange, symbol):
    """Binance dan BEPUL market ma'lumotlari"""
    try:
        ticker_24h = exchange.fetch_ticker(symbol)
        
        # Market cap taxminiy hisoblash (faqat USDT juftliklari uchun)
        base_currency = symbol.split('/')[0]
        
        market_data = {
            'current_price': ticker_24h.get('last', 0),
            'market_cap_rank': 0,
            'total_volume_24h': ticker_24h.get('quoteVolume', 0),
            'price_change_24h': ticker_24h.get('percentage', 0),
            'high_24h': ticker_24h.get('high', 0),
            'low_24h': ticker_24h.get('low', 0),
            'ath': ticker_24h.get('high', 0),  # Approximation
            'atl': ticker_24h.get('low', 0),   # Approximation
        }
        
        return market_data
    except Exception as e:
        logging.error(f"Binance market data olishda xato: {str(e)}")
        return None


def analyze_project_fundamentals(coin_id, symbol, exchange):
    """Loyiha fundamental tahlili (BEPUL manbalardan)"""
    try:
        fundamentals = {}
        
        # 1. CoinGecko BEPUL API dan
        cg_data = get_coingecko_data_free(coin_id)
        
        if cg_data:
            fundamentals = {
                'name': cg_data.get('name', 'N/A'),
                'symbol': cg_data.get('symbol', 'N/A').upper(),
                'market_cap': cg_data.get('market_data', {}).get('market_cap', {}).get('usd', 0),
                'total_volume': cg_data.get('market_data', {}).get('total_volume', {}).get('usd', 0),
                'circulating_supply': cg_data.get('market_data', {}).get('circulating_supply', 0),
                'total_supply': cg_data.get('market_data', {}).get('total_supply', 0),
                'max_supply': cg_data.get('market_data', {}).get('max_supply', 0),
                'market_cap_rank': cg_data.get('market_cap_rank', 0),
                'description': cg_data.get('description', {}).get('en', 'N/A')[:300],
                'homepage': cg_data.get('links', {}).get('homepage', []),
                'twitter_followers': cg_data.get('community_data', {}).get('twitter_followers', 0),
                'reddit_subscribers': cg_data.get('community_data', {}).get('reddit_subscribers', 0),
                'telegram_channel_user_count': cg_data.get('community_data', {}).get('telegram_channel_user_count', 0),
                'github_stars': cg_data.get('developer_data', {}).get('stars', 0),
                'github_forks': cg_data.get('developer_data', {}).get('forks', 0),
                'github_commits_4_weeks': cg_data.get('developer_data', {}).get('commit_count_4_weeks', 0),
                'price_change_24h': cg_data.get('market_data', {}).get('price_change_percentage_24h', 0),
                'price_change_7d': cg_data.get('market_data', {}).get('price_change_percentage_7d', 0),
                'price_change_30d': cg_data.get('market_data', {}).get('price_change_percentage_30d', 0),
            }
        else:
            # Agar CoinGecko ishlamasa, Binance dan
            binance_data = get_binance_market_data(exchange, symbol)
            if binance_data:
                fundamentals = {
                    'name': symbol.split('/')[0],
                    'symbol': symbol.split('/')[0],
                    'market_cap': 0,  # Binance bermaydi
                    'total_volume': binance_data['total_volume_24h'],
                    'current_price': binance_data['current_price'],
                    'price_change_24h': binance_data['price_change_24h'],
                    'high_24h': binance_data['high_24h'],
                    'low_24h': binance_data['low_24h'],
                }
        
        return fundamentals if fundamentals else None
        
    except Exception as e:
        logging.error(f"Fundamental tahlilda xato: {str(e)}")
        return None


def get_whale_transactions_etherscan(token_address, api_key, limit=100):
    """Etherscan BEPUL API dan whale transaksiyalar"""
    try:
        base_url = "https://api.etherscan.io/api"
        
        params = {
            'module': 'account',
            'action': 'tokentx',
            'contractaddress': token_address,
            'page': 1,
            'offset': limit,
            'sort': 'desc',
            'apikey': api_key
        }
        
        response = requests.get(base_url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data['status'] == '1':
                transactions = data['result']
                
                # Katta transaksiyalarni filtrlash
                whale_txs = []
                for tx in transactions[:20]:
                    value = int(tx.get('value', 0)) / (10 ** int(tx.get('tokenDecimal', 18)))
                    # Katta transaksiyalar (masalan, > 50,000 token)
                    if value > 50000:
                        whale_txs.append({
                            'hash': tx.get('hash'),
                            'from': tx.get('from')[:10] + '...',
                            'to': tx.get('to')[:10] + '...',
                            'value': value,
                            'timestamp': datetime.fromtimestamp(int(tx.get('timeStamp', 0)))
                        })
                
                return {
                    'whale_count': len(whale_txs),
                    'transactions': whale_txs[:5]
                }
        
        return None
    except Exception as e:
        logging.error(f"Whale transaksiyalarni olishda xato: {str(e)}")
        return None


def get_social_sentiment_free(coin_symbol):
    """Social media sentiment (BEPUL manbalardan)"""
    try:
        # Alternative.me Crypto Fear & Greed Index (BEPUL)
        url = "https://api.alternative.me/fng/"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            return {
                'fear_greed_index': int(data['data'][0]['value']),
                'fear_greed_classification': data['data'][0]['value_classification']
            }
        
        return None
    except Exception as e:
        logging.error(f"Social sentiment olishda xato: {str(e)}")
        return None


# ============================================
# FILTRLAR
# ============================================

def volume_filter(ticker_data):
    """Hajm filtri: 24h hajm $2M dan yuqori bo'lishi kerak"""
    try:
        volume_usd = ticker_data.get('quoteVolume', 0)
        
        if volume_usd >= MIN_VOLUME_USD:
            logging.info(f"✅ Hajm filtri o'tdi: ${volume_usd:,.2f}")
            return True
        else:
            logging.warning(f"❌ Hajm filtri o'tmadi: ${volume_usd:,.2f} < ${MIN_VOLUME_USD:,.2f}")
            return False
    except Exception as e:
        logging.error(f"Hajm filtrida xato: {str(e)}")
        return False


def rsi_filter(rsi_value, signal_type):
    """RSI filtri: Overbought/Oversold holatlarni cheklash"""
    try:
        if signal_type == "LONG":
            if rsi_value <= RSI_OVERBOUGHT:
                logging.info(f"✅ RSI filtri o'tdi (LONG): RSI={rsi_value:.2f}")
                return True
            else:
                logging.warning(f"❌ RSI filtri o'tmadi (LONG): RSI={rsi_value:.2f} > {RSI_OVERBOUGHT}")
                return False
        
        elif signal_type == "SHORT":
            if rsi_value >= RSI_OVERSOLD:
                logging.info(f"✅ RSI filtri o'tdi (SHORT): RSI={rsi_value:.2f}")
                return True
            else:
                logging.warning(f"❌ RSI filtri o'tmadi (SHORT): RSI={rsi_value:.2f} < {RSI_OVERSOLD}")
                return False
        
        return False
    except Exception as e:
        logging.error(f"RSI filtrida xato: {str(e)}")
        return False


def adx_filter(adx_data):
    """ADX filtri: Trend kuchini tekshirish"""
    try:
        adx_value = adx_data.get('adx', 0)
        
        if adx_value >= MIN_ADX:
            logging.info(f"✅ ADX filtri o'tdi: ADX={adx_value:.2f}")
            return True
        else:
            logging.warning(f"❌ ADX filtri o'tmadi: ADX={adx_value:.2f} < {MIN_ADX}")
            return False
    except Exception as e:
        logging.error(f"ADX filtrida xato: {str(e)}")
        return False


def ote_filter(ote_data, signal_type):
    """OTE filtri: Signal OTE zonasiga mos kelishi kerak"""
    try:
        if signal_type == "LONG":
            if ote_data.get('in_bullish_ote', False):
                logging.info(f"✅ OTE filtri o'tdi (LONG): Narx bullish OTE zonasida")
                return True
            else:
                logging.warning(f"❌ OTE filtri o'tmadi (LONG): Narx bullish OTE zonasida emas")
                return False
        
        elif signal_type == "SHORT":
            if ote_data.get('in_bearish_ote', False):
                logging.info(f"✅ OTE filtri o'tdi (SHORT): Narx bearish OTE zonasida")
                return True
            else:
                logging.warning(f"❌ OTE filtri o'tmadi (SHORT): Narx bearish OTE zonasida emas")
                return False
        
        return False
    except Exception as e:
        logging.error(f"OTE filtrida xato: {str(e)}")
        return False


def score_filter(signal_score):
    """Score filtri: Signal bali yetarlicha yuqori bo'lishi kerak"""
    try:
        if signal_score >= MIN_SIGNAL_SCORE:
            logging.info(f"✅ Score filtri o'tdi: {signal_score}/10")
            return True
        else:
            logging.warning(f"❌ Score filtri o'tmadi: {signal_score}/10 < {MIN_SIGNAL_SCORE}/10")
            return False
    except Exception as e:
        logging.error(f"Score filtrida xato: {str(e)}")
        return False


def liquidity_filter(market_cap, order_book):
    """Likvidlik filtri: Market cap va order book depth tekshirish"""
    try:
        # Market cap tekshirish (agar mavjud bo'lsa)
        if market_cap and market_cap > 0:
            if market_cap < MIN_MARKET_CAP:
                logging.warning(f"❌ Likvidlik filtri o'tmadi: Market cap ${market_cap:,.2f} < ${MIN_MARKET_CAP:,.2f}")
                return False
        
        # Order book depth tekshirish
        if order_book:
            bids = order_book.get('bids', [])
            asks = order_book.get('asks', [])
            
            # Top 10 bid va ask hajmini hisoblash
            bid_volume = sum([bid[1] for bid in bids[:10]]) if bids else 0
            ask_volume = sum([ask[1] for ask in asks[:10]]) if asks else 0
            
            total_liquidity = bid_volume + ask_volume
            
            # Minimal likvidlik talabi
            if total_liquidity < 10:
                logging.warning(f"❌ Likvidlik filtri o'tmadi: Order book depth yetarli emas")
                return False
        
        logging.info(f"✅ Likvidlik filtri o'tdi")
        return True
        
    except Exception as e:
        logging.error(f"Likvidlik filtrida xato: {str(e)}")
        return False


# ============================================
# SIGNAL GENERATSIYA
# ============================================

def calculate_entry_stop_take_profit(current_price, signal_type, support_resistance, atr=None):
    """Entry, Stop Loss va Take Profit darajalarini hisoblash"""
    try:
        if signal_type == "LONG":
            entry = current_price
            stop_loss = support_resistance.get('support_1', current_price * 0.95)
            take_profit_1 = support_resistance.get('resistance_1', current_price * 1.05)
            take_profit_2 = support_resistance.get('resistance_2', current_price * 1.10)
            take_profit_3 = support_resistance.get('resistance_3', current_price * 1.15)
            
            risk = entry - stop_loss
            reward_1 = take_profit_1 - entry
            rr_ratio = reward_1 / risk if risk > 0 else 0
            
        elif signal_type == "SHORT":
            entry = current_price
            stop_loss = support_resistance.get('resistance_1', current_price * 1.05)
            take_profit_1 = support_resistance.get('support_1', current_price * 0.95)
            take_profit_2 = support_resistance.get('support_2', current_price * 0.90)
            take_profit_3 = support_resistance.get('support_3', current_price * 0.85)
            
            risk = stop_loss - entry
            reward_1 = entry - take_profit_1
            rr_ratio = reward_1 / risk if risk > 0 else 0
        
        else:
            return None
        
        return {
            'entry': entry,
            'stop_loss': stop_loss,
            'take_profit_1': take_profit_1,
            'take_profit_2': take_profit_2,
            'take_profit_3': take_profit_3,
            'risk_reward_ratio': rr_ratio
        }
        
    except Exception as e:
        logging.error(f"Entry/SL/TP hisoblashda xato: {str(e)}")
        return None


def calculate_signal_score(indicators, fundamentals, volume_data, social_data=None):
    """Signal balini hisoblash (0-10)"""
    try:
        score = 0
        
        # Texnik ko'rsatkichlar (5 ball)
        rsi = indicators.get('rsi', 50)
        adx = indicators.get('adx', {}).get('adx', 0)
        macd = indicators.get('macd', {})
        trend = indicators.get('trend', {})
        
        # RSI optimal diapazon (40-60): +1 ball
        if 40 <= rsi <= 60:
            score += 1
        
        # ADX yuqori (>25): +1 ball
        if adx > 25:
            score += 1
        
        # MACD signal: +1 ball
        if macd.get('histogram', 0) > 0:
            score += 1
        
        # Trend kuchli: +1 ball
        if trend.get('strength') == 'STRONG':
            score += 1
        
        # Volume surge: +1 ball
        if volume_data.get('volume_surge', False):
            score += 1
        
        # Fundamental ko'rsatkichlar (3 ball)
        if fundamentals:
            market_cap = fundamentals.get('market_cap', 0)
            social_score = (
                fundamentals.get('twitter_followers', 0) / 10000 +
                fundamentals.get('reddit_subscribers', 0) / 1000 +
                fundamentals.get('telegram_channel_user_count', 0) / 5000
            ) / 3
            
            # Market cap yuqori: +1 ball
            if market_cap > 100_000_000:
                score += 1
            
            # Social media faolligi: +1 ball
            if social_score > 1:
                score += 1
            
            # Developer faolligi: +1 ball
            if fundamentals.get('github_stars', 0) > 100:
                score += 1
        
        # Social sentiment (2 ball)
        if social_data:
            fear_greed = social_data.get('fear_greed_index', 50)
            # Optimal zona: 40-60
            if 40 <= fear_greed <= 60:
                score += 1
            # Extreme zones
            if fear_greed < 25 or fear_greed > 75:
                score += 1
        
        return min(score, 10)
        
    except Exception as e:
        logging.error(f"Signal score hisoblashda xato: {str(e)}")
        return 0


def generate_signal(symbol, exchange, coin_id=None):
    """Signal generatsiya qilish"""
    try:
        logging.info(f"\n{'='*60}")
        logging.info(f"Signal generatsiya: {symbol}")
        logging.info(f"{'='*60}")
        
        # 1. Ma'lumotlarni olish
        df = fetch_ohlcv_data(exchange, symbol)
        if df is None or df.empty:
            logging.error("OHLCV ma'lumot olishda xato")
            return None
        
        ticker = fetch_ticker_data(exchange, symbol)
        if ticker is None:
            logging.error("Ticker ma'lumot olishda xato")
            return None
        
        order_book = fetch_order_book(exchange, symbol)
        
        current_price = ticker.get('last', 0)
        price_change_24h = ticker.get('percentage', 0)
        
        # 2. Texnik indikatorlarni hisoblash
        rsi = calculate_rsi(df)
        macd = calculate_macd(df)
        adx = calculate_adx(df)
        support_resistance = calculate_support_resistance(df)
        ote_zone = calculate_ote_zone(df)
        volume_data = calculate_volume_surge(df)
        trend_data = detect_trend(df)
        
        if None in [rsi, macd, adx, support_resistance, ote_zone, volume_data, trend_data]:
            logging.error("Indikatorlarni hisoblashda xato")
            return None
        
        # 3. Fundamental tahlil
        fundamentals = None
        if coin_id:
            fundamentals = analyze_project_fundamentals(coin_id, symbol, exchange)
            time.sleep(1)  # Rate limit
        
        # 4. Social sentiment
        social_data = get_social_sentiment_free(symbol.split('/')[0])
        
        # 5. Signal yo'nalishini aniqlash
        signal_type = None
        
        # LONG signal shartlari
        if (trend_data['trend'] == 'BULLISH' and
            macd['histogram'] > 0 and
            rsi < 60 and
            adx['adx'] > 25):
            signal_type = "LONG"
        
        # SHORT signal shartlari
        elif (trend_data['trend'] == 'BEARISH' and
              macd['histogram'] < 0 and
              rsi > 40 and
              adx['adx'] > 25):
            signal_type = "SHORT"
        
        if not signal_type:
            logging.info("Signal shartlari bajarilmadi")
            return None
        
        # 6. Signal score hisoblash
        indicators = {
            'rsi': rsi,
            'macd': macd,
            'adx': adx,
            'trend': trend_data
        }
        
        signal_score = calculate_signal_score(indicators, fundamentals, volume_data, social_data)
        
        # 7. Filtrlarni qo'llash
        logging.info(f"\n--- Filtrlarni tekshirish ---")
        
        if not volume_filter(ticker):
            return None
        
        if not rsi_filter(rsi, signal_type):
            return None
        
        if not adx_filter(adx):
            return None
        
        if not ote_filter(ote_zone, signal_type):
            return None
        
        if not score_filter(signal_score):
            return None
        
        market_cap = fundamentals.get('market_cap', 0) if fundamentals else 0
        if not liquidity_filter(market_cap, order_book):
            return None
        
        logging.info(f"✅ Barcha filtrlar muvaffaqiyatli o'tdi!")
        
        # 8. Entry, Stop Loss, Take Profit hisoblash
        levels = calculate_entry_stop_take_profit(
            current_price,
            signal_type,
            support_resistance
        )
        
        if not levels:
            logging.error("Entry/SL/TP hisoblashda xato")
            return None
        
        # 9. Signal obyektini yaratish
        signal = {
            'symbol': symbol,
            'type': signal_type,
            'timestamp': datetime.now(),
            'price': current_price,
            'price_change_24h': price_change_24h,
            'score': signal_score,
            
            # Texnik indikatorlar
            'rsi': rsi,
            'macd': macd,
            'adx': adx,
            'trend': trend_data,
            'support_resistance': support_resistance,
            'ote_zone': ote_zone,
            'volume_data': volume_data,
            
            # Entry darajalari
            'entry': levels['entry'],
            'stop_loss': levels['stop_loss'],
            'take_profit_1': levels['take_profit_1'],
            'take_profit_2': levels['take_profit_2'],
            'take_profit_3': levels['take_profit_3'],
            'risk_reward_ratio': levels['risk_reward_ratio'],
            
            # Fundamental
            'fundamentals': fundamentals,
            'social_data': social_data
        }
        
        return signal
        
    except Exception as e:
        logging.error(f"Signal generatsiyada xato: {str(e)}")
        return None


# ============================================
# SIGNAL FORMATLASH VA YUBORISH
# ============================================

def format_signal_message(signal):
    """Signalni Telegram formatida tayyorlash"""
    try:
        emoji_map = {
            'LONG': '🟢',
            'SHORT': '🔴'
        }
        
        emoji = emoji_map.get(signal['type'], '⚪')
        
        message = f"""
{emoji} <b>{signal['type']} SIGNAL</b> {emoji}

━━━━━━━━━━━━━━━━━━━━━
<b>📊 ASOSIY MA'LUMOTLAR</b>
━━━━━━━━━━━━━━━━━━━━━

<b>Coin:</b> {signal['symbol']}
<b>Narx:</b> ${signal['price']:.8f}
<b>24h O'zgarish:</b> {signal['price_change_24h']:.2f}%
<b>Signal Score:</b> {signal['score']}/10 ⭐
<b>Vaqt:</b> {signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}

━━━━━━━━━━━━━━━━━━━━━
<b>📈 TEXNIK TAHLIL</b>
━━━━━━━━━━━━━━━━━━━━━

<b>Trend:</b> {signal['trend']['trend']} ({signal['trend']['strength']})
<b>RSI:</b> {signal['rsi']:.2f}
<b>MACD:</b> {signal['macd']['macd']:.2f}
<b>MACD Signal:</b> {signal['macd']['signal']:.2f}
<b>MACD Histogram:</b> {signal['macd']['histogram']:.2f}
<b>ADX:</b> {signal['adx']['adx']:.2f}
<b>+DI:</b> {signal['adx']['di_plus']:.2f}
<b>-DI:</b> {signal['adx']['di_minus']:.2f}

━━━━━━━━━━━━━━━━━━━━━
<b>💰 SAVDO DARAJALARI</b>
━━━━━━━━━━━━━━━━━━━━━

<b>📍 Entry:</b> ${signal['entry']:.8f}
<b>🛑 Stop Loss:</b> ${signal['stop_loss']:.8f}

<b>🎯 Take Profit 1:</b> ${signal['take_profit_1']:.8f}
<b>🎯 Take Profit 2:</b> ${signal['take_profit_2']:.8f}
<b>🎯 Take Profit 3:</b> ${signal['take_profit_3']:.8f}

<b>⚖️ Risk/Reward:</b> 1:{signal['risk_reward_ratio']:.2f}

━━━━━━━━━━━━━━━━━━━━━
<b>📊 SUPPORT & RESISTANCE</b>
━━━━━━━━━━━━━━━━━━━━━

<b>R3:</b> ${signal['support_resistance']['resistance_3']:.8f}
<b>R2:</b> ${signal['support_resistance']['resistance_2']:.8f}
<b>R1:</b> ${signal['support_resistance']['resistance_1']:.8f}
<b>Pivot:</b> ${signal['support_resistance']['pivot']:.8f}
<b>S1:</b> ${signal['support_resistance']['support_1']:.8f}
<b>S2:</b> ${signal['support_resistance']['support_2']:.8f}
<b>S3:</b> ${signal['support_resistance']['support_3']:.8f}

━━━━━━━━━━━━━━━━━━━━━
<b>📉 HAJM TAHLILI</b>
━━━━━━━━━━━━━━━━━━━━━

<b>Joriy Hajm:</b> {signal['volume_data']['current_volume']:,.0f}
<b>O'rtacha Hajm:</b> {signal['volume_data']['avg_volume']:,.0f}
<b>Hajm Nisbati:</b> {signal['volume_data']['volume_ratio']:.2f}x
<b>Volume Surge:</b> {'✅ Ha' if signal['volume_data']['volume_surge'] else '❌ Yo\'q'}
"""

        # Fundamental ma'lumotlar
        if signal['fundamentals']:
            fund = signal['fundamentals']
            message += f"""
━━━━━━━━━━━━━━━━━━━━━
<b>🏢 FUNDAMENTAL TAHLIL</b>
━━━━━━━━━━━━━━━━━━━━━

<b>Nomi:</b> {fund.get('name', 'N/A')}
<b>Market Cap:</b> ${fund.get('market_cap', 0):,.0f}
<b>24h Hajm:</b> ${fund.get('total_volume', 0):,.0f}
<b>Market Cap Rank:</b> #{fund.get('market_cap_rank', 'N/A')}

<b>🌐 Ijtimoiy Tarmoqlar:</b>
<b>Twitter:</b> {fund.get('twitter_followers', 0):,}
<b>Reddit:</b> {fund.get('reddit_subscribers', 0):,}
<b>Telegram:</b> {fund.get('telegram_channel_user_count', 0):,}

<b>💻 Ishlanma:</b>
<b>GitHub Stars:</b> {fund.get('github_stars', 0):,}
<b>4 Haftalik Commitlar:</b> {fund.get('github_commits_4_weeks', 0):,}
"""

        # Social sentiment
        if signal.get('social_data'):
            social = signal['social_data']
            message += f"""
━━━━━━━━━━━━━━━━━━━━━
<b>😨 BOZOR KAYFIYATI</b>
━━━━━━━━━━━━━━━━━━━━━

<b>Fear & Greed Index:</b> {social.get('fear_greed_index', 'N/A')} - {social.get('fear_greed_classification', 'N/A')}
"""

        message += f"""
━━━━━━━━━━━━━━━━━━━━━

⚠️ <b>OGOHLANTIRISH:</b> Bu signal faqat ma'lumot maqsadida berilgan. 
Investitsiya qilishdan oleh o'z tadqiqotingizni o'tkazing!

━━━━━━━━━━━━━━━━━━━━━
"""

        return message
        
    except Exception as e:
        logging.error(f"Signal formatini tayyorlashda xato: {str(e)}")
        return None


def send_signal(signal):
    """Signalni Telegram orqali yuborish"""
    try:
        message = format_signal_message(signal)
        if message:
            success = send_telegram_message(message)
            if success:
                logging.info(f"✅ Signal muvaffaqiyatli yuborildi: {signal['symbol']} - {signal['type']}")
                return True
            else:
                logging.error(f"❌ Signal yuborishda xato")
                return False
        return False
    except Exception as e:
        logging.error(f"Signal yuborishda xatolik: {str(e)}")
        return False


# ============================================
# ASOSIY FUNKSIYA
# ============================================

def analyze_multiple_symbols(symbols_list, exchange):
    """Bir nechta symbollarni tahlil qilish"""
    try:
        for symbol_data in symbols_list:
            symbol = symbol_data['symbol']
            coin_id = symbol_data.get('coin_id', None)
            
            logging.info(f"\n{'='*60}")
            logging.info(f"Tahlil qilinmoqda: {symbol}")
            logging.info(f"{'='*60}")
            
            signal = generate_signal(symbol, exchange, coin_id)
            
            if signal:
                send_signal(signal)
                time.sleep(2)
            else:
                logging.info(f"Signal topilmadi yoki filtrlardan o'tmadi: {symbol}")
            
            time.sleep(3)
            
    except Exception as e:
        logging.error(f"Ko'p symbollarni tahlil qilishda xato: {str(e)}")


def main():
    """Asosiy dastur"""
    try:
        logging.info("="*60)
        logging.info("KRIPTO SIGNAL BOT ISHGA TUSHDI (BEPUL VERSION)")
        logging.info("="*60)
        
        exchange = get_exchange_connection()
        if not exchange:
            logging.error("Exchange bilan ulanishda xato")
            return
        
        # Tahlil qilinadigan symbollar (CoinGecko ID bilan)
        symbols_to_analyze = [
            {'symbol': 'BTC/USDT', 'coin_id': 'bitcoin'},
            {'symbol': 'ETH/USDT', 'coin_id': 'ethereum'},
            {'symbol': 'BNB/USDT', 'coin_id': 'binancecoin'},
            {'symbol': 'SOL/USDT', 'coin_id': 'solana'},
            {'symbol': 'ADA/USDT', 'coin_id': 'cardano'},
            {'symbol': 'XRP/USDT', 'coin_id': 'ripple'},
            {'symbol': 'DOGE/USDT', 'coin_id': 'dogecoin'},
            {'symbol': 'DOT/USDT', 'coin_id': 'polkadot'},
            {'symbol': 'MATIC/USDT', 'coin_id': 'matic-network'},
            {'symbol': 'LINK/USDT', 'coin_id': 'chainlink'},
        ]
        
        # Doimiy tahlil sikli
        while True:
            try:
                logging.info("\n" + "="*60)
                logging.info(f"Yangi tahlil sikli: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                logging.info("="*60)
                
                analyze_multiple_symbols(symbols_to_analyze, exchange)
                
                logging.info(f"\n{'='*60}")
                logging.info(f"Keyingi tekshirish {CHECK_INTERVAL} sekunddan keyin...")
                logging.info(f"{'='*60}\n")
                
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                logging.info("\n\nDastur foydalanuvchi tomonidan to'xtatildi")
                break
            except Exception as e:
                logging.error(f"Sikl ichida xatolik: {str(e)}")
                time.sleep(60)
                
    except Exception as e:
        logging.error(f"Asosiy dasturda xatolik: {str(e)}")


if __name__ == "__main__":
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("⚠️ OGOHLANTIRISH: Telegram bot tokenini kiriting!")
        exit(1)
    
    if TELEGRAM_CHAT_ID == "YOUR_CHAT_ID_HERE":
        print("⚠️ OGOHLANTIRISH: Telegram chat ID ni kiriting!")
        exit(1)
    
    main()
