import os
import time
import json
import requests
from datetime import datetime, timedelta, timezone

import pandas as pd

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

STATE_FILE = "black_bison_stock_v3_state.json"

SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "PLTR", "SOFI", "COIN", "MSTR",
    "AMZN", "GOOGL", "NFLX", "AVGO", "CRM", "UBER", "SHOP", "SMCI", "MU", "ARM",
    "TSM", "INTC", "ADBE", "PANW", "SNOW", "CRWD", "NET", "DDOG", "RBLX", "HOOD",
    "SPY", "QQQ", "IWM", "DIA",
    "JPM", "BAC", "GS", "MS", "WFC",
    "XOM", "CVX", "SLB",
    "WMT", "COST", "TGT",
    "DIS", "NKE", "ABNB", "PYPL", "SQ",
    "ORCL", "NOW", "MDB", "ZS", "AI", "BABA", "PDD", "LI", "RIVN", "LCID",
    "F", "GM", "BA", "CAT", "DE", "LULU", "ELF", "CELH", "ROKU", "DKNG",
    "SPCX", "RKLB", "ASTS", "LUNR", "PL", "SPIR", "IRDM"
]

STOCK_POSITION_SIZE_DOLLARS = 5000
STOCK_TAKE_PROFIT_PERCENT = 2.0
STOCK_STOP_LOSS_PERCENT = 1.0

OPTION_QTY = 1
OPTION_TAKE_PROFIT_PERCENT = 30.0
OPTION_STOP_LOSS_PERCENT = 20.0

SCAN_SECONDS = 300

MIN_SCORE_FOR_STOCK = 7
MIN_SCORE_FOR_OPTION = 8


trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    except Exception:
        state = {}

    state.setdefault("stock_trades", [])
    state.setdefault("option_trades", [])
    state.setdefault("learning_log", [])
    state.setdefault("telegram_offset", None)
    state.setdefault("start_equity", None)

    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
    except Exception as e:
        print("Telegram error:", e, flush=True)


def get_account_value():
    try:
        return float(trading_client.get_account().equity)
    except Exception as e:
        print("Account error:", e, flush=True)
        return None


def init_start_equity():
    state = load_state()

    if state["start_equity"] is None:
        equity = get_account_value()

        if equity is not None:
            state["start_equity"] = equity
            save_state(state)


def get_positions():
    try:
        return trading_client.get_all_positions()
    except Exception as e:
        print("Position error:", e, flush=True)
        return []


def get_open_symbols():
    positions = get_positions()
    return {p.symbol for p in positions}


def get_bars(symbol, timeframe, days):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        feed=DataFeed.IEX
    )

    df = data_client.get_stock_bars(req).df

    if df.empty:
        return None

    if "symbol" in df.index.names:
        df = df.loc[symbol]

    df = df.copy()
    df.index = pd.to_datetime(df.index)

    return df


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def resample_bars(df, rule):
    if df is None or df.empty:
        return None

    out = pd.DataFrame()
    out["open"] = df["open"].resample(rule).first()
    out["high"] = df["high"].resample(rule).max()
    out["low"] = df["low"].resample(rule).min()
    out["close"] = df["close"].resample(rule).last()
    out["volume"] = df["volume"].resample(rule).sum()
    out = out.dropna()

    if len(out) < 30:
        return None

    return out


def trend_direction(df):
    if df is None or len(df) < 50:
        return "UNKNOWN"

    close = df["close"]
    ema20 = ema(close, 20).iloc[-1]
    ema50 = ema(close, 50).iloc[-1]
    price = close.iloc[-1]

    if price > ema20 and ema20 > ema50:
        return "UP"

    if price < ema20 and ema20 < ema50:
        return "DOWN"

    return "SIDEWAYS"


def volume_ratio(df, lookback=20):
    if df is None or len(df) < lookback + 1:
        return 0

    last_volume = float(df["volume"].iloc[-1])
    avg_volume = float(df["volume"].iloc[-lookback - 1:-1].mean())

    if avg_volume <= 0:
        return 0

    return last_volume / avg_volume


def breakout_signal(df):
    if df is None or len(df) < 25:
        return None

    last_close = float(df["close"].iloc[-1])
    prev_high = float(df["high"].iloc[-21:-1].max())
    prev_low = float(df["low"].iloc[-21:-1].min())
    vol = volume_ratio(df)

    if last_close > prev_high and vol >= 1.3:
        return "LONG"

    if last_close < prev_low and vol >= 1.3:
        return "SHORT"

    return None


