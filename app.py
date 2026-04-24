import os
import json
from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import *
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "mi_clave_secreta")

client = Client(API_KEY, API_SECRET)
client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "bot activo"}), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True)
        if not data:
            raw = request.data.decode("utf-8").strip()
            data = json.loads(raw)

        token = data.get("token", "")
        if token != WEBHOOK_SECRET:
            return jsonify({"error": "token invalido"}), 403

        symbol   = data.get("symbol", "BTCUSDT")
        side     = data.get("side", "BUY").upper()
        quantity = float(data.get("quantity", 0.001))
        sl_pct   = float(data.get("sl_pct", 1.5))
        tp_pct   = float(data.get("tp_pct", 3.0))

        order = client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY if side == "BUY" else SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=quantity
        )

        mark = client.futures_mark_price(symbol=symbol)
        entry_price = float(mark["markPrice"])

        if side == "BUY":
            sl_price = round(entry_price * (1 - sl_pct / 100), 2)
            tp_price = round(entry_price * (1 + tp_pct / 100), 2)
            sl_side  = SIDE_SELL
            tp_side  = SIDE_SELL
        else:
            sl_price = round(entry_price * (1 + sl_pct / 100), 2)
            tp_price = round(entry_price * (1 - tp_pct / 100), 2)
            sl_side  = SIDE_BUY
            tp_side  = SIDE_BUY

        client.futures_create_order(
            symbol=symbol,
            side=sl_side,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=sl_price,
            closePosition=True
        )

        client.futures_create_order(
            symbol=symbol,
            side=tp_side,
            type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
            stopPrice=tp_price,
            closePosition=True
        )

        print(f"Orden: {side} {quantity} {symbol} | SL: {sl_price} | TP: {tp_price}")

        return jsonify({
            "status": "ok",
            "order_id": order["orderId"],
            "sl": sl_price,
            "tp": tp_price
        }), 200

    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)