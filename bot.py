import os
import time
import requests
import pandas as pd
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv
from binance.client import Client
from requests.exceptions import RequestException

# =========================================================
# CONFIGURATION & STRATEGY PARAMETERS
# =========================================================

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID", ""))

TESTNET = True
SYMBOL = "TRXUSDT"
INTERVAL = Client.KLINE_INTERVAL_1MINUTE

TRADE_AMOUNT_USDT = 10

# Risk Management Parameters (Tighter Targets)
TAKE_PROFIT = 0.015       # 1.5% Target
STOP_LOSS = 0.008         # 0.8% Stop Loss (Tighter to prevent large drawdown)
TRAILING_STOP = 0.005     # 0.5% Trailing Stop

# Indicator Settings (Smoothed & Stable)
RSI_PERIOD = 14           # Standard 14-period RSI (Replaced volatile period 2)
MA_FAST_PERIOD = 20
MA_SLOW_PERIOD = 50       # 50 MA for trend direction filter
COOLDOWN_CANDLES = 3
RSI_BROADCAST_INTERVAL = 300

RENDER_APP_URL = "https://pinpbot.onrender.com"

# =========================================================
# HEALTH CHECK & KEEP-ALIVE THREADS
# =========================================================

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return

def start_health_check_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def keep_alive_ping():
    time.sleep(20)
    while True:
        try:
            res = requests.get(RENDER_APP_URL, timeout=10)
            print(f"Self-ping status: {res.status_code}")
        except Exception as e:
            print(f"Self-ping error: {e}")
        time.sleep(600)

threading.Thread(target=start_health_check_server, daemon=True).start()
threading.Thread(target=keep_alive_ping, daemon=True).start()

# =========================================================
# INITIALIZE BINANCE
# =========================================================

if not API_KEY or not API_SECRET or not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("Missing API credentials in environment variables.")

def init_binance_client():
    while True:
        try:
            c = Client(API_KEY, API_SECRET, testnet=TESTNET)
            c.TIME_SYNC_INTERVAL = 60
            c.ping()
            print("Binance connection: OK")
            return c
        except Exception as e:
            print(f"Binance connection failed ({e}). Retrying in 10s...")
            time.sleep(10)

client = init_binance_client()

# =========================================================
# STATE VARIABLES
# =========================================================

bot_running = True
last_update_id = None
last_candle_time = None
last_rsi_broadcast = 0

in_position = False
entry_price = 0.0
quantity = 0.0
highest_price = 0.0

total_profit = 0.0
total_trades = 0
wins = 0
losses = 0
cooldown_counter = 0

# =========================================================
# HELPER FUNCTIONS & TELEGRAM
# =========================================================

def safe_request(method, url, retries=3, backoff_factor=2, **kwargs):
    for attempt in range(retries):
        try:
            return requests.request(method, url, **kwargs)
        except RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(backoff_factor ** attempt)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    response = safe_request("POST", url, data=payload, timeout=15, retries=3)
    return True if response and response.ok else False

def check_telegram():
    global last_update_id, bot_running

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {}
    if last_update_id is not None:
        params["offset"] = last_update_id + 1

    response = safe_request("GET", url, params=params, timeout=25, retries=2)
    if not response or not response.ok:
        return

    try:
        data = response.json()
        if not data.get("ok"):
            return

        for update in data.get("result", []):
            last_update_id = update["update_id"]
            message = update.get("message")
            if not message:
                continue

            user_chat_id = str(message.get("chat", {}).get("id", ""))
            if user_chat_id != CHAT_ID:
                continue

            text = message.get("text", "").strip().lower()

            if text == "/start":
                bot_running = True
                send_telegram("🟢 BOT STARTED\n\nTrading is enabled with RSI 14 & Trend Filters.")
            elif text == "/stop":
                bot_running = False
                send_telegram("🛑 BOT STOPPED\n\nNew trades disabled. Open positions monitored.")
            elif text == "/status":
                send_status()
            elif text == "/balance":
                send_balance()
            elif text == "/help":
                send_telegram(
                    "🤖 Binance Bot Commands\n\n"
                    "/start - Start trading\n"
                    "/stop - Stop new trades\n"
                    "/status - Bot status & RSI\n"
                    "/balance - Wallet balance\n"
                    "/help - Command list"
                )
    except Exception as e:
        print("Telegram error:", e)

