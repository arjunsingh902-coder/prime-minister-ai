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
# PRIME MINISTER AI — PROFESSIONAL INTRADAY COMMANDER
# ============================================================

app = Flask(__name__)

DELTA_BASE = "https://api.india.delta.exchange"
STATE_FILE = "bot_state.json"

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
CMC_API_KEY = os.getenv("CMC_API_KEY", "").strip()

USER_AGENT = "PrimeMinisterAI/5.0-Bulletproof"

SCAN_SECONDS = 5
PRODUCT_CACHE_SECONDS = 60
CMC_CACHE_SECONDS = 30 * 60 
CANDLE_LIMIT_LTF = 160  
CANDLE_LIMIT_HTF = 60   

SYMBOLS = [
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "XRPUSD",
    "DOGEUSD",
]

# ============================================================
# GLOBAL RUNTIME
# ============================================================

lock = threading.RLock()
engine_thread = None
engine_stop = threading.Event()

product_cache = {}
cmc_cache = {
    "timestamp": 0,
    "data": {},
    "error": None,
}

runtime = {
    "started_at": None,
    "last_scan": 0,
    "last_scan_error": None,
    "public_ip": None,
    "is_scanning": False  # Live Heartbeat Sensor
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
        "armed_signal": None,  
        "last_signal": {
            "side": "NO_TRADE",
            "score": 0,
            "price": None,
            "trigger_price": None,
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
        "coins": {symbol: default_coin_state() for symbol in SYMBOLS},
    }

def load_state():
    if not os.path.exists(STATE_FILE): return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f: state = json.load(f)
        base = default_state()
        for key in base:
            if key not in state: state[key] = base[key]
        if "coins" not in state: state["coins"] = base["coins"]
        for symbol in SYMBOLS:
            if symbol not in state["coins"]: state["coins"][symbol] = default_coin_state()
            coin = state["coins"][symbol]
            for key, value in default_coin_state().items():
                if key not in coin: coin[key] = value
        return state
    except Exception: return default_state()

state = load_state()

def save_state():
    with lock:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(state, f, indent=2)
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
# DELTA API CORE (STRICT TIMEOUTS & ERROR PARSING)
# ============================================================

def delta_signature(method, timestamp, path, query_string="", body=""):
    message = method.upper() + str(timestamp) + path + query_string + body
    return hmac.new(API_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

def delta_request(method, path, params=None, body=None, authenticated=False, timeout=3):
    url = DELTA_BASE + path
    params = params or {}
    body_text = "" if body is None else json.dumps(body, separators=(",", ":"))

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None: headers["Content-Type"] = "application/json"

    if authenticated:
        if not API_KEY or not API_SECRET: raise RuntimeError("API keys missing")
        timestamp = str(int(time.time()))
        query_string = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())]) if params else ""
        signature = delta_signature(method, timestamp, path, query_string, body_text)
        headers.update({"api-key": API_KEY, "timestamp": timestamp, "signature": signature})

    try:
        response = requests.request(method=method.upper(), url=url, params=params, data=body_text if body is not None else None, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        raise RuntimeError("Timeout: Delta Server Slow/Unresponsive")
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Connection Error: DNS or Blocked")
    except Exception as e:
        raise RuntimeError(f"Request Failed: {str(e)}")

    if response.status_code >= 400: 
        err_msg = response.text
        try:
            err_json = response.json()
            if "error" in err_json and "message" in err_json["error"]:
                err_msg = err_json["error"]["message"]
        except Exception: pass
        raise RuntimeError(f"HTTP {response.status_code}: {err_msg}")
    
    try: return response.json()
    except Exception: return {"success": False, "error": response.text}

def get_products():
    now = time.time()
    if product_cache.get("all") and now - product_cache["all"]["time"] < PRODUCT_CACHE_SECONDS: return product_cache["all"]["data"]
    data = delta_request("GET", "/v2/products", authenticated=False)
    product_cache["all"] = {"time": now, "data": data.get("result", [])}
    return product_cache["all"]["data"]

def find_product(symbol):
    try:
        for product in get_products():
            if str(product.get("symbol", "")).upper() == symbol.upper(): return product
    except Exception: pass
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
    if not product: return {"available": False, "message": "Product unavailable"}
    
    max_lev = extract_number(product, ["max_leverage", "maximum_leverage", "leverage"])
    def_lev = extract_number(product, ["default_leverage"])
    if max_lev is None and def_lev is not None: max_lev = def_lev

    return {
        "available": True, "id": product.get("id"), "symbol": product.get("symbol"),
        "trading_status": product.get("trading_status"),
        "contract_value": extract_number(product, ["contract_value"], 1),
        "min_quantity": extract_number(product, ["min_order_size", "minimum_order_size"]),
        "max_quantity": extract_number(product, ["max_order_size", "maximum_order_size"]),
        "quantity_step": extract_number(product, ["order_size_increment", "step_size"]),
        "min_leverage": extract_number(product, ["min_leverage"], 1),
        "max_leverage": max_lev, "default_leverage": def_lev,
    }

def set_delta_leverage(symbol, leverage):
    product = find_product(symbol)
    if not product or not product.get("id"): raise RuntimeError(f"{symbol}: Product ID missing")
    return delta_request("POST", f"/v2/products/{product['id']}/orders/leverage", body={"leverage": int(leverage)}, authenticated=True)

def get_candles(symbol, resolution="5m", limit=160):
    data = delta_request("GET", "/v2/history/candles", params={"symbol": symbol, "resolution": resolution, "limit": limit}, authenticated=False)
    candles = []
    for item in data.get("result", []):
        try:
            if isinstance(item, dict): candles.append({"time": float(item.get("time")), "open": float(item.get("open")), "high": float(item.get("high")), "low": float(item.get("low")), "close": float(item.get("close")), "volume": float(item.get("volume", 0))})
            else: candles.append({"time": float(item[0]), "open": float(item[1]), "high": float(item[2]), "low": float(item[3]), "close": float(item[4]), "volume": float(item[5]) if len(item) > 5 else 0})
        except Exception: continue
    candles.sort(key=lambda x: x["time"])
    return candles

def get_position_for_symbol(symbol):
    try:
        for pos in delta_request("GET", "/v2/positions", authenticated=True).get("result", []):
            if str(pos.get("product_symbol") or pos.get("symbol") or "").upper() == symbol.upper():
                if abs(float(pos.get("size", 0) or 0)) > 0: return pos
    except Exception: pass
    return None

# ============================================================
# CMC INTEGRATION
# ============================================================

def refresh_cmc_if_needed(force=False):
    global cmc_cache
    if not CMC_API_KEY:
        return {"connected": False, "cached": False, "data": {}, "error": "CMC_API_KEY missing"}

    now = time.time()
    if not force and cmc_cache["timestamp"] and now - cmc_cache["timestamp"] < CMC_CACHE_SECONDS:
        return {"connected": True, "cached": True, "data": cmc_cache["data"], "error": cmc_cache["error"]}

    try:
        response = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
            params={"symbol": ",".join(x.replace("USD", "") for x in SYMBOLS), "convert": "USD"},
            headers={"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accepts": "application/json"},
            timeout=5,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        cmc_cache = {"timestamp": now, "data": data, "error": None}
        return {"connected": True, "cached": False, "data": data, "error": None}
    except Exception as exc:
        cmc_cache["error"] = str(exc)
        return {"connected": False, "cached": False, "data": cmc_cache["data"], "error": str(exc)}

# ============================================================
# INDICATORS & MULTI-TIMEFRAME STRATEGY
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
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
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

def calculate_signal(candles_5m, trend_1h):
    if len(candles_5m) < 60: return {"side": "NO_TRADE", "score": 0, "price": candles_5m[-1]["close"] if candles_5m else None, "reason": "Loading 5M Data"}
    
    closes, volumes = [x["close"] for x in candles_5m], [x["volume"] for x in candles_5m]
    price, ema9, ema21, rsi_val, atr_val = closes[-1], ema(closes, 9), ema(closes, 21), rsi(closes, 14), atr(candles_5m, 14)

    if None in (ema9, ema21, rsi_val, atr_val): return {"side": "NO_TRADE", "score": 0, "price": price, "reason": "Calculating Indicators"}

    ls, ss, rl, rs = 0, 0, [], []

    if trend_1h == "UP": ls += 20; rl.append("1H Trend UP")
    elif trend_1h == "DOWN": ss += 20; rs.append("1H Trend DOWN")
    else: rl.append("1H Trend Flat"); rs.append("1H Trend Flat")

    if ema9 > ema21: ls += 20; rl.append("5M EMA Bullish")
    elif ema9 < ema21: ss += 20; rs.append("5M EMA Bearish")

    mom = closes[-4:]
    if mom[-1] > mom[0]: ls += 10
    elif mom[-1] < mom[0]: ss += 10

    if 52 <= rsi_val <= 68: ls += 15
    if 32 <= rsi_val <= 48: ss += 15
    if rsi_val > 75: ls -= 10
    if rsi_val < 25: ss -= 10

    ph, pl = max(x["high"] for x in candles_5m[-21:-1]), min(x["low"] for x in candles_5m[-21:-1])
    if price > ph: ls += 15; rl.append("5M Breakout")
    if price < pl: ss += 15; rs.append("5M Breakdown")

    rv = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 0
    bv = sum(volumes[-25:-5]) / 20 if len(volumes) >= 25 else 0
    if bv > 0 and (rv / bv) >= 1.2:
        if ls > ss: ls += 15; rl.append("High Vol")
        elif ss > ls: ss += 15; rs.append("High Vol")

    recent_high = max(x["high"] for x in candles_5m[-4:-1])
    recent_low = min(x["low"] for x in candles_5m[-4:-1])

    if ls >= 75 and ls >= ss + 15 and trend_1h != "DOWN":
        return {"side": "LONG", "score": min(ls, 100), "price": price, "trigger_price": recent_high + (atr_val*0.1), "reason": ", ".join(rl), "atr": atr_val, "updated_at": datetime.now(timezone.utc).isoformat()}
    elif ss >= 75 and ss >= ls + 15 and trend_1h != "UP":
        return {"side": "SHORT", "score": min(ss, 100), "price": price, "trigger_price": recent_low - (atr_val*0.1), "reason": ", ".join(rs), "atr": atr_val, "updated_at": datetime.now(timezone.utc).isoformat()}
    
    score = max(min(ls, 100), min(ss, 100))
    reason = f"Wait: Trend Conflict" if (ls>75 and trend_1h=="DOWN") or (ss>75 and trend_1h=="UP") else "No Clear Edge"
    return {"side": "NO_TRADE", "score": score, "price": price, "trigger_price": None, "reason": reason, "atr": atr_val, "updated_at": datetime.now(timezone.utc).isoformat()}

# ============================================================
# IP TRACKER
# ============================================================

def get_public_ip():
    services = [("https://api.ipify.org?format=json", True), ("https://icanhazip.com", False), ("https://ident.me", False)]
    for url, is_json in services:
        try:
            res = requests.get(url, timeout=5)
            ip = res.json().get("ip") if is_json else res.text.strip()
            if ip and "." in ip:
                runtime["public_ip"] = ip
                return
        except Exception: continue
    if not runtime.get("public_ip"): runtime["public_ip"] = "Error Loading IP"

def ip_updater_loop():
    while True:
        get_public_ip()
        time.sleep(60)

def validate_coin_settings(symbol, quantity, leverage):
    rules = product_rules(symbol)
    if not rules.get("available"): return False, rules.get("message", "Product unavailable")
    try: quantity, leverage = float(quantity), float(leverage)
    except: return False, "Must be numeric"
    if quantity <= 0 or leverage <= 0: return False, "Must be > 0"
    return True, "OK"

# ============================================================
# SMART EXECUTION (ARMED & TRAILING)
# ============================================================

def execute_trade(symbol, mode, armed_data):
    try:
        coin = state["coins"][symbol]
        qty, lev = float(coin["quantity"]), float(coin["leverage"])
        side, entry_price, atr_val = armed_data["side"], armed_data["trigger_price"], armed_data["atr"]
        stop = entry_price - (atr_val * 1.5) if side == "LONG" else entry_price + (atr_val * 1.5)

        if mode == "LIVE":
            if get_position_for_symbol(symbol): return False
            set_delta_leverage(symbol, lev)
            result = delta_request("POST", "/v2/orders", body={"product_symbol": symbol, "size": qty, "side": ("buy" if side == "LONG" else "sell"), "order_type": "market_order"}, authenticated=True)
        else:
            result = "PAPER_SIMULATION"

        with lock:
            state["current_trade"] = {
                "symbol": symbol, "side": side, "entry": entry_price, "quantity": qty,
                "leverage": lev, "stop": stop, "entry_atr": atr_val,
                "highest_price": entry_price, "lowest_price": entry_price, "mode": mode,
                "exchange_order": result, "opened_at": datetime.now(timezone.utc).isoformat(), "reason": armed_data["reason"],
            }
            state["coins"][symbol]["armed_signal"] = None
        
        save_state()
        event(f"🚀 {mode} {side} EXECUTED: {symbol} @ {entry_price:.2f}. Initial Stop: {stop:.2f}")
        return True
    except Exception as e:
        event(f"❌ Execution Error on {symbol}: {e}")
        return False

def manage_active_trade():
    with lock: trade = state["current_trade"]
    if not trade: return
    symbol = trade["symbol"]
    try:
        candles = get_candles(symbol, "5m", 10)
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
                        event(f"📈 Trailing Stop (L) Moved UP to: {new_stop:.2f} (Price: {price:.2f})")
                if trade["stop"] and price <= trade["stop"]: close_trade(price, "Trailing Stop Loss Hit")

            elif side == "SHORT":
                if "lowest_price" not in trade: trade["lowest_price"] = trade["entry"]
                if price < trade["lowest_price"]:
                    trade["lowest_price"] = price
                    new_stop = trade["lowest_price"] + (atr_val * 1.5)
                    if trade["stop"] is None or new_stop < trade["stop"]:
                        trade["stop"] = new_stop
                        event(f"📉 Trailing Stop (S) Moved DOWN to: {new_stop:.2f} (Price: {price:.2f})")
                if trade["stop"] and price >= trade["stop"]: close_trade(price, "Trailing Stop Loss Hit")
        save_state()
    except Exception as exc: event(f"Trade management error: {exc}")

def close_trade(price, reason):
    with lock: trade = state["current_trade"]
    if not trade: return False

    if trade["mode"] == "LIVE":
        try:
            pos = get_position_for_symbol(trade["symbol"])
            if pos and float(pos.get("size", 0)) > 0:
                delta_request("POST", "/v2/orders", body={"product_symbol": trade["symbol"], "size": abs(float(pos["size"])), "side": "sell" if str(pos.get("side", "")).lower() == "buy" else "buy", "order_type": "market_order", "reduce_only": True}, authenticated=True)
        except Exception as e:
            event(f"❌ Failed to close LIVE trade: {e}")
            return False
            
    event(f"🔒 {trade['mode']} trade closed: {trade['symbol']} @ {price} ({reason})")
    with lock: state["current_trade"] = None
    save_state()
    return True

# ============================================================
# MASTER SCANNER (ISOLATED ERRORS)
# ============================================================

def scan_all_coins():
    results = {}
    has_global_error = False
    
    for symbol in SYMBOLS:
        try:
            candles_1h = get_candles(symbol, "1h", CANDLE_LIMIT_HTF)
            trend_1h = "NEUTRAL"
            if len(candles_1h) > 21:
                c1h = [x["close"] for x in candles_1h]
                e9, e21 = ema(c1h, 9), ema(c1h, 21)
                if e9 and e21: trend_1h = "UP" if e9 > e21 else "DOWN"

            candles_5m = get_candles(symbol, "5m", CANDLE_LIMIT_LTF)
            signal = calculate_signal(candles_5m, trend_1h)
            
            with lock: state["coins"][symbol]["last_signal"] = signal
            results[symbol] = signal
            
            time.sleep(0.3) # DONT SPAM DELTA
            
        except Exception as exc:
            has_global_error = True
            err_msg = str(exc)
            with lock: 
                state["coins"][symbol]["last_signal"]["reason"] = f"Error: {err_msg}"
                state["coins"][symbol]["last_signal"]["score"] = 0
            runtime["last_scan_error"] = f"{symbol} Failed: {err_msg}"
            
    if not has_global_error:
        runtime["last_scan_error"] = None
        
    save_state()
    return results

def trading_cycle():
    runtime["is_scanning"] = True
    try:
        signals = scan_all_coins()

        with lock: trade = state["current_trade"]
        if trade:
            manage_active_trade()
            return

        with lock: system_on, selected = bool(state["system_on"]), state["selected_coin"]
        if not system_on or selected not in SYMBOLS: return
        
        with lock: coin_data = state["coins"][selected]
        if not coin_data["enabled"]: return

        armed_data = coin_data.get("armed_signal")
        if not armed_data: return

        if time.time() > armed_data["expiry_time"]:
            with lock:
                state["coins"][selected]["enabled"] = False
                state["coins"][selected]["armed_signal"] = None
                if state["selected_coin"] == selected: state["selected_coin"] = None
            save_state()
            event(f"⏳ {selected} {armed_data['side']} Signal EXPIRED. System Disarmed.")
            return

        current_price = signals.get(selected, {}).get("price")
        if not current_price: return

        triggered = False
        if armed_data["side"] == "LONG" and current_price >= armed_data["trigger_price"]: triggered = True
        elif armed_data["side"] == "SHORT" and current_price <= armed_data["trigger_price"]: triggered = True

        if triggered: execute_trade(selected, state["mode"], armed_data)

    except Exception as exc:
        runtime["last_scan_error"] = f"Engine Error: {str(exc)}"
    finally:
        runtime["is_scanning"] = False
        runtime["last_scan"] = time.time()

def engine_loop():
    event("🟢 Background Scanner Engine Started.")
    while True:
        started = time.time()
        trading_cycle()
        time.sleep(max(0.5, SCAN_SECONDS - (time.time() - started)))

# ============================================================
# API ENDPOINTS
# ============================================================

@app.get("/api/state")
def api_state():
    with lock: snapshot = json.loads(json.dumps(state))
    cmc_status = refresh_cmc_if_needed(force=False)
    snapshot["runtime"] = runtime
    
    # Engine is running if actively scanning, OR finished a scan less than 15s ago
    snapshot["engine_running"] = runtime["is_scanning"] or bool(time.time() - runtime["last_scan"] < 15)
    
    snapshot["cmc_connected"] = cmc_status["connected"]
    snapshot["cmc_error"] = cmc_status["error"]
    return jsonify(snapshot)

@app.get("/api/coin/<symbol>/rules")
def api_coin_rules(symbol):
    rules = product_rules(symbol.upper())
    if not rules.get("available"): return jsonify({"success": False, "error": rules.get("message")}), 400
    return jsonify({"success": True, "rules": rules})

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
            signal = state["coins"][symbol.upper()]["last_signal"]
            if signal["side"] == "NO_TRADE":
                return jsonify({"success": False, "error": "Cannot Arm: No valid signal right now."}), 400
            for other in SYMBOLS: state["coins"][other]["enabled"] = (other == symbol.upper())
            state["selected_coin"] = symbol.upper()
            state["coins"][symbol.upper()]["enabled"] = True
            state["coins"][symbol.upper()]["armed_signal"] = {
                "side": signal["side"],
                "trigger_price": signal["trigger_price"],
                "expiry_time": time.time() + (20 * 60),
                "atr": signal["atr"],
                "reason": signal["reason"]
            }
            event(f"🔫 {symbol.upper()} ARMED for {signal['side']}. Waiting for Price to cross {signal['trigger_price']:.2f}")
        else:
            state["coins"][symbol.upper()]["enabled"] = False
            state["coins"][symbol.upper()]["armed_signal"] = None
            if state["selected_coin"] == symbol.upper(): state["selected_coin"] = None
            event(f"🛑 {symbol.upper()} DISARMED manually.")
    save_state()
    return jsonify({"success": True})

@app.post("/api/system")
def api_system():
    enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
    with lock: state["system_on"] = enabled
    save_state()
    if enabled: event("⚡ SYSTEM ON: Execution Engine Active")
    else: event("⏸️ SYSTEM OFF: Execution Engine Paused")
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
        candles = get_candles(trade["symbol"], "5m", 10)
        price = candles[-1]["close"] if candles else trade.get("entry")
        ok = close_trade(price, "Manual close")
        if not ok: return jsonify({"success": False, "error": "Failed to close trade. Check Event Log."}), 500
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
.small { color: #71849a; font-size: 11px; }
.margin-calc { background: #16263a; padding: 10px; border-radius: 8px; margin-top: 15px; font-size: 14px; font-weight: bold; text-align: center; color: #ffc857; }
</style>
</head>
<body>

<header>
    <h1>Prime Minister AI (Pro Sniper)</h1>
    <div class="subtitle">Multi-Timeframe Smart Trigger System</div>
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
            <button onclick="systemToggle()">SYSTEM ON / OFF</button>
            <button onclick="setMode('PAPER')">PAPER</button>
            <button onclick="setMode('LIVE')">LIVE</button>
            <button onclick="manualClose()">CLOSE CURRENT TRADE</button>
        </div>
    </div>
    
    <div class="section">
        <div class="panel">
            <div class="label">CONNECTIONS & HEALTH</div>
            <div id="connections" style="margin-top:12px"></div>
        </div>
    </div>

    <div class="section">
        <div class="panel">
            <div class="label">TOP 5 MARKET WATCH (Click to ARM Sniper)</div>
            <div id="coins" class="coin-grid" style="margin-top:12px"></div>
        </div>
    </div>

    <div class="section">
        <div class="panel">
            <div class="label">ACTIVE TRADE / TRAILING DATA</div>
            <div id="trade" style="margin-top:12px">None</div>
        </div>
    </div>

    <div class="section">
        <div class="panel">
            <div class="label">EVENTS / ALERTS LOG</div>
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
        <div class="form-row"><label>Quantity</label><input id="quantity" type="number" step="any" oninput="calculateMargin()" /></div>
        <div class="form-row"><label>Leverage</label><select id="leverage" onchange="calculateMargin()"></select></div>
        <div id="marginDisplay" class="margin-calc">Estimated Margin Required: -- USD</div>
        <div class="controls" style="margin-top:16px">
            <button onclick="saveSettings()">SAVE SETTINGS</button>
            <button onclick="toggleSelectedCoin()">ARM / DISARM</button>
        </div>
    </div>
</div>

<script>
let appState = null;
let selectedModalCoin = null;
let currentModalRules = null;
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

function calculateMargin() {
    if(!appState || !selectedModalCoin || !currentModalRules) return;
    const qty = Number(document.getElementById("quantity").value);
    const lev = Number(document.getElementById("leverage").value);
    const price = appState.coins[selectedModalCoin].last_signal.price;
    const cv = currentModalRules.contract_value || 1;
    
    if(qty > 0 && lev > 0 && price > 0) {
        const margin = (qty * cv * price) / lev;
        document.getElementById("marginDisplay").innerText = `Estimated Margin Required: $${margin.toFixed(2)}`;
    } else {
        document.getElementById("marginDisplay").innerText = `Estimated Margin Required: -- USD`;
    }
}

function checkAndNotify(state) {
    if (state.runtime.last_scan_error) {
        if (lastNotifiedSignals["error"] !== state.runtime.last_scan_error) {
            lastNotifiedSignals["error"] = state.runtime.last_scan_error;
            if (Notification.permission === "granted") new Notification("⚠️ Error Alert", { body: state.runtime.last_scan_error });
        }
    } else { lastNotifiedSignals["error"] = null; }

    for (const symbol of ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"]) {
        const signal = state.coins[symbol]?.last_signal;
        if (!signal) continue;
        if (signal.score >= 75 && signal.side !== "NO_TRADE") {
            if (lastNotifiedSignals[symbol] !== signal.side) {
                lastNotifiedSignals[symbol] = signal.side;
                if (Notification.permission === "granted") {
                    new Notification(`🚀 Setup Ready: ${symbol}`, { body: `Target Breakout Price: ${signal.trigger_price.toFixed(2)}` });
                }
            }
        } else { lastNotifiedSignals[symbol] = null; }
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
    
    document.getElementById("connections").innerHTML = `
        <div class="rule"><span>Delta API Status</span><span class="${state.runtime.last_scan_error ? 'red' : 'green'}">${state.runtime.last_scan_error ? 'CONNECTION ERROR' : 'CONNECTED & READY'}</span></div>
        <div class="rule"><span>Delta Error Detail</span><span class="${state.runtime.last_scan_error ? 'red' : 'gray'}">${state.runtime.last_scan_error ? state.runtime.last_scan_error : 'None'}</span></div>
        <div class="rule"><span>CoinMarketCap API</span><span class="${state.cmc_connected ? 'green' : 'red'}">${state.cmc_connected ? 'CONNECTED' : (state.cmc_error ? state.cmc_error : 'NOT CONNECTED')}</span></div>
        <div class="rule"><span>Background Scanner</span><span class="${state.engine_running ? 'green' : 'red'}">${state.engine_running ? 'SCANNING LIVE' : 'STOPPED/LOADING'}</span></div>
    `;

    const coins = document.getElementById("coins");
    coins.innerHTML = "";

    for (const symbol of ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD"]) {
        const coin = state.coins[symbol];
        const signal = coin.last_signal || {};
        const armed = coin.armed_signal;

        let dotClass = "dot-none";
        let dotTitle = "No Setup";
        if (signal.score >= 75) {
            dotClass = signal.side === "LONG" ? "dot-green" : "dot-red";
            dotTitle = `${signal.side} Setup Ready!`;
        }

        let badgeHtml = coin.enabled ? `<div class="badge badge-armed">ARMED</div>` : `<div class="badge">OFF</div>`;
        let statusDisplay = "";

        if (armed) {
            statusDisplay = `<div class="yellow" style="font-size:12px; margin-top:5px;">⏳ Waiting to break: <b>${armed.trigger_price.toFixed(2)}</b></div>`;
        } else {
            let trg = signal.trigger_price ? `Breakout lvl: ${signal.trigger_price.toFixed(2)}` : "";
            statusDisplay = `<div class="gray" style="font-size:12px; margin-top:5px;">${trg}</div>`;
        }

        const div = document.createElement("div");
        div.className = "panel coin " + (coin.enabled ? "on" : "");
        div.onclick = () => openCoin(symbol);
        div.innerHTML = `
            <div class="coin-title">
                <div class="coin-name">${symbol} <span class="setup-dot ${dotClass}" title="${dotTitle}"></span></div>
                ${badgeHtml}
            </div>
            <div class="signal ${signalClass(signal.side)}">${signal.side || "NO_TRADE"}</div>
            <div class="price">${money(signal.price)}</div>
            ${statusDisplay}
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
        if (item.message.toLowerCase().includes("error") || item.message.toLowerCase().includes("failed") || item.message.includes("HTTP")) div.style.color = "#ff6577";
        else if (item.message.includes("Trailing Stop")) div.style.color = "#ffc857";
        else if (item.message.includes("EXECUTED") || item.message.includes("ARMED") || item.message.includes("Started")) div.style.color = "#45e09b";
        else if (item.message.includes("EXPIRED") || item.message.includes("DISARMED") || item.message.includes("Paused")) div.style.color = "#8294aa";
        
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
    const data = await res.json();
    if (!data.success) alert(data.error);
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
    
    document.getElementById("modal").classList.add("show");
    
    currentModalRules = { contract_value: 1 };
    calculateMargin();
    
    await loadRules(symbol);
}

async function loadRules(symbol) {
    try {
        const res = await fetch(`/api/coin/${symbol}/rules`, {cache: "no-store"});
        const data = await res.json();
        if (!data.success) { 
            document.getElementById("rules").innerHTML = `<div class="red">${data.error}</div>`; 
            return; 
        }
        currentModalRules = data.rules;
        renderRules(data.rules);
        calculateMargin();
    } catch (e) { 
        document.getElementById("rules").innerHTML = `<div class="red">Failed to load Delta rules</div>`;
    }
}

function renderRules(rules) {
    const box = document.getElementById("rules");
    const maxRaw = rules.max_leverage ?? rules.default_leverage ?? 100;
    const max = Number.isFinite(Number(maxRaw)) ? Number(maxRaw) : 100;
    const min = Number.isFinite(Number(rules.min_leverage)) ? Number(rules.min_leverage) : 1;

    box.innerHTML = `
        <div class="rule"><span>Contract Value</span><span>${rules.contract_value ?? "--"}</span></div>
        <div class="rule"><span>Min qty</span><span>${rules.min_quantity ?? "--"}</span></div>
        <div class="rule"><span>Max qty</span><span>${rules.max_quantity ?? "--"}</span></div>
        <div class="rule"><span>Step</span><span>${rules.quantity_step ?? "--"}</span></div>
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

def startup():
    get_public_ip()
    threading.Thread(target=ip_updater_loop, daemon=True).start()
    threading.Thread(target=engine_loop, daemon=True).start()

startup()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
