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

USER_AGENT = "PrimeMinisterAI/1.1"
SCAN_INTERVAL = 5
PRODUCT_CACHE_SECONDS = 60

SYMBOLS = [
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "XRPUSD",
    "DOGEUSD",
]

DEFAULT_COIN_CONFIG = {
    symbol: {
        "enabled": False,
        "quantity": None,
        "leverage": None,
        "rules": {},
        "last_signal": None,
    }
    for symbol in SYMBOLS
}

DEFAULT_STATE = {
    "enabled": False,
    "mode": "PAPER",
    "timeframe": "5m",
    "symbols": SYMBOLS,
    "adaptive_target": True,
    "auto_trailing": True,
    "coins": DEFAULT_COIN_CONFIG,
    "trade": None,
    "last_signal": None,
    "events": [],
    "last_ip": None,
}


state_lock = threading.RLock()
engine_thread = None

product_cache = {}
product_cache_time = 0
product_cache_lock = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def deep_copy(value):
    return json.loads(json.dumps(value))


def build_default_state():
    return deep_copy(DEFAULT_STATE)


def load_state():
    if not os.path.exists(STATE_FILE):
        return build_default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)

        state = build_default_state()

        if isinstance(saved, dict):
            state.update(saved)

        if not isinstance(state.get("coins"), dict):
            state["coins"] = deep_copy(DEFAULT_COIN_CONFIG)

        for symbol in SYMBOLS:
            if symbol not in state["coins"]:
                state["coins"][symbol] = deep_copy(
                    DEFAULT_COIN_CONFIG[symbol]
                )

        state["symbols"] = SYMBOLS

        return state

    except Exception:
        return build_default_state()


state = load_state()


def save_state():
    with state_lock:
        temporary = STATE_FILE + ".tmp"

        with open(
            temporary,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                state,
                f,
                indent=2,
            )

        os.replace(
            temporary,
            STATE_FILE,
        )


def add_event(message):
    event = {
        "time": utc_now(),
        "message": str(message),
    }

    with state_lock:
        state.setdefault("events", [])
        state["events"].insert(0, event)
        state["events"] = state["events"][:150]

    save_state()


def public_ip():
    try:
        response = requests.get(
            "https://api.ipify.org",
            timeout=5,
            headers={
                "User-Agent": USER_AGENT,
            },
        )

        response.raise_for_status()

        return response.text.strip()

    except Exception:
        return None


