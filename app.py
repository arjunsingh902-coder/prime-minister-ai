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
CMC_API_KEY = os.getenv("CMC_API_KEY", "").strip()

USER_AGENT = "PrimeMinisterAI/1.0"

SCAN_SECONDS = 5
CMC_CACHE_SECONDS = 30 * 60
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
cmc_cache = {
    "timestamp": 0,
    "data": {},
    "error": None,
}

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
        state["events"].insert(
            0,
            {
                "time": timestamp,
                "message": message,
            },
        )

        state["events"] = state["events"][:100]

    save_state()


# ============================================================
# DELTA SIGNING
# ============================================================

def delta_signature(method, timestamp, path, query_string="", body=""):
    message = (
        method.upper()
        + str(timestamp)
        + path
        + query_string
        + body
    )

    return hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def delta_request(
    method,
    path,
    params=None,
    body=None,
    authenticated=False,
    timeout=10,
):
    url = DELTA_BASE + path

    params = params or {}

    body_text = ""
    if body is not None:
        body_text = json.dumps(
            body,
            separators=(",", ":"),
        )

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    if body is not None:
        headers["Content-Type"] = "application/json"

    if authenticated:
        if not API_KEY or not API_SECRET:
            raise RuntimeError(
                "DELTA_API_KEY / DELTA_API_SECRET not configured"
            )

        timestamp = str(int(time.time()))

        query_string = ""

        if params:
            parts = []
            for key in sorted(params.keys()):
                parts.append(
                    f"{key}={params[key]}"
                )
            query_string = "&".join(parts)

        signature = delta_signature(
            method,
            timestamp,
            path,
            query_string,
            body_text,
        )

        headers.update(
            {
                "api-key": API_KEY,
                "timestamp": timestamp,
                "signature": signature,
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

    try:
        data = response.json()
    except Exception:
        data = {
            "success": False,
            "error": response.text,
        }

    if response.status_code >= 400:
        raise RuntimeError(
            f"Delta HTTP {response.status_code}: "
            f"{data}"
        )

    return data


# ============================================================
# DELTA PRODUCTS
# ============================================================

def get_products():
    now = time.time()

    if (
        product_cache.get("all")
        and now - product_cache["all"]["time"]
        < PRODUCT_CACHE_SECONDS
    ):
        return product_cache["all"]["data"]

    data = delta_request(
        "GET",
        "/v2/products",
        authenticated=False,
    )

    products = data.get("result", [])

    product_cache["all"] = {
        "time": now,
        "data": products,
    }

    return products


def find_product(symbol):
    symbol = symbol.upper()

    try:
        products = get_products()

        for product in products:
            if str(product.get("symbol", "")).upper() == symbol:
                return product

    except Exception as exc:
        event(f"{symbol}: product lookup failed: {exc}")

    return None


def extract_number(product, names, default=None):
    for name in names:
        value = product.get(name)

        if value is None:
            continue

        try:
            return float(value)
        except Exception:
            pass

    return default


def product_rules(symbol):
    product = find_product(symbol)

    if not product:
        return {
            "symbol": symbol,
            "available": False,
            "message": "Delta product information unavailable",
        }

    min_qty = extract_number(
        product,
        [
            "min_order_size",
            "minimum_order_size",
            "order_min_size",
            "min_size",
        ],
        None,
    )

    max_qty = extract_number(
        product,
        [
            "max_order_size",
            "maximum_order_size",
            "order_max_size",
            "max_size",
        ],
        None,
    )

    step = extract_number(
        product,
        [
            "order_size_increment",
            "size_increment",
            "step_size",
        ],
        None,
    )

    min_leverage = extract_number(
        product,
        [
            "min_leverage",
            "minimum_leverage",
        ],
        1,
    )

    max_leverage = extract_number(
        product,
        [
            "max_leverage",
            "maximum_leverage",
            "leverage",
        ],
        None,
    )

    default_leverage = extract_number(
        product,
        [
            "default_leverage",
        ],
        None,
    )

    return {
        "available": True,
        "id": product.get("id"),
        "symbol": product.get("symbol"),
        "description": product.get("description"),
        "contract_value": product.get("contract_value"),
        "tick_size": product.get("tick_size"),
        "trading_status": product.get("trading_status"),
        "min_quantity": min_qty,
        "max_quantity": max_qty,
        "quantity_step": step,
        "min_leverage": min_leverage,
        "max_leverage": max_leverage,
        "default_leverage": default_leverage,
        "max_leverage_notional": product.get(
            "max_leverage_notional"
        ),
        "raw": product,
    }


# ============================================================
# LEVERAGE
# ============================================================

def set_delta_leverage(symbol, leverage):
    product = find_product(symbol)

    if not product:
        raise RuntimeError(
            f"{symbol}: Delta product not found"
        )

    product_id = product.get("id")

    if product_id is None:
        raise RuntimeError(
            f"{symbol}: Delta product ID missing"
        )

    body = {
        "leverage": int(leverage),
    }

    return delta_request(
        "POST",
        f"/v2/products/{product_id}/orders/leverage",
        body=body,
        authenticated=True,
    )


def get_delta_leverage(symbol):
    product = find_product(symbol)

    if not product:
        return None

    product_id = product.get("id")

    if product_id is None:
        return None

    try:
        data = delta_request(
            "GET",
            f"/v2/products/{product_id}/orders/leverage",
            authenticated=True,
        )

        result = data.get("result", {})

        if isinstance(result, dict):
            value = result.get("leverage")

            if value is not None:
                return float(value)

    except Exception:
        pass

    return None


# ============================================================
# DELTA CANDLES
# ============================================================

def get_candles(symbol, resolution=RESOLUTION):
    data = delta_request(
        "GET",
        "/v2/history/candles",
        params={
            "symbol": symbol,
            "resolution": resolution,
            "limit": CANDLE_LIMIT,
        },
        authenticated=False,
    )

    result = data.get("result", [])

    candles = []

    for item in result:
        try:
            if isinstance(item, dict):
                ts = item.get("time")
                open_price = item.get("open")
                high = item.get("high")
                low = item.get("low")
                close = item.get("close")
                volume = item.get("volume", 0)
            else:
                ts = item[0]
                open_price = item[1]
                high = item[2]
                low = item[3]
                close = item[4]
                volume = item[5] if len(item) > 5 else 0

            candles.append(
                {
                    "time": float(ts),
                    "open": float(open_price),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": float(volume or 0),
                }
            )

        except Exception:
            continue

    candles.sort(key=lambda x: x["time"])

    return candles


# ============================================================
# DELTA POSITION
# ============================================================

def get_positions():
    data = delta_request(
        "GET",
        "/v2/positions",
        authenticated=True,
    )

    return data.get("result", [])


def get_position_for_symbol(symbol):
    try:
        positions = get_positions()

        for position in positions:
            psymbol = str(
                position.get("product_symbol")
                or position.get("symbol")
                or ""
            ).upper()

            if psymbol == symbol.upper():
                size = float(
                    position.get("size", 0) or 0
                )

                if abs(size) > 0:
                    return position

    except Exception as exc:
        event(
            f"{symbol}: position check failed: {exc}"
        )

    return None


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    current = sum(values[:period]) / period

    for price in values[period:]:
        current = (
            (price - current) * multiplier
            + current
        )

    return current


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, period + 1):
        change = values[i] - values[i - 1]

        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]

        gain = max(change, 0)
        loss = max(-change, 0)

        avg_gain = (
            ((avg_gain * (period - 1)) + gain)
            / period
        )

        avg_loss = (
            ((avg_loss * (period - 1)) + loss)
            / period
        )

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(
                current["high"]
                - previous["close"]
            ),
            abs(
                current["low"]
                - previous["close"]
            ),
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    current_atr = (
        sum(true_ranges[:period]) / period
    )

    for tr in true_ranges[period:]:
        current_atr = (
            (
                current_atr * (period - 1)
                + tr
            )
            / period
        )

    return current_atr


