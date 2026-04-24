import os
import re
import json
import threading
import requests
import time
from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import *
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

API_KEY        = os.environ.get("BINANCE_API_KEY")
API_SECRET     = os.environ.get("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
RISK_PERCENT   = float(os.environ.get("RISK_PERCENT", "10"))
WEBHOOK_URL    = os.environ.get("WEBHOOK_URL", "http://127.0.0.1:5000/webhook")

client = Client(API_KEY, API_SECRET)
client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

# ─── Utilidades de precisión ───────────────────────────────────────────────────

def get_symbol_info(symbol):
    """Trae tick size y step size del símbolo"""
    info = client.futures_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            tick_size = None
            step_size = None
            for f in s["filters"]:
                if f["filterType"] == "PRICE_FILTER":
                    tick_size = float(f["tickSize"])
                if f["filterType"] == "LOT_SIZE":
                    step_size = float(f["stepSize"])
            return tick_size, step_size
    return 0.01, 0.001

def round_to_tick(price, tick_size):
    """Redondea precio al tick size correcto"""
    decimals = len(str(tick_size).rstrip("0").split(".")[-1])
    return round(round(price / tick_size) * tick_size, decimals)

def round_to_step(qty, step_size):
    """Redondea cantidad al step size correcto"""
    decimals = len(str(step_size).rstrip("0").split(".")[-1])
    return round(round(qty / step_size) * step_size, decimals)

# ─── Ejecución de órdenes ──────────────────────────────────────────────────────

def execute_trade(symbol, side, sl_price, tp_price):
    """
    Ejecuta orden de mercado con SL y TP.
    Usa el nuevo algoOrder endpoint para SL/TP.
    """
    tick_size, step_size = get_symbol_info(symbol)

    # Balance disponible
    account = client.futures_account()
    balance = float(next(
        a["availableBalance"]
        for a in account["assets"]
        if a["asset"] == "USDT"
    ))

    # Precio actual
    mark        = client.futures_mark_price(symbol=symbol)
    entry_price = float(mark["markPrice"])

    # Calcular quantity con risk management
    sl_pct      = abs(entry_price - sl_price) / entry_price * 100
    risk_amount = balance * (RISK_PERCENT / 100)
    quantity    = risk_amount / (entry_price * sl_pct / 100)
    quantity    = round_to_step(max(quantity, step_size), step_size)

    # Ajustar precios al tick size
    sl_price = round_to_tick(sl_price, tick_size)
    tp_price = round_to_tick(tp_price, tick_size)

    binance_side = SIDE_BUY if side == "BUY" else SIDE_SELL
    close_side   = SIDE_SELL if side == "BUY" else SIDE_BUY

    # Orden de mercado principal
    order = client.futures_create_order(
        symbol   = symbol,
        side     = binance_side,
        type     = ORDER_TYPE_MARKET,
        quantity = quantity
    )

    # Stop Loss — nuevo endpoint algoOrder
    client.futures_create_order(
        symbol        = symbol,
        side          = close_side,
        type          = "STOP_MARKET",
        stopPrice     = sl_price,
        closePosition = "true",
        workingType   = "MARK_PRICE"
    )

    # Take Profit — nuevo endpoint algoOrder
    client.futures_create_order(
        symbol        = symbol,
        side          = close_side,
        type          = "TAKE_PROFIT_MARKET",
        stopPrice     = tp_price,
        closePosition = "true",
        workingType   = "MARK_PRICE"
    )

    print(f"✅ {side} {quantity} {symbol} | Balance: {balance:.2f} | SL: {sl_price} | TP: {tp_price}")

    return {
        "status"      : "ok",
        "order_id"    : order["orderId"],
        "side"        : side,
        "quantity"    : quantity,
        "balance_usdt": round(balance, 2),
        "entry"       : entry_price,
        "sl"          : sl_price,
        "tp"          : tp_price
    }

# ─── Rutas Flask ───────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "bot activo"}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True)
        if not data:
            raw  = request.data.decode("utf-8").strip()
            data = json.loads(raw)

        # Verificar token
        if data.get("token", "") != WEBHOOK_SECRET:
            return jsonify({"error": "token invalido"}), 403

        symbol  = data.get("symbol", "BTCUSDT")
        message = data.get("message", "")
        print(f"📩 Señal recibida para {symbol}: {message}")

        # Parsear mensaje del indicador
        if "LONG ENTRY" in message:
            side = "BUY"
        elif "SHORT ENTRY" in message:
            side = "SELL"
        else:
            return jsonify({"status": "ignorado", "reason": "no es señal de entrada"}), 200

        sl_match = re.search(r"SL:\s*([\d.]+)", message)
        tp_match = re.search(r"TP:\s*([\d.]+)", message)

        mark        = client.futures_mark_price(symbol=symbol)
        entry_price = float(mark["markPrice"])

        if sl_match and tp_match:
            sl_price = float(sl_match.group(1))
            tp_price = float(tp_match.group(1))
        else:
            # Fallback: calcular SL/TP si no vienen en el mensaje
            sl_pct   = 1.5
            tp_pct   = 4.5
            sl_price = entry_price * (1 - sl_pct/100) if side == "BUY" else entry_price * (1 + sl_pct/100)
            tp_price = entry_price * (1 + tp_pct/100) if side == "BUY" else entry_price * (1 - tp_pct/100)

        result = execute_trade(symbol, side, sl_price, tp_price)
        return jsonify(result), 200

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({"error": str(e)}), 500

# ─── Signal Engine ─────────────────────────────────────────────────────────────

def get_klines(symbol, interval, limit=100):
    raw    = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    opens  = [float(k[1]) for k in raw]
    highs  = [float(k[2]) for k in raw]
    lows   = [float(k[3]) for k in raw]
    closes = [float(k[4]) for k in raw]
    return opens, highs, lows, closes

def pivot_high(highs, left, right):
    results = [None] * len(highs)
    for i in range(left, len(highs) - right):
        if all(highs[i] >= highs[i-j] for j in range(1, left+1)) and \
           all(highs[i] >= highs[i+j] for j in range(1, right+1)):
            results[i] = highs[i]
    return results

def pivot_low(lows, left, right):
    results = [None] * len(lows)
    for i in range(left, len(lows) - right):
        if all(lows[i] <= lows[i-j] for j in range(1, left+1)) and \
           all(lows[i] <= lows[i+j] for j in range(1, right+1)):
            results[i] = lows[i]
    return results

def detect_htf_bos(symbol, htf_interval="1h", swing_len=5):
    opens, highs, lows, closes = get_klines(symbol, htf_interval, limit=100)
    ph = pivot_high(highs, swing_len, swing_len)
    pl = pivot_low(lows,   swing_len, swing_len)

    last_hh = next((v for v in reversed(ph) if v is not None), None)
    last_ll = next((v for v in reversed(pl) if v is not None), None)

    if last_hh is None or last_ll is None:
        return 0, None, None

    current_close = closes[-1]
    if current_close > last_hh:
        return 1,  last_hh, last_ll
    if current_close < last_ll:
        return -1, last_hh, last_ll
    return 0, last_hh, last_ll

def detect_ltf_entry(symbol, bos_dir, channel_top, channel_bot, ltf_interval="5m", swing_len=3):
    opens, highs, lows, closes = get_klines(symbol, ltf_interval, limit=100)

    channel_range = channel_top - channel_bot
    current_pos   = (closes[-1] - channel_bot) / channel_range * 100
    in_fifty_zone = 45 <= current_pos <= 55

    if not in_fifty_zone:
        return None

    ph = pivot_high(highs, swing_len, swing_len)
    pl = pivot_low(lows,   swing_len, swing_len)

    last_internal_high = next((v for v in reversed(ph) if v is not None), None)
    last_internal_low  = next((v for v in reversed(pl) if v is not None), None)

    if last_internal_high is None or last_internal_low is None:
        return None

    if bos_dir == 1:
        c1 = closes[-3] > opens[-3]
        c2 = closes[-2] > opens[-2]
        c3 = closes[-1] > opens[-1]
        prog = closes[-2] > closes[-3] and closes[-1] > closes[-2]
        if c1 and c2 and c3 and prog and closes[-1] > last_internal_high:
            return {
                "side" : "BUY",
                "entry": closes[-1],
                "sl"   : round(last_internal_low  * 0.999, 2),
                "tp"   : round(channel_top         * 0.999, 2)
            }

    if bos_dir == -1:
        c1 = closes[-3] < opens[-3]
        c2 = closes[-2] < opens[-2]
        c3 = closes[-1] < opens[-1]
        prog = closes[-2] < closes[-3] and closes[-1] < closes[-2]
        if c1 and c2 and c3 and prog and closes[-1] < last_internal_low:
            return {
                "side" : "SELL",
                "entry": closes[-1],
                "sl"   : round(last_internal_high * 1.001, 2),
                "tp"   : round(channel_bot         * 1.001, 2)
            }

    return None

def send_signal(symbol, signal):
    msg = (
        f"{'▲ LONG' if signal['side'] == 'BUY' else '▼ SHORT'} ENTRY R1 "
        f"[Smart Liquidity V3]\nEntry: {signal['entry']} | "
        f"SL: {signal['sl']} | TP: {signal['tp']}"
    )
    payload = {
        "token"  : WEBHOOK_SECRET,
        "symbol" : symbol,
        "message": msg
    }
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"📤 Señal enviada: {r.json()}")
    except Exception as e:
        print(f"❌ Error enviando señal: {e}")