def market_filter():
    try:
        spy_daily = get_bars("SPY", TimeFrame.Day, 120)
        qqq_daily = get_bars("QQQ", TimeFrame.Day, 120)

        spy_trend = trend_direction(spy_daily)
        qqq_trend = trend_direction(qqq_daily)

        if spy_trend == "UP" and qqq_trend == "UP":
            return "BULLISH"

        if spy_trend == "DOWN" and qqq_trend == "DOWN":
            return "BEARISH"

        return "MIXED"

    except Exception as e:
        print("Market filter error:", e, flush=True)
        return "UNKNOWN"


def analyze_symbol(symbol, market_status):
    try:
        daily = get_bars(symbol, TimeFrame.Day, 120)
        hourly = get_bars(symbol, TimeFrame.Hour, 30)
        minute = get_bars(symbol, TimeFrame.Minute, 5)

        if daily is None or hourly is None or minute is None:
            print(f"{symbol}: not enough data", flush=True)
            return None

        h4 = resample_bars(hourly, "4h")
        h1 = hourly
        m15 = resample_bars(minute, "15min")
        m5 = resample_bars(minute, "5min")

        d_trend = trend_direction(daily)
        h4_trend = trend_direction(h4)
        h1_trend = trend_direction(h1)

        setup_15m = breakout_signal(m15)
        entry_5m = breakout_signal(m5)

        last_price = float(minute["close"].iloc[-1])
        vol_5m = volume_ratio(m5) if m5 is not None else 0
        vol_15m = volume_ratio(m15) if m15 is not None else 0

        long_score = 0
        short_score = 0

        if market_status == "BULLISH":
            long_score += 1
        if market_status == "BEARISH":
            short_score += 1

        if d_trend == "UP":
            long_score += 2
        if d_trend == "DOWN":
            short_score += 2

        if h4_trend == "UP":
            long_score += 2
        if h4_trend == "DOWN":
            short_score += 2

        if h1_trend == "UP":
            long_score += 1
        if h1_trend == "DOWN":
            short_score += 1

        if setup_15m == "LONG":
            long_score += 2
        if setup_15m == "SHORT":
            short_score += 2

        if entry_5m == "LONG":
            long_score += 2
        if entry_5m == "SHORT":
            short_score += 2

        if vol_5m >= 1.5 or vol_15m >= 1.5:
            long_score += 1
            short_score += 1

        if long_score >= MIN_SCORE_FOR_STOCK and long_score > short_score:
            return {
                "symbol": symbol,
                "side": "LONG",
                "price": last_price,
                "score": long_score,
                "market": market_status,
                "daily": d_trend,
                "h4": h4_trend,
                "h1": h1_trend,
                "m15": setup_15m,
                "m5": entry_5m,
                "vol5": vol_5m,
                "vol15": vol_15m
            }

        if short_score >= MIN_SCORE_FOR_OPTION and short_score > long_score:
            return {
                "symbol": symbol,
                "side": "SHORT",
                "price": last_price,
                "score": short_score,
                "market": market_status,
                "daily": d_trend,
                "h4": h4_trend,
                "h1": h1_trend,
                "m15": setup_15m,
                "m5": entry_5m,
                "vol5": vol_5m,
                "vol15": vol_15m
            }

        print(
            f"{symbol}: no setup | market={market_status} "
            f"24H={d_trend} 4H={h4_trend} 1H={h1_trend} "
            f"15M={setup_15m} 5M={entry_5m} "
            f"L={long_score} S={short_score}",
            flush=True
        )

        return None

    except Exception as e:
        print(f"{symbol} analysis error: {e}", flush=True)
        return None


def stock_trade_already_open(symbol):
    state = load_state()
    return any(t.get("symbol") == symbol and t.get("status") == "OPEN" for t in state["stock_trades"])


def option_trade_already_open(symbol):
    state = load_state()
    return any(t.get("underlying") == symbol and t.get("status") == "OPEN" for t in state["option_trades"])