def delta_signature(
    method,
    path,
    timestamp,
    body="",
):
    payload = (
        f"{method.upper()}"
        f"{timestamp}"
        f"{path}"
        f"{body}"
    )

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
    timeout=10,
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
            raise RuntimeError(
                "Delta API credentials are not configured."
            )

        timestamp = str(int(time.time()))

        headers.update(
            {
                "api-key": API_KEY,
                "timestamp": timestamp,
                "signature": delta_signature(
                    method,
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
        data=(
            body_text
            if body is not None
            else None
        ),
        headers=headers,
        timeout=timeout,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Delta API {response.status_code}: "
            f"{response.text[:700]}"
        )

    try:
        return response.json()
    except Exception:
        return {}


def get_products(force=False):
    global product_cache
    global product_cache_time

    now = time.time()

    with product_cache_lock:
        if (
            not force
            and product_cache
            and now - product_cache_time
            < PRODUCT_CACHE_SECONDS
        ):
            return product_cache

    data = delta_request(
        "GET",
        "/v2/products",
        authenticated=False,
    )

    result = data.get("result", [])

    products = {}

    if isinstance(result, list):
        for product in result:
            if not isinstance(product, dict):
                continue

            symbol = str(
                product.get("symbol", "")
            ).upper()

            if symbol:
                products[symbol] = product

    with product_cache_lock:
        product_cache = products
        product_cache_time = now

    return products


def find_product(symbol):
    products = get_products()

    return products.get(
        str(symbol).upper()
    )


def first_number(data, keys):
    for key in keys:
        if key not in data:
            continue

        value = data.get(key)

        if value is None:
            continue

        try:
            return float(value)
        except Exception:
            continue

    return None


def first_value(data, keys):
    for key in keys:
        if key in data and data.get(key) is not None:
            return data.get(key)

    return None


def normalise_product_rules(product):
    if not product:
        return {
            "available": False,
            "message": "Product not found on Delta.",
        }

    minimum_quantity = first_number(
        product,
        [
            "min_order_size",
            "minimum_order_size",
            "min_size",
            "minimum_size",
            "order_min_size",
        ],
    )

    maximum_quantity = first_number(
        product,
        [
            "max_order_size",
            "maximum_order_size",
            "max_size",
            "maximum_size",
            "order_max_size",
        ],
    )

    quantity_step = first_number(
        product,
        [
            "order_size_increment",
            "size_increment",
            "lot_size",
            "quantity_step",
            "size_step",
        ],
    )

    minimum_leverage = first_number(
        product,
        [
            "min_leverage",
            "minimum_leverage",
        ],
    )

    maximum_leverage = first_number(
        product,
        [
            "max_leverage",
            "maximum_leverage",
            "leverage",
        ],
    )

    product_id = first_value(
        product,
        [
            "id",
            "product_id",
        ],
    )

    trading_status = first_value(
        product,
        [
            "state",
            "status",
            "trading_status",
        ],
    )

    return {
        "available": True,
        "product_id": product_id,
        "symbol": product.get("symbol"),
        "description": product.get(
            "description"
        ),
        "contract_type": product.get(
            "contract_type"
        ),
        "settling_asset": product.get(
            "settling_asset"
        ),
        "quoting_asset": product.get(
            "quoting_asset"
        ),
        "underlying_asset": product.get(
            "underlying_asset"
        ),
        "contract_value": product.get(
            "contract_value"
        ),
        "minimum_quantity": minimum_quantity,
        "maximum_quantity": maximum_quantity,
        "quantity_step": quantity_step,
        "minimum_leverage": minimum_leverage,
        "maximum_leverage": maximum_leverage,
        "trading_status": trading_status,
        "raw": product,
    }


def get_all_coin_rules(force=False):
    products = get_products(force=force)

    result = {}

    for symbol in SYMBOLS:
        product = products.get(symbol)

        result[symbol] = normalise_product_rules(
            product
        )

    return result


def validate_coin_settings(
    symbol,
    quantity,
    leverage,
):
    product = find_product(symbol)

    if not product:
        return False, (
            f"{symbol}: Delta product not found."
        )

    rules = normalise_product_rules(
        product
    )

    try:
        quantity = float(quantity)
    except Exception:
        return False, (
            f"{symbol}: invalid quantity."
        )

    try:
        leverage = float(leverage)
    except Exception:
        return False, (
            f"{symbol}: invalid leverage."
        )

    minimum_quantity = rules.get(
        "minimum_quantity"
    )

    maximum_quantity = rules.get(
        "maximum_quantity"
    )

    quantity_step = rules.get(
        "quantity_step"
    )

    minimum_leverage = rules.get(
        "minimum_leverage"
    )

    maximum_leverage = rules.get(
        "maximum_leverage"
    )

    if quantity <= 0:
        return False, (
            f"{symbol}: quantity must be greater than 0."
        )

    if leverage <= 0:
        return False, (
            f"{symbol}: leverage must be greater than 0."
        )

    if (
        minimum_quantity is not None
        and quantity < minimum_quantity
    ):
        return False, (
            f"{symbol}: quantity {quantity} "
            f"is below Delta minimum "
            f"{minimum_quantity}."
        )

    if (
        maximum_quantity is not None
        and quantity > maximum_quantity
    ):
        return False, (
            f"{symbol}: quantity {quantity} "
            f"exceeds Delta maximum "
            f"{maximum_quantity}."
        )

    if quantity_step:
        minimum_base = (
            minimum_quantity
            if minimum_quantity is not None
            else 0
        )

        steps = (
            quantity - minimum_base
        ) / quantity_step

        if abs(
            steps - round(steps)
        ) > 1e-8:
            return False, (
                f"{symbol}: quantity must "
                f"follow Delta quantity step "
                f"{quantity_step}."
            )

    if (
        minimum_leverage is not None
        and leverage < minimum_leverage
    ):
        return False, (
            f"{symbol}: leverage {leverage} "
            f"is below Delta minimum "
            f"{minimum_leverage}."
        )

    if (
        maximum_leverage is not None
        and leverage > maximum_leverage
    ):
        return False, (
            f"{symbol}: leverage {leverage} "
            f"exceeds Delta maximum "
            f"{maximum_leverage}."
        )

    return True, "OK"


def get_ticker(symbol):
    data = delta_request(
        "GET",
        "/v2/tickers",
        params={
            "symbol": symbol,
        },
        authenticated=False,
    )

    result = data.get("result", [])

    if isinstance(result, list):
        return (
            result[0]
            if result
            else {}
        )

    if isinstance(result, dict):
        return result

    return {}


def resolution_seconds(resolution):
    values = {
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "6h": 21600,
        "1d": 86400,
    }

    return values.get(
        resolution,
        300,
    )


def get_candles(
    symbol,
    resolution="5m",
    limit=150,
):
    seconds = resolution_seconds(
        resolution
    )

    end = int(time.time())

    start = (
        end
        - (
            limit
            * seconds
        )
    )

    data = delta_request(
        "GET",
        "/v2/history/candles",
        params={
            "symbol": symbol,
            "resolution": resolution,
            "start": start,
            "end": end,
        },
        authenticated=False,
    )

    candles = data.get(
        "result",
        [],
    )

    if not isinstance(candles, list):
        return []

    normalized = []

    for candle in candles:
        try:
            if isinstance(
                candle,
                dict,
            ):
                normalized.append(
                    {
                        "open": float(
                            candle["open"]
                        ),
                        "high": float(
                            candle["high"]
                        ),
                        "low": float(
                            candle["low"]
                        ),
                        "close": float(
                            candle["close"]
                        ),
                        "volume": float(
                            candle.get(
                                "volume",
                                0,
                            )
                        ),
                    }
                )

            elif (
                isinstance(
                    candle,
                    list,
                )
                and len(candle) >= 6
            ):
                normalized.append(
                    {
                        "open": float(
                            candle[1]
                        ),
                        "high": float(
                            candle[2]
                        ),
                        "low": float(
                            candle[3]
                        ),
                        "close": float(
                            candle[4]
                        ),
                        "volume": float(
                            candle[5]
                        ),
                    }
                )

        except Exception:
            continue

    return normalized


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (
        period + 1
    )

    result = (
        sum(values[:period])
        / period
    )

    for value in values[period:]:
        result = (
            (
                value
                - result
            )
            * multiplier
            + result
        )

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(
        1,
        len(values),
    ):
        change = (
            values[i]
            - values[i - 1]
        )

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    for i in range(
        period,
        len(gains),
    ):
        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + gains[i]
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = (
        avg_gain
        / avg_loss
    )

    return 100 - (
        100
        / (1 + rs)
    )


def atr(
    candles,
    period=14,
):
    if len(candles) <= period:
        return None

    true_ranges = []

    for i in range(
        1,
        len(candles),
    ):
        current = candles[i]
        previous = candles[i - 1]

        true_range = max(
            current["high"]
            - current["low"],
            abs(
                current["high"]
                - previous["close"]
            ),
            abs(
                current["low"]
                - previous["close"]
            ),
        )

        true_ranges.append(
            true_range
        )

    if len(true_ranges) < period:
        return None

    return (
        sum(
            true_ranges[-period:]
        )
        / period
    )


def analyze_market(symbol):
    candles = get_candles(
        symbol,
        state.get(
            "timeframe",
            "5m",
        ),
        150,
    )

    if len(candles) < 30:
        return None

    closes = [
        x["close"]
        for x in candles
    ]

    volumes = [
        x["volume"]
        for x in candles
    ]

    current = candles[-1]
    previous = candles[-2]

    ema9 = ema(
        closes,
        9,
    )

    ema21 = ema(
        closes,
        21,
    )

    rsi14 = rsi(
        closes,
        14,
    )

    atr14 = atr(
        candles,
        14,
    )

    if any(
        x is None
        for x in [
            ema9,
            ema21,
            rsi14,
            atr14,
        ]
    ):
        return None

    recent_volume = volumes[-1]

    volume_base = (
        sum(
            volumes[-21:-1]
        ) / 20
        if len(volumes) >= 21
        else max(
            sum(volumes)
            / len(volumes),
            1,
        )
    )

    volume_ratio = (
        recent_volume
        / volume_base
        if volume_base
        else 1
    )

    recent_high = max(
        x["high"]
        for x in candles[-21:-1]
    )

    recent_low = min(
        x["low"]
        for x in candles[-21:-1]
    )

    long_score = 0
    short_score = 0

    if ema9 > ema21:
        long_score += 25

    if ema9 < ema21:
        short_score += 25

    if current["close"] > previous["close"]:
        long_score += 10

    if current["close"] < previous["close"]:
        short_score += 10

    if rsi14 >= 52:
        long_score += 15

    if rsi14 <= 48:
        short_score += 15

    if current["close"] > recent_high:
        long_score += 20

    if current["close"] < recent_low:
        short_score += 20

    if volume_ratio >= 1.2:
        if current["close"] > previous["close"]:
            long_score += 15

        elif current["close"] < previous["close"]:
            short_score += 15

    if (
        long_score >= 75
        and long_score
        > short_score + 10
    ):
        direction = "LONG"
        score = long_score

    elif (
        short_score >= 75
        and short_score
        > long_score + 10
    ):
        direction = "SHORT"
        score = short_score

    else:
        direction = "NO_TRADE"
        score = max(
            long_score,
            short_score,
        )

    return {
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "long_score": long_score,
        "short_score": short_score,
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

    result = data.get(
        "result",
        [],
    )

    if isinstance(
        result,
        list,
    ):
        return result

    return []


def get_open_position(
    symbol=None,
):
    positions = get_positions()

    for position in positions:
        try:
            size = float(
                position.get(
                    "size",
                    0,
                )
                or 0
            )
        except Exception:
            size = 0

        if size == 0:
            continue

        if symbol:
            if (
                str(
                    position.get(
                        "symbol",
                        "",
                    )
                ).upper()
                != symbol.upper()
            ):
                continue

        return position

    return None


def has_any_live_position():
    try:
        return (
            get_open_position()
            is not None
        )

    except Exception:
        return False


def set_leverage(
    product_id,
    leverage,
):
    return delta_request(
        "POST",
        f"/v2/products/{product_id}/orders/leverage",
        body={
            "leverage": str(
                leverage
            ),
        },
        authenticated=True,
    )


def place_market_order(
    symbol,
    side,
    size,
):
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


def close_market_order(
    symbol,
    size,
    position_side,
):
    side = (
        "sell"
        if position_side == "LONG"
        else "buy"
    )

    return place_market_order(
        symbol,
        side,
        abs(size),
    )


def selected_coin():
    with state_lock:
        for symbol in SYMBOLS:
            coin = state["coins"].get(
                symbol,
                {},
            )

            if coin.get("enabled"):
                return symbol

    return None


def paper_enter(signal):
    symbol = signal["symbol"]

    with state_lock:
        config = state["coins"].get(
            symbol,
            {},
        )

        quantity = config.get(
            "quantity"
        )

        leverage = config.get(
            "leverage"
        )

    if quantity is None or leverage is None:
        add_event(
            f"PAPER ENTRY BLOCKED {symbol}: "
            "quantity/leverage not configured."
        )
        return

    with state_lock:
        state["trade"] = {
            "symbol": symbol,
            "side": signal["direction"],
            "entry": signal["price"],
            "size": quantity,
            "leverage": leverage,
            "highest": signal["price"],
            "lowest": signal["price"],
            "initial_atr": signal["atr"],
            "opened_at": utc_now(),
            "mode": "PAPER",
        }

    save_state()

    add_event(
        f"PAPER ENTRY "
        f"{signal['direction']} "
        f"{symbol} @ "
        f"{signal['price']}"
    )


def paper_manage(signal):
    with state_lock:
        trade = deep_copy(
            state.get("trade")
        )

    if not trade:
        return

    if (
        trade["symbol"]
        != signal["symbol"]
    ):
        return

    entry = float(
        trade["entry"]
    )

    atr_value = float(
        trade["initial_atr"]
    )

    price = float(
        signal["price"]
    )

    if trade["side"] == "LONG":
        trade["highest"] = max(
            float(
                trade.get(
                    "highest",
                    entry,
                )
            ),
            price,
        )

        peak = float(
            trade["highest"]
        )

        stop = max(
            entry
            - atr_value * 1.5,
            peak
            - atr_value * 1.8,
        )

        if price <= stop:
            pnl = (
                price
                - entry
            ) * trade["size"]

            with state_lock:
                state["trade"] = None

            save_state()

            add_event(
                f"PAPER EXIT LONG "
                f"{trade['symbol']} "
                f"@ {price} "
                f"PnL={pnl:.4f}"
            )

            return

        if (
            signal["direction"]
            == "SHORT"
            and signal["score"] >= 85
        ):
            pnl = (
                price
                - entry
            ) * trade["size"]

            with state_lock:
                state["trade"] = None

            save_state()

            add_event(
                f"PAPER REVERSAL EXIT LONG "
                f"{trade['symbol']} "
                f"@ {price} "
                f"PnL={pnl:.4f}"
            )

            return

    elif trade["side"] == "SHORT":
        trade["lowest"] = min(
            float(
                trade.get(
                    "lowest",
                    entry,
                )
            ),
            price,
        )

        low = float(
            trade["lowest"]
        )

        stop = min(
            entry
            + atr_value * 1.5,
            low
            + atr_value * 1.8,
        )

        if price >= stop:
            pnl = (
                entry
                - price
            ) * trade["size"]

            with state_lock:
                state["trade"] = None

            save_state()

            add_event(
                f"PAPER EXIT SHORT "
                f"{trade['symbol']} "
                f"@ {price} "
                f"PnL={pnl:.4f}"
            )

            return

        if (
            signal["direction"]
            == "LONG"
            and signal["score"] >= 85
        ):
            pnl = (
                entry
                - price
            ) * trade["size"]

            with state_lock:
                state["trade"] = None

            save_state()

            add_event(
                f"PAPER REVERSAL EXIT SHORT "
                f"{trade['symbol']} "
                f"@ {price} "
                f"PnL={pnl:.4f}"
            )

            return

    with state_lock:
        state["trade"] = trade

    save_state()


def live_enter(signal):
    symbol = signal["symbol"]

    if not API_KEY or not API_SECRET:
        add_event(
            "LIVE ENTRY BLOCKED: "
            "API credentials missing."
        )
        return

    with state_lock:
        config = deep_copy(
            state["coins"].get(
                symbol,
                {},
            )
        )

    quantity = config.get(
        "quantity"
    )

    leverage = config.get(
        "leverage"
    )

    if (
        quantity is None
        or leverage is None
    ):
        add_event(
            f"LIVE ENTRY BLOCKED {symbol}: "
            "quantity/leverage not configured."
        )
        return

    valid, message = (
        validate_coin_settings(
            symbol,
            quantity,
            leverage,
        )
    )

    if not valid:
        add_event(
            f"LIVE ENTRY BLOCKED: "
            f"{message}"
        )
        return

    product = find_product(
        symbol
    )

    if not product:
        add_event(
            f"LIVE ENTRY BLOCKED: "
            f"Delta product not found "
            f"{symbol}"
        )
        return

    product_id = product.get(
        "id"
    )

    if product_id is None:
        add_event(
            f"LIVE ENTRY BLOCKED: "
            f"{symbol} product ID missing."
        )
        return

    try:
        set_leverage(
            product_id,
            leverage,
        )

        side = (
            "buy"
            if signal["direction"]
            == "LONG"
            else "sell"
        )

        result = place_market_order(
            symbol,
            side,
            quantity,
        )

        order = result.get(
            "result",
            result,
        )

        with state_lock:
            state["trade"] = {
                "symbol": symbol,
                "side": signal["direction"],
                "entry": signal["price"],
                "size": quantity,
                "leverage": leverage,
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
            f"LIVE ENTRY "
            f"{signal['direction']} "
            f"{symbol} @ "
            f"{signal['price']}"
        )

        add_event(
            "LIVE protective bracket is "
            "not yet implemented."
        )

    except Exception as exc:
        add_event(
            f"LIVE ENTRY ERROR "
            f"{symbol}: {exc}"
        )


def live_manage(signal):
    with state_lock:
        trade = deep_copy(
            state.get("trade")
        )

    if not trade:
        return

    position = get_open_position(
        trade["symbol"]
    )

    if not position:
        with state_lock:
            state["trade"] = None

        save_state()

        add_event(
            f"Position closed externally: "
            f"{trade['symbol']}"
        )

        return

    size = float(
        position.get(
            "size",
            0,
        )
        or 0
    )

    entry = float(
        position.get(
            "entry_price",
            position.get(
                "entryPrice",
                trade["entry"],
            ),
        )
        or trade["entry"]
    )

    price = float(
        signal["price"]
    )

    trade["entry"] = entry

    if trade["side"] == "LONG":
        trade["highest"] = max(
            float(
                trade.get(
                    "highest",
                    entry,
                )
            ),
            price,
        )

        if (
            signal["direction"]
            == "SHORT"
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
                    f"{trade['symbol']} "
                    f"@ {price}"
                )

            except Exception as exc:
                add_event(
                    f"LIVE EXIT ERROR: "
                    f"{exc}"
                )

    elif trade["side"] == "SHORT":
        trade["lowest"] = min(
            float(
                trade.get(
                    "lowest",
                    entry,
                )
            ),
            price,
        )

        if (
            signal["direction"]
            == "LONG"
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
                    f"{trade['symbol']} "
                    f"@ {price}"
                )

            except Exception as exc:
                add_event(
                    f"LIVE EXIT ERROR: "
                    f"{exc}"
                )

    with state_lock:
        state["trade"] = trade

    save_state()


def trading_cycle():
    while True:
        try:
            with state_lock:
                enabled = bool(
                    state.get(
                        "enabled"
                    )
                )

                mode = state.get(
                    "mode",
                    "PAPER",
                )

                trade = deep_copy(
                    state.get("trade")
                )

            if not enabled:
                time.sleep(
                    SCAN_INTERVAL
                )
                continue

            if mode == "LIVE":
                try:
                    exchange_position = (
                        get_open_position()
                    )

                    if (
                        exchange_position
                        is None
                        and trade
                    ):
                        with state_lock:
                            state["trade"] = None

                        save_state()

                        add_event(
                            "Reconciliation: "
                            "exchange position absent; "
                            "local trade cleared."
                        )

                    elif (
                        exchange_position
                        and not trade
                    ):
                        add_event(
                            "SAFETY BLOCK: exchange "
                            "position exists without "
                            "local trade state."
                        )

                        time.sleep(
                            SCAN_INTERVAL
                        )
                        continue

                except Exception as exc:
                    add_event(
                        f"Reconciliation error: "
                        f"{exc}"
                    )

                    time.sleep(
                        SCAN_INTERVAL
                    )
                    continue

            if trade:
                signal = analyze_market(
                    trade["symbol"]
                )

                if signal:
                    with state_lock:
                        state["last_signal"] = signal

                        if (
                            signal["symbol"]
                            in state["coins"]
                        ):
                            state["coins"][
                                signal["symbol"]
                            ]["last_signal"] = (
                                signal
                            )

                    save_state()

                    if mode == "PAPER":
                        paper_manage(
                            signal
                        )

                    elif mode == "LIVE":
                        live_manage(
                            signal
                        )

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            selected = selected_coin()

            if not selected:
                time.sleep(
                    SCAN_INTERVAL
                )
                continue

            if mode == "LIVE":
                try:
                    if has_any_live_position():
                        add_event(
                            "ENTRY BLOCKED: "
                            "exchange position already exists."
                        )

                        time.sleep(
                            SCAN_INTERVAL
                        )
                        continue

                except Exception as exc:
                    add_event(
                        f"Position check failed: "
                        f"{exc}"
                    )

                    time.sleep(
                        SCAN_INTERVAL
                    )
                    continue

            signal = analyze_market(
                selected
            )

            if signal:
                with state_lock:
                    state["last_signal"] = signal

                    state["coins"][
                        selected
                    ]["last_signal"] = (
                        signal
                    )

                save_state()

                if (
                    signal["direction"]
                    != "NO_TRADE"
                ):
                    if mode == "PAPER":
                        paper_enter(
                            signal
                        )

                    elif mode == "LIVE":
                        live_enter(
                            signal
                        )

            time.sleep(
                SCAN_INTERVAL
            )

        except Exception as exc:
            add_event(
                f"ENGINE ERROR: {exc}"
            )

            time.sleep(
                SCAN_INTERVAL
            )


def start_engine():
    global engine_thread

    if (
        engine_thread
        and engine_thread.is_alive()
    ):
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
    )


@app.route("/api/status")
def api_status():
    with state_lock:
        snapshot = deep_copy(
            state
        )

    return jsonify(
        snapshot
    )


@app.route("/api/products")
def api_products():
    try:
        force = (
            request.args.get(
                "refresh",
                "0",
            )
            == "1"
        )

        rules = get_all_coin_rules(
            force=force
        )

        return jsonify(
            {
                "ok": True,
                "rules": rules,
            }
        )

    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
            }
        ), 500


