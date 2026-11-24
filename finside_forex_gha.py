import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ==============================================================================
# FINSIDE ZING FOREX & GOLD 24/5 SCANNER (PRO 8-COMMAND EDITION - CLEAN)
# ==============================================================================

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("FOREX_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

PAIRS = ["XAU/USD", "EUR/USD", "GBP/USD", "USD/JPY"]
STATE_FILE = "forex_zing_state.json"
INITIAL_CAPITAL = 10000.0
RISK_PER_TRADE_PCT = 0.01  # 1% risk per virtual trade ($100)
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST)


def get_session_name(dt_ist):
    hr = dt_ist.hour + dt_ist.minute / 60.0
    if 18.5 <= hr <= 22.5:
        return "London-NY Overlap (High Liquidity)"
    elif 13.5 <= hr < 18.5:
        return "London Session"
    elif 22.5 < hr or hr < 2.5:
        return "New York Session"
    else:
        return "Asian / Sydney Session"


def normalize_trade(t, fallback_pair="UNKNOWN"):
    if not isinstance(t, dict):
        return None
    pair = t.get("pair") or t.get("symbol") or fallback_pair
    side = t.get("side") or t.get("direction") or t.get("action") or "BUY"
    entry = float(t.get("entry") or t.get("entry_price") or 0.0)
    sl = float(t.get("sl") or t.get("stop_loss") or 0.0)
    target = float(t.get("target") or t.get("tp") or 0.0)
    units = float(t.get("units") or t.get("qty") or t.get("size") or 0.0)
    return {
        "pair": pair,
        "side": side,
        "strategy": t.get("strategy", "ZING Trend"),
        "session": t.get("session", ""),
        "entry": entry,
        "sl": sl,
        "target": target,
        "units": units,
        "time": t.get("time") or t.get("opened_at") or "",
        "exit": float(t.get("exit", 0.0)),
        "pnl": float(t.get("pnl", 0.0)),
        "reason": t.get("reason", ""),
        "closed_time": t.get("closed_time", "")
    }


