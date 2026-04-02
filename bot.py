import yfinance as yf
import pandas as pd
import pandas_ta as ta
import requests
import time
import random
from datetime import datetime, timedelta
import pytz
import os

# --- CONFIG ---
TOKEN   = os.getenv('TELEGRAM_TOKEN',  '8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '1950462171')
WATCHLIST_FILE = "watchlist.txt"

# --- STRATEGY PARAMETERS ---
RSI_PERIOD = 40
WMA_PERIOD = 15
TIMEFRAMES = ['2h', '4h', '1d']          # 1h removed — biggest memory saver
BLACKLIST  = ["WOCKHARDT.NS"]

# --- STATE ---
# last_alerts format: {"TCS.NS_2h": datetime}  — cleared at midnight each day
last_alerts: dict = {}

# ── HELPERS ──────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    url = (
        f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        f"?chat_id={CHAT_ID}&text={requests.utils.quote(message)}&parse_mode=Markdown"
    )
    try:
        requests.get(url, timeout=10)
    except Exception as e:
        print(f"[Telegram Error] {e}")


def load_watchlist() -> list[str]:
    if not os.path.exists(WATCHLIST_FILE):
        return ["RELIANCE.NS", "TCS.NS"]
    with open(WATCHLIST_FILE) as f:
        lines = f.read().splitlines()
    return list({
        (s.strip().upper() if "." in s else f"{s.strip().upper()}.NS")
        for s in lines
        if s.strip() and s.strip().upper() not in BLACKLIST
    })


def next_market_open() -> float:
    """Return seconds until 9:15 AM IST on the next trading day."""
    tz  = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    target = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now.time() >= target.time():
        target += timedelta(days=1)
    while target.weekday() > 4:          # skip weekends
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ── CORE SCAN ─────────────────────────────────────────────────────────────────

def run_scan() -> None:
    """
    Download data once, resample, check signals for every symbol × TF.
    Collect hits per timeframe, then send at most 3 Telegram messages.
    """
    watchlist = load_watchlist()
    print(f"[Scan] {datetime.now().strftime('%H:%M')} — {len(watchlist)} symbols")

    # Small random jitter so Railway doesn't see perfectly timed bursts
    time.sleep(random.randint(1, 3))

    # ── 1. Download 1h data (used as base for resampling) ──
    try:
        raw = yf.download(
            watchlist,
            period="30d",              # 30d is enough for RSI(40) on daily
            interval="1h",
            group_by='ticker',
            progress=False,
            threads=True,
        )
    except Exception as e:
        print(f"[Download Error] {e}")
        return

    # ── 2. Per-timeframe signal buckets ──
    hits: dict[str, list[str]] = {tf: [] for tf in TIMEFRAMES}

    for symbol in watchlist:
        try:
            # Extract single-symbol DataFrame
            df_1h = (raw[symbol] if len(watchlist) > 1 else raw).dropna(how='all')
            if df_1h.empty:
                continue

            for tf in TIMEFRAMES:
                # ── 3. Resample ──
                if tf == '2h':
                    work = df_1h.resample('2h').agg(
                        {'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}
                    ).dropna()
                elif tf == '4h':
                    work = df_1h.resample('4h').agg(
                        {'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}
                    ).dropna()
                else:  # 1d
                    work = df_1h.resample('D').agg(
                        {'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}
                    ).dropna()

                if len(work) < (RSI_PERIOD + WMA_PERIOD + 2):
                    continue

                # ── 4. Indicators ──
                work = work.copy()
                work['RSI']     = ta.rsi(work['Close'], length=RSI_PERIOD)
                work['WMA_RSI'] = ta.wma(work['RSI'],   length=WMA_PERIOD)
                work.dropna(subset=['WMA_RSI'], inplace=True)

                if len(work) < 2:
                    continue

                curr, prev = work.iloc[-1], work.iloc[-2]

                # ── 5. Signal check ──
                crossover = (
                    prev['RSI'] <= prev['WMA_RSI'] and
                    curr['RSI'] >  curr['WMA_RSI']
                )
                if not crossover:
                    continue

                # ── 6. De-duplicate (20-hour window) ──
                key = f"{symbol}_{tf}"
                now = datetime.now()
                if key in last_alerts and (now - last_alerts[key]) < timedelta(hours=20):
                    continue

                last_alerts[key] = now
                price = f"₹{curr['Close']:.2f}"
                hits[tf].append(f"`{symbol}` @ {price}")
                print(f"  Signal: {key} @ {price}")

        except Exception:
            continue

        finally:
            # Free memory after each symbol
            del df_1h

    # Free raw download
    del raw

    # ── 7. Send one message per timeframe (always — signals or none) ──
    scan_time = datetime.now().strftime('%d %b %H:%M')
    for tf, signals in hits.items():
        if signals:
            lines = "\n".join(f"  {s}" for s in signals)
            msg = (
                f"📊 *RSI Crossover — {tf.upper()}*\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{lines}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"_Scan: {scan_time} IST_"
            )
        else:
            msg = (
                f"📭 *No signals — {tf.upper()}*\n"
                f"_Scan: {scan_time} IST_"
            )
        send_telegram(msg)


# ── SCHEDULER ────────────────────────────────────────────────────────────────

def ist_now() -> datetime:
    return datetime.now(pytz.timezone('Asia/Kolkata'))


def wait_until(h: int, m: int) -> None:
    """Block until HH:MM IST today (or skip if already past)."""
    tz     = pytz.timezone('Asia/Kolkata')
    now    = datetime.now(tz)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    diff   = (target - now).total_seconds()
    if diff > 0:
        print(f"  Waiting {diff/60:.1f} min until {h:02d}:{m:02d}…")
        time.sleep(diff)


# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Scanner started.")

    while True:
        now = ist_now()

        # ── Weekend / after-hours: sleep until next open ──
        if now.weekday() > 4:
            secs = next_market_open()
            print(f"Weekend. Sleeping {secs/3600:.1f} h until market open.")
            time.sleep(secs)
            continue

        market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)

        if now < market_open:
            secs = (market_open - now).total_seconds()
            print(f"Pre-market. Sleeping {secs/60:.1f} min.")
            time.sleep(secs)
            continue

        if now > market_close:
            secs = next_market_open()
            print(f"Market closed. Sleeping {secs/3600:.1f} h until next open.")
            time.sleep(secs)
            continue

        # ── It is a trading day inside market hours ──

        # ── Opening scan at 9:20 (wait for gap to settle) ──
        wait_until(9, 20)
        print("[9:20] Opening scan")
        run_scan()

        # ── Hourly scans: 10:15, 11:15, 12:15, 13:15, 14:15, 15:15 ──
        scan_times = [(10,15),(11,15),(12,15),(13,15),(14,15),(15,15)]
        for h, m in scan_times:
            now = ist_now()
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now > target:
                continue       # already past this slot today — skip
            wait_until(h, m)
            print(f"[{h:02d}:{m:02d}] Scan")
            run_scan()

        # ── All scans done for today ──
        # Sleep until midnight, clear alerts, then sleep until next open
        tz      = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(tz)
        midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        secs_to_midnight = (midnight - now_ist).total_seconds()
        print(f"All scans done. Sleeping {secs_to_midnight/3600:.1f} h until midnight reset.")
        time.sleep(secs_to_midnight)

        last_alerts.clear()
        print(f"[Midnight Reset] last_alerts cleared for {midnight.date()}")

        secs = next_market_open()
        print(f"Sleeping {secs/3600:.1f} h until market open.")
        time.sleep(secs)