# ============================================================
# STRATEGY
# ============================================================

def calculate_signal(candles):
    if len(candles) < 60:
        return {
            "side": "NO_TRADE",
            "score": 0,
            "price": candles[-1]["close"]
            if candles
            else None,
            "reason": "Not enough candles",
            "ema9": None,
            "ema21": None,
            "rsi": None,
            "atr": None,
        }

    closes = [
        x["close"]
        for x in candles
    ]

    volumes = [
        x["volume"]
        for x in candles
    ]

    price = closes[-1]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi_value = rsi(closes, 14)
    atr_value = atr(candles, 14)

    if (
        ema9 is None
        or ema21 is None
        or rsi_value is None
        or atr_value is None
    ):
        return {
            "side": "NO_TRADE",
            "score": 0,
            "price": price,
            "reason": "Indicator calculation unavailable",
        }

    long_score = 0
    short_score = 0

    reasons_long = []
    reasons_short = []

    # --------------------------------------------------------
    # EMA TREND
    # --------------------------------------------------------

    if ema9 > ema21:
        long_score += 25
        reasons_long.append("EMA bullish")
    elif ema9 < ema21:
        short_score += 25
        reasons_short.append("EMA bearish")

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    momentum_window = closes[-4:]

    if momentum_window[-1] > momentum_window[0]:
        long_score += 10
        reasons_long.append("Momentum up")
    elif momentum_window[-1] < momentum_window[0]:
        short_score += 10
        reasons_short.append("Momentum down")

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if 52 <= rsi_value <= 68:
        long_score += 15
        reasons_long.append("RSI supports long")

    if 32 <= rsi_value <= 48:
        short_score += 15
        reasons_short.append("RSI supports short")

    # Avoid chasing extreme RSI
    if rsi_value > 75:
        long_score -= 10

    if rsi_value < 25:
        short_score -= 10

    # --------------------------------------------------------
    # BREAKOUT
    # --------------------------------------------------------

    previous_high = max(
        x["high"]
        for x in candles[-21:-1]
    )

    previous_low = min(
        x["low"]
        for x in candles[-21:-1]
    )

    if price > previous_high:
        long_score += 20
        reasons_long.append("20-candle breakout")

    if price < previous_low:
        short_score += 20
        reasons_short.append("20-candle breakdown")

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    recent_volume = (
        sum(volumes[-5:]) / 5
        if len(volumes) >= 5
        else 0
    )

    base_volume = (
        sum(volumes[-25:-5]) / 20
        if len(volumes) >= 25
        else 0
    )

    volume_ratio = (
        recent_volume / base_volume
        if base_volume > 0
        else 0
    )

    if volume_ratio >= 1.15:
        if long_score > short_score:
            long_score += 15
            reasons_long.append("Volume confirmation")
        elif short_score > long_score:
            short_score += 15
            reasons_short.append("Volume confirmation")

    # --------------------------------------------------------
    # FINAL DECISION
    # --------------------------------------------------------

    if (
        long_score >= 75
        and long_score >= short_score + 10
    ):
        side = "LONG"
        score = min(long_score, 100)
        reason = ", ".join(reasons_long)

    elif (
        short_score >= 75
        and short_score >= long_score + 10
    ):
        side = "SHORT"
        score = min(short_score, 100)
        reason = ", ".join(reasons_short)

    else:
        side = "NO_TRADE"
        score = max(
            min(long_score, 100),
            min(short_score, 100),
        )

        if long_score > short_score:
            reason = (
                f"Long setup incomplete "
                f"({long_score}/75)"
            )
        elif short_score > long_score:
            reason = (
                f"Short setup incomplete "
                f"({short_score}/75)"
            )
        else:
            reason = "No directional edge"

    return {
        "side": side,
        "score": score,
        "price": price,
        "reason": reason,
        "long_score": long_score,
        "short_score": short_score,
        "ema9": ema9,
        "ema21": ema21,
        "rsi": rsi_value,
        "atr": atr_value,
        "volume_ratio": volume_ratio,
        "updated_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }


# ============================================================
# CMC
# ============================================================

def refresh_cmc_if_needed(force=False):
    global cmc_cache

    if not CMC_API_KEY:
        return {
            "connected": False,
            "cached": False,
            "data": {},
            "error": "CMC_API_KEY not configured",
        }

    now = time.time()

    if (
        not force
        and cmc_cache["timestamp"]
        and now - cmc_cache["timestamp"]
        < CMC_CACHE_SECONDS
    ):
        return {
            "connected": True,
            "cached": True,
            "data": cmc_cache["data"],
            "error": cmc_cache["error"],
        }

    try:
        response = requests.get(
            "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
            params={
                "symbol": ",".join(
                    x.replace("USD", "")
                    for x in SYMBOLS
                ),
                "convert": "USD",
            },
            headers={
                "X-CMC_PRO_API_KEY": CMC_API_KEY,
                "Accepts": "application/json",
            },
            timeout=10,
        )

        response.raise_for_status()

        payload = response.json()
        data = payload.get("data", {})

        cmc_cache = {
            "timestamp": now,
            "data": data,
            "error": None,
        }

        return {
            "connected": True,
            "cached": False,
            "data": data,
            "error": None,
        }

    except Exception as exc:
        cmc_cache["error"] = str(exc)

        return {
            "connected": False,
            "cached": False,
            "data": cmc_cache["data"],
            "error": str(exc),
        }


# ============================================================
# PUBLIC IP
# ============================================================

def get_public_ip():
    try:
        response = requests.get(
            "https://api.ipify.org?format=json",
            timeout=5,
        )

        data = response.json()

        runtime["public_ip"] = data.get("ip")

        return runtime["public_ip"]

    except Exception:
        return None


# ============================================================
# QUANTITY / LEVERAGE VALIDATION
# ============================================================

def validate_coin_settings(symbol, quantity, leverage):
    rules = product_rules(symbol)

    if not rules.get("available"):
        return False, rules.get(
            "message",
            "Delta product unavailable",
        )

    try:
        quantity = float(quantity)
        leverage = float(leverage)
    except Exception:
        return False, "Quantity and leverage must be numeric"

    if quantity <= 0:
        return False, "Quantity must be greater than zero"

    if leverage <= 0:
        return False, "Leverage must be greater than zero"

    min_qty = rules.get("min_quantity")
    max_qty = rules.get("max_quantity")
    step = rules.get("quantity_step")

    if min_qty is not None and quantity < min_qty:
        return False, (
            f"Quantity below Delta minimum "
            f"{min_qty}"
        )

    if max_qty is not None and quantity > max_qty:
        return False, (
            f"Quantity above Delta maximum "
            f"{max_qty}"
        )

    if step is not None and step > 0:
        quotient = quantity / step

        if abs(
            quotient - round(quotient)
        ) > 1e-8:
            return False, (
                f"Quantity must follow Delta "
                f"step {step}"
            )

    min_lev = rules.get("min_leverage")
    max_lev = rules.get("max_leverage")

    if min_lev is not None and leverage < min_lev:
        return False, (
            f"Leverage below Delta minimum "
            f"{min_lev}x"
        )

    if max_lev is not None and leverage > max_lev:
        return False, (
            f"Leverage above Delta maximum "
            f"{max_lev}x"
        )

    return True, "OK"


# ============================================================
# PAPER TRADING
# ============================================================

def paper_open(symbol, side, signal):
    with lock:
        if state["current_trade"] is not None:
            return False

        coin = state["coins"][symbol]

        price = float(signal["price"])
        atr_value = float(signal["atr"])

        quantity = float(
            coin["quantity"]
        )

        leverage = float(
            coin["leverage"]
        )

        if side == "LONG":
            stop = price - (
                atr_value * 1.5
            )

            target = price + (
                atr_value * 3.0
            )

        else:
            stop = price + (
                atr_value * 1.5
            )

            target = price - (
                atr_value * 3.0
            )

        state["current_trade"] = {
            "symbol": symbol,
            "side": side,
            "entry": price,
            "quantity": quantity,
            "leverage": leverage,
            "stop": stop,
            "target": target,
            "mode": "PAPER",
            "opened_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "reason": signal["reason"],
        }

    save_state()

    event(
        f"PAPER {side} opened: "
        f"{symbol} @ {price}"
    )

    return True


def paper_manage():
    with lock:
        trade = state["current_trade"]

    if not trade:
        return

    symbol = trade["symbol"]

    try:
        candles = get_candles(symbol)

        if not candles:
            return

        price = candles[-1]["close"]

        side = trade["side"]

        exit_reason = None

        if side == "LONG":

            if price <= trade["stop"]:
                exit_reason = "Stop loss"

            elif price >= trade["target"]:
                exit_reason = "Target reached"

        elif side == "SHORT":

            if price >= trade["stop"]:
                exit_reason = "Stop loss"

            elif price <= trade["target"]:
                exit_reason = "Target reached"

        if exit_reason:
            close_trade(
                price,
                exit_reason,
            )

    except Exception as exc:
        event(
            f"Paper trade management error: {exc}"
        )


# ============================================================
# LIVE ORDER
# ============================================================

def place_live_market_order(
    symbol,
    side,
    quantity,
    leverage,
):
    valid, message = validate_coin_settings(
        symbol,
        quantity,
        leverage,
    )

    if not valid:
        raise RuntimeError(message)

    set_delta_leverage(
        symbol,
        leverage,
    )

    product = find_product(symbol)

    if not product:
        raise RuntimeError(
            f"{symbol}: product unavailable"
        )

    body = {
        "product_symbol": symbol,
        "size": quantity,
        "side": (
            "buy"
            if side == "LONG"
            else "sell"
        ),
        "order_type": "market_order",
    }

    return delta_request(
        "POST",
        "/v2/orders",
        body=body,
        authenticated=True,
    )


def live_open(symbol, side, signal):
    with lock:
        if state["current_trade"] is not None:
            return False

        coin = state["coins"][symbol]

        quantity = float(
            coin["quantity"]
        )

        leverage = float(
            coin["leverage"]
        )

    # --------------------------------------------------------
    # IMPORTANT:
    # Existing exchange position check prevents accidental
    # second position.
    # --------------------------------------------------------

    existing = get_position_for_symbol(
        symbol
    )

    if existing:
        event(
            f"LIVE entry blocked: existing "
            f"{symbol} position detected"
        )

        return False

    result = place_live_market_order(
        symbol,
        side,
        quantity,
        leverage,
    )

    price = float(signal["price"])

    with lock:
        state["current_trade"] = {
            "symbol": symbol,
            "side": side,
            "entry": price,
            "quantity": quantity,
            "leverage": leverage,
            "stop": None,
            "target": None,
            "mode": "LIVE",
            "exchange_order": result,
            "opened_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "reason": signal["reason"],
            "protective_order": False,
        }

    save_state()

    event(
        f"LIVE {side} opened: "
        f"{symbol} @ {price}"
    )

    event(
        "WARNING: exchange-side protective "
        "bracket must be configured before "
        "production LIVE deployment."
    )

    return True


# ============================================================
# CLOSE TRADE
# ============================================================

def close_live_trade(price, reason):
    with lock:
        trade = state["current_trade"]

    if not trade:
        return False

    symbol = trade["symbol"]

    position = get_position_for_symbol(
        symbol
    )

    if not position:
        with lock:
            state["current_trade"] = None

        save_state()

        event(
            f"LIVE position already closed: "
            f"{symbol}"
        )

        return True

    size = float(
        position.get("size", 0)
    )

    if size == 0:
        return False

    side = str(
        position.get("side", "")
    ).lower()

    close_side = (
        "sell"
        if side == "buy"
        else "buy"
    )

    body = {
        "product_symbol": symbol,
        "size": abs(size),
        "side": close_side,
        "order_type": "market_order",
        "reduce_only": True,
    }

    delta_request(
        "POST",
        "/v2/orders",
        body=body,
        authenticated=True,
    )

    with lock:
        state["current_trade"] = None

    save_state()

    event(
        f"LIVE position closed: "
        f"{symbol} @ {price} "
        f"({reason})"
    )

    return True


def close_trade(price, reason):
    with lock:
        trade = state["current_trade"]

    if not trade:
        return False

    if trade["mode"] == "LIVE":
        return close_live_trade(
            price,
            reason,
        )

    with lock:
        state["current_trade"] = None

    save_state()

    event(
        f"PAPER trade closed: "
        f"{trade['symbol']} @ {price} "
        f"({reason})"
    )

    return True


# ============================================================
# SIGNAL SCANNER
# ============================================================

def scan_all_coins():
    results = {}

    for symbol in SYMBOLS:
        try:
            candles = get_candles(symbol)

            signal = calculate_signal(
                candles
            )

            with lock:
                state["coins"][symbol][
                    "last_signal"
                ] = signal

            results[symbol] = signal

        except Exception as exc:
            signal = {
                "side": "NO_TRADE",
                "score": 0,
                "price": None,
                "reason": f"Data error: {exc}",
                "updated_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }

            with lock:
                state["coins"][symbol][
                    "last_signal"
                ] = signal

            results[symbol] = signal

    save_state()

    return results


# ============================================================
# STRONGEST SIGNAL
# ============================================================

def strongest_signal():
    candidates = []

    with lock:
        for symbol in SYMBOLS:
            signal = state["coins"][symbol][
                "last_signal"
            ]

            if signal.get("side") in (
                "LONG",
                "SHORT",
            ):
                candidates.append(
                    (
                        symbol,
                        signal,
                    )
                )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[1].get(
            "score",
            0,
        ),
        reverse=True,
    )

    symbol, signal = candidates[0]

    return {
        "symbol": symbol,
        **signal,
    }


# ============================================================
# TRADING ENGINE
# ============================================================

def trading_cycle():
    runtime["last_scan"] = datetime.now(
        timezone.utc
    ).isoformat()

    runtime["last_scan_error"] = None

    try:
        # ----------------------------------------------------
        # Always analyze all five coins.
        # ----------------------------------------------------

        signals = scan_all_coins()

        # ----------------------------------------------------
        # Manage existing trade first.
        # ----------------------------------------------------

        with lock:
            trade = state["current_trade"]

        if trade:
            if trade["mode"] == "PAPER":
                paper_manage()

            elif trade["mode"] == "LIVE":
                # No automatic LIVE exit without
                # exchange-side protection.
                #
                # If exchange says position is gone,
                # reconcile local state.
                position = get_position_for_symbol(
                    trade["symbol"]
                )

                if not position:
                    with lock:
                        state["current_trade"] = None

                    save_state()

                    event(
                        f"Reconciled closed LIVE "
                        f"position: {trade['symbol']}"
                    )

            return

        # ----------------------------------------------------
        # System OFF = absolutely no bot entry.
        # ----------------------------------------------------

        with lock:
            system_on = bool(
                state["system_on"]
            )

            selected = (
                state["selected_coin"]
            )

        if not system_on:
            return

        # ----------------------------------------------------
        # No selected coin = no trade.
        # ----------------------------------------------------

        if selected not in SYMBOLS:
            return

        signal = signals.get(
            selected
        )

        if not signal:
            return

        if signal.get("side") not in (
            "LONG",
            "SHORT",
        ):
            return

        # ----------------------------------------------------
        # Only the ON coin can trade.
        # ----------------------------------------------------

        with lock:
            coin_enabled = bool(
                state["coins"][selected][
                    "enabled"
                ]
            )

            mode = state["mode"]

            quantity = float(
                state["coins"][selected][
                    "quantity"
                ]
            )

            leverage = float(
                state["coins"][selected][
                    "leverage"
                ]
            )

        if not coin_enabled:
            return

        valid, message = validate_coin_settings(
            selected,
            quantity,
            leverage,
        )

        if not valid:
            event(
                f"{selected}: entry blocked - "
                f"{message}"
            )
            return

        # ----------------------------------------------------
        # Execute.
        # ----------------------------------------------------

        if mode == "PAPER":
            paper_open(
                selected,
                signal["side"],
                signal,
            )

        elif mode == "LIVE":
            live_open(
                selected,
                signal["side"],
                signal,
            )

    except Exception as exc:
        runtime["last_scan_error"] = str(exc)

        event(
            f"Trading engine error: {exc}"
        )


def engine_loop():
    event("Trading engine thread started")

    while not engine_stop.is_set():
        started = time.time()

        trading_cycle()

        elapsed = time.time() - started

        wait_time = max(
            0.5,
            SCAN_SECONDS - elapsed,
        )

        engine_stop.wait(wait_time)

    event("Trading engine thread stopped")


def start_engine():
    global engine_thread

    with lock:
        if state["system_on"] is False:
            return False, (
                "System is OFF"
            )

        if state["selected_coin"] not in SYMBOLS:
            return False, (
                "Select one coin before starting"
            )

        selected = state[
            "selected_coin"
        ]

        if not state["coins"][selected][
            "enabled"
        ]:
            return False, (
                f"{selected} is OFF"
            )

    if engine_thread and engine_thread.is_alive():
        return True, "Already running"

    engine_stop.clear()

    runtime["started_at"] = datetime.now(
        timezone.utc
    ).isoformat()

    engine_thread = threading.Thread(
        target=engine_loop,
        daemon=True,
        name="PrimeMinisterTradingEngine",
    )

    engine_thread.start()

    return True, "Engine started"


def stop_engine():
    engine_stop.set()

    return True, "Engine stopping"


# ============================================================
# STARTUP RECONCILIATION
# ============================================================

def reconcile_live_state():
    if not API_KEY or not API_SECRET:
        return

    try:
        positions = get_positions()

        live_positions = []

        for position in positions:
            try:
                size = float(
                    position.get("size", 0)
                    or 0
                )
            except Exception:
                size = 0

            if abs(size) <= 0:
                continue

            live_positions.append(
                position
            )

        with lock:
            local_trade = state[
                "current_trade"
            ]

        if local_trade:
            symbol = local_trade.get(
                "symbol"
            )

            found = any(
                str(
                    p.get("product_symbol")
                    or p.get("symbol")
                    or ""
                ).upper()
                == str(symbol).upper()
                for p in live_positions
            )

            if not found:
                with lock:
                    state[
                        "current_trade"
                    ] = None

                save_state()

                event(
                    f"Startup reconciliation: "
                    f"{symbol} position not found"
                )

        elif len(live_positions) == 1:
            # Do not invent entry information.
            # Block autonomous trading until user
            # explicitly reconciles this position.
            position = live_positions[0]

            symbol = (
                position.get(
                    "product_symbol"
                )
                or position.get("symbol")
            )

            with lock:
                state[
                    "current_trade"
                ] = {
                    "symbol": symbol,
                    "side": str(
                        position.get(
                            "side",
                            "unknown",
                        )
                    ).upper(),
                    "entry": position.get(
                        "entry_price"
                    ),
                    "quantity": abs(
                        float(
                            position.get(
                                "size",
                                0,
                            )
                            or 0
                        )
                    ),
                    "leverage": position.get(
                        "leverage"
                    ),
                    "mode": "LIVE",
                    "opened_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "reconciled": True,
                    "protective_order": False,
                }

            save_state()

            event(
                f"Startup found unmanaged LIVE "
                f"position on {symbol}; "
                f"new bot entries blocked"
            )

        elif len(live_positions) > 1:
            event(
                "CRITICAL: multiple LIVE positions "
                "detected. Autonomous entries blocked."
            )

    except Exception as exc:
        event(
            f"Startup reconciliation failed: {exc}"
        )


# ============================================================
# API — DASHBOARD STATE
# ============================================================

@app.get("/api/state")
def api_state():
    with lock:
        snapshot = json.loads(
            json.dumps(state)
        )

    cmc = refresh_cmc_if_needed(
        force=False
    )

    snapshot["runtime"] = runtime
    snapshot["strongest_signal"] = (
        strongest_signal()
    )

    snapshot["cmc"] = {
        "connected": cmc["connected"],
        "cached": cmc["cached"],
        "error": cmc["error"],
        "timestamp": cmc_cache["timestamp"],
    }

    snapshot["engine_running"] = bool(
        engine_thread
        and engine_thread.is_alive()
    )

    return jsonify(snapshot)


# ============================================================
# API — PRODUCT RULES
# ============================================================

@app.get("/api/coin/<symbol>/rules")
def api_coin_rules(symbol):
    symbol = symbol.upper()

    if symbol not in SYMBOLS:
        return jsonify(
            {
                "success": False,
                "error": "Unsupported coin",
            }
        ), 400

    rules = product_rules(symbol)

    return jsonify(
        {
            "success": True,
            "rules": rules,
        }
    )


# ============================================================
# API — COIN SETTINGS
# ============================================================

@app.post("/api/coin/<symbol>/settings")
def api_coin_settings(symbol):
    symbol = symbol.upper()

    if symbol not in SYMBOLS:
        return jsonify(
            {
                "success": False,
                "error": "Unsupported coin",
            }
        ), 400

    data = request.get_json(
        silent=True
    ) or {}

    try:
        quantity = float(
            data.get("quantity")
        )

        leverage = float(
            data.get("leverage")
        )

    except Exception:
        return jsonify(
            {
                "success": False,
                "error": (
                    "Quantity and leverage "
                    "are required"
                ),
            }
        ), 400

    valid, message = validate_coin_settings(
        symbol,
        quantity,
        leverage,
    )

    if not valid:
        return jsonify(
            {
                "success": False,
                "error": message,
            }
        ), 400

    with lock:
        state["coins"][symbol][
            "quantity"
        ] = quantity

        state["coins"][symbol][
            "leverage"
        ] = leverage

        state["coins"][symbol][
            "rules"
        ] = product_rules(symbol)

    save_state()

    event(
        f"{symbol} settings updated: "
        f"qty={quantity}, lev={leverage}x"
    )

    return jsonify(
        {
            "success": True,
            "symbol": symbol,
            "quantity": quantity,
            "leverage": leverage,
        }
    )


# ============================================================
# API — COIN ON/OFF
# ============================================================

@app.post("/api/coin/<symbol>/toggle")
def api_coin_toggle(symbol):
    symbol = symbol.upper()

    if symbol not in SYMBOLS:
        return jsonify(
            {
                "success": False,
                "error": "Unsupported coin",
            }
        ), 400

    data = request.get_json(
        silent=True
    ) or {}

    enabled = bool(
        data.get("enabled")
    )

    with lock:
        if enabled:
            # Exactly one coin ON.
            for other in SYMBOLS:
                state["coins"][other][
                    "enabled"
                ] = (
                    other == symbol
                )

            state["selected_coin"] = symbol

        else:
            state["coins"][symbol][
                "enabled"
            ] = False

            if (
                state["selected_coin"]
                == symbol
            ):
                state["selected_coin"] = None

    save_state()

    event(
        f"{symbol} turned "
        f"{'ON' if enabled else 'OFF'}"
    )

    return jsonify(
        {
            "success": True,
            "selected_coin": state[
                "selected_coin"
            ],
        }
    )


# ============================================================
# API — SYSTEM
# ============================================================

@app.post("/api/system")
def api_system():
    data = request.get_json(
        silent=True
    ) or {}

    enabled = bool(
        data.get("enabled")
    )

    with lock:
        state["system_on"] = enabled

    if enabled:
        ok, message = start_engine()

        if not ok:
            with lock:
                state["system_on"] = False

            save_state()

            return jsonify(
                {
                    "success": False,
                    "error": message,
                }
            ), 400

        event("SYSTEM ON")

    else:
        stop_engine()

        event(
            "SYSTEM OFF — no new "
            "bot-initiated trades"
        )

    save_state()

    return jsonify(
        {
            "success": True,
            "system_on": enabled,
        }
    )


# ============================================================
# API — MODE
# ============================================================

@app.post("/api/mode")
def api_mode():
    data = request.get_json(
        silent=True
    ) or {}

    mode = str(
        data.get("mode", "")
    ).upper()

    if mode not in (
        "PAPER",
        "LIVE",
    ):
        return jsonify(
            {
                "success": False,
                "error": "Mode must be PAPER or LIVE",
            }
        ), 400

    with lock:
        if state["current_trade"] is not None:
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "Cannot change mode "
                        "while a trade is open"
                    ),
                }
            ), 400

        state["mode"] = mode

    save_state()

    event(
        f"Mode changed to {mode}"
    )

    return jsonify(
        {
            "success": True,
            "mode": mode,
        }
    )