def get_balances():
    try:
        account = client.get_account()
        balances = {}
        for item in account.get("balances", []):
            free = float(item["free"])
            locked = float(item["locked"])
            if free > 0 or locked > 0:
                balances[item["asset"].upper()] = {"free": free, "locked": locked}
        return balances
    except Exception as e:
        print("Error fetching balance:", e)
        return {}

def send_balance():
    try:
        balances = get_balances()
        usdt = balances.get("USDT", {"free": 0.0, "locked": 0.0})
        trx = balances.get("TRX", {"free": 0.0, "locked": 0.0})

        ticker = client.get_symbol_ticker(symbol=SYMBOL)
        price = float(ticker["price"])
        trx_value = trx["free"] * price
        total_usdt = usdt["free"] + trx_value

        message = (
            "💰 BINANCE BALANCE\n\n"
            f"USDT Free: {usdt['free']:.4f}\n"
            f"TRX Free: {trx['free']:.4f}\n"
            f"TRX Price: {price:.6f} USDT\n"
            f"Total Value: {total_usdt:.4f} USDT"
        )
        send_telegram(message)
    except Exception as e:
        send_telegram(f"❌ Balance error:\n{e}")

def send_status():
    try:
        status = "🟢 RUNNING" if bot_running else "🔴 STOPPED"
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        df = get_data()
        current_rsi = "N/A"
        current_price = "N/A"

        if df is not None and len(df) >= MA_SLOW_PERIOD + 5:
            df = calculate_indicators(df)
            current_rsi = f"{df.iloc[-2]['rsi']:.2f}"
            current_price = f"{df.iloc[-2]['close']:.6f}"

        message = (
            "📊 BOT STATUS\n\n"
            f"Status: {status}\n"
            f"Symbol: {SYMBOL}\n"
            f"Mode: {'TESTNET' if TESTNET else 'LIVE'}\n"
            f"Price: {current_price}\n"
            f"RSI (14): {current_rsi}\n\n"
            f"In Position: {'YES' if in_position else 'NO'}\n"
        )

        if in_position:
            price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
            unrealized = (price - entry_price) * quantity
            message += (
                f"Entry: {entry_price:.6f}\n"
                f"Current: {price:.6f}\n"
                f"Unrealized P/L: {unrealized:.4f} USDT\n\n"
            )

        message += (
            f"Realized Profit: {total_profit:.4f} USDT\n"
            f"Trades: {total_trades}\n"
            f"Wins: {wins} | Losses: {losses}\n"
            f"Win Rate: {win_rate:.2f}%"
        )
        send_telegram(message)
    except Exception as e:
        send_telegram(f"❌ Status error:\n{e}")

# =========================================================
# DATA & INDICATORS WITH MULTI-FILTERS
# =========================================================

def get_data():
    for attempt in range(3):
        try:
            klines = client.get_klines(symbol=SYMBOL, interval=INTERVAL, limit=120)
            df = pd.DataFrame(
                klines,
                columns=[
                    "time", "open", "high", "low", "close", "volume",
                    "close_time", "quote_volume", "trades",
                    "base_volume", "quote_volume2", "ignore"
                ]
            )
            df["close"] = df["close"].astype(float)
            df["volume"] = df["volume"].astype(float)
            return df
        except Exception:
            if attempt < 2:
                time.sleep(2)
            else:
                return None

