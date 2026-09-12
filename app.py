import os
import json
import time
import hmac
import hashlib
import threading
import smtplib
import socket
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify, request, render_template_string

# ============================================================
# CONSTANTS & CONFIGURATION
# ============================================================

app = Flask(__name__)

DELTA_BASE = "https://api.india.delta.exchange"
STATE_FILE = "bot_state.json"

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
CMC_API_KEY = os.getenv("CMC_API_KEY", "").strip()

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "").strip()
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip()
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER", EMAIL_SENDER).strip()

USER_AGENT = "PrimeMinisterAI/Production-Sniper"

SCAN_SECONDS = 5
PRODUCT_CACHE_SECONDS = 3600
IST = timezone(timedelta(hours=5, minutes=30))

SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"]

# ============================================================
# STATE & CACHE
# ============================================================

lock = threading.RLock()
engine_thread = None
engine_socket_lock = None 

product_cache = {"timestamp": 0, "data": []}

runtime = {
    "started_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    "last_scan": 0,
    "last_scan_error": None,
    "public_ip": "Loading...",
    "is_scanning": False
}

backend_notified_signals = {symbol: None for symbol in SYMBOLS}

def default_coin_state():
    return {
        "enabled": False, "quantity": 1, "leverage": 2,
        "armed_signal": None,  
        "last_signal": {
            "side": "NO_TRADE", "score": 0, "price": None, 
            "high": None, "low": None, # ADDED HIGH/LOW FOR SNIPER PRECISION
            "trigger_price": None, "reason": "Waiting...", "updated_at": None
        }
    }

def default_state():
    return {
        "system_on": False, "selected_coin": None, "mode": "PAPER", "current_trade": None,
        "events": [], "coins": {s: default_coin_state() for s in SYMBOLS}
    }

def load_state():
    if not os.path.exists(STATE_FILE): return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f: state = json.load(f)
        base = default_state()
        for k in base:
            if k not in state: state[k] = base[k]
        for s in SYMBOLS:
            if s not in state["coins"]: state["coins"][s] = default_coin_state()
            for k, v in default_coin_state().items():
                if k not in state["coins"][s]: state["coins"][s][k] = v
        return state
    except Exception: return default_state()

state = load_state()

def save_state():
    with lock:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)

def event(message):
    timestamp = datetime.now(IST).strftime("%H:%M:%S")
    full_message = f"{timestamp} — {message}"
    with lock:
        state["events"].insert(0, {"time": datetime.now(IST).isoformat(), "message": full_message})
        state["events"] = state["events"][:100]
    save_state()

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def send_email_alert(symbol, side, trigger_price):
    if not EMAIL_SENDER or not EMAIL_PASSWORD: return 
    tp_str = f"{trigger_price:.8f}".rstrip('0').rstrip('.') if trigger_price else str(trigger_price)
    subject = f"🚀 {symbol} {side} Setup Ready!"
    body = f"Prime Minister AI Alert\n\nCoin: {symbol}\nSetup: {side} (Score 75+)\nTarget Breakout: {tp_str}\n\nOpen dashboard to ARM the coin."
    msg = MIMEText(body)
    msg['Subject'], msg['From'], msg['To'] = subject, "Prime Minister AI", EMAIL_RECEIVER
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
    except Exception: pass 

def get_public_ip():
    try:
        res = requests.get("https://api.ipify.org?format=json", timeout=5)
        ip = res.json().get("ip")
        if ip: runtime["public_ip"] = ip
    except Exception:
        if "Loading" in runtime.get("public_ip", ""): runtime["public_ip"] = "IP Loading Error (Safe to ignore)"

def round_to_tick(price, tick_size):
    if price is None or tick_size is None or tick_size <= 0: return price
    return round(round(price / tick_size) * tick_size, 8)

# ============================================================
# DELTA API CORE
# ============================================================

