import yfinance as yf
import pandas as pd
import pandas_ta as ta
import requests
import time
import random
from datetime import datetime, timedelta
import pytz
import os

# --- SECURE CONFIG ---
# Recommendation: In Railway, set these as Environment Variables
TOKEN = os.getenv('TELEGRAM_TOKEN', '8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '1950462171')
WATCHLIST_FILE = "watchlist.txt"

# --- STRATEGY PARAMETERS ---
RSI_PERIOD = 40
WMA_PERIOD = 15
# We only download '1h' and calculate the others locally to save API hits
TIMEFRAMES = ['1h', '2h', '4h', '1d']
BLACKLIST = ["WOCKHARDT.NS"] 

# --- FUNCTIONS ---
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage?chat_id={CHAT_ID}&text={message}&parse_mode=Markdown"
    try:
        requests.get(url, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

def load_watchlist():
    if not os.path.exists(WATCHLIST_FILE):
        return ["RELIANCE.NS", "TCS.NS"]
    
    with open(WATCHLIST_FILE, "r") as f:
        lines = f.read().splitlines()
    
    cleaned = []
    for s in lines:
        s = s.strip().upper()
        if s and s not in BLACKLIST:
            formatted = s if "." in s or "-" in s else f"{s}.NS"
            cleaned.append(formatted)
    return list(set(cleaned))

def get_seconds_until_open():
    """Calculates exactly how long to sleep until the next NSE opening."""
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    
    # Target is 09:15 AM
    target = now.replace(hour=9, minute=15, second=0, microsecond=0)
    
    # If it's already past 15:30, move target to tomorrow
    if now.time() >= datetime.strptime("15:30", "%H:%M").time():
        target += timedelta(days=1)
    
    # If target is Sat/Sun, move to Monday
    while target.weekday() > 4:
        target += timedelta(days=1)
        
    return (target - now).total_seconds()

def process_logic(symbol, df, tf):
    """The math engine for RSI Crossover."""
    if df.empty or len(df) < (RSI_PERIOD + WMA_PERIOD):
        return

    df['RSI'] = ta.rsi(df['Close'], length=RSI_PERIOD)
    df['WMA_RSI'] = ta.wma(df['RSI'], length=WMA_PERIOD)
    
    df = df.dropna(subset=['WMA_RSI'])
    if len(df) < 2: return

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    if prev['RSI'] <= prev['WMA_RSI'] and curr['RSI'] > curr['WMA_RSI']:
        msg = (f"🚀 *BULLISH CROSSOVER*\n"
               f"*Symbol:* `{symbol}`\n"
               f"*Timeframe:* {tf}\n"
               f"*RSI:* {curr['RSI']:.2f}\n"
               f"*WMA:* {curr['WMA_RSI']:.2f}")
        send_telegram(msg)
        print(f"✅ Alert Sent: {symbol} {tf}")

def run_bulk_scan():
    watchlist = load_watchlist()
    print(f"📦 Starting Bulk Fetch for {len(watchlist)} symbols...")
    
    # Adding jitter to avoid static fingerprinting
    time.sleep(random.randint(5, 15))

    try:
        # Download 1h data for everyone in ONE request
        # 60d period is enough to resample into 1d, 4h, 2h
        data = yf.download(watchlist, period="60d", interval="1h", group_by='ticker', progress=False)
    except Exception as e:
        print(f"Critical Download Error: {e}")
        return

    for symbol in watchlist:
        try:
            # Grab the specific data for this ticker
            ticker_df = data[symbol].dropna(how='all') if len(watchlist) > 1 else data.dropna(how='all')
            
            if ticker_df.empty: continue

            # Resample locally (No extra API calls!)
            for tf in TIMEFRAMES:
                if tf == '1h':
                    work_df = ticker_df.copy()
                elif tf == '2h':
                    work_df = ticker_df.resample('2h').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
                elif tf == '4h':
                    work_df = ticker_df.resample('4h').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
                elif tf == '1d':
                    work_df = ticker_df.resample('D').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
                
                process_logic(symbol, work_df, tf)
                
        except Exception as e:
            print(f"Skipping {symbol} due to error: {e}")

# --- MAIN ENGINE ---
if __name__ == "__main__":
    print("🤖 Trading Bot Initialized. Monitoring NSE...")
    tz = pytz.timezone('Asia/Kolkata')
    
    while True:
        now = datetime.now(tz)
        
        # Check if Market is Open (Mon-Fri, 09:15 - 15:30)
        is_weekday = now.weekday() < 5
        is_hours = datetime.strptime("09:15", "%H:%M").time() <= now.time() <= datetime.strptime("15:30", "%H:%M").time()
        
        if is_weekday and is_hours:
            start_mark = time.time()
            run_bulk_scan()
            
            # Wait 15 minutes before next scan
            elapsed = time.time() - start_mark
            sleep_time = max(900 - elapsed, 60) 
            print(f"☕ Scan complete in {elapsed:.1f}s. Next scan in {sleep_time/60:.1f}m.")
            time.sleep(sleep_time)
        else:
            # Deep Sleep until next market open
            seconds_to_wait = get_seconds_until_open()
            print(f"🌙 Market Closed. Sleeping for {seconds_to_wait/3600:.2f} hours...")
            time.sleep(seconds_to_wait)
