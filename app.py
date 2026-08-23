ALL_SIGNALS_TELEGRAM = False

# ─── Candlestick + Trade Validation Engine ────────────────────────────────────
def _candle_body(c):
    return abs(c["close"] - c["open"])

def _candle_range(c):
    return max(c["high"] - c["low"], 1e-12)

def detect_candlestick_patterns(candles):
    """Detect common single-, two-, and three-candle patterns."""
    if len(candles) < 3:
        return []
    a, b, c = candles[-3], candles[-2], candles[-1]
    out = []

    def bull(x): return x["close"] > x["open"]
    def bear(x): return x["close"] < x["open"]

    for x, name_bull, name_bear in [
        (c, "bullish_marubozu", "bearish_marubozu")
    ]:
        body, rng = _candle_body(x), _candle_range(x)
        if body / rng > 0.80:
            out.append(name_bull if bull(x) else name_bear)

    body = _candle_body(c)
    rng = _candle_range(c)
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]

    if body / rng < 0.10:
        out.append("doji")
    if lower > body * 2 and upper < body * 0.75:
        out.append("hammer" if bull(c) else "hanging_man")
    if upper > body * 2 and lower < body * 0.75:
        out.append("inverted_hammer" if bull(c) else "shooting_star")

    # Two candle
    if bull(c) and bear(b) and c["open"] <= b["close"] and c["close"] >= b["open"]:
        out.append("bullish_engulfing")
    if bear(c) and bull(b) and c["open"] >= b["close"] and c["close"] <= b["open"]:
        out.append("bearish_engulfing")

    # Harami
    if bull(b) and bear(c) and c["open"] >= b["close"] and c["close"] <= b["open"]:
        out.append("bearish_harami")
    if bear(b) and bull(c) and c["open"] <= b["close"] and c["close"] >= b["open"]:
        out.append("bullish_harami")

    # Piercing / dark cloud
    mid_b = (b["open"] + b["close"]) / 2
    if bear(b) and bull(c) and c["close"] > mid_b and c["close"] < b["open"]:
        out.append("piercing_line")
    if bull(b) and bear(c) and c["close"] < mid_b and c["close"] > b["open"]:
        out.append("dark_cloud_cover")

    # Tweezers
    tol = _candle_range(c) * 0.001
    if abs(c["low"] - b["low"]) <= tol:
        out.append("tweezer_bottom")
    if abs(c["high"] - b["high"]) <= tol:
        out.append("tweezer_top")

    # Three candle
    ba, bb, bc = _candle_body(a), _candle_body(b), _candle_body(c)
    ma = (a["open"] + a["close"]) / 2
    if bear(a) and bb < ba * 0.6 and bull(c) and c["close"] > ma:
        out.append("morning_star")
    if bull(a) and bb < ba * 0.6 and bear(c) and c["close"] < ma:
        out.append("evening_star")

    if bull(a) and bull(b) and bull(c) and b["close"] > a["close"] and c["close"] > b["close"]:
        out.append("three_white_soldiers")
    if bear(a) and bear(b) and bear(c) and b["close"] < a["close"] and c["close"] < b["close"]:
        out.append("three_black_crows")

    return sorted(set(out))


def classify_setup(score, gemini_result=None):
    """<70: no Gemini; >=70: Gemini; only APPROVE becomes a trade."""
    if float(score) < MIN_SCORE:
        return {"status": "NO TRADE", "trade": False, "reason": "Score below Gemini threshold"}
    if not gemini_result:
        return {"status": "WAIT/WATCH", "trade": False, "reason": "Awaiting Gemini validation"}
    decision = str(gemini_result.get("decision", "REJECT")).upper()
    if decision == "APPROVE":
        return {"status": "APPROVED", "trade": True,
                "reason": gemini_result.get("reason", "Gemini approved")}
    return {"status": "WAIT/WATCH", "trade": False,
            "reason": gemini_result.get("reason", "Gemini rejected")}


def _final_level_check(direction, entry, sl, tp1, tp2, tp3):
    vals = [entry, sl, tp1, tp2, tp3]
    if any(v is None for v in vals):
        return False, "Missing trade level"
    entry, sl, tp1, tp2, tp3 = map(float, vals)
    if direction == "LONG":
        if not (sl < entry < tp1 <= tp2 <= tp3):
            return False, "Invalid LONG level ordering"
    else:
        if not (sl > entry > tp1 >= tp2 >= tp3):
            return False, "Invalid SHORT level ordering"
    risk = abs(entry - sl)
    reward = abs(tp2 - entry)
    if risk <= 0 or reward / risk < 2.0:
        return False, "R:R below 1:2"
    return True, "OK"


def structure_agreement(quant_flags, ai):
    """How much quant's own structural read and Gemini's independent read of
    the SAME candles actually agree, checked key-by-key. Both sides are asked
    to detect bos/choch/order_block/fvg/crt/tbs independently -- if Gemini's
    reported structure disagrees with quant's on most of these, that's a real
    signal neither side should be blindly trusted here, whatever the headline
    scores say (headline scores were confirmed non-discriminating on real
    trade outcomes: avg quant score was ~55 for both wins AND losses, avg
    gemini score ~74 for both -- the specific factors are what carry signal,
    not the aggregate number). Returns (agree_count, total_checked, ratio)."""
    pairs = [('bos','bos_detected'), ('choch','choch_detected'), ('order_block','order_block_detected'),
             ('fvg','fvg_detected'), ('crt','crt_detected'), ('tbs','tbs_detected')]
    agree=0; total=0
    for qk, gk in pairs:
        gv = ai.get(gk)
        if gv is None:
            continue
        total += 1
        if bool(quant_flags.get(qk)) == bool(gv):
            agree += 1
    ratio = (agree/total) if total else 1.0
    return agree, total, ratio


def build_pattern_score(patterns, direction):
    bullish = {
        "hammer","inverted_hammer","bullish_engulfing","bullish_harami",
        "piercing_line","tweezer_bottom","morning_star","three_white_soldiers",
        "bullish_marubozu"
    }
    bearish = {
        "shooting_star","hanging_man","bearish_engulfing","bearish_harami",
        "dark_cloud_cover","tweezer_top","evening_star","three_black_crows",
        "bearish_marubozu"
    }
    # Capped low on purpose -- candle patterns are the weakest, noisiest
    # standalone confluence in SMC/ICT community consensus, kept mostly as a
    # tiebreaker. Was nudged UP to 8/-3 earlier on an n=2-win sample where it
    # happened to be common to both winners -- a much bigger sample since
    # (n=26-28 closed) flipped that to a small NEGATIVE edge, so reverted and
    # cut further rather than just restored, matching the now-negative sign.
    if direction == "LONG":
        return min(4, 2 * len(set(patterns) & bullish)) - min(2, len(set(patterns) & bearish))
    return min(4, 2 * len(set(patterns) & bearish)) - min(2, len(set(patterns) & bullish))
import os, re, json, time, math, sqlite3, threading, logging
from datetime import datetime, timezone, timedelta
from typing import Any

import requests
from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

# ----------------------------- Config ---------------------------------
PORT = int(os.getenv('PORT', '10000'))
BINANCE_BASE = os.getenv('BINANCE_BASE', 'https://fapi.binance.com')
BYBIT_BASE = os.getenv('BYBIT_BASE', 'https://api.bybit.com')
MEXC_BASE = os.getenv('MEXC_BASE', 'https://api.mexc.com')
EXCHANGE_ORDER = [x.strip().lower() for x in os.getenv('EXCHANGE_ORDER', 'binance,bybit,mexc').split(',') if x.strip()]
WATCHLIST = [x.strip().upper() for x in os.getenv('WATCHLIST', 'BTCUSDT,ETHUSDT,SOLUSDT').split(',') if x.strip()]
FRAMEWORK = os.getenv('FRAMEWORK', '1h_15m_5m')
SCAN_INTERVAL = max(5, int(os.getenv('SCAN_INTERVAL_MINUTES', '15')))
TRACK_INTERVAL = max(1, int(os.getenv('TRACK_INTERVAL_MINUTES', '1')))
TRACK_TIMEFRAME = os.getenv('TRACK_TIMEFRAME', '1m')
MIN_SCORE = max(0, min(100, int(os.getenv('MIN_SCORE', '70'))))
GEMINI_MIN_SCORE = max(0, min(100, int(os.getenv('GEMINI_MIN_SCORE', '65'))))
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
# Five-project fallback. GEMINI_API_KEY remains backward compatible as project 1.
GEMINI_API_KEYS = [
    os.getenv('GEMINI_API_KEY_1', '') or GEMINI_API_KEY,
    os.getenv('GEMINI_API_KEY_2', ''),
    os.getenv('GEMINI_API_KEY_3', ''),
    os.getenv('GEMINI_API_KEY_4', ''),
    os.getenv('GEMINI_API_KEY_5', ''),
]
GEMINI_API_KEYS = list(dict.fromkeys(k.strip() for k in GEMINI_API_KEYS if k and k.strip()))
GEMINI_MIN_INTERVAL = max(1.0, float(os.getenv('GEMINI_MIN_INTERVAL_SECONDS', '13')))
GEMINI_TIMEOUT = int(os.getenv('GEMINI_TIMEOUT_SECONDS', '60'))
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
DATA_DIR = os.getenv('DATA_DIR', './data')
RESET_ON_START = os.getenv('RESET_ON_START', '1').strip().lower() in ('1','true','yes','on')
TELEGRAM_AUTO_BIND = os.getenv('TELEGRAM_AUTO_BIND', '1').strip().lower() in ('1','true','yes','on')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'smc_pro.db')

TF_MAP = {
    '4h_1h_15m': ['4h', '1h', '15m'],
    '1h_15m_5m': ['1h', '15m', '5m'],
    '4h_15m_5m': ['4h', '15m', '5m'],
    '1h_30m_5m': ['1h', '30m', '5m'],
}
TIMEFRAMES = TF_MAP.get(FRAMEWORK, FRAMEWORK.split('_') if '_' in FRAMEWORK else ['1h','15m','5m'])
if len(TIMEFRAMES) != 3:
    TIMEFRAMES = ['1h','15m','5m']

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('smc-pro')
app = Flask(__name__)
scheduler = BackgroundScheduler(daemon=True)
DB_LOCK = threading.Lock()
GEMINI_LOCK = threading.Lock()
GEMINI_LAST_CALL = 0.0
SCAN_LOCK = threading.Lock()
TELEGRAM_OFFSET = 0
BOT_ACTIVE = False
STATE_LOCK = threading.Lock()
LAST_SCAN = None
LAST_ERROR = None

