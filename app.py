import os
import re
import json
import threading
import requests
import time
import numpy as np
import pandas as pd
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

# ─── Estado global por símbolo ─────────────────────────────────────────────────
# Replica las variables `var` del Pine Script
state = {}

def get_state(symbol):
    if symbol not in state:
        state[symbol] = {
            # HTF
            "htf_bos_dir"          : 0,
            "htf_bos_valid"        : False,
            "htf_bos_level"        : None,
            "htf_channel_top"      : None,
            "htf_channel_bot"      : None,
            "htf_channel_mid"      : None,
            "htf_channel_active"   : False,
            "htf_channel_built"    : False,
            "htf_ob_high"          : None,
            "htf_ob_low"           : None,
            "htf_ref_hl"           : None,
            "htf_ref_lh"           : None,
            "waiting_hl"           : False,
            "waiting_lh"           : False,
            "waiting_hh_confirm"   : False,
            "waiting_ll_confirm"   : False,
            "confirmed_hh"         : None,
            "confirmed_ll"         : None,
            "last_bull_bos_level"  : None,
            "last_bear_bos_level"  : None,
            # LTF
            "ltf_state"            : 0,
            "ltf_internal_high"    : None,
            "ltf_internal_low"     : None,
            "ltf_mss_level"        : None,
            "ltf_mss_high"         : None,
            "ltf_mss_low"          : None,
            "ltf_fifty_level"      : None,
            "ltf_fifty_hit"        : False,
            "ltf_ob_high"          : None,
            "ltf_ob_low"           : None,
            "ltf_ob_confirmed"     : False,
            "ltf_trade_active"     : False,
            "ltf_entry_price"      : None,
            "ltf_stop_loss"        : None,
            "ltf_take_profit"      : None,
            "ltf_reentry_count"    : 0,
            "trade_state"          : "Neutral",
        }
    return state[symbol]

# ─── Utilidades de datos ───────────────────────────────────────────────────────

def get_klines(symbol, interval, limit=200):
    """Trae velas y las devuelve como DataFrame"""
    raw = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df  = pd.DataFrame(raw, columns=[
        "time","open","high","low","close","volume",
        "close_time","quote_vol","trades","taker_buy_base",
        "taker_buy_quote","ignore"
    ])
    for col in ["open","high","low","close"]:
        df[col] = df[col].astype(float)
    return df

def pivot_high(highs, left, right):
    """
    Equivalente a ta.pivothigh(high, left, right) de Pine.
    Retorna array donde hay valor solo en pivots confirmados.
    """
    n   = len(highs)
    res = [None] * n
    for i in range(left, n - right):
        is_ph = all(highs[i] >= highs[i-j] for j in range(1, left+1)) and \
                all(highs[i] >= highs[i+j] for j in range(1, right+1))
        if is_ph:
            res[i] = highs[i]
    return res

def pivot_low(lows, left, right):
    """Equivalente a ta.pivotlow(low, left, right) de Pine"""
    n   = len(lows)
    res = [None] * n
    for i in range(left, n - right):
        is_pl = all(lows[i] <= lows[i-j] for j in range(1, left+1)) and \
                all(lows[i] <= lows[i+j] for j in range(1, right+1))
        if is_pl:
            res[i] = lows[i]
    return res

def last_val(arr):
    """Último valor no None de un array"""
    for v in reversed(arr):
        if v is not None:
            return v
    return None

def prev_val(arr):
    """Penúltimo valor no None de un array"""
    count = 0
    for v in reversed(arr):
        if v is not None:
            count += 1
            if count == 2:
                return v
    return None