def delta_request(method, path, params=None, body=None, authenticated=False):
    url = DELTA_BASE + path
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    body_text = json.dumps(body, separators=(",", ":")) if body else ""
    if body: headers["Content-Type"] = "application/json"

    if authenticated:
        if not API_KEY or not API_SECRET: raise RuntimeError("API keys missing")
        timestamp = str(int(time.time()))
        query_string = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())]) if params else ""
        msg = method.upper() + timestamp + path + query_string + body_text
        signature = hmac.new(API_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
        headers.update({"api-key": API_KEY, "timestamp": timestamp, "signature": signature})

    res = requests.request(method.upper(), url, params=params, data=body_text if body else None, headers=headers, timeout=10)
    
    if res.status_code >= 400:
        err = res.text
        try: err = res.json().get("error", {}).get("message", err)
        except Exception: pass
        raise RuntimeError(f"HTTP {res.status_code}: {err}")
    
    data = res.json()
    if isinstance(data, dict) and str(data.get("success", "")).lower() == "false":
        raise RuntimeError(data.get("error", "API Error"))
    return data

def get_products():
    now = time.time()
    if product_cache["data"] and (now - product_cache["timestamp"] < PRODUCT_CACHE_SECONDS):
        return product_cache["data"]
    data = delta_request("GET", "/v2/products").get("result", [])
    product_cache["data"] = data
    product_cache["timestamp"] = now
    return data

def product_rules(symbol):
    for p in get_products():
        if str(p.get("symbol", "")).upper() == symbol.upper():
            return {
                "id": p.get("id"),
                "contract_value": float(p.get("contract_value", 1)),
                "quantity_step": float(p.get("size_increment") or p.get("step_size") or 1),
                "tick_size": float(p.get("tick_size") or 0.001),
                "min_leverage": float(p.get("min_leverage") or 1),
                "max_leverage": float(p.get("max_leverage") or p.get("leverage") or 100)
            }
    return {"id": None, "quantity_step": 1, "tick_size": 0.001, "contract_value": 1, "min_leverage": 1, "max_leverage": 100}

def get_candles(symbol, resolution="5m", limit=100):
    end_time = int(time.time())
    start_time = end_time - ((limit + 10) * (3600 if resolution == "1h" else 300))
    res = delta_request("GET", "/v2/history/candles", params={"symbol": symbol, "resolution": resolution, "start": start_time, "end": end_time})
    candles = []
    for item in res.get("result", []):
        try:
            if isinstance(item, dict): candles.append({"time": float(item["time"]), "close": float(item["close"]), "high": float(item["high"]), "low": float(item["low"]), "volume": float(item.get("volume", 0))})
            else: candles.append({"time": float(item[0]), "close": float(item[4]), "high": float(item[2]), "low": float(item[3]), "volume": float(item[5]) if len(item)>5 else 0})
        except Exception: continue
    candles.sort(key=lambda x: x["time"])
    return candles[-limit:]

def get_position(symbol):
    try:
        for pos in delta_request("GET", "/v2/positions", authenticated=True).get("result", []):
            if str(pos.get("product_symbol", "")).upper() == symbol.upper() and abs(float(pos.get("size", 0))) > 0:
                return pos
        return "NO_POSITION"
    except Exception: return None

def cancel_specific_sl(symbol, sl_order_id):
    if not sl_order_id: return
    try:
        delta_request("DELETE", "/v2/orders", body={"id": sl_order_id, "product_symbol": symbol}, authenticated=True)
    except Exception: pass

# ============================================================
# STRATEGY LOGIC
# ============================================================

def ema(values, period):
    if len(values) < period: return None
    mult = 2 / (period + 1)
    curr = sum(values[:period]) / period
    for p in values[period:]: curr = ((p - curr) * mult + curr)
    return curr

def rsi(values, period=14):
    if len(values) < period + 1: return None
    gains, losses = [], []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(change if change >= 0 else 0)
        losses.append(abs(change) if change < 0 else 0)
    avg_g, avg_l = sum(gains)/period, sum(losses)/period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(change, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-change, 0)) / period
    return 100 if avg_l == 0 else 100 - (100 / (1 + (avg_g / avg_l)))

def atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])) for c, p in zip(candles[1:], candles[:-1])]
    curr = sum(trs[:period]) / period
    for tr in trs[period:]: curr = (curr * (period - 1) + tr) / period
    return curr

