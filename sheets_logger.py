import os
import json
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

def get_client():
    import base64
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS")
    # Intentar base64 primero, sino usar JSON directo
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
        ws = sh.add_worksheet(title=title, rows=1000, cols=20)
        return ws

def setup_headers():
    """Crea las hojas con headers si no existen"""
    try:
        gc = get_client()

        # Hoja de trades
        trades_ws = get_or_create_sheet(gc, "Trades")
        if trades_ws.row_count == 0 or trades_ws.cell(1, 1).value is None:
            trades_ws.update("A1:L1", [[
                "Fecha", "Par", "Dirección", "Entry",
                "SL", "TP", "Cantidad", "Balance USDT",
                "Estado", "P&L USDT", "P&L %", "Notas"
            ]])
            trades_ws.format("A1:L1", {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2}
            })

        # Hoja de estado del bot
        state_ws = get_or_create_sheet(gc, "Estado Bot")
        if state_ws.cell(1, 1).value is None:
            state_ws.update("A1:C1", [["Símbolo", "Estado", "Última actualización"]])
            state_ws.format("A1:C1", {"textFormat": {"bold": True}})

        print("✅ Google Sheets configurado correctamente")

    except Exception as e:
        print(f"❌ Error configurando Google Sheets: {e}")

def log_trade_entry(symbol, side, entry, sl, tp, quantity, balance):
    """Registra la apertura de un trade"""
    try:
        gc = get_client()
        ws = get_or_create_sheet(gc, "Trades")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            now, symbol, side, entry,
            sl, tp, quantity, balance,
            "ABIERTO", "", "", ""
        ]
        ws.append_row(row)
        print(f"📝 Trade registrado en Sheets: {side} {symbol}")

    except Exception as e:
        print(f"❌ Error registrando trade: {e}")

def log_trade_exit(symbol, side, entry, exit_price, quantity, balance, reason):
    """Actualiza el trade cuando se cierra con P&L"""
    try:
        gc = get_client()
        ws = get_or_create_sheet(gc, "Trades")

        # Buscar la última fila abierta de este símbolo
        all_rows = ws.get_all_values()
        target_row = None
        for i, row in enumerate(all_rows[1:], start=2):
            if row[1] == symbol and row[8] == "ABIERTO":
                target_row = i

        if target_row is None:
            print(f"⚠️ No se encontró trade abierto para {symbol}")
            return

        # Calcular P&L
        if side == "BUY":
            pnl_usdt = (exit_price - entry) * quantity
        else:
            pnl_usdt = (entry - exit_price) * quantity

        pnl_pct = (pnl_usdt / (entry * quantity)) * 100

        # Actualizar fila
        ws.update(f"I{target_row}:L{target_row}", [[
            reason,
            round(pnl_usdt, 2),
            round(pnl_pct, 2),
            f"Exit: {exit_price}"
        ]])
        print(f"📝 Trade cerrado en Sheets: {reason} | P&L: {pnl_usdt:.2f} USDT")

    except Exception as e:
        print(f"❌ Error actualizando trade: {e}")

def update_bot_state(symbol, trade_state):
    """Actualiza el estado actual del bot en la hoja Estado"""
    try:
        gc = get_client()
        ws = get_or_create_sheet(gc, "Estado Bot")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        all_rows = ws.get_all_values()

        # Buscar si ya existe fila para este símbolo
        target_row = None
        for i, row in enumerate(all_rows[1:], start=2):
            if row[0] == symbol:
                target_row = i
                break

        if target_row:
            ws.update(f"A{target_row}:C{target_row}", [[symbol, trade_state, now]])
        else:
            ws.append_row([symbol, trade_state, now])

    except Exception as e:
        print(f"❌ Error actualizando estado: {e}")