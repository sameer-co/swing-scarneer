"""
RSI(40) × WMA(15) Stock Scanner
Platform : Railway (Serverless mode)
Memory   : One symbol at a time, explicit del + gc after each
"""

import gc
import os
import random
import threading
import time
from datetime import datetime, timedelta, time as dtime

import pytz
import requests
from flask import Flask, jsonify

# ── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN          = os.getenv('TELEGRAM_TOKEN',  '8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE')
CHAT_ID        = os.getenv('TELEGRAM_CHAT_ID', '1950462171')
WATCHLIST_FILE = os.getenv('WATCHLIST_FILE',  'watchlist.txt')
PORT           = int(os.getenv('PORT', 8080))   # Railway injects PORT automatically

RSI_PERIOD = 40
WMA_PERIOD = 15
TIMEFRAMES = ['2h', '4h', '1d']
BLACKLIST  = {'WOCKHARDT.NS'}
IST        = pytz.timezone('Asia/Kolkata')

# ── SHARED STATE ──────────────────────────────────────────────────────────────
_lock       = threading.Lock()
last_alerts : dict = {}
_status     : dict = {
    "last_scan":    "never",
    "next_scan":    "09:20 IST Mon-Fri",
    "last_summary": "—",
}

# ── FLASK ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/health')
def health():
    """Railway pings this — keeps process alive during market hours."""
    return jsonify({"ok": True, "ist": datetime.now(IST).strftime('%H:%M:%S')}), 200

@app.route('/status')
def status():
    with _lock:
        s = dict(_status)
        s["dedup_keys"] = len(last_alerts)
    return jsonify(s), 200

@app.route('/')
def root():
    return (
        "<h3>Scanner ✅</h3>"
        "<a href='/health'>health</a> | "
        "<a href='/status'>status</a>"
    )

# ── HELPERS ───────────────────────────────────────────────────────────────────

def send_telegram(msg: str) -> None:
    url = (
        f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        f"?chat_id={CHAT_ID}"
        f"&text={requests.utils.quote(msg)}"
        f"&parse_mode=Markdown"
    )
    try:
        requests.get(url, timeout=10)
    except Exception as e:
        print(f"[TG Error] {e}")


def load_watchlist() -> list:
    if not os.path.exists(WATCHLIST_FILE):
        return ["RELIANCE.NS", "TCS.NS"]
    with open(WATCHLIST_FILE) as f:
        lines = f.read().splitlines()
    return list({
        (s.strip().upper() if "." in s else f"{s.strip().upper()}.NS")
        for s in lines
        if s.strip() and s.strip().upper() not in BLACKLIST
    })


def ist_now() -> datetime:
    return datetime.now(IST)


def is_market_open() -> bool:
    now = ist_now()
    if now.weekday() > 4:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)


# ── PER-SYMBOL SCAN (max memory efficiency) ───────────────────────────────────

def scan_symbol(symbol: str, hits: dict) -> None:
    """
    Download + process one symbol. All dataframes deleted immediately after use.
    yfinance and pandas_ta imported inside function so they are
    not resident in memory during the long sleep between scans.
    """
    import yfinance as yf
    import pandas_ta as ta

    raw = None
    try:
        raw = yf.download(
            symbol,
            period="30d",
            interval="1h",
            progress=False,
            auto_adjust=True,
        )

        if raw is None or raw.empty:
            return

        # Flatten MultiIndex columns if present
        if hasattr(raw.columns, 'levels'):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [str(c) for c in raw.columns]

        for tf in TIMEFRAMES:
            rule = {'2h': '2h', '4h': '4h', '1d': 'D'}[tf]

            work = raw.resample(rule).agg({
                'Open':   'first',
                'High':   'max',
                'Low':    'min',
                'Close':  'last',
                'Volume': 'sum',
            }).dropna()

            if len(work) < (RSI_PERIOD + WMA_PERIOD + 2):
                del work
                continue

            work = work.copy()
            work['RSI']     = ta.rsi(work['Close'], length=RSI_PERIOD)
            work['WMA_RSI'] = ta.wma(work['RSI'],   length=WMA_PERIOD)
            work.dropna(subset=['WMA_RSI'], inplace=True)

            if len(work) < 2:
                del work
                continue

            curr, prev = work.iloc[-1], work.iloc[-2]
            crossover  = (
                prev['RSI'] <= prev['WMA_RSI'] and
                curr['RSI'] >  curr['WMA_RSI']
            )

            close_price = float(curr['Close'])
            del work

            if not crossover:
                continue

            key = f"{symbol}_{tf}"
            now = datetime.now()
            with _lock:
                last_hit = last_alerts.get(key)
                if last_hit and (now - last_hit) < timedelta(hours=20):
                    continue
                last_alerts[key] = now

            hits[tf].append(f"`{symbol}` @ ₹{close_price:.2f}")
            print(f"  Signal: {key} @ ₹{close_price:.2f}")

    except Exception as e:
        print(f"  [Err] {symbol}: {e}")
    finally:
        if raw is not None:
            del raw
        gc.collect()


# ── FULL SCAN ─────────────────────────────────────────────────────────────────

def run_scan() -> None:
    if not is_market_open():
        print(f"[Skip] Market closed — {ist_now().strftime('%H:%M IST')}")
        return

    watchlist = load_watchlist()
    scan_time = ist_now().strftime('%d %b %H:%M')
    print(f"[Scan] {scan_time} — {len(watchlist)} symbols")

    hits: dict = {tf: [] for tf in TIMEFRAMES}

    for i, symbol in enumerate(watchlist):
        time.sleep(random.uniform(0.3, 0.8))   # gentle rate limiting
        scan_symbol(symbol, hits)
        if i % 10 == 0:
            gc.collect()

    # Send one Telegram message per timeframe
    summary_parts = []
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
            summary_parts.append(f"{tf}: {len(signals)} hit(s)")
        else:
            msg = (
                f"📭 *No signals — {tf.upper()}*\n"
                f"_Scan: {scan_time} IST_"
            )
            summary_parts.append(f"{tf}: none")
        send_telegram(msg)

    summary = " | ".join(summary_parts)
    print(f"[Done] {summary}")

    with _lock:
        _status["last_scan"]    = scan_time
        _status["last_summary"] = summary

    gc.collect()


def midnight_reset() -> None:
    with _lock:
        last_alerts.clear()
    gc.collect()
    print(f"[Reset] last_alerts cleared — {ist_now().date()}")


# ── SCHEDULER ─────────────────────────────────────────────────────────────────

def start_scheduler() -> None:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone=IST)

    slots = [(9,20),(10,15),(11,15),(12,15),(13,15),(14,15),(15,15)]
    for h, m in slots:
        scheduler.add_job(
            run_scan,
            CronTrigger(day_of_week='mon-fri', hour=h, minute=m, timezone=IST),
            id=f"scan_{h:02d}{m:02d}",
            misfire_grace_time=300,   # fire up to 5 min late after cold-start
            replace_existing=True,
        )

    scheduler.add_job(
        midnight_reset,
        CronTrigger(hour=0, minute=1, timezone=IST),
        id="midnight_reset",
        replace_existing=True,
    )

    scheduler.start()
    print(f"[Scheduler] {len(scheduler.get_jobs())} jobs registered")


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== RSI×WMA Scanner starting ===")
    start_scheduler()
    # Flask runs in main thread — keeps Railway process alive via /health pings
    # use_reloader=False prevents APScheduler from starting twice
    app.run(host="0.0.0.0", port=PORT, use_reloader=False, debug=False)