def calculate_signal(symbol):
    candles_1h = get_candles(symbol, "1h", 60)
    trend_1h = "NEUTRAL"
    if len(candles_1h) > 21:
        c1h = [x["close"] for x in candles_1h]
        e9, e21 = ema(c1h, 9), ema(c1h, 21)
        if e9 and e21: trend_1h = "UP" if e9 > e21 else "DOWN"

    candles_5m = get_candles(symbol, "5m", 160)
    if len(candles_5m) < 60: return {"side": "NO_TRADE", "score": 0, "price": None, "high": None, "low": None, "reason": "Loading Data"}
    
    closes, volumes = [x["close"] for x in candles_5m], [x["volume"] for x in candles_5m]
    curr_candle = candles_5m[-1]
    price, high, low = curr_candle["close"], curr_candle["high"], curr_candle["low"]
    
    e9, e21, rsi_v, atr_v = ema(closes, 9), ema(closes, 21), rsi(closes, 14), atr(candles_5m, 14)
    if None in (e9, e21, rsi_v, atr_v): return {"side": "NO_TRADE", "score": 0, "price": price, "high": high, "low": low, "reason": "Calc Indicators"}

    ls, ss, rl, rs = 0, 0, [], []

    if trend_1h == "UP": ls += 20; rl.append("1H UP")
    elif trend_1h == "DOWN": ss += 20; rs.append("1H DOWN")

    if e9 > e21: ls += 20; rl.append("5M Bull")
    elif e9 < e21: ss += 20; rs.append("5M Bear")

    if closes[-1] > closes[-4]: ls += 10
    elif closes[-1] < closes[-4]: ss += 10

    if 52 <= rsi_v <= 68: ls += 15
    if 32 <= rsi_v <= 48: ss += 15

    ph, pl = max(x["high"] for x in candles_5m[-21:-1]), min(x["low"] for x in candles_5m[-21:-1])
    if price > ph: ls += 15; rl.append("Breakout")
    if price < pl: ss += 15; rs.append("Breakdown")

    rv, bv = sum(volumes[-5:])/5, sum(volumes[-25:-5])/20 if len(volumes)>=25 else 1
    if bv > 0 and (rv/bv) >= 1.2:
        if ls > ss: ls += 15; rl.append("Vol")
        elif ss > ls: ss += 15; rs.append("Vol")

    rh, rl_m = max(x["high"] for x in candles_5m[-4:-1]), min(x["low"] for x in candles_5m[-4:-1])
    tick = product_rules(symbol).get("tick_size", 0.001)

    if ls >= 75 and ls >= ss + 15 and trend_1h != "DOWN":
        tp = round_to_tick(rh + (atr_v * 0.1), tick)
        return {"side": "LONG", "score": min(ls, 100), "price": price, "high": high, "low": low, "trigger_price": tp, "reason": ", ".join(rl), "atr": atr_v}
    elif ss >= 75 and ss >= ls + 15 and trend_1h != "UP":
        tp = round_to_tick(rl_m - (atr_v * 0.1), tick)
        return {"side": "SHORT", "score": min(ss, 100), "price": price, "high": high, "low": low, "trigger_price": tp, "reason": ", ".join(rs), "atr": atr_v}
    
    return {"side": "NO_TRADE", "score": max(ls, ss), "price": price, "high": high, "low": low, "trigger_price": None, "reason": "No Edge", "atr": atr_v}

# ============================================================
# EXECUTION & TRADE MANAGEMENT
# ============================================================

def execute_trade(symbol, mode, armed_data, execute_price):
    try:
        rules = product_rules(symbol)
        raw_qty = float(state["coins"][symbol]["quantity"])
        lev = float(state["coins"][symbol]["leverage"])
        
        step_size = float(rules.get("quantity_step", 1))
        tick_size = float(rules.get("tick_size", 0.001))
        qty = max(step_size, round(raw_qty / step_size) * step_size)

        side, atr_val = armed_data["side"], armed_data["atr"]
        
        sl_raw = execute_price - (atr_val * 1.5) if side == "LONG" else execute_price + (atr_val * 1.5)
        stop_price = round_to_tick(sl_raw, tick_size)

        sl_order_id = None

        if mode == "LIVE":
            if get_position(symbol) != "NO_POSITION":
                event(f"⚠️ Skipped: Live Position already exists for {symbol}.")
                return False
                
            delta_request("POST", f"/v2/products/{rules['id']}/orders/leverage", body={"leverage": int(lev)}, authenticated=True)
            
            payload = {
                "product_symbol": symbol, "size": qty, "side": "buy" if side == "LONG" else "sell",
                "order_type": "market_order", "bracket_stop_loss_price": str(stop_price)
            }
            res = delta_request("POST", "/v2/orders", body=payload, authenticated=True)
            if isinstance(res, dict) and "result" in res:
                sl_order_id = "BRACKET_PENDING_SYNC" 
            result = "LIVE_BRACKET_PLACED"
        else: result = "PAPER_SIMULATION"

        with lock:
            state["current_trade"] = {
                "symbol": symbol, "side": side, "entry": execute_price, "quantity": qty,
                "leverage": lev, "stop": stop_price, "entry_atr": atr_val,
                "highest_price": execute_price, "lowest_price": execute_price, "mode": mode,
                "sl_order_id": sl_order_id,
                "opened_at": datetime.now(IST).isoformat()
            }
            state["coins"][symbol]["armed_signal"] = None
        
        save_state()
        tp_str = f"{stop_price:.8f}".rstrip('0').rstrip('.')
        event(f"🚀 {mode} EXECUTED: {symbol} {side} @ {execute_price}. SL: {tp_str}")
        return True
    except Exception as e:
        event(f"❌ Execution Error on {symbol}: {e}")
        return False