def load_state():
    default_state = {
        "balance": INITIAL_CAPITAL,
        "paused": False,
        "last_update_id": 0,
        "open_trades": [],
        "closed_trades": [],
        "last_prices": {}
    }
    if not os.path.exists(STATE_FILE):
        return default_state
    try:
        with open(STATE_FILE, "r") as f:
            raw = json.load(f)

        state = {
            "balance": float(raw.get("balance", raw.get("capital", INITIAL_CAPITAL))),
            "paused": bool(raw.get("paused", False)),
            "last_update_id": int(raw.get("last_update_id", 0)),
            "open_trades": [],
            "closed_trades": [],
            "last_prices": raw.get("last_prices", {}) if isinstance(raw.get("last_prices"), dict) else {}
        }

        raw_open = raw.get("open_trades", [])
        if isinstance(raw_open, dict):
            for k, v in raw_open.items():
                nt = normalize_trade(v, fallback_pair=k)
                if nt and nt["entry"] > 0 and nt["target"] > 0:
                    state["open_trades"].append(nt)
        elif isinstance(raw_open, list):
            for item in raw_open:
                nt = normalize_trade(item)
                if nt and nt["entry"] > 0 and nt["target"] > 0:
                    state["open_trades"].append(nt)

        raw_closed = raw.get("closed_trades", [])
        if isinstance(raw_closed, list):
            for item in raw_closed:
                nt = normalize_trade(item)
                # Filter out invalid 0.0 migration artifacts
                if nt and nt["exit"] > 0:
                    state["closed_trades"].append(nt)

        return state
    except Exception as e:
        print(f"State Load Warning: {e}")
        return default_state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram(text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = urllib.parse.urlencode(payload).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        print(f"Telegram Send Error: {e}")


def send_excel_report(state):
    try:
        import openpyxl
        wb = openpyxl.Workbook()

        ws_sum = wb.active
        ws_sum.title = "Summary"
        closed = state["closed_trades"]
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        total_pnl = sum(t.get("pnl", 0) for t in closed)
        win_rate = (len(wins) / len(closed) * 100) if closed else 0.0

        ws_sum.append(["FINSIDE ZING FOREX & GOLD REPORT", now_ist().strftime("%Y-%m-%d %H:%M IST")])
        ws_sum.append([])
        ws_sum.append(["Initial Capital ($)", INITIAL_CAPITAL])
        ws_sum.append(["Current Balance ($)", round(state["balance"], 2)])
        ws_sum.append(["Total Net P&L ($)", round(total_pnl, 2)])
        ws_sum.append(["Total Closed Trades", len(closed)])
        ws_sum.append(["Win Rate (%)", round(win_rate, 2)])
        ws_sum.append(["Active Open Trades", len(state["open_trades"])])

        ws_open = wb.create_sheet("Open Trades")
        ws_open.append(["Pair", "Direction", "Strategy", "Session", "Entry", "SL", "Target", "Units", "Opened At"])
        for t in state["open_trades"]:
            ws_open.append([
                t["pair"], t["side"], t.get("strategy", ""), t.get("session", ""),
                t["entry"], t["sl"], t["target"], t["units"], t["time"]
            ])

        ws_closed = wb.create_sheet("Closed History")
        ws_closed.append(["Pair", "Direction", "Strategy", "Entry", "Exit", "P&L ($)", "Reason", "Closed At"])
        for t in closed:
            ws_closed.append([
                t["pair"], t["side"], t.get("strategy", ""),
                t["entry"], t.get("exit", ""), round(t.get("pnl", 0), 2),
                t.get("reason", ""), t.get("closed_time", "")
            ])

        filename = f"FINSIDE_Forex_Report_{now_ist().strftime('%Y%m%d_%H%M')}.xlsx"
        wb.save(filename)

        boundary = "----FinsideBoundary7MA4YWxkTrZu0gW"
        with open(filename, "rb") as f:
            file_bytes = f.read()

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{TELEGRAM_CHAT_ID}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n📊 FINSIDE ZING Forex & Gold Performance Report\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
        ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()

        if os.path.exists(filename):
            os.remove(filename)
    except Exception as e:
        send_telegram(f"⚠️ Excel Report Error: {e}")


def get_menu_keyboard():
    return {
        "keyboard": [
            [{"text": "/status"}, {"text": "/open"}, {"text": "/virtual"}],
            [{"text": "/pnl"}, {"text": "/excel"}, {"text": "/closeall"}],
            [{"text": "/pause"}, {"text": "/resume"}]
        ],
        "resize_keyboard": True
    }


def process_telegram_commands(state):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    offset = state.get("last_update_id", 0) + 1
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=5"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Telegram Poll Error: {e}")
        return

    if not data.get("ok"):
        return

    for update in data.get("result", []):
        state["last_update_id"] = max(state.get("last_update_id", 0), update["update_id"])
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip().lower()

        if chat_id != str(TELEGRAM_CHAT_ID) or not text:
            continue

        cmd = text.split()[0]

        if cmd in ["/start", "/menu", "/help"]:
            send_telegram(
                "🤖 <b>FINSIDE ZING FOREX & GOLD BOT</b>\n"
                "Choose an option below:",
                reply_markup=get_menu_keyboard()
            )

        elif cmd == "/status":
            status_icon = "⏸ PAUSED" if state["paused"] else "🟢 ACTIVE (24/5)"
            prices_txt = "\n".join(
                [f"• <b>{p}:</b> {state['last_prices'].get(p, 'N/A')}" for p in PAIRS]
            )
            send_telegram(
                f"📡 <b>FINSIDE FOREX STATUS</b>\n"
                f"State: <b>{status_icon}</b>\n"
                f"Session: <b>{get_session_name(now_ist())}</b>\n"
                f"Open Trades: <b>{len(state['open_trades'])}</b>\n\n"
                f"<b>Latest Tracked Prices:</b>\n{prices_txt}",
                reply_markup=get_menu_keyboard()
            )

        elif cmd == "/open":
            if not state["open_trades"]:
                send_telegram("📭 <b>No Open Forex Positions</b> right now.")
            else:
                lines = ["📂 <b>OPEN FOREX POSITIONS</b>\n"]
                for idx, t in enumerate(state["open_trades"], 1):
                    pair_name = t.get("pair", "UNKNOWN")
                    entry_px = float(t.get("entry", 0.0))
                    curr = float(state["last_prices"].get(pair_name, entry_px))
                    diff = (curr - entry_px) if t.get("side") == "BUY" else (entry_px - curr)
                    float_pnl = diff * float(t.get("units", 0.0))
                    lines.append(
                        f"{idx}. <b>{pair_name} ({t.get('side', 'BUY')})</b>\n"
                        f"   Entry: {entry_px} | Live: {curr}\n"
                        f"   SL: {t.get('sl', 0)} | Target: {t.get('target', 0)}\n"
                        f"   Floating P&L: <b>${float_pnl:+.2f}</b>"
                    )
                send_telegram("\n".join(lines))

        elif cmd == "/virtual":
            floating_total = 0.0
            for t in state["open_trades"]:
                pair_name = t.get("pair", "UNKNOWN")
                entry_px = float(t.get("entry", 0.0))
                curr = float(state["last_prices"].get(pair_name, entry_px))
                diff = (curr - entry_px) if t.get("side") == "BUY" else (entry_px - curr)
                floating_total += diff * float(t.get("units", 0.0))
            equity = state["balance"] + floating_total
            send_telegram(
                f"💼 <b>VIRTUAL WALLET SUMMARY</b>\n"
                f"Initial Capital: <b>${INITIAL_CAPITAL:,.2f}</b>\n"
                f"Cash Balance: <b>${state['balance']:,.2f}</b>\n"
                f"Floating P&L: <b>${floating_total:+.2f}</b>\n"
                f"Net Equity: <b>${equity:,.2f}</b>"
            )

        elif cmd == "/pnl":
            closed = state["closed_trades"]
            today_str = now_ist().strftime("%Y-%m-%d")
            today_trades = [t for t in closed if t.get("closed_time", "").startswith(today_str)]
            today_pnl = sum(t.get("pnl", 0) for t in today_trades)
            total_pnl = sum(t.get("pnl", 0) for t in closed)
            wins = len([t for t in closed if t.get("pnl", 0) > 0])
            win_rate = (wins / len(closed) * 100) if closed else 0.0

            send_telegram(
                f"📈 <b>P&L PERFORMANCE</b>\n"
                f"Today's Closed P&L: <b>${today_pnl:+.2f}</b> ({len(today_trades)} trades)\n"
                f"All-Time Net P&L: <b>${total_pnl:+.2f}</b>\n"
                f"Total Closed: <b>{len(closed)}</b> | Win Rate: <b>{win_rate:.1f}%</b>"
            )

        elif cmd == "/excel":
            send_telegram("⏳ Generating Excel performance report...")
            send_excel_report(state)

        elif cmd == "/closeall":
            if not state["open_trades"]:
                send_telegram("ℹ️ No open trades to close.")
            else:
                closed_count = 0
                total_realized = 0.0
                for t in state["open_trades"]:
                    pair_name = t.get("pair", "UNKNOWN")
                    entry_px = float(t.get("entry", 0.0))
                    exit_px = float(state["last_prices"].get(pair_name, entry_px))
                    diff = (exit_px - entry_px) if t.get("side") == "BUY" else (entry_px - exit_px)
                    pnl = round(diff * float(t.get("units", 0.0)), 2)
                    state["balance"] = round(state["balance"] + pnl, 2)
                    t["exit"] = exit_px
                    t["pnl"] = pnl
                    t["reason"] = "Manual /closeall"
                    t["closed_time"] = now_ist().strftime("%Y-%m-%d %H:%M IST")
                    state["closed_trades"].append(t)
                    total_realized += pnl
                    closed_count += 1
                state["open_trades"] = []
                send_telegram(
                    f"🛑 <b>ALL POSITIONS CLOSED</b>\n"
                    f"Closed Trades: <b>{closed_count}</b>\n"
                    f"Realized P&L: <b>${total_realized:+.2f}</b>\n"
                    f"Updated Balance: <b>${state['balance']:,.2f}</b>"
                )

        elif cmd == "/pause":
            state["paused"] = True
            send_telegram("⏸ <b>New Entries PAUSED.</b> Existing positions will still be monitored for SL/Target.")

        elif cmd == "/resume":
            state["paused"] = False
            send_telegram("▶️ <b>Bot RESUMED.</b> Scanning for new Forex & Gold setups.")


