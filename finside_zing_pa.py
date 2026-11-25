import os
import re
import time
import pyotp
import pytz
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from SmartApi import SmartConnect

# ================= 1. API & Telegram Credentials (via GitHub Secrets) =================
API_KEY = os.environ.get("ANGEL_API_KEY", "").strip()
CLIENT_ID = os.environ.get("ANGEL_CLIENT_ID", "").strip()
PASSWORD = os.environ.get("ANGEL_PASSWORD", "").strip()
TOTP_SECRET = os.environ.get("ANGEL_TOTP_SECRET", "").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("FNO_TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("FNO_TELEGRAM_CHAT_ID", "").strip()

WATCHLIST = [
    {"name": "NIFTY 50", "scrip_name": "NIFTY", "exchange": "NSE", "token": "99926000", "strike_step": 50, "lot_size": 75},
    {"name": "BANK NIFTY", "scrip_name": "BANKNIFTY", "exchange": "NSE", "token": "99926009", "strike_step": 100, "lot_size": 30}
]

MCX_LOTS = {
    "CRUDEOIL": 100,
    "CRUDEOILM": 10,
    "NATURALGAS": 1250,
    "NATGASMINI": 250,
    "GOLD": 100,
    "GOLDM": 10,
    "GOLDGUINEA": 1,
    "GOLDPETAL": 1,
    "SILVER": 30,
    "SILVERM": 5
}

# ================= 2. v2.8 UNIVERSAL DUAL-WALLET & RISK SHIELD =========
FNO_VIRTUAL_CAPITAL = 100000.0      # ₹1,00,000 F&O Paper Trading Wallet
EQUITY_VIRTUAL_CAPITAL = 100000.0   # ₹1,00,000 Equity Stock Calls Wallet
EQUITY_MAX_RISK_RS = 500.0          # ஒரு Stock Call-க்கு அதிகபட்ச நஷ்டம் ₹500
EQUITY_MAX_EXPOSURE_RS = 40000.0    # ஒரு Stock Call-க்கு அதிகபட்ச முதலீடு ₹40,000
MAX_EQUITY_SL_PER_DAY = 3           # Equity Wallet-ல் ஒரு நாளைக்கு அதிகபட்சம் 3 SL

TARGET_PCT = 0.15                   # Option Premium-ல் 15% Target
DEFAULT_SL_PCT = 0.10               # Normal 10% SL
MAX_LOSS_PER_TRADE_RS = 550.0       # ஒரு F&O டிரேடிற்கு அதிகபட்ச நஷ்டம் ₹550
MIN_SL_POINTS = 6.0                 # குறைந்தபட்சம் ₹6 SL வித்தியாசம்
TRAIL_STEP1_PCT = 0.06              # Step 1: +6% வந்தால் SL = Entry (Cost-to-Cost)
TRAIL_STEP2_PCT = 0.10              # Step 2: +10% வந்தால் SL = +5% Profit Lock!
PROFIT_LOCK_PCT = 0.05              # Step 2-ல் பாதுகாக்கப்படும் குறைந்தபட்ச லாபம் (+5%)
MAX_DAILY_SL = 2                    # F&O-வில் ஒரு நாளைக்கு அதிகபட்சம் 2 Stop-Loss
INDEX_PROFIT_LOCK_RS = 1500.0       # ஒரு Index-ல் +₹1,500 லாபம் வந்தால் அன்றைக்கு Lock ஆகும்
MAX_ADX_EXHAUSTION = 48.0           # ADX 48 தாண்டினால் Top Exhaustion-ஆக கருதி 1/5 என்ட்ரி தவிர்க்கும்
# ========================================================================

IST = pytz.timezone('Asia/Kolkata')
sent_alerts = set()
active_trades = []          # F&O Trades (ZING + Angel One + Alice Blue)
active_equity_trades = []   # Equity Stock Calls (Angel One + Alice Blue)
scrip_master_df = None
equity_master_df = None
htf_cache = {}
daily_sl_limit_notified = False
locked_profit_indices = set()
eod_squareoff_done = False
scanner_paused = False
last_update_id = 0

def get_ist_now():
    return datetime.now(IST)

def setup_telegram_menu():
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "status", "description": "📊 Live Market Trend & ADX Check"},
        {"command": "open", "description": "⚡ Active F&O + Equity Running Trades"},
        {"command": "virtual", "description": "💼 ₹1L F&O Paper Trading Wallet"},
        {"command": "equity", "description": "📈 ₹1L Equity Stock Calls Wallet"},
        {"command": "pnl", "description": "🏆 Combined F&O + Equity Performance"},
        {"command": "excel", "description": "📥 Download 3-Sheet Live Excel Report"},
        {"command": "closeall", "description": "🛑 Close All Open Trades & Unlock"},
        {"command": "pause", "description": "⏸️ Pause New Signals"},
        {"command": "resume", "description": "▶️ Resume Signal Scanner"}
    ]
    try:
        requests.post(url, json={"commands": commands}, timeout=10)
    except Exception as e:
        print("Menu Setup Error:", e)

def send_telegram_alert(message, reply_to_msg_id=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    if reply_to_msg_id:
        payload["reply_to_message_id"] = reply_to_msg_id
    try:
        res = requests.post(url, data=payload, timeout=15).json()
        if res.get("ok"):
            return res["result"]["message_id"]
    except Exception as e:
        print("Telegram Error:", e)
    return None

def send_telegram_document(file_path, caption=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            res = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"document": f},
                timeout=30
            ).json()
        return res.get("ok", False)
    except Exception as e:
        print("Send Document Error:", e)
        return False

