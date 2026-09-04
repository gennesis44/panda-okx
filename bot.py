import os
import time

import ccxt
import pandas as pd

# ==================== CONFIGURACIÓN ====================
SYMBOL           = 'DOGE/USD:DOGE'   # swap DOGE en OKX
TIMEFRAME        = '15m'
AMOUNT           = 136               # contratos por entrada
SL_PCT           = 0.010             # Stop Loss   1.0%
TP_PCT           = 0.015             # Take Profit 1.5%
MAX_ENTRIES      = 2                 # máx. entradas acumuladas por dirección
WAIT_AFTER_ENTRY = 120               # 2 min de espera tras entrar
TD_MODE          = 'cross'           # 'cross' o 'isolated'
HEDGE_MODE       = False             # True solo si tu cuenta OKX está en modo cobertura

exchange_public = ccxt.okx({
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})


def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def get_trade_exchange():
    api_key  = os.getenv('OKX_API_KEY')
    secret   = os.getenv('OKX_SECRET_KEY')
    password = os.getenv('OKX_PASSWORD')

    # Fail-fast: si falta alguna variable, el error es claro (no un 50119 críptico)
    faltantes = [nombre for nombre, valor in
                 [('OKX_API_KEY', api_key),
                  ('OKX_SECRET_KEY', secret),
                  ('OKX_PASSWORD', password)] if not valor]
    if faltantes:
        raise RuntimeError(
            f"Variables de entorno VACÍAS: {', '.join(faltantes)}. "
            "Revisa el bloque env: de bot.yml (mapeo de secrets) y haz commit en main."
        )

    return ccxt.okx({
        'apiKey':    api_key,
        'secret':    secret,
        'password':  password,
        'enableRateLimit': True,
        'options':   {'defaultType': 'swap'},
    })


def verify_credentials(exchange):
    """Login real contra OKX antes de operar. Falla rápido y con diagnóstico."""
    try:
        bal = exchange.fetch_balance()
        usdt = (bal.get('USDT') or {}).get('free')
        print(f"[{now()}] OK: Autenticación correcta | USDT libre: {usdt}")
    except ccxt.AuthenticationError as e:
        print(f"[{now()}] ERROR de autenticación OKX: {e}")
        print("  Causas típicas del 50119 ('API key doesn't exist'):")
        print("  1) El secret contiene una key vieja/borrada o de DEMO (no vale para real).")
        print("  2) Comillas/espacios/saltos de línea pegados en el valor del secret.")
        print("  3) La API key no tiene permiso Trade o tiene whitelist de IP.")
        raise


