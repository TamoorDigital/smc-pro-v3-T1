ALL_SIGNALS_TELEGRAM = True

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


def _raw_candles_for_ai(symbol, timeframe, limit=30):
    """Uses the same Binance kline fetcher as the rest of the app (fetch_klines)."""
    raw = fetch_klines(symbol, timeframe, limit)
    return raw[-limit:]


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


def validate_trade_with_gemini(symbol, direction, score, analysis, trade_levels, raw_context):
    """
    Gemini is a validator, not the primary signal generator.
    It must explicitly validate Entry/SL/TP1/TP2/TP3 against supplied raw candles.
    """
    prompt = {
        "task": "Validate this proposed crypto trade. Do NOT approve merely because the score is high.",
        "symbol": symbol,
        "direction": direction,
        "quant_score": score,
        "analysis": analysis,
        "proposed_trade": trade_levels,
        "raw_market_candles": raw_context,
        "required_checks": [
            "Validate entry against current price/structure.",
            "Validate SL against market structure, liquidity and volatility.",
            "Validate TP1, TP2 and TP3 against raw candles, structure and nearby liquidity.",
            "Check that the trade direction agrees with the higher timeframe bias.",
            "Check candlestick patterns and whether they support or contradict the setup.",
            "Check that risk/reward is realistic.",
            "Reject if the proposed levels are structurally invalid or contradictory.",
            "If a level is wrong, suggest corrected levels but do not approve until the corrected levels are internally valid."
        ],
        "response_format": {
            "decision": "APPROVE or REJECT",
            "confidence": "0-100",
            "entry_valid": "true/false",
            "sl_valid": "true/false",
            "tp1_valid": "true/false",
            "tp2_valid": "true/false",
            "tp3_valid": "true/false",
            "suggested_entry": "number or null",
            "suggested_sl": "number or null",
            "suggested_tp1": "number or null",
            "suggested_tp2": "number or null",
            "suggested_tp3": "number or null",
            "reason": "short reason"
        }
    }
    system = "You are the FINAL VALIDATOR for a crypto trading research bot. Do NOT approve merely because the score is high. Validate every level against the supplied raw candles and structure. Return JSON only, matching the requested response_format."
    try:
        result = gemini_json(system, prompt, max_output_tokens=500)
        return result if isinstance(result, dict) else {"decision": "REJECT", "confidence": 0, "reason": "Invalid AI response"}
    except Exception as exc:
        log.warning(f"[{symbol}] Gemini validation failed: {exc}")
        return {"decision": "REJECT", "confidence": 0, "reason": f"Gemini validation failed: {exc}"}


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
    if direction == "LONG":
        return min(10, 2 * len(set(patterns) & bullish)) - min(4, len(set(patterns) & bearish))
    return min(10, 2 * len(set(patterns) & bearish)) - min(4, len(set(patterns) & bullish))
import os, re, json, time, math, sqlite3, threading, logging
from datetime import datetime, timezone
from typing import Any

import requests
from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

# ----------------------------- Config ---------------------------------
PORT = int(os.getenv('PORT', '10000'))
BINANCE_BASE = os.getenv('BINANCE_BASE', 'https://fapi.binance.com')
WATCHLIST = [x.strip().upper() for x in os.getenv('WATCHLIST', 'BTCUSDT,ETHUSDT,SOLUSDT').split(',') if x.strip()]
FRAMEWORK = os.getenv('FRAMEWORK', '4h_1h_15m')
SCAN_INTERVAL = max(5, int(os.getenv('SCAN_INTERVAL_MINUTES', '15')))
TRACK_INTERVAL = max(1, int(os.getenv('TRACK_INTERVAL_MINUTES', '1')))
TRACK_TIMEFRAME = os.getenv('TRACK_TIMEFRAME', '1m')
MIN_SCORE = max(0, min(100, int(os.getenv('MIN_SCORE', '70'))))
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GEMINI_MIN_INTERVAL = max(1.0, float(os.getenv('GEMINI_MIN_INTERVAL_SECONDS', '13')))
GEMINI_TIMEOUT = int(os.getenv('GEMINI_TIMEOUT_SECONDS', '60'))
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
DATA_DIR = os.getenv('DATA_DIR', './data')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'smc_pro.db')