# ============================================================
# API — MANUAL CLOSE
# ============================================================

@app.post("/api/trade/close")
def api_close_trade():
    with lock:
        trade = state["current_trade"]

    if not trade:
        return jsonify(
            {
                "success": False,
                "error": "No open trade",
            }
        ), 400

    symbol = trade["symbol"]

    try:
        candles = get_candles(symbol)

        price = (
            candles[-1]["close"]
            if candles
            else trade.get("entry")
        )

        close_trade(
            price,
            "Manual close",
        )

        return jsonify(
            {
                "success": True,
            }
        )

    except Exception as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 500


# ============================================================
# API — CMC STATUS
# ============================================================

@app.get("/api/cmc")
def api_cmc():
    data = refresh_cmc_if_needed(
        force=False
    )

    return jsonify(
        {
            "success": True,
            "connected": data["connected"],
            "cached": data["cached"],
            "error": data["error"],
            "timestamp": cmc_cache[
                "timestamp"
            ],
            "data": data["data"],
        }
    )


# ============================================================
# API — PUBLIC IP
# ============================================================

@app.get("/api/ip")
def api_ip():
    ip = runtime.get(
        "public_ip"
    )

    if not ip:
        ip = get_public_ip()

    return jsonify(
        {
            "success": bool(ip),
            "ip": ip,
        }
    )