def sync_true_stateless():
    if not API_KEY: return
    try:
        positions = delta_request("GET", "/v2/positions", authenticated=True).get("result", [])
        active_pos = None
        for p in positions:
            if abs(float(p.get("size", 0))) > 0:
                active_pos = p
                break
        
        with lock:
            if active_pos:
                sym = active_pos.get("product_symbol")
                size = float(active_pos.get("size", 0))
                side = "LONG" if size > 0 else "SHORT"
                entry = float(active_pos.get("entry_price", 0))
                
                # Fetch open orders to find the active Stop Loss (Fixing the Duplicate SL Bug)
                open_orders = delta_request("GET", "/v2/orders", params={"state": "open", "product_symbol": sym}, authenticated=True).get("result", [])
                sl_id = None
                sl_price = entry
                for o in open_orders:
                    if o.get("order_type") == "stop_order":
                        sl_id = o.get("id")
                        sl_price = float(o.get("stop_price", entry))
                        break

                if not state["current_trade"] or state["current_trade"]["symbol"] != sym:
                    state["current_trade"] = {
                        "symbol": sym, "side": side, "entry": entry, "quantity": abs(size),
                        "leverage": 1, "stop": sl_price, "entry_atr": 0.001, 
                        "highest_price": entry, "lowest_price": entry, "mode": "LIVE",
                        "sl_order_id": sl_id, "opened_at": datetime.now(IST).isoformat()
                    }
                    event(f"🔗 Recovered Orphan Trade: {sym}. Synced SL: {sl_price}")
            else:
                if state["current_trade"] and state["current_trade"]["mode"] == "LIVE":
                    state["current_trade"] = None
                    event("🧹 Cleared stale local trade (No live positions found).")
        save_state()
    except Exception: pass

def manage_active_trade():
    with lock: trade = state["current_trade"]
    if not trade: return
    symbol = trade["symbol"]
    rules = product_rules(symbol)
    tick_size = float(rules.get("tick_size", 0.001))
    
    try:
        if trade["mode"] == "LIVE":
            pos = get_position(symbol)
            if pos == "NO_POSITION":
                cancel_specific_sl(symbol, trade.get("sl_order_id"))
                with lock: state["current_trade"] = None
                save_state()
                event(f"🔒 {symbol} Trade closed on exchange (Target/SL hit).")
                return
            elif pos is None: return

        candles = get_candles(symbol, "5m", 10)
        if not candles: return
        live_price, side, atr_val = candles[-1]["close"], trade["side"], trade.get("entry_atr", 10)

        # PAPER MODE SL CHECK
        if trade["mode"] == "PAPER":
            if (side == "LONG" and live_price <= trade["stop"]) or (side == "SHORT" and live_price >= trade["stop"]):
                with lock: state["current_trade"] = None
                save_state()
                event(f"🔒 PAPER {symbol} Trade closed (Stop Loss Hit).")
                return

        # TRAILING STOP LOSS LOGIC
        with lock:
            if side == "LONG":
                if live_price > trade.get("highest_price", trade["entry"]):
                    trade["highest_price"] = live_price
                    new_sl = round_to_tick(live_price - (atr_val * 1.5), tick_size)
                    if new_sl > trade["stop"]:
                        trade["stop"] = new_sl
                        if trade["mode"] == "LIVE":
                            cancel_specific_sl(symbol, trade.get("sl_order_id"))
                            res = delta_request("POST", "/v2/orders", body={"product_symbol": symbol, "size": trade["quantity"], "side": "sell", "order_type": "stop_order", "stop_price": str(new_sl), "reduce_only": True}, authenticated=True)
                            trade["sl_order_id"] = res.get("result", {}).get("id") if isinstance(res, dict) else None
                        event(f"📈 Trailing SL Moved UP to: {new_sl}")

            elif side == "SHORT":
                if live_price < trade.get("lowest_price", trade["entry"]):
                    trade["lowest_price"] = live_price
                    new_sl = round_to_tick(live_price + (atr_val * 1.5), tick_size)
                    if new_sl < trade["stop"]:
                        trade["stop"] = new_sl
                        if trade["mode"] == "LIVE":
                            cancel_specific_sl(symbol, trade.get("sl_order_id"))
                            res = delta_request("POST", "/v2/orders", body={"product_symbol": symbol, "size": trade["quantity"], "side": "buy", "order_type": "stop_order", "stop_price": str(new_sl), "reduce_only": True}, authenticated=True)
                            trade["sl_order_id"] = res.get("result", {}).get("id") if isinstance(res, dict) else None
                        event(f"📉 Trailing SL Moved DOWN to: {new_sl}")
        save_state()
    except Exception as exc: 
        event(f"Trade sync error: {exc}")

def close_trade_manual():
    with lock: trade = state["current_trade"]
    if not trade: return False
    
    if trade["mode"] == "LIVE":
        try:
            cancel_specific_sl(trade["symbol"], trade.get("sl_order_id"))
            pos = get_position(trade["symbol"])
            if pos and pos != "NO_POSITION":
                actual_size = float(pos["size"])
                side_to_close = "sell" if actual_size > 0 else "buy"
                
                delta_request("POST", "/v2/orders", body={
                    "product_symbol": trade["symbol"], "size": abs(actual_size), 
                    "side": side_to_close, "order_type": "market_order", "reduce_only": True
                }, authenticated=True)
        except Exception as e:
            event(f"❌ Failed to close LIVE trade: {e}")
            return False
            
    event(f"🔒 {trade['mode']} trade closed manually via Dashboard.")
    with lock: state["current_trade"] = None
    save_state()
    return True

# ============================================================
# BACKGROUND ENGINE
# ============================================================

