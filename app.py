import os
import json
import time
import hmac
import hashlib
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

DELTA_BASE = "https://api.india.delta.exchange"
STATE_FILE = "bot_state.json"

API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

USER_AGENT = "PrimeMinisterAI/1.0"
SCAN_INTERVAL = 5

SYMBOLS = [
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "XRPUSD",
    "DOGEUSD",
]

DEFAULT_STATE = {
    "enabled": False,
    "mode": "PAPER",
    "quantity": 1,
    "leverage": 2,
    "timeframe": "5m",
    "symbols": SYMBOLS,
    "adaptive_target": True,
    "auto_trailing": True,
    "trade": None,
    "last_signal": None,
    "last_ip": None,
    "events": [],
}


state_lock = threading.Lock()
engine_thread = None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_state():
    if not os.path.exists(STATE_FILE):
        return DEFAULT_STATE.copy()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)

        state = DEFAULT_STATE.copy()
        state.update(saved)
        return state
    except Exception:
        return DEFAULT_STATE.copy()


state = load_state()


def save_state():
    with state_lock:
        tmp = STATE_FILE + ".tmp"

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        os.replace(tmp, STATE_FILE)


def add_event(message):
    event = {
        "time": utc_now(),
        "message": str(message),
    }

    with state_lock:
        state.setdefault("events", [])
        state["events"].insert(0, event)
        state["events"] = state["events"][:100]

    save_state()


def public_ip():
    try:
        r = requests.get(
            "https://api.ipify.org",
            timeout=5,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        return r.text.strip()
    except Exception:
        return None


def delta_signature(method, path, timestamp, body=""):
    payload = f"{method}{timestamp}{path}{body}"

    return hmac.new(
        API_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def delta_request(
    method,
    path,
    params=None,
    body=None,
    authenticated=False,
    timeout=8,
):
    url = DELTA_BASE + path

    body_text = ""

    if body is not None:
        body_text = json.dumps(
            body,
            separators=(",", ":"),
        )

    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }

    if authenticated:
        if not API_KEY or not API_SECRET:
            raise RuntimeError("Delta API credentials are not configured.")

        timestamp = str(int(time.time()))

        headers.update(
            {
                "api-key": API_KEY,
                "timestamp": timestamp,
                "signature": delta_signature(
                    method.upper(),
                    path,
                    timestamp,
                    body_text,
                ),
            }
        )

    response = requests.request(
        method=method.upper(),
        url=url,
        params=params,
        data=body_text if body is not None else None,
        headers=headers,
        timeout=timeout,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Delta API error {response.status_code}: {response.text[:500]}"
        )

    try:
        return response.json()
    except Exception:
        return {}


def get_products():
    data = delta_request(
        "GET",
        "/v2/products",
        authenticated=False,
    )

    return data.get("result", [])


def find_product(symbol):
    products = get_products()

    for product in products:
        if str(product.get("symbol", "")).upper() == symbol.upper():
            return product

    return None


def get_ticker(symbol):
    data = delta_request(
        "GET",
        "/v2/tickers",
        params={"symbol": symbol},
        authenticated=False,
    )

    result = data.get("result", [])

    if isinstance(result, list) and result:
        return result[0]

    if isinstance(result, dict):
        return result

    return {}


def get_candles(symbol, resolution="5m", limit=150):
    data = delta_request(
        "GET",
        "/v2/history/candles",
        params={
            "symbol": symbol,
            "resolution": resolution,
            "start": int(time.time()) - (limit * 300),
            "end": int(time.time()),
        },
        authenticated=False,
    )

    candles = data.get("result", [])

    if not isinstance(candles, list):
        return []

    normalized = []

    for candle in candles:
        try:
            if isinstance(candle, dict):
                normalized.append(
                    {
                        "open": float(candle["open"]),
                        "high": float(candle["high"]),
                        "low": float(candle["low"]),
                        "close": float(candle["close"]),
                        "volume": float(candle.get("volume", 0)),
                    }
                )
            elif isinstance(candle, list) and len(candle) >= 6:
                normalized.append(
                    {
                        "open": float(candle[1]),
                        "high": float(candle[2]),
                        "low": float(candle[3]),
                        "close": float(candle[4]),
                        "volume": float(candle[5]),
                    }
                )
        except Exception:
            continue

    return normalized


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (value - result) * multiplier + result

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if len(candles) <= period:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    return sum(true_ranges[-period:]) / period


def analyze_market(symbol):
    candles = get_candles(
        symbol,
        state.get("timeframe", "5m"),
        150,
    )

    if len(candles) < 30:
        return None

    closes = [x["close"] for x in candles]
    volumes = [x["volume"] for x in candles]

    current = candles[-1]
    previous = candles[-2]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi14 = rsi(closes, 14)
    atr14 = atr(candles, 14)

    if None in (ema9, ema21, rsi14, atr14):
        return None

    recent_volume = volumes[-1]

    volume_base = (
        sum(volumes[-21:-1]) / 20
        if len(volumes) >= 21
        else max(sum(volumes) / len(volumes), 1)
    )

    volume_ratio = (
        recent_volume / volume_base
        if volume_base
        else 1
    )

    recent_high = max(x["high"] for x in candles[-21:-1])
    recent_low = min(x["low"] for x in candles[-21:-1])

    score_long = 0
    score_short = 0

    if ema9 > ema21:
        score_long += 25

    if ema9 < ema21:
        score_short += 25

    if current["close"] > previous["close"]:
        score_long += 10

    if current["close"] < previous["close"]:
        score_short += 10

    if rsi14 >= 52:
        score_long += 15

    if rsi14 <= 48:
        score_short += 15

    if current["close"] > recent_high:
        score_long += 20

    if current["close"] < recent_low:
        score_short += 20

    if volume_ratio >= 1.2:
        if current["close"] > previous["close"]:
            score_long += 15
        elif current["close"] < previous["close"]:
            score_short += 15

    if score_long >= 75 and score_long > score_short + 10:
        direction = "LONG"
        score = score_long
    elif score_short >= 75 and score_short > score_long + 10:
        direction = "SHORT"
        score = score_short
    else:
        direction = "NO_TRADE"
        score = max(score_long, score_short)

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "long_score": score_long,
        "short_score": score_short,
        "price": current["close"],
        "ema9": ema9,
        "ema21": ema21,
        "rsi": rsi14,
        "atr": atr14,
        "volume_ratio": volume_ratio,
        "time": utc_now(),
    }