TF_MAP = {
    '4h_1h_15m': ['4h', '1h', '15m'],
    '1h_15m_5m': ['1h', '15m', '5m'],
    '4h_15m_5m': ['4h', '15m', '5m'],
    '1h_30m_5m': ['1h', '30m', '5m'],
}
TIMEFRAMES = TF_MAP.get(FRAMEWORK, FRAMEWORK.split('_') if '_' in FRAMEWORK else ['4h','1h','15m'])
if len(TIMEFRAMES) != 3:
    TIMEFRAMES = ['4h','1h','15m']

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('smc-pro')
app = Flask(__name__)
scheduler = BackgroundScheduler(daemon=True)
DB_LOCK = threading.Lock()
GEMINI_LOCK = threading.Lock()
GEMINI_LAST_CALL = 0.0
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
        ''')
        con.commit(); con.close()

def state_get(key, default=None):
    with DB_LOCK:
        con = db(); row = con.execute('SELECT value FROM bot_state WHERE key=?', (key,)).fetchone(); con.close()
    return row['value'] if row else default

def state_set(key, value):
    with DB_LOCK:
        con = db(); con.execute('INSERT INTO bot_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value))); con.commit(); con.close()

def telegram_send(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage', json={
            'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'Markdown', 'disable_web_page_preview': True
        }, timeout=10)
        if not r.ok:
            log.warning('Telegram error %s: %s', r.status_code, r.text[:300])
        return r.ok
    except Exception as e:
        log.warning('Telegram exception: %s', e)
        return False

def tf_label(tf):
    return tf.upper().replace('H','H').replace('M','M')

# ----------------------------- Binance --------------------------------
def fetch_klines(symbol, interval, limit=150):
    r = requests.get(f'{BINANCE_BASE}/fapi/v1/klines', params={'symbol':symbol,'interval':interval,'limit':limit}, timeout=20)
    r.raise_for_status()
    out=[]
    for x in r.json():
        out.append({'open_time':x[0], 'open':float(x[1]), 'high':float(x[2]), 'low':float(x[3]), 'close':float(x[4]), 'volume':float(x[5])})
    return out

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

def analyze_tf(c):
    closes=[x['close'] for x in c]; vols=[x['volume'] for x in c]
    e20=ema(closes,20); e50=ema(closes,50); r=rsi(closes); a=atr(c)
    highs,lows=swing_levels(c,3)
    last=c[-1]; prev=c[-2]
    recent_high=max(x['high'] for x in c[-30:]); recent_low=min(x['low'] for x in c[-30:])
    avgvol=sum(vols[-20:])/min(20,len(vols)); vol_ratio=(last['volume']/avgvol) if avgvol else 1
    bullish = e20 > e50 and last['close'] > e20 and slope(closes,5)>0
    bearish = e20 < e50 and last['close'] < e20 and slope(closes,5)<0
    bias='BULLISH' if bullish else 'BEARISH' if bearish else 'NEUTRAL'
    # liquidity sweep: last candle pierced a recent swing/extreme and closed back inside.
    prior_high=max(x['high'] for x in c[-21:-1]); prior_low=min(x['low'] for x in c[-21:-1])
    bull_sweep = last['low'] < prior_low and last['close'] > prior_low
    bear_sweep = last['high'] > prior_high and last['close'] < prior_high
    # simple BOS: close breaks previous 20-candle extreme.
    bull_bos = last['close'] > prior_high
    bear_bos = last['close'] < prior_low
    # momentum confirmation
    bull_mom = r >= 52 and r <= 72
    bear_mom = r <= 48 and r >= 28
    return {
        'bias':bias, 'ema20':e20, 'ema50':e50, 'rsi':r, 'atr':a,
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

def build_analysis(symbol):
    tf1,tf2,tf3=TIMEFRAMES
    c1=fetch_klines(symbol,tf1,160); c2=fetch_klines(symbol,tf2,160); c3=fetch_klines(symbol,tf3,160)
    a1,a2,a3=analyze_tf(c1),analyze_tf(c2),analyze_tf(c3)
    # Score both directions. Require MTF alignment, but allow one lower TF disagreement if setup is strong.
    scores={'LONG':0,'SHORT':0}; reasons={'LONG':[],'SHORT':[]}
    # HTF bias 20
    if a1['bias']=='BULLISH': scores['LONG']+=20; reasons['LONG'].append('HTF bullish')
    if a1['bias']=='BEARISH': scores['SHORT']+=20; reasons['SHORT'].append('HTF bearish')
    # Market regime 10: trend strength proxy from EMA separation / ATR.
    sep=abs(a1['ema20']-a1['ema50'])/max(a1['atr'],1e-12)
    if sep >= 1.0:
        for d in scores: scores[d]+=10 if ((d=='LONG' and a1['bias']=='BULLISH') or (d=='SHORT' and a1['bias']=='BEARISH')) else 0
        if a1['bias'] in ('BULLISH','BEARISH'): reasons[a1['bias'].replace('BULLISH','LONG').replace('BEARISH','SHORT')].append('trending regime')
    # Liquidity sweep 15
    if a2['bull_sweep']: scores['LONG']+=15; reasons['LONG'].append('sell-side liquidity sweep')
    if a2['bear_sweep']: scores['SHORT']+=15; reasons['SHORT'].append('buy-side liquidity sweep')
    # BOS / CHoCH 15
    if a2['bull_bos']: scores['LONG']+=15; reasons['LONG'].append('bullish BOS')
    if a2['bear_bos']: scores['SHORT']+=15; reasons['SHORT'].append('bearish BOS')
    # OB/FVG proxy 10 when price is near a detected zone.
    for d in ('LONG','SHORT'):
        z=detect_zone(c2,d)
        if z and z['low'] <= a3['price'] <= z['high']*1.002:
            scores[d]+=10; reasons[d].append('price at order-block zone')
    # Volume 10
    if a3['volume_ratio']>=1.2:
        d='LONG' if a3['bull_mom'] else 'SHORT' if a3['bear_mom'] else None
        if d: scores[d]+=10; reasons[d].append('volume expansion')
    # Momentum 5
    if a3['bull_mom']: scores['LONG']+=5; reasons['LONG'].append(f'RSI {a3["rsi"]:.0f}')
    if a3['bear_mom']: scores['SHORT']+=5; reasons['SHORT'].append(f'RSI {a3["rsi"]:.0f}')
    # R:R is calculated after entry/SL/TP plan.
    direction=max(scores, key=scores.get); base=scores[direction]
    price=a3['price']; a=a3['atr'] or (price*0.005)
    if direction=='LONG':
        sl=price-1.2*a; risk=price-sl; tp1=price+1.5*risk; tp2=price+2.3*risk; tp3=price+3.2*risk
    else:
        sl=price+1.2*a; risk=sl-price; tp1=price-1.5*risk; tp2=price-2.3*risk; tp3=price-3.2*risk
    rr=abs(tp3-price)/max(abs(price-sl),1e-12)
    if rr>=2: scores[direction]+=5; reasons[direction].append('R:R >= 1:2')
    score=min(100,scores[direction])
    return {
        'symbol':symbol,'framework':FRAMEWORK,'timeframes':TIMEFRAMES,
        'direction':direction,'score':score,'price':price,'entry':price,'sl':sl,'tp1':tp1,'tp2':tp2,'tp3':tp3,'rr':rr,
        'reasons':reasons[direction], 'tf':{tf1:a1,tf2:a2,tf3:a3}, 'candles':{tf1:c1[-20:],tf2:c2[-20:],tf3:c3[-20:]}
    }

# ----------------------------- Gemini ---------------------------------
def gemini_json(system_text, user_payload, max_output_tokens=300):
    """Shared low-level Gemini call: rate-limited, JSON-only. Used by every Gemini-calling function."""
    global GEMINI_LAST_CALL
    if not GEMINI_API_KEY:
        raise RuntimeError('GEMINI_API_KEY is missing')
    body={'system_instruction':{'parts':[{'text':system_text}]},'contents':[{'role':'user','parts':[{'text':json.dumps(user_payload)}]}], 'generationConfig':{'temperature':0.1,'maxOutputTokens':max_output_tokens,'responseMimeType':'application/json'}}
    with GEMINI_LOCK:
        wait=GEMINI_MIN_INTERVAL-(time.monotonic()-GEMINI_LAST_CALL)
        if wait>0: time.sleep(wait)
        url=f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'
        r=requests.post(url, params={'key':GEMINI_API_KEY}, json=body, timeout=GEMINI_TIMEOUT)
        GEMINI_LAST_CALL=time.monotonic()
        if r.status_code==429:
            raise RuntimeError(f'Gemini 429 RESOURCE_EXHAUSTED: {r.text[:700]}')
        r.raise_for_status(); data=r.json()
    txt=data['candidates'][0]['content']['parts'][0]['text'].strip()
    try: return json.loads(txt)
    except json.JSONDecodeError:
        m=re.search(r'\{.*\}',txt,re.S)
        if not m: raise RuntimeError('Gemini returned non-JSON: '+txt[:500])
        return json.loads(m.group(0))

def gemini_validate(analysis):
    prompt={
      'symbol':analysis['symbol'], 'framework':analysis['framework'], 'timeframes':analysis['timeframes'],
      'direction_candidate':analysis['direction'], 'quant_score':analysis['score'],
      'entry':analysis['entry'],'sl':analysis['sl'],'tp1':analysis['tp1'],'tp2':analysis['tp2'],'tp3':analysis['tp3'],'rr':analysis['rr'],
      'reasons':analysis['reasons'],
      'timeframe_analysis':{k:{x:v for x,v in a.items() if x not in ('high','low','range')} for k,a in analysis['tf'].items()}
    }
    system='''You are the FINAL VALIDATOR for a crypto trading research bot. Do not invent market data. Review only the supplied structured analysis. Approve only when the multi-timeframe direction is coherent, the setup has a plausible liquidity/structure trigger, and the proposed risk plan is valid. You are not a guaranteed predictor. Return JSON only: {"decision":"APPROVE"|"REJECT","confidence":0-100,"reason":"short reason","risk_note":"short note"}. Reject contradictions, weak structure, or invalid risk/reward.'''
    return gemini_json(system, prompt)

# ----------------------------- Trades ---------------------------------
def insert_trade(a, ai):
    with DB_LOCK:
        con=db(); cur=con.execute('''INSERT INTO trades(symbol,direction,entry,sl,tp1,tp2,tp3,score,ai_confidence,ai_reason,framework,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
          (a['symbol'],a['direction'],a['entry'],a['sl'],a['tp1'],a['tp2'],a['tp3'],a['score'],int(ai.get('confidence',0)),ai.get('reason',''),FRAMEWORK,now_utc()))
        con.commit(); tid=cur.lastrowid; con.close()
    return tid