def fetch_candles(symbol, interval):
    url = (
        f"https://api.twelvedata.com/time_series?"
        f"symbol={urllib.parse.quote(symbol)}&interval={interval}&outputsize=35&apikey={TWELVE_DATA_API_KEY}"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != "ok" or "values" not in data:
            print(f"Fetch Error ({symbol} {interval}): {data.get('message', 'Unknown error')}")
            return None
        candles = list(reversed(data["values"]))
        for c in candles:
            c["open"] = float(c["open"])
            c["high"] = float(c["high"])
            c["low"] = float(c["low"])
            c["close"] = float(c["close"])
        return candles
    except Exception as e:
        print(f"Fetch Error ({symbol} {interval}): {e}")
        return None


def calc_ema(closes, period):
    if len(closes) < period:
        return closes[-1]
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return candles[-1]["high"] - candles[-1]["low"]
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def manage_open_trades(state, pair, curr_price):
    remaining = []
    for t in state["open_trades"]:
        if t.get("pair") != pair:
            remaining.append(t)
            continue

        hit_reason = None
        exit_px = curr_price
        sl_px = float(t.get("sl", 0.0))
        tp_px = float(t.get("target", 0.0))

        if t["side"] == "BUY":
            if sl_px > 0 and curr_price <= sl_px:
                hit_reason = "Stop-Loss Hit"
                exit_px = sl_px
            elif tp_px > 0 and curr_price >= tp_px:
                hit_reason = "Target Hit 🎯"
                exit_px = tp_px
        else:
            if sl_px > 0 and curr_price >= sl_px:
                hit_reason = "Stop-Loss Hit"
                exit_px = sl_px
            elif tp_px > 0 and curr_price <= tp_px:
                hit_reason = "Target Hit 🎯"
                exit_px = tp_px

        if hit_reason:
            diff = (exit_px - t["entry"]) if t["side"] == "BUY" else (t["entry"] - exit_px)
            pnl = round(diff * t["units"], 2)
            state["balance"] = round(state["balance"] + pnl, 2)
            t["exit"] = exit_px
            t["pnl"] = pnl
            t["reason"] = hit_reason
            t["closed_time"] = now_ist().strftime("%Y-%m-%d %H:%M IST")
            state["closed_trades"].append(t)

            emoji = "✅" if pnl > 0 else "❌"
            send_telegram(
                f"{emoji} <b>TRADE CLOSED: {pair} ({t['side']})</b>\n"
                f"Reason: <b>{hit_reason}</b>\n"
                f"Entry: {t['entry']} | Exit: {exit_px}\n"
                f"Realized P&L: <b>${pnl:+.2f}</b>\n"
                f"New Balance: <b>${state['balance']:,.2f}</b>"
            )
        else:
            remaining.append(t)

    state["open_trades"] = remaining


def evaluate_signal(pair, c15, c1h, state):
    if state["paused"]:
        return
    if any(t.get("pair") == pair for t in state["open_trades"]):
        return

    closes_15 = [c["close"] for c in c15]
    closes_1h = [c["close"] for c in c1h]

    ema9_15 = calc_ema(closes_15, 9)
    ema21_15 = calc_ema(closes_15, 21)
    prev_ema9 = calc_ema(closes_15[:-1], 9)
    prev_ema21 = calc_ema(closes_15[:-1], 21)

    ema21_1h = calc_ema(closes_1h, 21)
    curr_price = closes_15[-1]
    atr = calc_atr(c15, 14)

    decimals = 2 if "XAU" in pair else (3 if "JPY" in pair else 5)
    side = None
    strategy = None

    if curr_price > ema21_1h and prev_ema9 <= prev_ema21 and ema9_15 > ema21_15:
        side = "BUY"
        strategy = "1H Trend + 15M EMA Cross"
    elif curr_price < ema21_1h and prev_ema9 >= prev_ema21 and ema9_15 < ema21_15:
        side = "SELL"
        strategy = "1H Trend + 15M EMA Cross"

    if not side or atr <= 0:
        return

    sl_dist = round(atr * 1.5, decimals)
    tp_dist = round(atr * 2.5, decimals)
    if sl_dist <= 0 or tp_dist <= 0:
        return

    entry = round(curr_price, decimals)
    sl = round(entry - sl_dist if side == "BUY" else entry + sl_dist, decimals)
    target = round(entry + tp_dist if side == "BUY" else entry - tp_dist, decimals)

    risk_dollars = state["balance"] * RISK_PER_TRADE_PCT
    units = round(risk_dollars / sl_dist, 2)
    session = get_session_name(now_ist())

    trade = {
        "pair": pair,
        "side": side,
        "strategy": strategy,
        "session": session,
        "entry": entry,
        "sl": sl,
        "target": target,
        "units": units,
        "time": now_ist().strftime("%Y-%m-%d %H:%M IST")
    }
    state["open_trades"].append(trade)

    send_telegram(
        f"🚨 <b>NEW FOREX SIGNAL: {pair}</b>\n"
        f"Action: <b>{side}</b> | Strategy: {strategy}\n"
        f"Session: {session}\n"
        f"Entry: <b>{entry}</b>\n"
        f"Stop-Loss: <b>{sl}</b>\n"
        f"Target: <b>{target}</b>\n"
        f"Risk: ${risk_dollars:.2f} (1%)"
    )


def main():
    print(f"[{now_ist().strftime('%Y-%m-%d %H:%M IST')}] Running 5-Min Forex Scan...")
    state = load_state()

    # 1. Fetch fresh live prices & evaluate setups first
    for idx, pair in enumerate(PAIRS):
        c15 = fetch_candles(pair, "15min")
        time.sleep(8)
        c1h = fetch_candles(pair, "1h")
        if idx < len(PAIRS) - 1:
            time.sleep(8)

        if not c15 or not c1h:
            continue

        curr_price = c15[-1]["close"]
        state["last_prices"][pair] = curr_price

        manage_open_trades(state, pair, curr_price)
        evaluate_signal(pair, c15, c1h, state)

    # 2. Reply to Telegram commands using the freshly updated live prices
    process_telegram_commands(state)

    save_state(state)
    print("5-Min Forex Scan Completed Successfully.")


if __name__ == "__main__":
    main()