def get_positions():
    if not API_KEY or not API_SECRET:
        return []

    data = delta_request(
        "GET",
        "/v2/positions",
        authenticated=True,
    )

    result = data.get("result", [])

    return result if isinstance(result, list) else []


def get_open_position(symbol=None):
    positions = get_positions()

    for position in positions:
        size = float(position.get("size", 0) or 0)

        if size == 0:
            continue

        if symbol:
            if str(position.get("symbol", "")).upper() != symbol.upper():
                continue

        return position

    return None


def set_leverage(product_id, leverage):
    return delta_request(
        "POST",
        f"/v2/products/{product_id}/orders/leverage",
        body={
            "leverage": str(leverage),
        },
        authenticated=True,
    )


def place_market_order(symbol, side, size):
    return delta_request(
        "POST",
        "/v2/orders",
        body={
            "product_symbol": symbol,
            "order_type": "market_order",
            "size": size,
            "side": side,
        },
        authenticated=True,
    )


def close_market_order(symbol, size, position_side):
    side = "sell" if position_side == "LONG" else "buy"

    return place_market_order(
        symbol,
        side,
        abs(size),
    )


def has_any_live_position():
    try:
        return get_open_position() is not None
    except Exception:
        return False


def paper_enter(signal):
    with state_lock:
        state["trade"] = {
            "symbol": signal["symbol"],
            "side": signal["direction"],
            "entry": signal["price"],
            "size": state["quantity"],
            "leverage": state["leverage"],
            "highest": signal["price"],
            "lowest": signal["price"],
            "initial_atr": signal["atr"],
            "opened_at": utc_now(),
            "mode": "PAPER",
        }

    save_state()

    add_event(
        f"PAPER ENTRY {signal['direction']} "
        f"{signal['symbol']} @ {signal['price']}"
    )


