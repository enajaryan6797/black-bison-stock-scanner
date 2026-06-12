import os
import time
import json
import requests
from datetime import datetime, timedelta, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

STATE_FILE = "stock_options_state.json"

SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "PLTR", "SOFI", "COIN", "MSTR",
    "AMZN", "GOOGL", "NFLX", "AVGO", "CRM", "UBER", "SHOP", "SMCI", "MU", "ARM",
    "TSM", "INTC", "ADBE", "PANW", "SNOW", "CRWD", "NET", "DDOG", "RBLX", "HOOD",
    "SPY", "QQQ", "IWM", "DIA",
    "JPM", "BAC", "GS", "MS", "WFC",
    "XOM", "CVX", "SLB",
    "WMT", "COST", "TGT",
    "DIS", "NKE", "ABNB", "PYPL", "SQ"
]

STOCK_POSITION_SIZE_DOLLARS = 5000
STOCK_TAKE_PROFIT_PERCENT = 2.0
STOCK_STOP_LOSS_PERCENT = 1.0

OPTION_QTY = 1
OPTION_TAKE_PROFIT_PERCENT = 30.0
OPTION_STOP_LOSS_PERCENT = 20.0

SCAN_SECONDS = 300


trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def now_ts():
    return int(time.time())


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {
            "stock_trades": [],
            "option_trades": [],
            "telegram_offset": None,
            "start_equity": None
        }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
    }


def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
    except Exception as e:
        print("Telegram error:", e, flush=True)


def get_account_value():
    try:
        account = trading_client.get_account()
        return float(account.equity)
    except Exception as e:
        print("Account error:", e, flush=True)
        return None


def init_start_equity():
    state = load_state()

    if state.get("start_equity") is None:
        equity = get_account_value()

        if equity is not None:
            state["start_equity"] = equity
            save_state(state)

    return state


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
            "level": prev_high
        }

    if short_signal:
        return {
            "symbol": symbol,
            "side": "SHORT",
            "price": price,
            "volume_ratio": volume_ratio,
            "level": prev_low
        }

    print(f"{symbol}: no setup | price={price} vol={volume_ratio:.2f}x", flush=True)
    return None


def stock_trade_already_open(state, symbol):
    for t in state.get("stock_trades", []):
        if t.get("symbol") == symbol and t.get("status") == "OPEN":
            return True
    return False


def option_trade_already_open(state, symbol):
    for t in state.get("option_trades", []):
        if t.get("underlying") == symbol and t.get("status") == "OPEN":
            return True
    return False


def place_stock_trade(signal):
    symbol = signal["symbol"]

    if signal["side"] != "LONG":
        return

    state = load_state()

    if stock_trade_already_open(state, symbol):
        print(f"{symbol}: stock trade already open", flush=True)
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

    submitted = trading_client.submit_order(order_data=order)

    state["stock_trades"].append({
        "symbol": symbol,
        "side": "LONG",
        "qty": qty,
        "entry_approx": price,
        "tp": tp_price,
        "sl": sl_price,
        "status": "OPEN",
        "created_at": now_ts(),
        "order_id": str(submitted.id)
    })
    save_state(state)

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

    params = {
        "underlying_symbols": symbol,
        "status": "active",
        "expiration_date_gte": str(exp_gte),
        "expiration_date_lte": str(exp_lte),
        "type": contract_type,
        "limit": 100
    }

    try:
        r = requests.get(url, headers=alpaca_headers(), params=params, timeout=20)
        data = r.json()

        contracts = data.get("option_contracts") or data.get("contracts") or []

        if not contracts:
            print(f"{symbol}: no option contracts found", flush=True)
            return None

        best = None
        best_distance = None

        for c in contracts:
            try:
                strike = float(c.get("strike_price"))
                tradable = c.get("tradable", True)

                if not tradable:
                    continue

                distance = abs(strike - stock_price)

                if best is None or distance < best_distance:
                    best = c
                    best_distance = distance

            except Exception:
                continue

        if not best:
            return None

        return best.get("symbol")

    except Exception as e:
        print(f"{symbol}: option contract error {e}", flush=True)
        return None