@app.route(
    "/api/coin/<symbol>",
    methods=["GET"],
)
def api_coin(symbol):
    symbol = symbol.upper()

    if symbol not in SYMBOLS:
        return jsonify(
            {
                "ok": False,
                "error": "Unsupported coin.",
            }
        ), 404

    try:
        product = find_product(
            symbol
        )

        rules = normalise_product_rules(
            product
        )

        with state_lock:
            config = deep_copy(
                state["coins"][
                    symbol
                ]
            )

        return jsonify(
            {
                "ok": True,
                "symbol": symbol,
                "rules": rules,
                "config": config,
            }
        )

    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
            }
        ), 500


@app.route(
    "/api/coin/<symbol>",
    methods=["POST"],
)
def api_coin_config(symbol):
    symbol = symbol.upper()

    if symbol not in SYMBOLS:
        return jsonify(
            {
                "ok": False,
                "error": "Unsupported coin.",
            }
        ), 404

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    enabled = bool(
        data.get(
            "enabled",
            False,
        )
    )

    quantity_raw = data.get(
        "quantity"
    )

    leverage_raw = data.get(
        "leverage"
    )

    if (
        quantity_raw in (
            None,
            "",
        )
        or leverage_raw in (
            None,
            "",
        )
    ):
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Quantity and leverage "
                    "are required."
                ),
            }
        ), 400

    try:
        quantity = float(
            quantity_raw
        )

        leverage = float(
            leverage_raw
        )

    except Exception:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Quantity and leverage "
                    "must be numeric."
                ),
            }
        ), 400

    valid, message = (
        validate_coin_settings(
            symbol,
            quantity,
            leverage,
        )
    )

    if not valid:
        return jsonify(
            {
                "ok": False,
                "error": message,
            }
        ), 400

    with state_lock:
        if enabled:
            # Only ONE coin can be selected/ON.
            for other_symbol in SYMBOLS:
                state["coins"][
                    other_symbol
                ]["enabled"] = (
                    other_symbol
                    == symbol
                )

        else:
            state["coins"][
                symbol
            ]["enabled"] = False

        state["coins"][
            symbol
        ]["quantity"] = quantity

        state["coins"][
            symbol
        ]["leverage"] = leverage

        product = find_product(
            symbol
        )

        state["coins"][
            symbol
        ]["rules"] = (
            normalise_product_rules(
                product
            )
        )

    save_state()

    add_event(
        f"{symbol} settings saved: "
        f"quantity={quantity}, "
        f"leverage={leverage}, "
        f"ON={enabled}"
    )

    return jsonify(
        {
            "ok": True,
            "symbol": symbol,
            "config": state[
                "coins"
            ][symbol],
        }
    )