def paper_manage(signal):
    with state_lock:
        trade = state.get("trade")

    if not trade:
        return

    if trade["symbol"] != signal["symbol"]:
        return

    entry = float(trade["entry"])
    atr_value = float(trade["initial_atr"])

    price = float(signal["price"])

    if trade["side"] == "LONG":
        trade["highest"] = max(
            float(trade.get("highest", entry)),
            price,
        )

        peak = float(trade["highest"])

        stop = max(
            entry - atr_value * 1.5,
            peak - atr_value * 1.8,
        )

        if price <= stop:
            pnl = (price - entry) * trade["size"]

            with state_lock:
                state["trade"] = None

            save_state()

            add_event(
                f"PAPER EXIT LONG {trade['symbol']} "
                f"@ {price} PnL={pnl:.4f}"
            )
            return

        if (
            signal["direction"] == "SHORT"
            and signal["score"] >= 85
        ):
            pnl = (price - entry) * trade["size"]

            with state_lock:
                state["trade"] = None

            save_state()

            add_event(
                f"PAPER REVERSAL EXIT LONG "
                f"{trade['symbol']} @ {price} PnL={pnl:.4f}"
            )

    elif trade["side"] == "SHORT":
        trade["lowest"] = min(
            float(trade.get("lowest", entry)),
            price,
        )

        low = float(trade["lowest"])

        stop = min(
            entry + atr_value * 1.5,
            low + atr_value * 1.8,
        )

        if price >= stop:
            pnl = (entry - price) * trade["size"]

            with state_lock:
                state["trade"] = None

            save_state()

            add_event(
                f"PAPER EXIT SHORT {trade['symbol']} "
                f"@ {price} PnL={pnl:.4f}"
            )
            return

        if (
            signal["direction"] == "LONG"
            and signal["score"] >= 85
        ):
            pnl = (entry - price) * trade["size"]

            with state_lock:
                state["trade"] = None

            save_state()

            add_event(
                f"PAPER REVERSAL EXIT SHORT "
                f"{trade['symbol']} @ {price} PnL={pnl:.4f}"
            )
            return

    with state_lock:
        state["trade"] = trade

    save_state()


def live_enter(signal):
    if not API_KEY or not API_SECRET:
        add_event("LIVE ENTRY BLOCKED: API credentials missing.")
        return

    product = find_product(signal["symbol"])

    if not product:
        add_event(
            f"LIVE ENTRY BLOCKED: product not found "
            f"{signal['symbol']}"
        )
        return

    product_id = product.get("id")

    try:
        set_leverage(
            product_id,
            state["leverage"],
        )

        side = (
            "buy"
            if signal["direction"] == "LONG"
            else "sell"
        )

        result = place_market_order(
            signal["symbol"],
            side,
            state["quantity"],
        )

        order = result.get("result", result)

        with state_lock:
            state["trade"] = {
                "symbol": signal["symbol"],
                "side": signal["direction"],
                "entry": signal["price"],
                "size": state["quantity"],
                "leverage": state["leverage"],
                "highest": signal["price"],
                "lowest": signal["price"],
                "initial_atr": signal["atr"],
                "opened_at": utc_now(),
                "mode": "LIVE",
                "order": order,
                "protective_stop": None,
            }

        save_state()

        add_event(
            f"LIVE ENTRY {signal['direction']} "
            f"{signal['symbol']} @ {signal['price']}"
        )

        add_event(
            "WARNING: live protective bracket must be "
            "attached/reconciled before live deployment."
        )

    except Exception as exc:
        add_event(
            f"LIVE ENTRY ERROR: {exc}"
        )


def live_manage(signal):
    with state_lock:
        trade = state.get("trade")

    if not trade:
        return

    position = get_open_position(trade["symbol"])

    if not position:
        with state_lock:
            state["trade"] = None

        save_state()

        add_event(
            f"Position closed externally: {trade['symbol']}"
        )
        return

    size = float(position.get("size", 0) or 0)

    entry = float(
        position.get("entry_price")
        or position.get("entryPrice")
        or trade["entry"]
    )

    price = float(signal["price"])

    trade["entry"] = entry

    if trade["side"] == "LONG":
        trade["highest"] = max(
            float(trade.get("highest", entry)),
            price,
        )

        if (
            signal["direction"] == "SHORT"
            and signal["score"] >= 85
        ):
            try:
                close_market_order(
                    trade["symbol"],
                    size,
                    "LONG",
                )

                with state_lock:
                    state["trade"] = None

                save_state()

                add_event(
                    f"LIVE EXIT LONG "
                    f"{trade['symbol']} @ {price}"
                )

            except Exception as exc:
                add_event(
                    f"LIVE EXIT ERROR: {exc}"
                )

    elif trade["side"] == "SHORT":
        trade["lowest"] = min(
            float(trade.get("lowest", entry)),
            price,
        )

        if (
            signal["direction"] == "LONG"
            and signal["score"] >= 85
        ):
            try:
                close_market_order(
                    trade["symbol"],
                    size,
                    "SHORT",
                )

                with state_lock:
                    state["trade"] = None

                save_state()

                add_event(
                    f"LIVE EXIT SHORT "
                    f"{trade['symbol']} @ {price}"
                )

            except Exception as exc:
                add_event(
                    f"LIVE EXIT ERROR: {exc}"
                )

    with state_lock:
        state["trade"] = trade

    save_state()