def find_htf_ob(opens, highs, lows, closes, direction, lookback=6):
    """
    Replica la lógica del HTF OB del Pine Script.
    Busca las últimas 3 velas consecutivas en dirección opuesta al BOS.
    direction=1 → BOS alcista → busca 3 velas bajistas (bullish OB)
    direction=-1 → BOS bajista → busca 3 velas alcistas (bearish OB)
    """
    for start in range(0, lookback - 2):
        if direction == 1:
            # Busca 3 velas bajistas consecutivas
            if (closes[-(start+1)] < opens[-(start+1)] and
                closes[-(start+2)] < opens[-(start+2)] and
                closes[-(start+3)] < opens[-(start+3)]):
                ob_high = max(highs[-(start+1)], highs[-(start+2)], highs[-(start+3)])
                ob_low  = min(lows[-(start+1)],  lows[-(start+2)],  lows[-(start+3)])
                return ob_high, ob_low
        else:
            # Busca 3 velas alcistas consecutivas
            if (closes[-(start+1)] > opens[-(start+1)] and
                closes[-(start+2)] > opens[-(start+2)] and
                closes[-(start+3)] > opens[-(start+3)]):
                ob_high = max(highs[-(start+1)], highs[-(start+2)], highs[-(start+3)])
                ob_low  = min(lows[-(start+1)],  lows[-(start+2)],  lows[-(start+3)])
                return ob_high, ob_low
    return None, None

# ─── Motor HTF ─────────────────────────────────────────────────────────────────