def has_open_similar(symbol,direction):
    with DB_LOCK:
        con=db(); row=con.execute("SELECT 1 FROM trades WHERE symbol=? AND direction=? AND status IN ('WAITING_ENTRY','OPEN') LIMIT 1",(symbol,direction)).fetchone(); con.close()
    return bool(row)

def check_trade(row, candle):
    direction=row['direction']; high=candle['high']; low=candle['low']; entry=row['entry']; sl=row['sl']
    status=row['status']; htp=row['highest_tp'] or 0
    if status=='WAITING_ENTRY':
        if low <= entry <= high: status='OPEN'
        else: return None
    tp_hits=[]
    for n,key in [(1,'tp1'),(2,'tp2'),(3,'tp3')]:
        if row[key] is not None and ((direction=='LONG' and high>=row[key]) or (direction=='SHORT' and low<=row[key])): tp_hits.append(n)
    sl_hit=(low<=sl) if direction=='LONG' else (high>=sl)
    if sl_hit and tp_hits:
        new=max([htp]+tp_hits); return {'status':'CLOSED','highest_tp':new,'tp1_hit':int(new>=1),'tp2_hit':int(new>=2),'tp3_hit':int(new>=3),'sl_hit':1,'final_result':f'TP{new}_AND_SL_SAME_CANDLE','close_time':now_utc()}
    if sl_hit:
        result='SL_LOSS' if htp==0 else f'TP{htp}_HIT_THEN_SL'
        return {'status':'CLOSED','sl_hit':1,'final_result':result,'close_time':now_utc()}
    if tp_hits:
        new=max([htp]+tp_hits)
        u={'status':'OPEN','highest_tp':new,'tp1_hit':int(new>=1),'tp2_hit':int(new>=2),'tp3_hit':int(new>=3),'last_checked':now_utc()}
        if new>=3: u.update({'status':'CLOSED','final_result':'TP3_WIN','close_time':now_utc()})
        return u
    return {'status':status,'last_checked':now_utc()}