def engine_loop():
    get_public_ip()
    sync_true_stateless()
    event("🟢 Master Sniper Engine Started.")
    
    while True:
        runtime["is_scanning"] = True
        try:
            has_err = False
            for sym in SYMBOLS:
                try:
                    sig = calculate_signal(sym)
                    with lock: state["coins"][sym]["last_signal"] = sig
                    
                    if sig["score"] >= 75 and sig["side"] in ["LONG", "SHORT"] and backend_notified_signals[sym] != sig["side"]:
                        backend_notified_signals[sym] = sig["side"]
                        threading.Thread(target=send_email_alert, args=(sym, sig["side"], sig["trigger_price"]), daemon=True).start()
                    elif sig["side"] == "NO_TRADE": backend_notified_signals[sym] = None
                    time.sleep(1)
                except Exception as e:
                    has_err = True
                    runtime["last_scan_error"] = f"{sym} Sync Error"
                    time.sleep(1)
            
            if not has_err: runtime["last_scan_error"] = None

            with lock: trade = state["current_trade"]
            if trade: 
                manage_active_trade()
            elif state["system_on"]:
                sel = state["selected_coin"]
                if sel and state["coins"][sel]["enabled"] and state["coins"][sel].get("armed_signal"):
                    arm = state["coins"][sel]["armed_signal"]
                    
                    if time.time() > arm["expiry_time"]:
                        with lock: state["coins"][sel]["enabled"], state["coins"][sel]["armed_signal"], state["selected_coin"] = False, None, None
                        event(f"⏳ {sel} Setup EXPIRED (20 mins). Disarmed.")
                    else:
                        sig_data = state["coins"][sel]["last_signal"]
                        # FIX: SNIPER WICK BUG RESOLVED (Now checks High/Low instead of just Close)
                        current_close = sig_data.get("price")
                        current_high = sig_data.get("high")
                        current_low = sig_data.get("low")
                        
                        if current_close and current_high and current_low:
                            if arm["side"] == "LONG" and current_high >= arm["trigger_price"]:
                                # Enter at the trigger price if it crossed, or close if it opened above
                                exec_price = max(arm["trigger_price"], current_close) if current_close < arm["trigger_price"] else current_close
                                execute_trade(sel, state["mode"], arm, exec_price)
                            elif arm["side"] == "SHORT" and current_low <= arm["trigger_price"]:
                                exec_price = min(arm["trigger_price"], current_close) if current_close > arm["trigger_price"] else current_close
                                execute_trade(sel, state["mode"], arm, exec_price)
        except Exception: pass
        finally:
            runtime["is_scanning"] = False
            runtime["last_scan"] = time.time()
            save_state()
            time.sleep(SCAN_SECONDS)

def start_engine():
    global engine_thread, engine_socket_lock
    try:
        engine_socket_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        engine_socket_lock.bind(("127.0.0.1", 47500))
    except socket.error:
        return

    if engine_thread is None or not engine_thread.is_alive():
        engine_thread = threading.Thread(target=engine_loop, daemon=True)
        engine_thread.start()

start_engine()

# ============================================================
# API ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def index(): return render_template_string(HTML)

@app.route("/api/state", methods=["GET"])
def api_state():
    with lock: snap = json.loads(json.dumps(state))
    snap["runtime"] = runtime
    snap["engine_running"] = bool(time.time() - runtime["last_scan"] < 25)
    return jsonify(snap)

@app.route("/api/coin/<symbol>/rules", methods=["GET"])
def api_rules(symbol):
    r = product_rules(symbol.upper())
    return jsonify({"success": r.get("id") is not None, "rules": r})

@app.route("/api/coin/<symbol>/settings", methods=["POST"])
def api_settings(symbol):
    data = request.get_json(silent=True) or {}
    with lock: state["coins"][symbol.upper()]["quantity"], state["coins"][symbol.upper()]["leverage"] = float(data.get("quantity", 1)), float(data.get("leverage", 1))
    save_state()
    return jsonify({"success": True})

@app.route("/api/coin/<symbol>/toggle", methods=["POST"])
def api_toggle(symbol):
    if request.get_json(silent=True).get("enabled"):
        sig = state["coins"][symbol.upper()]["last_signal"]
        if sig["side"] == "NO_TRADE": 
            return jsonify({"success": False, "error": f"Cannot Arm: Score is {sig.get('score', 0)} (Need 75+ for Sniper Entry)."}), 400
        with lock:
            for s in SYMBOLS: state["coins"][s]["enabled"] = (s == symbol.upper())
            state["selected_coin"] = symbol.upper()
            state["coins"][symbol.upper()]["enabled"] = True
            state["coins"][symbol.upper()]["armed_signal"] = {"side": sig["side"], "trigger_price": sig["trigger_price"], "expiry_time": time.time() + 1200, "atr": sig["atr"]}
        tp_str = f"{sig['trigger_price']:.8f}".rstrip('0').rstrip('.')
        event(f"🔫 {symbol.upper()} ARMED. Target: {tp_str}")
    else:
        with lock: state["coins"][symbol.upper()]["enabled"], state["coins"][symbol.upper()]["armed_signal"], state["selected_coin"] = False, None, None
        event(f"🛑 {symbol.upper()} DISARMED manually.")
    save_state()
    return jsonify({"success": True})