def generate_and_send_excel(smart_api=None, is_eod=True):
    now_ist = get_ist_now()
    date_str = now_ist.strftime("%Y-%m-%d")
    filename = f"FINSIDE_ZING_v2.8_{date_str}.xlsx"

    fno_rows = []
    for idx, t in enumerate(active_trades, start=1):
        qty = t["lot_size"]
        if t["status"] == "CLOSED":
            exit_px = t.get("exit_price", t["entry"])
            exit_time = t.get("exit_time", "-")
        else:
            exit_px = get_ltp(smart_api, t["symbol"], t["token"], t.get("exch_seg", "NFO")) if smart_api else t["entry"]
            exit_px = exit_px or t["entry"]
            exit_time = "OPEN"

        pts_pnl = round(exit_px - t["entry"], 2)
        net_rs = round(pts_pnl * qty, 2)
        fno_rows.append({
            "Trade #": idx, "Date": date_str, "Entry Time": t.get("entry_time", "-"),
            "Exit Time": exit_time, "Index": t["index_name"], "Source / Strategy": t.get("strategy", "-"),
            "Option Symbol": t["symbol"], "Lot Qty": qty, "Entry Price (₹)": t["entry"],
            "Exit / Live Price (₹)": exit_px, "Points P&L": pts_pnl, "Net P&L (₹)": net_rs, "Outcome": t["outcome"]
        })

    df_fno = pd.DataFrame(fno_rows) if fno_rows else pd.DataFrame(columns=[
        "Trade #", "Date", "Entry Time", "Exit Time", "Index", "Source / Strategy",
        "Option Symbol", "Lot Qty", "Entry Price (₹)", "Exit / Live Price (₹)",
        "Points P&L", "Net P&L (₹)", "Outcome"
    ])

    eq_rows = []
    for idx, t in enumerate(active_equity_trades, start=1):
        qty = t["qty"]
        if t["status"] == "CLOSED":
            exit_px = t.get("exit_price", t["entry"])
            exit_time = t.get("exit_time", "-")
        else:
            exit_px = get_ltp(smart_api, t["symbol"], t["token"], "NSE") if smart_api else t["entry"]
            exit_px = exit_px or t["entry"]
            exit_time = "OPEN"

        pts_pnl = round(exit_px - t["entry"], 2)
        net_rs = round(pts_pnl * qty, 2)
        eq_rows.append({
            "Trade #": idx, "Date": date_str, "Entry Time": t.get("entry_time", "-"),
            "Exit Time": exit_time, "Broker": t.get("broker", "Broker"), "Stock Symbol": t["symbol"],
            "Qty (Shares)": qty, "Entry Price (₹)": t["entry"], "Target (₹)": t["target"],
            "Stop Loss (₹)": t["sl"], "Exit / Live Price (₹)": exit_px, "Points P&L": pts_pnl,
            "Net P&L (₹)": net_rs, "Outcome": t["outcome"]
        })

    df_eq = pd.DataFrame(eq_rows) if eq_rows else pd.DataFrame(columns=[
        "Trade #", "Date", "Entry Time", "Exit Time", "Broker", "Stock Symbol", "Qty (Shares)",
        "Entry Price (₹)", "Target (₹)", "Stop Loss (₹)", "Exit / Live Price (₹)",
        "Points P&L", "Net P&L (₹)", "Outcome"
    ])

    fno_real, fno_unreal = calculate_fno_pnl(smart_api)
    fno_net = round(fno_real + fno_unreal, 2)
    eq_real, eq_unreal = calculate_equity_pnl(smart_api)
    eq_net = round(eq_real + eq_unreal, 2)
    combined_net = round(fno_net + eq_net, 2)

    df_summary = pd.DataFrame([
        {"Category": "F&O Wallet", "Metric": "Starting Capital (₹)", "Value": FNO_VIRTUAL_CAPITAL},
        {"Category": "F&O Wallet", "Metric": "Total F&O Trades", "Value": len(active_trades)},
        {"Category": "F&O Wallet", "Metric": "F&O Net P&L (₹)", "Value": fno_net},
        {"Category": "F&O Wallet", "Metric": "F&O Closing Balance (₹)", "Value": round(FNO_VIRTUAL_CAPITAL + fno_net, 2)},
        {"Category": "Equity Wallet", "Metric": "Starting Capital (₹)", "Value": EQUITY_VIRTUAL_CAPITAL},
        {"Category": "Equity Wallet", "Metric": "Total Stock Calls", "Value": len(active_equity_trades)},
        {"Category": "Equity Wallet", "Metric": "Equity Net P&L (₹)", "Value": eq_net},
        {"Category": "Equity Wallet", "Metric": "Equity Closing Balance (₹)", "Value": round(EQUITY_VIRTUAL_CAPITAL + eq_net, 2)},
        {"Category": "Combined", "Metric": "Total Portfolio P&L (₹)", "Value": combined_net}
    ])

    try:
        with pd.ExcelWriter(filename, engine="openpyxl") as writer:
            df_fno.to_excel(writer, sheet_name="FNO_Trades", index=False)
            df_eq.to_excel(writer, sheet_name="Equity_Stock_Calls", index=False)
            df_summary.to_excel(writer, sheet_name="Combined_Summary", index=False)

        tag = "EOD Final" if is_eod else "Live Interim"
        caption = (
            f"📥 <b>FINSIDE ZING v2.8 {tag} Excel Report ({date_str})</b>\n"
            f"💼 F&O P&L: <b>₹{fno_net:,.2f}</b> | 📈 Equity P&L: <b>₹{eq_net:,.2f}</b>"
        )
        send_telegram_document(filename, caption=caption)
    except Exception as e:
        print("Excel Generation Error:", e)
        send_telegram_alert(f"⚠️ Excel Report Error: {e}")
    finally:
        if os.path.exists(filename):
            os.remove(filename)

def connect_angel():
    for attempt in range(5):
        try:
            smart_api = SmartConnect(api_key=API_KEY, timeout=30)
            totp = pyotp.TOTP(TOTP_SECRET).now()
            res = smart_api.generateSession(CLIENT_ID, PASSWORD, totp)
            if res and res.get("status"):
                return smart_api
        except Exception as e:
            print(f"Login Retry {attempt+1}/5 Error:", e)
            time.sleep(5)
    return None

def load_scrip_masters():
    global scrip_master_df, equity_master_df
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        data = requests.get(url, timeout=30).json()
        df = pd.DataFrame(data)

        opt_df = df[
            ((df['exch_seg'] == 'NFO') & (df['instrumenttype'] == 'OPTIDX')) |
            ((df['exch_seg'] == 'MCX') & (df['instrumenttype'].isin(['OPTFUT', 'FUTCOM'])))
        ].copy()
        opt_df['expiry_dt'] = pd.to_datetime(opt_df['expiry'], format='%d%b%Y', errors='coerce')
        opt_df['strike_val'] = pd.to_numeric(opt_df['strike'], errors='coerce') / 100.0
        today = pd.to_datetime(get_ist_now().strftime('%Y-%m-%d'))
        scrip_master_df = opt_df[opt_df['expiry_dt'] >= today]

        eq_df = df[(df['exch_seg'] == 'NSE') & (df['symbol'].str.endswith('-EQ'))].copy()
        equity_master_df = eq_df
    except Exception as e:
        print("Scrip Master Load Error:", e)

def get_ltp(smart_api, symbol, token, exch_seg="NFO"):
    try:
        res = smart_api.ltpData(exch_seg, symbol, token)
        if res and res.get("data"):
            return float(res["data"]["ltp"])
    except Exception as e:
        print("LTP Fetch Error:", e)
    return None

def get_equity_symbol_token(stock_name):
    if equity_master_df is None:
        return None, None
    clean_name = stock_name.upper().replace("-EQ", "").strip()
    sub = equity_master_df[equity_master_df['name'] == clean_name]
    if sub.empty:
        sub = equity_master_df[equity_master_df['symbol'] == f"{clean_name}-EQ"]
    if not sub.empty:
        return sub.iloc[0]['symbol'], sub.iloc[0]['token']
    return None, None

def get_optimal_option(smart_api, scrip_name, atm_strike, strike_step, opt_type):
    if scrip_master_df is None:
        return None, None, None

    sub_atm = scrip_master_df[
        (scrip_master_df['exch_seg'] == 'NFO') &
        (scrip_master_df['name'] == scrip_name) &
        (scrip_master_df['strike_val'] == float(atm_strike)) &
        (scrip_master_df['symbol'].str.endswith(opt_type))
    ].sort_values('expiry_dt')

    if sub_atm.empty:
        return None, None, None

    nearest_exp = sub_atm.iloc[0]['expiry_dt']
    atm_sym = sub_atm.iloc[0]['symbol']
    atm_tok = sub_atm.iloc[0]['token']
    atm_ltp = get_ltp(smart_api, atm_sym, atm_tok, "NFO")

    if not atm_ltp:
        return None, None, None

    if atm_ltp <= 500.0:
        return atm_sym, atm_tok, atm_ltp

    best_sym, best_tok, best_ltp = atm_sym, atm_tok, atm_ltp
    for step_mult in range(1, 16):
        candidate_strike = atm_strike + (step_mult * strike_step) if opt_type == "CE" else atm_strike - (step_mult * strike_step)
        sub_otm = scrip_master_df[
            (scrip_master_df['exch_seg'] == 'NFO') &
            (scrip_master_df['name'] == scrip_name) &
            (scrip_master_df['expiry_dt'] == nearest_exp) &
            (scrip_master_df['strike_val'] == float(candidate_strike)) &
            (scrip_master_df['symbol'].str.endswith(opt_type))
        ]
        if not sub_otm.empty:
            c_sym = sub_otm.iloc[0]['symbol']
            c_tok = sub_otm.iloc[0]['token']
            c_ltp = get_ltp(smart_api, c_sym, c_tok, "NFO")
            if c_ltp:
                best_sym, best_tok, best_ltp = c_sym, c_tok, c_ltp
                if 180.0 <= c_ltp <= 480.0:
                    return c_sym, c_tok, c_ltp
                if c_ltp < 180.0:
                    break

    return best_sym, best_tok, best_ltp

def get_specific_option(scrip_name, strike_price, opt_type, exch_seg="NFO"):
    if scrip_master_df is None:
        return None, None
    sub = scrip_master_df[
        (scrip_master_df['exch_seg'] == exch_seg) &
        (scrip_master_df['name'] == scrip_name) &
        (scrip_master_df['strike_val'] == float(strike_price)) &
        (scrip_master_df['symbol'].str.endswith(opt_type))
    ].sort_values('expiry_dt')
    if not sub.empty:
        row = sub.iloc[0]
        return row['symbol'], row['token']
    return None, None