@app.route(
    "/api/coin/<symbol>/toggle",
    methods=["POST"],
)
def api_coin_toggle(symbol):
    symbol = symbol.upper()

    if symbol not in SYMBOLS:
        return jsonify(
            {
                "ok": False,
                "error": "Unsupported coin.",
            }
        ), 404

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    enabled = bool(
        data.get(
            "enabled",
            False,
        )
    )

    with state_lock:
        coin = state["coins"][
            symbol
        ]

        if enabled:
            if (
                coin.get(
                    "quantity"
                )
                is None
                or coin.get(
                    "leverage"
                )
                is None
            ):
                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            "Set quantity and "
                            "leverage first."
                        ),
                    }
                ), 400

            valid, message = (
                validate_coin_settings(
                    symbol,
                    coin["quantity"],
                    coin["leverage"],
                )
            )

            if not valid:
                return jsonify(
                    {
                        "ok": False,
                        "error": message,
                    }
                ), 400

            for other_symbol in SYMBOLS:
                state["coins"][
                    other_symbol
                ]["enabled"] = (
                    other_symbol
                    == symbol
                )

        else:
            coin["enabled"] = False

    save_state()

    add_event(
        f"{symbol} "
        f"{'ON' if enabled else 'OFF'}"
    )

    return jsonify(
        {
            "ok": True,
            "selected": selected_coin(),
        }
    )