def process_htf(symbol, htf_interval="1h", swing_len=5):
    """
    Replica el módulo HTF Structure del indicador V3.
    Detecta BOS, construye el canal y gestiona el estado.
    """
    s   = get_state(symbol)
    df  = get_klines(symbol, htf_interval, limit=150)
    
    opens  = df["open"].tolist()
    highs  = df["high"].tolist()
    lows   = df["low"].tolist()
    closes = df["close"].tolist()

    ph = pivot_high(highs, swing_len, swing_len)
    pl = pivot_low(lows,   swing_len, swing_len)

    # Tracking de HH, HL, LL, LH (igual que Pine)
    last_high = last_val(ph)
    prev_high = prev_val(ph)
    last_low  = last_val(pl)
    prev_low  = prev_val(pl)

    if last_high is None or prev_high is None:
        return
    if last_low is None or prev_low is None:
        return

    last_hh = last_high if last_high > prev_high else None
    last_lh = last_high if last_high < prev_high else None
    last_hl = last_low  if last_low  > prev_low  else None
    last_ll = last_low  if last_low  < prev_low  else None

    current_close = closes[-1]

    # Detectar OB en HTF
    ob_high, ob_low = find_htf_ob(opens, highs, lows, closes, 1)
    bull_ob_valid   = ob_high is not None

    ob_high_b, ob_low_b = find_htf_ob(opens, highs, lows, closes, -1)
    bear_ob_valid        = ob_high_b is not None

    # BOS alcista: cierre sobre último HH con OB válido
    bull_bos = (last_hh is not None and
                current_close > last_hh and
                bull_ob_valid and
                (s["last_bull_bos_level"] is None or last_hh != s["last_bull_bos_level"]))

    # BOS bajista: cierre bajo último LL con OB válido
    bear_bos = (last_ll is not None and
                current_close < last_ll and
                bear_ob_valid and
                (s["last_bear_bos_level"] is None or last_ll != s["last_bear_bos_level"]))

    # Invalidación del BOS activo
    if s["htf_bos_valid"]:
        if s["htf_bos_dir"] == 1 and s["htf_ref_hl"] and current_close < s["htf_ref_hl"]:
            _reset_htf_state(s, "HTF Invalidated (Below HL)")
        if s["htf_bos_dir"] == -1 and s["htf_ref_lh"] and current_close > s["htf_ref_lh"]:
            _reset_htf_state(s, "HTF Invalidated (Above LH)")
        if s["htf_bos_dir"] == 1 and bear_bos:
            _reset_htf_state(s, "HTF Invalidated (Opposing BOS)")
        if s["htf_bos_dir"] == -1 and bull_bos:
            _reset_htf_state(s, "HTF Invalidated (Opposing BOS)")

    # Nuevo BOS alcista
    if bull_bos and not (s["htf_bos_dir"] == 1 and s["htf_bos_valid"]):
        s["htf_bos_dir"]         = 1
        s["htf_bos_level"]       = last_hh
        s["htf_bos_valid"]       = True
        s["htf_ref_hl"]          = last_hl
        s["waiting_hl"]          = True
        s["waiting_lh"]          = False
        s["waiting_hh_confirm"]  = False
        s["confirmed_hh"]        = last_hh
        s["htf_ob_high"]         = ob_high
        s["htf_ob_low"]          = ob_low
        s["last_bull_bos_level"] = last_hh
        s["htf_channel_active"]  = False
        s["htf_channel_built"]   = False
        _reset_ltf_state(s)
        s["trade_state"] = "Bullish BOS — Waiting for HL"
        print(f"  📈 BULLISH BOS detectado | Nivel: {last_hh:.2f}")

    # Nuevo BOS bajista
    if bear_bos and not (s["htf_bos_dir"] == -1 and s["htf_bos_valid"]):
        s["htf_bos_dir"]         = -1
        s["htf_bos_level"]       = last_ll
        s["htf_bos_valid"]       = True
        s["htf_ref_lh"]          = last_lh
        s["waiting_lh"]          = True
        s["waiting_hl"]          = False
        s["waiting_ll_confirm"]  = False
        s["confirmed_ll"]        = last_ll
        s["htf_ob_high"]         = ob_high_b
        s["htf_ob_low"]          = ob_low_b
        s["last_bear_bos_level"] = last_ll
        s["htf_channel_active"]  = False
        s["htf_channel_built"]   = False
        _reset_ltf_state(s)
        s["trade_state"] = "Bearish BOS — Waiting for LH"
        print(f"  📉 BEARISH BOS detectado | Nivel: {last_ll:.2f}")

    # Esperar HL después de BOS alcista
    if s["waiting_hl"] and s["htf_bos_dir"] == 1 and last_hl is not None:
        s["waiting_hl"]         = False
        s["waiting_hh_confirm"] = True
        s["htf_ref_hl"]         = last_hl
        s["trade_state"]        = "HL Formed — Waiting for HH"

    # Confirmar HH → construir canal
    if s["waiting_hh_confirm"] and s["htf_bos_dir"] == 1:
        if last_hh is not None and s["confirmed_hh"] and last_hh > s["confirmed_hh"]:
            s["waiting_hh_confirm"]  = False
            s["htf_channel_bot"]     = s["htf_ob_low"]
            s["htf_channel_top"]     = last_hh
            s["htf_channel_mid"]     = s["htf_channel_bot"] + (s["htf_channel_top"] - s["htf_channel_bot"]) * 0.5
            s["htf_channel_built"]   = True
            s["htf_channel_active"]  = False
            _reset_ltf_state(s)
            s["trade_state"] = "Channel Built — Waiting for 50% Retrace"
            print(f"  🔲 Canal alcista construido | Bot: {s['htf_channel_bot']:.2f} | 50%: {s['htf_channel_mid']:.2f} | Top: {s['htf_channel_top']:.2f}")

    # Esperar LH después de BOS bajista
    if s["waiting_lh"] and s["htf_bos_dir"] == -1 and last_lh is not None:
        s["waiting_lh"]         = False
        s["waiting_ll_confirm"] = True
        s["htf_ref_lh"]         = last_lh
        s["trade_state"]        = "LH Formed — Waiting for LL"

    # Confirmar LL → construir canal
    if s["waiting_ll_confirm"] and s["htf_bos_dir"] == -1:
        if last_ll is not None and s["confirmed_ll"] and last_ll < s["confirmed_ll"]:
            s["waiting_ll_confirm"]  = False
            s["htf_channel_top"]     = s["htf_ob_high"]
            s["htf_channel_bot"]     = last_ll
            s["htf_channel_mid"]     = s["htf_channel_bot"] + (s["htf_channel_top"] - s["htf_channel_bot"]) * 0.5
            s["htf_channel_built"]   = True
            s["htf_channel_active"]  = False
            _reset_ltf_state(s)
            s["trade_state"] = "Channel Built — Waiting for 50% Retrace"
            print(f"  🔲 Canal bajista construido | Bot: {s['htf_channel_bot']:.2f} | 50%: {s['htf_channel_mid']:.2f} | Top: {s['htf_channel_top']:.2f}")

    # Activar canal cuando precio llega al 50%
    if s["htf_channel_built"] and not s["htf_channel_active"] and s["htf_bos_valid"]:
        if s["htf_bos_dir"] == 1 and current_close <= s["htf_channel_mid"]:
            s["htf_channel_active"] = True
            s["trade_state"]        = "Channel Active — 50% Validated"
            print(f"  ✅ Canal activo — precio en zona 50%: {current_close:.2f}")
        if s["htf_bos_dir"] == -1 and current_close >= s["htf_channel_mid"]:
            s["htf_channel_active"] = True
            s["trade_state"]        = "Channel Active — 50% Validated"
            print(f"  ✅ Canal activo — precio en zona 50%: {current_close:.2f}")