def get_candle_data(smart_api, exchange, token, interval="FIVE_MINUTE", days=4):
    now_ist = get_ist_now()
    to_date = now_ist.strftime("%Y-%m-%d %H:%M")
    from_date = (now_ist - timedelta(days=days)).strftime("%Y-%m-%d 09:15")
    params = {
        "exchange": exchange,
        "symboltoken": token,
        "interval": interval,
        "fromdate": from_date,
        "todate": to_date
    }
    try:
        res = smart_api.getCandleData(params)
        if res and res.get("data"):
            return pd.DataFrame(res["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
    except Exception as e:
        print(f"Candle Fetch Error ({interval}):", e)
    return None

def calculate_supertrend(df):
    hl2 = (df['high'] + df['low']) / 2
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low'] - df['close'].shift()).abs()
    ], axis=1).max(axis=1)
    atr10 = tr.ewm(alpha=1/10, adjust=False).mean()
    upperband = (hl2 + (3 * atr10)).copy()
    lowerband = (hl2 - (3 * atr10)).copy()
    st_dir = [1] * len(df)
    
    for i in range(1, len(df)):
        if df['close'].iloc[i] > upperband.iloc[i-1]:
            st_dir[i] = 1
        elif df['close'].iloc[i] < lowerband.iloc[i-1]:
            st_dir[i] = -1
        else:
            st_dir[i] = st_dir[i-1]
            if st_dir[i] == 1 and lowerband.iloc[i] < lowerband.iloc[i-1]:
                lowerband.iloc[i] = lowerband.iloc[i-1]
            if st_dir[i] == -1 and upperband.iloc[i] > upperband.iloc[i-1]:
                upperband.iloc[i] = upperband.iloc[i-1]
    return st_dir, tr

def add_indicators(df):
    df['ema_5'] = df['close'].ewm(span=5, adjust=False).mean()
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
    
    delta = df['close'].diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    df['rsi'] = 100 - (100 / (1 + (gain / loss)))
    
    sma20 = df['close'].rolling(20).mean()
    std20 = df['close'].rolling(20).std()
    df['bb_upper'] = sma20 + (2 * std20)
    df['bb_lower'] = sma20 - (2 * std20)
    
    st_dir, tr = calculate_supertrend(df)
    df['st_dir'] = st_dir
    df['tr'] = tr
    
    up_move = df['high'].diff()
    down_move = -df['low'].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    atr14 = tr.ewm(alpha=1/14, adjust=False).mean()
    df['atr14'] = atr14
    plus_di = 100 * (pd.Series(plus_dm).ewm(alpha=1/14, adjust=False).mean() / atr14)
    minus_di = 100 * (pd.Series(minus_dm).ewm(alpha=1/14, adjust=False).mean() / atr14)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    df['adx'] = dx.ewm(alpha=1/14, adjust=False).mean()
    return df

def get_15m_trend(smart_api, exchange, token):
    now_ts = time.time()
    cached = htf_cache.get(token)
    if cached and (now_ts - cached["time"] < 300):
        return cached["trend"]

    df15 = get_candle_data(smart_api, exchange, token, interval="FIFTEEN_MINUTE", days=5)
    if df15 is not None and len(df15) > 15:
        st_dir, _ = calculate_supertrend(df15)
        trend = st_dir[-1]
        htf_cache[token] = {"trend": trend, "time": now_ts}
        return trend
    return cached["trend"] if cached else 0

def has_open_trade_for_index(idx_name):
    return any(t["status"] == "OPEN" and t["index_name"] == idx_name for t in active_trades)

def get_daily_sl_count():
    return sum(1 for t in active_trades if t["outcome"] == "SL")

def get_equity_sl_count():
    return sum(1 for t in active_equity_trades if t["outcome"] == "SL")

def get_index_realized_pnl(idx_name):
    total = 0.0
    for t in active_trades:
        if t["index_name"] == idx_name and t["status"] == "CLOSED":
            total += (t.get("exit_price", t["entry"]) - t["entry"]) * t["lot_size"]
    return round(total, 2)

def calculate_fno_pnl(smart_api=None):
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    for t in active_trades:
        qty = t["lot_size"]
        if t["status"] == "CLOSED":
            exit_px = t.get("exit_price", t["entry"])
            realized_pnl += (exit_px - t["entry"]) * qty
        elif t["status"] == "OPEN" and smart_api:
            ltp = get_ltp(smart_api, t["symbol"], t["token"], t.get("exch_seg", "NFO")) or t["entry"]
            unrealized_pnl += (ltp - t["entry"]) * qty
    return round(realized_pnl, 2), round(unrealized_pnl, 2)

def calculate_equity_pnl(smart_api=None):
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    for t in active_equity_trades:
        qty = t["qty"]
        if t["status"] == "CLOSED":
            exit_px = t.get("exit_price", t["entry"])
            realized_pnl += (exit_px - t["entry"]) * qty
        elif t["status"] == "OPEN" and smart_api:
            ltp = get_ltp(smart_api, t["symbol"], t["token"], "NSE") or t["entry"]
            unrealized_pnl += (ltp - t["entry"]) * qty
    return round(realized_pnl, 2), round(unrealized_pnl, 2)

def compute_protected_sl_and_trails(opt_ltp, lot_qty, is_vip_late_session=False):
    if is_vip_late_session:
        pct_sl_pts = opt_ltp * 0.07
        max_cap_pts = 400.0 / lot_qty
        t1_pct = 0.04
        t2_pct = 0.08
        lock_pct = 0.04
    else:
        pct_sl_pts = opt_ltp * DEFAULT_SL_PCT
        max_cap_pts = MAX_LOSS_PER_TRADE_RS / lot_qty
        t1_pct = TRAIL_STEP1_PCT
        t2_pct = TRAIL_STEP2_PCT
        lock_pct = PROFIT_LOCK_PCT

    sl_points = max(MIN_SL_POINTS, min(pct_sl_pts, max_cap_pts))
    opt_sl = round(max(1.0, opt_ltp - sl_points), 1)
    trail_step1 = round(opt_ltp * (1 + t1_pct), 1)
    trail_step2 = round(opt_ltp * (1 + t2_pct), 1)
    profit_lock_sl = round(opt_ltp * (1 + lock_pct), 1)
    return opt_sl, trail_step1, trail_step2, profit_lock_sl

def detect_broker_name(upper_txt):
    if "TRADING IDEA" in upper_txt or "STRIKE PRICE NAME" in upper_txt or "CMP -" in upper_txt:
        return "Alice Blue"
    return "Angel One"

