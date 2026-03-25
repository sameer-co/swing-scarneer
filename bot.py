import yfinance as yf
import pandas as pd
import pandas_ta as ta
import requests
import time
from datetime import datetime
import pytz
import os

# --- SECURE CONFIGURATION (Loaded from Railway Variables) ---
# If testing locally, you can replace os.getenv() with your actual strings, 
# but for Railway, set these in the 'Variables' tab.
TOKEN = os.getenv( '8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE', "YOUR_LOCAL_TOKEN_HERE")
CHAT_ID = os.getenv('1950462171', "YOUR_LOCAL_CHAT_ID_HERE")

# --- STRATEGY SETTINGS ---
WATCHLIST = ["RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ITC.NS", "SBI.NS"] 
RSI_PERIOD = 40
WMA_PERIOD = 15
TIMEFRAMES = ['1h', '2h', '4h', '1d']
LOG_FILE = "alerts_log.txt"

# --- FUNCTIONS ---
def log_alert(message):
    """Saves the alert to a local text file for historical review."""
    tz = pytz.timezone('Asia/Kolkata')
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
    
    # Clean the markdown formatting for the text file
    clean_message = message.replace('*', '').replace('🚀', '[BUY]').replace('📉', '[SELL]')
    
    with open(LOG_FILE, "a") as file:
        file.write(f"[{timestamp}] {clean_message}\n")
    print(f"Logged: {clean_message}")

def send_telegram(message):
    """Sends the alert to Telegram and triggers the logger."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage?chat_id={CHAT_ID}&text={message}&parse_mode=Markdown"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            log_alert(message)
        else:
            print(f"Telegram Failed: {response.text}")
    except Exception as e:
        print(f"Telegram Request Error: {e}")

def is_market_open():
    """Checks if the current time is between 9:15 AM and 3:30 PM IST (Mon-Fri)."""
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    
    # Monday = 0, Sunday = 6
    if now.weekday() < 5:
        market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_start <= now <= market_end
    return False

def get_data(symbol, interval):
    """Fetches data and resamples if necessary to create 2h and 4h timeframes."""
    period = "60d" if "h" in interval else "2y"
    yf_interval = '1h' if 'h' in interval else '1d'
    
    df = yf.download(symbol, period=period, interval=yf_interval, progress=False)
    
    if df.empty:
        return None

    # Strip multi-index columns if yfinance returns them
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    # Resample 1h data into 2h or 4h blocks
    if interval == '2h':
        df = df.resample('2h').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}).dropna()
    elif interval == '4h':
        df = df.resample('4h').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}).dropna()
    
    return df

def check_signals(symbol, df, tf):
    """Calculates RSI & WMA, checks for crossovers, and sends alerts."""
    df['RSI'] = ta.rsi(df['Close'], length=RSI_PERIOD)
    df['WMA'] = ta.wma(df['RSI'], length=WMA_PERIOD)
    
    # Drop rows where indicators are still calculating (NaN)
    df = df.dropna(subset=['RSI', 'WMA'])
    
    if len(df) < 2: 
        return

    # Look at the most recently closed candle and the one before it
    current = df.iloc[-1]
    previous = df.iloc[-2]

    # Bullish Cross: RSI was below WMA, now above
    if previous['RSI'] <= previous['WMA'] and current['RSI'] > current['WMA']:
        msg = f"🚀 *BULLISH SWING ALERT*\n*Symbol:* {symbol}\n*Timeframe:* {tf}\n*Trigger:* RSI ({current['RSI']:.1f}) crossed above WMA ({current['WMA']:.1f})"
        send_telegram(msg)

    # Bearish Cross: RSI was above WMA, now below
    elif previous['RSI'] >= previous['WMA'] and current['RSI'] < current['WMA']:
        msg = f"📉 *BEARISH SWING ALERT*\n*Symbol:* {symbol}\n*Timeframe:* {tf}\n*Trigger:* RSI ({current['RSI']:.1f}) crossed below WMA ({current['WMA']:.1f})"
        send_telegram(msg)

# --- MAIN EXECUTION LOOP ---
if __name__ == "__main__":
    print("🤖 Trading Bot Started! Environment configured.")
    print(f"Monitoring: {', '.join(WATCHLIST)}")
    
    while True:
        if is_market_open():
            tz = pytz.timezone('Asia/Kolkata')
            print(f"[{datetime.now(tz).strftime('%H:%M:%S')}] Market Open. Scanning Watchlist...")
            
            for symbol in WATCHLIST:
                for tf in TIMEFRAMES:
                    try:
                        data = get_data(symbol, tf)
                        if data is not None:
                            check_signals(symbol, data, tf)
                        time.sleep(1) # Delay to prevent Yahoo Finance from blocking your IP
                    except Exception as e:
                        print(f"Error checking {symbol} on {tf}: {e}")
            
            print("✅ Scan Complete. Sleeping for 15 minutes...")
            time.sleep(900) # Wait exactly 15 minutes before checking the next candle
        else:
            tz = pytz.timezone('Asia/Kolkata')
            print(f"[{datetime.now(tz).strftime('%H:%M:%S')}] Market Closed. Sleeping for 5 minutes...")
            time.sleep(300) # Poll every 5 minutes until the market opens