def _reset_htf_state(s, msg):
    s["htf_bos_valid"]      = False
    s["htf_channel_active"] = False
    s["htf_channel_built"]  = False
    s["waiting_hl"]         = False
    s["waiting_lh"]         = False
    s["waiting_hh_confirm"] = False
    s["waiting_ll_confirm"] = False
    s["trade_state"]        = msg
    _reset_ltf_state(s)

def _reset_ltf_state(s):
    s["ltf_state"]         = 0
    s["ltf_reentry_count"] = 0
    s["ltf_trade_active"]  = False
    s["ltf_internal_high"] = None
    s["ltf_internal_low"]  = None
    s["ltf_mss_level"]     = None
    s["ltf_mss_high"]      = None
    s["ltf_mss_low"]       = None
    s["ltf_fifty_level"]   = None
    s["ltf_fifty_hit"]     = False
    s["ltf_ob_high"]       = None
    s["ltf_ob_low"]        = None
    s["ltf_ob_confirmed"]  = False
    s["ltf_entry_price"]   = None
    s["ltf_stop_loss"]     = None
    s["ltf_take_profit"]   = None

# ─── Motor LTF ─────────────────────────────────────────────────────────────────

def process_ltf(symbol, ltf_interval="5m", swing_len=3, fifty_tolerance=5.0):
    """
    Replica el módulo LTF Execution del indicador V3.
    Solo corre si el canal HTF está activo.
    """
    s = get_state(symbol)

    if not s["htf_bos_valid"] or not s["htf_channel_active"]:
        return None

    df     = get_klines(symbol, ltf_interval, limit=100)
    opens  = df["open"].tolist()
    highs  = df["high"].tolist()
    lows   = df["low"].tolist()
    closes = df["close"].tolist()

    channel_top = s["htf_channel_top"]
    channel_bot = s["htf_channel_bot"]
    channel_mid = s["htf_channel_mid"]
    bos_dir     = s["htf_bos_dir"]

    # Verificar si precio está en zona 50% con tolerancia
    channel_range = channel_top - channel_bot
    current_pos   = (closes[-1] - channel_bot) / channel_range * 100
    in_fifty      = (50 - fifty_tolerance) <= current_pos <= (50 + fifty_tolerance)

    # Estado 0 → esperar zona 50%
    if s["ltf_state"] == 0:
        if in_fifty:
            s["ltf_state"]  = 1
            s["trade_state"] = "In 50% Zone — Building LTF Structure"
            print(f"    🎯 LTF: Precio en zona 50% ({current_pos:.1f}%)")
        return None

    # Si sale de la zona 50%, resetear LTF
    if not in_fifty and s["ltf_state"] == 1:
        s["ltf_state"]         = 0
        s["ltf_internal_high"] = None
        s["ltf_internal_low"]  = None
        s["trade_state"]       = "Outside 50% Zone — LTF Paused"
        return None

    # Detectar pivots LTF
    ph = pivot_high(highs, swing_len, swing_len)
    pl = pivot_low(lows,   swing_len, swing_len)

    last_ph = last_val(ph)
    last_pl = last_val(pl)

    if last_ph:
        s["ltf_internal_high"] = last_ph
    if last_pl:
        s["ltf_internal_low"]  = last_pl

    # Estado 1 → detectar MSS en LTF
    if s["ltf_state"] == 1:
        if s["ltf_internal_high"] is None or s["ltf_internal_low"] is None:
            return None

        # MSS alcista: 3 velas alcistas consecutivas que rompen internal high
        if bos_dir == 1 and s["ltf_internal_high"]:
            c1   = closes[-3] > opens[-3]
            c2   = closes[-2] > opens[-2]
            c3   = closes[-1] > opens[-1]
            prog = closes[-2] > closes[-3] and closes[-1] > closes[-2]
            if c1 and c2 and c3 and prog and closes[-1] > s["ltf_internal_high"]:
                s["ltf_state"]      = 2
                s["ltf_mss_level"]  = s["ltf_internal_high"]
                s["ltf_mss_high"]   = highs[-1]
                s["ltf_mss_low"]    = s["ltf_internal_low"]
                s["ltf_fifty_level"] = s["ltf_mss_low"] + (s["ltf_mss_high"] - s["ltf_mss_low"]) * 0.5
                s["ltf_fifty_hit"]  = False
                s["trade_state"]    = "LTF MSS Bullish — Waiting for 50%"
                print(f"    📈 LTF MSS Bullish | 50% en: {s['ltf_fifty_level']:.2f}")

        # MSS bajista
        if bos_dir == -1 and s["ltf_internal_low"]:
            c1   = closes[-3] < opens[-3]
            c2   = closes[-2] < opens[-2]
            c3   = closes[-1] < opens[-1]
            prog = closes[-2] < closes[-3] and closes[-1] < closes[-2]
            if c1 and c2 and c3 and prog and closes[-1] < s["ltf_internal_low"]:
                s["ltf_state"]       = 2
                s["ltf_mss_level"]   = s["ltf_internal_low"]
                s["ltf_mss_low"]     = lows[-1]
                s["ltf_mss_high"]    = s["ltf_internal_high"]
                s["ltf_fifty_level"] = s["ltf_mss_low"] + (s["ltf_mss_high"] - s["ltf_mss_low"]) * 0.5
                s["ltf_fifty_hit"]   = False
                s["trade_state"]     = "LTF MSS Bearish — Waiting for 50%"
                print(f"    📉 LTF MSS Bearish | 50% en: {s['ltf_fifty_level']:.2f}")

    # Estado 2 → esperar retroceso al 50% del MSS
    if s["ltf_state"] == 2 and not s["ltf_fifty_hit"] and s["ltf_fifty_level"]:
        if bos_dir == 1 and lows[-1] <= s["ltf_fifty_level"]:
            s["ltf_fifty_hit"] = True
            s["ltf_state"]     = 3
            s["trade_state"]   = "LTF 50% Hit — Tracking OB"
            print(f"    ✅ LTF 50% alcanzado — buscando OB")
        if bos_dir == -1 and highs[-1] >= s["ltf_fifty_level"]:
            s["ltf_fifty_hit"] = True
            s["ltf_state"]     = 3
            s["trade_state"]   = "LTF 50% Hit — Tracking OB"
            print(f"    ✅ LTF 50% alcanzado — buscando OB")

    # Estado 3 → identificar OB en LTF
    if s["ltf_state"] == 3 and s["ltf_fifty_hit"]:
        if bos_dir == 1:
            # Vela bajista seguida de alcista → bullish OB
            if closes[-1] < opens[-1]:
                s["ltf_ob_high"] = highs[-1]
                s["ltf_ob_low"]  = lows[-1]
            if closes[-1] > opens[-1] and closes[-2] < opens[-2] and s["ltf_ob_high"]:
                s["ltf_ob_confirmed"] = True
                s["ltf_state"]        = 4
                s["trade_state"]      = "LTF OB Identified — Waiting for Entry"
                print(f"    📦 LTF OB alcista: {s['ltf_ob_low']:.2f} — {s['ltf_ob_high']:.2f}")

        if bos_dir == -1:
            # Vela alcista seguida de bajista → bearish OB
            if closes[-1] > opens[-1]:
                s["ltf_ob_high"] = highs[-1]
                s["ltf_ob_low"]  = lows[-1]
            if closes[-1] < opens[-1] and closes[-2] > opens[-2] and s["ltf_ob_low"]:
                s["ltf_ob_confirmed"] = True
                s["ltf_state"]        = 4
                s["trade_state"]      = "LTF OB Identified — Waiting for Entry"
                print(f"    📦 LTF OB bajista: {s['ltf_ob_low']:.2f} — {s['ltf_ob_high']:.2f}")

    # Estado 4 → esperar que precio entre al OB → ENTRADA
    if s["ltf_state"] == 4 and not s["ltf_trade_active"]:
        if s["ltf_ob_high"] is None or s["ltf_ob_low"] is None:
            return None

        ob_buffer = 0.001  # 0.1% buffer igual que el Pine

        if bos_dir == 1 and lows[-1] <= s["ltf_ob_high"] and highs[-1] >= s["ltf_ob_low"]:
            entry = closes[-1]
            sl    = s["ltf_ob_low"]  * (1 - ob_buffer)
            tp    = s["htf_channel_top"]
            s["ltf_trade_active"]  = True
            s["ltf_reentry_count"] += 1
            s["ltf_state"]         = 5
            s["ltf_entry_price"]   = entry
            s["ltf_stop_loss"]     = sl
            s["ltf_take_profit"]   = tp
            s["trade_state"]       = f"LONG R{s['ltf_reentry_count']} Active"
            print(f"    🟢 SEÑAL LONG | Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}")
            return {"side": "BUY", "entry": entry, "sl": round(sl, 2), "tp": round(tp, 2)}

        if bos_dir == -1 and highs[-1] >= s["ltf_ob_low"] and lows[-1] <= s["ltf_ob_high"]:
            entry = closes[-1]
            sl    = s["ltf_ob_high"] * (1 + ob_buffer)
            tp    = s["htf_channel_bot"]
            s["ltf_trade_active"]  = True
            s["ltf_reentry_count"] += 1
            s["ltf_state"]         = 5
            s["ltf_entry_price"]   = entry
            s["ltf_stop_loss"]     = sl
            s["ltf_take_profit"]   = tp
            s["trade_state"]       = f"SHORT R{s['ltf_reentry_count']} Active"
            print(f"    🔴 SEÑAL SHORT | Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}")
            return {"side": "SELL", "entry": entry, "sl": round(sl, 2), "tp": round(tp, 2)}

    # Estado 5 → trade activo: monitorear SL/TP
    if s["ltf_state"] == 5 and s["ltf_trade_active"]:
        bos_dir = s["htf_bos_dir"]
        if bos_dir == 1:
            if lows[-1] <= s["ltf_stop_loss"]:
                print(f"    ❌ SL alcanzado — re-entry disponible")
                _reset_ltf_state(s)
                s["ltf_state"]  = 1  # Volver a buscar en LTF
            elif highs[-1] >= s["ltf_take_profit"]:
                print(f"    ✅ TP alcanzado")
                _reset_ltf_state(s)
                s["ltf_state"]  = 1
        if bos_dir == -1:
            if highs[-1] >= s["ltf_stop_loss"]:
                print(f"    ❌ SL alcanzado — re-entry disponible")
                _reset_ltf_state(s)
                s["ltf_state"]  = 1
            elif lows[-1] <= s["ltf_take_profit"]:
                print(f"    ✅ TP alcanzado")
                _reset_ltf_state(s)
                s["ltf_state"]  = 1

    return None

