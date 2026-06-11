import os
import time
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

POSITION_SIZE_DOLLARS = 5000
TAKE_PROFIT_PERCENT = 2.0
STOP_LOSS_PERCENT = 1.0
SCAN_SECONDS = 300

trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


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

    return df.tail(80)


def check_signal(symbol):
    bars = get_bars(symbol)

    if bars is None or len(bars) < 30:
        print(f"{symbol}: not enough data", flush=True)
        return None

    last = bars.iloc[-1]
    prev = bars.iloc[-21:-1]

    price = float(last["close"])
    prev_high = float(prev["high"].max())
    avg_volume = float(prev["volume"].mean())
    last_volume = float(last["volume"])

    volume_ratio = last_volume / avg_volume if avg_volume > 0 else 0
    breakout = price > prev_high
    volume_spike = volume_ratio >= 1.8

    if breakout and volume_spike:
        return {
            "symbol": symbol,
            "price": price,
            "volume_ratio": volume_ratio,
            "prev_high": prev_high,
        }

    print(f"{symbol}: no setup | price={price} vol={volume_ratio:.2f}x", flush=True)
    return None


def place_trade(signal):
    symbol = signal["symbol"]
    price = signal["price"]

    qty = max(1, int(POSITION_SIZE_DOLLARS / price))

    tp_price = round(price * (1 + TAKE_PROFIT_PERCENT / 100), 2)
    sl_price = round(price * (1 - STOP_LOSS_PERCENT / 100), 2)

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

    msg = f"""🐃 BLACK BISON STOCK PAPER TRADE

{symbol} LONG

Entry approx: ${price}
Qty: {qty}

TP: ${tp_price}
SL: ${sl_price}

Volume: {signal['volume_ratio']:.2f}x
Breakout above: ${signal['prev_high']:.2f}

Status: PAPER ORDER SENT"""

    print(msg, flush=True)
    send_telegram(msg)


def scan():
    open_positions = get_open_positions()

    for symbol in SYMBOLS:
        if symbol in open_positions:
            print(f"{symbol}: already open", flush=True)
            continue

        try:
            signal = check_signal(symbol)
            if signal:
                place_trade(signal)
        except Exception as e:
            print(f"{symbol} error: {e}", flush=True)


def main():
    print("BLACK BISON STOCK SCANNER LIVE", flush=True)
    send_telegram("🐃 Black Bison Stock Scanner started. Paper trading mode ON.")

    while True:
        print("Scanning stocks...", flush=True)
        scan()
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()
