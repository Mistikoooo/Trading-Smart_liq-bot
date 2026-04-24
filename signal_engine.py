import os
import time
import json
import requests
from binance.client import Client
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")

client = Client(API_KEY, API_SECRET)
client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

WEBHOOK_URL    = os.environ.get("WEBHOOK_URL", "http://127.0.0.1:5000/webhook")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

def get_klines(symbol, interval, limit=100):
    """Trae velas y las convierte a listas de floats"""
    raw = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    opens  = [float(k[1]) for k in raw]
    highs  = [float(k[2]) for k in raw]
    lows   = [float(k[3]) for k in raw]
    closes = [float(k[4]) for k in raw]
    return opens, highs, lows, closes

def pivot_high(highs, left, right):
    """Detecta pivot highs — equivalente a ta.pivothigh en Pine"""
    results = [None] * len(highs)
    for i in range(left, len(highs) - right):
        is_pivot = all(highs[i] >= highs[i-j] for j in range(1, left+1)) and \
                   all(highs[i] >= highs[i+j] for j in range(1, right+1))
        if is_pivot:
            results[i] = highs[i]
    return results

def pivot_low(lows, left, right):
    """Detecta pivot lows — equivalente a ta.pivotlow en Pine"""
    results = [None] * len(lows)
    for i in range(left, len(lows) - right):
        is_pivot = all(lows[i] <= lows[i-j] for j in range(1, left+1)) and \
                   all(lows[i] <= lows[i+j] for j in range(1, right+1))
        if is_pivot:
            results[i] = lows[i]
    return results

def sma(values, length):
    """Media móvil simple"""
    results = [None] * len(values)
    for i in range(length - 1, len(values)):
        results[i] = sum(values[i-length+1:i+1]) / length
    return results

def detect_htf_bos(symbol, htf_interval="1h", swing_len=5):
    """
    Detecta BOS en el HTF.
    Retorna: 1 (bullish BOS), -1 (bearish BOS), 0 (nada)
    """
    opens, highs, lows, closes = get_klines(symbol, htf_interval, limit=100)

    ph = pivot_high(highs, swing_len, swing_len)
    pl = pivot_low(lows, swing_len, swing_len)

    # Encontrar ultimo HH y LL
    last_hh = None
    last_ll = None
    for i in range(len(ph)-1, -1, -1):
        if ph[i] is not None and last_hh is None:
            last_hh = ph[i]
        if pl[i] is not None and last_ll is None:
            last_ll = pl[i]
        if last_hh and last_ll:
            break

    if last_hh is None or last_ll is None:
        return 0, None, None

    current_close = closes[-1]

    # Bullish BOS: precio cierra por encima del ultimo HH
    if current_close > last_hh:
        return 1, last_hh, last_ll

    # Bearish BOS: precio cierra por debajo del ultimo LL
    if current_close < last_ll:
        return -1, last_hh, last_ll

    return 0, last_hh, last_ll