def trading_cycle():
    while True:
        try:
            with state_lock:
                enabled = bool(state.get("enabled"))
                mode = state.get("mode", "PAPER")
                trade = state.get("trade")

            # OFF means the bot performs no trading action.
            if not enabled:
                time.sleep(SCAN_INTERVAL)
                continue

            # Reconcile local state with exchange before acting.
            if mode == "LIVE":
                try:
                    exchange_position = get_open_position()

                    if exchange_position is None and trade:
                        with state_lock:
                            state["trade"] = None

                        save_state()

                        add_event(
                            "Reconciliation: no exchange position; "
                            "local trade cleared."
                        )

                    elif exchange_position and not trade:
                        add_event(
                            "SAFETY BLOCK: exchange position exists "
                            "but local state has no trade."
                        )

                        time.sleep(SCAN_INTERVAL)
                        continue

                except Exception as exc:
                    add_event(
                        f"Reconciliation error: {exc}"
                    )

                    time.sleep(SCAN_INTERVAL)
                    continue

            # Existing trade gets priority.
            if trade:
                signal = analyze_market(
                    trade["symbol"]
                )

                if signal:
                    with state_lock:
                        state["last_signal"] = signal

                    save_state()

                    if mode == "PAPER":
                        paper_manage(signal)
                    elif mode == "LIVE":
                        live_manage(signal)

                time.sleep(SCAN_INTERVAL)
                continue

            # Hard one-trade safety rule.
            if mode == "LIVE":
                try:
                    if has_any_live_position():
                        add_event(
                            "ENTRY BLOCKED: an exchange position "
                            "already exists."
                        )

                        time.sleep(SCAN_INTERVAL)
                        continue
                except Exception as exc:
                    add_event(
                        f"Position check failed: {exc}"
                    )

                    time.sleep(SCAN_INTERVAL)
                    continue

            best_signal = None

            for symbol in state.get("symbols", SYMBOLS):
                try:
                    signal = analyze_market(symbol)

                    if not signal:
                        continue

                    if (
                        best_signal is None
                        or signal["score"]
                        > best_signal["score"]
                    ):
                        best_signal = signal

                except Exception as exc:
                    add_event(
                        f"Analysis error {symbol}: {exc}"
                    )

            if best_signal:
                with state_lock:
                    state["last_signal"] = best_signal

                save_state()

                if best_signal["direction"] != "NO_TRADE":
                    if mode == "PAPER":
                        paper_enter(best_signal)

                    elif mode == "LIVE":
                        live_enter(best_signal)

            time.sleep(SCAN_INTERVAL)

        except Exception as exc:
            add_event(
                f"ENGINE ERROR: {exc}"
            )

            time.sleep(SCAN_INTERVAL)


def start_engine():
    global engine_thread

    if engine_thread and engine_thread.is_alive():
        return

    engine_thread = threading.Thread(
        target=trading_cycle,
        daemon=True,
    )

    engine_thread.start()


@app.route("/")
def index():
    return render_template_string(
        HTML,
        state=state,
    )


