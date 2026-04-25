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
from sheets_logger import setup_headers, log_trade_entry, log_trade_exit, update_bot_state

load_dotenv()

app = Flask(__name__)

API_KEY        = os.environ.get("BINANCE_API_KEY")
API_SECRET     = os.environ.get("BINANCE_API_SECRET")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
RISK_PERCENT   = float(os.environ.get("RISK_PERCENT", "10"))
WEBHOOK_URL    = os.environ.get("WEBHOOK_URL", "http://127.0.0.1:5000/webhook")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")

client = Client(API_KEY, API_SECRET)
client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

# ─── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id"   : TELEGRAM_CHAT,
            "text"      : msg,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"❌ Error Telegram: {e}")

# ─── Estado global por símbolo ─────────────────────────────────────────────────

state = {}

def get_state(symbol):
    if symbol not in state:
        state[symbol] = {
            # HTF
            "htf_bos_dir"         : 0,
            "htf_bos_valid"       : False,
            "htf_bos_level"       : None,
            "htf_channel_top"     : None,
            "htf_channel_bot"     : None,
            "htf_channel_mid"     : None,
            "htf_channel_active"  : False,
            "htf_channel_built"   : False,
            "htf_ob_high"         : None,
            "htf_ob_low"          : None,
            "htf_ref_hl"          : None,
            "htf_ref_lh"          : None,
            "waiting_hl"          : False,
            "waiting_lh"          : False,
            "waiting_hh_confirm"  : False,
            "waiting_ll_confirm"  : False,
            "confirmed_hh"        : None,
            "confirmed_ll"        : None,
            "last_bull_bos_level" : None,
            "last_bear_bos_level" : None,
            # LTF
            "ltf_state"           : 0,
            "ltf_internal_high"   : None,
            "ltf_internal_low"    : None,
            "ltf_mss_level"       : None,
            "ltf_mss_high"        : None,
            "ltf_mss_low"         : None,
            "ltf_fifty_level"     : None,
            "ltf_fifty_hit"       : False,
            "ltf_ob_high"         : None,
            "ltf_ob_low"          : None,
            "ltf_ob_confirmed"    : False,
            "ltf_trade_active"    : False,
            "ltf_entry_price"     : None,
            "ltf_stop_loss"       : None,
            "ltf_take_profit"     : None,
            "ltf_reentry_count"   : 0,
            "ltf_quantity"        : None,
            "ltf_balance"         : None,
            "trade_state"         : "Neutral",
        }
    return state[symbol]

# ─── Utilidades de datos ───────────────────────────────────────────────────────

def get_klines(symbol, interval, limit=200):
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
    """Equivalente exacto a ta.pivothigh(high, left, right) de Pine"""
    n   = len(highs)
    res = [None] * n
    for i in range(left, n - right):
        is_ph = all(highs[i] >= highs[i-j] for j in range(1, left+1)) and \
                all(highs[i] >= highs[i+j] for j in range(1, right+1))
        if is_ph:
            res[i] = highs[i]
    return res

def pivot_low(lows, left, right):
    """Equivalente exacto a ta.pivotlow(low, left, right) de Pine"""
    n   = len(lows)
    res = [None] * n
    for i in range(left, n - right):
        is_pl = all(lows[i] <= lows[i-j] for j in range(1, left+1)) and \
                all(lows[i] <= lows[i+j] for j in range(1, right+1))
        if is_pl:
            res[i] = lows[i]
    return res

def last_val(arr):
    for v in reversed(arr):
        if v is not None:
            return v
    return None

def prev_val(arr):
    count = 0
    for v in reversed(arr):
        if v is not None:
            count += 1
            if count == 2:
                return v
    return None

def find_htf_ob(opens, highs, lows, closes, direction):
    """
    Replica EXACTAMENTE la lógica del Pine Script V3.
    Busca en las últimas 8 velas (índices 0-7 desde el final)
    3 velas consecutivas en dirección opuesta al BOS.
    
    Pine Script original:
    for startIdx = 0 to 5  (busca en posiciones 0,1,2 hasta 5,6,7)
        if c0,c1,c2 todas bajistas (para bullish OB)
            ob_high = max(h0,h1,h2)
            ob_low  = min(l0,l1,l2)
    """
    for start in range(0, 6):  # startIdx = 0 to 5 como en Pine
        i1 = -(start + 1)
        i2 = -(start + 2)
        i3 = -(start + 3)

        if direction == 1:
            # Bullish OB: busca 3 velas bajistas consecutivas
            if (closes[i1] < opens[i1] and
                closes[i2] < opens[i2] and
                closes[i3] < opens[i3]):
                ob_high = max(highs[i1], highs[i2], highs[i3])
                ob_low  = min(lows[i1],  lows[i2],  lows[i3])
                return ob_high, ob_low
        else:
            # Bearish OB: busca 3 velas alcistas consecutivas
            if (closes[i1] > opens[i1] and
                closes[i2] > opens[i2] and
                closes[i3] > opens[i3]):
                ob_high = max(highs[i1], highs[i2], highs[i3])
                ob_low  = min(lows[i1],  lows[i2],  lows[i3])
                return ob_high, ob_low
    return None, None