def fetch_tracking_candle(symbol, limit=3):
    """Dedicated 1-minute OHLCV feed used only for TP/SL monitoring."""
    return fetch_klines(symbol, TRACK_TIMEFRAME, limit)


def track_trades():
    with DB_LOCK:
        con=db(); rows=con.execute("SELECT * FROM trades WHERE status IN ('WAITING_ENTRY','OPEN')").fetchall(); con.close()
    for row in rows:
        try:
            candles=fetch_klines(row['symbol'],TRACK_TIMEFRAME,3)
            if not candles: continue
            u=check_trade(row,candles[-1]);
            if not u: continue
            with DB_LOCK:
                con=db();
                sets=', '.join(f'{k}=?' for k in u); con.execute(f'UPDATE trades SET {sets},last_price=?,last_checked=? WHERE id=?',list(u.values())+[candles[-1]['close'],now_utc(),row['id']]); con.commit(); con.close()
            if u.get('status')=='CLOSED':
                result=u.get('final_result','CLOSED'); icon='🟢' if result=='TP3_WIN' else '🟡' if 'TP' in result else '🔴'
                telegram_send(f'{icon} *TRADE CLOSED — {row["symbol"]} {row["direction"]}*\nTrade #{row["id"]}\nResult: *{result}*\nEntry: `{row["entry"]}` | SL: `{row["sl"]}`\nTP1: `{row["tp1"]}` | TP2: `{row["tp2"]}` | TP3: `{row["tp3"]}`')
        except Exception as e: log.warning('tracker %s: %s',row['symbol'],e)