# ─── Utilidades de precisión ───────────────────────────────────────────────────

def get_symbol_info(symbol):
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
    decimals = len(str(tick_size).rstrip("0").split(".")[-1])
    return round(round(price / tick_size) * tick_size, decimals)

def round_to_step(qty, step_size):
    decimals = len(str(step_size).rstrip("0").split(".")[-1])
    return round(round(qty / step_size) * step_size, decimals)

# ─── Ejecución de órdenes ──────────────────────────────────────────────────────

def execute_trade(symbol, side, sl_price, tp_price):
    tick_size, step_size = get_symbol_info(symbol)

    account = client.futures_account()
    balance = float(next(
        a["availableBalance"] for a in account["assets"] if a["asset"] == "USDT"
    ))

    mark        = client.futures_mark_price(symbol=symbol)
    entry_price = float(mark["markPrice"])

    sl_pct      = abs(entry_price - sl_price) / entry_price * 100
    risk_amount = balance * (RISK_PERCENT / 100)
    quantity    = risk_amount / (entry_price * sl_pct / 100)
    quantity    = round_to_step(max(quantity, step_size), step_size)

    sl_price = round_to_tick(sl_price, tick_size)
    tp_price = round_to_tick(tp_price, tick_size)

    binance_side = SIDE_BUY  if side == "BUY"  else SIDE_SELL
    close_side   = SIDE_SELL if side == "BUY"  else SIDE_BUY

    order = client.futures_create_order(
        symbol=symbol, side=binance_side,
        type=ORDER_TYPE_MARKET, quantity=quantity
    )

    client.futures_create_order(
        symbol=symbol, side=close_side,
        type="STOP_MARKET", stopPrice=sl_price,
        closePosition="true", workingType="MARK_PRICE"
    )

    client.futures_create_order(
        symbol=symbol, side=close_side,
        type="TAKE_PROFIT_MARKET", stopPrice=tp_price,
        closePosition="true", workingType="MARK_PRICE"
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

@app.route("/status", methods=["GET"])
def status():
    """Muestra el estado actual de cada símbolo"""
    return jsonify({
        sym: {
            "trade_state"      : s["trade_state"],
            "htf_bos_dir"      : s["htf_bos_dir"],
            "htf_channel_active": s["htf_channel_active"],
            "ltf_state"        : s["ltf_state"],
            "ltf_trade_active" : s["ltf_trade_active"],
        }
        for sym, s in state.items()
    }), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True)
        if not data:
            raw  = request.data.decode("utf-8").strip()
            data = json.loads(raw)

        if data.get("token", "") != WEBHOOK_SECRET:
            return jsonify({"error": "token invalido"}), 403

        symbol  = data.get("symbol", "BTCUSDT")
        message = data.get("message", "")
        print(f"📩 Señal recibida para {symbol}: {message}")

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

