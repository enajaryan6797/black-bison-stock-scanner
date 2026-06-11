import os
import time
import json
import requests
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

SYMBOLS = ["AAPL", "TSLA", "NVDA", "AMD", "PLTR", "SOFI", "COIN", "MSTR", "META", "MSFT"]

STOCK_POSITION_SIZE_DOLLARS = 5000

STOCK_TAKE_PROFIT_PERCENT = 2.0
STOCK_STOP_LOSS_PERCENT = 1.0

OPTION_QTY = 1
OPTION_TAKE_PROFIT_PERCENT = 30.0
OPTION_STOP_LOSS_PERCENT = 20.0

SCAN_SECONDS = 300
STATE_FILE = "stock_options_state.json"

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"option_trades": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
    except Exception as e:
        print("Telegram error:", e, flush=True)


def get_open_positions():
    try:
        return {p.symbol for p in trading_client.get_all_positions()}
    except Exception as e:
        print("Position error:", e, flush=True)
        return set()


def get_all_positions():
    try:
        return trading_client.get_all_positions()
    except Exception as e:
        print("Get all positions error:", e, flush=True)
        return []


def get_bars(symbol):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=3)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=DataFeed.IEX
    )

    df = data_client.get_stock_bars(req).df

    if df.empty:
        return None

    if "symbol" in df.index.names:
        df = df.loc[symbol]

    return df.tail(100)


def check_signal(symbol):
    bars = get_bars(symbol)

    if bars is None or len(bars) < 40:
        print(f"{symbol}: not enough data", flush=True)
        return None

    last = bars.iloc[-1]
    prev = bars.iloc[-21:-1]

    price = float(last["close"])
    prev_high = float(prev["high"].max())
    prev_low = float(prev["low"].min())
    avg_volume = float(prev["volume"].mean())
    last_volume = float(last["volume"])

    volume_ratio = last_volume / avg_volume if avg_volume > 0 else 0

    long_signal = price > prev_high and volume_ratio >= 1.8
    short_signal = price < prev_low and volume_ratio >= 1.8

    if long_signal:
        return {
            "symbol": symbol,
            "side": "LONG",
            "price": price,
            "volume_ratio": volume_ratio,
            "level": prev_high,
        }

    if short_signal:
        return {
            "symbol": symbol,
            "side": "SHORT",
            "price": price,
            "volume_ratio": volume_ratio,
            "level": prev_low,
        }

    print(f"{symbol}: no setup | price={price} vol={volume_ratio:.2f}x", flush=True)
    return None


def place_stock_trade(signal):
    symbol = signal["symbol"]

    if signal["side"] != "LONG":
        return

    price = signal["price"]
    qty = max(1, int(STOCK_POSITION_SIZE_DOLLARS / price))

    tp_price = round(price * (1 + STOCK_TAKE_PROFIT_PERCENT / 100), 2)
    sl_price = round(price * (1 - STOCK_STOP_LOSS_PERCENT / 100), 2)

    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=tp_price),
        stop_loss=StopLossRequest(stop_price=sl_price),
    )

    trading_client.submit_order(order_data=order)

    msg = f"""🐃 BLACK BISON STOCK TRADE

📈 {symbol} LONG

Entry approx: ${price:.2f}
Qty: {qty}

TP: ${tp_price}
SL: ${sl_price}

Volume: {signal['volume_ratio']:.2f}x

Status: PAPER STOCK ORDER SENT"""

    print(msg, flush=True)
    send_telegram(msg)


def get_option_contract(symbol, side, stock_price):
    today = datetime.now(timezone.utc).date()
    exp_gte = today + timedelta(days=7)
    exp_lte = today + timedelta(days=21)

    contract_type = "call" if side == "LONG" else "put"

    url = "https://paper-api.alpaca.markets/v2/options/contracts"

    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

    params = {
        "underlying_symbols": symbol,
        "status": "active",
        "expiration_date_gte": str(exp_gte),
        "expiration_date_lte": str(exp_lte),
        "type": contract_type,
        "limit": 100,
    }

    try:
        r = requests.get(url, headers=headers, params=params, timeout=20)
        data = r.json()

        contracts = data.get("option_contracts") or data.get("contracts") or []

        if not contracts:
            print(f"{symbol}: no option contracts found", flush=True)
            return None

        best = None
        best_distance = None

        for c in contracts:
            strike = float(c.get("strike_price"))
            tradable = c.get("tradable", True)

            if not tradable:
                continue

            distance = abs(strike - stock_price)

            if best is None or distance < best_distance:
                best = c
                best_distance = distance

        if not best:
            return None

        return best.get("symbol")

    except Exception as e:
        print(f"{symbol}: option contract error {e}", flush=True)
        return None


