import os
import time

import ccxt
import pandas as pd

# ==================== CONFIGURACIÓN ====================
SYMBOL           = 'DOGE/USD:DOGE'   # swap perpetuo DOGE en OKX
TIMEFRAME        = '15m'
AMOUNT           = 136               # contratos por entrada
SL_PCT           = 0.010             # Stop Loss 1.0%
TP_PCT           = 0.015             # Take Profit 1.5%
MAX_ENTRIES      = 2                 # máx. entradas acumuladas por dirección
WAIT_AFTER_ENTRY = 120               # 2 min de espera tras entrar
SIGNAL_ON_CLOSE  = True              # True: cruce con velas CERRADAS (sin repintado)
TD_MODE          = 'cross'           # 'cross' o 'isolated'
HEDGE_MODE       = False             # True solo si la cuenta OKX está en modo cobertura

# Cuentas creadas en my.okx.com viven en una instancia separada de www.okx.com.
# El bot prueba los dominios oficiales de OKX y opera contra el que reconozca la key.
HOST_CANDIDATES = [
    'https://www.okx.com',
    'https://my.okx.com',
    'https://aws.okx.com',
    'https://www.okx.eu',
    'https://www.okx.us',
]


def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _creds():
    api_key  = (os.getenv('OKX_API_KEY') or '').strip()
    secret   = (os.getenv('OKX_SECRET_KEY') or '').strip()
    password = (os.getenv('OKX_PASSWORD') or '').strip()
    faltantes = [n for n, v in [('OKX_API_KEY', api_key),
                                ('OKX_SECRET_KEY', secret),
                                ('OKX_PASSWORD', password)] if not v]
    if faltantes:
        raise RuntimeError(f"Variables VACIAS: {', '.join(faltantes)}. Revisa el env del workflow.")
    return api_key, secret, password


def build_exchange(host):
    api_key, secret, password = _creds()
    return ccxt.okx({
        'apiKey':    api_key,
        'secret':    secret,
        'password':  password,
        'enableRateLimit': True,
        'options':   {'defaultType': 'swap'},
        'urls':      {'api': {'rest': host}},
    })


def detect_exchange():
    """Prueba los dominios oficiales de OKX y devuelve el exchange del primer
    host que reconozca la API key (verificado con login real)."""
    ultimo = None
    for host in HOST_CANDIDATES:
        try:
            ex = build_exchange(host)
            ex.private_get_account_balance()  # endpoint privado: solo pasa con credenciales válidas
            print(f"[{now()}] HOST OKX detectado: {host}")
        except ccxt.AuthenticationError:
            print(f"[{now()}] {host} -> key rechazada (no existe en esta instancia)")
            ultimo = 'autenticacion'
            continue
        except Exception as e:
            print(f"[{now()}] {host} -> no disponible ({str(e)[:80]})")
            ultimo = str(e)[:200]
            continue
        try:
            bal = ex.fetch_balance()
            usdt = (bal.get('USDT') or {}).get('free')
            print(f"[{now()}] OK: Autenticacion correcta | USDT libre: {usdt}")
        except Exception as e:
            print(f"[{now()}] Autenticacion OK (aviso cargando mercados: {str(e)[:80]})")
        return ex
    raise RuntimeError(
        "Ningun host OKX reconoce la API key. "
        "Verifica que la key es de TRADING REAL y esta activa. Ultimo fallo: " + str(ultimo)
    )


# ==================== SEÑAL ====================
def get_signal(exchange):
    """Devuelve ('long' | 'short' | None, precio_actual)."""
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    df['ema7']  = df['close'].ewm(span=7,  adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()

    if SIGNAL_ON_CLOSE:
        i_prev, i_curr = -3, -2      # dos últimas velas CERRADAS
    else:
        i_prev, i_curr = -2, -1      # vela en curso

    prev7, prev21 = df['ema7'].iloc[i_prev], df['ema21'].iloc[i_prev]
    curr7, curr21 = df['ema7'].iloc[i_curr], df['ema21'].iloc[i_curr]

    ticker = exchange.fetch_ticker(SYMBOL)
    price = ticker.get('last') or df['close'].iloc[-1]

    print(f"[{now()}] Precio: {price} | EMA7: {curr7:.5f} | EMA21: {curr21:.5f}")

    if prev7 <= prev21 and curr7 > curr21:
        return 'long', price
    if prev7 >= prev21 and curr7 < curr21:
        return 'short', price
    return None, price


# ==================== POSICIÓN ====================
def get_position(exchange):
    """Devuelve (side, contratos). (None, 0) si está flat."""
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
    print(f"[{now()}] Posicion {side} cerrada ({amount} contratos). Flat.")


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
        # SL/TP adjuntos: OKX los ejecuta en servidor y se anulan solos al cerrar
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
    """Ejecuta un ciclo. Devuelve True si abrió posición."""
    signal, price = get_signal(exchange)
    if signal is None:
        print(f"[{now()}] Sin cruces nuevos en este ciclo. Vigilando.")
        return False

    side, contracts = get_position(exchange)

    # Cruce contrario con posición abierta -> cerrar (flat) antes de girar
    if side and side != signal:
        close_position(exchange, side, contracts)
        side, contracts = None, 0
        time.sleep(2)

    # Máximo 2 entradas acumuladas en la misma dirección
    if side == signal:
        entries_done = round(contracts / AMOUNT)
        if entries_done >= MAX_ENTRIES:
            print(f"[{now()}] Ya hay {entries_done} entradas en {signal}. Maximo alcanzado.")
            return False

    cross = 'Golden Cross' if signal == 'long' else 'Death Cross'
    print(f"[{now()}] {cross} (7/21) detectado. Lanzando orden {signal.upper()}...")
    open_entry(exchange, signal, price)
    return True


def sleep_until_next_candle():
    unit = TIMEFRAME[-1]
    secs = int(TIMEFRAME[:-1]) * {'m': 60, 'h': 3600, 'd': 86400}.get(unit, 60)
    period_ms = secs * 1000
    now_ms = time.time() * 1000
    next_ms = (int(now_ms // period_ms) + 1) * period_ms
    time.sleep(max(1.0, (next_ms - now_ms) / 1000))


# ==================== MODOS DE EJECUCIÓN ====================
def run_once():
    """Un ciclo y sale. GitHub Actions (SINGLE_CYCLE=1)."""
    print(f"[{now()}] Modo ciclo unico (GitHub Actions).")
    exchange = detect_exchange()
    entered = run_cycle(exchange)
    if entered:
        print(f"[{now()}] Espera post-entrada: {WAIT_AFTER_ENTRY}s")
        time.sleep(WAIT_AFTER_ENTRY)


def main():
    """Bucle infinito. VPS / local."""
    print(f"[{now()}] Bot EMA 7/21 | {SYMBOL} {TIMEFRAME} | SL {SL_PCT:.1%} / TP {TP_PCT:.1%}")
    exchange = detect_exchange()
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
            print(f"[{now()}] Autenticacion rechazada: {e}")
        except ccxt.NetworkError as e:
            print(f"[{now()}] Error de red: {e}")
        except ccxt.ExchangeError as e:
            print(f"[{now()}] Error del exchange: {e}")
        except Exception as e:
            print(f"[{now()}] Error critico: {e}")
        sleep_until_next_candle()


if __name__ == '__main__':
    if os.getenv('SINGLE_CYCLE') == '1':
        run_once()
    else:
        main()
