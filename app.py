import os
import json
import time
import hmac
import hashlib
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, render_template_string


# ============================================================
# PRIME MINISTER AI — DELTA COMMANDER
# ============================================================

app = Flask(__name__)

DELTA_BASE = "https://api.india.delta.exchange"
STATE_FILE = "bot_state.json"

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

USER_AGENT = "PrimeMinisterAI/2.1"

SCAN_SECONDS = 5
PRODUCT_CACHE_SECONDS = 60
CANDLE_LIMIT = 160

SYMBOLS = [
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "XRPUSD",
    "DOGEUSD",
]

RESOLUTION = "5m"


# ============================================================
# GLOBAL RUNTIME
# ============================================================

lock = threading.RLock()
engine_thread = None
engine_stop = threading.Event()

product_cache = {}

runtime = {
    "started_at": None,
    "last_scan": None,
    "last_scan_error": None,
    "public_ip": None,
}


# ============================================================
# DEFAULT STATE
# ============================================================

def default_coin_state():
    return {
        "enabled": False,
        "quantity": 1,
        "leverage": 2,
        "rules": {},
        "last_signal": {
            "side": "NO_TRADE",
            "score": 0,
            "price": None,
            "reason": "Waiting for market data",
            "updated_at": None,
        },
    }


def default_state():
    return {
        "system_on": False,
        "selected_coin": None,
        "mode": "PAPER",
        "current_trade": None,
        "events": [],
        "coins": {
            symbol: default_coin_state()
            for symbol in SYMBOLS
        },
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        base = default_state()

        for key in base:
            if key not in state:
                state[key] = base[key]

        if "coins" not in state:
            state["coins"] = base["coins"]

        for symbol in SYMBOLS:
            if symbol not in state["coins"]:
                state["coins"][symbol] = default_coin_state()

            coin = state["coins"][symbol]
            for key, value in default_coin_state().items():
                if key not in coin:
                    coin[key] = value

        return state

    except Exception:
        return default_state()


state = load_state()


def save_state():
    with lock:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)


# ============================================================
# EVENTS
# ============================================================

def event(message):
    timestamp = datetime.now(timezone.utc).isoformat()
    with lock:
        state["events"].insert(0, {"time": timestamp, "message": message})
        state["events"] = state["events"][:100]
    save_state()


# ============================================================
# DELTA SIGNING
# ============================================================

def delta_signature(method, timestamp, path, query_string="", body=""):
    message = method.upper() + str(timestamp) + path + query_string + body
    return hmac.new(API_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def delta_request(method, path, params=None, body=None, authenticated=False, timeout=10):
    url = DELTA_BASE + path
    params = params or {}
    body_text = "" if body is None else json.dumps(body, separators=(",", ":"))

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    if body is not None: headers["Content-Type"] = "application/json"

    if authenticated:
        if not API_KEY or not API_SECRET: raise RuntimeError("API keys not configured")
        timestamp = str(int(time.time()))
        query_string = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())]) if params else ""
        signature = delta_signature(method, timestamp, path, query_string, body_text)
        headers.update({"api-key": API_KEY, "timestamp": timestamp, "signature": signature})

    response = requests.request(method=method.upper(), url=url, params=params, data=body_text if body is not None else None, headers=headers, timeout=timeout)
    try: data = response.json()
    except Exception: data = {"success": False, "error": response.text}

    if response.status_code >= 400: raise RuntimeError(f"Delta HTTP {response.status_code}: {data}")
    return data


# ============================================================
# DELTA PRODUCTS & LEVERAGE
# ============================================================

def get_products():
    now = time.time()
    if product_cache.get("all") and now - product_cache["all"]["time"] < PRODUCT_CACHE_SECONDS:
        return product_cache["all"]["data"]
    data = delta_request("GET", "/v2/products", authenticated=False)
    product_cache["all"] = {"time": now, "data": data.get("result", [])}
    return product_cache["all"]["data"]


def find_product(symbol):
    try:
        for product in get_products():
            if str(product.get("symbol", "")).upper() == symbol.upper(): return product
    except Exception as exc: event(f"{symbol}: product lookup failed: {exc}")
    return None


def extract_number(product, names, default=None):
    for name in names:
        val = product.get(name)
        if val is not None:
            try: return float(val)
            except Exception: pass
    return default


