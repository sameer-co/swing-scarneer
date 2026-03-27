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
TOKEN = os.getenv('TELEGRAM_TOKEN', '8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '1950462171')
WATCHLIST_FILE = "watchlist.txt"

# --- STRATEGY PARAMETERS ---
RSI_PERIOD = 40
WMA_PERIOD = 15
TIMEFRAMES = ['1h', '2h', '4h', '1d']
BLACKLIST = ["WOCKHARDT.NS"] 

# --- MEMORY & SUMMARY (New) ---
last_alerts = {}  # Format: {"TCS.NS_1h": datetime_object}
daily_summary = [] # List of strings for the end-of-day report

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
    return list(set([s.strip().upper() if "." in s else f"{s.strip().upper()}.NS" 
                     for s in lines if s.strip() and s.strip().upper() not in BLACKLIST]))

def get_seconds_until_open():
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    target = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now.time() >= datetime.strptime("15:30", "%H:%M").time():
        target += timedelta(days=1)
    while target.weekday() > 4:
        target += timedelta(days=1)
    return (target - now).total_seconds()

def process_logic(symbol, df, tf):
    global daily_summary
    key = f"{symbol}_{tf}"
    
    if df.empty or len(df) < (RSI_PERIOD + WMA_PERIOD):
        return

    df['RSI'] = ta.rsi(df['Close'], length=RSI_PERIOD)
    df['WMA_RSI'] = ta.wma(df['RSI'], length=WMA_PERIOD)
    df = df.dropna(subset=['WMA_RSI'])
    if len(df) < 2: return

    curr, prev = df.iloc[-1], df.iloc[-2]

    # --- 1. ALERT DE-DUPLICATION LOGIC ---
    if prev['RSI'] <= prev['WMA_RSI'] and curr['RSI'] > curr['WMA_RSI']:
        now = datetime.now()
        # Only alert if we haven't alerted this combo in the last 20 hours
        if key not in last_alerts or (now - last_alerts[key]) > timedelta(hours=20):
            msg = (f"🚀 *BULLISH CROSSOVER*\n"
                   f"*Symbol:* `{symbol}`\n"
                   f"*Timeframe:* {tf}\n"
                   f"*RSI:* {curr['RSI']:.2f}")
            
            send_telegram(msg)
            last_alerts[key] = now
            daily_summary.append(f"✅ {symbol} ({tf}) @ {curr['Close']:.2f}")
            print(f"Alert Sent: {key}")

def send_daily_report():
    """Sends a summary of all hits at the end of market hours."""
    global daily_summary
    if not daily_summary:
        return
    
    report = "📊 *DAILY SIGNAL SUMMARY*\n\n" + "\n".join(daily_summary)
    send_telegram(report)
    daily_summary = [] # Reset for tomorrow

def run_bulk_scan():
    watchlist = load_watchlist()
    print(f"📦 Scanning {len(watchlist)} symbols...")
    time.sleep(random.randint(2, 5))

    try:
        data = yf.download(watchlist, period="60d", interval="1h", group_by='ticker', progress=False)
    except Exception as e:
        print(f"Download Error: {e}")
        return

    for symbol in watchlist:
        try:
            ticker_df = data[symbol].dropna(how='all') if len(watchlist) > 1 else data.dropna(how='all')
            if ticker_df.empty: continue

            # 2. LOCAL RESAMPLING
            for tf in TIMEFRAMES:
                if tf == '1h': work_df = ticker_df.copy()
                elif tf == '2h': work_df = ticker_df.resample('2h').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
                elif tf == '4h': work_df = ticker_df.resample('4h').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
                elif tf == '1d': work_df = ticker_df.resample('D').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
                
                process_logic(symbol, work_df, tf)
        except Exception as e:
            continue

# --- MAIN ENGINE ---
if __name__ == "__main__":
    tz = pytz.timezone('Asia/Kolkata')
    report_sent_today = False
    
    while True:
        now = datetime.now(tz)
        is_weekday = now.weekday() < 5
        market_open = datetime.strptime("09:15", "%H:%M").time()
        market_close = datetime.strptime("15:30", "%H:%M").time()
        
        if is_weekday and (market_open <= now.time() <= market_close):
            report_sent_today = False # Reset flag during market hours
            run_bulk_scan()
            time.sleep(900)
        else:
            # Send the summary once when the market closes
            if not report_sent_today and now.weekday() < 5:
                send_daily_report()
                report_sent_today = True
                
            wait = get_seconds_until_open()
            print(f"🌙 Sleeping {wait/3600:.1f} hrs until Market Open.")
            time.sleep(wait)