# ============================================================
# DASHBOARD
# ============================================================

HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
/>

<title>Prime Minister AI</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #07111f;
    color: #e8eef7;
    font-family:
        Inter,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

header {
    padding: 18px;
    border-bottom: 1px solid #1b2a3e;
    background: #091625;
}

h1 {
    margin: 0;
    font-size: 23px;
}

.subtitle {
    margin-top: 5px;
    color: #8294aa;
    font-size: 13px;
}

.container {
    max-width: 1200px;
    margin: auto;
    padding: 16px;
}

.topbar {
    display: grid;
    grid-template-columns:
        repeat(auto-fit,minmax(180px,1fr));
    gap: 10px;
    margin-bottom: 15px;
}

.panel {
    background: #0b1828;
    border: 1px solid #1b2a3e;
    border-radius: 14px;
    padding: 14px;
}

.label {
    color: #7e91a8;
    font-size: 12px;
}

.value {
    font-size: 20px;
    font-weight: 700;
    margin-top: 4px;
}

.green {
    color: #45e09b;
}

.red {
    color: #ff6577;
}

.yellow {
    color: #ffc857;
}

.gray {
    color: #91a0b5;
}

.controls {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

button,
select,
input {
    background: #101f32;
    color: white;
    border: 1px solid #2a405a;
    border-radius: 9px;
    padding: 10px 12px;
    font-size: 14px;
}

button {
    cursor: pointer;
}

button:hover {
    background: #172b43;
}

.coin-grid {
    display: grid;
    grid-template-columns:
        repeat(auto-fit,minmax(210px,1fr));
    gap: 12px;
}

.coin {
    cursor: pointer;
    transition: .15s;
}

.coin:hover {
    transform: translateY(-2px);
    border-color: #52759d;
}

.coin.on {
    border-color: #45e09b;
}

.coin-title {
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.coin-name {
    font-size: 17px;
    font-weight: 700;
}

.badge {
    font-size: 11px;
    padding: 4px 8px;
    border-radius: 999px;
    background: #16263a;
}

.signal {
    font-size: 21px;
    font-weight: 800;
    margin: 15px 0 4px;
}

.price {
    font-size: 15px;
}

.reason {
    margin-top: 10px;
    color: #8294aa;
    font-size: 12px;
    min-height: 32px;
}

.meta {
    margin-top: 12px;
    display: flex;
    justify-content: space-between;
    color: #9aacbf;
    font-size: 12px;
}

.section {
    margin-top: 16px;
}

.trade-box {
    display: grid;
    grid-template-columns:
        repeat(auto-fit,minmax(150px,1fr));
    gap: 10px;
}

.event-list {
    max-height: 250px;
    overflow: auto;
}

.event-item {
    padding: 8px 0;
    border-bottom: 1px solid #18273a;
    font-size: 12px;
    color: #a9b8ca;
}

.modal {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,.7);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 15px;
    z-index: 50;
}

.modal.show {
    display: flex;
}

.modal-box {
    width: 100%;
    max-width: 480px;
    max-height: 90vh;
    overflow: auto;
    background: #0b1828;
    border: 1px solid #2a405a;
    border-radius: 16px;
    padding: 18px;
}

.modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.form-row {
    margin-top: 12px;
}

.form-row label {
    display: block;
    color: #8ea0b4;
    font-size: 12px;
    margin-bottom: 5px;
}

.form-row input,
.form-row select {
    width: 100%;
}

.rule {
    display: flex;
    justify-content: space-between;
    padding: 7px 0;
    border-bottom: 1px solid #18273a;
    font-size: 12px;
}

.rule span:first-child {
    color: #8497ac;
}

.small {
    color: #71849a;
    font-size: 11px;
}

</style>
</head>

<body>

<header>
    <h1>Prime Minister AI</h1>
    <div class="subtitle">
        Autonomous Delta Trading Commander
    </div>
</header>

<div class="container">

    <div class="topbar">

        <div class="panel">
            <div class="label">SYSTEM</div>
            <div id="system" class="value gray">
                OFF
            </div>
        </div>

        <div class="panel">
            <div class="label">MODE</div>
            <div id="mode" class="value">
                PAPER
            </div>
        </div>

        <div class="panel">
            <div class="label">SELECTED COIN</div>
            <div id="selected" class="value">
                None
            </div>
        </div>

        <div class="panel">
            <div class="label">STRONGEST SIGNAL</div>
            <div id="strongest" class="value">
                None
            </div>
        </div>

    </div>

    <div class="panel">

        <div class="controls">

            <button onclick="systemToggle()">
                SYSTEM ON / OFF
            </button>

            <button onclick="setMode('PAPER')">
                PAPER
            </button>

            <button onclick="setMode('LIVE')">
                LIVE
            </button>

            <button onclick="manualClose()">
                CLOSE CURRENT TRADE
            </button>

        </div>

    </div>

    <div class="section">

        <div class="panel">

            <div class="label">
                TOP 5 MARKET WATCH
            </div>

            <div
                id="coins"
                class="coin-grid"
                style="margin-top:12px"
            ></div>

        </div>

    </div>

    <div class="section">

        <div class="panel">

            <div class="label">
                CURRENT TRADE
            </div>

            <div
                id="trade"
                style="margin-top:12px"
            >
                None
            </div>

        </div>

    </div>

    <div class="section">

        <div class="panel">

            <div class="label">
                CONNECTIONS
            </div>

            <div
                id="connections"
                style="margin-top:12px"
            ></div>

        </div>

    </div>

    <div class="section">

        <div class="panel">

            <div class="label">
                EVENTS
            </div>

            <div
                id="events"
                class="event-list"
                style="margin-top:12px"
            ></div>

        </div>

    </div>

</div>


<div id="modal" class="modal">

    <div class="modal-box">

        <div class="modal-header">

            <div>
                <div
                    id="modalTitle"
                    style="
                    font-size:20px;
                    font-weight:800;
                    "
                ></div>

                <div
                    id="modalSubtitle"
                    class="small"
                ></div>
            </div>

            <button onclick="closeModal()">
                X
            </button>

        </div>

        <div
            id="rules"
            style="margin-top:14px"
        ></div>

        <div class="form-row">

            <label>
                Quantity
            </label>

            <input
                id="quantity"
                type="number"
                step="any"
            />

        </div>

        <div class="form-row">

            <label>
                Leverage
            </label>

            <select id="leverage"></select>

        </div>

        <div
            class="controls"
            style="margin-top:16px"
        >

            <button onclick="saveSettings()">
                SAVE SETTINGS
            </button>

            <button onclick="toggleSelectedCoin()">
                ON / OFF
            </button>

        </div>

    </div>

</div>


<script>

let appState = null;
let selectedModalCoin = null;
let selectedRules = null;


function money(value) {

    if (value === null ||
        value === undefined) {
        return "--";
    }

    const n = Number(value);

    if (!Number.isFinite(n)) {
        return "--";
    }

    if (n >= 1000) {
        return n.toLocaleString(
            undefined,
            {
                maximumFractionDigits: 2
            }
        );
    }

    return n.toLocaleString(
        undefined,
        {
            maximumFractionDigits: 8
        }
    );
}


function signalClass(side) {

    if (side === "LONG") {
        return "green";
    }

    if (side === "SHORT") {
        return "red";
    }

    return "gray";
}


function render(state) {

    appState = state;

    const system =
        document.getElementById("system");

    system.innerText =
        state.system_on ? "ON" : "OFF";

    system.className =
        "value " +
        (state.system_on
            ? "green"
            : "gray");

    document.getElementById(
        "mode"
    ).innerText = state.mode;

    document.getElementById(
        "selected"
    ).innerText =
        state.selected_coin || "None";

    const strongest =
        state.strongest_signal;

    document.getElementById(
        "strongest"
    ).innerHTML =
        strongest
        ? `
            <span class="${signalClass(
                strongest.side
            )}">
                ${strongest.symbol}
                ${strongest.side}
                ${strongest.score}
            </span>
          `
        : "None";


    const coins =
        document.getElementById("coins");

    coins.innerHTML = "";

    for (const symbol of [
        "BTCUSD",
        "ETHUSD",
        "SOLUSD",
        "XRPUSD",
        "DOGEUSD"
    ]) {

        const coin =
            state.coins[symbol];

        const signal =
            coin.last_signal || {};

        const div =
            document.createElement("div");

        div.className =
            "panel coin " +
            (coin.enabled ? "on" : "");

        div.onclick = () =>
            openCoin(symbol);

        div.innerHTML = `

            <div class="coin-title">

                <div class="coin-name">
                    ${symbol}
                </div>

                <div class="badge">
                    ${coin.enabled
                        ? "ON"
                        : "OFF"}
                </div>

            </div>

            <div
                class="signal
                ${signalClass(
                    signal.side
                )}"
            >
                ${signal.side || "NO_TRADE"}
            </div>

            <div class="price">
                ${money(signal.price)}
            </div>

            <div class="reason">
                ${signal.reason || ""}
            </div>

            <div class="meta">

                <span>
                    Score:
                    ${signal.score ?? 0}
                </span>

                <span>
                    Qty:
                    ${coin.quantity}
                </span>

                <span>
                    ${coin.leverage}x
                </span>

            </div>

        `;

        coins.appendChild(div);
    }


    renderTrade(state);
    renderConnections(state);
    renderEvents(state);
}


function renderTrade(state) {

    const box =
        document.getElementById("trade");

    const trade =
        state.current_trade;

    if (!trade) {
        box.innerHTML =
            `<span class="gray">
                No active trade
            </span>`;

        return;
    }

    box.innerHTML = `

        <div class="trade-box">

            <div>
                <div class="label">
                    SYMBOL
                </div>
                <div class="value">
                    ${trade.symbol}
                </div>
            </div>

            <div>
                <div class="label">
                    SIDE
                </div>
                <div
                    class="value
                    ${signalClass(
                        trade.side
                    )}"
                >
                    ${trade.side}
                </div>
            </div>

            <div>
                <div class="label">
                    ENTRY
                </div>
                <div class="value">
                    ${money(
                        trade.entry
                    )}
                </div>
            </div>

            <div>
                <div class="label">
                    QTY
                </div>
                <div class="value">
                    ${trade.quantity}
                </div>
            </div>

            <div>
                <div class="label">
                    LEVERAGE
                </div>
                <div class="value">
                    ${trade.leverage}x
                </div>
            </div>

            <div>
                <div class="label">
                    MODE
                </div>
                <div class="value">
                    ${trade.mode}
                </div>
            </div>

        </div>
    `;
}


function renderConnections(state) {

    const cmc =
        state.cmc || {};

    document.getElementById(
        "connections"
    ).innerHTML = `

        <div class="rule">

            <span>Delta API</span>

            <span class="${
                state.runtime &&
                state.runtime.last_scan_error
                    ? "red"
                    : "green"
            }">
                ${
                    state.runtime &&
                    state.runtime.last_scan_error
                        ? "ERROR"
                        : "READY"
                }
            </span>

        </div>

        <div class="rule">

            <span>CMC</span>

            <span class="${
                cmc.connected
                    ? "green"
                    : "yellow"
            }">
                ${
                    cmc.connected
                        ? (
                            cmc.cached
                                ? "CONNECTED / CACHED"
                                : "CONNECTED"
                        )
                        : "NOT CONNECTED"
                }
            </span>

        </div>

        <div class="rule">

            <span>Public IP</span>

            <span>
                ${
                    state.runtime &&
                    state.runtime.public_ip
                        ? state.runtime.public_ip
                        : "--"
                }
            </span>

        </div>

        <div class="rule">

            <span>Engine</span>

            <span class="${
                state.engine_running
                    ? "green"
                    : "gray"
            }">
                ${
                    state.engine_running
                        ? "RUNNING"
                        : "STOPPED"
                }
            </span>

        </div>

    `;
}


function renderEvents(state) {

    const box =
        document.getElementById("events");

    box.innerHTML = "";

    for (
        const item of
        (state.events || [])
    ) {

        const div =
            document.createElement("div");

        div.className =
            "event-item";

        div.innerText =
            `${item.time} — ${item.message}`;

        box.appendChild(div);
    }
}


async function loadState() {

    try {

        const response =
            await fetch(
                "/api/state",
                {
                    cache: "no-store"
                }
            );

        const data =
            await response.json();

        render(data);

    } catch (error) {

        console.error(error);
    }
}


async function systemToggle() {

    if (!appState) {
        return;
    }

    const enabled =
        !appState.system_on;

    const response =
        await fetch(
            "/api/system",
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body: JSON.stringify({
                    enabled
                })
            }
        );

    const data =
        await response.json();

    if (!data.success) {
        alert(data.error);
    }

    await loadState();
}