def product_rules(symbol):
    product = find_product(symbol)
    if not product: return {"symbol": symbol, "available": False, "message": "Delta product unavailable"}

    min_qty = extract_number(product, ["min_order_size", "minimum_order_size", "order_min_size", "min_size"])
    max_qty = extract_number(product, ["max_order_size", "maximum_order_size", "order_max_size", "max_size"])
    step = extract_number(product, ["order_size_increment", "size_increment", "step_size"])
    min_leverage = extract_number(product, ["min_leverage", "minimum_leverage"], 1)
    max_leverage = extract_number(product, ["max_leverage", "maximum_leverage", "leverage"])
    default_leverage = extract_number(product, ["default_leverage"])

    if max_leverage is None and default_leverage is not None:
        max_leverage = default_leverage

    return {
        "available": True, "id": product.get("id"), "symbol": product.get("symbol"),
        "trading_status": product.get("trading_status"), "min_quantity": min_qty,
        "max_quantity": max_qty, "quantity_step": step, "min_leverage": min_leverage,
        "max_leverage": max_leverage, "default_leverage": default_leverage,
    }


def set_delta_leverage(symbol, leverage):
    product = find_product(symbol)
    if not product or not product.get("id"): raise RuntimeError(f"{symbol}: Delta product ID missing")
    return delta_request("POST", f"/v2/products/{product['id']}/orders/leverage", body={"leverage": int(leverage)}, authenticated=True)


# ============================================================
# DELTA CANDLES & POSITIONS
# ============================================================

def get_candles(symbol, resolution=RESOLUTION):
    data = delta_request("GET", "/v2/history/candles", params={"symbol": symbol, "resolution": resolution, "limit": CANDLE_LIMIT}, authenticated=False)
    candles = []
    for item in data.get("result", []):
        try:
            if isinstance(item, dict):
                candles.append({"time": float(item.get("time")), "open": float(item.get("open")), "high": float(item.get("high")), "low": float(item.get("low")), "close": float(item.get("close")), "volume": float(item.get("volume", 0))})
            else:
                candles.append({"time": float(item[0]), "open": float(item[1]), "high": float(item[2]), "low": float(item[3]), "close": float(item[4]), "volume": float(item[5]) if len(item) > 5 else 0})
        except Exception: continue
    candles.sort(key=lambda x: x["time"])
    return candles


def get_position_for_symbol(symbol):
    try:
        for pos in delta_request("GET", "/v2/positions", authenticated=True).get("result", []):
            if str(pos.get("product_symbol") or pos.get("symbol") or "").upper() == symbol.upper():
                if abs(float(pos.get("size", 0) or 0)) > 0: return pos
    except Exception as exc: event(f"{symbol}: position check failed: {exc}")
    return None


# ============================================================
# INDICATORS & STRATEGY
# ============================================================

def ema(values, period):
    if len(values) < period: return None
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    for price in values[period:]: current = ((price - current) * multiplier + current)
    return current


