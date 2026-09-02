import os
import time
from dotenv import load_dotenv, find_dotenv
import okx.MarketData as MarketData
import okx.Trade as Trade
import okx.Account as Account

# Cargar claves buscando automáticamente el archivo .env en el directorio o superiores
load_dotenv(find_dotenv())

API_KEY = os.getenv("OKX_API_KEY", "").strip()
API_SECRET = os.getenv("OKX_API_SECRET", "").strip()
API_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()
FLAG = os.getenv("OKX_FLAG", "0").strip()  # 0 = real, 1 = demo
DOMAIN = os.getenv("OKX_DOMAIN", "https://eea.okx.com").strip()

if not API_KEY or not API_SECRET or not API_PASSPHRASE:
    raise ValueError(
        "Faltan credenciales de OKX. Asegúrate de que tu archivo .env contenga: "
        "OKX_API_KEY, OKX_API_SECRET y OKX_PASSPHRASE."
    )

INST_ID = "DOGE-USDT-SWAP"
TD_MODE = "cross"

SL_PCT = 0.01      # 1% SL
TP_PCT = 0.015     # 1.5% TP
FIXED_SZ = 50      # tamaño fijo conservador

# Inicialización con soporte de dominio EEA para Europa
try:
    market_api = MarketData.MarketAPI(flag=FLAG, domain=DOMAIN)
    trade_api = Trade.TradeAPI(API_KEY, API_SECRET, API_PASSPHRASE, flag=FLAG, domain=DOMAIN)
    account_api = Account.AccountAPI(API_KEY, API_SECRET, API_PASSPHRASE, flag=FLAG, domain=DOMAIN)
except TypeError:
    market_api = MarketData.MarketAPI(flag=FLAG)
    trade_api = Trade.TradeAPI(API_KEY, API_SECRET, API_PASSPHRASE, flag=FLAG)
    account_api = Account.AccountAPI(API_KEY, API_SECRET, API_PASSPHRASE, flag=FLAG)


def get_last_price():
    res = market_api.get_ticker(instId=INST_ID)
    data = res.get("data", [])
    return float(data[0]["last"])


def get_klines(limit=100, bar="1m"):
    res = market_api.get_candlesticks(instId=INST_ID, bar=bar, limit=str(limit))
    data = res.get("data", [])
    closes = [float(c[4]) for c in reversed(data)]
    return closes


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def get_position():
    res = account_api.get_positions(instId=INST_ID)
    data = res.get("data", [])
    if not data:
        return None
    return data[0]


def place_order(side, pos_side, price):
    sz = FIXED_SZ

    if pos_side == "long":
        sl_px = price * (1 - SL_PCT)
        tp_px = price * (1 + TP_PCT)
    else:
        sl_px = price * (1 + SL_PCT)
        tp_px = price * (1 - TP_PCT)

    print(f"ENTRADA {pos_side.upper()} | Precio={price} | SL={sl_px} | TP={tp_px}")

    # Ejecución de orden de mercado estándar compatible con el SDK
    res = trade_api.place_order(
        instId=INST_ID,
        tdMode=TD_MODE,
        side=side,
        ordType="market",
        sz=str(sz),
        posSide=pos_side
    )

    print("Orden enviada:", res)
    print("Esperando 2 minutos tras abrir operación...")
    time.sleep(120)


def strategy_step():
    pos = get_position()
    if pos:
        print("Ya hay una posición abierta. No se abre otra.")
        return

    closes = get_klines(limit=100, bar="1m")
    price = closes[-1]

    print(f"Precio actual OKX (DOGE-USDT-SWAP): {price}")

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    print(f"EMA20={ema20} | EMA50={ema50}")

    if ema20 > ema50:
        place_order("buy", "long", price)
    elif ema20 < ema50:
        place_order("sell", "short", price)
    else:
        print("Sin señal clara. EMAs iguales.")


def main():
    print("Bot DOGE-USDT-SWAP conservador iniciado.")
    strategy_step()


if __name__ == "__main__":
    main()