@app.route(
    "/api/config",
    methods=["POST"],
)
def api_config():
    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    mode = str(
        data.get(
            "mode",
            state.get(
                "mode",
                "PAPER",
            ),
        )
    ).upper()

    if mode not in (
        "PAPER",
        "LIVE",
    ):
        return jsonify(
            {
                "ok": False,
                "error": "Invalid mode.",
            }
        ), 400

    with state_lock:
        state["mode"] = mode

    save_state()

    add_event(
        f"Mode changed to {mode}"
    )

    return jsonify(
        {
            "ok": True,
            "mode": mode,
        }
    )


@app.route(
    "/api/start",
    methods=["POST"],
)
def api_start():
    with state_lock:
        selected = selected_coin()

        if not selected:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "Select one coin and "
                        "configure its quantity "
                        "and leverage first."
                    ),
                }
            ), 400

        state["enabled"] = True

    save_state()

    add_event(
        f"SYSTEM ON: automatic trading enabled "
        f"for {selected}."
    )

    return jsonify(
        {
            "ok": True,
            "enabled": True,
            "selected": selected,
        }
    )


@app.route(
    "/api/stop",
    methods=["POST"],
)
def api_stop():
    with state_lock:
        state["enabled"] = False

    save_state()

    add_event(
        "SYSTEM OFF: bot automatic trading "
        "actions disabled."
    )

    return jsonify(
        {
            "ok": True,
            "enabled": False,
        }
    )