def calculate_indicators(df):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/RSI_PERIOD, adjust=False).mean()

    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))
    df["ma_fast"] = df["close"].rolling(MA_FAST_PERIOD).mean()
    df["ma_slow"] = df["close"].rolling(MA_SLOW_PERIOD).mean()
    df["vol_ma"] = df["volume"].rolling(20).mean()
    return df

def get_quantity():
    try:
        price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
        raw_quantity = TRADE_AMOUNT_USDT / price

        symbol_info = client.get_symbol_info(SYMBOL)
        if not symbol_info:
            return 0

        lot_filter = next((f for f in symbol_info["filters"] if f["filterType"] == "LOT_SIZE"), None)
        if not lot_filter:
            return 0

        step_size = float(lot_filter["stepSize"])
        min_qty = float(lot_filter["minQty"])
        step_str = str(lot_filter["stepSize"]).rstrip('0')
        precision = len(step_str.split('.')[1]) if '.' in step_str else 0

        quantity = round(raw_quantity - (raw_quantity % step_size), precision)
        return quantity if quantity >= min_qty else 0
    except Exception as e:
        print("Quantity error:", e)
        return 0

def get_average_fill_price(order):
    fills = order.get("fills", [])
    if fills:
        total_qty = sum(float(f["qty"]) for f in fills)
        total_val = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        if total_qty > 0:
            return total_val / total_qty

    executed_qty = float(order.get("executedQty", 0))
    quote_qty = float(order.get("cummulativeQuoteQty", 0))
    return (quote_qty / executed_qty) if executed_qty > 0 else 0

def execute_buy():
    global in_position, entry_price, quantity, highest_price

    try:
        quantity_to_buy = get_quantity()
        if quantity_to_buy <= 0:
            send_telegram("❌ BUY cancelled.\nInsufficient balance or size below minimum.")
            return

        order = client.order_market_buy(
            symbol=SYMBOL,
            quantity=quantity_to_buy,
            recvWindow=10000
        )

        actual_price = get_average_fill_price(order)
        executed_quantity = float(order.get("executedQty", quantity_to_buy))

        if actual_price <= 0:
            return

        entry_price = actual_price
        quantity = executed_quantity
        highest_price = actual_price
        in_position = True

        send_telegram(
            "🟢 BUY EXECUTED\n\n"
            f"Symbol: {SYMBOL}\n"
            f"Price: {actual_price:.6f}\n"
            f"Quantity: {executed_quantity:.4f}\n\n"
            f"TP (1.5%): {actual_price * (1 + TAKE_PROFIT):.6f}\n"
            f"SL (0.8%): {actual_price * (1 - STOP_LOSS):.6f}"
        )
    except Exception as e:
        send_telegram(f"❌ BUY failed:\n{e}")

def execute_sell(reason):
    global in_position, entry_price, quantity, highest_price
    global total_profit, total_trades, wins, losses, cooldown_counter

    try:
        if quantity <= 0:
            return

        order = client.order_market_sell(
            symbol=SYMBOL,
            quantity=quantity,
            recvWindow=10000
        )

        sell_price = get_average_fill_price(order)
        executed_quantity = float(order.get("executedQty", quantity))

        if sell_price <= 0:
            return

        profit = (sell_price - entry_price) * executed_quantity
        total_profit += profit
        total_trades += 1

        if profit > 0:
            wins += 1
        else:
            losses += 1

        emoji = "🟢" if profit >= 0 else "🔴"
        send_telegram(
            f"{emoji} SELL EXECUTED\n\n"
            f"Reason: {reason}\n"
            f"Price: {sell_price:.6f}\n"
            f"Quantity: {executed_quantity:.4f}\n\n"
            f"Trade P/L: {profit:.4f} USDT\n"
            f"Total Profit: {total_profit:.4f} USDT"
        )

        in_position = False
        entry_price = 0
        quantity = 0
        highest_price = 0
        cooldown_counter = COOLDOWN_CANDLES
    except Exception as e:
        send_telegram(f"❌ SELL error:\n{e}")