# ----------------------------- Utilities ------------------------------
def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with DB_LOCK:
        con = db()
        con.executescript('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, direction TEXT NOT NULL,
            entry REAL NOT NULL, sl REAL NOT NULL,
            tp1 REAL, tp2 REAL, tp3 REAL,
            score INTEGER, ai_confidence INTEGER,
            ai_reason TEXT, framework TEXT,
            created_at TEXT NOT NULL, entry_time TEXT,
            status TEXT NOT NULL DEFAULT 'WAITING_ENTRY',
            tp1_hit INTEGER DEFAULT 0, tp2_hit INTEGER DEFAULT 0, tp3_hit INTEGER DEFAULT 0,
            highest_tp INTEGER DEFAULT 0, sl_hit INTEGER DEFAULT 0,
            final_result TEXT, close_time TEXT,
            last_price REAL, last_checked TEXT
        );
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT, symbol TEXT, score INTEGER, decision TEXT,
            gemini_called INTEGER DEFAULT 0, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY, value TEXT
        );
        CREATE TABLE IF NOT EXISTS watch_state (
            symbol TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'IDLE',
            direction TEXT,
            bias_set_at TEXT,
            zone_reached_at TEXT,
            last_updated TEXT
        );
        ''')
        # Migration: older DBs won't have this column yet. SQLite has no
        # "ADD COLUMN IF NOT EXISTS", so try and swallow the duplicate-column
        # error. Stores which scoring factors fired on each trade so we can
        # later backtest which factor combinations actually win.
        try:
            con.execute('ALTER TABLE trades ADD COLUMN factors_json TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            con.execute('ALTER TABLE trades ADD COLUMN effective_sl REAL')
        except sqlite3.OperationalError:
            pass
        try:
            con.execute('ALTER TABLE trades ADD COLUMN pending_multipliers_json TEXT')
        except sqlite3.OperationalError:
            pass
        con.commit(); con.close()

def state_get(key, default=None):
    with DB_LOCK:
        con = db(); row = con.execute('SELECT value FROM bot_state WHERE key=?', (key,)).fetchone(); con.close()
    return row['value'] if row else default

def state_set(key, value):
    with DB_LOCK:
        con = db(); con.execute('INSERT INTO bot_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value))); con.commit(); con.close()

def current_chat_id():
    return state_get('telegram_chat_id', TELEGRAM_CHAT_ID) or TELEGRAM_CHAT_ID

def scanner_is_active():
    return state_get('active', '0') == '1'

def telegram_send(text, parse_mode='Markdown'):
    chat_id = current_chat_id()
    if not TELEGRAM_TOKEN or not chat_id:
        return False
    try:
        payload = {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True}
        if parse_mode:
            payload['parse_mode'] = parse_mode
        r = requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage', json=payload, timeout=10)
        if not r.ok:
            log.warning('Telegram error %s: %s', r.status_code, r.text[:300])
        return r.ok
    except Exception as e:
        log.warning('Telegram exception: %s', e)
        return False

def tf_label(tf):
    return tf.upper().replace('H','H').replace('M','M')

def fmt_price(v):
    """Adaptive-precision price formatting.

    A fixed '.6f' truncates micro-price coins (PEPE, SHIB, BONK, ...) down to
    a handful of decimals, e.g. 0.0000024789 -> 0.000002, which silently
    destroys the real entry/SL/TP level. This scales decimal places to the
    coin's own magnitude so the real level is always shown, then trims
    trailing zeros for readability.
    """
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    av = abs(v)
    if av == 0:
        return '0'
    if av >= 100:
        d = 2
    elif av >= 1:
        d = 4
    elif av >= 0.01:
        d = 6
    else:
        # keep ~5 significant figures past the first nonzero digit
        d = max(8, -int(math.floor(math.log10(av))) + 5)
    s = f'{v:.{d}f}'
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s

def fmt_price_display(v):
    """Fixed-width, comma-grouped price for the signal card (Entry/SL/TP
    column) -- e.g. $1,873.0500 or $0.00000288. Unlike fmt_price (which trims
    trailing zeros to keep other messages/payloads compact), this keeps a
    stable decimal width so the card's price column lines up cleanly."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    av = abs(v)
    if av == 0:
        return '$0.0000'
    d = 4 if av >= 0.01 else max(8, -int(math.floor(math.log10(av))) + 5)
    neg = v < 0
    return f'{"-" if neg else ""}${abs(v):,.{d}f}'

def _round_price(v):
    """Same adaptive precision as fmt_price but returns a rounded float, for
    payloads (e.g. to Gemini) where we want compact JSON numbers rather than
    display strings."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return v
    av = abs(v)
    if av == 0:
        return 0.0
    if av >= 100:
        d = 2
    elif av >= 1:
        d = 4
    elif av >= 0.01:
        d = 6
    else:
        d = max(8, -int(math.floor(math.log10(av))) + 5)
    return round(v, d)

def _compact_candles(candles, n=15):
    """Token-lean candle payload for Gemini: short keys (o/h/l/c/v), adaptive
    rounding, and no open_time (array order already conveys recency -- oldest
    first, most recent last). Full float precision (e.g.
    64221.499999999996) across 20 candles x 3 timeframes x 6 fields was the
    single largest token cost per Gemini call; this cuts the candle payload
    to roughly a third of its previous size."""
    out=[]
    for c in candles[-n:]:
        out.append({
            'o':_round_price(c.get('open')), 'h':_round_price(c.get('high')),
            'l':_round_price(c.get('low')), 'c':_round_price(c.get('close')),
            'v':round(float(c['volume']),2) if c.get('volume') is not None else None,
        })
    return out

def build_signal_card(trade_plan, ai, best, best_score, gemini_score):
    """The detailed Telegram signal card format -- plain text (no Markdown
    bold), fixed-width prices, explicit UTC timestamp, and an Action line
    that deterministically says 'enter now' vs 'limit order' based on how far
    the approved entry sits from the live price, rather than trusting a
    self-reported field."""
    direction=trade_plan['direction']; symbol=trade_plan['symbol']
    icon='🟢 ▲' if direction=='LONG' else '🔴 ▼'
    current_price=best.get('price', trade_plan['entry'])
    entry=trade_plan['entry']
    dist=abs(entry-current_price)/current_price if current_price else 0
    action=f'Enter now (market ~ {fmt_price_display(current_price)})' if dist<=0.0015 else f'Limit order @ {fmt_price_display(entry)}'
    prob=int(ai.get('confidence',0) or 0)
    rr_val=trade_plan.get('rr',0)
    rr_str=f'1:{rr_val:.0f}' if abs(rr_val-round(rr_val))<0.05 else f'1:{rr_val:.1f}'
    pattern=ai.get('pattern_summary') or (', '.join(ai.get('candle_patterns') or []) or 'N/A')
    location=ai.get('location_summary') or '\u2014'
    meaning=ai.get('meaning_summary') or '\u2014'
    summary=ai.get('reason','')
    ts=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    tf_str=''.join(trade_plan.get('timeframes', TIMEFRAMES))
    bar='\u2501'*20
    lines=[
        f'{icon} {direction} \u2014 {symbol}', bar,
        f'\U0001F4B0 Entry    : {fmt_price_display(entry)}',
        f'\U0001F6D1 Stop Loss: {fmt_price_display(trade_plan["sl"])}',
        f'\U0001F3AF TP 1     : {fmt_price_display(trade_plan["tp1"])}',
        f'\U0001F3AF TP 2     : {fmt_price_display(trade_plan["tp2"])}',
        f'\U0001F3AF TP 3     : {fmt_price_display(trade_plan["tp3"])}', bar,
        f'\U0001F4CA R:R      : {rr_str}',
        f'\U0001F3B2 Prob     : {prob}%',
        f'Quant score : {best_score:.0f}',
        f'gemini score: {gemini_score:.0f}', bar,
        f'\U0001F56F\uFE0F Pattern  : {pattern}',
        f'\U0001F4CD Location : {location}',
        f'\U0001F4D6 Meaning  : {meaning}',
        f'\u26A1 Action   : {action}', bar,
        f'\U0001F4DD {summary}',
        f'\u23F0 {ts} | {tf_str}',
    ]
    return '\n'.join(lines)

# ----------------------------- Market Data / Exchange Fallbacks --------
_INTERVAL_SECONDS = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600, '8h': 28800,
    '12h': 43200, '1d': 86400,
}
_BYBIT_INTERVAL = {'1m':'1','3m':'3','5m':'5','15m':'15','30m':'30','1h':'60','2h':'120','4h':'240','6h':'360','8h':'480','12h':'720','1d':'D'}
_MEXC_INTERVAL = {'1m':'Min1','5m':'Min5','15m':'Min15','30m':'Min30','1h':'Min60','4h':'Hour4','8h':'Hour8','1d':'Day1'}

def _fetch_binance(symbol, interval, limit):
    r = requests.get(f'{BINANCE_BASE}/fapi/v1/klines', params={'symbol':symbol,'interval':interval,'limit':limit}, timeout=20)
    r.raise_for_status()
    out=[]
    for x in r.json():
        out.append({'open_time':x[0], 'open':float(x[1]), 'high':float(x[2]), 'low':float(x[3]), 'close':float(x[4]), 'volume':float(x[5])})
    return out

def _fetch_bybit(symbol, interval, limit):
    bi = _BYBIT_INTERVAL.get(interval)
    if not bi: raise ValueError(f'Bybit does not support interval {interval}')
    r = requests.get(f'{BYBIT_BASE}/v5/market/kline', params={'category':'linear','symbol':symbol,'interval':bi,'limit':min(limit,1000)}, timeout=20)
    r.raise_for_status(); payload=r.json()
    if payload.get('retCode') != 0: raise RuntimeError(payload.get('retMsg','Bybit error'))
    rows=(payload.get('result') or {}).get('list') or []
    # Bybit returns newest first: normalize to oldest -> newest.
    rows=list(reversed(rows))
    return [{'open_time':int(x[0]),'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'volume':float(x[5])} for x in rows]

def _fetch_mexc(symbol, interval, limit):
    mi = _MEXC_INTERVAL.get(interval)
    if not mi: raise ValueError(f'MEXC does not support interval {interval}')
    msymbol = symbol[:-4] + '_USDT' if symbol.endswith('USDT') else symbol.replace('/','_')
    sec=_INTERVAL_SECONDS.get(interval,60); end=int(time.time()); start=end-(limit*sec)
    r=requests.get(f'{MEXC_BASE}/api/v1/contract/kline/{msymbol}', params={'interval':mi,'start':start,'end':end}, timeout=20)
    r.raise_for_status(); payload=r.json()
    if not payload.get('success', False): raise RuntimeError(payload.get('message','MEXC error'))
    d=payload.get('data') or {}
    times=d.get('time') or []; opens=d.get('open') or []; closes=d.get('close') or []; highs=d.get('high') or []; lows=d.get('low') or []; vols=d.get('vol') or []
    n=min(len(times),len(opens),len(closes),len(highs),len(lows),len(vols))
    return [{'open_time':int(times[i])*1000,'open':float(opens[i]),'high':float(highs[i]),'low':float(lows[i]),'close':float(closes[i]),'volume':float(vols[i])} for i in range(n)][-limit:]

def fetch_klines(symbol, interval, limit=150):
    errors=[]
    for exchange in EXCHANGE_ORDER:
        try:
            if exchange == 'binance': rows=_fetch_binance(symbol,interval,limit)
            elif exchange == 'bybit': rows=_fetch_bybit(symbol,interval,limit)
            elif exchange == 'mexc': rows=_fetch_mexc(symbol,interval,limit)
            else: continue
            if len(rows) >= min(limit,3):
                if exchange != EXCHANGE_ORDER[0]: log.warning('[%s %s] market-data fallback -> %s', symbol, interval, exchange)
                return rows
            errors.append(f'{exchange}: insufficient candles ({len(rows)})')
        except Exception as exc:
            errors.append(f'{exchange}: {type(exc).__name__}: {exc}')
            log.warning('[%s %s] %s failed: %s', symbol, interval, exchange, exc)
    raise RuntimeError(f'All market-data sources failed for {symbol} {interval}: ' + ' | '.join(errors))

# ----------------------------- Indicators -----------------------------
def ema(vals, n):
    if not vals: return None
    a = 2/(n+1); e = vals[0]
    for v in vals[1:]: e = a*v + (1-a)*e
    return e

def rsi(vals, n=14):
    if len(vals) <= n: return 50.0
    gains=[]; losses=[]
    for i in range(1,len(vals)):
        d=vals[i]-vals[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains[:n])/n; al=sum(losses[:n])/n
    for g,l in zip(gains[n:],losses[n:]):
        ag=(ag*(n-1)+g)/n; al=(al*(n-1)+l)/n
    if al == 0: return 100.0
    return 100 - 100/(1+ag/al)

def atr(candles, n=14):
    if len(candles) < n+1: return 0
    trs=[]
    for i in range(1,len(candles)):
        c=candles[i]; p=candles[i-1]
        trs.append(max(c['high']-c['low'], abs(c['high']-p['close']), abs(c['low']-p['close'])))
    return sum(trs[-n:])/min(n,len(trs))

def slope(vals, look=5):
    if len(vals)<look+1: return 0
    return vals[-1]-vals[-1-look]

# ----------------------------- SMC / Score -----------------------------
def swing_levels(c, window=3):
    highs=[]; lows=[]
    for i in range(window, len(c)-window):
        h=c[i]['high']; l=c[i]['low']
        if h == max(x['high'] for x in c[i-window:i+window+1]): highs.append((i,h))
        if l == min(x['low'] for x in c[i-window:i+window+1]): lows.append((i,l))
    return highs, lows

def detect_equal_levels(swings, tolerance_pct=0.0015):
    """Equal Highs / Equal Lows: a genuine SMC liquidity-pool concept not
    previously implemented. Real resting stops/liquidity cluster where
    several swing points sit at nearly the same price -- a far stronger,
    more reliable sweep target/magnet than one arbitrary single swing point.
    Looks at the most recent handful of swings and returns the tightest
    cluster of 2+ points within tolerance_pct of each other, or None."""
    if len(swings) < 2:
        return None
    prices=[p for _,p in swings[-6:]]
    best=None
    for i in range(len(prices)):
        cluster=[prices[i]]
        for j in range(len(prices)):
            if i==j: continue
            if abs(prices[j]-prices[i])/prices[i] <= tolerance_pct:
                cluster.append(prices[j])
        if len(cluster)>=2 and (best is None or len(cluster)>best['count']):
            best={'level':sum(cluster)/len(cluster),'count':len(cluster)}
    return best

def analyze_tf(c):
    closes=[x['close'] for x in c]; vols=[x['volume'] for x in c]
    e20=ema(closes,20); e50=ema(closes,50); e200=ema(closes,200) if len(closes)>=50 else None; r=rsi(closes); a=atr(c)
    highs,lows=swing_levels(c,3)
    last=c[-1]; prev=c[-2]
    recent_high=max(x['high'] for x in c[-30:]); recent_low=min(x['low'] for x in c[-30:])
    avgvol=sum(vols[-20:])/min(20,len(vols)); vol_ratio=(last['volume']/avgvol) if avgvol else 1
    bullish = e20 > e50 and last['close'] > e20 and slope(closes,5)>0
    bearish = e20 < e50 and last['close'] < e20 and slope(closes,5)<0
    bias='BULLISH' if bullish else 'BEARISH' if bearish else 'NEUTRAL'
    # liquidity sweep: price took out a prior swing extreme within the last
    # few candles and has since closed back on the right side of it. This is
    # checked across a short window (not just the single latest candle) --
    # requiring the wick AND the reclaim close in the exact same bar was
    # missing the vast majority of real sweeps, which typically wick out on
    # one candle and get reclaimed one or two candles later.
    prior_high=max(x['high'] for x in c[-21:-1]); prior_low=min(x['low'] for x in c[-21:-1])
    swing_ref_high=max(x['high'] for x in c[-23:-3]); swing_ref_low=min(x['low'] for x in c[-23:-3])
    recent3=c[-3:]
    swept_low = min(x['low'] for x in recent3) < swing_ref_low
    swept_high = max(x['high'] for x in recent3) > swing_ref_high
    bull_sweep = swept_low and last['close'] > swing_ref_low
    bear_sweep = swept_high and last['close'] < swing_ref_high
    # simple BOS: close breaks previous 20-candle extreme -- checked over the
    # last 2 candles, not just the literal most-recent one. A break that
    # happened one candle ago is still fresh/tradeable; requiring it to be
    # the EXACT last candle was a common source of quant missing a BOS that
    # Gemini (looking at the whole recent picture, not one bar) correctly saw.
    recent2=c[-2:]
    bull_bos = any(x['close'] > prior_high for x in recent2)
    bear_bos = any(x['close'] < prior_low for x in recent2)
    # momentum confirmation
    bull_mom = r >= 52 and r <= 72
    bear_mom = r <= 48 and r >= 28
    return {
        'bias':bias, 'ema20':e20, 'ema50':e50, 'ema200':e200, 'rsi':r, 'atr':a,
        'volume_ratio':vol_ratio, 'bull_sweep':bull_sweep, 'bear_sweep':bear_sweep,
        'bull_bos':bull_bos, 'bear_bos':bear_bos,
        'bull_mom':bull_mom, 'bear_mom':bear_mom,
        'high':recent_high, 'low':recent_low, 'price':last['close'],
        'range':max(1e-12,recent_high-recent_low)
    }

def detect_zone(c, direction):
    # Lightweight OB/FVG proxy: use last opposite candle before a displacement candle.
    if len(c)<5: return None
    for i in range(len(c)-2, max(1,len(c)-12), -1):
        cur=c[i+1]; prev=c[i]
        if direction=='LONG' and cur['close']>cur['open'] and prev['close']<prev['open']:
            return {'low':prev['low'], 'high':prev['high'], 'type':'bullish_ob'}
        if direction=='SHORT' and cur['close']<cur['open'] and prev['close']>prev['open']:
            return {'low':prev['low'], 'high':prev['high'], 'type':'bearish_ob'}
    return None


# ---------------- TBS + CRT confirmations ----------------
def detect_crt(candles):
    """CRT-style range sweep/reclaim confirmation. candles = list of OHLCV dicts (oldest->newest)."""
    try:
        if candles is None or len(candles) < 3:
            return False, "CRT unavailable"
        prev, cur = candles[-2], candles[-1]
        ph, pl = float(prev["high"]), float(prev["low"])
        co, cc = float(cur["open"]), float(cur["close"])
        ch, cl = float(cur["high"]), float(cur["low"])
        mid = (ph + pl) / 2.0
        if cl < pl and cc > mid and cc > co:
            return True, "CRT bullish sweep/reclaim"
        if ch > ph and cc < mid and cc < co:
            return True, "CRT bearish sweep/reclaim"
    except Exception:
        pass
    return False, "No CRT confirmation"

def detect_tbs(candles):
    """TBS-style failed breakout/reclaim confirmation. candles = list of OHLCV dicts (oldest->newest)."""
    try:
        if candles is None or len(candles) < 3:
            return False, "TBS unavailable"
        prev, cur = candles[-2], candles[-1]
        ph, pl = float(prev["high"]), float(prev["low"])
        co, cc = float(cur["open"]), float(cur["close"])
        ch, cl = float(cur["high"]), float(cur["low"])
        if cl < pl and cc > pl and cc > co:
            return True, "TBS bullish failed breakdown"
        if ch > ph and cc < ph and cc < co:
            return True, "TBS bearish failed breakout"
    except Exception:
        pass
    return False, "No TBS confirmation"

def tbs_crt_bonus(candles, direction):
    """+4 each for CRT/TBS confirmation aligned with `direction`, capped at 8."""
    bonus, reasons = 0, []
    for detector in (detect_crt, detect_tbs):
        ok, reason = detector(candles)
        if ok:
            bullish = "bullish" in reason.lower()
            aligned = (direction == "LONG" and bullish) or (direction == "SHORT" and not bullish)
            if aligned:
                bonus += 4
                reasons.append(reason)
    return min(bonus, 8), reasons


# ---------------- CHOCH + FVG detection ----------------
def detect_choch(c, bias, window=3):
    """Change of CHaracter: price breaks the most recent opposite-side swing point
    while the prevailing EMA bias is not already confidently aligned with that
    break -- an early reversal/transition signal (distinct from BOS, which is a
    continuation break in an already-aligned trend).

    Was previously gated on bias being the FULL OPPOSITE ('BEARISH' for a bull
    CHoCH) -- but candidates only score highly for LONG once a2's bias is
    already BULLISH from several other factors, so that condition and a bull
    CHoCH could structurally never both be true at once. Confirmed in live
    data: 0/17 real closed trades ever had choch=true. Loosened to "not
    already aligned the same way" (BEARISH or NEUTRAL) so it can actually
    fire on genuine early-transition setups instead of being permanently dead.
    """
    out = {'bull_choch': False, 'bear_choch': False}
    highs, lows = swing_levels(c, window)
    if not highs or not lows:
        return out
    last = c[-1]
    last_swing_high = highs[-1][1]
    last_swing_low = lows[-1][1]
    out['bull_choch'] = bias != 'BULLISH' and last['close'] > last_swing_high
    out['bear_choch'] = bias != 'BEARISH' and last['close'] < last_swing_low
    return out

def detect_fvg(c, direction, lookback=15):
    """3-candle Fair Value Gap: an imbalance left by a displacement candle that
    price has not yet filled."""
    if len(c) < 3:
        return None
    for i in range(len(c) - 1, max(1, len(c) - lookback), -1):
        a, disp, cur = c[i - 2], c[i - 1], c[i]
        if direction == 'LONG' and a['high'] < cur['low']:
            return {'low': a['high'], 'high': cur['low'], 'type': 'bullish_fvg'}
        if direction == 'SHORT' and a['low'] > cur['high']:
            return {'low': cur['high'], 'high': a['low'], 'type': 'bearish_fvg'}
    return None


def build_analysis(symbol):
    tf1,tf2,tf3=TIMEFRAMES
    c1=fetch_klines(symbol,tf1,220); c2=fetch_klines(symbol,tf2,160); c3=fetch_klines(symbol,tf3,160)
    a1,a2,a3=analyze_tf(c1),analyze_tf(c2),analyze_tf(c3)
    # Score both directions. Require MTF alignment, but allow one lower TF disagreement if setup is strong.
    #
    # Weights below were rebalanced against SMC/ICT community backtests and
    # published guides (liquidity sweep + a body-close BOS are consistently
    # cited as the strongest, most independently-predictive confluences;
    # CHoCH is repeatedly described as an early warning/reversal *alert*
    # rather than a standalone entry trigger, so it should never weigh as
    # much as a confirmed BOS; a lone Order Block with nothing else behind
    # it is called out as the single most common false-signal source --
    # "most order blocks fail... only trade the ones with confluence"). See
    # notes inline at each factor. Treat this as a documented starting point,
    # not a guarantee -- forward-test / backtest before trusting it with size.
    scores={'LONG':0,'SHORT':0}; reasons={'LONG':[],'SHORT':[]}
    # HTF bias 18 -- consistently required in practice already (fires on
    # ~92-100% of real trades across every sample checked); kept as-is.
    if a1['bias']=='BULLISH': scores['LONG']+=18; reasons['LONG'].append('HTF bullish')
    if a1['bias']=='BEARISH': scores['SHORT']+=18; reasons['SHORT'].append('HTF bearish')
    # 1H 200 EMA position 4 -- part of the user's stated 3-stage methodology
    # ("price 200 EMA ke upar ya neeche") which this system didn't check at
    # all before (only had EMA20/EMA50). Small bonus, not a hard gate --
    # EMA200 on a 220-candle 1H fetch is still a bit short of fully settled,
    # so this is treated as a confirming nudge, not a strong standalone
    # factor, until there's backtest data on it specifically.
    if a1.get('ema200') is not None:
        if a1['price']>a1['ema200']: scores['LONG']+=4; reasons['LONG'].append('1H above 200EMA')
        if a1['price']<a1['ema200']: scores['SHORT']+=4; reasons['SHORT'].append('1H below 200EMA')
    # Market regime (trend strength via EMA separation/ATR) 15 -- raised
    # again. This factor's edge has now REPLICATED across three separate
    # live samples (n=11, n=17, n=26/28) and every single time the "trend
    # regime absent" bucket showed a 0% win rate. That's the most
    # consistently-reproduced signal found in this system so far -- also
    # gated as a HARD FILTER below (build_analysis returns a "no_trade"
    # candidate if this is absent), not just a score weight.
    sep=abs(a1['ema20']-a1['ema50'])/max(a1['atr'],1e-12)
    trend_regime_ok = sep >= 1.0
    if trend_regime_ok:
        for d in scores: scores[d]+=15 if ((d=='LONG' and a1['bias']=='BULLISH') or (d=='SHORT' and a1['bias']=='BEARISH')) else 0
        if a1['bias'] in ('BULLISH','BEARISH'): reasons[a1['bias'].replace('BULLISH','LONG').replace('BEARISH','SHORT')].append('trending regime')
    # Liquidity sweep 10 -- MOVED from 15M (a2) to 5M (a3). This is the
    # user's stated methodology: 1H decides bias, 15M identifies the
    # zone (OB/FVG -- kept on a2 below), and 5M is where the actual sweep +
    # structure-shift ENTRY TRIGGER happens. Checking sweep/BOS/CHoCH on 15M
    # blurred "zone" and "trigger" into the same timeframe; this separates
    # them properly. Weight itself unchanged (still lowered from the
    # original 20 -- see below).
    #
    # Weight lowered from 20. Community consensus says this should be one of
    # the strongest factors, but real data on the OLD (15M) wiring
    # disagreed consistently: -31.8 to -38.5pt edge across three separate
    # samples (though always on a thin n=4 "with sweep" bucket). Moving the
    # check to 5M is a fresh start for this factor -- watch /api/backtest
    # again once enough trades accumulate under the new wiring before
    # re-adjusting the weight either way.
    if a3['bull_sweep']: scores['LONG']+=10; reasons['LONG'].append('5M sell-side liquidity sweep')
    if a3['bear_sweep']: scores['SHORT']+=10; reasons['SHORT'].append('5M buy-side liquidity sweep')
    # BOS 12 -- MOVED from 15M to 5M for the same reason as sweep above (this
    # is the "MSS" / entry-trigger structure break in the user's 3-stage
    # model, not the zone-finding timeframe). Weight kept the same for now;
    # prior edge data was measured on the old 15M wiring and needs re-
    # checking against fresh trades under the new one.
    if a3['bull_bos']: scores['LONG']+=12; reasons['LONG'].append('5M bullish BOS')
    if a3['bear_bos']: scores['SHORT']+=12; reasons['SHORT'].append('5M bearish BOS')
    # Order Block 12 -- raised from 8. Real edge has been consistently
    # positive and sizeable (+17.3 to +35.7pts across samples), still gated
    # on confluence (same-direction sweep or FVG nearby) so a lone OB with
    # nothing backing it still doesn't score. The OB/FVG *zone* itself stays
    # identified on 15M (c2) -- only the confluence check now looks at the
    # 5M sweep, matching "zone on 15M, trigger on 5M".
    for d in ('LONG','SHORT'):
        z=detect_zone(c2,d)
        if z and z['low'] <= a3['price'] <= z['high']*1.002:
            sweep_ok = a3['bull_sweep'] if d=='LONG' else a3['bear_sweep']
            fvg_ok = detect_fvg(c2,d) is not None
            if sweep_ok or fvg_ok:
                scores[d]+=12; reasons[d].append('price at order-block zone (confluence-backed)')
    # CHOCH 5 -- MOVED from 15M to 5M, same reasoning as sweep/BOS above:
    # this is the entry-trigger timeframe's structure-shift signal, not the
    # zone-identification timeframe's. Kept low; SMC guides treat CHoCH as
    # an early reversal *warning*, not a confirmed entry trigger the way BOS
    # is.
    choch=detect_choch(c3, a3['bias'])
    if choch['bull_choch']: scores['LONG']+=5; reasons['LONG'].append('5M bullish CHoCH')
    if choch['bear_choch']: scores['SHORT']+=5; reasons['SHORT'].append('5M bearish CHoCH')
    # FVG 8 -- slightly raised, edge has settled close to neutral-to-positive
    # as sample grew (was -23.3 on a tiny sample, now roughly neutral).
    fvg_hits={}
    for d in ('LONG','SHORT'):
        fvg=detect_fvg(c2,d)
        fvg_hits[d]=fvg
        if fvg and fvg['low'] <= a3['price'] <= fvg['high']*1.002:
            scores[d]+=8; reasons[d].append('price inside FVG')
    # CRT + TBS confirmation (lower-timeframe sweep/reclaim), up to 8 (+4 each).
    crt_tbs_hits={}
    for d in ('LONG','SHORT'):
        bonus,breasons=tbs_crt_bonus(c3,d)
        crt_tbs_hits[d]={'bonus':bonus,'reasons':breasons}
        if bonus:
            scores[d]+=bonus; reasons[d].extend(breasons)
    # Candle patterns (single/double/triple) on the lowest timeframe, up to 4 / -2.
    # REVERTED back down -- an earlier bump to 6/-3 was based on an n=2-win
    # sample where candle_pattern happened to be common to both; a much
    # bigger sample since (n=26-28) flipped this to a small NEGATIVE edge
    # (-2.3 to -4.8pts). Exactly the overfitting risk that was flagged when
    # that bump was made -- reverted, and cut further given the now-negative
    # sign, not just back to the old value.
    patterns=detect_candlestick_patterns(c3)
    for d in ('LONG','SHORT'):
        pbonus=build_pattern_score(patterns,d)
        if pbonus:
            scores[d]=max(0,scores[d]+pbonus)
            if pbonus>0: reasons[d].append(f'candle pattern(s): {", ".join(patterns)}')
    # Volume 5: this is the one factor whose negative edge REPLICATED across
    # multiple independent samples -- unlike several others above that
    # flipped sign as sample grew, this one has stayed consistently negative,
    # so kept lower with more confidence than a single-sample cut would
    # justify. Still not dropped to zero -- keep watching /api/backtest.
    if a3['volume_ratio']>=1.2:
        d='LONG' if a3['bull_mom'] else 'SHORT' if a3['bear_mom'] else None
        if d: scores[d]+=5; reasons[d].append('volume expansion')
    # Momentum 3
    if a3['bull_mom']: scores['LONG']+=3; reasons['LONG'].append(f'RSI {a3["rsi"]:.0f}')
    if a3['bear_mom']: scores['SHORT']+=3; reasons['SHORT'].append(f'RSI {a3["rsi"]:.0f}')
    # R:R is calculated after entry/SL/TP plan.
    direction=max(scores, key=scores.get); base=scores[direction]
    price=a3['price']; a=a3['atr'] or (price*0.005)
    # Zone bounds for the WINNING direction -- exposed so a limit-order can
    # be placed at the zone's near edge (waiting for price to pull back into
    # it) rather than only ever reading confluence at whatever price happens
    # to be RIGHT NOW.
    _zone=detect_zone(c2,direction) or detect_fvg(c2,direction)
    zone_entry=None
    if _zone:
        zone_entry=_zone['high'] if direction=='LONG' else _zone['low']
    # Structural SL: placed below/above the actual recent swing low/high (not
    # just a fixed ATR multiple from the current price), with a small ATR
    # buffer on top so normal noise doesn't tag it. A fixed "price - 1.2*ATR"
    # ignores where the real structure actually sits -- if the swept/pullback
    # low is much closer than 1.2 ATR, that fixed formula overshoots past
    # perfectly good structure into wasted risk; if it's further away, the
    # fixed formula can plant the stop LEFT INSIDE normal chop below the
    # entry, exactly the "SL hits before the real move happens" pattern seen
    # in the live trade log. Bounded so it's never tighter than 0.6 ATR (pure
    # noise) nor wider than 3.0 ATR (a broken/outlier swing level) from price.
    highs1, lows1 = swing_levels(c1, 3)
    highs2, lows2 = swing_levels(c2, 3)
    eqh1=detect_equal_levels(highs1); eql1=detect_equal_levels(lows1)
    eqh2=detect_equal_levels(highs2); eql2=detect_equal_levels(lows2)
    if direction=='LONG':
        structural=lows2[-1][1] if lows2 else price-1.2*a
        # If the structural low IS an Equal-Lows liquidity pool (2+ swing
        # lows clustered together), give it MORE room, not less -- this is
        # exactly the pattern confirmed visually live (LINKUSDT, HYPEUSDT):
        # price wicks through a well-defined pool to hunt stops, THEN
        # reverses hard the intended direction. A pool draws a sharper,
        # further-overshooting wick than a random single swing low does.
        eql=eql2 or eql1
        pool_hit=eql and abs(eql['level']-structural)/price<=0.01
        buf=0.5*a if pool_hit else 0.25*a
        sl=min(structural-buf, price-0.6*a)
        sl=max(sl, price-(3.5*a if pool_hit else 3.0*a))
        risk=price-sl
        if pool_hit: reasons[direction].append(f'SL widened -- sits at an equal-lows liquidity pool ({eql["count"]}x)')
    else:
        structural=highs2[-1][1] if highs2 else price+1.2*a
        eqh=eqh2 or eqh1
        pool_hit=eqh and abs(eqh['level']-structural)/price<=0.01
        buf=0.5*a if pool_hit else 0.25*a
        sl=max(structural+buf, price+0.6*a)
        sl=min(sl, price+(3.5*a if pool_hit else 3.0*a))
        risk=sl-price
        if pool_hit: reasons[direction].append(f'SL widened -- sits at an equal-highs liquidity pool ({eqh["count"]}x)')
    # Equal Highs/Lows confluence 6 -- only credited when our OWN sweep flag
    # also fired in the same direction, so this is confirmation that the
    # swept level was a real multi-touch liquidity pool (stronger, more
    # reliable) rather than one arbitrary single swing point, not a
    # standalone signal on its own.
    if eql1 and a2['bull_sweep']:
        scores['LONG']+=6; reasons['LONG'].append(f"swept equal-lows pool ({eql1['count']}x @ {fmt_price(eql1['level'])})")
    if eqh1 and a2['bear_sweep']:
        scores['SHORT']+=6; reasons['SHORT'].append(f"swept equal-highs pool ({eqh1['count']}x @ {fmt_price(eqh1['level'])})")
    # Auto R:R sizing: the FINAL target (TP3, the "let it run" leg) scales
    # with how many independent confluences actually confirmed this setup --
    # a thin setup gets a realistic, tighter target (1:2); a setup backed by
    # many aligned confluences gets room to run further (up to 1:5). TP1/TP2
    # stay capped at their usual near-term levels (1.5R/2.3R) either way --
    # only the final leg moves, so the early breakeven-lock behavior (see
    # check_trade) doesn't change for weak vs strong setups, just how far we
    # let a genuinely strong one run before calling it done.
    confirm_count=len(reasons[direction])
    tp3_mult=2.0 if confirm_count<=3 else 3.2 if confirm_count<=6 else 5.0
    tp1_mult=min(1.5,tp3_mult*0.35); tp2_mult=min(2.3,tp3_mult*0.6)
    if direction=='LONG':
        tp1=price+tp1_mult*risk; tp2=price+tp2_mult*risk; tp3=price+tp3_mult*risk
        # Anchor TP3 to a real structural level -- prefer an Equal-Highs
        # liquidity pool above price (a genuine "draw on liquidity" magnet,
        # the concept behind "TP = previous high / liquidity pool"), falling
        # back to the plain recent 1H swing high if no pool is detected.
        # Only caps the ATR-based guess (never extends beyond it), and only
        # if the level still leaves room beyond TP2 to be a meaningful
        # third target.
        structural_cap=eqh1['level'] if (eqh1 and eqh1['level']>price) else a1.get('high')
        if structural_cap and structural_cap>tp2*1.002:
            tp3=min(tp3, structural_cap)
    else:
        tp1=price-tp1_mult*risk; tp2=price-tp2_mult*risk; tp3=price-tp3_mult*risk
        structural_cap=eql1['level'] if (eql1 and eql1['level']<price) else a1.get('low')
        if structural_cap and structural_cap<tp2*0.998:
            tp3=max(tp3, structural_cap)
    rr=abs(tp3-price)/max(abs(price-sl),1e-12)
    reasons[direction].append(f'auto R:R target 1:{tp3_mult:.1f} ({confirm_count} confluences confirmed)')
    # NOTE: no separate R:R scoring bonus here -- previously this always
    # fired (fixed 3.2 multiplier meant R:R was ALWAYS >=2, confirmed in
    # 12/12 real trades), so it carried zero discriminating value while still
    # eating 5 points of score budget on every trade. rr is still
    # computed/shown/used for the trade plan and by Gemini's own independent
    # R:R check on its own proposed levels -- it's just no longer counted as
    # a quant scoring factor, and now it's not fixed either.
    score=min(100,scores[direction])
    # Canonical per-factor booleans for this trade, derived from the reasons
    # that actually fired for the winning direction. Stored on every trade so
    # later backtesting can measure each factor's real win rate instead of
    # guessing from the weight table.
    reason_text=' | '.join(reasons[direction])
    factor_flags={
        'htf_bias': 'HTF ' in reason_text,
        'trend_regime': 'trending regime' in reason_text,
        'liquidity_sweep': 'liquidity sweep' in reason_text,
        'bos': ' BOS' in reason_text,
        'order_block': 'order-block zone' in reason_text,
        'choch': 'CHoCH' in reason_text,
        'fvg': 'inside FVG' in reason_text,
        'crt': 'CRT' in reason_text,
        'tbs': 'TBS' in reason_text,
        'candle_pattern': 'candle pattern' in reason_text,
        'volume_expansion': 'volume expansion' in reason_text,
        'momentum': 'RSI' in reason_text,
        'eqh_eql_pool': 'liquidity pool' in reason_text and 'swept' in reason_text,
    }
    # 200 EMA alignment on the 1H timeframe -- a slower, less noisy trend
    # filter than the EMA20/50 crossover that currently drives `bias` itself.
    # Added to the HARD FILTER (not just the existing small +4 score bonus)
    # on the reasoning that EMA20/50 crosses far more often in choppy
    # conditions than price actually holding one side of its 200 EMA -- this
    # is exactly the kind of extra confirmation meant to cut down on the
    # choppy/ranging false "trending" calls that have been driving losses.
    # Falls back to "aligned" (doesn't block) when there isn't enough history
    # to compute a real 200 EMA yet, rather than permanently excluding new
    # listings.
    ema200_aligned = True
    if a1.get('ema200') is not None:
        ema200_aligned = (a1['bias']=='BULLISH' and a1['price']>a1['ema200']) or (a1['bias']=='BEARISH' and a1['price']<a1['ema200'])
    return {
        'symbol':symbol,'framework':FRAMEWORK,'timeframes':TIMEFRAMES,
        'direction':direction,'score':score,'price':price,'entry':price,'sl':sl,'tp1':tp1,'tp2':tp2,'tp3':tp3,'rr':rr,
        'reasons':reasons[direction], 'tf':{tf1:a1,tf2:a2,tf3:a3},
        'candles':{tf1:_compact_candles(c1),tf2:_compact_candles(c2),tf3:_compact_candles(c3)},
        'factor_flags':factor_flags,
        # Hard-filter flags: HTF bias and trend regime are the two factors
        # whose "absent" bucket has shown a 0% win rate consistently across
        # every backtest sample checked (n=4-5, replicated 3+ times) --
        # stronger and more consistent than any other single factor found.
        # scan_once rejects a candidate outright if either is missing,
        # before even spending a Gemini call on it.
        'htf_bias_ok': a1['bias'] in ('BULLISH','BEARISH') and factor_flags['htf_bias'] and ema200_aligned,
        'trend_regime_ok': trend_regime_ok,
        'zone_entry': _round_price(zone_entry) if zone_entry else None,
        'structure_flags':{
            'choch':choch,
            'fvg':{d:(fvg_hits[d] if fvg_hits.get(d) else None) for d in ('LONG','SHORT')},
            'crt_tbs':crt_tbs_hits,
            'candlestick_patterns':patterns,
            'equal_highs':{'level':_round_price(eqh1['level']),'count':eqh1['count']} if eqh1 else None,
            'equal_lows':{'level':_round_price(eql1['level']),'count':eql1['count']} if eql1 else None,
        }
    }

# ----------------------------- Gemini ---------------------------------
def _parse_gemini_json_text(txt):
    """Parse Gemini JSON robustly, including markdown fences and truncated REJECTs.

    A REJECT decision is safe to honor even if Gemini truncated optional fields.
    An APPROVE must contain valid JSON; otherwise it is never treated as approval.
    """
    raw=(txt or '').strip()
    candidates=[raw]
    cleaned=re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.I|re.S).strip()
    if cleaned != raw:
        candidates.append(cleaned)
    for candidate in candidates:
        try:
            obj=json.loads(candidate)
            if isinstance(obj,dict): return obj
        except json.JSONDecodeError:
            pass
    # Extract the decision even if the model stopped before closing the JSON.
    dm=re.search(r'"decision"\s*:\s*"\s*(APPROVE|REJECT)\s*"', raw, re.I)
    if dm:
        decision=dm.group(1).upper()
        cm=re.search(r'"confidence"\s*:\s*(\d+(?:\.\d+)?)', raw, re.I)
        reasonm=re.search(r'"reason"\s*:\s*"((?:\\.|[^"\\])*)', raw, re.I|re.S)
        out={'decision':decision}
        if cm: out['confidence']=float(cm.group(1))
        if reasonm:
            try: out['reason']=json.loads('"'+reasonm.group(1)+'"')
            except Exception: out['reason']=reasonm.group(1)
        if decision=='REJECT':
            out['_truncated_reject']=True
            return out
        raise RuntimeError('Gemini returned incomplete APPROVE JSON; approval blocked')
    raise RuntimeError('Gemini returned non-JSON: '+raw[:700])


def gemini_json(system_text, user_payload, max_output_tokens=2500):
    """Call Gemini with 5-key fallback. Normal REJECT is a final decision.

    Fallback occurs ONLY for transport/API/configuration failures or incomplete
    non-decision responses. A parsed Gemini REJECT immediately stops the chain.
    """
    global GEMINI_LAST_CALL
    if not GEMINI_API_KEYS:
        raise RuntimeError('No Gemini API key configured. Set GEMINI_API_KEY_1..5.')
    body={'system_instruction':{'parts':[{'text':system_text}]},
          'contents':[{'role':'user','parts':[{'text':json.dumps(user_payload)}]}],
          'generationConfig':{'temperature':0.1,'maxOutputTokens':max_output_tokens,
                              'responseMimeType':'application/json'}}
    last_error=None
    for idx,key in enumerate(GEMINI_API_KEYS,1):
        try:
            with GEMINI_LOCK:
                wait=GEMINI_MIN_INTERVAL-(time.monotonic()-GEMINI_LAST_CALL)
                if wait>0: time.sleep(wait)
                url=f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'
                log.info('[GEMINI] CALL project=%d/%d model=%s symbol=%s',idx,len(GEMINI_API_KEYS),GEMINI_MODEL,user_payload.get('symbol','?'))
                r=requests.post(url,params={'key':key},json=body,timeout=GEMINI_TIMEOUT)
                GEMINI_LAST_CALL=time.monotonic()
            if r.status_code==429:
                last_error=RuntimeError(f'Gemini 429 RESOURCE_EXHAUSTED project={idx}: {r.text[:700]}')
                log.warning('[GEMINI] FALLBACK project=%d/%d HTTP=429',idx,len(GEMINI_API_KEYS)); continue
            if r.status_code in (408,409,500,502,503,504):
                last_error=RuntimeError(f'Gemini HTTP {r.status_code} project={idx}: {r.text[:700]}')
                log.warning('[GEMINI] FALLBACK project=%d/%d HTTP=%d',idx,len(GEMINI_API_KEYS),r.status_code); continue
            if r.status_code in (400,401,403):
                last_error=RuntimeError(f'Gemini HTTP {r.status_code} project={idx}: {r.text[:700]}')
                log.warning('[GEMINI] FALLBACK project=%d/%d HTTP=%d',idx,len(GEMINI_API_KEYS),r.status_code); continue
            r.raise_for_status()
            data=r.json()
            txt=data['candidates'][0]['content']['parts'][0]['text'].strip()
            parsed=_parse_gemini_json_text(txt)
            # CRITICAL: a normal REJECT is a valid final answer. Never fallback.
            if str(parsed.get('decision','')).upper()=='REJECT':
                log.info('[GEMINI] FINAL REJECT project=%d/%d symbol=%s',idx,len(GEMINI_API_KEYS),user_payload.get('symbol','?'))
                return parsed
            # APPROVE must be fully valid JSON and therefore reaches here only safely.
            if str(parsed.get('decision','')).upper()=='APPROVE':
                log.info('[GEMINI] FINAL APPROVE project=%d/%d symbol=%s',idx,len(GEMINI_API_KEYS),user_payload.get('symbol','?'))
                return parsed
            raise RuntimeError('Gemini response missing valid decision')
        except (requests.RequestException, KeyError, ValueError, RuntimeError) as exc:
            last_error=exc
            # If the parser identified a safe REJECT, it is returned above and
            # never enters this fallback path.
            log.warning('[GEMINI] FALLBACK project=%d/%d response error=%s',idx,len(GEMINI_API_KEYS),exc)
            continue
    raise last_error or RuntimeError('All configured Gemini projects failed')

def gemini_validate(analysis):
    prompt={
      'task': 'Build your OWN independent score and your OWN Entry/SL/TP1/TP2/TP3 from the raw candles and structure below. Do NOT just rubber-stamp the quant numbers -- treat quant_score/quant_entry/quant_sl/quant_tp1/quant_tp2/quant_tp3 as a reference only, not ground truth.',
      'symbol':analysis['symbol'], 'framework':analysis['framework'], 'timeframes':analysis['timeframes'],
      'direction_candidate':analysis['direction'],
      'quant_score':analysis['score'],
      'quant_entry':_round_price(analysis['entry']),'quant_sl':_round_price(analysis['sl']),'quant_tp1':_round_price(analysis['tp1']),'quant_tp2':_round_price(analysis['tp2']),'quant_tp3':_round_price(analysis['tp3']),'quant_rr':round(analysis['rr'],2),
      'quant_reasons':analysis['reasons'],
      'timeframe_analysis':{k:{x:(_round_price(v) if x in ('ema20','ema50','ema200','atr','price') else (round(v,2) if x in ('rsi','volume_ratio') else v)) for x,v in a.items() if x not in ('high','low','range')} for k,a in analysis['tf'].items()},
      'raw_ohlcv_candles': analysis.get('candles', {}),
      # Python's own read of structure -- reference only, Gemini must verify against raw candles itself.
      'python_structure_flags': analysis.get('structure_flags', {}),
      'scoring_factors_reference_max_points': {
          'htf_bias_1h': 18, 'trend': 15, 'liquidity_sweep': 10, 'bos': 12, 'choch': 5,
          'order_block': 12, 'fvg': 8, 'volume_expansion': 5, 'rsi_momentum': 3,
          'risk_reward_ge_1_2': 5, 'crt': 4, 'tbs': 4,
          'single_candle_pattern': 2, 'double_candle_pattern': 2, 'triple_candle_pattern': 2
      }
    }
    system="""You are an INDEPENDENT SECOND ANALYST for a crypto trading research bot -- not a rubber-stamp validator.
Using ONLY the supplied raw OHLCV candles (1H/15M/5M) and structured multi-timeframe data, build your OWN independent read of this setup by checking EACH of the following yourself directly against the raw candles:
- HTF Bias
- Trend
- Liquidity Sweep
- BOS (Break of Structure)
- CHOCH (Change of Character)
- Order Block
- FVG (Fair Value Gap)
- Volume Expansion
- RSI Momentum
- CRT
- TBS
- Single Candle Pattern
- Double Candle Pattern
- Triple Candle Pattern

Hard rules:
1. Mark a structure element true ONLY if you can point to the specific candle(s) in raw_ohlcv_candles that show it. If you are not fully certain you can see it yourself, mark it false -- do not mark something true merely because quant_reasons or python_structure_flags mention it. Quant and Python's own detectors are known to sometimes disagree with each other; you verifying independently is the entire point of your role.
2. Actively look for reasons to REJECT before you look for reasons to approve: check for choppy/ranging price action, conflicting timeframes, a setup that only "sort of" fits, or an entry that is chasing price rather than at a real structural level. If you find any of these, REJECT.
3. Do not let a high quant_score talk you into a high score of your own -- score strictly from what you independently verify in the candles.

raw_ohlcv_candles uses short keys to save tokens: o=open, h=high, l=low, c=close, v=volume. Each timeframe's array is oldest-first, most recent candle last.

python_structure_flags is Python's own detection of CHoCH/FVG/CRT/TBS/candle patterns -- treat it as a reference only, not ground truth; verify or overturn it yourself from raw_ohlcv_candles.

Score it yourself (0-100) using the factor set above (max points per factor are given in scoring_factors_reference_max_points). Do not copy quant_score -- compute your own from what you actually see in the candles.
Propose your OWN Entry, SL, TP1, TP2, TP3 for whichever direction your analysis supports (it may agree or disagree with direction_candidate), derived purely from the raw candles/structure you were given -- do not just echo the quant levels.
Set decision to APPROVE only if your own analysis genuinely supports a valid, coherent trade with correct level ordering and R:R >= 1:2. Otherwise REJECT.
When approving, also fill these four short fields for the trade card sent to the user -- write them yourself from what you see, do not leave them generic:
- pattern_summary: the single clearest candle/structure pattern plus its timeframe and the price it formed at, e.g. "Three Black Crows on 15M at 1873.02"
- location_summary: where price is sitting relative to structure right now, e.g. "at new lows, below 1H Bearish FVG 1878.91-1891.80"
- meaning_summary: one sentence on what that implies for the trade, e.g. "Signals strong bearish continuation after breaking key support"
- confidence: your own 0-100 probability estimate that this trade reaches TP1, based on everything above
Return JSON only, no markdown fences:
{"decision":"APPROVE"|"REJECT","score":0-100,"direction":"LONG"|"SHORT","confidence":0-100,"entry":number,"sl":number,"tp1":number,"tp2":number,"tp3":number,"htf_bias":"BULLISH"|"BEARISH"|"NEUTRAL","bos_detected":true|false,"choch_detected":true|false,"order_block_detected":true|false,"fvg_detected":true|false,"crt_detected":true|false,"tbs_detected":true|false,"candle_patterns":["pattern_name", "..."],"pattern_summary":"short string","location_summary":"short string","meaning_summary":"short string","reason":"short reason","risk_note":"short note"}."""
    return gemini_json(system, prompt)

# ----------------------------- Trades ---------------------------------
def insert_trade(a, ai):
    # Store quant factor flags + Gemini's own factor detections together so
    # closed trades can later be backtested per-factor (see /api/backtest).
    factors_json=json.dumps({
        'quant': a.get('factor_flags', {}),
        'quant_score': a.get('score'),
        'gemini': {
            'score': ai.get('score'), 'confidence': ai.get('confidence'),
            'htf_bias': ai.get('htf_bias'), 'bos': ai.get('bos_detected'),
            'choch': ai.get('choch_detected'), 'order_block': ai.get('order_block_detected'),
            'fvg': ai.get('fvg_detected'), 'crt': ai.get('crt_detected'),
            'tbs': ai.get('tbs_detected'), 'candle_patterns': ai.get('candle_patterns'),
        }
    })
    with DB_LOCK:
        con=db(); cur=con.execute('''INSERT INTO trades(symbol,direction,entry,sl,tp1,tp2,tp3,score,ai_confidence,ai_reason,framework,created_at,factors_json,effective_sl) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (a['symbol'],a['direction'],a['entry'],a['sl'],a['tp1'],a['tp2'],a['tp3'],a['score'],int(ai.get('confidence',0)),ai.get('reason',''),FRAMEWORK,now_utc(),factors_json,a['sl']))
        con.commit(); tid=cur.lastrowid; con.close()
    return tid