# ----------------------------- Scanning -------------------------------

def send_setup_status(symbol, direction, score, status, reason=""):
    """Telegram helper for APPROVED / WAIT-WATCH setups (uses telegram_send, not the undefined send_tg)."""
    if status == "APPROVED":
        telegram_send(f"🚨 *{symbol} {direction} TRADE APPROVED*\nScore: *{score}/100*\n" + (f"Reason: {reason}" if reason else ""))
    elif status == "WAIT/WATCH":
        telegram_send(f"👀 *{symbol} {direction} — WAIT / WATCH*\nScore: *{score}/100*\n" + (f"Reason: {reason}" if reason else ""))

def scan_once(force=False):
    global LAST_SCAN,LAST_ERROR
    if not BOT_ACTIVE and not force: return {'status':'stopped'}
    result={'time':now_utc(),'symbols':{}}
    for symbol in WATCHLIST:
        try:
            a=build_analysis(symbol); score=a['score']
            if score<MIN_SCORE:
                reason='; '.join(a['reasons']) or 'insufficient confluence'
                with DB_LOCK:
                    con=db(); con.execute('INSERT INTO scans(time,symbol,score,decision,gemini_called,reason) VALUES(?,?,?,?,?,?)',(now_utc(),symbol,score,'NO_TRADE',0,reason)); con.commit(); con.close()
                telegram_send(f'⚪ *NO TRADE — {symbol}*\nScore: `{score}/100`\nRequired: `{MIN_SCORE}`\nGemini: `SKIPPED`\nReason: {reason}')
                result['symbols'][symbol]={'score':score,'decision':'NO_TRADE','gemini':False}; continue
            if has_open_similar(symbol,a['direction']):
                result['symbols'][symbol]={'score':score,'decision':'SKIP_OPEN_TRADE','gemini':False}; continue
            ai=gemini_validate(a)
            gemini_approved=str(ai.get('decision','REJECT')).upper()=='APPROVE'
            approved=False; reason=ai.get('reason','Gemini rejected')
            if gemini_approved:
                # Hard structural check: Gemini can say APPROVE, but the levels still must be internally valid.
                level_ok, level_reason = _final_level_check(a['direction'], a['entry'], a['sl'], a['tp1'], a['tp2'], a['tp3'])
                if level_ok:
                    approved=True; reason=ai.get('reason','Gemini approved')
                else:
                    reason=f'Gemini approved but failed hard level check: {level_reason}'
            decision='APPROVED' if approved else 'REJECTED'
            with DB_LOCK:
                con=db(); con.execute('INSERT INTO scans(time,symbol,score,decision,gemini_called,reason) VALUES(?,?,?,?,?,?)',(now_utc(),symbol,score,decision,1,reason)); con.commit(); con.close()
            if approved:
                tid=insert_trade(a,ai)
                telegram_send(f'🚨 *SMC AI PRO — {a["direction"]} {symbol}*\nTrade #{tid}\nQuant Score: `{score}/100`\nAI Confidence: `{ai.get("confidence",0)}%`\nEntry: `{a["entry"]:.6f}`\nSL: `{a["sl"]:.6f}`\nTP1: `{a["tp1"]:.6f}`\nTP2: `{a["tp2"]:.6f}`\nTP3: `{a["tp3"]:.6f}`\nR:R: `1:{a["rr"]:.2f}`\nFramework: `{FRAMEWORK}`\nAI: *APPROVED*\nReason: {ai.get("reason","")}')
            elif ALL_SIGNALS_TELEGRAM:
                send_setup_status(symbol, a['direction'], score, 'WAIT/WATCH', reason)
            result['symbols'][symbol]={'score':score,'decision':decision,'gemini':True,'ai':ai,'reason':reason}
        except Exception as e:
            LAST_ERROR=f'{symbol}: {type(e).__name__}: {e}'; log.exception('scan %s',symbol); result['symbols'][symbol]={'error':str(e)}
        time.sleep(1)
    LAST_SCAN=now_utc(); return result