async function setMode(mode) {

    const response =
        await fetch(
            "/api/mode",
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body: JSON.stringify({
                    mode
                })
            }
        );

    const data =
        await response.json();

    if (!data.success) {
        alert(data.error);
    }

    await loadState();
}


async function manualClose() {

    const response =
        await fetch(
            "/api/trade/close",
            {
                method: "POST"
            }
        );

    const data =
        await response.json();

    if (!data.success) {
        alert(data.error);
    }

    await loadState();
}


async function openCoin(symbol) {

    selectedModalCoin = symbol;

    const coin =
        appState.coins[symbol];

    document.getElementById(
        "modalTitle"
    ).innerText = symbol;

    document.getElementById(
        "modalSubtitle"
    ).innerText =
        "Delta product rules";

    document.getElementById(
        "quantity"
    ).value =
        coin.quantity;

    await loadRules(symbol);

    document.getElementById(
        "modal"
    ).classList.add("show");
}


async function loadRules(symbol) {

    try {

        const response =
            await fetch(
                `/api/coin/${symbol}/rules`,
                {
                    cache: "no-store"
                }
            );

        const data =
            await response.json();

        if (!data.success) {
            alert(data.error);
            return;
        }

        selectedRules =
            data.rules;

        renderRules(
            data.rules
        );

    } catch (error) {

        alert(
            "Failed to load Delta rules"
        );
    }
}