def calc_retry_risk(reentry_count, base_risk, retry_decay=True):
    """
    Replica retryDecay del Pine Script:
    R1 = 100% del riesgo base
    R2 = 75% del riesgo base  
    R3+ = 50% del riesgo base
    """
    if not retry_decay:
        return base_risk
    if reentry_count == 1:
        return base_risk * 1.0
    elif reentry_count == 2:
        return base_risk * 0.75
    else:
        return base_risk * 0.5

# ─── Motor HTF ─────────────────────────────────────────────────────────────────

def process_htf(symbol, htf_interval="1h", swing_len=5):
    """
    Replica EXACTAMENTE el módulo HTF Structure del Pine Script V3.
    """
    s      = get_state(symbol)
    df     = get_klines(symbol, htf_interval, limit=150)
    opens  = df["open"].tolist()
    highs  = df["high"].tolist()
    lows   = df["low"].tolist()
    closes = df["close"].tolist()

    # Detectar pivots HTF — igual que Pine con htfSwingLen
    ph = pivot_high(highs, swing_len, swing_len)
    pl = pivot_low(lows,   swing_len, swing_len)

    last_high = last_val(ph)
    prev_high = prev_val(ph)
    last_low  = last_val(pl)
    prev_low  = prev_val(pl)

    if None in [last_high, prev_high, last_low, prev_low]:
        return

    # HH, HL, LH, LL — igual que Pine
    last_hh = last_high if last_high > prev_high else None
    last_lh = last_high if last_high < prev_high else None
    last_hl = last_low  if last_low  > prev_low  else None
    last_ll = last_low  if last_low  < prev_low  else None

    current_close = closes[-1]

    # Detectar OBs con lookback exacto del Pine (for startIdx = 0 to 5)
    ob_high,   ob_low   = find_htf_ob(opens, highs, lows, closes,  1)
    ob_high_b, ob_low_b = find_htf_ob(opens, highs, lows, closes, -1)
    bull_ob_valid = ob_high   is not None
    bear_ob_valid = ob_high_b is not None

    # BOS conditions — igual que Pine
    # bullBosQualified = bullBosRaw and bullBosNewLevel and bullOBValid
    bull_bos = (last_hh is not None and
                current_close > last_hh and
                bull_ob_valid and
                (s["last_bull_bos_level"] is None or
                 last_hh != s["last_bull_bos_level"]))

    bear_bos = (last_ll is not None and
                current_close < last_ll and
                bear_ob_valid and
                (s["last_bear_bos_level"] is None or
                 last_ll != s["last_bear_bos_level"]))

    # ── Invalidación del BOS activo ──────────────────────────────────────────
    # Pine: chequea en CADA barra si el precio viola el HL/LH de referencia
    if s["htf_bos_valid"]:
        # Bullish BOS invalidado si precio cae bajo el HL
        if (s["htf_bos_dir"] == 1 and
                s["htf_ref_hl"] is not None and
                current_close < s["htf_ref_hl"]):
            _reset_htf_state(s, "HTF Invalidated (Below HL)")
            send_telegram(f"⚠️ <b>HTF Invalidado</b> — {symbol}\nPrecio bajo HL: {s['htf_ref_hl']:.2f}")

        # Bearish BOS invalidado si precio sube sobre el LH
        elif (s["htf_bos_dir"] == -1 and
                s["htf_ref_lh"] is not None and
                current_close > s["htf_ref_lh"]):
            _reset_htf_state(s, "HTF Invalidated (Above LH)")
            send_telegram(f"⚠️ <b>HTF Invalidado</b> — {symbol}\nPrecio sobre LH: {s['htf_ref_lh']:.2f}")

        # BOS opuesto invalida el actual
        elif s["htf_bos_dir"] == 1 and bear_bos:
            _reset_htf_state(s, "HTF Invalidated (Opposing BOS)")
        elif s["htf_bos_dir"] == -1 and bull_bos:
            _reset_htf_state(s, "HTF Invalidated (Opposing BOS)")

    # ── Nuevo BOS alcista ─────────────────────────────────────────────────────
    if bull_bos and not (s["htf_bos_dir"] == 1 and s["htf_bos_valid"]):
        s.update({
            "htf_bos_dir"        : 1,
            "htf_bos_level"      : last_hh,
            "htf_bos_valid"      : True,
            "htf_ref_hl"         : last_hl,
            "htf_ref_lh"         : None,
            "waiting_hl"         : True,
            "waiting_lh"         : False,
            "waiting_hh_confirm" : False,
            "waiting_ll_confirm" : False,
            "confirmed_hh"       : last_hh,
            "confirmed_ll"       : None,
            "htf_ob_high"        : ob_high,
            "htf_ob_low"         : ob_low,
            "last_bull_bos_level": last_hh,
            "htf_channel_active" : False,
            "htf_channel_built"  : False,
            "trade_state"        : "Bullish BOS — Waiting for HL"
        })
        _reset_ltf_state(s)
        print(f"  📈 BULLISH BOS | {last_hh:.2f}")
        send_telegram(
            f"📈 <b>BULLISH BOS</b> — {symbol}\n"
            f"Nivel: {last_hh:.2f}\n"
            f"HTF OB: {ob_low:.2f} — {ob_high:.2f}\n"
            f"Esperando HL..."
        )

    # ── Nuevo BOS bajista ─────────────────────────────────────────────────────
    if bear_bos and not (s["htf_bos_dir"] == -1 and s["htf_bos_valid"]):
        s.update({
            "htf_bos_dir"        : -1,
            "htf_bos_level"      : last_ll,
            "htf_bos_valid"      : True,
            "htf_ref_lh"         : last_lh,
            "htf_ref_hl"         : None,
            "waiting_lh"         : True,
            "waiting_hl"         : False,
            "waiting_hh_confirm" : False,
            "waiting_ll_confirm" : False,
            "confirmed_ll"       : last_ll,
            "confirmed_hh"       : None,
            "htf_ob_high"        : ob_high_b,
            "htf_ob_low"         : ob_low_b,
            "last_bear_bos_level": last_ll,
            "htf_channel_active" : False,
            "htf_channel_built"  : False,
            "trade_state"        : "Bearish BOS — Waiting for LH"
        })
        _reset_ltf_state(s)
        print(f"  📉 BEARISH BOS | {last_ll:.2f}")
        send_telegram(
            f"📉 <b>BEARISH BOS</b> — {symbol}\n"
            f"Nivel: {last_ll:.2f}\n"
            f"HTF OB: {ob_low_b:.2f} — {ob_high_b:.2f}\n"
            f"Esperando LH..."
        )

    # ── Esperar HL → confirmar HH ─────────────────────────────────────────────
    if s["waiting_hl"] and s["htf_bos_dir"] == 1 and last_hl is not None:
        s["waiting_hl"]         = False
        s["waiting_hh_confirm"] = True
        s["htf_ref_hl"]         = last_hl
        s["trade_state"]        = "HL Formed — Waiting for HH"

    if s["waiting_hh_confirm"] and s["htf_bos_dir"] == 1:
        if (last_hh is not None and
                s["confirmed_hh"] is not None and
                last_hh > s["confirmed_hh"]):
            # Canal: Bot = htf_ob_low, Top = nuevo HH
            channel_bot = s["htf_ob_low"]
            channel_top = last_hh
            channel_mid = channel_bot + (channel_top - channel_bot) * 0.5
            s.update({
                "waiting_hh_confirm" : False,
                "htf_channel_bot"    : channel_bot,
                "htf_channel_top"    : channel_top,
                "htf_channel_mid"    : channel_mid,
                "htf_channel_built"  : True,
                "htf_channel_active" : False,
                "trade_state"        : "Channel Built — Waiting for 50% Retrace"
            })
            _reset_ltf_state(s)
            print(f"  🔲 Canal alcista | Bot: {channel_bot:.2f} | 50%: {channel_mid:.2f} | Top: {channel_top:.2f}")
            send_telegram(
                f"🔲 <b>Canal alcista</b> — {symbol}\n"
                f"▲ Top: {channel_top:.2f}\n"
                f"◈ 50%: {channel_mid:.2f}\n"
                f"▼ Bot: {channel_bot:.2f}\n"
                f"Esperando retroceso al 50%..."
            )

    # ── Esperar LH → confirmar LL ─────────────────────────────────────────────
    if s["waiting_lh"] and s["htf_bos_dir"] == -1 and last_lh is not None:
        s["waiting_lh"]         = False
        s["waiting_ll_confirm"] = True
        s["htf_ref_lh"]         = last_lh
        s["trade_state"]        = "LH Formed — Waiting for LL"

    if s["waiting_ll_confirm"] and s["htf_bos_dir"] == -1:
        if (last_ll is not None and
                s["confirmed_ll"] is not None and
                last_ll < s["confirmed_ll"]):
            # Canal: Top = htf_ob_high, Bot = nuevo LL
            channel_top = s["htf_ob_high"]
            channel_bot = last_ll
            channel_mid = channel_bot + (channel_top - channel_bot) * 0.5
            s.update({
                "waiting_ll_confirm" : False,
                "htf_channel_top"    : channel_top,
                "htf_channel_bot"    : channel_bot,
                "htf_channel_mid"    : channel_mid,
                "htf_channel_built"  : True,
                "htf_channel_active" : False,
                "trade_state"        : "Channel Built — Waiting for 50% Retrace"
            })
            _reset_ltf_state(s)
            print(f"  🔲 Canal bajista | Bot: {channel_bot:.2f} | 50%: {channel_mid:.2f} | Top: {channel_top:.2f}")
            send_telegram(
                f"🔲 <b>Canal bajista</b> — {symbol}\n"
                f"▲ Top: {channel_top:.2f}\n"
                f"◈ 50%: {channel_mid:.2f}\n"
                f"▼ Bot: {channel_bot:.2f}\n"
                f"Esperando retroceso al 50%..."
            )

    # ── Activar canal cuando precio llega al 50% ──────────────────────────────
    # Pine: htfBosDir == 1 and low <= htfChannelMid
    #       htfBosDir == -1 and high >= htfChannelMid
    if s["htf_channel_built"] and not s["htf_channel_active"] and s["htf_bos_valid"]:
        if s["htf_bos_dir"] == 1 and closes[-1] <= s["htf_channel_mid"]:
            s["htf_channel_active"] = True
            s["trade_state"]        = "Channel Active — 50% Validated"
            print(f"  ✅ Canal activo — precio en 50%: {closes[-1]:.2f}")
            send_telegram(
                f"⬛ <b>Canal activo</b> — {symbol}\n"
                f"Precio en zona 50%: {closes[-1]:.2f}\n"
                f"Buscando entrada LTF..."
            )
        if s["htf_bos_dir"] == -1 and closes[-1] >= s["htf_channel_mid"]:
            s["htf_channel_active"] = True
            s["trade_state"]        = "Channel Active — 50% Validated"
            print(f"  ✅ Canal activo — precio en 50%: {closes[-1]:.2f}")
            send_telegram(
                f"⬛ <b>Canal activo</b> — {symbol}\n"
                f"Precio en zona 50%: {closes[-1]:.2f}\n"
                f"Buscando entrada LTF..."
            )

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
    s["ltf_quantity"]      = None
    s["ltf_balance"]       = None