def scheduled_scan():
    if BOT_ACTIVE:
        log.info('Scheduled scan started'); scan_once()

def scheduled_track():
    track_trades()

# ----------------------------- Telegram commands ----------------------
def telegram_poll():
    global TELEGRAM_OFFSET,BOT_ACTIVE
    if not TELEGRAM_TOKEN: return
    while True:
        try:
            r=requests.get(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates',params={'timeout':25,'offset':TELEGRAM_OFFSET},timeout=35)
            if not r.ok: time.sleep(5); continue
            for u in r.json().get('result',[]):
                TELEGRAM_OFFSET=u['update_id']+1
                msg=u.get('message') or {}; chat=str(msg.get('chat',{}).get('id','')); text=(msg.get('text') or '').strip().split()[0].lower()
                if TELEGRAM_CHAT_ID and chat!=str(TELEGRAM_CHAT_ID): continue
                if text=='/start':
                    BOT_ACTIVE=True; state_set('active','1'); telegram_send(f'▶️ *SMC AI PRO started*\nScanning every {SCAN_INTERVAL} min\nTracking existing trades every {TRACK_INTERVAL} min\nFramework: `{FRAMEWORK}`'); scheduled_scan()
                elif text=='/stop':
                    BOT_ACTIVE=False; state_set('active','0'); telegram_send('⏹️ *New signal scanning stopped.*\nExisting trades will CONTINUE to be tracked.')
                elif text in ('/status','/health'):
                    st=stats(); telegram_send(f'ℹ️ *Status*\nScanner: `{"ON" if BOT_ACTIVE else "OFF"}`\nTracker: `ON`\nFramework: `{FRAMEWORK}`\nTrades: `{st["total"]}`\nOpen: `{st["open"]}`\nWin rate: `{st["win_rate"]}%`')
                elif text=='/run-now':
                    if BOT_ACTIVE: telegram_send('🔎 Manual scan started.'); scan_once(force=True)
                    else: telegram_send('⏸️ Scanner is stopped. Send /start first. Trade tracking remains active.')
                elif text=='/trades':
                    send_trade_summary()
        except Exception as e:
            log.warning('telegram poll: %s',e); time.sleep(5)

def stats():
    with DB_LOCK:
        con=db(); row=con.execute('''SELECT COUNT(*) total, SUM(status IN (\'WAITING_ENTRY\',\'OPEN\')) open, SUM(final_result=\'TP3_WIN\') wins, SUM(final_result=\'SL_LOSS\') losses, SUM(final_result LIKE \'TP%HIT_THEN_SL\' OR final_result LIKE \'TP%AND_SL_SAME_CANDLE\') partials FROM trades''').fetchone(); con.close()
    total=row['total'] or 0; open_=row['open'] or 0; closed=total-open_; wins=row['wins'] or 0
    return {'total':total,'open':open_,'closed':closed,'wins':wins,'losses':row['losses'] or 0,'partials':row['partials'] or 0,'win_rate':round(wins/closed*100,2) if closed else 0}

def send_trade_summary():
    s=stats(); telegram_send(f'📊 *TRADE STATS*\nTotal: `{s["total"]}`\nOpen: `{s["open"]}`\nWins: `{s["wins"]}`\nLosses: `{s["losses"]}`\nPartial: `{s["partials"]}`\nWin rate: `{s["win_rate"]}%`')

# ----------------------------- Web -----------------------------------
@app.get('/health')
def health():
    return jsonify({'status':'ok','scanner_active':BOT_ACTIVE,'tracker_active':True,'framework':FRAMEWORK,'timeframes':TIMEFRAMES,'watchlist':WATCHLIST,'gemini_configured':bool(GEMINI_API_KEY),'telegram_configured':bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),'last_scan':LAST_SCAN,'last_error':LAST_ERROR})