def detect_ltf_entry(symbol, bos_dir, channel_top, channel_bot, 
                      ltf_interval="5m", swing_len=3):
    """
    Detecta señal de entrada en LTF cuando:
    1. Precio está en zona del 50% del canal HTF
    2. Se forma MSS en LTF
    3. Precio retrocede al 50% del MSS
    4. Se forma OB en LTF
    5. Precio entra al OB → ENTRADA
    """
    opens, highs, lows, closes = get_klines(symbol, ltf_interval, limit=100)

    channel_mid = channel_bot + (channel_top - channel_bot) * 0.5

    # Verificar si precio está en zona del 50%
    tolerance = 0.05  # 5% de tolerancia
    channel_range = channel_top - channel_bot
    current_pos = (closes[-1] - channel_bot) / channel_range * 100
    in_fifty_zone = (50 - tolerance*100) <= current_pos <= (50 + tolerance*100)

    if not in_fifty_zone:
        return None

    # Detectar MSS en LTF
    ph = pivot_high(highs, swing_len, swing_len)
    pl = pivot_low(lows, swing_len, swing_len)

    last_internal_high = None
    last_internal_low  = None

    for i in range(len(ph)-1, -1, -1):
        if ph[i] is not None and last_internal_high is None:
            last_internal_high = ph[i]
        if pl[i] is not None and last_internal_low is None:
            last_internal_low = pl[i]
        if last_internal_high and last_internal_low:
            break

    if last_internal_high is None or last_internal_low is None:
        return None

    # Calcular bodies para displacement
    bodies = [abs(closes[i] - opens[i]) for i in range(len(closes))]
    mean_bodies = sma(bodies, swing_len)

    # Bullish MSS: 3 velas alcistas consecutivas que rompen internal high
    if bos_dir == 1:
        c1 = closes[-3] > opens[-3]
        c2 = closes[-2] > opens[-2]
        c3 = closes[-1] > opens[-1]
        progression = closes[-2] > closes[-3] and closes[-1] > closes[-2]
        mss_bull = c1 and c2 and c3 and progression and closes[-1] > last_internal_high

        if mss_bull:
            fifty_level = last_internal_low + (last_internal_high - last_internal_low) * 0.5
            sl_price = last_internal_low
            tp_price = channel_top
            entry_price = closes[-1]

            print(f"🟢 SEÑAL LONG detectada | Entry: {entry_price} | SL: {sl_price} | TP: {tp_price}")
            return {
                "side": "BUY",
                "entry": entry_price,
                "sl": round(sl_price, 2),
                "tp": round(tp_price, 2)
            }

    # Bearish MSS
    if bos_dir == -1:
        c1 = closes[-3] < opens[-3]
        c2 = closes[-2] < opens[-2]
        c3 = closes[-1] < opens[-1]
        progression = closes[-2] < closes[-3] and closes[-1] < closes[-2]
        mss_bear = c1 and c2 and c3 and progression and closes[-1] < last_internal_low

        if mss_bear:
            sl_price = last_internal_high
            tp_price = channel_bot
            entry_price = closes[-1]

            print(f"🔴 SEÑAL SHORT detectada | Entry: {entry_price} | SL: {sl_price} | TP: {tp_price}")
            return {
                "side": "SELL",
                "entry": entry_price,
                "sl": round(sl_price, 2),
                "tp": round(tp_price, 2)
            }

    return None

def send_signal(symbol, signal):
    """Manda la señal al webhook del bot"""
    payload = {
        "token": WEBHOOK_SECRET,
        "symbol": symbol,
        "message": f"{'▲ LONG' if signal['side'] == 'BUY' else '▼ SHORT'} ENTRY R1 [Smart Liquidity V3]\nEntry: {signal['entry']} | SL: {signal['sl']} | TP: {signal['tp']}"
    }
    try:
        r = requests.post(WEBHOOK_URL, json=payload)
        print(f"Respuesta del bot: {r.json()}")
    except Exception as e:
        print(f"Error enviando señal: {e}")

def run(symbols=["BTCUSDT", "ETHUSDT"], interval_seconds=60):
    """Loop principal — corre cada X segundos"""
    print(f"🚀 Motor de señales iniciado. Chequeando cada {interval_seconds} segundos...")
    
    while True:
        for symbol in symbols:
            try:
                print(f"\n📊 Analizando {symbol}...")

                # Detectar BOS en HTF (1H)
                bos_dir, channel_top, channel_bot = detect_htf_bos(symbol, "1h", swing_len=5)

                if bos_dir == 0:
                    print(f"  Sin BOS en HTF para {symbol}")
                    continue

                direction = "BULLISH" if bos_dir == 1 else "BEARISH"
                print(f"  HTF BOS {direction} | Canal: {channel_bot:.2f} - {channel_top:.2f}")

                # Detectar entrada en LTF (5M)
                signal = detect_ltf_entry(symbol, bos_dir, channel_top, channel_bot, "5m", swing_len=3)

                if signal:
                    send_signal(symbol, signal)

            except Exception as e:
                print(f"Error en {symbol}: {e}")

        time.sleep(interval_seconds)

if __name__ == "__main__":
    run()