def send_signal(symbol, signal):
    msg = (
        f"{'▲ LONG' if signal['side'] == 'BUY' else '▼ SHORT'} ENTRY R1 "
        f"[Smart Liquidity V3]\nEntry: {signal['entry']} | "
        f"SL: {signal['sl']} | TP: {signal['tp']}"
    )
    payload = {"token": WEBHOOK_SECRET, "symbol": symbol, "message": msg}
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"📤 Señal enviada: {r.json()}")
    except Exception as e:
        print(f"❌ Error enviando señal: {e}")

def run_engine(symbols=["BTCUSDT", "ETHUSDT"], interval_seconds=300):
    print(f"🚀 Motor iniciado — chequeando cada {interval_seconds}s")
    while True:
        for symbol in symbols:
            try:
                print(f"\n📊 [{symbol}] Estado: {get_state(symbol)['trade_state']}")

                # Procesar HTF primero
                process_htf(symbol, htf_interval="1h", swing_len=5)

                # Procesar LTF solo si el canal está activo
                signal = process_ltf(symbol, ltf_interval="5m", swing_len=3, fifty_tolerance=5.0)

                if signal:
                    send_signal(symbol, signal)

            except Exception as e:
                print(f"  ❌ Error en {symbol}: {e}")

        time.sleep(interval_seconds)

# ─── Arranque ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    engine_thread = threading.Thread(target=run_engine, daemon=True)
    engine_thread.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)