import yfinance as yf
import pandas as pd
import pandas_ta as ta
import requests
import time
import random
import gc
import logging
from datetime import datetime, timedelta
import pytz
import os

# --- LOGGING SETUP ---
logging.basicConfig(
    filename="scanner.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# --- SECURE CONFIG ---
TOKEN   = os.getenv('TELEGRAM_TOKEN',   '8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '1950462171')
WATCHLIST_FILE = "watchlist.txt"

# --- STRATEGY PARAMETERS ---
RSI_PERIOD = 40
WMA_PERIOD = 15
TIMEFRAMES = ['2h', '4h']
BLACKLIST  = {"WOCKHARDT.NS"}

# --- FIXED SCAN SCHEDULE (IST) ---
# First scan at 9:20, then every hour from 10:15 to 15:15
SCAN_TIMES: list[str] = (
    ["09:20"] +
    [f"{h:02d}:15" for h in range(10, 16)]   # 10:15, 11:15 … 15:15
)

# --- STATE ---
last_alerts:       dict[str, datetime] = {}
daily_summary:     list[str]           = []
_watchlist_mtime:  float               = 0.0
_cached_watchlist: list[str]           = []
last_heartbeat:    datetime            = datetime.min


# ─────────────────────────── HELPERS ────────────────────────────

def send_telegram(message: str) -> None:
    url = (
        f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        f"?chat_id={CHAT_ID}&text={message}&parse_mode=Markdown"
    )
    try:
        requests.get(url, timeout=10)
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def load_watchlist() -> list[str]:
    """Hot-reload: re-reads file only when it has been modified on disk."""
    global _watchlist_mtime, _cached_watchlist

    if not os.path.exists(WATCHLIST_FILE):
        return ["RELIANCE.NS", "TCS.NS"]

    mtime = os.path.getmtime(WATCHLIST_FILE)
    if mtime == _watchlist_mtime:
        return _cached_watchlist          # unchanged — return cache

    log.info("Watchlist file changed — reloading.")
    with open(WATCHLIST_FILE) as f:
        lines = f.read().splitlines()

    symbols = set()
    for s in lines:
        s = s.strip().upper()
        if not s or s in BLACKLIST:
            continue
        symbols.add(s if "." in s else f"{s}.NS")

    _cached_watchlist = list(symbols)
    _watchlist_mtime  = mtime
    return _cached_watchlist


def prune_old_alerts() -> None:
    """Keep last_alerts dict small — drop entries older than 25 h."""
    cutoff = datetime.now() - timedelta(hours=25)
    stale  = [k for k, v in last_alerts.items() if v < cutoff]
    for k in stale:
        del last_alerts[k]


def get_next_scan_time(tz: pytz.BaseTzInfo) -> datetime:
    """Return the next scheduled scan as a timezone-aware datetime."""
    now = datetime.now(tz)
    for t in SCAN_TIMES:
        h, m = map(int, t.split(":"))
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > now:
            return candidate
    # All today's slots passed — first slot tomorrow (skip weekends)
    tomorrow = now + timedelta(days=1)
    while tomorrow.weekday() > 4:
        tomorrow += timedelta(days=1)
    h, m = map(int, SCAN_TIMES[0].split(":"))
    return tomorrow.replace(hour=h, minute=m, second=0, microsecond=0)


# ─────────────────────────── DOWNLOAD ───────────────────────────

RESAMPLE_MAP = {'2h': '2h', '4h': '4h', '1d': 'D'}
AGGS         = {'Open': 'first', 'High': 'max', 'Low': 'min',
                'Close': 'last', 'Volume': 'sum'}
MAX_RETRIES  = 3
RETRY_DELAY  = 10   # seconds between retries


def download_with_retry(watchlist: list[str]) -> dict | None:
    """
    Download 1 h OHLCV data with up to MAX_RETRIES attempts.
    Always returns a plain dict {symbol: DataFrame} regardless of
    watchlist size — fixes the single-symbol flat-column bug.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw = yf.download(
                watchlist,
                period="60d", interval="1h",
                group_by='ticker', progress=False,
                threads=True, auto_adjust=True
            )
            if raw is None or raw.empty:
                raise ValueError("Empty DataFrame returned")

            # Normalise to dict regardless of single vs multi symbol
            if len(watchlist) == 1:
                result = {watchlist[0]: raw}
            else:
                lvl0 = raw.columns.get_level_values(0).unique()
                result = {sym: raw[sym] for sym in watchlist if sym in lvl0}

            log.info(f"Download OK (attempt {attempt}): {len(result)} symbols")
            return result

        except Exception as e:
            log.warning(f"Download attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    log.error("All download attempts failed.")
    return None


# ─────────────────────────── SIGNAL CHECK ───────────────────────

def check_crossover(symbol: str, df: pd.DataFrame,
                    tf: str) -> tuple[str, str] | None:
    """
    Returns (alert_line, 'bull'|'bear') on a fresh crossover, else None.
    Checks both bullish AND bearish crossovers.
    """
    if df.empty or len(df) < (RSI_PERIOD + WMA_PERIOD + 2):
        return None

    rsi     = ta.rsi(df['Close'], length=RSI_PERIOD)
    wma_rsi = ta.wma(rsi,         length=WMA_PERIOD)

    # Only keep last 2 valid rows — avoids holding full series in memory
    valid = pd.DataFrame({'RSI': rsi, 'WMA': wma_rsi}).dropna().tail(2)
    if len(valid) < 2:
        return None

    prev, curr = valid.iloc[-2], valid.iloc[-1]
    last_close = df['Close'].iloc[-1]
    now        = datetime.now()

    def _fresh(key: str) -> bool:
        return key not in last_alerts or (now - last_alerts[key]) > timedelta(hours=20)

    # Bullish
    if prev['RSI'] <= prev['WMA'] and curr['RSI'] > curr['WMA']:
        key = f"{symbol}_{tf}_bull"
        if _fresh(key):
            last_alerts[key] = now
            return (f"  🟢 `{symbol}` — RSI {curr['RSI']:.2f} | ₹{last_close:.2f}", 'bull')

    # Bearish
    if prev['RSI'] >= prev['WMA'] and curr['RSI'] < curr['WMA']:
        key = f"{symbol}_{tf}_bear"
        if _fresh(key):
            last_alerts[key] = now
            return (f"  🔴 `{symbol}` — RSI {curr['RSI']:.2f} | ₹{last_close:.2f}", 'bear')

    return None


# ─────────────────────────── MAIN SCAN ──────────────────────────

def run_bulk_scan() -> None:
    global daily_summary, last_heartbeat

    watchlist = load_watchlist()
    log.info(f"Scan started — {len(watchlist)} symbols | timeframes: {TIMEFRAMES}")
    time.sleep(random.randint(2, 5))

    raw = download_with_retry(watchlist)
    if raw is None:
        send_telegram("⚠️ *Scanner*: Data download failed after 3 retries. Scan skipped.")
        return

    # Collect hits: hits[tf] = {'bull': [...lines], 'bear': [...lines]}
    hits: dict[str, dict[str, list[str]]] = {
        tf: {'bull': [], 'bear': []} for tf in TIMEFRAMES
    }
    scanned = 0

    for symbol in watchlist:
        try:
            ticker_df = raw.get(symbol)
            if ticker_df is None or ticker_df.empty:
                continue
            ticker_df = ticker_df.dropna(how='all')
            scanned += 1

            for tf in TIMEFRAMES:
                if tf == '1h':
                    work_df = ticker_df
                else:
                    rule = RESAMPLE_MAP.get(tf)
                    if not rule:
                        continue
                    # closed/label='left' avoids partial-candle edge issues
                    work_df = (
                        ticker_df
                        .resample(rule, closed='left', label='left')
                        .agg(AGGS)
                        .dropna()
                    )

                result = check_crossover(symbol, work_df, tf)
                if result:
                    line, kind = result
                    hits[tf][kind].append(line)
                    tag = "✅" if kind == 'bull' else "🔻"
                    daily_summary.append(
                        f"{tag} {symbol} ({tf}) [{kind.upper()}] @ ₹{ticker_df['Close'].iloc[-1]:.2f}"
                    )

                if tf != '1h':
                    del work_df

        except Exception as e:
            log.warning(f"Error processing {symbol}: {e}")
            continue
        finally:
            try:
                del ticker_df
            except NameError:
                pass

    # Free bulk download immediately
    del raw
    gc.collect()

    # ── ONE message per timeframe ──
    total_hits = 0
    for tf in TIMEFRAMES:
        bull_lines = hits[tf]['bull']
        bear_lines = hits[tf]['bear']
        n = len(bull_lines) + len(bear_lines)
        total_hits += n

        if n == 0:
            continue

        sections = []
        if bull_lines:
            sections.append("*📈 Bullish Crossovers:*\n" + "\n".join(bull_lines))
        if bear_lines:
            sections.append("*📉 Bearish Crossovers:*\n" + "\n".join(bear_lines))

        msg = (
            f"🔔 *CROSSOVER ALERT — {tf.upper()}*\n"
            f"_Scanned {scanned} symbols · {n} signal(s)_\n\n"
            + "\n\n".join(sections)
        )
        send_telegram(msg)
        log.info(f"Alert sent [{tf}]: {len(bull_lines)} bull, {len(bear_lines)} bear")

    # ── Heartbeat every 3 h when nothing fires ──
    now = datetime.now()
    if total_hits == 0 and (now - last_heartbeat) >= timedelta(hours=3):
        send_telegram(
            f"✅ *Scanner Heartbeat*\n"
            f"_Scanned {scanned} symbols across {', '.join(TIMEFRAMES)} — no signals this window._"
        )
        last_heartbeat = now
        log.info("Heartbeat sent.")

    prune_old_alerts()
    log.info(f"Scan complete — {scanned} scanned, {total_hits} total hits.")


def send_daily_report() -> None:
    global daily_summary
    if daily_summary:
        report = "📊 *DAILY SIGNAL SUMMARY*\n\n" + "\n".join(daily_summary)
    else:
        report = "📊 *Daily Summary*\n_No signals fired today._"
    send_telegram(report)
    daily_summary.clear()
    log.info("Daily report sent.")


# ─────────────────────────── MAIN LOOP ──────────────────────────

if __name__ == "__main__":
    tz = pytz.timezone('Asia/Kolkata')
    report_sent_today = False

    log.info("=== Scanner started ===")
    send_telegram(
        "🟢 *Scanner Online*\n"
        "📅 Schedule: " + "  |  ".join(SCAN_TIMES)
    )

    while True:
        now       = datetime.now(tz)
        is_market = (
            now.weekday() < 5 and
            datetime.strptime("09:15", "%H:%M").time()
            <= now.time() <=
            datetime.strptime("15:30", "%H:%M").time()
        )

        if is_market:
            report_sent_today = False
            next_scan = get_next_scan_time(tz)
            wait_sec  = max(0, (next_scan - now).total_seconds())

            if wait_sec > 0:
                log.info(f"Next scan at {next_scan.strftime('%H:%M')} — waiting {wait_sec:.0f}s")
                time.sleep(wait_sec)

            run_bulk_scan()

        else:
            # Post-market: send daily report once per day
            if not report_sent_today and now.weekday() < 5:
                send_daily_report()
                report_sent_today = True

            next_scan = get_next_scan_time(tz)
            wait_sec  = (next_scan - datetime.now(tz)).total_seconds()
            log.info(
                f"Market closed. Next scan {next_scan.strftime('%d-%b %H:%M')} "
                f"— sleeping {wait_sec / 3600:.1f}h"
            )
            time.sleep(max(wait_sec, 60))
