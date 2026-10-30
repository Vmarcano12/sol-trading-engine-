import os
import time
import hmac
import hashlib
import urllib.parse
from datetime import datetime
import requests

# =====================================================================
# CONFIGURACIÓN Y LECTURA DE CREDENCIALES CIFRADAS
# =====================================================================
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FUTURES_BASE = "https://fapi.binance.com"
SYMBOL = "SOLUSDT"
LEVERAGE = 5
MARGIN_PER_TRADE = 25.0
TP_PCT = 0.030   # +3.0% Take Profit
SL_PCT = 0.015   # -1.5% Stop Loss
COOLDOWN_HOURS = 4

consecutive_losses = 0
cooldown_until = 0

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[ERROR TELEGRAM] {e}")

def sign_query(query_str: str) -> str:
    return hmac.new(API_SECRET.encode("utf-8"), query_str.encode("utf-8"), hashlib.sha256).hexdigest()

def fapi_request(endpoint: str, method: str = "GET", params: dict = None):
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query_str = urllib.parse.urlencode(params)
    signature = sign_query(query_str)
    url = f"{FUTURES_BASE}{endpoint}?{query_str}&signature={signature}"
    headers = {"X-MBX-APIKEY": API_KEY}
    
    if method == "GET":
        res = requests.get(url, headers=headers, timeout=15)
    elif method == "POST":
        res = requests.post(url, headers=headers, timeout=15)
    elif method == "DELETE":
        res = requests.delete(url, headers=headers, timeout=15)
    else:
        raise ValueError("Metodo no soportado")
    
    return res.json()

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += abs(diff)
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            avg_gain = (avg_gain * (period - 1) + diff) / period
            avg_loss = (avg_loss * (period - 1)) / period
        else:
            avg_gain = (avg_gain * (period - 1)) / period
            avg_loss = (avg_loss * (period - 1) + abs(diff)) / period
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def run_engine_cycle():
    global consecutive_losses, cooldown_until
    
    # 1. Leer Cuenta
    account = fapi_request("/fapi/v2/account")
    if "assets" not in account:
        print(f"[SYS] Error leyendo balance: {account}")
        return

    available_balance = 0.0
    total_balance = 0.0
    for asset in account.get("assets", []):
        if asset.get("asset") == "USDT":
            available_balance = float(asset.get("availableBalance", 0))
            total_balance = float(asset.get("walletBalance", 0))

    # 2. Detectar Posicion Abierta
    open_position = None
    for pos in account.get("positions", []):
        if pos.get("symbol") == SYMBOL and float(pos.get("positionAmt", 0)) != 0:
            open_position = {
                "amt": float(pos.get("positionAmt")),
                "entryPrice": float(pos.get("entryPrice")),
                "pnl": float(pos.get("unrealizedProfit")),
                "leverage": int(pos.get("leverage"))
            }

    # 3. Velas de 1H y RSI
    r_klines = requests.get(f"{FUTURES_BASE}/fapi/v1/klines?symbol={SYMBOL}&interval=1h&limit=50", timeout=10).json()
    closes = [float(k[4]) for k in r_klines]
    current_price = closes[-1]
    rsi14 = calculate_rsi(closes, 14)

    # 4. Gestion de Posicion Activa
    if open_position:
        is_long = open_position["amt"] > 0
        entry = open_position["entryPrice"]
        qty = abs(open_position["amt"])
        exit_side = "SELL" if is_long else "BUY"
        diff_pct = (current_price - entry) / entry if is_long else (entry - current_price) / entry
        pnl = open_position["pnl"]

        # Take Profit (+3.0%)
        if diff_pct >= TP_PCT:
            fapi_request("/fapi/v1/order", "POST", {
                "symbol": SYMBOL, "side": exit_side, "type": "MARKET", "quantity": f"{qty:.2f}", "reduceOnly": "true"
            })
            consecutive_losses = 0
            send_telegram(f"<b>[CIERRE EN GANANCIA] TAKE PROFIT ALCANZADO</b>\n\n• Ganancia neta: <b>+${pnl:.2f} USDT</b> (+{(diff_pct * 100 * LEVERAGE):.1f}% ROE)\n• Precio de salida: ${current_price:.2f}\n• Saldo consolidado: <b>${(total_balance + pnl):.2f} USDT</b>")
        
        # Stop Loss (-1.5%)
        elif diff_pct <= -SL_PCT:
            fapi_request("/fapi/v1/order", "POST", {
                "symbol": SYMBOL, "side": exit_side, "type": "MARKET", "quantity": f"{qty:.2f}", "reduceOnly": "true"
            })
            consecutive_losses += 1
            if consecutive_losses >= 2:
                cooldown_until = time.time() + (COOLDOWN_HOURS * 3600)
                send_telegram(f"<b>[PROTECCION DE CAPITAL] STOP LOSS EJECUTADO</b>\n\n• Perdida limitada: -${abs(pnl):.2f} USDT\n• Saldo protegido: <b>${(total_balance + pnl):.2f} USDT</b>\n\n<i>Disyuntor Activado: 2 perdidas consecutivas. Pausa de 4 horas para proteger capital.</i>")
            else:
                send_telegram(f"<b>[PROTECCION DE CAPITAL] STOP LOSS EJECUTADO</b>\n\n• Perdida limitada: -${abs(pnl):.2f} USDT\n• Saldo protegido: <b>${(total_balance + pnl):.2f} USDT</b>")
        else:
            print(f"[MONITOR] SOL @ ${current_price:.2f} | Posicion: {qty} SOL | PnL: ${pnl:.2f} USDT")

    # 5. Evaluar Entrada
    else:
        if time.time() < cooldown_until:
            mins_left = int((cooldown_until - time.time()) / 60)
            print(f"[COOLDOWN] Restan {mins_left} minutos de pausa.")
            return

        if available_balance >= MARGIN_PER_TRADE:
            try:
                fapi_request("/fapi/v1/marginType", "POST", {"symbol": SYMBOL, "marginType": "ISOLATED"})
            except:
                pass
            try:
                fapi_request("/fapi/v1/leverage", "POST", {"symbol": SYMBOL, "leverage": str(LEVERAGE)})
            except:
                pass

            if 30.0 <= rsi14 <= 48.0:
                notional = MARGIN_PER_TRADE * LEVERAGE
                qty = round((notional / current_price), 1)
                if qty >= 0.1:
                    fapi_request("/fapi/v1/order", "POST", {
                        "symbol": SYMBOL, "side": "BUY", "type": "MARKET", "quantity": f"{qty:.2f}"
                    })
                    tp_price = current_price * (1 + TP_PCT)
                    sl_price = current_price * (1 - SL_PCT)
                    send_telegram(f"<b>[NUEVA ORDEN EJECUTADA] FUTUROS 5X SOL</b>\n\n• Posicion: <b>LONG</b>\n• Volumen: <b>{qty} SOL</b> (~${notional:.2f})\n• Margen: <b>${MARGIN_PER_TRADE} USDT (Aislado)</b>\n• RSI 1h: <b>{rsi14:.1f}</b>\n\n• Meta Take Profit: <b>${tp_price:.2f}</b> (+3.0% / +$3.75 USDT)\n• Stop Loss Estricto: <b>${sl_price:.2f}</b> (-1.5% / -$1.87 USDT)\n\n<i>Monitoreo continuo activo en Koyeb Cloud.</i>")
            else:
                print(f"[SCAN] RSI 1h: {rsi14:.1f} (Esperando zona 30-48)")

if __name__ == "__main__":
    send_telegram("<b>[SISTEMA INICIADO]</b> Motor Cuantitativo 5x activo en Koyeb Cloud. Monitoreo continuo 24/7 sin limites de ejecucion.")
    print("[SYS] Motor iniciado exitosamente.")
    while True:
        try:
            run_engine_cycle()
        except Exception as e:
            print(f"[ERROR CICLO] {e}")
        time.sleep(60)  # Revisa cada 60 segundos