def place_stock_trade(signal):
    if signal["side"] != "LONG":
        return

    symbol = signal["symbol"]

    if stock_trade_already_open(symbol):
        return

    open_symbols = get_open_symbols()

    if symbol in open_symbols:
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

    state = load_state()
    state["stock_trades"].append({
        "symbol": symbol,
        "side": "LONG",
        "qty": qty,
        "entry_approx": price,
        "tp": tp_price,
        "sl": sl_price,
        "score": signal["score"],
        "features": signal,
        "status": "OPEN",
        "created_at": int(time.time()),
        "order_id": str(submitted.id)
    })

    state["learning_log"].append({
        "type": "STOCK",
        "symbol": symbol,
        "side": "LONG",
        "score": signal["score"],
        "features": signal,
        "result": "OPEN",
        "created_at": int(time.time())
    })

    save_state(state)

    msg = f"""🐃 BLACK BISON STOCK V3

📈 {symbol} LONG

Entry approx: ${price:.2f}
Qty: {qty}

TP: ${tp_price}
SL: ${sl_price}

Score: {signal['score']}/10
Market: {signal['market']}
24H: {signal['daily']}
4H: {signal['h4']}
1H: {signal['h1']}
15M: {signal['m15']}
5M: {signal['m5']}

Status: PAPER STOCK ORDER SENT"""

    print(msg, flush=True)
    send_telegram(msg)


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
    }


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

    if signal["score"] < MIN_SCORE_FOR_OPTION:
        return

    if option_trade_already_open(symbol):
        return

    option_symbol = get_option_contract(symbol, signal["side"], signal["price"])

    if not option_symbol:
        print(f"{symbol}: no option contract found", flush=True)
        return

    order = MarketOrderRequest(
        symbol=option_symbol,
        qty=OPTION_QTY,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )

    submitted = trading_client.submit_order(order_data=order)

    state = load_state()
    state["option_trades"].append({
        "underlying": symbol,
        "option_symbol": option_symbol,
        "side": signal["side"],
        "qty": OPTION_QTY,
        "score": signal["score"],
        "features": signal,
        "status": "OPEN",
        "created_at": int(time.time()),
        "order_id": str(submitted.id)
    })

    state["learning_log"].append({
        "type": "OPTION",
        "symbol": symbol,
        "option_symbol": option_symbol,
        "side": signal["side"],
        "score": signal["score"],
        "features": signal,
        "result": "OPEN",
        "created_at": int(time.time())
    })

    save_state(state)

    option_type = "CALL" if signal["side"] == "LONG" else "PUT"

    msg = f"""🐃 BLACK BISON OPTION V3

🎯 {symbol} {option_type}

Option: {option_symbol}
Qty: {OPTION_QTY}

Score: {signal['score']}/10
Market: {signal['market']}
24H: {signal['daily']}
4H: {signal['h4']}
1H: {signal['h1']}
15M: {signal['m15']}
5M: {signal['m5']}

TP: +{OPTION_TAKE_PROFIT_PERCENT:.0f}%
SL: -{OPTION_STOP_LOSS_PERCENT:.0f}%

Status: PAPER OPTION ORDER SENT"""

    print(msg, flush=True)
    send_telegram(msg)


def monitor_stock_trades():
    state = load_state()
    open_symbols = get_open_symbols()
    changed = False

    for trade in state["stock_trades"]:
        if trade.get("status") != "OPEN":
            continue

        symbol = trade["symbol"]

        if symbol in open_symbols:
            continue

        trade["status"] = "CLOSED"
        trade["closed_at"] = int(time.time())
        trade["note"] = "Closed by Alpaca bracket or manual close. Exact P/L in Alpaca."

        changed = True

        send_telegram(f"🐃 STOCK CLOSED\n{symbol}\nCheck Alpaca Activities for exact fill.")

    if changed:
        save_state(state)


def monitor_options():
    state = load_state()
    positions = get_positions()
    position_map = {p.symbol: p for p in positions}
    changed = False

    for trade in state["option_trades"]:
        if trade.get("status") != "OPEN":
            continue

        option_symbol = trade["option_symbol"]

        if option_symbol not in position_map:
            trade["status"] = "CLOSED"
            trade["closed_at"] = int(time.time())
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
            trade["closed_at"] = int(time.time())
            trade["pnl_percent"] = pnl_percent
            trade["pnl_dollars"] = pnl_dollars

            for item in state["learning_log"]:
                if (
                    item.get("type") == "OPTION" and
                    item.get("option_symbol") == option_symbol and
                    item.get("result") == "OPEN"
                ):
                    item["result"] = result
                    item["pnl_percent"] = pnl_percent
                    item["pnl_dollars"] = pnl_dollars

            changed = True

            emoji = "✅" if result == "TP" else "❌"

            send_telegram(
                f"🐃 OPTION RESULT\n\n{emoji} {trade['underlying']}\n"
                f"{option_symbol}\nResult: {result}\nP/L: {pnl_percent:.2f}%\n${pnl_dollars:.2f}"
            )

        except Exception as e:
            print(f"Option close error {option_symbol}: {e}", flush=True)

    if changed:
        save_state(state)