# ================= UNIVERSAL EQUITY STOCK CALL HANDLER (₹1L EQUITY WALLET) =================
def handle_broker_equity_call(smart_api, raw_text, broker_name):
    if get_equity_sl_count() >= MAX_EQUITY_SL_PER_DAY:
        send_telegram_alert(f"🛡️ <b>{broker_name} Equity Call Ignored:</b> Daily Equity SL Limit (3/3) reached.")
        return

    upper_txt = raw_text.upper()

    sym_match = re.search(r'(?:BUY|NAME|SCRIP|STOCK)[:\s-]+([A-Z0-9&-]{2,20})', upper_txt)
    sl_match = re.search(r'(?:STOP\s*LOSS|SL)[:\s₹-]*([\d.]+)', upper_txt)
    tgt_match = re.search(r'(?:TARGET\s*PRICE|TARGET|TGT)[:\s₹-]*([\d.]+)', upper_txt)

    if not (sym_match and sl_match and tgt_match):
        send_telegram_alert(f"⚠️ <b>{broker_name} Equity Parse Failed:</b> Missing Symbol/SL/Target in:\n<code>{raw_text}</code>")
        return

    stock_raw = sym_match.group(1)
    sl_price = float(sl_match.group(1))
    tgt_price = float(tgt_match.group(1))

    eq_symbol, eq_token = get_equity_symbol_token(stock_raw)
    if not eq_symbol:
        send_telegram_alert(f"⚠️ <b>{broker_name} Equity Skipped:</b> NSE symbol not found for <b>{stock_raw}</b>.")
        return

    if any(t["status"] == "OPEN" and t["symbol"] == eq_symbol for t in active_equity_trades):
        send_telegram_alert(f"⏳ <b>{broker_name} Equity Ignored:</b> <code>{eq_symbol}</code> is already OPEN.")
        return

    htf_trend = get_15m_trend(smart_api, "NSE", eq_token)
    if htf_trend == -1:
        send_telegram_alert(
            f"🚫 <b>{broker_name} Equity Call Rejected by ZING Filter!</b>\n"
            f"• Stock: <code>{eq_symbol}</code>\n"
            f"• Reason: 15m Supertrend is <b>BEARISH 🔴</b> (Avoiding falling knife)."
        )
        return

    ltp = get_ltp(smart_api, eq_symbol, eq_token, "NSE")
    if not ltp:
        send_telegram_alert(f"⚠️ <b>{broker_name} Equity Skipped:</b> Live LTP unavailable for <code>{eq_symbol}</code>.")
        return

    if ltp <= sl_price or ltp >= tgt_price:
        send_telegram_alert(f"🚫 <b>{broker_name} Equity Skipped:</b> Live price ₹{ltp} outside SL ₹{sl_price} – Target ₹{tgt_price}.")
        return

    risk_per_share = ltp - sl_price
    qty_by_risk = int(EQUITY_MAX_RISK_RS // risk_per_share)
    qty_by_cap = int(EQUITY_MAX_EXPOSURE_RS // ltp)
    qty = max(1, min(qty_by_risk, qty_by_cap))

    halfway_price = round(ltp + ((tgt_price - ltp) * 0.5), 2)
    entry_t = get_ist_now().strftime("%H:%M")
    max_risk_actual = round(risk_per_share * qty, 1)

    msg = (
        f"📈 <b>ZING EQUITY CALL EXECUTED ({broker_name})</b>\n"
        f"✅ <b>15m Trend Filter Passed (BULLISH 🟢)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>Stock:</b> <code>{eq_symbol}</code> (Qty: <b>{qty} Shares</b>)\n"
        f"💰 <b>Entry Price:</b> ₹{ltp}\n"
        f"🎯 <b>Target Price:</b> ₹{tgt_price} (+₹{round((tgt_price-ltp)*qty, 1)})\n"
        f"🛑 <b>Stop Loss:</b> ₹{sl_price} (-₹{max_risk_actual} Max Risk)\n"
        f"🔒 <b>50% Target Cost-Trail:</b> ₹{halfway_price}\n"
        f"⏰ <b>Time:</b> {entry_t}"
    )
    msg_id = send_telegram_alert(msg)
    if msg_id:
        active_equity_trades.append({
            "broker": broker_name,
            "symbol": eq_symbol,
            "token": eq_token,
            "qty": qty,
            "entry": ltp,
            "exit_price": ltp,
            "target": tgt_price,
            "sl": sl_price,
            "halfway": halfway_price,
            "cost_trailed": False,
            "entry_time": entry_t,
            "exit_time": "-",
            "msg_id": msg_id,
            "status": "OPEN",
            "outcome": "OPEN"
        })

# ================= UNIVERSAL BROKER CALL ROUTER (ANGEL ONE + ALICE BLUE) =================
def handle_broker_notification(smart_api, raw_text):
    upper_txt = raw_text.upper()
    broker_name = detect_broker_name(upper_txt)

    if scanner_paused:
        send_telegram_alert(f"⏸️ <b>{broker_name} Call Ignored:</b> Scanner is currently Paused.")
        return

    now_ist = get_ist_now()
    if now_ist.hour >= 15:
        send_telegram_alert(f"⏰ <b>{broker_name} Call Ignored:</b> Past 3:00 PM entry cutoff.")
        return

    supported_opt = ["BANKNIFTY", "NIFTY"] + list(MCX_LOTS.keys())
    scrip = next((s for s in supported_opt if s in upper_txt), None)

    if not scrip:
        handle_broker_equity_call(smart_api, raw_text, broker_name)
        return

    if get_daily_sl_count() >= MAX_DAILY_SL:
        send_telegram_alert(f"🛡️ <b>{broker_name} F&O Call Ignored:</b> Daily F&O SL Shield (2/2) Active.")
        return

    exch_seg = "MCX" if scrip in MCX_LOTS else "NFO"
    idx_display = "BANK NIFTY" if scrip == "BANKNIFTY" else ("NIFTY 50" if scrip == "NIFTY" else scrip)

    if idx_display in locked_profit_indices:
        send_telegram_alert(f"🏆 <b>{broker_name} Call Ignored:</b> {idx_display} Daily Profit Target already locked!")
        return

    if has_open_trade_for_index(idx_display):
        send_telegram_alert(f"⏳ <b>{broker_name} Call Ignored:</b> {idx_display} already has an OPEN trade.")
        return

    opt_match = re.search(r'\b(CE|PE)\s*(\d{4,6})', upper_txt)
    if not opt_match:
        opt_match_rev = re.search(r'(\d{4,6})\s*(CE|PE)\b', upper_txt)
        if not opt_match_rev:
            send_telegram_alert(f"⚠️ <b>{broker_name} Option Parse Failed:</b> Strike/Option type not found in:\n<code>{raw_text}</code>")
            return
        strike_val = float(opt_match_rev.group(1))
        opt_type = opt_match_rev.group(2)
    else:
        opt_type = opt_match.group(1)
        strike_val = float(opt_match.group(2))

    if exch_seg == "NFO":
        item = next((w for w in WATCHLIST if w["scrip_name"] == scrip), None)
        htf_trend = get_15m_trend(smart_api, item["exchange"], item["token"])
        df5 = get_candle_data(smart_api, item["exchange"], item["token"])
        adx_val = 0.0
        if df5 is not None and len(df5) > 20:
            df5 = add_indicators(df5)
            adx_val = round(df5.iloc[-2]['adx'], 1)
        expected_trend = 1 if opt_type == "CE" else -1
        if htf_trend != expected_trend or adx_val < 20:
            trend_str = "BULLISH 🟢" if htf_trend == 1 else ("BEARISH 🔴" if htf_trend == -1 else "NEUTRAL")
            send_telegram_alert(
                f"🚫 <b>{broker_name} Option Call Rejected by ZING Filter!</b>\n"
                f"• Call: <b>{scrip} {int(strike_val)} {opt_type}</b>\n"
                f"• Reason: 15m Trend is <b>{trend_str}</b> & ADX is <b>{adx_val}</b>"
            )
            return
        lot_qty = item["lot_size"]
    else:
        lot_qty = MCX_LOTS[scrip]

    opt_symbol, opt_token = get_specific_option(scrip, strike_val, opt_type, exch_seg=exch_seg)
    opt_ltp = get_ltp(smart_api, opt_symbol, opt_token, exch_seg=exch_seg) if opt_symbol else None
    if not opt_ltp:
        send_telegram_alert(f"⚠️ <b>{broker_name} Call Skipped:</b> Live LTP unavailable for {scrip} {int(strike_val)} {opt_type}.")
        return

    angel_price = re.search(r'RECOMMENDED PRICE[:\s₹]*([\d.]+)\s*[-–TO]+\s*([\d.]+)', upper_txt)
    alice_entry = re.search(r'\bENTRY[:\s₹-]+([\d.]+)', upper_txt)

    if angel_price:
        rec_high = float(angel_price.group(2))
        if opt_ltp > (rec_high * 1.10):
            send_telegram_alert(
                f"🚫 <b>{broker_name} Call Rejected (Price Chased):</b>\n"
                f"• <code>{opt_symbol}</code> Live ₹{opt_ltp} > Recommended ₹{rec_high}."
            )
            return
    elif alice_entry:
        rec_entry = float(alice_entry.group(1))
        if opt_ltp > (rec_entry * 1.10):
            send_telegram_alert(
                f"🚫 <b>{broker_name} Call Rejected (Price Chased):</b>\n"
                f"• <code>{opt_symbol}</code> Live ₹{opt_ltp} > Entry ₹{rec_entry}."
            )
            return

    opt_target = round(opt_ltp * (1 + TARGET_PCT), 1)
    opt_sl, t_step1, t_step2, p_lock_sl = compute_protected_sl_and_trails(opt_ltp, lot_qty)
    entry_t = now_ist.strftime("%H:%M")
    icon = "🟢" if opt_type == "CE" else "🔴"

    msg = (
        f"{icon} <b>ZING v2.8 — {broker_name.upper()} CALL CONFIRMED</b>\n"
        f"✅ <b>ZING Trend & Risk Filter Passed</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Source:</b> {broker_name} Call ({exch_seg})\n"
        f"⚡ <b>Buy Option:</b> <code>{opt_symbol}</code> (1 Lot = {lot_qty})\n"
        f"💰 <b>Entry Premium:</b> ₹{opt_ltp}\n"
        f"🎯 <b>Target (+15%):</b> ₹{opt_target}\n"
        f"🛑 <b>Protected SL (Max ₹550):</b> ₹{opt_sl}\n"
        f"🔒 <b>Trail Step 1 (+6% Cost):</b> ₹{t_step1}\n"
        f"💰 <b>Trail Step 2 (+10% → +5% Lock):</b> ₹{t_step2} (SL → ₹{p_lock_sl})\n"
        f"⏰ <b>Time:</b> {entry_t}"
    )
    msg_id = send_telegram_alert(msg)
    if msg_id:
        active_trades.append({
            "index_name": idx_display,
            "exch_seg": exch_seg,
            "strategy": f"{broker_name} Call ({exch_seg})",
            "lot_size": lot_qty,
            "symbol": opt_symbol,
            "token": opt_token,
            "entry": opt_ltp,
            "exit_price": opt_ltp,
            "entry_time": entry_t,
            "exit_time": "-",
            "target": opt_target,
            "sl": opt_sl,
            "trail_step1": t_step1,
            "trail_step2": t_step2,
            "profit_lock_sl": p_lock_sl,
            "trail_stage": 0,
            "msg_id": msg_id,
            "status": "OPEN",
            "outcome": "OPEN"
        })

# ================= INTERACTIVE TELEGRAM COMMAND HANDLER =================
def check_telegram_commands(smart_api):
    global last_update_id, scanner_paused
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 1}
    try:
        res = requests.get(url, params=params, timeout=5).json()
        if not res.get("ok"):
            return

        for update in res.get("result", []):
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            raw_text = msg.get("text", "").strip()
            text = raw_text.lower()

            if chat_id != str(TELEGRAM_CHAT_ID) or not text:
                continue

            if text.startswith("/angel"):
                handle_broker_notification(smart_api, raw_text)

            elif text.startswith("/status") or "status" in text:
                lines = ["📊 <b>LIVE MARKET STATUS (v2.8 Universal Dual-Wallet)</b>\n━━━━━━━━━━━━━━━━━━━━"]
                for item in WATCHLIST:
                    df = get_candle_data(smart_api, item["exchange"], item["token"])
                    if df is not None and len(df) > 20:
                        df = add_indicators(df)
                        curr = df.iloc[-1]
                        htf = get_15m_trend(smart_api, item["exchange"], item["token"])
                        trend_txt = "BULLISH 🟢" if htf == 1 else ("BEARISH 🔴" if htf == -1 else "NEUTRAL")
                        lock_tag = " [🏆 PROFIT LOCKED]" if item["name"] in locked_profit_indices else ""
                        lines.append(
                            f"🔹 <b>{item['name']}{lock_tag}:</b> {curr['close']}\n"
                            f"   15m Trend: {trend_txt} | ADX: {round(curr['adx'], 1)}"
                        )
                state = "⏸️ PAUSED" if scanner_paused else "▶️ ACTIVE"
                lines.append(f"\n🤖 <b>Scanner State:</b> {state}")
                send_telegram_alert("\n".join(lines))

            elif text.startswith("/open") or "open" in text:
                open_fno = [t for t in active_trades if t["status"] == "OPEN"]
                open_eq = [t for t in active_equity_trades if t["status"] == "OPEN"]
                if not open_fno and not open_eq:
                    send_telegram_alert("📭 <b>No OPEN trades in F&O or Equity right now.</b>")
                else:
                    lines = ["⚡ <b>ACTIVE RUNNING TRADES (Dual Wallet)</b>\n━━━━━━━━━━━━━━━━━━━━"]
                    if open_fno:
                        lines.append("<b>🔹 F&O Wallet Positions:</b>")
                        for t in open_fno:
                            ltp = get_ltp(smart_api, t["symbol"], t["token"], t.get("exch_seg", "NFO")) or t["entry"]
                            diff = round(ltp - t["entry"], 1)
                            pnl_rs = round(diff * t["lot_size"], 1)
                            sign = "+" if diff >= 0 else ""
                            lines.append(f"• <code>{t['symbol']}</code> | Entry: ₹{t['entry']} | Live: ₹{ltp} (<b>{sign}₹{pnl_rs}</b>)")
                    if open_eq:
                        lines.append("\n<b>📈 Equity Stock Positions:</b>")
                        for t in open_eq:
                            ltp = get_ltp(smart_api, t["symbol"], t["token"], "NSE") or t["entry"]
                            diff = round(ltp - t["entry"], 2)
                            pnl_rs = round(diff * t["qty"], 1)
                            sign = "+" if diff >= 0 else ""
                            lines.append(f"• [{t.get('broker', 'EQ')}] <code>{t['symbol']}</code> ({t['qty']} sh) | Entry: ₹{t['entry']} | Live: ₹{ltp} (<b>{sign}₹{pnl_rs}</b>)")
                    send_telegram_alert("\n".join(lines))

            elif text.startswith("/virtual") or "virtual" in text:
                realized, unrealized = calculate_fno_pnl(smart_api)
                net_pnl = round(realized + unrealized, 2)
                curr_bal = round(FNO_VIRTUAL_CAPITAL + net_pnl, 2)
                sign = "+" if net_pnl >= 0 else ""
                send_telegram_alert(
                    f"💼 <b>F&O PAPER TRADING WALLET (1 Lot)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏦 <b>Starting Capital:</b> ₹{FNO_VIRTUAL_CAPITAL:,.2f}\n"
                    f"✅ <b>Closed Trades P&L:</b> ₹{realized:,.2f}\n"
                    f"⏳ <b>Running Trades P&L:</b> ₹{unrealized:,.2f}\n"
                    f"📈 <b>Net F&O P&L:</b> <b>{sign}₹{net_pnl:,.2f}</b>\n"
                    f"💰 <b>Current Balance:</b> <b>₹{curr_bal:,.2f}</b>"
                )

            elif text.startswith("/equity") or "equity" in text:
                realized, unrealized = calculate_equity_pnl(smart_api)
                net_pnl = round(realized + unrealized, 2)
                curr_bal = round(EQUITY_VIRTUAL_CAPITAL + net_pnl, 2)
                sign = "+" if net_pnl >= 0 else ""
                total_eq = len(active_equity_trades)
                wins_eq = sum(1 for t in active_equity_trades if t["outcome"] == "TARGET")
                sls_eq = sum(1 for t in active_equity_trades if t["outcome"] == "SL")
                send_telegram_alert(
                    f"📈 <b>EQUITY STOCK CALLS WALLET (Angel One + Alice Blue)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏦 <b>Starting Capital:</b> ₹{EQUITY_VIRTUAL_CAPITAL:,.2f}\n"
                    f"📌 <b>Total Calls Taken:</b> {total_eq} (🎯 Targets: {wins_eq} | 🛑 SL: {sls_eq}/{MAX_EQUITY_SL_PER_DAY})\n"
                    f"✅ <b>Closed Calls P&L:</b> ₹{realized:,.2f}\n"
                    f"⏳ <b>Running Calls P&L:</b> ₹{unrealized:,.2f}\n"
                    f"📈 <b>Net Equity P&L:</b> <b>{sign}₹{net_pnl:,.2f}</b>\n"
                    f"💰 <b>Current Balance:</b> <b>₹{curr_bal:,.2f}</b>"
                )

            elif text.startswith("/excel") or "excel" in text:
                send_telegram_alert("⏳ Generating 3-Sheet Dual-Wallet Excel report...")
                generate_and_send_excel(smart_api=smart_api, is_eod=False)

            elif text.startswith("/closeall") or "closeall" in text:
                open_fno = [t for t in active_trades if t["status"] == "OPEN"]
                open_eq = [t for t in active_equity_trades if t["status"] == "OPEN"]
                if not open_fno and not open_eq:
                    send_telegram_alert("📭 <b>No open trades to close.</b>")
                else:
                    now_str = get_ist_now().strftime("%H:%M")
                    closed_lines = ["🛑 <b>MANUAL EXIT EXECUTED (/closeall)</b>\n━━━━━━━━━━━━━━━━━━━━"]
                    for t in open_fno:
                        ltp = get_ltp(smart_api, t["symbol"], t["token"], t.get("exch_seg", "NFO")) or t["entry"]
                        t["status"] = "CLOSED"
                        t["outcome"] = "MANUAL_EXIT"
                        t["exit_price"] = ltp
                        t["exit_time"] = now_str
                        pnl_rs = round((ltp - t["entry"]) * t["lot_size"], 1)
                        closed_lines.append(f"• [F&O] <code>{t['symbol']}</code> @ ₹{ltp} ({'+' if pnl_rs>=0 else ''}₹{pnl_rs})")
                    for t in open_eq:
                        ltp = get_ltp(smart_api, t["symbol"], t["token"], "NSE") or t["entry"]
                        t["status"] = "CLOSED"
                        t["outcome"] = "MANUAL_EXIT"
                        t["exit_price"] = ltp
                        t["exit_time"] = now_str
                        pnl_rs = round((ltp - t["entry"]) * t["qty"], 1)
                        closed_lines.append(f"• [EQ] <code>{t['symbol']}</code> @ ₹{ltp} ({'+' if pnl_rs>=0 else ''}₹{pnl_rs})")
                    send_telegram_alert("\n".join(closed_lines))

            elif text.startswith("/pnl") or "pnl" in text:
                fno_r, fno_u = calculate_fno_pnl(smart_api)
                fno_net = round(fno_r + fno_u, 2)
                eq_r, eq_u = calculate_equity_pnl(smart_api)
                eq_net = round(eq_r + eq_u, 2)
                tot_net = round(fno_net + eq_net, 2)

                targets = sum(1 for t in active_trades if t["outcome"] == "TARGET")
                p_locks = sum(1 for t in active_trades if t["outcome"] == "PROFIT_LOCK_EXIT")
                c_exits = sum(1 for t in active_trades if t["outcome"] == "COST_EXIT")
                sls = sum(1 for t in active_trades if t["outcome"] == "SL")

                send_telegram_alert(
                    f"🏆 <b>TODAY'S COMBINED PERFORMANCE (v2.8)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💼 <b>F&O Wallet P&L:</b> {'+' if fno_net>=0 else ''}₹{fno_net:,.2f}\n"
                    f"   (Signals: {len(active_trades)} | 🎯 {targets} | 💰 {p_locks} | 🛡️ {c_exits} | 🛑 {sls})\n"
                    f"📈 <b>Equity Stock Calls P&L:</b> {'+' if eq_net>=0 else ''}₹{eq_net:,.2f}\n"
                    f"   (Calls: {len(active_equity_trades)} | 🎯 {sum(1 for t in active_equity_trades if t['outcome']=='TARGET')} | 🛑 {get_equity_sl_count()})\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🌟 <b>TOTAL COMBINED P&L:</b> <b>{'+' if tot_net>=0 else ''}₹{tot_net:,.2f}</b>"
                )

            elif text.startswith("/pause") or "pause" in text:
                scanner_paused = True
                send_telegram_alert("⏸️ <b>Scanner Paused!</b>")

            elif text.startswith("/resume") or "resume" in text:
                scanner_paused = False
                send_telegram_alert("▶️ <b>Scanner Resumed!</b>")

            else:
                send_telegram_alert(
                    "🤖 <b>FINSIDE ZING v2.8 Universal Commands:</b>\n"
                    "• /virtual — ₹1,00,000 F&O Wallet P&L\n"
                    "• /equity — ₹1,00,000 Equity Stock Calls Wallet\n"
                    "• /pnl — Combined F&O + Equity Summary\n"
                    "• /open — Live Running F&O + Stock Positions\n"
                    "• /status — Live NIFTY & BANKNIFTY Trend/ADX\n"
                    "• /excel — Download 3-Sheet Excel Report\n"
                    "• /closeall — Close All Open Trades"
                )
    except Exception as e:
        print("Command Check Error:", e)
# =========================================================================

def evaluate_all_strategies(smart_api, df, item):
    global daily_sl_limit_notified
    if scanner_paused:
        return

    now_ist = get_ist_now()
    if now_ist.hour >= 15:
        return

    is_vip_late_session = (now_ist.hour == 14 and now_ist.minute >= 15)
    if is_vip_late_session:
        realized_day, _ = calculate_fno_pnl()
        if realized_day <= 0:
            return

    idx_name = item['name']

    idx_pnl = get_index_realized_pnl(idx_name)
    if idx_pnl >= INDEX_PROFIT_LOCK_RS:
        if idx_name not in locked_profit_indices:
            locked_profit_indices.add(idx_name)
            send_telegram_alert(
                f"🏆 <b>PROFIT LOCKED FOR {idx_name}!</b>\n"
                f"{idx_name} reached <b>+₹{idx_pnl:,.2f}</b> today.\n"
                f"🔒 <i>{idx_name} locked for the rest of the day!</i>"
            )
        return

    if get_daily_sl_count() >= MAX_DAILY_SL:
        if not daily_sl_limit_notified:
            daily_sl_limit_notified = True
            send_telegram_alert(
                f"🛡️ <b>F&O CAPITAL SHIELD ACTIVATED!</b>\n"
                f"Daily F&O Max Loss Limit ({MAX_DAILY_SL} SL) reached. Equity Stock Calls remain active!"
            )
        return

    if has_open_trade_for_index(idx_name):
        return

    df = add_indicators(df)
    curr = df.iloc[-2]
    prev = df.iloc[-3]
    mother = df.iloc[-4]
    
    candle_size = curr['high'] - curr['low']
    if curr['atr14'] > 0 and candle_size > (2.2 * curr['atr14']):
        return

    spot = curr['close']
    adx_val = round(curr['adx'], 1)
    if is_vip_late_session and adx_val <= 28:
        return

    atm_strike = round(spot / item['strike_step']) * item['strike_step']
    htf_trend = get_15m_trend(smart_api, item['exchange'], item['token'])
    htf_label = "BULLISH 🟢" if htf_trend == 1 else ("BEARISH 🔴" if htf_trend == -1 else "NEUTRAL")
    
    ce_strategies = []
    pe_strategies = []

    if adx_val > 22:
        if curr['st_dir'] == 1 and prev['st_dir'] == -1 and spot > curr['ema_9'] and htf_trend == 1:
            ce_strategies.append("Booming Bulls Supertrend")
        elif curr['st_dir'] == -1 and prev['st_dir'] == 1 and spot < curr['ema_9'] and htf_trend == -1:
            pe_strategies.append("Booming Bulls Supertrend")

        if curr['ema_5'] > curr['ema_9'] and prev['ema_5'] <= prev['ema_9'] and htf_trend == 1:
            ce_strategies.append("Prime Scalper EMA")
        elif curr['ema_5'] < curr['ema_9'] and prev['ema_5'] >= prev['ema_9'] and htf_trend == -1:
            pe_strategies.append("Prime Scalper EMA")

        if curr['ema_9'] > curr['ema_21'] and prev['ema_9'] <= prev['ema_21'] and htf_trend == 1:
            ce_strategies.append("EMA Cross (9/21)")
        elif curr['ema_9'] < curr['ema_21'] and prev['ema_9'] >= prev['ema_21'] and htf_trend == -1:
            pe_strategies.append("EMA Cross (9/21)")

        if (prev['high'] < mother['high']) and (prev['low'] > mother['low']):
            if curr['close'] > mother['high'] and htf_trend == 1:
                ce_strategies.append("Inside Candle Breakout")
            elif curr['close'] < mother['low'] and htf_trend == -1:
                pe_strategies.append("Inside Candle Breakout")

    if adx_val < 25 and not is_vip_late_session:
        if prev['low'] <= prev['bb_lower'] and curr['close'] > prev['bb_lower'] and curr['rsi'] < 35:
            ce_strategies.append("Mean Reversion Bollinger")
        elif prev['high'] >= prev['bb_upper'] and curr['close'] < prev['bb_upper'] and curr['rsi'] > 65:
            pe_strategies.append("Mean Reversion Bollinger")

    chosen_type = None
    matched_list = []
    if len(ce_strategies) > 0 and len(pe_strategies) == 0:
        chosen_type = "CE"
        matched_list = ce_strategies
    elif len(pe_strategies) > 0 and len(ce_strategies) == 0:
        chosen_type = "PE"
        matched_list = pe_strategies

    if not chosen_type:
        return

    if adx_val > MAX_ADX_EXHAUSTION and len(matched_list) < 2:
        return

    if is_vip_late_session and len(matched_list) < 2:
        return

    alert_id = f"{idx_name}_{chosen_type}_{curr['timestamp']}"
    if alert_id in sent_alerts:
        return

    sent_alerts.add(alert_id)

    opt_symbol, opt_token, opt_ltp = get_optimal_option(
        smart_api, item['scrip_name'], atm_strike, item['strike_step'], chosen_type
    )
    icon = "🟢" if chosen_type == "CE" else "🔴"

    if opt_ltp:
        lot_qty = item['lot_size']
        opt_target = round(opt_ltp * (1 + TARGET_PCT), 1)
        opt_sl, t_step1, t_step2, p_lock_sl = compute_protected_sl_and_trails(
            opt_ltp, lot_qty, is_vip_late_session=is_vip_late_session
        )

        count = len(matched_list)
        vip_tag = " [🌟 VIP LATE SESSION]" if is_vip_late_session else ""
        conviction = f"🔥 <b>HIGH CONVICTION ({count}/5 Confluence){vip_tag}</b>" if count >= 2 else "⚡ <b>STANDARD SIGNAL (1/5)</b>"
        strat_text = " + ".join(matched_list)
        entry_t = curr['timestamp'][11:16]
        cap_lbl = "₹400 VIP Cap" if is_vip_late_session else "₹550 Max Cap"

        msg = (
            f"{icon} <b>ZING INSTITUTIONAL v2.8</b>\n"
            f"{conviction}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Strategy:</b> {strat_text}\n"
            f"📊 <b>Index:</b> {idx_name} (Spot: {spot})\n"
            f"🧭 <b>15m Trend:</b> {htf_label} | <b>ADX:</b> {adx_val}\n"
            f"⚡ <b>Buy Option:</b> <code>{opt_symbol}</code> (1 Lot = {lot_qty})\n"
            f"💰 <b>Entry Premium:</b> ₹{opt_ltp}\n"
            f"🎯 <b>Target (+15%):</b> ₹{opt_target}\n"
            f"🛑 <b>Protected SL ({cap_lbl}):</b> ₹{opt_sl}\n"
            f"🔒 <b>Trail Step 1 (+6% Cost):</b> ₹{t_step1}\n"
            f"💰 <b>Trail Step 2 (+10% → +5% Lock):</b> ₹{t_step2} (SL → ₹{p_lock_sl})\n"
            f"⏰ <b>Time:</b> {entry_t}"
        )
        msg_id = send_telegram_alert(msg)
        if msg_id:
            active_trades.append({
                "index_name": idx_name,
                "exch_seg": "NFO",
                "strategy": strat_text,
                "lot_size": lot_qty,
                "symbol": opt_symbol,
                "token": opt_token,
                "entry": opt_ltp,
                "exit_price": opt_ltp,
                "entry_time": entry_t,
                "exit_time": "-",
                "target": opt_target,
                "sl": opt_sl,
                "trail_step1": t_step1,
                "trail_step2": t_step2,
                "profit_lock_sl": p_lock_sl,
                "trail_stage": 0,
                "msg_id": msg_id,
                "status": "OPEN",
                "outcome": "OPEN"
            })

def auto_squareoff_eod(smart_api):
    global eod_squareoff_done
    if eod_squareoff_done:
        return
    eod_squareoff_done = True
    open_fno = [t for t in active_trades if t["status"] == "OPEN"]
    open_eq = [t for t in active_equity_trades if t["status"] == "OPEN"]
    if not open_fno and not open_eq:
        return

    now_str = get_ist_now().strftime("%H:%M")
    lines = ["⏰ <b>3:25 PM AUTO EOD SQUARE-OFF</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for t in open_fno:
        ltp = get_ltp(smart_api, t["symbol"], t["token"], t.get("exch_seg", "NFO")) or t["entry"]
        t["status"] = "CLOSED"
        t["outcome"] = "EOD_EXIT"
        t["exit_price"] = ltp
        t["exit_time"] = now_str
        pnl_rs = round((ltp - t["entry"]) * t["lot_size"], 1)
        lines.append(f"• [F&O] <code>{t['symbol']}</code> @ ₹{ltp} ({'+' if pnl_rs>=0 else ''}₹{pnl_rs})")
    for t in open_eq:
        ltp = get_ltp(smart_api, t["symbol"], t["token"], "NSE") or t["entry"]
        t["status"] = "CLOSED"
        t["outcome"] = "EOD_EXIT"
        t["exit_price"] = ltp
        t["exit_time"] = now_str
        pnl_rs = round((ltp - t["entry"]) * t["qty"], 1)
        lines.append(f"• [EQ] <code>{t['symbol']}</code> @ ₹{ltp} ({'+' if pnl_rs>=0 else ''}₹{pnl_rs})")
    send_telegram_alert("\n".join(lines))

def monitor_active_trades(smart_api):
    now_str = get_ist_now().strftime("%H:%M")
    for trade in active_trades:
        if trade["status"] != "OPEN":
            continue
        ltp = get_ltp(smart_api, trade["symbol"], trade["token"], trade.get("exch_seg", "NFO"))
        if not ltp:
            continue

        qty = trade["lot_size"]
        stage = trade.get("trail_stage", 0)

        if ltp >= trade["target"]:
            trade["status"] = "CLOSED"
            trade["outcome"] = "TARGET"
            trade["exit_price"] = ltp
            trade["exit_time"] = now_str
            profit_pts = round(ltp - trade["entry"], 1)
            profit_rs = round(profit_pts * qty, 1)
            send_telegram_alert(
                f"🎯 <b>F&O TARGET HIT! (+₹{profit_pts} | +₹{profit_rs})</b>\n<code>{trade['symbol']}</code> reached ₹{ltp}\n🔓 <i>{trade['index_name']} unlocked.</i>",
                reply_to_msg_id=trade["msg_id"]
            )
            continue

        if stage < 2 and ltp >= trade["trail_step2"]:
            trade["trail_stage"] = 2
            trade["sl"] = trade["profit_lock_sl"]
            locked_rs = round((trade["profit_lock_sl"] - trade["entry"]) * qty, 1)
            send_telegram_alert(
                f"💰 <b>PROFIT LOCKED (+5% → SL @ ₹{trade['profit_lock_sl']})</b>\n"
                f"<code>{trade['symbol']}</code> crossed ₹{ltp} (+10%)! Minimum <b>+₹{locked_rs}</b> profit guaranteed.",
                reply_to_msg_id=trade["msg_id"]
            )
            continue

        if stage == 0 and ltp >= trade["trail_step1"]:
            trade["trail_stage"] = 1
            trade["sl"] = trade["entry"]
            send_telegram_alert(
                f"🔒 <b>SL TRAILED TO COST (₹{trade['entry']})</b>\n"
                f"<code>{trade['symbol']}</code> reached ₹{ltp} (+6%). Risk-Free Trade!",
                reply_to_msg_id=trade["msg_id"]
            )
            continue

        if ltp <= trade["sl"]:
            trade["status"] = "CLOSED"
            trade["exit_price"] = ltp
            trade["exit_time"] = now_str
            if stage == 2:
                trade["outcome"] = "PROFIT_LOCK_EXIT"
                profit_pts = round(ltp - trade["entry"], 1)
                profit_rs = round(profit_pts * qty, 1)
                send_telegram_alert(
                    f"💰 <b>PROFIT-LOCK EXIT (+₹{profit_pts} | +₹{profit_rs})</b>\n"
                    f"<code>{trade['symbol']}</code> booked locked profit @ ₹{ltp}\n🔓 <i>{trade['index_name']} unlocked.</i>",
                    reply_to_msg_id=trade["msg_id"]
                )
            elif stage == 1:
                trade["outcome"] = "COST_EXIT"
                send_telegram_alert(
                    f"🛡️ <b>COST-TO-COST EXIT (₹0 Loss)</b>\n"
                    f"<code>{trade['symbol']}</code> returned to Entry ₹{trade['entry']}\n🔓 <i>{trade['index_name']} unlocked.</i>",
                    reply_to_msg_id=trade["msg_id"]
                )
            else:
                trade["outcome"] = "SL"
                loss_pts = round(trade["entry"] - ltp, 1)
                loss_rs = round(loss_pts * qty, 1)
                sl_used = get_daily_sl_count()
                send_telegram_alert(
                    f"🛑 <b>F&O STOP LOSS HIT (-₹{loss_pts} | -₹{loss_rs})</b> [{sl_used}/{MAX_DAILY_SL} Daily SL]\n"
                    f"<code>{trade['symbol']}</code> touched ₹{ltp}\n🔓 <i>{trade['index_name']} unlocked.</i>",
                    reply_to_msg_id=trade["msg_id"]
                )

    for eq in active_equity_trades:
        if eq["status"] != "OPEN":
            continue
        ltp = get_ltp(smart_api, eq["symbol"], eq["token"], "NSE")
        if not ltp:
            continue

        qty = eq["qty"]
        if ltp >= eq["target"]:
            eq["status"] = "CLOSED"
            eq["outcome"] = "TARGET"
            eq["exit_price"] = ltp
            eq["exit_time"] = now_str
            pnl_rs = round((ltp - eq["entry"]) * qty, 1)
            send_telegram_alert(
                f"🎯 <b>EQUITY TARGET HIT! (+₹{pnl_rs})</b>\n<code>{eq['symbol']}</code> ({qty} sh) reached ₹{ltp}!",
                reply_to_msg_id=eq["msg_id"]
            )
            continue

        if not eq["cost_trailed"] and ltp >= eq["halfway"]:
            eq["cost_trailed"] = True
            eq["sl"] = eq["entry"]
            send_telegram_alert(
                f"🔒 <b>EQUITY SL TRAILED TO COST (₹{eq['entry']})</b>\n"
                f"<code>{eq['symbol']}</code> reached 50% Target (₹{ltp}). Now Risk-Free!",
                reply_to_msg_id=eq["msg_id"]
            )
            continue

        if ltp <= eq["sl"]:
            eq["status"] = "CLOSED"
            eq["exit_price"] = ltp
            eq["exit_time"] = now_str
            if eq["cost_trailed"]:
                eq["outcome"] = "COST_EXIT"
                send_telegram_alert(
                    f"🛡️ <b>EQUITY COST-TO-COST EXIT (₹0 Loss)</b>\n<code>{eq['symbol']}</code> returned to Entry ₹{eq['entry']}.",
                    reply_to_msg_id=eq["msg_id"]
                )
            else:
                eq["outcome"] = "SL"
                loss_rs = round((eq["entry"] - ltp) * qty, 1)
                send_telegram_alert(
                    f"🛑 <b>EQUITY STOP LOSS HIT (-₹{loss_rs})</b>\n<code>{eq['symbol']}</code> touched SL ₹{ltp}.",
                    reply_to_msg_id=eq["msg_id"]
                )

def send_eod_report(smart_api=None):
    fno_r, fno_u = calculate_fno_pnl(smart_api)
    fno_net = round(fno_r + fno_u, 2)
    eq_r, eq_u = calculate_equity_pnl(smart_api)
    eq_net = round(eq_r + eq_u, 2)
    tot_net = round(fno_net + eq_net, 2)

    report = (
        f"📊 <b>FINSIDE ZING v2.8 - DUAL-WALLET EOD REPORT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>F&O Wallet (₹1L):</b> <b>{'+' if fno_net>=0 else ''}₹{fno_net:,.2f}</b> (Balance: ₹{FNO_VIRTUAL_CAPITAL + fno_net:,.2f})\n"
        f"   • Signals: {len(active_trades)} | 🎯 {sum(1 for t in active_trades if t['outcome']=='TARGET')} | 💰 {sum(1 for t in active_trades if t['outcome']=='PROFIT_LOCK_EXIT')} | 🛡️ {sum(1 for t in active_trades if t['outcome']=='COST_EXIT')} | 🛑 {sum(1 for t in active_trades if t['outcome']=='SL')}\n\n"
        f"📈 <b>Equity Wallet (₹1L):</b> <b>{'+' if eq_net>=0 else ''}₹{eq_net:,.2f}</b> (Balance: ₹{EQUITY_VIRTUAL_CAPITAL + eq_net:,.2f})\n"
        f"   • Stock Calls: {len(active_equity_trades)} | 🎯 {sum(1 for t in active_equity_trades if t['outcome']=='TARGET')} | 🛑 {get_equity_sl_count()}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌟 <b>COMBINED DAY P&L:</b> <b>{'+' if tot_net>=0 else ''}₹{tot_net:,.2f}</b>\n"
        f"🛑 <i>Scanner Stopped for the Day.</i>"
    )
    send_telegram_alert(report)
    generate_and_send_excel(smart_api=smart_api, is_eod=True)

def run_scanner():
    now_ist = get_ist_now()
    if now_ist.weekday() >= 5:
        print("Weekend - Market Closed.")
        return

    smart_api = connect_angel()
    if not smart_api:
        send_telegram_alert("⚠️ <b>Failed to connect Angel One API after 5 retries.</b>")
        return

    setup_telegram_menu()
    load_scrip_masters()
    send_telegram_alert(
        "🚀 <b>FINSIDE ZING Institutional v2.8 (Universal Dual-Wallet) Active!</b>\n"
        "📲 <b>Brokers Connected:</b> Angel One + Alice Blue (ANT) Auto-Parser\n"
        "💼 <b>F&O Wallet:</b> ₹1,00,000 (+6% Cost Trail → +10% locks +5% Profit)\n"
        "📈 <b>Equity Wallet:</b> ₹1,00,000 (Stock Calls @ ₹500 Max Risk)"
    )
    
    while True:
        now_ist = get_ist_now()
        if now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 30):
            send_eod_report(smart_api)
            break

        check_telegram_commands(smart_api)

        if (now_ist.hour == 9 and now_ist.minute >= 15) or (10 <= now_ist.hour <= 15):
            try:
                if now_ist.hour == 15 and now_ist.minute >= 25:
                    auto_squareoff_eod(smart_api)
                else:
                    monitor_active_trades(smart_api)
                    for item in WATCHLIST:
                        df = get_candle_data(smart_api, item["exchange"], item["token"])
                        if df is not None and len(df) > 25:
                            evaluate_all_strategies(smart_api, df, item)
            except Exception as e:
                print("Loop Error (Recovering):", e)
                time.sleep(10)
                new_api = connect_angel()
                if new_api:
                    smart_api = new_api
                
        time.sleep(10)

if __name__ == "__main__":
    run_scanner()