def rsi(values, period=14):
    if len(values) < period + 1: return None
    gains, losses = [], []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0: gains.append(change); losses.append(0)
        else: gains.append(0); losses.append(abs(change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (((avg_gain * (period - 1)) + max(change, 0)) / period)
        avg_loss = (((avg_loss * (period - 1)) + max(-change, 0)) / period)
    if avg_loss == 0: return 100.0
    return 100 - (100 / (1 + (avg_gain / avg_loss)))


def atr(candles, period=14):
    if len(candles) < period + 1: return None
    true_ranges = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        true_ranges.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    if len(true_ranges) < period: return None
    current_atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]: current_atr = ((current_atr * (period - 1) + tr) / period)
    return current_atr


def calculate_signal(candles):
    if len(candles) < 60: return {"side": "NO_TRADE", "score": 0, "price": candles[-1]["close"] if candles else None, "reason": "Not enough candles", "ema9": None, "ema21": None, "rsi": None, "atr": None}
    closes, volumes = [x["close"] for x in candles], [x["volume"] for x in candles]
    price, ema9, ema21, rsi_val, atr_val = closes[-1], ema(closes, 9), ema(closes, 21), rsi(closes, 14), atr(candles, 14)

    if None in (ema9, ema21, rsi_val, atr_val): return {"side": "NO_TRADE", "score": 0, "price": price, "reason": "Indicator unavailable"}

    ls, ss, rl, rs = 0, 0, [], []

    if ema9 > ema21: ls += 25; rl.append("EMA bullish")
    elif ema9 < ema21: ss += 25; rs.append("EMA bearish")

    mom = closes[-4:]
    if mom[-1] > mom[0]: ls += 10; rl.append("Momentum up")
    elif mom[-1] < mom[0]: ss += 10; rs.append("Momentum down")

    if 52 <= rsi_val <= 68: ls += 15; rl.append("RSI long")
    if 32 <= rsi_val <= 48: ss += 15; rs.append("RSI short")
    if rsi_val > 75: ls -= 10
    if rsi_val < 25: ss -= 10

    ph = max(x["high"] for x in candles[-21:-1])
    pl = min(x["low"] for x in candles[-21:-1])
    if price > ph: ls += 20; rl.append("Breakout")
    if price < pl: ss += 20; rs.append("Breakdown")

    rv = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0
    bv = sum(volumes[-25:-5]) / 20 if len(volumes) >= 25 else 0
    vr = rv / bv if bv > 0 else 0
    if vr >= 1.15:
        if ls > ss: ls += 15; rl.append("Volume conf")
        elif ss > ls: ss += 15; rs.append("Volume conf")

    if ls >= 75 and ls >= ss + 10: return {"side": "LONG", "score": min(ls, 100), "price": price, "reason": ", ".join(rl), "ema9": ema9, "ema21": ema21, "rsi": rsi_val, "atr": atr_val, "updated_at": datetime.now(timezone.utc).isoformat()}
    elif ss >= 75 and ss >= ls + 10: return {"side": "SHORT", "score": min(ss, 100), "price": price, "reason": ", ".join(rs), "ema9": ema9, "ema21": ema21, "rsi": rsi_val, "atr": atr_val, "updated_at": datetime.now(timezone.utc).isoformat()}
    
    score = max(min(ls, 100), min(ss, 100))
    reason = f"Long setup incomplete ({ls}/75)" if ls > ss else f"Short setup incomplete ({ss}/75)" if ss > ls else "No edge"
    return {"side": "NO_TRADE", "score": score, "price": price, "reason": reason, "ema9": ema9, "ema21": ema21, "rsi": rsi_val, "atr": atr_val, "updated_at": datetime.now(timezone.utc).isoformat()}


# ============================================================
# IP TRACKER (UPDATED - Bulletproof Render Fix)
# ============================================================

def get_public_ip():
    # 4 Backup APIs to guarantee IP loading
    services = [
        ("https://api.ipify.org?format=json", True),
        ("https://icanhazip.com", False),
        ("https://ident.me", False),
        ("https://ifconfig.me/ip", False)
    ]
    
    for url, is_json in services:
        try:
            res = requests.get(url, timeout=5)
            ip = res.json().get("ip") if is_json else res.text.strip()
            
            # Simple check to make sure it's an actual IP address
            if ip and "." in ip:
                runtime["public_ip"] = ip
                return
        except Exception:
            continue
            
    if not runtime.get("public_ip"):
        runtime["public_ip"] = "Error Loading IP"

def ip_updater_loop():
    while True:
        get_public_ip()
        time.sleep(60)

def validate_coin_settings(symbol, quantity, leverage):
    rules = product_rules(symbol)
    if not rules.get("available"): return False, rules.get("message", "Delta product unavailable")
    try: quantity, leverage = float(quantity), float(leverage)
    except: return False, "Must be numeric"

    if quantity <= 0 or leverage <= 0: return False, "Must be > 0"
    min_qty, max_qty = rules.get("min_quantity"), rules.get("max_quantity")
    if min_qty is not None and quantity < min_qty: return False, f"Below min {min_qty}"
    if max_qty is not None and quantity > max_qty: return False, f"Above max {max_qty}"
    
    min_lev, max_lev = rules.get("min_leverage"), rules.get("max_leverage")
    if min_lev is not None and leverage < min_lev: return False, f"Below min {min_lev}x"
    if max_lev is not None and leverage > max_lev: return False, f"Above max {max_lev}x"
    return True, "OK"


# ============================================================
# SMART TRADE OPEN & MANAGEMENT (TRAILING)
# ============================================================

def paper_open(symbol, side, signal):
    with lock:
        if state["current_trade"]: return False
        coin = state["coins"][symbol]
        price, atr_value = float(signal["price"]), float(signal["atr"])
        stop = price - (atr_value * 1.5) if side == "LONG" else price + (atr_value * 1.5)

        state["current_trade"] = {
            "symbol": symbol, "side": side, "entry": price, "quantity": float(coin["quantity"]),
            "leverage": float(coin["leverage"]), "stop": stop, "entry_atr": atr_value,
            "highest_price": price, "lowest_price": price, "mode": "PAPER",
            "opened_at": datetime.now(timezone.utc).isoformat(), "reason": signal["reason"],
        }
    save_state()
    event(f"PAPER {side} opened: {symbol} @ {price}. Initial Stop: {stop:.2f}")
    return True

def live_open(symbol, side, signal):
    with lock:
        if state["current_trade"]: return False
        coin = state["coins"][symbol]
        quantity, leverage = float(coin["quantity"]), float(coin["leverage"])

    if get_position_for_symbol(symbol):
        event(f"LIVE entry blocked: existing {symbol} position detected")
        return False

    valid, msg = validate_coin_settings(symbol, quantity, leverage)
    if not valid: raise RuntimeError(msg)
    
    set_delta_leverage(symbol, leverage)
    result = delta_request("POST", "/v2/orders", body={"product_symbol": symbol, "size": quantity, "side": ("buy" if side == "LONG" else "sell"), "order_type": "market_order"}, authenticated=True)
    
    price, atr_value = float(signal["price"]), float(signal["atr"])
    stop = price - (atr_value * 1.5) if side == "LONG" else price + (atr_value * 1.5)

    with lock:
        state["current_trade"] = {
            "symbol": symbol, "side": side, "entry": price, "quantity": quantity,
            "leverage": leverage, "stop": stop, "entry_atr": atr_value,
            "highest_price": price, "lowest_price": price, "mode": "LIVE",
            "exchange_order": result, "opened_at": datetime.now(timezone.utc).isoformat(), "reason": signal["reason"],
        }
    save_state()
    event(f"LIVE {side} opened: {symbol} @ {price}. Initial Stop: {stop:.2f}")
    return True


def manage_active_trade():
    with lock: trade = state["current_trade"]
    if not trade: return
    symbol = trade["symbol"]
    try:
        candles = get_candles(symbol)
        if not candles: return
        price, side, atr_val = candles[-1]["close"], trade["side"], trade.get("entry_atr", 10)

        with lock:
            if side == "LONG":
                if "highest_price" not in trade: trade["highest_price"] = trade["entry"]
                if price > trade["highest_price"]:
                    trade["highest_price"] = price
                    new_stop = trade["highest_price"] - (atr_val * 1.5)
                    if trade["stop"] is None or new_stop > trade["stop"]:
                        trade["stop"] = new_stop
                        event(f"📈 Trailing Stop UPDATED UP for {symbol}: {new_stop:.2f} (Price: {price:.2f})")
                if trade["stop"] and price <= trade["stop"]: close_trade(price, "Trailing Stop Loss Hit")

            elif side == "SHORT":
                if "lowest_price" not in trade: trade["lowest_price"] = trade["entry"]
                if price < trade["lowest_price"]:
                    trade["lowest_price"] = price
                    new_stop = trade["lowest_price"] + (atr_val * 1.5)
                    if trade["stop"] is None or new_stop < trade["stop"]:
                        trade["stop"] = new_stop
                        event(f"📉 Trailing Stop UPDATED DOWN for {symbol}: {new_stop:.2f} (Price: {price:.2f})")
                if trade["stop"] and price >= trade["stop"]: close_trade(price, "Trailing Stop Loss Hit")
        save_state()
    except Exception as exc: event(f"Trade management error: {exc}")


def close_trade(price, reason):
    with lock: trade = state["current_trade"]
    if not trade: return False

    if trade["mode"] == "LIVE":
        pos = get_position_for_symbol(trade["symbol"])
        if pos and float(pos.get("size", 0)) > 0:
            delta_request("POST", "/v2/orders", body={"product_symbol": trade["symbol"], "size": abs(float(pos["size"])), "side": "sell" if str(pos.get("side", "")).lower() == "buy" else "buy", "order_type": "market_order", "reduce_only": True}, authenticated=True)
        event(f"LIVE trade closed: {trade['symbol']} @ {price} ({reason})")
    else: event(f"PAPER trade closed: {trade['symbol']} @ {price} ({reason})")

    with lock: state["current_trade"] = None
    save_state()
    return True


# ============================================================
# SIGNAL SCANNER & ENGINE
# ============================================================

def scan_all_coins():
    results = {}
    for symbol in SYMBOLS:
        try:
            signal = calculate_signal(get_candles(symbol))
            with lock: state["coins"][symbol]["last_signal"] = signal
            results[symbol] = signal
        except Exception as exc:
            signal = {"side": "NO_TRADE", "score": 0, "price": None, "reason": f"Error: {exc}"}
            with lock: state["coins"][symbol]["last_signal"] = signal
            results[symbol] = signal
    save_state()
    return results

def strongest_signal():
    candidates = []
    with lock:
        for symbol in SYMBOLS:
            sig = state["coins"][symbol]["last_signal"]
            if sig.get("side") in ("LONG", "SHORT"): candidates.append((symbol, sig))
    if not candidates: return None
    candidates.sort(key=lambda item: item[1].get("score", 0), reverse=True)
    return {"symbol": candidates[0][0], **candidates[0][1]}

def trading_cycle():
    runtime["last_scan"] = datetime.now(timezone.utc).isoformat()
    try:
        signals = scan_all_coins()
        
        # IP error reset on successful scan
        runtime["last_scan_error"] = None 

        with lock: trade = state["current_trade"]
        if trade:
            manage_active_trade()
            return

        with lock:
            system_on, selected = bool(state["system_on"]), state["selected_coin"]

        if not system_on or selected not in SYMBOLS: return
        signal = signals.get(selected)
        if not signal or signal.get("side") not in ("LONG", "SHORT"): return

        with lock:
            coin_enabled, mode = bool(state["coins"][selected]["enabled"]), state["mode"]

        if not coin_enabled: return

        if mode == "PAPER": paper_open(selected, signal["side"], signal)
        elif mode == "LIVE": live_open(selected, signal["side"], signal)

    except Exception as exc:
        runtime["last_scan_error"] = str(exc)
        event(f"Trading error (Check API/IP): {exc}")

def engine_loop():
    event("Trading engine started")
    while not engine_stop.is_set():
        started = time.time()
        trading_cycle()
        engine_stop.wait(max(0.5, SCAN_SECONDS - (time.time() - started)))
    event("Trading engine stopped")

def start_engine():
    global engine_thread
    with lock:
        if not state["system_on"]: return False, "System is OFF"
        if state["selected_coin"] not in SYMBOLS: return False, "Select one coin"
        if not state["coins"][state["selected_coin"]]["enabled"]: return False, "Selected coin is OFF"
    if engine_thread and engine_thread.is_alive(): return True, "Already running"
    engine_stop.clear()
    engine_thread = threading.Thread(target=engine_loop, daemon=True)
    engine_thread.start()
    return True, "Engine started"

def stop_engine():
    engine_stop.set()
    return True, "Engine stopping"

# ============================================================
# API ENDPOINTS
# ============================================================

@app.get("/api/state")
def api_state():
    with lock: snapshot = json.loads(json.dumps(state))
    snapshot["runtime"] = runtime
    snapshot["strongest_signal"] = strongest_signal()
    snapshot["engine_running"] = bool(engine_thread and engine_thread.is_alive())
    return jsonify(snapshot)

@app.get("/api/coin/<symbol>/rules")
def api_coin_rules(symbol):
    return jsonify({"success": True, "rules": product_rules(symbol.upper())})

@app.post("/api/coin/<symbol>/settings")
def api_coin_settings(symbol):
    data = request.get_json(silent=True) or {}
    quantity, leverage = float(data.get("quantity", 1)), float(data.get("leverage", 1))
    valid, msg = validate_coin_settings(symbol.upper(), quantity, leverage)
    if not valid: return jsonify({"success": False, "error": msg}), 400
    with lock:
        state["coins"][symbol.upper()]["quantity"] = quantity
        state["coins"][symbol.upper()]["leverage"] = leverage
    save_state()
    return jsonify({"success": True})

@app.post("/api/coin/<symbol>/toggle")
def api_coin_toggle(symbol):
    enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
    with lock:
        if enabled:
            for other in SYMBOLS: state["coins"][other]["enabled"] = (other == symbol.upper())
            state["selected_coin"] = symbol.upper()
        else:
            state["coins"][symbol.upper()]["enabled"] = False
            if state["selected_coin"] == symbol.upper(): state["selected_coin"] = None
    save_state()
    return jsonify({"success": True})

@app.post("/api/system")
def api_system():
    enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
    with lock: state["system_on"] = enabled
    if enabled: start_engine()
    else: stop_engine()
    save_state()
    return jsonify({"success": True})

@app.post("/api/mode")
def api_mode():
    mode = str((request.get_json(silent=True) or {}).get("mode", "")).upper()
    with lock:
        if state["current_trade"]: return jsonify({"success": False, "error": "Cannot change mode during active trade"}), 400
        state["mode"] = mode
    save_state()
    return jsonify({"success": True})

@app.post("/api/trade/close")
def api_close_trade():
    with lock: trade = state["current_trade"]
    if not trade: return jsonify({"success": False, "error": "No open trade"}), 400
    try:
        candles = get_candles(trade["symbol"])
        price = candles[-1]["close"] if candles else trade.get("entry")
        close_trade(price, "Manual close")
        return jsonify({"success": True})
    except Exception as exc: return jsonify({"success": False, "error": str(exc)}), 500

# ============================================================
# DASHBOARD / HTML FRONTEND 
# ============================================================

HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prime Minister AI</title>
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
.coin.on { border-color: #45e09b; box-shadow: 0 0 10px rgba(69, 224, 155, 0.2); }
.coin-title { display: flex; justify-content: space-between; align-items: center; }
.coin-name { font-size: 17px; font-weight: 700; display: flex; align-items: center;}
.setup-dot { width: 10px; height: 10px; border-radius: 50%; margin-left: 8px; display: inline-block; animation: pulse 2s infinite; }
.dot-green { background-color: #45e09b; box-shadow: 0 0 8px #45e09b; }
.dot-red { background-color: #ff6577; box-shadow: 0 0 8px #ff6577; }
.dot-none { background-color: transparent; animation: none; }
@keyframes pulse { 0% { transform: scale(0.95); opacity: 0.8; } 50% { transform: scale(1.1); opacity: 1; } 100% { transform: scale(0.95); opacity: 0.8; } }
.badge { font-size: 11px; padding: 4px 8px; border-radius: 999px; background: #16263a; }
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
.small { color: #71849a; font-size: 11px; }
</style>
</head>
<body>

<header>
    <h1>Prime Minister AI</h1>
    <div class="subtitle">Autonomous Delta Trading Commander</div>
</header>

<div class="container">
    <div class="topbar">
        <div class="panel"><div class="label">PUBLIC IP (Copy for Delta)</div><div id="public_ip" class="value yellow">--</div></div>
        <div class="panel"><div class="label">SYSTEM</div><div id="system" class="value gray">OFF</div></div>
        <div class="panel"><div class="label">MODE</div><div id="mode" class="value">PAPER</div></div>
        <div class="panel"><div class="label">SELECTED COIN</div><div id="selected" class="value">None</div></div>
        <div class="panel"><div class="label">STRONGEST SIGNAL</div><div id="strongest" class="value">None</div></div>
    </div>

    <div class="panel">
        <div class="controls">
            <button onclick="systemToggle()">SYSTEM ON / OFF</button>
            <button onclick="setMode('PAPER')">PAPER</button>
            <button onclick="setMode('LIVE')">LIVE</button>
            <button onclick="manualClose()">CLOSE CURRENT TRADE</button>
        </div>
    </div>

    <div class="section">
        <div class="panel">
            <div class="label">TOP 5 MARKET WATCH (Click coin to setup)</div>
            <div id="coins" class="coin-grid" style="margin-top:12px"></div>
        </div>
    </div>

    <div class="section">
        <div class="panel">
            <div class="label">CURRENT TRADE (With Smart Trailing)</div>
            <div id="trade" style="margin-top:12px">None</div>
        </div>
    </div>

    <div class="section">
        <div class="panel">
            <div class="label">EVENTS / ALERTS</div>
            <div id="events" class="event-list" style="margin-top:12px"></div>
        </div>
    </div>
</div>

<div id="modal" class="modal">
    <div class="modal-box">
        <div class="modal-header">
            <div>
                <div id="modalTitle" style="font-size:20px; font-weight:800;"></div>
                <div id="modalSubtitle" class="small"></div>
            </div>
            <button onclick="closeModal()">X</button>
        </div>
        <div id="rules" style="margin-top:14px"></div>
        <div class="form-row"><label>Quantity</label><input id="quantity" type="number" step="any" /></div>
        <div class="form-row"><label>Leverage</label><select id="leverage"></select></div>
        <div class="controls" style="margin-top:16px">
            <button onclick="saveSettings()">SAVE SETTINGS</button>
            <button onclick="toggleSelectedCoin()">TURN ON / OFF</button>
        </div>
    </div>
</div>

<script>
let appState = null;
let selectedModalCoin = null;
let lastNotifiedSignals = { error: null };

if (Notification.permission !== "granted" && Notification.permission !== "denied") {
    Notification.requestPermission();
}

function money(value) {
    if (value === null || value === undefined) return "--";
    const n = Number(value);
    if (!Number.isFinite(n)) return "--";
    return n >= 1000 ? n.toLocaleString(undefined, {maximumFractionDigits: 2}) : n.toLocaleString(undefined, {maximumFractionDigits: 8});
}

function signalClass(side) {
    if (side === "LONG") return "green";
    if (side === "SHORT") return "red";
    return "gray";
}

function checkAndNotify(state) {
    // Check Delta Errors (Connection / IP Issues)
    if (state.runtime.last_scan_error) {
        if (lastNotifiedSignals["error"] !== state.runtime.last_scan_error) {
            lastNotifiedSignals["error"] = state.runtime.last_scan_error;
            if (Notification.permission === "granted") {
                new Notification("⚠️ Delta Connection Error", {
                    body: `Connection Failed!\nYour IP: ${state.runtime.public_ip || 'Unknown'}\nError: ${state.runtime.last_scan_error}`,
                });
            }
        }
    } else {
        lastNotifiedSignals["error"] = null;
    }

    // Check setups
    for (const symbol of ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"]) {
        const signal = state.coins[symbol]?.last_signal;
        if (!signal) continue;
        if (signal.score >= 75 && signal.side !== "NO_TRADE") {
            if (lastNotifiedSignals[symbol] !== signal.side) {
                lastNotifiedSignals[symbol] = signal.side;
                if (Notification.permission === "granted") {
                    new Notification(`🚀 Setup Ready: ${symbol}`, { body: `${signal.side} setup ready. Turn Coin ON to trade.` });
                }
            }
        } else {
            lastNotifiedSignals[symbol] = null;
        }
    }
}

function render(state) {
    appState = state;
    checkAndNotify(state);

    document.getElementById("public_ip").innerText = state.runtime.public_ip || "Loading...";

    const system = document.getElementById("system");
    system.innerText = state.system_on ? "ON" : "OFF";
    system.className = "value " + (state.system_on ? "green" : "gray");

    document.getElementById("mode").innerText = state.mode;
    document.getElementById("selected").innerText = state.selected_coin || "None";

    const strongest = state.strongest_signal;
    document.getElementById("strongest").innerHTML = strongest
        ? `<span class="${signalClass(strongest.side)}">${strongest.symbol} ${strongest.side} ${strongest.score}</span>`
        : "None";

    const coins = document.getElementById("coins");
    coins.innerHTML = "";

    for (const symbol of ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"]) {
        const coin = state.coins[symbol];
        const signal = coin.last_signal || {};

        let dotClass = "dot-none";
        let dotTitle = "No Active Setup";
        if (signal.score >= 75) {
            dotClass = signal.side === "LONG" ? "dot-green" : "dot-red";
            dotTitle = `${signal.side} Setup Ready!`;
        }

        const div = document.createElement("div");
        div.className = "panel coin " + (coin.enabled ? "on" : "");
        div.onclick = () => openCoin(symbol);
        div.innerHTML = `
            <div class="coin-title">
                <div class="coin-name">${symbol} <span class="setup-dot ${dotClass}" title="${dotTitle}"></span></div>
                <div class="badge">${coin.enabled ? "ON" : "OFF"}</div>
            </div>
            <div class="signal ${signalClass(signal.side)}">${signal.side || "NO_TRADE"}</div>
            <div class="price">${money(signal.price)}</div>
            <div class="reason">${signal.reason || ""}</div>
            <div class="meta">
                <span>Score: ${signal.score ?? 0}</span>
                <span>Qty: ${coin.quantity}</span>
                <span>${coin.leverage}x</span>
            </div>
        `;
        coins.appendChild(div);
    }
    renderTrade(state);
    renderEvents(state);
}

function renderTrade(state) {
    const box = document.getElementById("trade");
    const trade = state.current_trade;
    if (!trade) { box.innerHTML = `<span class="gray">No active trade</span>`; return; }

    box.innerHTML = `
        <div class="trade-box">
            <div><div class="label">SYMBOL</div><div class="value">${trade.symbol}</div></div>
            <div><div class="label">SIDE</div><div class="value ${signalClass(trade.side)}">${trade.side}</div></div>
            <div><div class="label">ENTRY</div><div class="value">${money(trade.entry)}</div></div>
            <div><div class="label">TRAILING STOP</div><div class="value yellow">${money(trade.stop)}</div></div>
            <div><div class="label">QTY</div><div class="value">${trade.quantity}</div></div>
            <div><div class="label">LEVERAGE</div><div class="value">${trade.leverage}x</div></div>
        </div>
    `;
}

function renderEvents(state) {
    const box = document.getElementById("events");
    box.innerHTML = "";
    for (const item of (state.events || [])) {
        const div = document.createElement("div");
        div.className = "event-item";
        
        // Color coding for different events
        if (item.message.toLowerCase().includes("error") || item.message.toLowerCase().includes("failed")) {
            div.style.color = "#ff6577"; // RED for errors
        } else if (item.message.includes("Trailing Stop")) {
            div.style.color = "#ffc857"; // YELLOW for trailing updates
        } else if (item.message.includes("opened")) {
            div.style.color = "#45e09b"; // GREEN for new trades
        }
        
        div.innerText = `${item.time.split('T')[1].slice(0,8)} — ${item.message}`;
        box.appendChild(div);
    }
}

async function loadState() {
    try { const res = await fetch("/api/state", {cache: "no-store"}); render(await res.json()); } 
    catch (e) { console.error(e); }
}

async function systemToggle() {
    if (!appState) return;
    const res = await fetch("/api/system", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({enabled: !appState.system_on}) });
    await loadState();
}

async function setMode(mode) {
    const res = await fetch("/api/mode", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({mode}) });
    const data = await res.json();
    if (!data.success) alert(data.error);
    await loadState();
}

async function manualClose() {
    const res = await fetch("/api/trade/close", { method: "POST" });
    const data = await res.json();
    if (!data.success) alert(data.error);
    await loadState();
}

async function openCoin(symbol) {
    selectedModalCoin = symbol;
    const coin = appState.coins[symbol];
    document.getElementById("modalTitle").innerText = symbol;
    document.getElementById("modalSubtitle").innerText = "Delta product rules";
    document.getElementById("quantity").value = coin.quantity;
    await loadRules(symbol);
    document.getElementById("modal").classList.add("show");
}

async function loadRules(symbol) {
    try {
        const res = await fetch(`/api/coin/${symbol}/rules`, {cache: "no-store"});
        const data = await res.json();
        if (!data.success) { alert(data.error); return; }
        renderRules(data.rules);
    } catch (e) { alert("Failed to load Delta rules"); }
}

function renderRules(rules) {
    const box = document.getElementById("rules");
    if (!rules.available) { box.innerHTML = `<div class="red">${rules.message || "Delta rules unavailable"}</div>`; return; }
    
    const maxRaw = rules.max_leverage ?? rules.default_leverage ?? 100;
    const max = Number.isFinite(Number(maxRaw)) ? Number(maxRaw) : 100;
    const min = Number.isFinite(Number(rules.min_leverage)) ? Number(rules.min_leverage) : 1;

    box.innerHTML = `
        <div class="rule"><span>Trading status</span><span>${rules.trading_status ?? "--"}</span></div>
        <div class="rule"><span>Min qty</span><span>${rules.min_quantity ?? "--"}</span></div>
        <div class="rule"><span>Max qty</span><span>${rules.max_quantity ?? "--"}</span></div>
        <div class="rule"><span>Step</span><span>${rules.quantity_step ?? "--"}</span></div>
        <div class="rule"><span>Min lev</span><span>${rules.min_leverage ?? "--"}x</span></div>
        <div class="rule"><span>Max lev</span><span>${max}x</span></div>
    `;

    const select = document.getElementById("leverage");
    select.innerHTML = "";
    const common = [1, 2, 3, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100];
    const values = common.filter(v => v >= min && v <= max);
    if (values.length === 0) values.push(min);

    for (const value of values) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.innerText = `${value}x`;
        if (appState.coins[selectedModalCoin].leverage == value) opt.selected = true;
        select.appendChild(opt);
    }
}

function closeModal() { document.getElementById("modal").classList.remove("show"); }

async function saveSettings() {
    if (!selectedModalCoin) return;
    const quantity = Number(document.getElementById("quantity").value);
    const leverage = Number(document.getElementById("leverage").value);
    const res = await fetch(`/api/coin/${selectedModalCoin}/settings`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({quantity, leverage}) });
    const data = await res.json();
    if (!data.success) alert(data.error);
    else { closeModal(); await loadState(); }
}

async function toggleSelectedCoin() {
    if (!selectedModalCoin) return;
    const current = appState.coins[selectedModalCoin].enabled;
    const res = await fetch(`/api/coin/${selectedModalCoin}/toggle`, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({enabled: !current}) });
    const data = await res.json();
    if (!data.success) alert(data.error);
    else { closeModal(); await loadState(); }
}

loadState();
setInterval(loadState, 5000);
</script>
</body>
</html>
"""

# ============================================================
# WEB
# ============================================================

@app.get("/")
def index():
    return render_template_string(HTML)

# ============================================================
# STARTUP & SERVER
# ============================================================

def startup():
    get_public_ip()
    threading.Thread(target=ip_updater_loop, daemon=True).start()

if __name__ == "__main__":
    startup()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