def manage_position(price):
    global highest_price
    if not in_position:
        return

    if price > highest_price:
        highest_price = price

    if price >= entry_price * (1 + TAKE_PROFIT):
        execute_sell("TAKE PROFIT (1.5%)")
        return

    if price <= entry_price * (1 - STOP_LOSS):
        execute_sell("STOP LOSS (0.8%)")
        return

    if price <= highest_price * (1 - TRAILING_STOP):
        execute_sell("TRAILING STOP")

def trade_logic():
    global last_candle_time, cooldown_counter

    df = get_data()
    if df is None or len(df) < MA_SLOW_PERIOD + 5:
        return

    closed_candle = df.iloc[-2]
    candle_time = closed_candle["time"]

    try:
        live_price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
        manage_position(live_price)
    except Exception as e:
        print("Position monitoring ticker error:", e)

    if last_candle_time == candle_time:
        return

    last_candle_time = candle_time
    df = calculate_indicators(df)

    prev_rsi = float(df.iloc[-3]["rsi"])
    current_rsi = float(df.iloc[-2]["rsi"])
    price = float(df.iloc[-2]["close"])
    ma_fast = float(df.iloc[-2]["ma_fast"])
    ma_slow = float(df.iloc[-2]["ma_slow"])
    volume = float(df.iloc[-2]["volume"])
    vol_ma = float(df.iloc[-2]["vol_ma"])

    if pd.isna(prev_rsi) or pd.isna(current_rsi) or pd.isna(ma_slow):
        return

    print(f"Candle Closed | RSI(14): {current_rsi:.2f} | Price: {price:.6f} | MA50: {ma_slow:.6f}")

    if cooldown_counter > 0:
        cooldown_counter -= 1
        return

    if in_position:
        return

    # Filter Conditions:
    # 1. RSI crosses above 30 out of oversold area
    # 2. Price is above the 50 MA (Overall Upward Trend)
    # 3. Volume is above 20-period Average Volume (Buying Volume Support)
    rsi_signal = prev_rsi < 30 and current_rsi >= 30
    trend_filter = price > ma_slow and price > ma_fast
    volume_filter = volume >= vol_ma

    if rsi_signal and trend_filter and volume_filter:
        send_telegram(
            "📈 VALIDATED BUY SIGNAL\n\n"
            f"RSI(14): {current_rsi:.2f}\n"
            f"Price: {price:.6f}\n"
            f"MA 50: {ma_slow:.6f}\n"
            "Volume: Supported"
        )
        execute_buy()

# =========================================================
# MAIN ENTRY POINT
# =========================================================

def telegram_listener():
    while True:
        try:
            check_telegram()
            time.sleep(1)
        except Exception as e:
            print("Telegram listener error:", e)
            time.sleep(3)

def run_bot():
    global last_rsi_broadcast
    last_rsi_broadcast = time.time()

    print("================================")
    print(" Binance Trading Bot Active")
    print("================================")

    send_telegram(
        "🤖 Binance Bot Online\n\n"
        f"Mode: {'TESTNET' if TESTNET else 'LIVE'}\n"
        f"Symbol: {SYMBOL}\n"
        "Strategy: RSI (14) + Trend (MA50) + Volume Filter\n"
        "TP: 1.5% | SL: 0.8%\n\n"
        "Send /status to view parameters."
    )

    threading.Thread(target=telegram_listener, daemon=True).start()

    while True:
        try:
            if in_position:
                try:
                    price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
                    manage_position(price)
                except Exception as e:
                    print("Position ticker error:", e)

            if bot_running:
                trade_logic()

            time.sleep(5)

        except KeyboardInterrupt:
            send_telegram("🛑 Bot manually stopped.")
            break
        except Exception as e:
            print("MAIN LOOP ERROR:", e)
            time.sleep(5)

if __name__ == "__main__":
    run_bot()