@app.route(
    "/api/close",
    methods=["POST"],
)
def api_close():
    with state_lock:
        trade = deep_copy(
            state.get("trade")
        )

        mode = state.get(
            "mode",
            "PAPER",
        )

    if not trade:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "No bot trade recorded."
                ),
            }
        ), 400

    try:
        if mode == "PAPER":
            with state_lock:
                state["trade"] = None

            save_state()

            add_event(
                f"Manual PAPER close: "
                f"{trade['symbol']}"
            )

            return jsonify(
                {
                    "ok": True
                }
            )

        position = get_open_position(
            trade["symbol"]
        )

        if not position:
            with state_lock:
                state["trade"] = None

            save_state()

            return jsonify(
                {
                    "ok": True
                }
            )

        size = float(
            position.get(
                "size",
                0,
            )
            or 0
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
            f"Manual LIVE close: "
            f"{trade['symbol']}"
        )

        return jsonify(
            {
                "ok": True
            }
        )

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
            "ip": ip
        }
    )


HTML = r"""
<!doctype html>
<html>
<head>

<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>Prime Minister AI</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    font-family: Arial, sans-serif;
    background: #0f1217;
    color: #f4f5f7;
}

header {
    padding: 20px;
    background: #171b22;
    border-bottom: 1px solid #303640;
}

h1 {
    margin: 0;
    font-size: 25px;
}

.subtitle {
    margin-top: 5px;
    color: #9ea7b4;
    font-size: 13px;
}

.container {
    width: 100%;
    max-width: 1000px;
    margin: auto;
    padding: 15px;
}

.card {
    background: #181d25;
    border: 1px solid #303640;
    border-radius: 14px;
    padding: 16px;
    margin-bottom: 15px;
}

.status {
    font-size: 23px;
    font-weight: bold;
}

.status-on {
    color: #62d98b;
}

.status-off {
    color: #ff7777;
}

.coin-grid {
    display: grid;
    grid-template-columns:
        repeat(
            auto-fit,
            minmax(145px, 1fr)
        );
    gap: 10px;
}

.coin-card {
    background: #202630;
    border: 2px solid #343c49;
    border-radius: 13px;
    padding: 14px;
    cursor: pointer;
    transition: 0.15s;
}

.coin-card:hover {
    border-color: #758197;
}

.coin-card.selected {
    border-color: #67a6ff;
}

.coin-card.in-trade {
    border-color: #ffbd58;
}

.coin-name {
    font-size: 20px;
    font-weight: bold;
}

.signal {
    margin-top: 7px;
    font-weight: bold;
}

.signal-long {
    color: #5ee28b;
}

.signal-short {
    color: #ff7e7e;
}

.signal-none {
    color: #a9b1bd;
}

.coin-small {
    margin-top: 7px;
    font-size: 12px;
    color: #9da6b2;
}

.coin-on {
    margin-top: 10px;
    color: #62d98b;
    font-weight: bold;
}

.coin-off {
    margin-top: 10px;
    color: #9098a5;
}

button,
select,
input {
    width: 100%;
    border: 1px solid #3b4350;
    background: #222832;
    color: white;
    border-radius: 9px;
    padding: 12px;
    font-size: 15px;
}

button {
    cursor: pointer;
}

button.primary {
    background: #284d7a;
}

button.danger {
    background: #562c31;
}

.row {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
}

.row > * {
    flex: 1;
    min-width: 130px;
}

.small {
    color: #9da6b2;
    font-size: 12px;
}

pre {
    white-space: pre-wrap;
    word-break: break-word;
}

.event {
    border-bottom: 1px solid #2b313b;
    padding: 9px 0;
    font-size: 13px;
}

.modal {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(
        0,
        0,
        0,
        0.72
    );
    z-index: 100;
    padding: 15px;
    overflow-y: auto;
}

.modal.show {
    display: flex;
    align-items: center;
    justify-content: center;
}

.modal-box {
    width: 100%;
    max-width: 520px;
    background: #181d25;
    border: 1px solid #3c4553;
    border-radius: 16px;
    padding: 18px;
}

.rule-grid {
    display: grid;
    grid-template-columns:
        1fr 1fr;
    gap: 8px;
    margin: 12px 0;
}

.rule {
    background: #202630;
    border-radius: 9px;
    padding: 10px;
}

.rule-label {
    color: #929ba8;
    font-size: 11px;
}

.rule-value {
    margin-top: 4px;
    font-weight: bold;
}

.error {
    color: #ff7c7c;
    margin: 10px 0;
}

.success {
    color: #65dc8b;
    margin: 10px 0;
}

</style>

</head>

<body>

<header>
    <h1>Prime Minister AI</h1>
    <div class="subtitle">
        Deterministic trading commander
    </div>
</header>

<div class="container">

<div class="card">

    <div class="status">
        System:
        <span id="systemStatus"
              class="status-off">
            OFF
        </span>
    </div>

    <div class="small"
         style="margin-top:7px;">
        Selected coin:
        <b id="selectedCoin">
            None
        </b>
    </div>

    <br>

    <div class="row">

        <button class="primary"
                onclick="startSystem()">
            SYSTEM ON
        </button>

        <button
                onclick="stopSystem()">
            SYSTEM OFF
        </button>

        <button class="danger"
                onclick="manualClose()">
            MANUAL CLOSE
        </button>

    </div>

</div>


<div class="card">

    <h2>Top 5 Coins</h2>

    <div class="small"
         style="margin-bottom:12px;">
        Tap a coin to configure its
        Delta-specific quantity and leverage.
        Only one coin can be ON.
    </div>

    <div id="coinGrid"
         class="coin-grid">
    </div>

</div>


<div class="card">

    <h2>Current Trade</h2>

    <pre id="trade">
None
    </pre>

</div>


<div class="card">

    <h2>Latest Signal</h2>

    <pre id="signal">
None
    </pre>

</div>


<div class="card">

    <h2>Public IP</h2>

    <div id="ip">
        Not checked
    </div>

    <br>

    <button onclick="refreshIP()">
        CHECK IP
    </button>

</div>


<div class="card">

    <h2>Events</h2>

    <div id="events">
    </div>

</div>

</div>


<div id="coinModal"
     class="modal">

    <div class="modal-box">

        <h2 id="modalTitle">
            BTCUSD
        </h2>

        <div id="modalError"
             class="error">
        </div>

        <h3>Delta Rules</h3>

        <div id="rules"
             class="rule-grid">
        </div>

        <h3>Your Settings</h3>

        <label>
            Quantity
        </label>

        <input id="coinQuantity"
               type="number"
               min="0"
               step="any">

        <br><br>

        <label>
            Leverage
        </label>

        <input id="coinLeverage"
               type="number"
               min="0"
               step="any">

        <br><br>

        <div class="row">

            <button class="primary"
                    onclick="saveCoin()">
                SAVE SETTINGS
            </button>

            <button
                    onclick="toggleCoin()">
                ON / OFF
            </button>

            <button
                    onclick="closeModal()">
                CLOSE
            </button>

        </div>

        <div id="modalMessage"
             class="small"
             style="margin-top:12px;">
        </div>

    </div>

</div>


<script>

const SYMBOLS = [
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "XRPUSD",
    "DOGEUSD"
];

let currentSymbol = null;
let currentRules = null;


async function api(
    url,
    options = {}
) {
    const response = await fetch(
        url,
        {
            headers: {
                "Content-Type":
                    "application/json"
            },
            ...options
        }
    );

    const data =
        await response.json();

    if (!response.ok) {
        throw new Error(
            data.error ||
            "Request failed"
        );
    }

    return data;
}


function pretty(value) {
    if (
        value === null ||
        value === undefined
    ) {
        return "None";
    }

    return JSON.stringify(
        value,
        null,
        2
    );
}


function signalClass(
    direction
) {
    if (
        direction === "LONG"
    ) {
        return "signal-long";
    }

    if (
        direction === "SHORT"
    ) {
        return "signal-short";
    }

    return "signal-none";
}


function signalText(
    coin
) {
    const signal =
        coin.last_signal;

    if (!signal) {
        return "NO SIGNAL";
    }

    return (
        signal.direction +
        " • " +
        signal.score
    );
}


function renderCoins(
    state
) {
    const grid =
        document.getElementById(
            "coinGrid"
        );

    grid.innerHTML = "";

    const selected =
        Object.keys(
            state.coins
        ).find(
            symbol =>
                state.coins[
                    symbol
                ].enabled
        );

    SYMBOLS.forEach(
        function(symbol) {

            const coin =
                state.coins[
                    symbol
                ] || {};

            const signal =
                coin.last_signal;

            const card =
                document.createElement(
                    "div"
                );

            card.className =
                "coin-card";

            if (
                coin.enabled
            ) {
                card.classList.add(
                    "selected"
                );
            }

            if (
                state.trade &&
                state.trade.symbol
                === symbol
            ) {
                card.classList.add(
                    "in-trade"
                );
            }

            const name =
                document.createElement(
                    "div"
                );

            name.className =
                "coin-name";

            name.textContent =
                symbol.replace(
                    "USD",
                    ""
                );

            const sig =
                document.createElement(
                    "div"
                );

            sig.className =
                "signal " +
                signalClass(
                    signal
                        ? signal.direction
                        : "NO_TRADE"
                );

            sig.textContent =
                signalText(
                    coin
                );

            const config =
                document.createElement(
                    "div"
                );

            config.className =
                "coin-small";

            config.textContent =
                "Qty: " +
                (
                    coin.quantity
                    ?? "Not set"
                ) +
                " | Lev: " +
                (
                    coin.leverage
                    ?? "Not set"
                );

            const status =
                document.createElement(
                    "div"
                );

            status.className =
                coin.enabled
                    ? "coin-on"
                    : "coin-off";

            status.textContent =
                coin.enabled
                    ? "● ON"
                    : "○ OFF";

            card.appendChild(
                name
            );

            card.appendChild(
                sig
            );

            card.appendChild(
                config
            );

            card.appendChild(
                status
            );

            card.onclick =
                function() {
                    openCoin(
                        symbol
                    );
                };

            grid.appendChild(
                card
            );
        }
    );

    document.getElementById(
        "selectedCoin"
    ).textContent =
        selected || "None";
}


async function openCoin(
    symbol
) {
    currentSymbol =
        symbol;

    document.getElementById(
        "modalTitle"
    ).textContent =
        symbol;

    document.getElementById(
        "modalError"
    ).textContent =
        "";

    document.getElementById(
        "modalMessage"
    ).textContent =
        "Loading Delta product rules...";

    document.getElementById(
        "coinModal"
    ).classList.add(
        "show"
    );

    try {

        const data =
            await api(
                "/api/coin/" +
                symbol
            );

        currentRules =
            data.rules;

        renderRules(
            data.rules
        );

        const config =
            data.config || {};

        document.getElementById(
            "coinQuantity"
        ).value =
            config.quantity ??
            "";

        document.getElementById(
            "coinLeverage"
        ).value =
            config.leverage ??
            "";

        document.getElementById(
            "modalMessage"
        ).textContent =
            config.enabled
                ? "This coin is currently ON."
                : "This coin is currently OFF.";

    } catch (e) {

        document.getElementById(
            "modalError"
        ).textContent =
            e.message;

        document.getElementById(
            "modalMessage"
        ).textContent =
            "";
    }
}


function renderRules(
    rules
) {
    const box =
        document.getElementById(
            "rules"
        );

    box.innerHTML = "";

    const items = [
        [
            "Product ID",
            rules.product_id
        ],
        [
            "Minimum Quantity",
            rules.minimum_quantity
        ],
        [
            "Maximum Quantity",
            rules.maximum_quantity
        ],
        [
            "Quantity Step",
            rules.quantity_step
        ],
        [
            "Minimum Leverage",
            rules.minimum_leverage
        ],
        [
            "Maximum Leverage",
            rules.maximum_leverage
        ],
        [
            "Trading Status",
            rules.trading_status
        ],
        [
            "Contract Value",
            rules.contract_value
        ]
    ];

    items.forEach(
        function(item) {

            const rule =
                document.createElement(
                    "div"
                );

            rule.className =
                "rule";

            const label =
                document.createElement(
                    "div"
                );

            label.className =
                "rule-label";

            label.textContent =
                item[0];

            const value =
                document.createElement(
                    "div"
                );

            value.className =
                "rule-value";

            value.textContent =
                item[1] ??
                "Not supplied by Delta";

            rule.appendChild(
                label
            );

            rule.appendChild(
                value
            );

            box.appendChild(
                rule
            );
        }
    );
}


async function saveCoin() {

    if (!currentSymbol) {
        return;
    }

    const quantity =
        document.getElementById(
            "coinQuantity"
        ).value;

    const leverage =
        document.getElementById(
            "coinLeverage"
        ).value;

    try {

        const status =
            await api(
                "/api/status"
            );

        const existing =
            status.coins[
                currentSymbol
            ];

        const data =
            await api(
                "/api/coin/" +
                currentSymbol,
                {
                    method: "POST",
                    body: JSON.stringify(
                        {
                            enabled:
                                existing
                                    ? existing.enabled
                                    : false,
                            quantity:
                                Number(
                                    quantity
                                ),
                            leverage:
                                Number(
                                    leverage
                                )
                        }
                    )
                }
            );

        document.getElementById(
            "modalMessage"
        ).textContent =
            "Settings saved.";

        refresh();

    } catch (e) {

        document.getElementById(
            "modalError"
        ).textContent =
            e.message;
    }
}


async function toggleCoin() {

    if (!currentSymbol) {
        return;
    }

    try {

        const status =
            await api(
                "/api/status"
            );

        const coin =
            status.coins[
                currentSymbol
            ];

        const next =
            !coin.enabled;

        await api(
            "/api/coin/" +
            currentSymbol +
            "/toggle",
            {
                method: "POST",
                body: JSON.stringify(
                    {
                        enabled: next
                    }
                )
            }
        );

        document.getElementById(
            "modalMessage"
        ).textContent =
            next
                ? currentSymbol +
                  " is now ON."
                : currentSymbol +
                  " is now OFF.";

        refresh();

    } catch (e) {

        document.getElementById(
            "modalError"
        ).textContent =
            e.message;
    }
}


function closeModal() {
    document.getElementById(
        "coinModal"
    ).classList.remove(
        "show"
    );
}


async function startSystem() {

    try {

        await api(
            "/api/start",
            {
                method: "POST"
            }
        );

        refresh();

    } catch (e) {

        alert(
            e.message
        );
    }
}


async function stopSystem() {

    try {

        await api(
            "/api/stop",
            {
                method: "POST"
            }
        );

        refresh();

    } catch (e) {

        alert(
            e.message
        );
    }
}


async function manualClose() {

    if (
        !confirm(
            "Close current trade?"
        )
    ) {
        return;
    }

    try {

        await api(
            "/api/close",
            {
                method: "POST"
            }
        );

        refresh();

    } catch (e) {

        alert(
            e.message
        );
    }
}


async function refreshIP() {

    try {

        const data =
            await api(
                "/api/ip"
            );

        document.getElementById(
            "ip"
        ).textContent =
            data.ip ||
            "Unable to detect";

    } catch (e) {

        document.getElementById(
            "ip"
        ).textContent =
            "Unable to detect";
    }
}


async function refresh() {

    try {

        const data =
            await api(
                "/api/status"
            );

        const status =
            document.getElementById(
                "systemStatus"
            );

        status.textContent =
            data.enabled
                ? "ON"
                : "OFF";

        status.className =
            data.enabled
                ? "status-on"
                : "status-off";

        document.getElementById(
            "trade"
        ).textContent =
            pretty(
                data.trade
            );

        document.getElementById(
            "signal"
        ).textContent =
            pretty(
                data.last_signal
            );

        document.getElementById(
            "ip"
        ).textContent =
            data.last_ip ||
            "Not checked";

        renderCoins(
            data
        );

        const events =
            document.getElementById(
                "events"
            );

        events.innerHTML = "";

        (
            data.events ||
            []
        ).forEach(
            function(event) {

                const div =
                    document.createElement(
                        "div"
                    );

                div.className =
                    "event";

                const time =
                    document.createElement(
                        "div"
                    );

                time.className =
                    "small";

                time.textContent =
                    event.time ||
                    "";

                const message =
                    document.createElement(
                        "div"
                    );

                message.textContent =
                    event.message ||
                    "";

                div.appendChild(
                    time
                );

                div.appendChild(
                    message
                );

                events.appendChild(
                    div
                );
            }
        );

    } catch (e) {

        console.error(e);
    }
}


refreshIP();
refresh();

setInterval(
    refresh,
    3000
);

</script>

</body>
</html>
"""


if __name__ == "__main__":
    start_engine()

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "5000",
            )
        ),
        debug=False,
        threaded=True,
    )