function renderRules(rules) {

    const box =
        document.getElementById("rules");

    if (!rules.available) {

        box.innerHTML = `
            <div class="red">
                ${
                    rules.message ||
                    "Delta rules unavailable"
                }
            </div>
        `;

        return;
    }

    box.innerHTML = `

        <div class="rule">
            <span>Trading status</span>
            <span>
                ${rules.trading_status ?? "--"}
            </span>
        </div>

        <div class="rule">
            <span>Minimum quantity</span>
            <span>
                ${rules.min_quantity ?? "--"}
            </span>
        </div>

        <div class="rule">
            <span>Maximum quantity</span>
            <span>
                ${rules.max_quantity ?? "--"}
            </span>
        </div>

        <div class="rule">
            <span>Quantity step</span>
            <span>
                ${rules.quantity_step ?? "--"}
            </span>
        </div>

        <div class="rule">
            <span>Minimum leverage</span>
            <span>
                ${
                    rules.min_leverage ??
                    "--"
                }x
            </span>
        </div>

        <div class="rule">
            <span>Maximum leverage</span>
            <span>
                ${
                    rules.max_leverage ??
                    "--"
                }x
            </span>
        </div>

        <div class="rule">
            <span>Default leverage</span>
            <span>
                ${
                    rules.default_leverage ??
                    "--"
                }x
            </span>
        </div>

        <div class="rule">
            <span>Tick size</span>
            <span>
                ${rules.tick_size ?? "--"}
            </span>
        </div>

    `;


    const select =
        document.getElementById(
            "leverage"
        );

    select.innerHTML = "";

    const min =
        Number.isFinite(
            Number(rules.min_leverage)
        )
        ? Number(rules.min_leverage)
        : 1;

    const max =
        Number.isFinite(
            Number(rules.max_leverage)
        )
        ? Number(rules.max_leverage)
        : 100;

    const common = [
        1,
        2,
        3,
        5,
        10,
        15,
        20,
        25,
        30,
        40,
        50,
        75,
        100
    ];

    const values =
        common.filter(
            value =>
                value >= min &&
                value <= max
        );

    if (values.length === 0) {
        values.push(min);
    }

    for (
        const value of values
    ) {

        const option =
            document.createElement(
                "option"
            );

        option.value = value;
        option.innerText =
            `${value}x`;

        if (
            appState.coins[
                selectedModalCoin
            ].leverage == value
        ) {
            option.selected = true;
        }

        select.appendChild(option);
    }
}