@app.route("/api/system", methods=["POST"])
def api_system():
    en = bool(request.get_json(silent=True).get("enabled"))
    with lock: state["system_on"] = en
    save_state()
    event(f"{'⚡ SYSTEM ON' if en else '⏸️ SYSTEM OFF'}")
    return jsonify({"success": True})

@app.route("/api/mode", methods=["POST"])
def api_mode():
    if state["current_trade"]: return jsonify({"success": False, "error": "Cannot change mode during active trade"}), 400
    with lock: state["mode"] = str(request.get_json(silent=True).get("mode", "")).upper()
    save_state()
    return jsonify({"success": True})

@app.route("/api/trade/close", methods=["POST"])
def api_trade_close():
    if close_trade_manual(): return jsonify({"success": True})
    return jsonify({"success": False, "error": "Failed to close trade. Check Delta app."}), 500

# ============================================================
# HTML UI
# ============================================================

HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prime Minister AI (Pro)</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; background: #07111f; color: #e8eef7; font-family: Inter, system-ui, sans-serif; }
header { padding: 18px; border-bottom: 1px solid #1b2a3e; background: #091625; }
h1 { margin: 0; font-size: 23px; }
.subtitle { margin-top: 5px; color: #8294aa; font-size: 13px; }
.container { max-width: 1200px; margin: auto; padding: 16px; }
.topbar { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 10px; margin-bottom: 15px; }
.panel { background: #0b1828; border: 1px solid #1b2a3e; border-radius: 14px; padding: 14px; }
.label { color: #7e91a8; font-size: 12px; }
.value { font-size: 20px; font-weight: 700; margin-top: 4px; }
.green { color: #45e09b; }
.red { color: #ff6577; }
.yellow { color: #ffc857; }
.gray { color: #91a0b5; }
.controls { display: flex; gap: 8px; flex-wrap: wrap; }
button, select, input { background: #101f32; color: white; border: 1px solid #2a405a; border-radius: 9px; padding: 10px 12px; font-size: 14px; cursor: pointer; }
button:hover { background: #172b43; }
.coin-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(210px,1fr)); gap: 12px; }
.coin { cursor: pointer; transition: .15s; }
.coin:hover { transform: translateY(-2px); border-color: #52759d; }
.coin.on { border-color: #ffc857; box-shadow: 0 0 10px rgba(255, 200, 87, 0.2); }
.coin-title { display: flex; justify-content: space-between; align-items: center; }
.coin-name { font-size: 17px; font-weight: 700; display: flex; align-items: center;}
.setup-dot { width: 10px; height: 10px; border-radius: 50%; margin-left: 8px; display: inline-block; animation: pulse 2s infinite; }
.dot-green { background-color: #45e09b; box-shadow: 0 0 8px #45e09b; }
.dot-red { background-color: #ff6577; box-shadow: 0 0 8px #ff6577; }
.dot-none { background-color: transparent; animation: none; }
@keyframes pulse { 0% { transform: scale(0.95); opacity: 0.8; } 50% { transform: scale(1.1); opacity: 1; } 100% { transform: scale(0.95); opacity: 0.8; } }
.badge { font-size: 11px; padding: 4px 8px; border-radius: 999px; background: #16263a; }
.badge-armed { background: #ffc857; color: #000; font-weight: bold; }
.signal { font-size: 21px; font-weight: 800; margin: 15px 0 4px; }
.price { font-size: 15px; }
.reason { margin-top: 10px; color: #8294aa; font-size: 12px; min-height: 32px; }
.meta { margin-top: 12px; display: flex; justify-content: space-between; color: #9aacbf; font-size: 12px; }
.section { margin-top: 16px; }
.trade-box { display: grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); gap: 10px; }
.event-list { max-height: 250px; overflow: auto; }
.event-item { padding: 8px 0; border-bottom: 1px solid #18273a; font-size: 12px; color: #a9b8ca; }
.modal { position: fixed; inset: 0; background: rgba(0,0,0,.7); display: none; align-items: center; justify-content: center; padding: 15px; z-index: 50; }
.modal.show { display: flex; }
.modal-box { width: 100%; max-width: 480px; max-height: 90vh; overflow: auto; background: #0b1828; border: 1px solid #2a405a; border-radius: 16px; padding: 18px; }
.modal-header { display: flex; justify-content: space-between; align-items: center; }
.form-row { margin-top: 12px; }
.form-row label { display: block; color: #8ea0b4; font-size: 12px; margin-bottom: 5px; }
.form-row input, .form-row select { width: 100%; }
.rule { display: flex; justify-content: space-between; padding: 7px 0; border-bottom: 1px solid #18273a; font-size: 12px; }
.rule span:first-child { color: #8497ac; }
.margin-calc { background: #16263a; padding: 10px; border-radius: 8px; margin-top: 15px; font-size: 14px; font-weight: bold; text-align: center; color: #ffc857; }
</style>
</head>
<body>
<header>
    <h1>Prime Minister AI (Pro)</h1>
    <div class="subtitle">Stateless Engine | Bracket SL | Full Range Lev.</div>
</header>
<div class="container">
    <div class="topbar">
        <div class="panel"><div class="label">PUBLIC IP</div><div id="public_ip" class="value yellow">--</div></div>
        <div class="panel"><div class="label">ENGINE SYSTEM</div><div id="system" class="value gray">OFF</div></div>
        <div class="panel"><div class="label">MODE</div><div id="mode" class="value">PAPER</div></div>
        <div class="panel"><div class="label">ARMED COIN</div><div id="selected" class="value">None</div></div>
    </div>
    <div class="panel">
        <div class="controls">
            <button onclick="apiPost('/api/system', {enabled: !appState.system_on})">SYSTEM ON / OFF</button>
            <button onclick="apiPost('/api/mode', {mode: 'PAPER'})">PAPER</button>
            <button onclick="apiPost('/api/mode', {mode: 'LIVE'})">LIVE</button>
            <button onclick="apiPost('/api/trade/close', {})">CLOSE CURRENT TRADE</button>
        </div>
    </div>
    <div class="section"><div class="panel"><div class="label">CONNECTIONS & HEALTH</div><div id="connections" style="margin-top:12px"></div></div></div>
    <div class="section"><div class="panel"><div class="label">MARKET WATCH (Click to ARM)</div><div id="coins" class="coin-grid" style="margin-top:12px"></div></div></div>
    <div class="section"><div class="panel"><div class="label">ACTIVE TRADE (Hard SL Managed)</div><div id="trade" style="margin-top:12px">None</div></div></div>
    <div class="section"><div class="panel"><div class="label">EVENTS LOG (IST)</div><div id="events" class="event-list" style="margin-top:12px"></div></div></div>
</div>
<div id="modal" class="modal">
    <div class="modal-box">
        <div class="modal-header">
            <div><div id="modalTitle" style="font-size:20px; font-weight:800;"></div><div id="modalSubtitle" class="small" style="color:#71849a;font-size:11px;">Delta product rules</div></div>
            <button onclick="document.getElementById('modal').classList.remove('show')">X</button>
        </div>
        <div id="rules" style="margin-top:14px"></div>
        <div class="form-row"><label>Quantity (Lots) <span id="coinEquivalent" class="yellow"></span></label><input id="quantity" type="number" step="1" oninput="calcMargin()" /></div>
        <div class="form-row"><label>Leverage</label><select id="leverage" onchange="calcMargin()"></select></div>
        <div id="marginDisplay" class="margin-calc">-- USD</div>
        <div class="controls" style="margin-top:16px">
            <button onclick="saveSet()">SAVE SETTINGS</button>
            <button onclick="apiPost(`/api/coin/${selectedModalCoin}/toggle`, {enabled: !appState.coins[selectedModalCoin].enabled}); document.getElementById('modal').classList.remove('show')">ARM / DISARM</button>
        </div>
    </div>
</div>
<script>
let appState = null, selectedModalCoin = null, currentRules = {contract_value: 1};

function fmt(n) { return n ? Number(n).toLocaleString('en-US', {maximumFractionDigits: 8}) : "--"; }

async function apiPost(url, body) {
    const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const d = await r.json(); if (!d.success) alert(d.error); load();
}

function calcMargin() {
    const q = Number(document.getElementById("quantity").value), l = Number(document.getElementById("leverage").value);
    const p = appState.coins[selectedModalCoin].last_signal.price, cv = currentRules.contract_value;
    if(q > 0) document.getElementById("coinEquivalent").innerText = `(Eq: ${(q*cv).toLocaleString('en-US',{maximumFractionDigits:5})} ${selectedModalCoin.replace("USD","")})`;
    if(q>0 && l>0 && p>0) document.getElementById("marginDisplay").innerText = `Req Margin: ~$${(((q*cv*p)/l) + (q*cv*p*0.0015)).toFixed(2)}`;
    else document.getElementById("marginDisplay").innerText = "Waiting for live price...";
}

function render(s) {
    appState = s;
    document.getElementById("public_ip").innerText = s.runtime.public_ip || "--";
    document.getElementById("system").innerText = s.system_on ? "ON" : "OFF";
    document.getElementById("system").className = "value " + (s.system_on ? "green" : "gray");
    document.getElementById("mode").innerText = s.mode;
    document.getElementById("selected").innerText = s.selected_coin || "None";
    
    document.getElementById("connections").innerHTML = `
        <div class="rule"><span>Delta API</span><span class="${s.runtime.last_scan_error?'red':'green'}">${s.runtime.last_scan_error||'READY'}</span></div>
        <div class="rule"><span>Scanner</span><span class="${s.engine_running?'green':'red'}">${s.engine_running?'LIVE':'STOPPED'}</span></div>
    `;

    const cbox = document.getElementById("coins"); cbox.innerHTML = "";
    for (const sym of ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"]) {
        const c = s.coins[sym], sig = c.last_signal || {}, arm = c.armed_signal;
        const dclass = sig.score >= 75 ? (sig.side === "LONG" ? "dot-green" : "dot-red") : "dot-none";
        
        const div = document.createElement("div");
        div.className = "panel coin " + (c.enabled ? "on" : "");
        div.onclick = async () => {
            selectedModalCoin = sym; document.getElementById("modalTitle").innerText = sym;
            document.getElementById("quantity").value = c.quantity; document.getElementById("modal").classList.add("show");
            try { const r = await fetch(`/api/coin/${sym}/rules`); const d = await r.json(); 
                if(d.success) { 
                    currentRules = d.rules; 
                    const sel = document.getElementById("leverage"); sel.innerHTML="";
                    
                    const minL = d.rules.min_leverage || 1;
                    const maxL = d.rules.max_leverage || 100;
                    let levList = [1,2,3,5,10,15,20,25,30,40,50,75,100,125,150,200].filter(v => v >= minL && v <= maxL);
                    if(!levList.includes(minL)) levList.unshift(minL);
                    if(!levList.includes(maxL)) levList.push(maxL);
                    levList.sort((a,b)=>a-b);

                    levList.forEach(v=>{
                        const opt = document.createElement("option"); opt.value=v; opt.innerText=v+"x";
                        if(c.leverage==v) opt.selected=true; sel.appendChild(opt);
                    });
                    document.getElementById("rules").innerHTML = `<div class="rule"><span>Contract</span><span>${d.rules.contract_value}</span></div><div class="rule"><span>Step</span><span>${d.rules.quantity_step}</span></div><div class="rule"><span>Tick Size</span><span>${d.rules.tick_size}</span></div>`;
                    calcMargin();
                }
            } catch(e){}
        };
        div.innerHTML = `
            <div class="coin-title"><div class="coin-name">${sym} <span class="setup-dot ${dclass}"></span></div>${c.enabled ? '<div class="badge badge-armed">ARMED</div>' : '<div class="badge">OFF</div>'}</div>
            <div class="signal ${sig.side==='LONG'?'green':sig.side==='SHORT'?'red':'gray'}">${sig.side}</div>
            <div class="price">${fmt(sig.price)}</div>
            <div class="gray" style="font-size:12px; margin-top:5px;">${arm ? `⏳ Waiting: <b>${fmt(arm.trigger_price)}</b>` : (sig.trigger_price ? `Breakout target: ${fmt(sig.trigger_price)}` : "")}</div>
            <div class="reason">${sig.reason||""}</div>
            <div class="meta"><span>Score: ${sig.score}</span><span>Qty: ${c.quantity}</span><span>${c.leverage}x</span></div>
        `;
        cbox.appendChild(div);
    }
    
    const tbox = document.getElementById("trade");
    if(s.current_trade) {
        const t = s.current_trade;
        tbox.innerHTML = `<div class="trade-box"><div><div class="label">SYMBOL</div><div class="value">${t.symbol}</div></div><div><div class="label">SIDE</div><div class="value ${t.side==='LONG'?'green':'red'}">${t.side}</div></div><div><div class="label">ENTRY</div><div class="value">${fmt(t.entry)}</div></div><div><div class="label">HARD SL</div><div class="value yellow">${fmt(t.stop)}</div></div><div><div class="label">QTY</div><div class="value">${t.quantity}</div></div></div>`;
    } else tbox.innerHTML = `<span class="gray">No active trade</span>`;

    const ebox = document.getElementById("events"); ebox.innerHTML = "";
    s.events.forEach(e => {
        const div = document.createElement("div"); div.className = "event-item";
        if(e.message.includes("Error")||e.message.includes("Failed")) div.style.color="#ff6577";
        else if(e.message.includes("EXECUTED")||e.message.includes("Started")||e.message.includes("ON")||e.message.includes("Recovered")) div.style.color="#45e09b";
        else if(e.message.includes("Trailing")||e.message.includes("SL")) div.style.color="#ffc857";
        div.innerText = e.message; ebox.appendChild(div);
    });
}

function saveSet() {
    apiPost(`/api/coin/${selectedModalCoin}/settings`, {quantity: Number(document.getElementById("quantity").value), leverage: Number(document.getElementById("leverage").value)});
    document.getElementById("modal").classList.remove("show");
}

async function load() { try { const r = await fetch("/api/state", {cache: "no-store"}); render(await r.json()); } catch(e){} }
load(); setInterval(load, 5000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False, threaded=True)