def run_engine(symbols=["BTCUSDT", "ETHUSDT"], interval_seconds=300):
    print(f"🚀 Motor de señales iniciado. Chequeando cada {interval_seconds}s...")
    while True:
        for symbol in symbols:
            try:
                print(f"\n📊 Analizando {symbol}...")
                bos_dir, channel_top, channel_bot = detect_htf_bos(symbol, "1h", swing_len=5)

                if bos_dir == 0:
                    print(f"  Sin BOS en HTF para {symbol}")
                    continue

                direction = "BULLISH" if bos_dir == 1 else "BEARISH"
                print(f"  HTF BOS {direction} | Canal: {channel_bot:.2f} — {channel_top:.2f}")

                signal = detect_ltf_entry(symbol, bos_dir, channel_top, channel_bot, "5m", swing_len=3)
                if signal:
                    print(f"  🎯 Señal {signal['side']} | SL: {signal['sl']} | TP: {signal['tp']}")
                    send_signal(symbol, signal)
                else:
                    print(f"  Sin señal de entrada en LTF")

            except Exception as e:
                print(f"  ❌ Error en {symbol}: {e}")

        time.sleep(interval_seconds)

# ─── Arranque ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    engine_thread = threading.Thread(target=run_engine, daemon=True)
    engine_thread.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)