function closeModal() {

    document.getElementById(
        "modal"
    ).classList.remove("show");
}


async function saveSettings() {

    if (!selectedModalCoin) {
        return;
    }

    const quantity =
        Number(
            document.getElementById(
                "quantity"
            ).value
        );

    const leverage =
        Number(
            document.getElementById(
                "leverage"
            ).value
        );

    const response =
        await fetch(
            `/api/coin/${selectedModalCoin}/settings`,
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body: JSON.stringify({
                    quantity,
                    leverage
                })
            }
        );

    const data =
        await response.json();

    if (!data.success) {
        alert(data.error);
        return;
    }

    closeModal();

    await loadState();
}


async function toggleSelectedCoin() {

    if (!selectedModalCoin) {
        return;
    }

    const current =
        appState.coins[
            selectedModalCoin
        ].enabled;

    const response =
        await fetch(
            `/api/coin/${selectedModalCoin}/toggle`,
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body: JSON.stringify({
                    enabled: !current
                })
            }
        );

    const data =
        await response.json();

    if (!data.success) {
        alert(data.error);
        return;
    }

    closeModal();

    await loadState();
}


loadState();

setInterval(
    loadState,
    5000
);

</script>

</body>
</html>
"""


# ============================================================
# WEB
# ============================================================

@app.get("/")
def index():
    return render_template_string(
        HTML
    )


# ============================================================
# STARTUP
# ============================================================

def startup():
    get_public_ip()

    if API_KEY and API_SECRET:
        reconcile_live_state()

    # CMC is intentionally cached and not called
    # every trading cycle.
    refresh_cmc_if_needed(
        force=False
    )


startup()


# ============================================================
# SERVER
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "5000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True,
    )