def option_already_open(symbol):
    state = load_state()

    for t in state.get("option_trades", []):
        if t.get("underlying") == symbol and t.get("status") == "OPEN":
            return True

    return False


def place_option_trade(signal):
    symbol = signal["symbol"]
    side = signal["side"]
    price = signal["price"]

    if option_already_open(symbol):
        print(f"{symbol}: option already open", flush=True)
        return

    option_symbol = get_option_contract(symbol, side, price)

    if not option_symbol:
        print(f"{symbol}: no valid option symbol", flush=True)
        return

    order = MarketOrderRequest(
        symbol=option_symbol,
        qty=OPTION_QTY,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )

    trading_client.submit_order(order_data=order)

    state = load_state()
    state["option_trades"].append({
        "underlying": symbol,
        "option_symbol": option_symbol,
        "side": side,
        "status": "OPEN",
        "created_at": int(time.time()),
    })
    save_state(state)

    option_type = "CALL" if side == "LONG" else "PUT"

    msg = f"""🐃 BLACK BISON OPTION TRADE

🎯 {symbol} {option_type}

Option: {option_symbol}
Qty: {OPTION_QTY}

Underlying price: ${price:.2f}
Signal: {side}
Volume: {signal['volume_ratio']:.2f}x

TP: +{OPTION_TAKE_PROFIT_PERCENT:.0f}%
SL: -{OPTION_STOP_LOSS_PERCENT:.0f}%

Status: PAPER OPTION ORDER SENT"""

    print(msg, flush=True)
    send_telegram(msg)


def monitor_options():
    state = load_state()
    positions = get_all_positions()

    position_map = {p.symbol: p for p in positions}
    changed = False

    for trade in state.get("option_trades", []):
        if trade.get("status") != "OPEN":
            continue

        option_symbol = trade["option_symbol"]

        if option_symbol not in position_map:
            continue

        pos = position_map[option_symbol]

        try:
            pnl_percent = float(pos.unrealized_plpc) * 100
        except Exception:
            continue

        if pnl_percent >= OPTION_TAKE_PROFIT_PERCENT:
            result = "TP"
        elif pnl_percent <= -OPTION_STOP_LOSS_PERCENT:
            result = "SL"
        else:
            continue

        try:
            trading_client.close_position(option_symbol)

            trade["status"] = result
            trade["closed_at"] = int(time.time())
            trade["pnl_percent"] = pnl_percent
            changed = True

            emoji = "✅" if result == "TP" else "❌"

            msg = f"""🐃 BLACK BISON OPTION RESULT

{emoji} {trade['underlying']} OPTION CLOSED

Option: {option_symbol}
Result: {result}
P/L: {pnl_percent:.2f}%

Status: PAPER OPTION CLOSED"""

            print(msg, flush=True)
            send_telegram(msg)

        except Exception as e:
            print(f"Close option error {option_symbol}: {e}", flush=True)

    if changed:
        save_state(state)


def scan():
    monitor_options()

    open_positions = get_open_positions()

    for symbol in SYMBOLS:
        try:
            signal = check_signal(symbol)

            if not signal:
                continue

            if signal["side"] == "LONG" and symbol not in open_positions:
                place_stock_trade(signal)

            if signal["volume_ratio"] >= 2.2:
                place_option_trade(signal)

        except Exception as e:
            print(f"{symbol} error: {e}", flush=True)


def main():
    print("BLACK BISON STOCK + OPTIONS BOT LIVE", flush=True)
    send_telegram("🐃 Black Bison Stock + Options Bot started. Paper mode ON.")

    while True:
        print("Scanning stocks and options...", flush=True)
        scan()
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()