# ─── Motor LTF ─────────────────────────────────────────────────────────────────

def process_ltf(symbol, ltf_interval="5m", swing_len=3, fifty_tolerance=5.0):
    """
    Replica EXACTAMENTE el módulo LTF Execution del Pine Script V3.
    fifty_tolerance = % de tolerancia alrededor del 50% del canal HTF
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
    bos_dir     = s["htf_bos_dir"]

    # Zona 50% con tolerancia — igual que Pine
    # Pine: currentPosition >= lowerBound and currentPosition <= upperBound
    # donde lowerBound = 50 - fiftyPercentTolerance
    channel_range = channel_top - channel_bot
    current_pos   = (closes[-1] - channel_bot) / channel_range * 100
    in_fifty      = (50 - fifty_tolerance) <= current_pos <= (50 + fifty_tolerance)

    # ── Estado 0: esperar zona 50% ────────────────────────────────────────────
    if s["ltf_state"] == 0:
        if in_fifty:
            s["ltf_state"]   = 1
            s["trade_state"] = "In 50% Zone — Building LTF Structure"
            print(f"    🎯 LTF: en zona 50% ({current_pos:.1f}%)")
        return None

    # Pine: if not insideFiftyZone and ltfState == 1 → resetear
    if not in_fifty and s["ltf_state"] == 1:
        s["ltf_state"]         = 0
        s["ltf_internal_high"] = None
        s["ltf_internal_low"]  = None
        s["trade_state"]       = "Outside 50% Zone — LTF Paused"
        return None

    # Detectar pivots LTF con ltfSwingLen
    ph = pivot_high(highs, swing_len, swing_len)
    pl = pivot_low(lows,   swing_len, swing_len)

    last_ph = last_val(ph)
    last_pl = last_val(pl)

    if last_ph:
        s["ltf_internal_high"] = last_ph
    if last_pl:
        s["ltf_internal_low"]  = last_pl

    # ── Estado 1: detectar MSS en LTF ────────────────────────────────────────
    # Pine replica: bullMSS_c1, bullMSS_c2, bullMSS_c3, bullMSS_prog
    if s["ltf_state"] == 1:
        if s["ltf_internal_high"] is None or s["ltf_internal_low"] is None:
            return None

        if bos_dir == 1 and s["ltf_internal_high"]:
            # 3 velas alcistas consecutivas con progresión
            c1   = closes[-3] > opens[-3]
            c2   = closes[-2] > opens[-2]
            c3   = closes[-1] > opens[-1]
            prog = closes[-2] > closes[-3] and closes[-1] > closes[-2]
            if c1 and c2 and c3 and prog and closes[-1] > s["ltf_internal_high"]:
                mss_high     = highs[-1]
                mss_low      = s["ltf_internal_low"]
                fifty_level  = mss_low + (mss_high - mss_low) * 0.5
                s.update({
                    "ltf_state"      : 2,
                    "ltf_mss_level"  : s["ltf_internal_high"],
                    "ltf_mss_high"   : mss_high,
                    "ltf_mss_low"    : mss_low,
                    "ltf_fifty_level": fifty_level,
                    "ltf_fifty_hit"  : False,
                    "trade_state"    : "LTF MSS Bullish — Waiting for 50%"
                })
                print(f"    📈 LTF MSS Bullish | 50%: {fifty_level:.2f}")

        if bos_dir == -1 and s["ltf_internal_low"]:
            # 3 velas bajistas consecutivas con progresión
            c1   = closes[-3] < opens[-3]
            c2   = closes[-2] < opens[-2]
            c3   = closes[-1] < opens[-1]
            prog = closes[-2] < closes[-3] and closes[-1] < closes[-2]
            if c1 and c2 and c3 and prog and closes[-1] < s["ltf_internal_low"]:
                mss_low      = lows[-1]
                mss_high     = s["ltf_internal_high"]
                fifty_level  = mss_low + (mss_high - mss_low) * 0.5
                s.update({
                    "ltf_state"      : 2,
                    "ltf_mss_level"  : s["ltf_internal_low"],
                    "ltf_mss_low"    : mss_low,
                    "ltf_mss_high"   : mss_high,
                    "ltf_fifty_level": fifty_level,
                    "ltf_fifty_hit"  : False,
                    "trade_state"    : "LTF MSS Bearish — Waiting for 50%"
                })
                print(f"    📉 LTF MSS Bearish | 50%: {fifty_level:.2f}")

    # ── Estado 2: esperar retroceso al 50% del MSS ───────────────────────────
    # Pine: if ltfState == 2 and not ltfFiftyHit and not na(ltfFiftyLevel)
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

    # ── Estado 3: identificar OB en LTF ──────────────────────────────────────
    # Pine replica exacta:
    # Bullish: vela bajista (close < open) → luego vela alcista confirma
    # Bearish: vela alcista (close > open) → luego vela bajista confirma
    if s["ltf_state"] == 3 and s["ltf_fifty_hit"]:
        if bos_dir == 1:
            if closes[-1] < opens[-1]:
                # Guardar la vela bajista como candidato OB
                s["ltf_ob_high"] = highs[-1]
                s["ltf_ob_low"]  = lows[-1]
            # Vela alcista confirma el OB de la vela bajista anterior
            if (closes[-1] > opens[-1] and
                    closes[-2] < opens[-2] and
                    s["ltf_ob_high"] is not None):
                s["ltf_ob_confirmed"] = True
                s["ltf_state"]        = 4
                s["trade_state"]      = "LTF OB Identified — Waiting for Entry"
                print(f"    📦 LTF OB alcista: {s['ltf_ob_low']:.2f} — {s['ltf_ob_high']:.2f}")
                send_telegram(
                    f"📦 <b>OB LTF identificado</b> — {symbol}\n"
                    f"Zona: {s['ltf_ob_low']:.2f} — {s['ltf_ob_high']:.2f}\n"
                    f"⏳ Esperando entrada..."
                )

        if bos_dir == -1:
            if closes[-1] > opens[-1]:
                # Guardar la vela alcista como candidato OB
                s["ltf_ob_high"] = highs[-1]
                s["ltf_ob_low"]  = lows[-1]
            # Vela bajista confirma el OB de la vela alcista anterior
            if (closes[-1] < opens[-1] and
                    closes[-2] > opens[-2] and
                    s["ltf_ob_low"] is not None):
                s["ltf_ob_confirmed"] = True
                s["ltf_state"]        = 4
                s["trade_state"]      = "LTF OB Identified — Waiting for Entry"
                print(f"    📦 LTF OB bajista: {s['ltf_ob_low']:.2f} — {s['ltf_ob_high']:.2f}")
                send_telegram(
                    f"📦 <b>OB LTF identificado</b> — {symbol}\n"
                    f"Zona: {s['ltf_ob_low']:.2f} — {s['ltf_ob_high']:.2f}\n"
                    f"⏳ Esperando entrada..."
                )

    # ── Estado 4: entrada cuando precio entra al OB ───────────────────────────
    # Pine: if htfBosDir == 1 and low <= ltfOBHigh and high >= ltfOBLow → LONG
    #        stopLoss = ltfOBLow - ((ltfOBHigh - ltfOBLow) * obBuffer)
    #        takeProfit = htfChannelTop
    if s["ltf_state"] == 4 and not s["ltf_trade_active"]:
        if s["ltf_ob_high"] is None or s["ltf_ob_low"] is None:
            return None

        ob_buffer = 0.001  # obBuffer = 0.1% igual que Pine

        if bos_dir == 1 and lows[-1] <= s["ltf_ob_high"] and highs[-1] >= s["ltf_ob_low"]:
            entry = closes[-1]
            # SL exacto del Pine: ltfOBLow - ((ltfOBHigh - ltfOBLow) * obBuffer)
            sl    = s["ltf_ob_low"] - ((s["ltf_ob_high"] - s["ltf_ob_low"]) * ob_buffer)
            tp    = s["htf_channel_top"]
            s["ltf_trade_active"]  = True
            s["ltf_reentry_count"] += 1
            s["ltf_state"]         = 5
            s["ltf_entry_price"]   = entry
            s["ltf_stop_loss"]     = sl
            s["ltf_take_profit"]   = tp
            s["trade_state"]       = f"LONG R{s['ltf_reentry_count']} Active"
            print(f"    🟢 LONG R{s['ltf_reentry_count']} | Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}")
            return {"side": "BUY", "entry": entry, "sl": round(sl, 2), "tp": round(tp, 2)}

        if bos_dir == -1 and highs[-1] >= s["ltf_ob_low"] and lows[-1] <= s["ltf_ob_high"]:
            entry = closes[-1]
            # SL exacto del Pine: ltfOBHigh + ((ltfOBHigh - ltfOBLow) * obBuffer)
            sl    = s["ltf_ob_high"] + ((s["ltf_ob_high"] - s["ltf_ob_low"]) * ob_buffer)
            tp    = s["htf_channel_bot"]
            s["ltf_trade_active"]  = True
            s["ltf_reentry_count"] += 1
            s["ltf_state"]         = 5
            s["ltf_entry_price"]   = entry
            s["ltf_stop_loss"]     = sl
            s["ltf_take_profit"]   = tp
            s["trade_state"]       = f"SHORT R{s['ltf_reentry_count']} Active"
            print(f"    🔴 SHORT R{s['ltf_reentry_count']} | Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}")
            return {"side": "SELL", "entry": entry, "sl": round(sl, 2), "tp": round(tp, 2)}

    # ── Estado 5: trade activo — monitorear SL/TP ────────────────────────────
    # Pine replica exacta incluyendo emergency exit si HTF se invalida
    if s["ltf_state"] == 5 and s["ltf_trade_active"]:
        bos_dir_current = s["htf_bos_dir"]
        side            = "BUY" if bos_dir_current == 1 else "SELL"

        # Emergency exit — HTF invalidado
        if not s["htf_bos_valid"]:
            print(f"    🚨 Emergency exit — HTF invalidado")
            log_trade_exit(
                symbol, side, s["ltf_entry_price"],
                closes[-1], s["ltf_quantity"] or 0.001,
                s["ltf_balance"] or 0, "EMERGENCY EXIT"
            )
            send_telegram(
                f"🚨 <b>EMERGENCY EXIT</b> — {symbol}\n"
                f"HTF estructura invalidada\n"
                f"Exit: {closes[-1]:.2f}"
            )
            _reset_ltf_state(s)
            s["ltf_state"] = 0
            return None

        if bos_dir_current == 1:
            if lows[-1] <= s["ltf_stop_loss"]:
                print(f"    ❌ SL R{s['ltf_reentry_count']} — re-entry disponible")
                log_trade_exit(
                    symbol, side, s["ltf_entry_price"],
                    lows[-1], s["ltf_quantity"] or 0.001,
                    s["ltf_balance"] or 0, f"STOP LOSS R{s['ltf_reentry_count']}"
                )
                send_telegram(
                    f"❌ <b>STOP LOSS R{s['ltf_reentry_count']}</b> — {symbol}\n"
                    f"Entry: {s['ltf_entry_price']:.2f} → SL: {lows[-1]:.2f}\n"
                    f"Re-entry disponible"
                )
                count = s["ltf_reentry_count"]
                _reset_ltf_state(s)
                s["ltf_reentry_count"] = count  # Mantener contador para retry decay
                s["ltf_state"]         = 1
            elif highs[-1] >= s["ltf_take_profit"]:
                print(f"    ✅ TP R{s['ltf_reentry_count']} alcanzado")
                log_trade_exit(
                    symbol, side, s["ltf_entry_price"],
                    highs[-1], s["ltf_quantity"] or 0.001,
                    s["ltf_balance"] or 0, f"TAKE PROFIT R{s['ltf_reentry_count']}"
                )
                send_telegram(
                    f"✅ <b>TAKE PROFIT R{s['ltf_reentry_count']}</b> — {symbol}\n"
                    f"Entry: {s['ltf_entry_price']:.2f} → TP: {highs[-1]:.2f} 🎯"
                )
                _reset_ltf_state(s)
                s["ltf_state"] = 1

        if bos_dir_current == -1:
            if highs[-1] >= s["ltf_stop_loss"]:
                print(f"    ❌ SL R{s['ltf_reentry_count']} — re-entry disponible")
                log_trade_exit(
                    symbol, side, s["ltf_entry_price"],
                    highs[-1], s["ltf_quantity"] or 0.001,
                    s["ltf_balance"] or 0, f"STOP LOSS R{s['ltf_reentry_count']}"
                )
                send_telegram(
                    f"❌ <b>STOP LOSS R{s['ltf_reentry_count']}</b> — {symbol}\n"
                    f"Entry: {s['ltf_entry_price']:.2f} → SL: {highs[-1]:.2f}\n"
                    f"Re-entry disponible"
                )
                count = s["ltf_reentry_count"]
                _reset_ltf_state(s)
                s["ltf_reentry_count"] = count
                s["ltf_state"]         = 1
            elif lows[-1] <= s["ltf_take_profit"]:
                print(f"    ✅ TP R{s['ltf_reentry_count']} alcanzado")
                log_trade_exit(
                    symbol, side, s["ltf_entry_price"],
                    lows[-1], s["ltf_quantity"] or 0.001,
                    s["ltf_balance"] or 0, f"TAKE PROFIT R{s['ltf_reentry_count']}"
                )
                send_telegram(
                    f"✅ <b>TAKE PROFIT R{s['ltf_reentry_count']}</b> — {symbol}\n"
                    f"Entry: {s['ltf_entry_price']:.2f} → TP: {lows[-1]:.2f} 🎯"
                )
                _reset_ltf_state(s)
                s["ltf_state"] = 1

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

def execute_trade(symbol, side, sl_price, tp_price, reentry_count=1):
    tick_size, step_size = get_symbol_info(symbol)

    account = client.futures_account()
    balance = float(next(
        a["availableBalance"] for a in account["assets"] if a["asset"] == "USDT"
    ))

    mark        = client.futures_mark_price(symbol=symbol)
    entry_price = float(mark["markPrice"])

    # Retry Risk Decay — R1=100%, R2=75%, R3+=50%
    effective_risk = calc_retry_risk(reentry_count, RISK_PERCENT, retry_decay=True)
    print(f"  💰 Riesgo efectivo R{reentry_count}: {effective_risk:.1f}%")

    sl_pct      = abs(entry_price - sl_price) / entry_price * 100
    risk_amount = balance * (effective_risk / 100)
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

    # Guardar en estado para log de salida
    s = get_state(symbol)
    s["ltf_quantity"] = quantity
    s["ltf_balance"]  = balance

    # Registrar entrada en Google Sheets
    log_trade_entry(symbol, side, entry_price, sl_price, tp_price, quantity, balance)

    # Notificar por Telegram
    send_telegram(
        f"{'🟢' if side == 'BUY' else '🔴'} <b>{'LONG' if side == 'BUY' else 'SHORT'} EJECUTADO</b> — {symbol}\n"
        f"R{reentry_count} | Riesgo: {effective_risk:.1f}%\n"
        f"Entry: {entry_price:.2f}\n"
        f"SL: {sl_price:.2f}\n"
        f"TP: {tp_price:.2f}\n"
        f"Qty: {quantity} | Balance: {balance:.2f} USDT"
    )

    return {
        "status"       : "ok",
        "order_id"     : order["orderId"],
        "side"         : side,
        "quantity"     : quantity,
        "balance_usdt" : round(balance, 2),
        "entry"        : entry_price,
        "sl"           : sl_price,
        "tp"           : tp_price,
        "reentry"      : reentry_count,
        "effective_risk": effective_risk
    }

# ─── Rutas Flask ───────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "bot activo"}), 200

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        sym: {
            "trade_state"        : s["trade_state"],
            "htf_bos_dir"        : s["htf_bos_dir"],
            "htf_channel_active" : s["htf_channel_active"],
            "htf_channel_top"    : s["htf_channel_top"],
            "htf_channel_mid"    : s["htf_channel_mid"],
            "htf_channel_bot"    : s["htf_channel_bot"],
            "ltf_state"          : s["ltf_state"],
            "ltf_trade_active"   : s["ltf_trade_active"],
            "ltf_reentry_count"  : s["ltf_reentry_count"],
        }
        for sym, s in state.items()
    }), 200

@app.route("/debug-creds", methods=["GET"])
def debug_creds():
    creds = os.environ.get("GOOGLE_CREDENTIALS", "NO EXISTE")
    return jsonify({
        "length"   : len(creds),
        "first_10" : creds[:10],
        "last_10"  : creds[-10:]
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

        s             = get_state(symbol)
        reentry_count = s.get("ltf_reentry_count", 1)
        result        = execute_trade(symbol, side, sl_price, tp_price, reentry_count)
        return jsonify(result), 200

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({"error": str(e)}), 500

# ─── Signal Engine ─────────────────────────────────────────────────────────────

def send_signal(symbol, signal):
    msg = (
        f"{'▲ LONG' if signal['side'] == 'BUY' else '▼ SHORT'} ENTRY "
        f"R{get_state(symbol)['ltf_reentry_count']} "
        f"[Smart Liquidity V3]\nEntry: {signal['entry']} | "
        f"SL: {signal['sl']} | TP: {signal['tp']}"
    )
    payload = {"token": WEBHOOK_SECRET, "symbol": symbol, "message": msg}
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print(f"📤 Señal enviada: {r.json()}")
    except Exception as e:
        print(f"❌ Error enviando señal: {e}")

def run_engine(symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "AAVEUSDT", "HYPEUSDT"], interval_seconds=300):
    print(f"🚀 Motor iniciado — sincronizando con velas de 5 minutos...")

    while True:
        # Sincronizar con el próximo cierre de vela
        now       = time.time()
        wait_time = interval_seconds - (now % interval_seconds)

        if wait_time < 5:
            wait_time += interval_seconds

        next_candle = time.strftime("%H:%M:%S", time.localtime(now + wait_time))
        print(f"⏳ Próxima vela en {wait_time:.0f}s (cierra {next_candle})")
        time.sleep(wait_time)
        time.sleep(2)  # 2 seg extra para que Binance cierre la vela

        for symbol in symbols:
            try:
                print(f"\n📊 [{symbol}] Estado: {get_state(symbol)['trade_state']}")
                process_htf(symbol, htf_interval="1h", swing_len=5)
                signal = process_ltf(
                    symbol,
                    ltf_interval    = "5m",
                    swing_len       = 3,
                    fifty_tolerance = 5.0
                )
                if signal:
                    send_signal(symbol, signal)
                update_bot_state(symbol, get_state(symbol)["trade_state"])
            except Exception as e:
                print(f"  ❌ Error en {symbol}: {e}")

# ─── Arranque ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_headers()

    engine_thread = threading.Thread(target=run_engine, daemon=True)
    engine_thread.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)