def place_option_trade(signal):
    symbol = signal["symbol"]
    side = signal["side"]
    price = signal["price"]

    state = load_state()

    if option_trade_already_open(state, symbol):
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

    submitted = trading_client.submit_order(order_data=order)

    state["option_trades"].append({
        "underlying": symbol,
        "option_symbol": option_symbol,
        "side": side,
        "qty": OPTION_QTY,
        "status": "OPEN",
        "created_at": now_ts(),
        "order_id": str(submitted.id)
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


def monitor_stock_trades():
    state = load_state()
    positions = get_open_positions()
    changed = False

    for trade in state.get("stock_trades", []):
        if trade.get("status") != "OPEN":
            continue

        symbol = trade["symbol"]

        if symbol in positions:
            continue

        entry = float(trade.get("entry_approx", 0))
        qty = int(trade.get("qty", 0))

        if entry <= 0 or qty <= 0:
            continue

        # Approx result if bracket closed. Exact realized P/L is still visible in Alpaca.
        current_equity = get_account_value()

        trade["status"] = "CLOSED"
        trade["closed_at"] = now_ts()
        trade["note"] = "Closed by Alpaca bracket or manual close. P/L approximate by account stats."
        changed = True

        msg = f"""🐃 BLACK BISON STOCK CLOSED

{symbol}

Qty: {qty}
Entry approx: ${entry:.2f}

Status: CLOSED
Check Alpaca Activities for exact fill price."""

        print(msg, flush=True)
        send_telegram(msg)

    if changed:
        save_state(state)


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
            trade["status"] = "CLOSED"
            trade["closed_at"] = now_ts()
            trade["note"] = "Option position no longer open."
            changed = True
            continue

        pos = position_map[option_symbol]

        try:
            pnl_percent = float(pos.unrealized_plpc) * 100
            pnl_dollars = float(pos.unrealized_pl)
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
            trade["closed_at"] = now_ts()
            trade["pnl_percent"] = pnl_percent
            trade["pnl_dollars"] = pnl_dollars
            changed = True

            emoji = "✅" if result == "TP" else "❌"

            msg = f"""🐃 BLACK BISON OPTION RESULT

{emoji} {trade['underlying']} OPTION CLOSED

Option: {option_symbol}
Result: {result}

P/L: {pnl_percent:.2f}%
P/L Dollars: ${pnl_dollars:.2f}

Status: PAPER OPTION CLOSED"""

            print(msg, flush=True)
            send_telegram(msg)

        except Exception as e:
            print(f"Close option error {option_symbol}: {e}", flush=True)

    if changed:
        save_state(state)


def scan():
    monitor_stock_trades()
    monitor_options()

    open_positions = get_open_positions()
    state = load_state()

    for symbol in SYMBOLS:
        try:
            signal = check_signal(symbol)

            if not signal:
                continue

            if signal["side"] == "LONG" and symbol not in open_positions:
                place_stock_trade(signal)

            if signal["volume_ratio"] >= 2.2:
                place_option_trade(signal)

            time.sleep(0.2)

        except Exception as e:
            print(f"{symbol} error: {e}", flush=True)


def get_stats_text():
    state = load_state()

    equity = get_account_value()
    start_equity = state.get("start_equity")

    stock_trades = state.get("stock_trades", [])
    option_trades = state.get("option_trades", [])

    closed_stock = [t for t in stock_trades if t.get("status") != "OPEN"]
    open_stock = [t for t in stock_trades if t.get("status") == "OPEN"]

    closed_options = [t for t in option_trades if t.get("status") != "OPEN"]
    open_options = [t for t in option_trades if t.get("status") == "OPEN"]

    option_wins = [t for t in closed_options if t.get("status") == "TP"]
    option_losses = [t for t in closed_options if t.get("status") == "SL"]

    option_realized = sum(float(t.get("pnl_dollars", 0)) for t in closed_options)

    total_closed = len(closed_stock) + len(closed_options)
    total_open = len(open_stock) + len(open_options)

    total_pnl = 0
    total_pnl_text = "N/A"

    if equity is not None and start_equity is not None:
        total_pnl = equity - float(start_equity)
        total_pnl_text = f"${total_pnl:.2f}"

    option_win_rate = 0
    if len(option_wins) + len(option_losses) > 0:
        option_win_rate = (len(option_wins) / (len(option_wins) + len(option_losses))) * 100

    return f"""📊 BLACK BISON STOCK + OPTIONS STATS

Account Equity: ${equity:.2f} 
Start Equity: ${float(start_equity):.2f}
Total Account P/L: {total_pnl_text}

Open Trades: {total_open}
Closed Trades: {total_closed}

📈 Stocks:
Open: {len(open_stock)}
Closed: {len(closed_stock)}

🎯 Options:
Open: {len(open_options)}
Closed: {len(closed_options)}
✅ Option Wins: {len(option_wins)}
❌ Option Losses: {len(option_losses)}
🏆 Option Win Rate: {option_win_rate:.2f}%
💵 Option Realized P/L: ${option_realized:.2f}

Tracked Symbols: {len(SYMBOLS)}
"""


def get_open_text():
    positions = get_all_positions()

    if not positions:
        return "No open positions."

    lines = ["📌 OPEN POSITIONS\n"]

    for p in positions[:40]:
        try:
            lines.append(
                f"{p.symbol}: Qty {p.qty}, P/L ${float(p.unrealized_pl):.2f}, "
                f"{float(p.unrealized_plpc) * 100:.2f}%"
            )
        except Exception:
            lines.append(f"{p.symbol}: open")

    return "\n".join(lines)


def handle_telegram_commands():
    state = load_state()

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"timeout": 5}

        if state.get("telegram_offset"):
            params["offset"] = state["telegram_offset"]

        r = requests.get(url, params=params, timeout=10)
        data = r.json()

        if not data.get("ok"):
            return

        for update in data.get("result", []):
            state["telegram_offset"] = update["update_id"] + 1

            message = update.get("message", {})
            text = message.get("text", "")
            chat_id = str(message.get("chat", {}).get("id", ""))

            if chat_id != str(CHAT_ID):
                continue

            if text == "/start":
                send_telegram("🐃 Black Bison Stock + Options Bot is online.")

            elif text == "/stats":
                send_telegram(get_stats_text())

            elif text == "/open":
                send_telegram(get_open_text())

            elif text == "/symbols":
                send_telegram("Tracked symbols:\n" + ", ".join(SYMBOLS))

    except Exception as e:
        print("Telegram command error:", e, flush=True)

    save_state(state)


def main():
    init_start_equity()

    print("BLACK BISON STOCK + OPTIONS BOT WITH STATS LIVE", flush=True)
    send_telegram("🐃 Black Bison Stock + Options Bot restarted. Stats enabled.")

    last_scan = 0

    while True:
        handle_telegram_commands()

        if time.time() - last_scan >= SCAN_SECONDS:
            print("Scanning stocks and options...", flush=True)
            scan()
            last_scan = time.time()

        time.sleep(2)


if __name__ == "__main__":
    main()