def has_open_similar(symbol,direction):
    with DB_LOCK:
        con=db(); row=con.execute("SELECT 1 FROM trades WHERE symbol=? AND direction=? AND status IN ('WAITING_ENTRY','OPEN','PENDING_LIMIT') LIMIT 1",(symbol,direction)).fetchone(); con.close()
    return bool(row)

PENDING_LIMIT_MIN_SCORE = max(0, int(os.getenv('PENDING_LIMIT_MIN_SCORE', str(MIN_SCORE))))
PENDING_LIMIT_TIMEOUT_MIN = max(0, int(os.getenv('PENDING_LIMIT_TIMEOUT_MIN', '150')))

def insert_pending_limit(a):
    """A limit-order 'watch' -- created as soon as a good-scoring setup
    reaches its 15M zone (ZONE_REACHED), rather than waiting for the full
    5M sweep/BOS/CHoCH trigger through Gemini. Skips Gemini entirely: the
    whole point of a limit order sitting at the zone is that it can't be
    "chasing" an extended move (Gemini's #1 rejection reason) -- that risk
    is structurally avoided by construction. SL/TP stored here are only
    PROVISIONAL (from the original ATR-based analysis); track_pending_limits
    refines them from the real sweep candle once the order actually fills."""
    entry=a.get('zone_entry') or a['entry']
    risk_orig=abs(a['entry']-a['sl']) or 1e-9
    multipliers={
        'tp1': abs(a['tp1']-a['entry'])/risk_orig,
        'tp2': abs(a['tp2']-a['entry'])/risk_orig,
        'tp3': abs(a['tp3']-a['entry'])/risk_orig,
    }
    factors_json=json.dumps({'quant': a.get('factor_flags', {}), 'quant_score': a.get('score'), 'gemini': None, 'note': 'pending_limit -- no Gemini call'})
    with DB_LOCK:
        con=db(); cur=con.execute('''INSERT INTO trades(symbol,direction,entry,sl,tp1,tp2,tp3,score,ai_confidence,ai_reason,framework,created_at,factors_json,effective_sl,pending_multipliers_json,status)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (a['symbol'],a['direction'],entry,a['sl'],a['tp1'],a['tp2'],a['tp3'],a['score'],0,
           'Limit order watch -- SL/TP provisional, refined at fill from the real sweep candle',
           FRAMEWORK,now_utc(),factors_json,a['sl'],json.dumps(multipliers),'PENDING_LIMIT'))
        con.commit(); tid=cur.lastrowid; con.close()
    return tid

def track_pending_limits():
    with DB_LOCK:
        con=db(); rows=con.execute("SELECT * FROM trades WHERE status='PENDING_LIMIT'").fetchall(); con.close()
    for row in rows:
        try:
            if PENDING_LIMIT_TIMEOUT_MIN>0:
                age=_minutes_since(row['created_at'])
                if age is not None and age>PENDING_LIMIT_TIMEOUT_MIN:
                    with DB_LOCK:
                        con=db(); con.execute("UPDATE trades SET status='CLOSED', final_result='EXPIRED_NO_ENTRY', close_time=? WHERE id=?",(now_utc(),row['id'])); con.commit(); con.close()
                    telegram_send(f'⌛ *LIMIT ORDER EXPIRED — {row["symbol"]} {row["direction"]}*\nTrade #{row["id"]}\nZone `{fmt_price(row["entry"])}` never filled within {PENDING_LIMIT_TIMEOUT_MIN}min -- cancelled.')
                    continue
            candles=fetch_tracking_candle(row['symbol'],6)
            if not candles: continue
            c=candles[-1]; direction=row['direction']; entry=row['entry']
            filled = (c['low']<=entry) if direction=='LONG' else (c['high']>=entry)
            if not filled: continue
            # FILLED: read the real sweep candle's own low/high (not the
            # original ATR-based guess from when the zone was first
            # detected) to place the actual SL, then rebuild TP1/2/3
            # proportionally using the same R:R multipliers the original
            # analysis chose based on confluence strength.
            atr_candles=fetch_klines(row['symbol'], TRACK_TIMEFRAME, 30)
            a_atr=atr(atr_candles) if atr_candles else abs(entry-row['sl'])*0.3
            try:
                mult=json.loads(row['pending_multipliers_json'] or '{}')
            except (json.JSONDecodeError, TypeError):
                mult={}
            m1=mult.get('tp1',1.5); m2=mult.get('tp2',2.3); m3=mult.get('tp3',3.2)
            if direction=='LONG':
                real_sweep_low=min(x['low'] for x in candles[-3:])
                new_sl=min(real_sweep_low-0.3*a_atr, entry-0.6*a_atr)
                risk=entry-new_sl
                new_tp1=entry+m1*risk; new_tp2=entry+m2*risk; new_tp3=entry+m3*risk
            else:
                real_sweep_high=max(x['high'] for x in candles[-3:])
                new_sl=max(real_sweep_high+0.3*a_atr, entry+0.6*a_atr)
                risk=new_sl-entry
                new_tp1=entry-m1*risk; new_tp2=entry-m2*risk; new_tp3=entry-m3*risk
            with DB_LOCK:
                con=db(); con.execute('''UPDATE trades SET status='WAITING_ENTRY', sl=?, effective_sl=?, tp1=?, tp2=?, tp3=?,
                                          ai_reason='Limit order filled -- SL/TP finalized from real sweep candle' WHERE id=?''',
                                       (new_sl,new_sl,new_tp1,new_tp2,new_tp3,row['id'])); con.commit(); con.close()
            telegram_send(
                f'🎯 *LIMIT ORDER FILLED — {row["symbol"]} {row["direction"]}*\nTrade #{row["id"]}\n'
                f'Entry: `{fmt_price(entry)}` (filled)\nSL (from real sweep): `{fmt_price(new_sl)}`\n'
                f'TP1: `{fmt_price(new_tp1)}` | TP2: `{fmt_price(new_tp2)}` | TP3: `{fmt_price(new_tp3)}`\n'
                f'Quant Score: `{row["score"]:.0f}/100` (no Gemini call -- limit-order path)'
            )
        except Exception as e:
            log.warning('pending-limit tracker %s: %s',row['symbol'],e)

SYMBOL_LOSS_COOLDOWN_MIN = max(0, int(os.getenv('SYMBOL_LOSS_COOLDOWN_MIN', '120')))

def has_recent_loss(symbol):
    """True if `symbol` closed a losing trade (any direction) within the
    cooldown window. In the first 12 live trades, HYPEUSDT alone was re-entered
    5 times within hours and lost 4 of them -- the same failing structure kept
    getting re-scored and re-approved before the market had actually changed.
    This is a simple, data-independent circuit breaker for that pattern."""
    if SYMBOL_LOSS_COOLDOWN_MIN <= 0:
        return False
    with DB_LOCK:
        con=db()
        row=con.execute(
            "SELECT close_time FROM trades WHERE symbol=? AND sl_hit=1 AND close_time IS NOT NULL "
            "ORDER BY close_time DESC LIMIT 1", (symbol,)
        ).fetchone()
        con.close()
    if not row or not row['close_time']:
        return False
    try:
        last_loss=datetime.fromisoformat(row['close_time'].replace('Z','+00:00'))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - last_loss) < timedelta(minutes=SYMBOL_LOSS_COOLDOWN_MIN)

def check_trade(row, candle):
    direction=row['direction']; high=candle['high']; low=candle['low']; entry=row['entry']
    status=row['status']; htp=row['highest_tp'] or 0
    eff_sl = row['effective_sl'] if row['effective_sl'] is not None else row['sl']
    just_entered=False
    if status=='WAITING_ENTRY':
        if low <= entry <= high: status='OPEN'; just_entered=True
        else: return None

    # Conservative same-candle rule: check the stop we ALREADY had going into
    # this candle first. OHLC alone can't tell us whether a candle that wicks
    # through both a fresh TP level and the stop touched the TP first (good)
    # or the stop first (bad) -- assuming the favorable order was optimistic
    # and produced misleading "TP hit AND stop hit" closes that still counted
    # as if the TP had safely locked in. We never give that benefit of the
    # doubt: if the PRE-EXISTING stop is touched anywhere in this candle,
    # that governs the exit, full stop, regardless of what else the candle's
    # wick also touched.
    pre_sl_hit=(low<=eff_sl) if direction=='LONG' else (high>=eff_sl)

    tp_hits=[]
    for n,key in [(1,'tp1'),(2,'tp2'),(3,'tp3')]:
        if row[key] is not None and ((direction=='LONG' and high>=row[key]) or (direction=='SHORT' and low<=row[key])): tp_hits.append(n)
    new_htp=max([htp]+tp_hits) if tp_hits else htp

    if pre_sl_hit:
        result='SL_LOSS' if htp==0 else f'TP{htp}_LOCKED_SL'
        out={'status':'CLOSED','sl_hit':1,'effective_sl':eff_sl,'final_result':result,'close_time':now_utc()}
        if tp_hits:
            # A fresh TP was also touched this candle -- record it for
            # visibility, but the exit is governed by the stop we already
            # had, not the new one, since we can't prove the TP came first.
            out.update({'highest_tp':new_htp,'tp1_hit':int(new_htp>=1),'tp2_hit':int(new_htp>=2),'tp3_hit':int(new_htp>=3)})
        if just_entered: out['entry_time']=now_utc()
        return out

    # Pre-existing stop was NOT touched -- safe to ratchet the stop forward
    # based on whatever TPs this candle reached. TP1 hit -> breakeven. TP2
    # hit -> TP1. Only ever moves in the direction that reduces risk.
    new_eff_sl=eff_sl
    if new_htp>=1:
        new_eff_sl=max(new_eff_sl,entry) if direction=='LONG' else min(new_eff_sl,entry)
    if new_htp>=2 and row['tp1'] is not None:
        new_eff_sl=max(new_eff_sl,row['tp1']) if direction=='LONG' else min(new_eff_sl,row['tp1'])

    if tp_hits:
        u={'status':'OPEN','highest_tp':new_htp,'tp1_hit':int(new_htp>=1),'tp2_hit':int(new_htp>=2),'tp3_hit':int(new_htp>=3),
           'effective_sl':new_eff_sl,'last_checked':now_utc()}
        if just_entered: u['entry_time']=now_utc()
        if new_htp>=3: u.update({'status':'CLOSED','final_result':'TP3_WIN','close_time':now_utc()})
        return u
    out={'status':status,'effective_sl':new_eff_sl,'last_checked':now_utc()}
    if just_entered: out['entry_time']=now_utc()
    return out


def _candle_is_closed(candle, interval):
    """True only if this candle's time window has fully elapsed."""
    sec = _INTERVAL_SECONDS.get(interval, 60)
    return (candle['open_time'] + sec*1000) <= int(time.time()*1000)


def fetch_tracking_candle(symbol, limit=6):
    """Dedicated feed used only for TP/SL/entry monitoring -- returns only
    FULLY CLOSED candles.

    Exchange kline endpoints always append the still-forming current candle
    as the last row. Using that unclosed candle for entry/TP/SL checks means
    a single noisy wick mid-minute can flip WAITING_ENTRY -> OPEN, or trigger
    an SL, before the candle actually finishes and confirms that move. That
    is what was causing trades to show OPEN before a real entry happened, and
    inflating SL hits on setups that were otherwise fine. We fetch a few
    extra candles and drop any that haven't closed yet.
    """
    candles = fetch_klines(symbol, TRACK_TIMEFRAME, limit)
    return [c for c in candles if _candle_is_closed(c, TRACK_TIMEFRAME)]


WAITING_ENTRY_TIMEOUT_MIN = max(0, int(os.getenv('WAITING_ENTRY_TIMEOUT_MIN', '180')))

# ----------------------------- Watch-state machine ----------------------
# Persists a per-symbol, multi-cycle "waiting for the setup to actually
# develop" workflow across scan_once() calls, instead of judging every
# candidate from a single-snapshot view each cycle:
#   IDLE          -- nothing being tracked, or the last watch was invalidated
#   BIAS_SET      -- 1H bias + hard filters (HTF bias, 200 EMA, trend regime)
#                    passed at least once; waiting for price to pull back
#                    into a 15M order-block/FVG zone
#   ZONE_REACHED  -- price has entered that zone; waiting for a genuine 5M
#                    trigger (sweep, BOS, or CHoCH) before treating it as a
#                    real, confirmed setup
# Only a transition into a fresh trigger while in ZONE_REACHED is allowed to
# proceed to full scoring/Gemini/trade creation this cycle -- everything
# else just updates state and is skipped, which also cuts down on repeatedly
# calling Gemini on the same still-forming setup every single cycle.
WATCH_STATE_ENABLED = os.getenv('WATCH_STATE_ENABLED', '1').strip().lower() in ('1','true','yes','on')
WATCH_BIAS_TIMEOUT_MIN = max(0, int(os.getenv('WATCH_BIAS_TIMEOUT_MIN', '480')))   # bias goes stale after 8h without reaching a zone
WATCH_ZONE_TIMEOUT_MIN = max(0, int(os.getenv('WATCH_ZONE_TIMEOUT_MIN', '150')))   # setup abandoned after 2.5h in-zone without a trigger

def _get_watch_state(symbol):
    with DB_LOCK:
        con=db(); row=con.execute('SELECT * FROM watch_state WHERE symbol=?',(symbol,)).fetchone(); con.close()
    return dict(row) if row else None

def _set_watch_state(symbol, state, direction, bias_set_at, zone_reached_at):
    with DB_LOCK:
        con=db()
        con.execute('''INSERT INTO watch_state(symbol,state,direction,bias_set_at,zone_reached_at,last_updated)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(symbol) DO UPDATE SET state=excluded.state,direction=excluded.direction,
                       bias_set_at=excluded.bias_set_at,zone_reached_at=excluded.zone_reached_at,last_updated=excluded.last_updated''',
                    (symbol,state,direction,bias_set_at,zone_reached_at,now_utc()))
        con.commit(); con.close()

def _minutes_since(ts):
    if not ts: return None
    try:
        t=datetime.fromisoformat(ts.replace('Z','+00:00'))
    except (ValueError, AttributeError):
        return None
    return (datetime.now(timezone.utc)-t).total_seconds()/60.0

def update_watch_state(a):
    """Advances (or resets) `a['symbol']`'s watch state given this cycle's
    fresh analysis. Returns (status, note):
      status='TRIGGERED' -> a genuine, freshly-confirmed setup; caller should
                             treat this candidate as eligible this cycle.
      status='IDLE'/'BIAS_SET'/'ZONE_REACHED' -> not eligible yet; note
                             explains what it's waiting for (dashboard-visible).
    """
    symbol=a['symbol']; direction=a['direction']
    hard_ok = bool(a.get('htf_bias_ok')) and bool(a.get('trend_regime_ok'))
    ff = a.get('factor_flags', {})
    in_zone_now = bool(ff.get('order_block')) or bool(ff.get('fvg'))
    trigger_now = bool(ff.get('liquidity_sweep')) or bool(ff.get('bos')) or bool(ff.get('choch')) or bool(ff.get('eqh_eql_pool'))
    row=_get_watch_state(symbol)

    if not hard_ok:
        if row and row['state']!='IDLE':
            _set_watch_state(symbol,'IDLE',None,None,None)
        return 'IDLE','HTF bias/trend regime not currently valid'

    if row is None or row['state']=='IDLE' or row['direction']!=direction:
        # fresh bias (or direction flipped) -- start watching, don't act yet
        _set_watch_state(symbol,'BIAS_SET',direction,now_utc(),None)
        return 'BIAS_SET','bias just set this cycle, waiting for 15M zone'

    if row['state']=='BIAS_SET':
        age=_minutes_since(row['bias_set_at'])
        if age is not None and age>WATCH_BIAS_TIMEOUT_MIN:
            _set_watch_state(symbol,'IDLE',None,None,None)
            return 'IDLE',f'bias went stale after {WATCH_BIAS_TIMEOUT_MIN}min without reaching a zone'
        if in_zone_now:
            _set_watch_state(symbol,'ZONE_REACHED',direction,row['bias_set_at'],now_utc())
            return 'ZONE_REACHED','zone just reached this cycle, waiting for 5M trigger'
        return 'BIAS_SET',f'waiting for 15M zone ({age:.0f}min since bias set)' if age is not None else 'waiting for 15M zone'

    if row['state']=='ZONE_REACHED':
        age=_minutes_since(row['zone_reached_at'])
        if age is not None and age>WATCH_ZONE_TIMEOUT_MIN:
            _set_watch_state(symbol,'IDLE',None,None,None)
            return 'IDLE',f'setup abandoned after {WATCH_ZONE_TIMEOUT_MIN}min in-zone without a trigger'
        if trigger_now:
            # Consumed -- reset to IDLE so the next real setup starts fresh
            # rather than immediately re-triggering next cycle on stale state.
            _set_watch_state(symbol,'IDLE',None,None,None)
            return 'TRIGGERED','fresh 5M trigger confirmed after zone + bias'
        return 'ZONE_REACHED',f'in zone, waiting for 5M trigger ({age:.0f}min)' if age is not None else 'in zone, waiting for 5M trigger'

    return 'IDLE','unrecognized state, reset'

def track_trades():
    with DB_LOCK:
        con=db(); rows=con.execute("SELECT * FROM trades WHERE status IN ('WAITING_ENTRY','OPEN')").fetchall(); con.close()
    for row in rows:
        try:
            # Trades whose limit entry never got filled: the setup that
            # justified this level is stale by now, and the price/SL/TP the
            # bot chose no longer reflects current structure. Auto-expire
            # instead of leaving it parked forever (or worse, having it fill
            # much later on totally different, unvetted market conditions).
            if row['status']=='WAITING_ENTRY' and WAITING_ENTRY_TIMEOUT_MIN>0:
                try:
                    created=datetime.fromisoformat(row['created_at'].replace('Z','+00:00'))
                except (ValueError, AttributeError):
                    created=None
                if created and (datetime.now(timezone.utc)-created) > timedelta(minutes=WAITING_ENTRY_TIMEOUT_MIN):
                    with DB_LOCK:
                        con=db(); con.execute(
                            "UPDATE trades SET status='CLOSED', final_result='EXPIRED_NO_ENTRY', close_time=? WHERE id=?",
                            (now_utc(), row['id'])
                        ); con.commit(); con.close()
                    telegram_send(f'⌛ *ENTRY EXPIRED — {row["symbol"]} {row["direction"]}*\nTrade #{row["id"]}\nLimit entry `{fmt_price(row["entry"])}` never filled within {WAITING_ENTRY_TIMEOUT_MIN}min -- setup is stale, cancelled.')
                    continue
            candles=fetch_tracking_candle(row['symbol'],6)
            if not candles: continue
            last_closed=candles[-1]
            u=check_trade(row,last_closed)
            if not u: continue
            old_eff_sl = row['effective_sl'] if row['effective_sl'] is not None else row['sl']
            new_eff_sl = u.get('effective_sl')
            with DB_LOCK:
                con=db();
                sets=', '.join(f'{k}=?' for k in u); con.execute(f'UPDATE trades SET {sets},last_price=?,last_checked=? WHERE id=?',list(u.values())+[last_closed['close'],now_utc(),row['id']]); con.commit(); con.close()
            if u.get('status')=='CLOSED':
                result=u.get('final_result','CLOSED'); icon='🟢' if result=='TP3_WIN' else '🟡' if 'TP' in result else '🔴'
                telegram_send(f'{icon} *TRADE CLOSED — {row["symbol"]} {row["direction"]}*\nTrade #{row["id"]}\nResult: *{result}*\nEntry: `{fmt_price(row["entry"])}` | SL: `{fmt_price(row["sl"])}`\nTP1: `{fmt_price(row["tp1"])}` | TP2: `{fmt_price(row["tp2"])}` | TP3: `{fmt_price(row["tp3"])}`')
            elif new_eff_sl is not None and new_eff_sl != old_eff_sl:
                # Stop just ratcheted forward (TP1 -> breakeven, TP2 -> TP1) on a
                # trade that's still running -- tell the user their risk on this
                # trade just changed, same as a human would move a resting order.
                htp=u.get('highest_tp', row['highest_tp'] or 0)
                label='breakeven' if htp==1 else f'TP{htp-1}' if htp>=2 else 'moved'
                telegram_send(f'🔒 *SL MOVED — {row["symbol"]} {row["direction"]}*\nTrade #{row["id"]}\nTP{htp} hit -- SL moved to {label}\nOld SL: `{fmt_price(old_eff_sl)}` → New SL: `{fmt_price(new_eff_sl)}`')
        except Exception as e: log.warning('tracker %s: %s',row['symbol'],e)

# ----------------------------- Scanning -------------------------------

def send_setup_status(symbol, direction, score, status, reason=""):
    """Telegram helper for APPROVED / WAIT-WATCH setups (uses telegram_send, not the undefined send_tg)."""
    if status == "APPROVED":
        telegram_send(f"🚨 *{symbol} {direction} TRADE APPROVED*\nScore: *{score}/100*\n" + (f"Reason: {reason}" if reason else ""))
    elif status == "WAIT/WATCH":
        telegram_send(f"👀 *{symbol} {direction} — WAIT / WATCH*\nScore: *{score}/100*\n" + (f"Reason: {reason}" if reason else ""))

def scan_once(force=False):
    """Score ALL first, rank ALL, Gemini ONLY the highest candidate, Telegram trade-only."""
    global LAST_SCAN,LAST_ERROR
    if not scanner_is_active() and not force: return {'status':'stopped'}
    if not SCAN_LOCK.acquire(blocking=False): return {'status':'already_running'}
    result={'time':now_utc(),'symbols':{},'best':None}; analyses=[]; scan_errors=[]
    try:
        # PASS 1: every wishlist coin is scored. No Gemini and no Telegram here.
        for symbol in WATCHLIST:
            try:
                a=build_analysis(symbol); analyses.append(a)
                result['symbols'][symbol]={'score':a['score'],'decision':'RANKED'}
            except Exception as e:
                err=f'{type(e).__name__}: {e}'
                LAST_ERROR=f'{symbol}: {err}'; log.exception('score %s',symbol)
                result['symbols'][symbol]={'error':str(e),'decision':'SCAN_ERROR'}
                scan_errors.append((symbol,err))
            time.sleep(1)
        if scan_errors:
            # Previously these were silent: dropped from `analyses`, never
            # logged in the [SCAN] summary line, and never written to the
            # `scans` table -- a symbol could fail every single cycle and be
            # invisible both in the terminal log and on the dashboard. Now
            # every failed symbol gets its own scan-log row so a persistent
            # per-symbol or exchange-wide outage is actually visible instead
            # of just quietly shrinking the effective watchlist.
            log.warning('[SCAN] %d/%d symbols failed to fetch: %s', len(scan_errors), len(WATCHLIST),
                        ', '.join(f'{s}({e.split(":")[0]})' for s,e in scan_errors))
            with DB_LOCK:
                con=db(); now=now_utc()
                for s,err in scan_errors:
                    con.execute('INSERT INTO scans(time,symbol,score,decision,gemini_called,reason) VALUES(?,?,?,?,?,?)',
                                (now,s,0,'SCAN_ERROR',0,err[:500]))
                con.commit(); con.close()
        if not analyses: return result
        # HARD FILTERS: htf_bias and trend_regime are the two factors whose
        # "absent" bucket has shown a 0% win rate consistently across every
        # real backtest sample checked so far -- reject candidates missing
        # either before they can even become the scan's "best" pick, rather
        # than just weighting them. A candidate that fails this never
        # reaches Gemini, no matter how high its raw score is otherwise.
        #
        # IMPORTANT: `analyses` is kept as the FULL list for dashboard/log
        # visibility -- only `candidates` (the hard-filter survivors) is used
        # to pick "best". A previous version reassigned `analyses` itself to
        # the filtered list, which meant any symbol failing the hard filter
        # got NO scan-log row at all once at least one other symbol passed --
        # 14 of 15 successfully-scored symbols could vanish from the
        # dashboard even though every fetch succeeded. Every symbol that was
        # actually scored now always gets its own row.
        candidates=[a for a in analyses if a.get('htf_bias_ok') and a.get('trend_regime_ok')]
        analyses.sort(key=lambda x: float(x.get('score',0)), reverse=True)

        # Watch-state layer: on top of the hard filters, require a genuine
        # multi-cycle progression (bias set -> price reaches a 15M zone ->
        # a fresh 5M trigger) before a candidate is actually eligible this
        # cycle, rather than acting the instant a single snapshot happens to
        # show everything aligned at once. Opt-out via WATCH_STATE_ENABLED=0
        # to restore the old immediate-eligibility behavior.
        watch_notes={}
        if WATCH_STATE_ENABLED:
            triggered=[]
            for a in analyses:
                status,note=update_watch_state(a)
                watch_notes[a['symbol']]=(status,note)
                if status=='TRIGGERED':
                    triggered.append(a)
                elif status=='ZONE_REACHED' and float(a.get('score',0))>=PENDING_LIMIT_MIN_SCORE:
                    # Good-scoring setup has reached its zone but hasn't
                    # produced a fresh 5M trigger yet -- rather than silently
                    # wait (and risk missing a fill that happens between two
                    # 5-minute scan cycles), place a tracked limit order at
                    # the zone right now. No Gemini call for this path -- see
                    # insert_pending_limit for why that's fine here.
                    if not has_open_similar(a['symbol'], a['direction']):
                        pid=insert_pending_limit(a)
                        telegram_send(
                            f'👀 *LIMIT ORDER PLACED — {a["symbol"]} {a["direction"]}*\nTrade #{pid}\n'
                            f'Watching for fill at `{fmt_price(a.get("zone_entry") or a["entry"])}`\n'
                            f'Quant Score: `{float(a["score"]):.0f}/100`\nSL/TP will be finalized once it fills.'
                        )
                    watch_notes[a['symbol']]=('ZONE_REACHED','limit order placed at zone, waiting for fill')
            candidates=triggered

        if not candidates:
            top=analyses[0]
            with DB_LOCK:
                con=db(); now=now_utc()
                for a in analyses:
                    ok_htf=a.get('htf_bias_ok'); ok_trend=a.get('trend_regime_ok')
                    if not (ok_htf and ok_trend):
                        missing=', '.join(x for x,ok in [('htf_bias',ok_htf),('trend_regime',ok_trend)] if not ok)
                        d,reason='HARD_FILTER_FAIL',f'Failed hard filter: {missing}'
                    elif a['symbol'] in watch_notes:
                        status,note=watch_notes[a['symbol']]
                        d,reason=f'WATCHING_{status}',note
                    else:
                        d,reason='HARD_FILTER_FAIL','n/a'
                    con.execute('INSERT INTO scans(time,symbol,score,decision,gemini_called,reason) VALUES(?,?,?,?,?,?)',
                                (now,a['symbol'],a['score'],d,0,reason))
                con.commit(); con.close()
            log.info('[SCAN] scores=%s | no triggered candidate this cycle (best would-be: %s score=%.1f)',
                      ', '.join(f"{a['symbol']}:{a['score']}" for a in analyses), top['symbol'], float(top['score']))
            return result
        candidates.sort(key=lambda x: float(x.get('score',0)), reverse=True)
        best=candidates[0]; best_symbol=best['symbol']; best_score=float(best['score'])
        result['best']={'symbol':best_symbol,'score':best_score}
        log.info('[SCAN] scores=%s | BEST=%s score=%.1f', ', '.join(f"{a['symbol']}:{a['score']}" for a in analyses), best_symbol,best_score)
        # Dashboard scan log: every candidate, but never Telegram.
        with DB_LOCK:
            con=db(); now=now_utc()
            for a in analyses:
                if not (a.get('htf_bias_ok') and a.get('trend_regime_ok')):
                    d='HARD_FILTER_FAIL'
                    missing=', '.join(x for x,ok in [('htf_bias',a.get('htf_bias_ok')),('trend_regime',a.get('trend_regime_ok'))] if not ok)
                    reason=f'Failed hard filter: {missing}'
                elif a['symbol'] in watch_notes and watch_notes[a['symbol']][0]!='TRIGGERED':
                    status,note=watch_notes[a['symbol']]
                    d,reason=f'WATCHING_{status}',note
                else:
                    d='RANKED_WAIT' if a['symbol']!=best_symbol else 'BEST_PENDING'
                    reason='Not highest score in this scan' if d=='RANKED_WAIT' else 'Highest score; MIN_SCORE pending'
                con.execute('INSERT INTO scans(time,symbol,score,decision,gemini_called,reason) VALUES(?,?,?,?,?,?)',(now,a['symbol'],a['score'],d,0,reason))
            con.commit(); con.close()
        # Only highest score reaches MIN_SCORE.
        if best_score < MIN_SCORE:
            reason=f'Highest score {best_score:.0f} below MIN_SCORE {MIN_SCORE}; Gemini skipped'
            with DB_LOCK:
                con=db(); con.execute('INSERT INTO scans(time,symbol,score,decision,gemini_called,reason) VALUES(?,?,?,?,?,?)',(now_utc(),best_symbol,best_score,'NO_TRADE',0,reason)); con.commit(); con.close()
            result['symbols'][best_symbol].update({'decision':'NO_TRADE','gemini':False,'reason':reason}); return result
        if has_open_similar(best_symbol,best['direction']):
            reason='Highest candidate already has an open/similar tracked trade; Gemini skipped'
            with DB_LOCK:
                con=db(); con.execute('INSERT INTO scans(time,symbol,score,decision,gemini_called,reason) VALUES(?,?,?,?,?,?)',(now_utc(),best_symbol,best_score,'SKIP_OPEN_TRADE',0,reason)); con.commit(); con.close()
            result['symbols'][best_symbol].update({'decision':'SKIP_OPEN_TRADE','gemini':False,'reason':reason}); return result
        if has_recent_loss(best_symbol):
            reason=f'{best_symbol} hit SL within the last {SYMBOL_LOSS_COOLDOWN_MIN}min; cooling down before re-entering, Gemini skipped'
            with DB_LOCK:
                con=db(); con.execute('INSERT INTO scans(time,symbol,score,decision,gemini_called,reason) VALUES(?,?,?,?,?,?)',(now_utc(),best_symbol,best_score,'SKIP_COOLDOWN',0,reason)); con.commit(); con.close()
            result['symbols'][best_symbol].update({'decision':'SKIP_COOLDOWN','gemini':False,'reason':reason}); return result
        # Exactly one Gemini validation chain for the winner.
        try:
            ai=gemini_validate(best)
        except Exception as exc:
            reason=f'Gemini error: {type(exc).__name__}: {exc}'
            with DB_LOCK:
                con=db(); con.execute('INSERT INTO scans(time,symbol,score,decision,gemini_called,reason) VALUES(?,?,?,?,?,?)',(now_utc(),best_symbol,best_score,'GEMINI_ERROR',1,reason)); con.commit(); con.close()
            result['symbols'][best_symbol].update({'decision':'GEMINI_ERROR','gemini':True,'ai':{'error':reason},'reason':reason}); return result
        # Gemini now scores independently -- it must beat (or match) the quant score
        # on ITS OWN read of the market before its OWN Entry/SL/TP levels are used.
        try:
            gemini_score=float(ai.get('score', ai.get('confidence', 0)) or 0)
        except (TypeError, ValueError):
            gemini_score=0.0
        approved=False; trade_plan=best; reason=ai.get('reason','Gemini rejected')
        if str(ai.get('decision','REJECT')).upper()=='APPROVE':
            # Two floors now, not one: gemini_score must beat quant's own read
            # (as before) AND clear an absolute floor (GEMINI_MIN_SCORE). The
            # relative-only check was nearly always satisfied in practice --
            # across the first 12 live trades gemini_score was HIGHER than
            # quant_score every single time (e.g. quant 45 -> gemini 82,
            # quant 49 -> gemini 76), so it was gating almost nothing on its
            # own. The absolute floor gives it real teeth.
            if gemini_score >= best_score and gemini_score >= GEMINI_MIN_SCORE:
                agree,total,ratio=structure_agreement(best.get('factor_flags',{}), ai)
                # Tightened from <0.4 to <0.5 -- real trades kept showing
                # exactly this pattern (quant bos=false/choch=false, Gemini
                # claims bos=true/choch=true, trade then hits SL). We don't
                # go as far as "quant automatically overrules Gemini" though:
                # quant's own detectors are simple rule-based checks with
                # real blind spots (see detect_choch/detect_zone), and the
                # entire point of asking Gemini to look at the raw candles
                # independently is that it can sometimes correctly see
                # structure quant's fixed rules miss. Disagreement is treated
                # as a genuine "neither read is trustworthy alone" signal,
                # not as quant being assumed right by default.
                if total>=3 and ratio<0.5:
                    reason=f'Gemini score {gemini_score:.0f} cleared both floors but structure disagreement with quant too high ({agree}/{total} elements agreed, {ratio*100:.0f}%); trade held for review'
                else:
                    g_direction=str(ai.get('direction') or best['direction']).upper()
                    try:
                        g_entry=float(ai['entry']); g_sl=float(ai['sl'])
                        g_tp1=float(ai['tp1']); g_tp2=float(ai['tp2']); g_tp3=float(ai['tp3'])
                    except (KeyError, TypeError, ValueError):
                        reason='Gemini approved but returned invalid/missing trade levels'
                    else:
                        ok,why=_final_level_check(g_direction,g_entry,g_sl,g_tp1,g_tp2,g_tp3)
                        if ok:
                            approved=True
                            trade_plan=dict(best)
                            trade_plan.update({'direction':g_direction,'entry':g_entry,'sl':g_sl,
                                                'tp1':g_tp1,'tp2':g_tp2,'tp3':g_tp3,
                                                'rr':abs(g_tp3-g_entry)/max(abs(g_entry-g_sl),1e-12)})
                            reason=ai.get('reason', f'Gemini score {gemini_score:.0f} >= quant score {best_score:.0f}, structure agreement {ratio*100:.0f}%')
                        else:
                            reason=f'Gemini approved but failed hard level check: {why}'
            else:
                reason=f'Gemini score {gemini_score:.0f} did not clear both floors (>= quant {best_score:.0f} AND >= GEMINI_MIN_SCORE {GEMINI_MIN_SCORE}); trade rejected'
        else:
            reason=ai.get('reason', f'Gemini score {gemini_score:.0f} below quant score {best_score:.0f}')
        decision='APPROVED' if approved else 'REJECTED'
        with DB_LOCK:
            con=db(); con.execute('INSERT INTO scans(time,symbol,score,decision,gemini_called,reason) VALUES(?,?,?,?,?,?)',(now_utc(),best_symbol,best_score,decision,1,f'[Gemini score {gemini_score:.0f}] {reason}')); con.commit(); con.close()
        result['symbols'][best_symbol].update({'decision':decision,'gemini':True,'ai':ai,'gemini_score':gemini_score,'reason':reason})
        # Telegram: ONLY an actually created trade. No score spam / no rejects / no waits.
        if approved:
            tid=insert_trade(trade_plan,ai)
            telegram_send(build_signal_card(trade_plan, ai, best, best_score, gemini_score), parse_mode=None)
        return result
    finally:
        LAST_SCAN=now_utc(); SCAN_LOCK.release()

def scheduled_scan():
    if scanner_is_active():
        log.info('Scheduled scan started'); scan_once()

def scheduled_track():
    track_trades()
    track_pending_limits()

# ----------------------------- Telegram commands ----------------------
def telegram_poll():
    global TELEGRAM_OFFSET, BOT_ACTIVE
    if not TELEGRAM_TOKEN:
        log.error('Telegram polling disabled: TELEGRAM_BOT_TOKEN is missing')
        return
    # getUpdates cannot run while a webhook is active. Remove any stale webhook on startup.
    try:
        wh=requests.get(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo', timeout=10).json()
        if (wh.get('result') or {}).get('url'):
            requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook', params={'drop_pending_updates':False}, timeout=10)
            log.info('Deleted Telegram webhook so long-polling can start.')
    except Exception as exc:
        log.warning('Telegram webhook check failed: %s', exc)
    while True:
        try:
            r=requests.get(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates', params={'timeout':25,'offset':TELEGRAM_OFFSET,'allowed_updates':['message']}, timeout=35)
            if r.status_code == 409:
                log.error('Telegram 409 Conflict: another process is polling this bot token. Stop every older Render/local service using this token.')
                time.sleep(10); continue
            if not r.ok:
                log.warning('Telegram getUpdates %s: %s', r.status_code, r.text[:300]); time.sleep(5); continue
            for u in r.json().get('result',[]):
                TELEGRAM_OFFSET=u['update_id']+1
                msg=u.get('message') or {}; chat=str(msg.get('chat',{}).get('id','')); text=(msg.get('text') or '').strip().split()[0].lower()
                configured=str(TELEGRAM_CHAT_ID or '')
                bound=str(state_get('telegram_chat_id', configured) or '')
                # First /start can bind this bot to the current Telegram chat.
                if text == '/start' and TELEGRAM_AUTO_BIND:
                    state_set('telegram_chat_id', chat); bound=chat
                if bound and chat != bound:
                    continue
                if text=='/start':
                    BOT_ACTIVE=True; state_set('active','1')
                    telegram_send(f'▶️ *SMC AI PRO started*\nScanning every {SCAN_INTERVAL} min\nTracking every {TRACK_INTERVAL} min\nFramework: `{FRAMEWORK}`')
                    threading.Thread(target=scan_once, kwargs={'force':True}, daemon=True, name='start-scan').start()
                elif text=='/stop':
                    BOT_ACTIVE=False; state_set('active','0'); telegram_send('⏹️ *New signal scanning stopped.*\nExisting trades will CONTINUE to be tracked.')
                elif text in ('/status','/health'):
                    active=scanner_is_active(); st=stats(); telegram_send(f'ℹ️ *Status*\nScanner: `{"ON" if active else "OFF"}`\nTracker: `ON`\nFramework: `{FRAMEWORK}`\nTrades: `{st["total"]}`\nOpen: `{st["open"]}`\nWin rate: `{st["win_rate"]}%`')
                elif text=='/run-now':
                    if scanner_is_active():
                        telegram_send('🔎 Manual scan started.'); threading.Thread(target=scan_once, kwargs={'force':True}, daemon=True, name='manual-scan').start()
                    else:
                        telegram_send('⏸️ Scanner is OFF. Send /start first.')
                elif text=='/trades':
                    send_trade_summary()
        except Exception as e:
            log.warning('telegram poll: %s',e); time.sleep(5)

def stats():
    with DB_LOCK:
        con=db(); row=con.execute('''SELECT COUNT(*) total,
            SUM(status IN ('WAITING_ENTRY','OPEN')) open,
            SUM(status='PENDING_LIMIT') pending,
            SUM(final_result='EXPIRED_NO_ENTRY') expired,
            SUM(final_result='TP3_WIN') wins,
            SUM(final_result='SL_LOSS') losses,
            SUM(final_result LIKE 'TP%\\_LOCKED\\_SL' ESCAPE '\\') partials
            FROM trades''').fetchone(); con.close()
    total=row['total'] or 0; open_=row['open'] or 0; pending=row['pending'] or 0; expired=row['expired'] or 0
    closed=total-open_-pending-expired; wins=row['wins'] or 0
    return {'total':total,'open':open_,'pending':pending,'expired':expired,'closed':closed,'wins':wins,'losses':row['losses'] or 0,'partials':row['partials'] or 0,'win_rate':round(wins/closed*100,2) if closed else 0}

def send_trade_summary():
    s=stats(); telegram_send(f'📊 *TRADE STATS*\nTotal: `{s["total"]}`\nOpen: `{s["open"]}`\nWins: `{s["wins"]}`\nLosses: `{s["losses"]}`\nPartial: `{s["partials"]}`\nWin rate: `{s["win_rate"]}%`')

# ----------------------------- Backtest / factor stats -----------------
FACTOR_KEYS = ['htf_bias','trend_regime','liquidity_sweep','bos','order_block','choch',
               'fvg','crt','tbs','candle_pattern','volume_expansion','momentum','eqh_eql_pool']

def _trade_r_multiple(row):
    """R-multiple actually achieved by a closed trade.

    A full SL_LOSS (never reached any TP) realizes -1R. A TPn_LOCKED_SL exit
    (stop had already ratcheted to breakeven/TP1 before the reversal hit it,
    see check_trade) realizes whatever that locked level actually banked --
    0R for a breakeven stop, a positive partial R for a TP1-locked stop --
    not a blanket -1R."""
    try:
        entry=float(row['entry']); sl=float(row['sl'])
    except (TypeError, ValueError):
        return None
    risk=abs(entry-sl)
    if risk <= 0:
        return None
    if row['final_result']=='TP3_WIN':
        try:
            tp3=float(row['tp3'])
        except (TypeError, ValueError):
            return None
        return abs(tp3-entry)/risk
    if row['sl_hit']:
        eff=row['effective_sl'] if row['effective_sl'] is not None else sl
        try:
            eff=float(eff)
        except (TypeError, ValueError):
            eff=sl
        return (eff-entry)/risk if row['direction']=='LONG' else (entry-eff)/risk
    return None

def factor_backtest_report(min_sample=5):
    """Splits every CLOSED trade that has recorded factors into 'factor
    present' vs 'factor absent' groups per scoring factor, and reports win
    rate + average R-multiple for each side. This is the actual data-driven
    answer to 'which factor combo wins more' -- not a guess."""
    with DB_LOCK:
        con=db()
        rows=[dict(x) for x in con.execute(
            "SELECT * FROM trades WHERE status='CLOSED' AND factors_json IS NOT NULL AND factors_json != ''"
        ).fetchall()]
        con.close()
    report={'total_closed_with_factors':len(rows),'min_sample_per_side':min_sample,'factors':{}}
    if not rows:
        report['note']=('No closed trades with recorded factors yet. Factor tags are only '
                         'saved on trades created after this backtest feature was added -- '
                         'let the bot run and accumulate closed trades, then call this again.')
        return report

    def _group_stats(group):
        n=len(group)
        if n==0:
            return {'trades':0,'win_rate':None,'avg_r':None}
        wins=sum(1 for r in group if r['final_result']=='TP3_WIN')
        r_vals=[v for v in (_trade_r_multiple(r) for r in group) if v is not None]
        return {
            'trades':n,
            'win_rate':round(wins/n*100,1),
            'avg_r':round(sum(r_vals)/len(r_vals),2) if r_vals else None,
        }

    for key in FACTOR_KEYS:
        with_f, without_f = [], []
        for r in rows:
            try:
                flags=(json.loads(r['factors_json']) or {}).get('quant',{})
            except Exception:
                flags={}
            (with_f if flags.get(key) else without_f).append(r)
        w=_group_stats(with_f); wo=_group_stats(without_f)
        report['factors'][key]={
            'with_factor':w, 'without_factor':wo,
            'edge_win_rate_pts': round(w['win_rate']-wo['win_rate'],1) if (w['win_rate'] is not None and wo['win_rate'] is not None) else None,
            'reliable_sample': w['trades']>=min_sample and wo['trades']>=min_sample,
        }
    report['factors']=dict(sorted(
        report['factors'].items(),
        key=lambda kv: (kv[1]['edge_win_rate_pts'] if kv[1]['edge_win_rate_pts'] is not None else -999),
        reverse=True
    ))
    return report

# ----------------------------- Web -----------------------------------

@app.get('/telegram-status')
def telegram_status():
    if not TELEGRAM_TOKEN:
        return jsonify({'configured':False,'error':'TELEGRAM_BOT_TOKEN missing'}), 503
    try:
        me=requests.get(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe', timeout=10).json()
        wh=requests.get(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo', timeout=10).json()
        return jsonify({'configured':True,'bot':me.get('result',{}),'webhook':wh.get('result',{}),'chat_id_bound':current_chat_id(),'scanner_active':scanner_is_active()})
    except Exception as exc:
        return jsonify({'configured':True,'error':str(exc)}), 502

@app.get('/health')
def health():
    return jsonify({'status':'ok','scanner_active':scanner_is_active(),'tracker_active':True,'framework':FRAMEWORK,'timeframes':TIMEFRAMES,'watchlist':WATCHLIST,'gemini_configured':bool(GEMINI_API_KEYS),'telegram_configured':bool(TELEGRAM_TOKEN),'telegram_chat_bound':bool(current_chat_id()),'exchanges':EXCHANGE_ORDER,'last_scan':LAST_SCAN,'last_error':LAST_ERROR})

@app.get('/run-now')
def run_now():
    if not scanner_is_active(): return jsonify({'status':'stopped','message':'Scanner is OFF. Send /start on Telegram first.','hint':'If /start is not reflected, check Telegram polling logs and TELEGRAM_BOT_TOKEN.'}), 409
    threading.Thread(target=scan_once,kwargs={'force':True},daemon=True).start(); return jsonify({'status':'scan started','time':now_utc()})

@app.get('/api/trades')
def api_trades():
    limit=min(max(int(request.args.get('limit',100)),1),500)
    with DB_LOCK:
        con=db(); rows=[dict(x) for x in con.execute('SELECT * FROM trades ORDER BY id DESC LIMIT ?', (limit,)).fetchall()]; con.close()
    return jsonify({'stats':stats(),'trades':rows})

@app.get('/api/scans')
def api_scans():
    with DB_LOCK:
        con=db(); rows=[dict(x) for x in con.execute('SELECT * FROM scans ORDER BY id DESC LIMIT 200').fetchall()]; con.close()
    return jsonify(rows)

@app.get('/api/backtest')
def api_backtest():
    """Data-driven factor win-rate report. See procedure/explanation given
    alongside this endpoint -- min_sample query param controls how many
    trades per side are required before a factor is marked 'reliable'."""
    min_sample=max(1,int(request.args.get('min_sample',5)))
    return jsonify(factor_backtest_report(min_sample))

@app.post('/api/reset')
def api_reset():
    with DB_LOCK:
        con=db(); con.execute('DELETE FROM trades'); con.execute('DELETE FROM scans'); con.execute("DELETE FROM sqlite_sequence WHERE name IN ('trades','scans')"); con.commit(); con.close()
    telegram_send('🧹 *Trade history reset from dashboard.* Counting starts from zero.')
    return jsonify({'status':'reset'})

@app.get('/')
def dashboard():
    s=stats()
    with DB_LOCK:
        con=db(); trades=[dict(x) for x in con.execute("SELECT * FROM trades WHERE status!='PENDING_LIMIT' ORDER BY id DESC LIMIT 50").fetchall()]
        pending=[dict(x) for x in con.execute("SELECT * FROM trades WHERE status='PENDING_LIMIT' ORDER BY id DESC LIMIT 30").fetchall()]
        scans=[dict(x) for x in con.execute('SELECT * FROM scans ORDER BY id DESC LIMIT 30').fetchall()]; con.close()
    rows=''.join(f'<tr><td>#{t["id"]}</td><td>{t["symbol"]}</td><td>{t["direction"]}</td><td>{t["score"]}</td><td>{t["status"]}</td><td>{t["highest_tp"] or 0}</td><td>{t["final_result"] or "—"}</td></tr>' for t in trades)
    pendrows=''.join(
        f'<tr><td>#{p["id"]}</td><td>{p["symbol"]}</td><td>{p["direction"]}</td><td>{p["score"]}</td>'
        f'<td>{fmt_price(p["entry"])}</td><td>{p["created_at"]}</td></tr>' for p in pending
    )
    scanrows=''.join(f'<tr><td>{x["time"]}</td><td>{x["symbol"]}</td><td>{x["score"]}</td><td>{x["decision"]}</td><td>{"YES" if x["gemini_called"] else "NO"}</td><td>{x["reason"] or "—"}</td></tr>' for x in scans)
    bt=factor_backtest_report()
    if bt.get('note'):
        btrows=f'<tr><td colspan="5">{bt["note"]}</td></tr>'
    else:
        def _cell(g):
            if g['trades']==0: return '—'
            r=f"{g['win_rate']}% ({g['trades']})"
            if g['avg_r'] is not None: r+=f" · avg {g['avg_r']}R"
            return r
        btrows=''.join(
            f'<tr><td>{k.replace("_"," ")}</td><td>{_cell(v["with_factor"])}</td><td>{_cell(v["without_factor"])}</td>'
            f'<td>{("+" if (v["edge_win_rate_pts"] or 0)>=0 else "")}{v["edge_win_rate_pts"]}pt</td>'
            f'<td>{"✅" if v["reliable_sample"] else "low sample"}</td></tr>'
            for k,v in bt['factors'].items()
        )
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="60"><title>SMC AI PRO</title><style>body{{font-family:Arial;background:#080b0f;color:#d7e0ea;margin:0;padding:24px}}h1{{color:#fff}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}.card{{background:#11161d;border:1px solid #252d36;border-radius:12px;padding:16px}}.n{{font-size:25px;color:#fff;font-weight:700}}table{{width:100%;border-collapse:collapse;margin-top:12px;background:#0e1319}}th,td{{padding:9px;border-bottom:1px solid #202730;text-align:left;font-size:12px}}td:last-child{{color:#9aa7b5;max-width:340px}}button{{padding:10px 14px;border:0;border-radius:8px;background:#ef4444;color:#fff;font-weight:700;cursor:pointer}}a{{color:#60a5fa}}section{{margin-top:28px}}</style></head><body><h1>⚡ SMC AI PRO</h1><p>Framework: <b>{FRAMEWORK}</b> · Timeframes: <b>{" → ".join(TIMEFRAMES)}</b> · Scanner: <b>{"ON" if scanner_is_active() else "OFF"}</b> · Tracker: <b>ON</b></p><div class="grid"><div class="card">Total<div class="n">{s['total']}</div></div><div class="card">Open<div class="n">{s['open']}</div></div><div class="card">Pending Limits<div class="n">{s['pending']}</div></div><div class="card">Wins<div class="n">{s['wins']}</div></div><div class="card">Losses<div class="n">{s['losses']}</div></div><div class="card">Partial<div class="n">{s['partials']}</div></div><div class="card">Win rate<div class="n">{s['win_rate']}%</div></div></div><section><button onclick="resetAll()">Reset All Trade Statistics</button> <a href="/health">Health</a> <a href="/api/trades">Trades JSON</a> <a href="/api/backtest">Backtest JSON</a> <a href="/run-now">Run Now</a></section><section><h2>Order Limits — watching for fill</h2><table><tr><th>ID</th><th>Symbol</th><th>Dir</th><th>Score</th><th>Zone Entry</th><th>Placed At</th></tr>{pendrows or '<tr><td colspan="6">No pending limit orders</td></tr>'}</table></section><section><h2>Trade Tracking</h2><table><tr><th>ID</th><th>Symbol</th><th>Dir</th><th>Score</th><th>Status</th><th>Highest TP</th><th>Result</th></tr>{rows or '<tr><td colspan="7">No tracked trades</td></tr>'}</table></section><section><h2>Factor Backtest — closed trades only ({bt.get("total_closed_with_factors",0)} sampled)</h2><table><tr><th>Factor</th><th>Win rate WITH it</th><th>Win rate WITHOUT it</th><th>Edge</th><th>Sample</th></tr>{btrows or '<tr><td colspan="5">No data yet</td></tr>'}</table></section><section><h2>Scan Log</h2><table><tr><th>Time</th><th>Symbol</th><th>Score</th><th>Decision</th><th>Gemini</th><th>Reason</th></tr>{scanrows or '<tr><td colspan="6">No scans</td></tr>'}</table></section><script>async function resetAll(){{if(!confirm('Delete ALL trades and scan history?'))return;let r=await fetch('/api/reset',{{method:'POST'}});if(r.ok)location.reload();else alert('Reset failed');}}</script></body></html>'''

# ----------------------------- Startup --------------------------------
init_db()
if RESET_ON_START:
    with DB_LOCK:
        con=db(); con.execute('DELETE FROM trades'); con.execute('DELETE FROM scans'); con.execute('DELETE FROM bot_state'); con.execute("DELETE FROM sqlite_sequence WHERE name IN ('trades','scans')"); con.commit(); con.close()
    log.info('RESET_ON_START=1 -> cleared trades, scan logs and bot state for a fresh session')
BOT_ACTIVE = False
state_set('active','0')
scheduler.add_job(scheduled_scan,'interval',minutes=SCAN_INTERVAL,id='scanner',replace_existing=True,max_instances=1,coalesce=True)
scheduler.add_job(scheduled_track,'interval',minutes=TRACK_INTERVAL,id='tracker',replace_existing=True,max_instances=1,coalesce=True)
scheduler.start()
if TELEGRAM_TOKEN:
    threading.Thread(target=telegram_poll,daemon=True,name='telegram-poll').start()

log.info('SMC AI PRO started | framework=%s | tf=%s | watchlist=%s | scan=%sm | track=%sm | min_score=%s', FRAMEWORK,TIMEFRAMES,WATCHLIST,SCAN_INTERVAL,TRACK_INTERVAL,MIN_SCORE)

if __name__=='__main__':
    app.run(host='0.0.0.0',port=PORT)

@app.route('/setup-status')
def setup_status():
    score = float(request.args.get("score", 0))
    ai = request.args.get("ai", "").upper()
    result = classify_setup(score, {"decision": ai} if ai else None)
    return jsonify(result)