@app.route("/api/status")
def api_status():
    with state_lock:
        snapshot = json.loads(
            json.dumps(state)
        )

    return jsonify(snapshot)


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(silent=True) or {}

    mode = str(
        data.get(
            "mode",
            state.get("mode", "PAPER"),
        )
    ).upper()

    if mode not in ("PAPER", "LIVE"):
        return jsonify(
            {
                "ok": False,
                "error": "Invalid mode.",
            }
        ), 400

    try:
        quantity = float(
            data.get(
                "quantity",
                state["quantity"],
            )
        )

        leverage = int(
            data.get(
                "leverage",
                state["leverage"],
            )
        )

        if quantity <= 0:
            raise ValueError

        if leverage <= 0:
            raise ValueError

    except Exception:
        return jsonify(
            {
                "ok": False,
                "error": "Invalid quantity or leverage.",
            }
        ), 400

    with state_lock:
        state["mode"] = mode
        state["quantity"] = quantity
        state["leverage"] = leverage

    save_state()

    add_event(
        f"Configuration updated: "
        f"mode={mode}, quantity={quantity}, leverage={leverage}"
    )

    return jsonify(
        {
            "ok": True,
            "state": state,
        }
    )


@app.route("/api/start", methods=["POST"])
def api_start():
    with state_lock:
        state["enabled"] = True

    save_state()

    add_event("SYSTEM ON: automatic trading enabled.")

    return jsonify(
        {
            "ok": True,
            "enabled": True,
        }
    )


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with state_lock:
        state["enabled"] = False

    save_state()

    add_event(
        "SYSTEM OFF: bot trading actions disabled. "
        "Existing exchange-side orders, if any, remain exchange-controlled."
    )

    return jsonify(
        {
            "ok": True,
            "enabled": False,
        }
    )


@app.route("/api/close", methods=["POST"])
def api_close():
    with state_lock:
        trade = state.get("trade")
        mode = state.get("mode")

    if not trade:
        return jsonify(
            {
                "ok": False,
                "error": "No bot trade recorded.",
            }
        ), 400

    try:
        if mode == "PAPER":
            with state_lock:
                state["trade"] = None

            save_state()

            add_event(
                f"Manual PAPER close: {trade['symbol']}"
            )

            return jsonify({"ok": True})

        position = get_open_position(
            trade["symbol"]
        )

        if not position:
            with state_lock:
                state["trade"] = None

            save_state()

            return jsonify({"ok": True})

        size = float(
            position.get("size", 0) or 0
        )

        position_side = (
            "LONG"
            if size > 0
            else "SHORT"
        )

        close_market_order(
            trade["symbol"],
            size,
            position_side,
        )

        with state_lock:
            state["trade"] = None

        save_state()

        add_event(
            f"Manual LIVE close: {trade['symbol']}"
        )

        return jsonify({"ok": True})

    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
            }
        ), 500


@app.route("/api/ip")
def api_ip():
    ip = public_ip()

    with state_lock:
        state["last_ip"] = ip

    save_state()

    return jsonify(
        {
            "ip": ip,
        }
    )


HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Prime Minister AI</title>

<style>
body {
    margin: 0;
    font-family: Arial, sans-serif;
    background: #101318;
    color: #f5f5f5;
}

header {
    padding: 18px;
    background: #171b22;
    border-bottom: 1px solid #303640;
}

h1 {
    margin: 0;
    font-size: 23px;
}

.container {
    padding: 15px;
    max-width: 900px;
    margin: auto;
}

.card {
    background: #181d25;
    border: 1px solid #303640;
    border-radius: 12px;
    padding: 15px;
    margin-bottom: 14px;
}

.row {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
}

button,
select,
input {
    border: 1px solid #3b4350;
    background: #222832;
    color: white;
    border-radius: 8px;
    padding: 11px;
}

button {
    cursor: pointer;
}

.on {
    background: #173b24;
}

.off {
    background: #3b2020;
}

.big {
    font-size: 20px;
    font-weight: bold;
}

pre {
    white-space: pre-wrap;
    word-break: break-word;
}

.event {
    border-bottom: 1px solid #2b313b;
    padding: 8px 0;
    font-size: 13px;
}

.small {
    color: #aeb6c2;
    font-size: 13px;
}
</style>
</head>

<body>

<header>
    <h1>Prime Minister AI</h1>
    <div class="small">
        Local deterministic trading commander
    </div>
</header>

<div class="container">

<div class="card">
    <div class="big">
        System:
        <span id="system">OFF</span>
    </div>

    <br>

    <div class="row">
        <button onclick="startBot()">SYSTEM ON</button>
        <button onclick="stopBot()">SYSTEM OFF</button>
        <button onclick="closeTrade()">MANUAL CLOSE</button>
    </div>
