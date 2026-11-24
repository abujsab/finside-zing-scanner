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
API_KEY = os.environ.get("4caGhDI1", "").strip()
CLIENT_ID = os.environ.get("J50052073", "").strip()
PASSWORD = os.environ.get("5511", "").strip()
TOTP_SECRET = os.environ.get("TRTNBDOK4A3SQU454GWFPRRWDQ", "").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("7951073749:AAEJbfQ5kLxfASJEwhWhif-nyUePTxLGsAI", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("7770756739", "").strip()

WATCHLIST = [
    {"name": "NIFTY 50", "scrip_name": "NIFTY", "exchange": "NSE", "token": "99926000", "strike_step": 50, "lot_size": 75},
    {"name": "BANK NIFTY", "scrip_name": "BANKNIFTY", "exchange": "NSE", "token": "99926009", "strike_step": 100, "lot_size": 30}
]

# ================= 2. v2.5 RISK & PROFIT PROTECTION RULES =================
VIRTUAL_CAPITAL = 100000.0      # ₹1,00,000 Paper Trading Wallet
TARGET_PCT = 0.15               # Option Premium-ல் 15% Target
DEFAULT_SL_PCT = 0.10           # Normal 10% SL
MAX_LOSS_PER_TRADE_RS = 550.0   # ஒரு டிரேடிற்கு அதிகபட்ச நஷ்டம் ₹550 (Slippage & Spike Shield)
MIN_SL_POINTS = 6.0             # குறைந்தபட்சம் ₹6 SL வித்தியாசம்
TRAIL_TRIGGER_PCT = 0.06        # +6% லாபம் வந்தாலே SL-ஐ Entry விலைக்கு நகர்த்தும் (Early Risk-Free)
MAX_DAILY_SL = 2                # ஒரு நாளைக்கு அதிகபட்சம் 2 Stop-Loss (Capital Shield)
INDEX_PROFIT_LOCK_RS = 1500.0   # ஒரு Index-ல் +₹1,500 லாபம் வந்தால் அந்த Index அன்றைக்கு Lock ஆகும்
# ========================================================================

IST = pytz.timezone('Asia/Kolkata')
sent_alerts = set()
active_trades = []
scrip_master_df = None
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
        {"command": "open", "description": "⚡ Active Running Trades & Live P&L"},
        {"command": "virtual", "description": "💼 ₹1,00,000 Paper Trading Wallet P&L"},
        {"command": "pnl", "description": "🏆 Today's Signal & Win Rate Summary"},
        {"command": "excel", "description": "📥 Download Live Excel Report Now"},
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
    filename = f"FINSIDE_ZING_Report_{date_str}.xlsx"

    rows = []
    for idx, t in enumerate(active_trades, start=1):
        qty = t["lot_size"]
        if t["status"] == "CLOSED":
            exit_px = t.get("exit_price", t["entry"])
            exit_time = t.get("exit_time", "-")
        else:
            exit_px = get_option_ltp(smart_api, t["symbol"], t["token"]) if smart_api else t["entry"]
            exit_px = exit_px or t["entry"]
            exit_time = "OPEN"

        pts_pnl = round(exit_px - t["entry"], 2)
        net_rs = round(pts_pnl * qty, 2)

        rows.append({
            "Trade #": idx,
            "Date": date_str,
            "Entry Time": t.get("entry_time", "-"),
            "Exit Time": exit_time,
            "Index": t["index_name"],
            "Strategy": t.get("strategy", "-"),
            "Option Symbol": t["symbol"],
            "Lot Qty": qty,
            "Entry Price (₹)": t["entry"],
            "Exit / Live Price (₹)": exit_px,
            "Points P&L": pts_pnl,
            "Net P&L (₹)": net_rs,
            "Outcome": t["outcome"]
        })

    df_trades = pd.DataFrame(rows) if rows else pd.DataFrame(columns=[
        "Trade #", "Date", "Entry Time", "Exit Time", "Index", "Strategy",
        "Option Symbol", "Lot Qty", "Entry Price (₹)", "Exit / Live Price (₹)",
        "Points P&L", "Net P&L (₹)", "Outcome"
    ])

    total = len(active_trades)
    targets = sum(1 for t in active_trades if t["outcome"] == "TARGET")
    cost_exits = sum(1 for t in active_trades if t["outcome"] == "COST_EXIT")
    sls = sum(1 for t in active_trades if t["outcome"] == "SL")
    manual = sum(1 for t in active_trades if t["outcome"] in ("MANUAL_EXIT", "EOD_EXIT"))
    win_rate = round((targets / total) * 100, 1) if total > 0 else 0.0
    realized, unrealized = calculate_virtual_pnl(smart_api)
    net_pnl = round(realized + unrealized, 2)
    closing_bal = round(VIRTUAL_CAPITAL + net_pnl, 2)

    df_summary = pd.DataFrame([
        {"Metric": "Report Date", "Value": date_str},
        {"Metric": "Total Signals", "Value": total},
        {"Metric": "Target Hits (+15%)", "Value": targets},
        {"Metric": "Cost-to-Cost Exits (₹0 Loss)", "Value": cost_exits},
        {"Metric": "Stop Loss Hits", "Value": sls},
        {"Metric": "Manual / EOD Exits", "Value": manual},
        {"Metric": "Target Win Rate (%)", "Value": f"{win_rate}%"},
        {"Metric": "Starting Virtual Capital (₹)", "Value": VIRTUAL_CAPITAL},
        {"Metric": "Net Day P&L (₹)", "Value": net_pnl},
        {"Metric": "Closing Virtual Balance (₹)", "Value": closing_bal}
    ])

    try:
        with pd.ExcelWriter(filename, engine="openpyxl") as writer:
            df_trades.to_excel(writer, sheet_name="Trade_Log", index=False)
            df_summary.to_excel(writer, sheet_name="Daily_Summary", index=False)

        tag = "EOD Final" if is_eod else "Live Interim"
        caption = f"📥 <b>FINSIDE ZING v2.5 {tag} Excel Report ({date_str})</b>\n💼 Net P&L: <b>₹{net_pnl:,.2f}</b> | Win Rate: <b>{win_rate}%</b>"
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

def load_option_master():
    global scrip_master_df
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        data = requests.get(url, timeout=30).json()
        df = pd.DataFrame(data)
        df = df[(df['exch_seg'] == 'NFO') & (df['instrumenttype'] == 'OPTIDX')]
        df['expiry_dt'] = pd.to_datetime(df['expiry'], format='%d%b%Y', errors='coerce')
        df['strike_val'] = pd.to_numeric(df['strike'], errors='coerce') / 100.0
        today = pd.to_datetime(get_ist_now().strftime('%Y-%m-%d'))
        scrip_master_df = df[df['expiry_dt'] >= today]
    except Exception as e:
        print("Scrip Master Load Error:", e)

def get_nearest_option(scrip_name, strike_price, opt_type):
    if scrip_master_df is None:
        return None, None
    sub = scrip_master_df[
        (scrip_master_df['name'] == scrip_name) &
        (scrip_master_df['strike_val'] == float(strike_price)) &
        (scrip_master_df['symbol'].str.endswith(opt_type))
    ].sort_values('expiry_dt')
    if not sub.empty:
        row = sub.iloc[0]
        return row['symbol'], row['token']
    return None, None

def get_option_ltp(smart_api, symbol, token):
    try:
        res = smart_api.ltpData("NFO", symbol, token)
        if res and res.get("data"):
            return float(res["data"]["ltp"])
    except Exception as e:
        print("LTP Fetch Error:", e)
    return None

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

def get_index_realized_pnl(idx_name):
    total = 0.0
    for t in active_trades:
        if t["index_name"] == idx_name and t["status"] == "CLOSED":
            total += (t.get("exit_price", t["entry"]) - t["entry"]) * t["lot_size"]
    return round(total, 2)

def calculate_virtual_pnl(smart_api=None):
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    for t in active_trades:
        qty = t["lot_size"]
        if t["status"] == "CLOSED":
            exit_px = t.get("exit_price", t["entry"])
            realized_pnl += (exit_px - t["entry"]) * qty
        elif t["status"] == "OPEN" and smart_api:
            ltp = get_option_ltp(smart_api, t["symbol"], t["token"]) or t["entry"]
            unrealized_pnl += (ltp - t["entry"]) * qty
    return round(realized_pnl, 2), round(unrealized_pnl, 2)

def compute_protected_sl_and_trail(opt_ltp, lot_qty, is_vip_late_session=False):
    """10% SL அல்லது Max ₹550 Cap (VIP Late Session-ல் 7% SL அல்லது ₹400 Cap)"""
    if is_vip_late_session:
        pct_sl_pts = opt_ltp * 0.07
        max_cap_pts = 400.0 / lot_qty
        trail_pct = 0.04  # +4% Quick Trail after 2:15 PM
    else:
        pct_sl_pts = opt_ltp * DEFAULT_SL_PCT
        max_cap_pts = MAX_LOSS_PER_TRADE_RS / lot_qty
        trail_pct = TRAIL_TRIGGER_PCT  # +6% Early Trail

    sl_points = max(MIN_SL_POINTS, min(pct_sl_pts, max_cap_pts))
    opt_sl = round(max(1.0, opt_ltp - sl_points), 1)
    trail_trigger = round(opt_ltp * (1 + trail_pct), 1)
    return opt_sl, trail_trigger, round(trail_pct * 100)

# ================= ANGEL ONE NOTIFICATION PARSER (/angel) =================
def handle_angel_notification(smart_api, raw_text):
    """MacroDroid அனுப்பும் Angel One Call Notification-ஐ ZING Filter உடன் பரிசீலிக்கும்"""
    if scanner_paused or get_daily_sl_count() >= MAX_DAILY_SL:
        send_telegram_alert("🛡️ <b>Angel Call Ignored:</b> Scanner Paused or Daily SL Shield Active.")
        return

    now_ist = get_ist_now()
    if now_ist.hour >= 15:
        send_telegram_alert("⏰ <b>Angel Call Ignored:</b> Past 3:00 PM entry cutoff.")
        return

    upper_txt = raw_text.upper()
    scrip = "BANKNIFTY" if "BANKNIFTY" in upper_txt else ("NIFTY" if "NIFTY" in upper_txt else None)
    if not scrip:
        return

    item = next((w for w in WATCHLIST if w["scrip_name"] == scrip), None)
    if not item:
        return

    if item["name"] in locked_profit_indices:
        send_telegram_alert(f"🏆 <b>Angel Call Ignored:</b> {item['name']} Daily Profit Target (+₹{INDEX_PROFIT_LOCK_RS}) already locked!")
        return

    if has_open_trade_for_index(item["name"]):
        send_telegram_alert(f"⏳ <b>Angel Call Ignored:</b> {item['name']} already has an OPEN trade.")
        return

    # Extract CE/PE and Strike Price
    opt_match = re.search(r'\b(CE|PE)\s*(\d{5})', upper_txt)
    if not opt_match:
        opt_match_rev = re.search(r'(\d{5})\s*(CE|PE)\b', upper_txt)
        if not opt_match_rev:
            send_telegram_alert(f"⚠️ <b>Angel Call Parse Failed:</b> Strike/Option type not found in:\n<code>{raw_text}</code>")
            return
        strike_val = float(opt_match_rev.group(1))
        opt_type = opt_match_rev.group(2)
    else:
        opt_type = opt_match.group(1)
        strike_val = float(opt_match.group(2))

    # Extract Recommended Price Range if available
    price_match = re.search(r'RECOMMENDED PRICE[:\s₹]*([\d.]+)\s*[-–TO]+\s*([\d.]+)', upper_txt)
    rec_low, rec_high = (float(price_match.group(1)), float(price_match.group(2))) if price_match else (None, None)

    # Check ZING 15m Trend & ADX alignment
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
            f"🚫 <b>Angel One Call Rejected by ZING Filter!</b>\n"
            f"• Call: <b>{scrip} {int(strike_val)} {opt_type}</b>\n"
            f"• Reason: 15m Trend is <b>{trend_str}</b> & ADX is <b>{adx_val}</b> (Mismatch/Weak Momentum)"
        )
        return

    opt_symbol, opt_token = get_nearest_option(scrip, strike_val, opt_type)
    opt_ltp = get_option_ltp(smart_api, opt_symbol, opt_token) if opt_symbol else None
    if not opt_ltp:
        send_telegram_alert(f"⚠️ <b>Angel Call Skipped:</b> Could not fetch live LTP for {scrip} {int(strike_val)} {opt_type}.")
        return

    # Price Chase Protection (max +10% above Angel One upper recommended price)
    if rec_high and opt_ltp > (rec_high * 1.10):
        send_telegram_alert(
            f"🚫 <b>Angel One Call Rejected (Price Chased):</b>\n"
            f"• <code>{opt_symbol}</code> Live LTP ₹{opt_ltp} exceeded recommended range ₹{rec_low}–₹{rec_high}."
        )
        return

    lot_qty = item["lot_size"]
    opt_target = round(opt_ltp * (1 + TARGET_PCT), 1)
    opt_sl, trail_trigger, trail_pct_lbl = compute_protected_sl_and_trail(opt_ltp, lot_qty)
    entry_t = now_ist.strftime("%H:%M")
    icon = "🟢" if opt_type == "CE" else "🔴"

    msg = (
        f"{icon} <b>ZING v2.5 — ANGEL ONE CALL CONFIRMED</b>\n"
        f"✅ <b>Matched 15m Trend + ADX ({adx_val})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Strategy:</b> Angel One Call (ZING Filtered)\n"
        f"⚡ <b>Buy Option:</b> <code>{opt_symbol}</code> (1 Lot = {lot_qty})\n"
        f"💰 <b>Entry Premium:</b> ₹{opt_ltp}\n"
        f"🎯 <b>Target (+15%):</b> ₹{opt_target}\n"
        f"🛑 <b>Protected SL (Max ₹550 Cap):</b> ₹{opt_sl}\n"
        f"🔒 <b>Early Cost Trail (+{trail_pct_lbl}%):</b> ₹{trail_trigger}\n"
        f"⏰ <b>Time:</b> {entry_t}"
    )
    msg_id = send_telegram_alert(msg)
    if msg_id:
        active_trades.append({
            "index_name": item["name"],
            "strategy": "Angel One Call (Filtered)",
            "lot_size": lot_qty,
            "symbol": opt_symbol,
            "token": opt_token,
            "entry": opt_ltp,
            "exit_price": opt_ltp,
            "entry_time": entry_t,
            "exit_time": "-",
            "target": opt_target,
            "sl": opt_sl,
            "trail_trigger": trail_trigger,
            "trail_pct_lbl": trail_pct_lbl,
            "trailed": False,
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
                handle_angel_notification(smart_api, raw_text)

            elif text.startswith("/status") or "status" in text or "market" in text:
                lines = ["📊 <b>LIVE MARKET STATUS (v2.5)</b>\n━━━━━━━━━━━━━━━━━━━━"]
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
                open_list = [t for t in active_trades if t["status"] == "OPEN"]
                if not open_list:
                    send_telegram_alert("📭 <b>No OPEN trades right now.</b>")
                else:
                    lines = ["⚡ <b>ACTIVE RUNNING TRADES (1 Lot)</b>\n━━━━━━━━━━━━━━━━━━━━"]
                    for t in open_list:
                        ltp = get_option_ltp(smart_api, t["symbol"], t["token"]) or t["entry"]
                        diff = round(ltp - t["entry"], 1)
                        pnl_rs = round(diff * t["lot_size"], 1)
                        sign = "+" if diff >= 0 else ""
                        sl_mode = "Cost-Trailed 🔒" if t["trailed"] else "Protected SL 🛑"
                        lines.append(
                            f"📌 <code>{t['symbol']}</code> (Qty: {t['lot_size']})\n"
                            f"   Entry: ₹{t['entry']} | Live: ₹{ltp} ({sign}₹{diff} | <b>{sign}₹{pnl_rs}</b>)\n"
                            f"   Target: ₹{t['target']} | SL: ₹{t['sl']} ({sl_mode})"
                        )
                    send_telegram_alert("\n".join(lines))

            elif text.startswith("/virtual") or "virtual" in text or "wallet" in text:
                realized, unrealized = calculate_virtual_pnl(smart_api)
                net_pnl = round(realized + unrealized, 2)
                curr_bal = round(VIRTUAL_CAPITAL + net_pnl, 2)
                sign = "+" if net_pnl >= 0 else ""
                send_telegram_alert(
                    f"💼 <b>VIRTUAL PAPER TRADING WALLET (1 Lot)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏦 <b>Starting Capital:</b> ₹{VIRTUAL_CAPITAL:,.2f}\n"
                    f"✅ <b>Closed Trades P&L:</b> ₹{realized:,.2f}\n"
                    f"⏳ <b>Running Trades P&L:</b> ₹{unrealized:,.2f}\n"
                    f"📈 <b>Net Today P&L:</b> <b>{sign}₹{net_pnl:,.2f}</b>\n"
                    f"💰 <b>Current Balance:</b> <b>₹{curr_bal:,.2f}</b>"
                )

            elif text.startswith("/excel") or "excel" in text:
                send_telegram_alert("⏳ Generating live Excel report...")
                generate_and_send_excel(smart_api=smart_api, is_eod=False)

            elif text.startswith("/closeall") or "closeall" in text:
                open_list = [t for t in active_trades if t["status"] == "OPEN"]
                if not open_list:
                    send_telegram_alert("📭 <b>No open trades to close.</b> Scanner is actively watching!")
                else:
                    now_str = get_ist_now().strftime("%H:%M")
                    closed_lines = ["🛑 <b>MANUAL EXIT EXECUTED (/closeall)</b>\n━━━━━━━━━━━━━━━━━━━━"]
                    for t in open_list:
                        ltp = get_option_ltp(smart_api, t["symbol"], t["token"]) or t["entry"]
                        t["status"] = "CLOSED"
                        t["outcome"] = "MANUAL_EXIT"
                        t["exit_price"] = ltp
                        t["exit_time"] = now_str
                        pnl_rs = round((ltp - t["entry"]) * t["lot_size"], 1)
                        sign = "+" if pnl_rs >= 0 else ""
                        closed_lines.append(f"• <code>{t['symbol']}</code> closed @ ₹{ltp} ({sign}₹{pnl_rs})")
                    closed_lines.append("\n🔓 <i>All Index Locks released. Scanner continues watching!</i>")
                    send_telegram_alert("\n".join(closed_lines))

            elif text.startswith("/pnl") or "pnl" in text or "report" in text:
                total = len(active_trades)
                if total == 0:
                    send_telegram_alert("📊 <b>Today's P&L:</b> No signals triggered yet today.")
                else:
                    targets = sum(1 for t in active_trades if t["outcome"] == "TARGET")
                    cost_exits = sum(1 for t in active_trades if t["outcome"] == "COST_EXIT")
                    sls = sum(1 for t in active_trades if t["outcome"] == "SL")
                    manual = sum(1 for t in active_trades if t["outcome"] in ("MANUAL_EXIT", "EOD_EXIT"))
                    opens = sum(1 for t in active_trades if t["outcome"] == "OPEN")
                    win_rate = round((targets / total) * 100, 1)
                    realized, unrealized = calculate_virtual_pnl(smart_api)
                    net_pnl = round(realized + unrealized, 2)
                    sign = "+" if net_pnl >= 0 else ""
                    send_telegram_alert(
                        f"🏆 <b>TODAY'S LIVE PERFORMANCE (v2.5)</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📌 Total Signals: {total} (Open: {opens})\n"
                        f"🎯 Targets: {targets} | 🛡️ Cost Exits: {cost_exits}\n"
                        f"🛑 Stop Losses: {sls}/{MAX_DAILY_SL} | ✋ Manual/EOD: {manual}\n"
                        f"🏆 Target Win Rate: {win_rate}%\n"
                        f"💼 1-Lot Net P&L: <b>{sign}₹{net_pnl:,.2f}</b>"
                    )

            elif text.startswith("/pause") or "pause" in text:
                scanner_paused = True
                send_telegram_alert("⏸️ <b>Scanner Paused!</b>\nNew signals stopped. Existing open trades will still be tracked.")

            elif text.startswith("/resume") or "resume" in text:
                scanner_paused = False
                send_telegram_alert("▶️ <b>Scanner Resumed!</b>\nScanning live market for new setups.")

            else:
                send_telegram_alert(
                    "🤖 <b>FINSIDE ZING v2.5 Commands:</b>\n"
                    "• /status — Live NIFTY & BANKNIFTY Trend/ADX\n"
                    "• /open — Check running trades & live P&L\n"
                    "• /virtual — ₹1,00,000 Paper Trading Wallet\n"
                    "• /pnl — Today's Win/Loss & Net P&L summary\n"
                    "• /excel — Download live .xlsx report anytime\n"
                    "• /closeall — Close open trades & unlock scanner\n"
                    "• /pause & /resume — Control signal alerts"
                )
    except Exception as e:
        print("Command Check Error:", e)
# =========================================================================

def evaluate_all_strategies(smart_api, df, item):
    global daily_sl_limit_notified
    if scanner_paused:
        return

    now_ist = get_ist_now()
    # 3:00 PM-க்குப் பிறகு புது என்ட்ரி கிடையாது
    if now_ist.hour >= 15:
        return

    # 2:15 PM – 3:00 PM = VIP Filter Mode (Profitable Day + ADX > 28 + 2/5 Confluence Only)
    is_vip_late_session = (now_ist.hour == 14 and now_ist.minute >= 15)
    if is_vip_late_session:
        realized_day, _ = calculate_virtual_pnl()
        if realized_day <= 0:
            return

    idx_name = item['name']

    # 1. Daily Index Profit Lock Check (+₹1,500)
    idx_pnl = get_index_realized_pnl(idx_name)
    if idx_pnl >= INDEX_PROFIT_LOCK_RS:
        if idx_name not in locked_profit_indices:
            locked_profit_indices.add(idx_name)
            send_telegram_alert(
                f"🏆 <b>PROFIT LOCKED FOR {idx_name}!</b>\n"
                f"{idx_name} reached <b>+₹{idx_pnl:,.2f}</b> today (Target: +₹{INDEX_PROFIT_LOCK_RS:,.0f}).\n"
                f"🔒 <i>{idx_name} locked for the rest of the day to protect profits!</i>"
            )
        return

    # 2. Daily Capital Shield Check (Max 2 SL)
    if get_daily_sl_count() >= MAX_DAILY_SL:
        if not daily_sl_limit_notified:
            daily_sl_limit_notified = True
            send_telegram_alert(
                f"🛡️ <b>CAPITAL SHIELD ACTIVATED!</b>\n"
                f"Daily Max Loss Limit ({MAX_DAILY_SL} Stop-Losses) reached.\n"
                f"🛑 New signals paused for today to protect your capital."
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

    # VIP Late Session (After 2:15 PM) requires at least 2 strategies confluence
    if is_vip_late_session and len(matched_list) < 2:
        return

    alert_id = f"{idx_name}_{chosen_type}_{curr['timestamp']}"
    if alert_id in sent_alerts:
        return

    sent_alerts.add(alert_id)

    opt_symbol, opt_token = get_nearest_option(item['scrip_name'], atm_strike, chosen_type)
    opt_ltp = get_option_ltp(smart_api, opt_symbol, opt_token) if opt_symbol else None
    icon = "🟢" if chosen_type == "CE" else "🔴"

    if opt_ltp:
        lot_qty = item['lot_size']
        opt_target = round(opt_ltp * (1 + TARGET_PCT), 1)
        opt_sl, trail_trigger, trail_pct_lbl = compute_protected_sl_and_trail(
            opt_ltp, lot_qty, is_vip_late_session=is_vip_late_session
        )

        count = len(matched_list)
        vip_tag = " [🌟 VIP LATE SESSION]" if is_vip_late_session else ""
        conviction = f"🔥 <b>HIGH CONVICTION ({count}/5 Confluence){vip_tag}</b>" if count >= 2 else "⚡ <b>STANDARD SIGNAL (1/5)</b>"
        strat_text = " + ".join(matched_list)
        entry_t = curr['timestamp'][11:16]
        cap_lbl = "₹400 VIP Cap" if is_vip_late_session else "₹550 Max Cap"

        msg = (
            f"{icon} <b>ZING INSTITUTIONAL v2.5</b>\n"
            f"{conviction}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>Strategy:</b> {strat_text}\n"
            f"📊 <b>Index:</b> {idx_name} (Spot: {spot})\n"
            f"🧭 <b>15m Trend:</b> {htf_label} | <b>ADX:</b> {adx_val}\n"
            f"⚡ <b>Buy Option:</b> <code>{opt_symbol}</code> (1 Lot = {lot_qty})\n"
            f"💰 <b>Entry Premium:</b> ₹{opt_ltp}\n"
            f"🎯 <b>Target (+15%):</b> ₹{opt_target}\n"
            f"🛑 <b>Protected SL ({cap_lbl}):</b> ₹{opt_sl}\n"
            f"🔒 <b>Early Cost Trail (+{trail_pct_lbl}%):</b> ₹{trail_trigger}\n"
            f"⏰ <b>Time:</b> {entry_t}"
        )
        msg_id = send_telegram_alert(msg)
        if msg_id:
            active_trades.append({
                "index_name": idx_name,
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
                "trail_trigger": trail_trigger,
                "trail_pct_lbl": trail_pct_lbl,
                "trailed": False,
                "msg_id": msg_id,
                "status": "OPEN",
                "outcome": "OPEN"
            })

def auto_squareoff_eod(smart_api):
    """3:25 PM-க்கு ஓப்பன் டிரேடுகள் இருந்தால் தானாக Square-off செய்யும்"""
    global eod_squareoff_done
    if eod_squareoff_done:
        return
    eod_squareoff_done = True
    open_list = [t for t in active_trades if t["status"] == "OPEN"]
    if not open_list:
        return

    now_str = get_ist_now().strftime("%H:%M")
    lines = ["⏰ <b>3:25 PM AUTO EOD SQUARE-OFF</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for t in open_list:
        ltp = get_option_ltp(smart_api, t["symbol"], t["token"]) or t["entry"]
        t["status"] = "CLOSED"
        t["outcome"] = "EOD_EXIT"
        t["exit_price"] = ltp
        t["exit_time"] = now_str
        pnl_rs = round((ltp - t["entry"]) * t["lot_size"], 1)
        sign = "+" if pnl_rs >= 0 else ""
        lines.append(f"• <code>{t['symbol']}</code> closed @ ₹{ltp} ({sign}₹{pnl_rs})")
    send_telegram_alert("\n".join(lines))

def monitor_active_trades(smart_api):
    now_str = get_ist_now().strftime("%H:%M")
    for trade in active_trades:
        if trade["status"] != "OPEN":
            continue
        ltp = get_option_ltp(smart_api, trade["symbol"], trade["token"])
        if not ltp:
            continue

        qty = trade["lot_size"]

        if ltp >= trade["target"]:
            trade["status"] = "CLOSED"
            trade["outcome"] = "TARGET"
            trade["exit_price"] = ltp
            trade["exit_time"] = now_str
            profit_pts = round(ltp - trade["entry"], 1)
            profit_rs = round(profit_pts * qty, 1)
            send_telegram_alert(
                f"🎯 <b>TARGET HIT! (+₹{profit_pts} | +₹{profit_rs})</b>\n<code>{trade['symbol']}</code> reached ₹{ltp}\n🔓 <i>{trade['index_name']} unlocked for next setup.</i>",
                reply_to_msg_id=trade["msg_id"]
            )
            continue

        if not trade["trailed"] and ltp >= trade["trail_trigger"]:
            trade["trailed"] = True
            trade["sl"] = trade["entry"]
            pct_lbl = trade.get("trail_pct_lbl", 6)
            send_telegram_alert(
                f"🔒 <b>SL TRAILED TO COST (₹{trade['entry']})</b>\n<code>{trade['symbol']}</code> reached ₹{ltp} (+{pct_lbl}%). Now a Risk-Free Trade!",
                reply_to_msg_id=trade["msg_id"]
            )
            continue

        if ltp <= trade["sl"]:
            trade["status"] = "CLOSED"
            trade["exit_price"] = ltp
            trade["exit_time"] = now_str
            if trade["trailed"]:
                trade["outcome"] = "COST_EXIT"
                send_telegram_alert(
                    f"🛡️ <b>COST-TO-COST EXIT (₹0 Loss)</b>\n<code>{trade['symbol']}</code> returned to Entry ₹{trade['entry']}\n🔓 <i>{trade['index_name']} unlocked for next setup.</i>",
                    reply_to_msg_id=trade["msg_id"]
                )
            else:
                trade["outcome"] = "SL"
                loss_pts = round(trade["entry"] - ltp, 1)
                loss_rs = round(loss_pts * qty, 1)
                sl_used = get_daily_sl_count()
                send_telegram_alert(
                    f"🛑 <b>STOP LOSS HIT (-₹{loss_pts} | -₹{loss_rs})</b> [{sl_used}/{MAX_DAILY_SL} Daily SL]\n<code>{trade['symbol']}</code> touched ₹{ltp}\n🔓 <i>{trade['index_name']} unlocked.</i>",
                    reply_to_msg_id=trade["msg_id"]
                )

def send_eod_report(smart_api=None):
    total = len(active_trades)
    if total == 0:
        send_telegram_alert("🛑 <b>Market Closed (3:30 PM IST).</b>\nNo institutional signals triggered today.")
        return

    targets = sum(1 for t in active_trades if t["outcome"] == "TARGET")
    cost_exits = sum(1 for t in active_trades if t["outcome"] == "COST_EXIT")
    sls = sum(1 for t in active_trades if t["outcome"] == "SL")
    manual = sum(1 for t in active_trades if t["outcome"] in ("MANUAL_EXIT", "EOD_EXIT"))
    open_trades = sum(1 for t in active_trades if t["outcome"] == "OPEN")
    win_rate = round((targets / total) * 100, 1)
    realized, unrealized = calculate_virtual_pnl(smart_api)
    net_pnl = round(realized + unrealized, 2)
    final_bal = round(VIRTUAL_CAPITAL + net_pnl, 2)
    sign = "+" if net_pnl >= 0 else ""

    report = (
        f"📊 <b>FINSIDE ZING v2.5 - DAILY PERFORMANCE REPORT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Total Signals:</b> {total}\n"
        f"🎯 <b>Target Hits (+15%):</b> {targets}\n"
        f"🛡️ <b>Cost-to-Cost Exits (₹0 Loss):</b> {cost_exits}\n"
        f"🛑 <b>Stop Loss Hits:</b> {sls}\n"
        f"✋ <b>Manual / EOD Exits:</b> {manual}\n"
        f"⏳ <b>Open at Close:</b> {open_trades}\n"
        f"🏆 <b>Target Win Rate:</b> {win_rate}%\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>1-Lot Virtual P&L:</b> <b>{sign}₹{net_pnl:,.2f}</b>\n"
        f"💰 <b>Closing Virtual Balance:</b> <b>₹{final_bal:,.2f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
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
    load_option_master()
    send_telegram_alert(
        "🚀 <b>FINSIDE ZING Institutional v2.5 Active!</b>\n"
        "🛡️ <b>Protections:</b> +6% Early Trail | Max ₹550 SL Cap | +₹1,500 Index Profit Lock\n"
        "🌟 <b>2:15 PM VIP Mode</b> & <b>/angel Call Filter</b> Enabled"
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