def scan():
    monitor_stock_trades()
    monitor_options()

    market_status = market_filter()
    print(f"Market status: {market_status}", flush=True)

    for symbol in SYMBOLS:
        try:
            signal = analyze_symbol(symbol, market_status)

            if not signal:
                continue

            place_stock_trade(signal)
            place_option_trade(signal)

            time.sleep(0.25)

        except Exception as e:
            print(f"{symbol} scan error: {e}", flush=True)


def get_stats_text():
    state = load_state()

    equity = get_account_value()
    start_equity = state.get("start_equity")

    stock_trades = state["stock_trades"]
    option_trades = state["option_trades"]

    open_stock = [t for t in stock_trades if t.get("status") == "OPEN"]
    closed_stock = [t for t in stock_trades if t.get("status") != "OPEN"]

    open_options = [t for t in option_trades if t.get("status") == "OPEN"]
    closed_options = [t for t in option_trades if t.get("status") != "OPEN"]

    option_wins = [t for t in closed_options if t.get("status") == "TP"]
    option_losses = [t for t in closed_options if t.get("status") == "SL"]

    option_win_rate = 0
    if len(option_wins) + len(option_losses) > 0:
        option_win_rate = (len(option_wins) / (len(option_wins) + len(option_losses))) * 100

    option_pnl = sum(float(t.get("pnl_dollars", 0)) for t in closed_options)

    total_pnl_text = "N/A"

    if equity is not None and start_equity is not None:
        total_pnl_text = f"${equity - float(start_equity):.2f}"

    return f"""📊 BLACK BISON STOCK V3 STATS

Account Equity: ${equity:.2f}
Start Equity: ${float(start_equity):.2f}
Total Account P/L: {total_pnl_text}

📈 Stocks:
Open: {len(open_stock)}
Closed: {len(closed_stock)}

🎯 Options:
Open: {len(open_options)}
Closed: {len(closed_options)}
✅ Wins: {len(option_wins)}
❌ Losses: {len(option_losses)}
🏆 Option Win Rate: {option_win_rate:.2f}%
💵 Option Realized P/L: ${option_pnl:.2f}

Tracked Symbols: {len(SYMBOLS)}
"""


def get_open_text():
    positions = get_positions()

    if not positions:
        return "No open positions."

    lines = ["📌 OPEN POSITIONS\n"]

    for p in positions[:80]:
        try:
            lines.append(
                f"{p.symbol}: Qty {p.qty}, P/L ${float(p.unrealized_pl):.2f}, "
                f"{float(p.unrealized_plpc) * 100:.2f}%"
            )
        except Exception:
            lines.append(f"{p.symbol}: open")

    return "\n".join(lines)


def get_learn_text():
    state = load_state()
    logs = state["learning_log"]

    closed = [x for x in logs if x.get("result") != "OPEN"]

    if not closed:
        return "🧠 Learning: not enough closed trades yet."

    by_score = {}

    for x in closed:
        score = str(x.get("score", "NA"))
        by_score.setdefault(score, {"wins": 0, "losses": 0, "count": 0, "net": 0})

        by_score[score]["count"] += 1

        pnl = float(x.get("pnl_dollars", 0))
        by_score[score]["net"] += pnl

        if x.get("result") == "TP":
            by_score[score]["wins"] += 1
        elif x.get("result") == "SL":
            by_score[score]["losses"] += 1

    lines = ["🧠 BLACK BISON STOCK V3 LEARNING\n"]

    for score, data in sorted(by_score.items()):
        total = data["wins"] + data["losses"]
        win_rate = (data["wins"] / total) * 100 if total > 0 else 0
        lines.append(
            f"Score {score}: {data['count']} closed | "
            f"W {data['wins']} / L {data['losses']} | "
            f"WR {win_rate:.1f}% | Net ${data['net']:.2f}"
        )

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
                send_telegram("🐃 Black Bison Stock V3 is online.")

            elif text == "/stats":
                send_telegram(get_stats_text())

            elif text == "/open":
                send_telegram(get_open_text())

            elif text == "/learn":
                send_telegram(get_learn_text())

            elif text == "/symbols":
                send_telegram("Tracked symbols:\n" + ", ".join(SYMBOLS))

    except Exception as e:
        print("Telegram command error:", e, flush=True)

    save_state(state)


def main():
    init_start_equity()

    print("BLACK BISON STOCK V3 LIVE", flush=True)
    send_telegram("🐃 Black Bison Stock V3 started. 4h bug fixed. Space sector added.")

    last_scan = 0

    while True:
        handle_telegram_commands()

        if time.time() - last_scan >= SCAN_SECONDS:
            print("Scanning Black Bison Stock V3...", flush=True)
            scan()
            last_scan = time.time()

        time.sleep(2)


if __name__ == "__main__":
    main()