# ==================== SEÑAL ====================
def get_signal():
    """Devuelve ('long' | 'short' | None, precio_actual)."""
    ohlcv = exchange_public.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    df['ema7']  = df['close'].ewm(span=7,  adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()

    prev7, prev21 = df['ema7'].iloc[-2], df['ema21'].iloc[-2]
    curr7, curr21 = df['ema7'].iloc[-1], df['ema21'].iloc[-1]

    ticker = exchange_public.fetch_ticker(SYMBOL)
    price = ticker.get('last') or df['close'].iloc[-1]

    print(f"[{now()}] Precio: {price} | EMA7: {curr7:.5f} | EMA21: {curr21:.5f}")

    if prev7 <= prev21 and curr7 > curr21:
        return 'long', price
    if prev7 >= prev21 and curr7 < curr21:
        return 'short', price
    return None, price


# ==================== POSICIÓN ====================
def get_position(exchange):
    """Devuelve (side, contratos) de la posición en SYMBOL. (None, 0) si está flat."""
    positions = exchange.fetch_positions([SYMBOL])
    for p in positions:
        contracts = p.get('contracts') or 0
        if contracts > 0:
            return p.get('side'), contracts
    return None, 0


def close_position(exchange, side, contracts):
    """Cierra la posición a mercado (flat)."""
    close_side = 'sell' if side == 'long' else 'buy'
    amount = exchange.amount_to_precision(SYMBOL, contracts)
    params = {'tdMode': TD_MODE, 'reduceOnly': True}
    if HEDGE_MODE:
        params['posSide'] = side
    exchange.create_order(SYMBOL, 'market', close_side, amount, params=params)
    print(f"[{now()}] Posición {side} cerrada ({amount} contratos). Flat.")


# ==================== ENTRADA CON SL/TP ====================
def open_entry(exchange, signal, ref_price):
    """Abre entrada a mercado con SL (1%) y TP (1.5%) adjuntos."""
    if signal == 'long':
        side, sl_raw, tp_raw = 'buy', ref_price * (1 - SL_PCT), ref_price * (1 + TP_PCT)
    else:
        side, sl_raw, tp_raw = 'sell', ref_price * (1 + SL_PCT), ref_price * (1 - TP_PCT)

    sl = exchange.price_to_precision(SYMBOL, sl_raw)
    tp = exchange.price_to_precision(SYMBOL, tp_raw)

    params = {
        'tdMode': TD_MODE,
        # SL/TP adjuntos: los gestiona OKX en servidor y se anulan solos al cerrar
        'stopLossPrice':   float(sl),
        'takeProfitPrice': float(tp),
    }
    if HEDGE_MODE:
        params['posSide'] = 'long' if signal == 'long' else 'short'

    order = exchange.create_order(SYMBOL, 'market', side, AMOUNT, params=params)
    print(f"[{now()}] Orden {side.upper()} enviada. ID: {order.get('id')}")

    entry = order.get('average')
    if not entry:
        for _ in range(5):
            time.sleep(1)
            try:
                o = exchange.fetch_order(order['id'], SYMBOL)
                if o.get('average'):
                    entry = o['average']
                    break
            except ccxt.ExchangeError:
                continue
    entry = entry or ref_price

    print(f"[{now()}] Entrada: {entry} | SL: {sl} | TP: {tp}")
    return entry


# ==================== CICLO ====================
def run_cycle(exchange):
    """Ejecuta un ciclo de análisis. Devuelve True si abrió posición."""
    signal, price = get_signal()
    if signal is None:
        print(f"[{now()}] Sin cruces nuevos en este ciclo. Vigilando.")
        return False

    side, contracts = get_position(exchange)

    if side and side != signal:
        close_position(exchange, side, contracts)
        side, contracts = None, 0
        time.sleep(2)

    if side == signal:
        entries_done = round(contracts / AMOUNT)
        if entries_done >= MAX_ENTRIES:
            print(f"[{now()}] Ya hay {entries_done} entradas en {signal}. Máximo alcanzado.")
            return False

    cross = 'Golden Cross' if signal == 'long' else 'Death Cross'
    print(f"[{now()}] ¡{cross} (7/21) detectado! Lanzando orden de ataque {signal.upper()}...")
    open_entry(exchange, signal, price)
    return True


def sleep_until_next_candle():
    period_ms = exchange_public.parse_timeframe(TIMEFRAME) * 1000
    now_ms = time.time() * 1000
    next_ms = (int(now_ms // period_ms) + 1) * period_ms
    time.sleep(max(1.0, (next_ms - now_ms) / 1000))


# ==================== MODOS DE EJECUCIÓN ====================
def run_once():
    """Un solo ciclo y sale. Para GitHub Actions (SINGLE_CYCLE=1)."""
    print(f"[{now()}] Modo ciclo único (GitHub Actions).")
    exchange = get_trade_exchange()
    verify_credentials(exchange)
    entered = run_cycle(exchange)
    if entered:
        print(f"[{now()}] Espera post-entrada: {WAIT_AFTER_ENTRY}s")
        time.sleep(WAIT_AFTER_ENTRY)


def main():
    """Bucle infinito. Para VPS / ejecución local."""
    print(f"[{now()}] Bot EMA 7/21 | {SYMBOL} {TIMEFRAME} | SL {SL_PCT:.1%} / TP {TP_PCT:.1%} | "
          f"máx {MAX_ENTRIES} entradas por dirección")
    exchange = get_trade_exchange()
    verify_credentials(exchange)
    while True:
        try:
            entered = run_cycle(exchange)
            if entered:
                print(f"[{now()}] Espera post-entrada: {WAIT_AFTER_ENTRY}s")
                time.sleep(WAIT_AFTER_ENTRY)
        except KeyboardInterrupt:
            print(f"[{now()}] Bot detenido por el usuario.")
            break
        except ccxt.AuthenticationError as e:
            print(f"[{now()}] Autenticación rechazada: {e}")
        except ccxt.NetworkError as e:
            print(f"[{now()}] Error de red: {e}")
        except ccxt.ExchangeError as e:
            print(f"[{now()}] Error del exchange: {e}")
        except Exception as e:
            print(f"[{now()}] Error crítico durante la ejecución: {e}")
        sleep_until_next_candle()


if __name__ == '__main__':
    if os.getenv('SINGLE_CYCLE') == '1':
        run_once()
    else:
        main()
