import yfinance as yf
import pandas as pd
import pandas_ta as ta
import requests
import time
from datetime import datetime
import pytz
import os

# --- SECURE CONFIG ---
TOKEN = os.getenv('8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE')
CHAT_ID = os.getenv('1950462171')
WATCHLIST_FILE = "watchlist.txt"

# --- STRATEGY PARAMETERS ---
RSI_PERIOD = 40
WMA_PERIOD = 15
TIMEFRAMES = ['1h', '2h', '4h', '1d']

# --- FUNCTIONS ---
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage?chat_id={CHAT_ID}&text={message}&parse_mode=Markdown"
    try:
        requests.get(url, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

def load_and_clean_watchlist():
    """Reads watchlist.txt and ensures symbols have .NS suffix for India."""
    if not os.path.exists(WATCHLIST_FILE):
        # Create a default if it doesn't exist
        with open(WATCHLIST_FILE, "w") as f:
            f.write("RELIANCE\nTCS\nINFY")
        return ["RELIANCE.NS", "TCS.NS", "INFY.NS"]
    
    with open(WATCHLIST_FILE, "r") as f:
        lines = f.read().splitlines()
    
    cleaned = []
    for s in lines:
        s = s.strip().upper()
        if s:
            # Add .NS if no suffix exists (Assumes NSE by default)
            formatted = s if "." in s or "-" in s else f"{s}.NS"
            cleaned.append(formatted)
    return list(set(cleaned)) # Remove duplicates

def is_market_open():
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    # Mon-Fri, 09:15 to 15:30 IST
    if now.weekday() < 5:
        start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return start <= now <= end
    return False

def fetch_data(symbol, interval):
    # Determine lookback based on interval
    period = "60d" if "h" in interval else "2y"
    yf_tf = "1h" if "h" in interval else "1d"
    
    df = yf.download(symbol, period=period, interval=yf_tf, progress=False)
    if df.empty: return None

    # Handle Multi-Index columns (yfinance v0.2.40+)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Resampling for 2h/4h
    if interval == '2h':
        df = df.resample('2h').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
    elif interval == '4h':
        df = df.resample('4h').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
    
    return df

def process_strategy(symbol, df, tf):
    # Calculate RSI(40)
    df['RSI'] = ta.rsi(df['Close'], length=RSI_PERIOD)
    # Calculate WMA(15) of the RSI
    df['WMA_RSI'] = ta.wma(df['RSI'], length=WMA_PERIOD)
    
    df = df.dropna(subset=['WMA_RSI'])
    if len(df) < 2: return

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    # BULLISH CROSSOVER (RSI crosses ABOVE WMA)
    if prev['RSI'] <= prev['WMA_RSI'] and curr['RSI'] > curr['WMA_RSI']:
        msg = (f"🚀 *BULLISH CROSSOVER*\n"
               f"*Symbol:* `{symbol}`\n"
               f"*Timeframe:* {tf}\n"
               f"*RSI:* {curr['RSI']:.2f}\n"
               f"*WMA:* {curr['WMA_RSI']:.2f}")
        send_telegram(msg)
        print(f"Alert Sent: {symbol} {tf} Bullish")

# --- MAIN LOOP ---
if __name__ == "__main__":
    print("✅ Bot Initialization Complete.")
    
    while True:
        if is_market_open():
            watchlist = load_and_clean_watchlist()
            print(f"Scanning {len(watchlist)} symbols...")
            
            for symbol in watchlist:
                for tf in TIMEFRAMES:
                    try:
                        data = fetch_data(symbol, tf)
                        if data is not None:
                            process_strategy(symbol, data, tf)
                        time.sleep(1) # Avoid rate limits
                    except Exception as e:
                        print(f"Error on {symbol} ({tf}): {e}")
            
            print("Scan finished. Waiting 15m.")
            time.sleep(900)
        else:
            # Market Closed Logic
            tz = pytz.timezone('Asia/Kolkata')
            now = datetime.now(tz)
            print(f"Market Closed ({now.strftime('%H:%M')}). Checking in 10m...")
            time.sleep(600)