</div>

<div class="card">

    <h3>Configuration</h3>

    <div class="row">

        <label>
            Mode<br>
            <select id="mode">
                <option value="PAPER">PAPER</option>
                <option value="LIVE">LIVE</option>
            </select>
        </label>

        <label>
            Quantity<br>
            <input id="quantity"
                   type="number"
                   min="0.0001"
                   step="0.0001">
        </label>

        <label>
            Leverage<br>
            <input id="leverage"
                   type="number"
                   min="1"
                   step="1">
        </label>

    </div>

    <br>

    <button onclick="saveConfig()">
        SAVE CONFIG
    </button>

</div>

<div class="card">

    <h3>Current Trade</h3>

    <pre id="trade">No trade</pre>

</div>

<div class="card">

    <h3>Latest Signal</h3>

    <pre id="signal">No signal</pre>

</div>

<div class="card">

    <h3>Public IP</h3>

    <div id="ip">Checking...</div>

    <br>

    <button onclick="refreshIP()">
        CHECK IP
    </button>

</div>

<div class="card">

    <h3>Events</h3>

    <div id="events"></div>

</div>

</div>

<script>

async function api(url, options = {}) {
    const response = await fetch(url, {
        headers: {
            "Content-Type": "application/json"
        },
        ...options
    });

    const data = await response.json();

    if (!response.ok) {
        throw new Error(
            data.error || "Request failed"
        );
    }

    return data;
}


async function startBot() {
    try {
        await api("/api/start", {
            method: "POST"
        });

        refresh();
    } catch (e) {
        alert(e.message);
    }
}


async function stopBot() {
    try {
        await api("/api/stop", {
            method: "POST"
        });

        refresh();
    } catch (e) {
        alert(e.message);
    }
}


async function closeTrade() {
    if (!confirm("Close current trade?")) {
        return;
    }

    try {
        await api("/api/close", {
            method: "POST"
        });

        refresh();
    } catch (e) {
        alert(e.message);
    }
}


async function saveConfig() {
    try {
        await api("/api/config", {
            method: "POST",
            body: JSON.stringify({
                mode: document.getElementById("mode").value,
                quantity: Number(
                    document.getElementById("quantity").value
                ),
                leverage: Number(
                    document.getElementById("leverage").value
                )
            })
        });

        refresh();

    } catch (e) {
        alert(e.message);
    }
}


async function refreshIP() {
    try {
        const data = await api("/api/ip");

        document.getElementById("ip").textContent =
            data.ip || "Unable to detect";

    } catch (e) {
        document.getElementById("ip").textContent =
            "Unable to detect";
    }
}


function pretty(value) {
    if (!value) {
        return "None";
    }

    return JSON.stringify(
        value,
        null,
        2
    );
}


async function refresh() {
    try {
        const data = await api("/api/status");

        const system =
            document.getElementById("system");

        system.textContent =
            data.enabled ? "ON" : "OFF";

        system.className =
            data.enabled ? "on" : "off";

        document.getElementById("mode").value =
            data.mode;

        document.getElementById("quantity").value =
            data.quantity;

        document.getElementById("leverage").value =
            data.leverage;

        document.getElementById("trade").textContent =
            pretty(data.trade);

        document.getElementById("signal").textContent =
            pretty(data.last_signal);

        document.getElementById("ip").textContent =
            data.last_ip || "Not checked";

        const events =
            document.getElementById("events");

        events.innerHTML = "";

        (data.events || []).forEach(
            function(event) {
                const div =
                    document.createElement("div");

                div.className = "event";

                const time =
                    document.createElement("div");

                time.className = "small";
                time.textContent =
                    event.time || "";

                const message =
                    document.createElement("div");

                message.textContent =
                    event.message || "";

                div.appendChild(time);
                div.appendChild(message);

                events.appendChild(div);
            }
        );

    } catch (e) {
        console.error(e);
    }
}


refreshIP();
refresh();

setInterval(refresh, 3000);

</script>

</body>
</html>
"""


if __name__ == "__main__":
    start_engine()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv("PORT", "5000")
        ),
        debug=False,
        threaded=True,
    )