@app.get('/run-now')
def run_now():
    if not BOT_ACTIVE: return jsonify({'status':'stopped','message':'Send /start on Telegram first.'}), 409
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
        con=db(); trades=[dict(x) for x in con.execute('SELECT * FROM trades ORDER BY id DESC LIMIT 50').fetchall()]; scans=[dict(x) for x in con.execute('SELECT * FROM scans ORDER BY id DESC LIMIT 30').fetchall()]; con.close()
    rows=''.join(f'<tr><td>#{t["id"]}</td><td>{t["symbol"]}</td><td>{t["direction"]}</td><td>{t["score"]}</td><td>{t["status"]}</td><td>{t["highest_tp"] or 0}</td><td>{t["final_result"] or "—"}</td></tr>' for t in trades)
    scanrows=''.join(f'<tr><td>{x["time"]}</td><td>{x["symbol"]}</td><td>{x["score"]}</td><td>{x["decision"]}</td><td>{"YES" if x["gemini_called"] else "NO"}</td></tr>' for x in scans)
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="60"><title>SMC AI PRO</title><style>body{{font-family:Arial;background:#080b0f;color:#d7e0ea;margin:0;padding:24px}}h1{{color:#fff}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}.card{{background:#11161d;border:1px solid #252d36;border-radius:12px;padding:16px}}.n{{font-size:25px;color:#fff;font-weight:700}}table{{width:100%;border-collapse:collapse;margin-top:12px;background:#0e1319}}th,td{{padding:9px;border-bottom:1px solid #202730;text-align:left;font-size:12px}}button{{padding:10px 14px;border:0;border-radius:8px;background:#ef4444;color:#fff;font-weight:700;cursor:pointer}}a{{color:#60a5fa}}section{{margin-top:28px}}</style></head><body><h1>⚡ SMC AI PRO</h1><p>Framework: <b>{FRAMEWORK}</b> · Timeframes: <b>{" → ".join(TIMEFRAMES)}</b> · Scanner: <b>{"ON" if BOT_ACTIVE else "OFF"}</b> · Tracker: <b>ON</b></p><div class="grid"><div class="card">Total<div class="n">{s['total']}</div></div><div class="card">Open<div class="n">{s['open']}</div></div><div class="card">Wins<div class="n">{s['wins']}</div></div><div class="card">Losses<div class="n">{s['losses']}</div></div><div class="card">Partial<div class="n">{s['partials']}</div></div><div class="card">Win rate<div class="n">{s['win_rate']}%</div></div></div><section><button onclick="resetAll()">Reset All Trade Statistics</button> <a href="/health">Health</a> <a href="/api/trades">Trades JSON</a> <a href="/run-now">Run Now</a></section><section><h2>Trade Tracking</h2><table><tr><th>ID</th><th>Symbol</th><th>Dir</th><th>Score</th><th>Status</th><th>Highest TP</th><th>Result</th></tr>{rows or '<tr><td colspan="7">No tracked trades</td></tr>'}</table></section><section><h2>Scan Log</h2><table><tr><th>Time</th><th>Symbol</th><th>Score</th><th>Decision</th><th>Gemini</th></tr>{scanrows or '<tr><td colspan="5">No scans</td></tr>'}</table></section><script>async function resetAll(){{if(!confirm('Delete ALL trades and scan history?'))return;let r=await fetch('/api/reset',{{method:'POST'}});if(r.ok)location.reload();else alert('Reset failed');}}</script></body></html>'''

# ----------------------------- Startup --------------------------------
init_db()
BOT_ACTIVE = state_get('active','0') == '1'
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



def final_trade_level_check(direction, entry, sl, tp1, tp2, tp3):
    try:
        entry, sl, tp1, tp2, tp3 = map(float, (entry, sl, tp1, tp2, tp3))
    except (TypeError, ValueError):
        return False, "Invalid numeric trade levels"
    direction = str(direction).upper()
    if direction == "LONG":
        if not (sl < entry < tp1 <= tp2 <= tp3):
            return False, "Invalid LONG level ordering"
    elif direction == "SHORT":
        if not (sl > entry > tp1 >= tp2 >= tp3):
            return False, "Invalid SHORT level ordering"
    else:
        return False, "Invalid direction"
    risk = abs(entry - sl)
    if risk <= 0:
        return False, "Zero risk"
    if abs(tp2-entry)/risk < 2:
        return False, "R:R below 1:2"
    return True, "PASS"
