# sheets_logger.py COMPLETO con get_recent_trades incluido
import os
import json
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import base64

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

def get_client():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS", "")
    try:
        creds_json = base64.b64decode(creds_raw).decode("utf-8")
    except Exception:
        creds_json = creds_raw
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)

def get_or_create_sheet(gc, title):
    sh = gc.open_by_key(SHEET_ID)
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=1000, cols=20)

def setup_headers():
    try:
        gc = get_client()
        trades_ws = get_or_create_sheet(gc, "Trades")
        if not trades_ws.get_all_values():
            trades_ws.update("A1:L1", [[
                "Fecha", "Par", "Dirección", "Entry",
                "SL", "TP", "Cantidad", "Balance USDT",
                "Estado", "P&L USDT", "P&L %", "Notas"
            ]])
            trades_ws.format("A1:L1", {"textFormat": {"bold": True}})

        state_ws = get_or_create_sheet(gc, "Estado Bot")
        if not state_ws.get_all_values():
            state_ws.update("A1:C1", [["Símbolo", "Estado", "Última actualización"]])
            state_ws.format("A1:C1", {"textFormat": {"bold": True}})

        print("✅ Google Sheets configurado correctamente")
    except Exception as e:
        print(f"❌ Error configurando Google Sheets: {e}")

def log_trade_entry(symbol, side, entry, sl, tp, quantity, balance):
    try:
        gc       = get_client()
        ws       = get_or_create_sheet(gc, "Trades")
        now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([now, symbol, side, entry, sl, tp, quantity, balance,
                       "ABIERTO", "", "", ""])
        print(f"📝 Trade registrado | {side} {symbol}")
    except Exception as e:
        print(f"❌ Error registrando trade: {e}")

def log_trade_exit(symbol, side, entry, exit_price, quantity, balance, reason):
    try:
        gc       = get_client()
        ws       = get_or_create_sheet(gc, "Trades")
        all_rows = ws.get_all_values()
        target_row = None
        for i, row in enumerate(all_rows[1:], start=2):
            if len(row) > 8 and row[1] == symbol and row[8] == "ABIERTO":
                target_row = i

        if target_row is None:
            print(f"⚠️ No se encontró trade abierto para {symbol}")
            return

        if side == "BUY":
            pnl_usdt = (exit_price - entry) * quantity
        else:
            pnl_usdt = (entry - exit_price) * quantity

        pnl_pct = (pnl_usdt / (entry * quantity)) * 100 if entry and quantity else 0

        ws.update(f"I{target_row}:L{target_row}", [[
            reason, round(pnl_usdt, 2), round(pnl_pct, 2), f"Exit: {exit_price}"
        ]])
        print(f"📝 Trade cerrado | {reason} | P&L: {pnl_usdt:.2f} USDT")
    except Exception as e:
        print(f"❌ Error actualizando trade: {e}")

def update_bot_state(symbol, trade_state):
    try:
        gc       = get_client()
        ws       = get_or_create_sheet(gc, "Estado Bot")
        now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        all_rows = ws.get_all_values()
        target_row = None
        for i, row in enumerate(all_rows[1:], start=2):
            if row and row[0] == symbol:
                target_row = i
                break
        if target_row:
            ws.update(f"A{target_row}:C{target_row}", [[symbol, trade_state, now]])
        else:
            ws.append_row([symbol, trade_state, now])
    except Exception as e:
        print(f"❌ Error actualizando estado: {e}")

def get_recent_trades(n=5):
    """Devuelve los últimos N trades como lista de dicts"""
    try:
        gc       = get_client()
        ws       = get_or_create_sheet(gc, "Trades")
        all_rows = ws.get_all_records()
        if not all_rows:
            return []
        return all_rows[-n:]
    except Exception as e:
        print(f"❌ Error obteniendo trades: {e}")
        return []