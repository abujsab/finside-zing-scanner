import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ==============================================================================
# FINSIDE ZING FOREX & GOLD 24/5 SCANNER (GITHUB ACTIONS 5-MIN FAST EDITION)
# ==============================================================================

TWELVE_DATA_API_KEY = os.environ.get("f1a371fed96f4b2f8e867a37838b8a5c", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("9238096863:AAFwAU6uaplz1wDhcgKK58WXkDtfN2_EDfE", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("7770756739", "").strip()

PAIRS = ["XAU/USD", "EUR/USD", "GBP/USD", "USD/JPY"]
STATE_FILE = "forex_zing_state.json"
INITIAL_CAPITAL = 10000.0
RISK_PER_TRADE_PCT = 0.01  # 1% risk per virtual trade ($100)


def get_ist_now():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def is_forex_market_open():
    """Forex & Gold market runs 24/5: Mon 02:30 IST to Sat 02:30 IST."""
    now_ist = get_ist_now()
    wd = now_ist.weekday()  # Mon=0 ... Sun=6
    hr = now_ist.hour
    minute = now_ist.minute
    if wd == 6:  # Sunday closed
        return False
    if wd == 5 and (hr > 2 or (hr == 2 and minute >= 30)):  # Saturday after 2:30 AM IST
        return False
    if wd == 0 and (hr < 2 or (hr == 2 and minute < 30)):  # Monday before 2:30 AM IST
        return False
    return True


def get_session_tag():
    hr = get_ist_now().hour
    if 5 <= hr < 12:
        return "Asian Session 🌏"
    elif 12 <= hr < 17:
        return "London Session 🇬🇧"
    elif 17 <= hr < 22:
        return "London-NY Overlap 🔥"
    else:
        return "New York Session 🇺🇸"


def send_telegram(text, chat_id=None):
    target_id = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target_id:
        print("Telegram credentials missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": target_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        print(f"Telegram Send Error: {e}")


def get_telegram_updates(offset=0):
    if not TELEGRAM_BOT_TOKEN:
        return {}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=5"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Telegram Updates Error: {e}")
        return {}


def load_state():
    default_state = {
        "capital": INITIAL_CAPITAL,
        "initial_capital": INITIAL_CAPITAL,
        "open_trades": [],
        "closed_trades": [],
        "last_signal_candle": {},
        "htf_cache": {},
        "last_update_id": 0
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                for k, v in default_state.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception as e:
            print(f"State Load Error: {e}")
    return default_state


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"State Save Error: {e}")


def fetch_candles(symbol, interval="15min", outputsize=60):
    encoded_sym = urllib.parse.quote(symbol)
    url = (
        f"https://api.twelvedata.com/time_series?symbol={encoded_sym}"
        f"&interval={interval}&outputsize={outputsize}&apikey={TWELVE_DATA_API_KEY}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FinsideForexBot/2.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        if raw.get("status") != "ok" or "values" not in raw:
            print(f"TwelveData Error ({symbol} {interval}): {raw.get('message', raw)}")
            return []
        candles = []
        for row in reversed(raw["values"]):
            candles.append({
                "datetime": row["datetime"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"])
            })
        return candles
    except Exception as e:
        print(f"Fetch Error ({symbol} {interval}): {e}")
        return []


def get_cached_1h_trend(symbol, state):
    """Caches 1H EMA Trend for 55 minutes to conserve API credits for 5-min scans."""
    now_ts = time.time()
    cache_entry = state.get("htf_cache", {}).get(symbol)
    if cache_entry and (now_ts - cache_entry.get("updated_at", 0)) < 3300:
        return cache_entry["trend"]

    c1h = fetch_candles(symbol, "1h", 35)
    time.sleep(8)
    if not c1h or len(c1h) < 25:
        return cache_entry["trend"] if cache_entry else None

    closes_1h = [c["close"] for c in c1h[:-1]]
    ema9_1h = calc_ema(closes_1h, 9)[-1]
    ema21_1h = calc_ema(closes_1h, 21)[-1]
    trend = "BULL" if ema9_1h > ema21_1h else "BEAR"

    state.setdefault("htf_cache", {})[symbol] = {
        "trend": trend,
        "updated_at": now_ts
    }
    return trend


# ==============================================================================
# TECHNICAL INDICATORS
# ==============================================================================

def calc_ema(values, period):
    if len(values) < period:
        return [values[-1]] * len(values)
    k = 2 / (period + 1)
    ema = [sum(values[:period]) / period]
    for price in values[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return [ema[0]] * (period - 1) + ema


def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calc_adx(candles, period=14):
    if len(candles) < period * 2:
        return 0.0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(candles)):
        up = candles[i]["high"] - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))

    atr = sum(tr_list[:period])
    p_di_s = sum(plus_dm[:period])
    m_di_s = sum(minus_dm[:period])
    dx_list = []

    for i in range(period, len(tr_list)):
        atr = atr - (atr / period) + tr_list[i]
        p_di_s = p_di_s - (p_di_s / period) + plus_dm[i]
        m_di_s = m_di_s - (m_di_s / period) + minus_dm[i]
        p_di = 100 * (p_di_s / atr) if atr else 0
        m_di = 100 * (m_di_s / atr) if atr else 0
        denom = p_di + m_di
        dx_list.append(100 * abs(p_di - m_di) / denom if denom else 0)

    if len(dx_list) < period:
        return sum(dx_list) / len(dx_list) if dx_list else 0.0
    adx = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def calc_supertrend(candles, period=10, multiplier=3.0):
    if len(candles) < period + 2:
        return "NEUTRAL", 0.0
    atr = calc_atr(candles[:-1], period)
    hl2 = (candles[-2]["high"] + candles[-2]["low"]) / 2
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr
    close_prev = candles[-2]["close"]
    if close_prev > upper:
        return "BUY", lower
    elif close_prev < lower:
        return "SELL", upper
    return ("BUY" if close_prev >= hl2 else "SELL"), (lower if close_prev >= hl2 else upper)


def calc_bollinger(closes, period=20, std_mult=2.0):
    if len(closes) < period:
        return closes[-1], closes[-1], closes[-1]
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    sd = variance ** 0.5
    return sma + std_mult * sd, sma, sma - std_mult * sd


# ==============================================================================
# FINSIDE ZING 5-STRATEGY ENGINE
# ==============================================================================

def analyze_pair(symbol, candles_15m, htf_trend):
    if len(candles_15m) < 40 or not htf_trend:
        return None

    c_sig = candles_15m[-2]
    c_prev = candles_15m[-3]
    closes_15 = [c["close"] for c in candles_15m[:-1]]

    adx_val = calc_adx(candles_15m[:-1], 14)
    if adx_val < 20.0:
        return None

    atr_val = calc_atr(candles_15m[:-1], 14)
    if atr_val <= 0:
        return None

    ema9 = calc_ema(closes_15, 9)
    ema21 = calc_ema(closes_15, 21)
    st_dir, _ = calc_supertrend(candles_15m, 10, 3.0)
    bb_up, _, bb_low = calc_bollinger(closes_15, 20, 2.0)

    signal_dir = None
    strategy_name = None

    # 1. EMA 9/21 Crossover + 1H Alignment
    if ema9[-2] <= ema21[-2] and ema9[-1] > ema21[-1] and htf_trend == "BULL":
        signal_dir, strategy_name = "BUY", "EMA 9/21 Cross + 1H Trend"
    elif ema9[-2] >= ema21[-2] and ema9[-1] < ema21[-1] and htf_trend == "BEAR":
        signal_dir, strategy_name = "SELL", "EMA 9/21 Cross + 1H Trend"

    # 2. Supertrend + Prime EMA Scalper
    elif st_dir == "BUY" and c_sig["close"] > ema9[-1] and c_prev["close"] <= ema9[-2] and htf_trend == "BULL":
        signal_dir, strategy_name = "BUY", "Supertrend + Prime Scalper"
    elif st_dir == "SELL" and c_sig["close"] < ema9[-1] and c_prev["close"] >= ema9[-2] and htf_trend == "BEAR":
        signal_dir, strategy_name = "SELL", "Supertrend + Prime Scalper"

    # 3. Inside Candle Breakout
    elif c_sig["high"] < c_prev["high"] and c_sig["low"] > c_prev["low"]:
        live_c = candles_15m[-1]
        if live_c["close"] > c_prev["high"] and htf_trend == "BULL":
            signal_dir, strategy_name = "BUY", "Inside Candle Breakout"
        elif live_c["close"] < c_prev["low"] and htf_trend == "BEAR":
            signal_dir, strategy_name = "SELL", "Inside Candle Breakout"

    # 4. Bollinger Band Trend Pullback
    elif c_prev["low"] <= bb_low and c_sig["close"] > bb_low and c_sig["close"] > c_sig["open"] and htf_trend == "BULL":
        signal_dir, strategy_name = "BUY", "Bollinger Trend Pullback"
    elif c_prev["high"] >= bb_up and c_sig["close"] < bb_up and c_sig["close"] < c_sig["open"] and htf_trend == "BEAR":
        signal_dir, strategy_name = "SELL", "Bollinger Trend Pullback"

    if not signal_dir:
        return None

    decimals = 2 if "XAU" in symbol else (3 if "JPY" in symbol else 5)
    entry = round(candles_15m[-1]["close"], decimals)
    sl_dist = round(1.5 * atr_val, decimals)

    if signal_dir == "BUY":
        sl = round(entry - sl_dist, decimals)
        tp1 = round(entry + sl_dist, decimals)
        tp2 = round(entry + (2.0 * sl_dist), decimals)
    else:
        sl = round(entry + sl_dist, decimals)
        tp1 = round(entry - sl_dist, decimals)
        tp2 = round(entry - (2.0 * sl_dist), decimals)

    return {
        "symbol": symbol,
        "direction": signal_dir,
        "strategy": strategy_name,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "adx": round(adx_val, 1),
        "candle_time": c_sig["datetime"],
        "decimals": decimals
    }


# ==============================================================================
# VIRTUAL PAPER TRADING ENGINE & COMMANDS
# ==============================================================================

def update_open_trades(state, latest_prices):
    remaining_trades = []
    for trade in state["open_trades"]:
        sym = trade["symbol"]
        if sym not in latest_prices:
            remaining_trades.append(trade)
            continue

        price = latest_prices[sym]
        direction = trade["direction"]
        entry = trade["entry"]
        sl = trade["sl"]
        tp1 = trade["tp1"]
        tp2 = trade["tp2"]
        risk_amt = trade["risk_amt"]

        if direction == "BUY":
            if not trade.get("trailed_to_cost") and price >= tp1:
                trade["sl"] = entry
                trade["trailed_to_cost"] = True
                send_telegram(f"🛡️ <b>COST TRAIL ACTIVATED: {sym} (BUY)</b>\nPrice hit 1:1 (<code>{price}</code>). SL moved to Entry (<code>{entry}</code>).")

            if price >= tp2:
                pnl = round(risk_amt * 2.0, 2)
                state["capital"] = round(state["capital"] + pnl, 2)
                trade.update({"exit_price": price, "pnl": pnl, "result": "TARGET_2_HIT", "closed_at": get_ist_now().strftime("%Y-%m-%d %H:%M IST")})
                state["closed_trades"].append(trade)
                send_telegram(f"🎯 <b>TARGET 2 HIT (1:2 RR): {sym} (BUY)</b>\n• Exit: <code>{price}</code> | Profit: <b>+${pnl}</b>\n• Wallet: <b>${state['capital']}</b>")
                continue

            if price <= trade["sl"]:
                pnl = 0.0 if trade.get("trailed_to_cost") else -risk_amt
                state["capital"] = round(state["capital"] + pnl, 2)
                res_tag = "BREAKEVEN_EXIT" if pnl == 0 else "SL_HIT"
                trade.update({"exit_price": price, "pnl": pnl, "result": res_tag, "closed_at": get_ist_now().strftime("%Y-%m-%d %H:%M IST")})
                state["closed_trades"].append(trade)
                send_telegram(f"🛑 <b>{res_tag}: {sym} (BUY)</b>\n• Exit: <code>{price}</code> | PnL: <b>${pnl}</b>\n• Wallet: <b>${state['capital']}</b>")
                continue

        else:  # SELL
            if not trade.get("trailed_to_cost") and price <= tp1:
                trade["sl"] = entry
                trade["trailed_to_cost"] = True
                send_telegram(f"🛡️ <b>COST TRAIL ACTIVATED: {sym} (SELL)</b>\nPrice hit 1:1 (<code>{price}</code>). SL moved to Entry (<code>{entry}</code>).")

            if price <= tp2:
                pnl = round(risk_amt * 2.0, 2)
                state["capital"] = round(state["capital"] + pnl, 2)
                trade.update({"exit_price": price, "pnl": pnl, "result": "TARGET_2_HIT", "closed_at": get_ist_now().strftime("%Y-%m-%d %H:%M IST")})
                state["closed_trades"].append(trade)
                send_telegram(f"🎯 <b>TARGET 2 HIT (1:2 RR): {sym} (SELL)</b>\n• Exit: <code>{price}</code> | Profit: <b>+${pnl}</b>\n• Wallet: <b>${state['capital']}</b>")
                continue

            if price >= trade["sl"]:
                pnl = 0.0 if trade.get("trailed_to_cost") else -risk_amt
                state["capital"] = round(state["capital"] + pnl, 2)
                res_tag = "BREAKEVEN_EXIT" if pnl == 0 else "SL_HIT"
                trade.update({"exit_price": price, "pnl": pnl, "result": res_tag, "closed_at": get_ist_now().strftime("%Y-%m-%d %H:%M IST")})
                state["closed_trades"].append(trade)
                send_telegram(f"🛑 <b>{res_tag}: {sym} (SELL)</b>\n• Exit: <code>{price}</code> | PnL: <b>${pnl}</b>\n• Wallet: <b>${state['capital']}</b>")
                continue

        remaining_trades.append(trade)
    state["open_trades"] = remaining_trades


def process_commands(state, market_snapshot=None):
    updates = get_telegram_updates(state.get("last_update_id", 0) + 1)
    if not updates.get("ok"):
        return
    for upd in updates.get("result", []):
        state["last_update_id"] = upd["update_id"]
        msg = upd.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not text:
            continue

        if text in ["/start", "/help"]:
            send_telegram(
                "🤖 <b>FINSIDE ZING FOREX & GOLD BOT (5-Min Fast Scan)</b>\n\n"
                "• /market - Live Prices, 1H Trend & ADX Status\n"
                "• /status - Wallet Balance & Win Rate\n"
                "• /open - Active Paper Trades\n"
                "• /pnl - Session-wise Performance\n"
                "• /reset - Reset $10,000 Paper Wallet",
                chat_id
            )
        elif text == "/market":
            if not market_snapshot:
                send_telegram("⏳ மார்க்கெட் டேட்டா சேகரிக்கப்படுகிறது. அடுத்த 5-நிமிட சுழற்சியில் மீண்டும் முயற்சிக்கவும்.", chat_id)
            else:
                lines = [f"📡 <b>LIVE FOREX & GOLD MARKET PULSE</b>\n<i>Session: {get_session_tag()}</i>\n"]
                for sym, info in market_snapshot.items():
                    trend_icon = "🟢 BULL" if info["trend"] == "BULL" else "🔴 BEAR"
                    adx_status = "⚡ Strong" if info["adx"] >= 20 else "💤 Sideways"
                    lines.append(
                        f"• <b>{sym}</b>: <code>{info['price']}</code>\n"
                        f"  1H Trend: {trend_icon} | ADX: {info['adx']} ({adx_status})"
                    )
                send_telegram("\n".join(lines), chat_id)
        elif text == "/status":
            total_closed = len(state["closed_trades"])
            wins = sum(1 for t in state["closed_trades"] if t["pnl"] > 0)
            losses = sum(1 for t in state["closed_trades"] if t["pnl"] < 0)
            be = sum(1 for t in state["closed_trades"] if t["pnl"] == 0)
            net_pnl = round(state["capital"] - state["initial_capital"], 2)
            win_rate = round((wins / total_closed * 100), 1) if total_closed > 0 else 0.0
            send_telegram(
                f"📊 <b>FINSIDE FOREX WALLET STATUS</b>\n"
                f"• Session: {get_session_tag()}\n"
                f"• Balance: <b>${state['capital']}</b> (Net: ${net_pnl})\n"
                f"• Open: {len(state['open_trades'])} | Closed: {total_closed} (✅{wins} ❌{losses} 🛡️{be})\n"
                f"• Win Rate: <b>{win_rate}%</b>",
                chat_id
            )
        elif text == "/open":
            if not state["open_trades"]:
                send_telegram("📭 தற்போது Open Trades எதுவும் இல்லை.", chat_id)
            else:
                lines = ["📌 <b>ACTIVE FOREX PAPER TRADES:</b>\n"]
                for t in state["open_trades"]:
                    trail = " [🛡️ Cost Trailed]" if t.get("trailed_to_cost") else ""
                    lines.append(f"• <b>{t['symbol']}</b> ({t['direction']}){trail}\n  Entry: <code>{t['entry']}</code> | SL: <code>{t['sl']}</code> | TP2: <code>{t['tp2']}</code>")
                send_telegram("\n".join(lines), chat_id)
        elif text == "/pnl":
            session_stats = {}
            for t in state["closed_trades"]:
                sess = t.get("session", "Unknown")
                session_stats[sess] = session_stats.get(sess, 0.0) + t["pnl"]
            if not session_stats:
                send_telegram("📉 இன்னும் Closed Trades எதுவும் பதிவாகவில்லை.", chat_id)
            else:
                lines = ["📈 <b>SESSION-WISE PNL REPORT:</b>\n"]
                for s, p in session_stats.items():
                    lines.append(f"• {s}: <b>${round(p, 2)}</b>")
                send_telegram("\n".join(lines), chat_id)
        elif text == "/reset":
            state.update({"capital": INITIAL_CAPITAL, "open_trades": [], "closed_trades": [], "last_signal_candle": {}})
            send_telegram("🔄 Virtual Wallet $10,000-க்கு ரீசெட் செய்யப்பட்டது!", chat_id)


def run_once():
    print(f"[{get_ist_now().strftime('%Y-%m-%d %H:%M IST')}] Running 5-Min Forex Scan...")
    state = load_state()

    if not is_forex_market_open():
        process_commands(state, None)
        print("Weekend Market Closed. Skipping scan.")
        save_state(state)
        return

    latest_prices = {}
    market_snapshot = {}
    current_session = get_session_tag()

    for sym in PAIRS:
        c15 = fetch_candles(sym, "15min", 60)
        time.sleep(8)
        htf_trend = get_cached_1h_trend(sym, state)

        if not c15 or not htf_trend:
            continue

        latest_prices[sym] = c15[-1]["close"]

        adx_val = round(calc_adx(c15[:-1], 14), 1)
        decimals = 2 if "XAU" in sym else (3 if "JPY" in sym else 5)
        market_snapshot[sym] = {
            "price": round(c15[-1]["close"], decimals),
            "trend": htf_trend,
            "adx": adx_val
        }

        if any(t["symbol"] == sym for t in state["open_trades"]):
            continue

        sig = analyze_pair(sym, c15, htf_trend)
        if sig and state["last_signal_candle"].get(sym) != sig["candle_time"]:
            state["last_signal_candle"][sym] = sig["candle_time"]
            risk_amt = round(state["capital"] * RISK_PER_TRADE_PCT, 2)
            state["open_trades"].append({
                "symbol": sym,
                "direction": sig["direction"],
                "strategy": sig["strategy"],
                "session": current_session,
                "entry": sig["entry"],
                "sl": sig["sl"],
                "tp1": sig["tp1"],
                "tp2": sig["tp2"],
                "risk_amt": risk_amt,
                "decimals": sig["decimals"],
                "trailed_to_cost": False,
                "opened_at": get_ist_now().strftime("%Y-%m-%d %H:%M IST")
            })
            emoji = "🟢" if sig["direction"] == "BUY" else "🔴"
            send_telegram(
                f"{emoji} <b>FINSIDE FOREX SIGNAL: {sym} ({sig['direction']})</b>\n"
                f"• Session: {current_session}\n"
                f"• Strategy: <i>{sig['strategy']}</i> (ADX: {sig['adx']})\n"
                f"• Entry: <code>{sig['entry']}</code>\n"
                f"• Stop Loss (1.5 ATR): <code>{sig['sl']}</code>\n"
                f"• Target 1 (Cost Trail): <code>{sig['tp1']}</code>\n"
                f"• Target 2 (1:2 RR): <code>{sig['tp2']}</code>\n"
                f"• Virtual Risk: ${risk_amt}"
            )

    process_commands(state, market_snapshot)
    update_open_trades(state, latest_prices)
    save_state(state)
    print("5-Min Forex Scan Completed Successfully.")


if __name__ == "__main__":
    run_once()
