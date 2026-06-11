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


BOT_TOKEN = os.getenv("BOT_TOKEN")
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOLS = ["AAPL", "TSLA", "NVDA", "AMD", "PLTR", "SOFI", "COIN", "MSTR", "META", "MSFT"]

POSITION_SIZE_DOLLARS = 500
TAKE_PROFIT_PERCENT = 2.0
STOP_LOSS_PERCENT = 1.0
SCAN_SECONDS = 300


trading_client = TradingClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    paper=True
)

data_client = StockHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY
)


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing BOT_TOKEN or CHAT_ID")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message
    }

    try:
        requests.post(url, json=payload, timeout=20)
    except Exception as e:
        print("Telegram error:", e)


def get_position_symbols():
    try:
        positions = trading_client.get_all_positions()
        return {p.symbol for p in positions}
    except Exception as e:
        print("Position error:", e)
        return set()


def get_bars(symbol):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=10)

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end
    )

    bars = data_client.get_stock_bars(request).df

    if bars.empty:
        return None

    if isinstance(bars.index, type(bars.index)) and "symbol" in bars.index.names:
        bars = bars.loc[symbol]

    return bars.tail(5)


def check_signal(symbol):
    bars = get_bars(symbol)

    if bars is None or len(bars) < 3:
        return None

    yesterday = bars.iloc[-2]
    today = bars.iloc[-1]

    today_close = float(today["close"])
    yesterday_high = float(yesterday["high"])
    yesterday_volume = float(yesterday["volume"])
    today_volume = float(today["volume"])

    price_change = ((today_close - float(yesterday["close"])) / float(yesterday["close"])) * 100
    volume_ratio = today_volume / yesterday_volume if yesterday_volume > 0 else 0

    breakout = today_close > yesterday_high
    green = price_change > 0
    volume_spike = volume_ratio >= 1.2

    if green and breakout and volume_spike:
        return {
            "symbol": symbol,
            "price": today_close,
            "price_change": price_change,
            "volume_ratio": volume_ratio
        }

    return None


def place_paper_trade(signal):
    symbol = signal["symbol"]
    price = signal["price"]

    qty = max(1, int(POSITION_SIZE_DOLLARS / price))

    take_profit_price = round(price * (1 + TAKE_PROFIT_PERCENT / 100), 2)
    stop_loss_price = round(price * (1 - STOP_LOSS_PERCENT / 100), 2)

    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=take_profit_price),
        stop_loss=StopLossRequest(stop_price=stop_loss_price)
    )

    trading_client.submit_order(order_data=order)

    msg = f"""🐃 BLACK BISON STOCK PAPER TRADE

{symbol} LONG

Entry Approx: ${price}
Qty: {qty}

TP: ${take_profit_price}
SL: ${stop_loss_price}

Move Today: {signal['price_change']:.2f}%
Volume Ratio: {signal['volume_ratio']:.2f}x

Status: PAPER ORDER SENT
Not financial advice."""

    send_telegram(msg)
    print(msg)


def scan_market():
    open_symbols = get_position_symbols()

    for symbol in SYMBOLS:
        if symbol in open_symbols:
            print(f"{symbol}: already in position")
            continue

        try:
            signal = check_signal(symbol)

            if signal:
                place_paper_trade(signal)
            else:
                print(f"{symbol}: no setup")

        except Exception as e:
            print(f"{symbol} error:", e)


def main():
    send_telegram("🐃 Black Bison Stock Scanner started. Paper trading mode ON.")
    print("BLACK BISON STOCK SCANNER STARTED")

    while True:
        print("Scanning stock market...")
        scan_market()
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()
