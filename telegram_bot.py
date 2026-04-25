# telegram_bot.py COMPLETO con manejo de errores robusto
import os
import time
import threading

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")

_state  = None
_client = None
_bot    = None

def init(state_ref, client_ref):
    global _state, _client, _bot
    _state  = state_ref
    _client = client_ref
    if TELEGRAM_TOKEN:
        try:
            import telebot
            _bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
            _register_handlers()
            print("🤖 Telegram bot configurado")
        except Exception as e:
            print(f"❌ Error iniciando Telegram bot: {e}")

def is_authorized(message):
    return str(message.chat.id) == str(TELEGRAM_CHAT)

def _register_handlers():
    if not _bot:
        return

    @_bot.message_handler(commands=["start", "help"])
    def cmd_start(message):
        if not is_authorized(message):
            return
        _bot.reply_to(message,
            "🤖 <b>Smart Liquidity Bot</b>\n\n"
            "Comandos:\n"
            "/status — Estado de todos los pares\n"
            "/balance — Balance en Binance\n"
            "/trades — Últimos 5 trades\n"
            "/config — Configuración actual\n"
            "/help — Esta ayuda"
        )

    @_bot.message_handler(commands=["status"])
    def cmd_status(message):
        if not is_authorized(message):
            return
        if not _state:
            _bot.reply_to(message, "❌ Estado no disponible")
            return
        lines = ["📊 <b>Estado actual</b>\n"]
        for symbol, s in _state.items():
            dir_emoji   = "📈" if s["htf_bos_dir"] == 1 else "📉" if s["htf_bos_dir"] == -1 else "➖"
            trade_emoji = "🟢" if s["ltf_trade_active"] else "⚪"
            lines.append(f"{dir_emoji} <b>{symbol}</b> {trade_emoji}")
            lines.append(f"   {s['trade_state']}")
            lines.append(f"   LTF: {s['ltf_state']} | R: {s['ltf_reentry_count']}")
            if s.get("htf_channel_top") and s.get("htf_channel_bot"):
                lines.append(f"   Canal: {s['htf_channel_bot']:.4f} — {s['htf_channel_top']:.4f}")
            lines.append("")
        _bot.reply_to(message, "\n".join(lines))

    @_bot.message_handler(commands=["balance"])
    def cmd_balance(message):
        if not is_authorized(message):
            return
        if not _client:
            _bot.reply_to(message, "❌ Cliente Binance no disponible")
            return
        try:
            account = _client.futures_account()
            assets  = [a for a in account["assets"] if float(a["walletBalance"]) > 0]
            lines   = ["💰 <b>Balance Binance Futures</b>\n"]
            total   = 0.0
            for a in assets:
                wallet     = float(a["walletBalance"])
                available  = float(a["availableBalance"])
                unrealized = float(a.get("unrealizedProfit", 0))
                total     += wallet
                lines.append(
                    f"<b>{a['asset']}</b>\n"
                    f"   Wallet: {wallet:.2f}\n"
                    f"   Disponible: {available:.2f}\n"
                    f"   PnL no realizado: {unrealized:+.2f}"
                )
            lines.append(f"\n📊 Total: <b>{total:.2f} USDT</b>")
            _bot.reply_to(message, "\n".join(lines))
        except Exception as e:
            _bot.reply_to(message, f"❌ Error: {e}")

    @_bot.message_handler(commands=["trades"])
    def cmd_trades(message):
        if not is_authorized(message):
            return
        try:
            from sheets_logger import get_recent_trades
            trades = get_recent_trades(5)
            if not trades:
                _bot.reply_to(message, "📭 No hay trades registrados aún")
                return
            lines = ["📋 <b>Últimos 5 trades</b>\n"]
            for t in trades:
                estado = t.get("Estado", "—")
                pnl    = t.get("P&L USDT", "—")
                if "PROFIT" in str(estado):
                    emoji = "✅"
                elif "STOP" in str(estado):
                    emoji = "❌"
                elif "EMERGENCY" in str(estado):
                    emoji = "🚨"
                else:
                    emoji = "🔵"
                lines.append(
                    f"{emoji} <b>{t.get('Par','—')}</b> {t.get('Dirección','—')}\n"
                    f"   Entry: {t.get('Entry','—')} | {estado}\n"
                    f"   P&L: {pnl} USDT | {t.get('Fecha','—')}"
                )
                lines.append("")
            _bot.reply_to(message, "\n".join(lines))
        except Exception as e:
            _bot.reply_to(message, f"❌ Error: {e}")

    @_bot.message_handler(commands=["config"])
    def cmd_config(message):
        if not is_authorized(message):
            return
        leverage  = os.environ.get("LEVERAGE", "10")
        risk      = os.environ.get("RISK_PERCENT", "10")
        min_ratio = os.environ.get("MIN_RATIO", "2.0")
        symbols   = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "AAVEUSDT", "HYPEUSDT"]
        _bot.reply_to(message,
            f"⚙️ <b>Configuración actual</b>\n\n"
            f"📊 Pares: {', '.join(symbols)}\n"
            f"💰 Riesgo por trade: {risk}%\n"
            f"⚡ Leverage: {leverage}x\n"
            f"📐 Ratio mínimo: {min_ratio}:1\n"
            f"🕐 Intervalo: 5 minutos\n"
            f"🌐 Exchange: Binance Futures Testnet"
        )

def start_polling():
    """Arrancar polling con reconexión automática"""
    if not _bot:
        print("❌ Telegram bot no disponible")
        return
    print("🤖 Telegram bot escuchando comandos...")
    while True:
        try:
            _bot.infinity_polling(timeout=20, long_polling_timeout=10)
        except Exception as e:
            print(f"⚠️ Telegram polling error: {e} — reconectando en 10s...")
            time.sleep(10)