import os
import time

import ccxt
import pandas as pd

# ==================== CONFIGURACIÓN ====================
DIAG_VERSION     = 'v3'
SYMBOL           = 'DOGE/USD:DOGE'   # swap perpetuo DOGE (objetivo original)
TIMEFRAME        = '15m'
AMOUNT           = 3                 # contratos por entrada
SL_PCT           = 0.010             # Stop Loss 1.0%
TP_PCT           = 0.015             # Take Profit 1.5%
MAX_ENTRIES      = 2                 # máx. entradas acumuladas por dirección
WAIT_AFTER_ENTRY = 120               # 2 min de espera tras entrar
SIGNAL_ON_CLOSE  = True              # True: cruce con velas CERRADAS
TD_MODE          = 'cross'
HEDGE_MODE       = False

HOST_CANDIDATES = [
    'https://my.okx.com',      # <- host confirmado de tu cuenta
    'https://www.okx.com',
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
        raise RuntimeError(f"Variables VACIAS: {', '.join(faltantes)}.")
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
    ultimo = None
    for host in HOST_CANDIDATES:
        try:
            ex = build_exchange(host)
            ex.private_get_account_balance()
            print(f"[{now()}] HOST OKX detectado: {host}")
        except ccxt.AuthenticationError:
            print(f"[{now()}] {host} -> key rechazada")
            ultimo = 'autenticacion'
            continue
        except Exception as e:
            print(f"[{now()}] {host} -> no disponible ({str(e)[:80]})")
            ultimo = str(e)[:200]
            continue
        try:
            bal = ex.fetch_balance()
            doge = (bal.get('DOGE') or {}).get('free')
            usd  = (bal.get('USD') or {}).get('free')
            usdc = (bal.get('USDC') or {}).get('free')
            print(f"[{now()}] OK: Autenticacion correcta")
            print(f"[{now()}] Saldos trading -> DOGE: {doge} | USD: {usd} | USDC: {usdc}")
        except Exception as e:
            print(f"[{now()}] Autenticacion OK (aviso mercados: {str(e)[:80]})")
        return ex
    raise RuntimeError("Ningun host OKX reconoce la key. Ultimo fallo: " + str(ultimo))


# ==================== SEÑAL ====================
def get_signal(exchange, symbol=None):
    sym = symbol or SYMBOL
    ohlcv = exchange.fetch_ohlcv(sym, TIMEFRAME, limit=100)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    df['ema7']  = df['close'].ewm(span=7,  adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()

    if SIGNAL_ON_CLOSE:
        i_prev, i_curr = -3, -2
    else:
        i_prev, i_curr = -2, -1

    prev7, prev21 = df['ema7'].iloc[i_prev], df['ema21'].iloc[i_prev]
    curr7, curr21 = df['ema7'].iloc[i_curr], df['ema21'].iloc[i_curr]

    ticker = exchange.fetch_ticker(sym)
    price = ticker.get('last') or df['close'].iloc[-1]

    print(f"[{now()}] Precio: {price} | EMA7: {curr7:.5f} | EMA21: {curr21:.5f}")

    if prev7 <= prev21 and curr7 > curr21:
        return 'long', price
    if prev7 >= prev21 and curr7 < curr21:
        return 'short', price
    return None, price


# ==================== POSICIÓN ====================
def get_position(exchange, symbol=None):
    positions = exchange.fetch_positions([symbol or SYMBOL])
    for p in positions:
        contracts = p.get('contracts') or 0
        if contracts > 0:
            return p.get('side'), contracts
    return None, 0


def close_position(exchange, side, contracts, symbol=None):
    sym = symbol or SYMBOL
    close_side = 'sell' if side == 'long' else 'buy'
    amount = exchange.amount_to_precision(sym, contracts)
    params = {'tdMode': TD_MODE, 'reduceOnly': True}
    if HEDGE_MODE:
        params['posSide'] = side
    exchange.create_order(sym, 'market', close_side, amount, params=params)
    print(f"[{now()}] Posicion {side} cerrada ({amount} contratos). Flat.")


# ==================== ENTRADA CON SL/TP ====================
def open_entry(exchange, signal, ref_price, symbol=None):
    sym = symbol or SYMBOL
    if signal == 'long':
        side, sl_raw, tp_raw = 'buy', ref_price * (1 - SL_PCT), ref_price * (1 + TP_PCT)
    else:
        side, sl_raw, tp_raw = 'sell', ref_price * (1 + SL_PCT), ref_price * (1 - TP_PCT)

    sl = exchange.price_to_precision(sym, sl_raw)
    tp = exchange.price_to_precision(sym, tp_raw)

    market = exchange.market(sym)
    ctval = float(market.get('contractSize') or 1)
    print(f"[{now()}] Tamano orden: {AMOUNT} contratos x {ctval} DOGE/contrato "
          f"= {AMOUNT * ctval} DOGE (~{AMOUNT * ctval * ref_price:.2f} USD)")

    params = {
        'tdMode': TD_MODE,
        'stopLossPrice':   float(sl),
        'takeProfitPrice': float(tp),
    }
    if HEDGE_MODE:
        params['posSide'] = 'long' if signal == 'long' else 'short'

    order = exchange.create_order(sym, 'market', side, AMOUNT, params=params)
    print(f"[{now()}] Orden {side.upper()} enviada. ID: {order.get('id')}")

    entry = order.get('average')
    if not entry:
        for _ in range(5):
            time.sleep(1)
            try:
                o = exchange.fetch_order(order['id'], sym)
                if o.get('average'):
                    entry = o['average']
                    break
            except ccxt.ExchangeError:
                continue
    entry = entry or ref_price

    print(f"[{now()}] Entrada: {entry} | SL: {sl} | TP: {tp}")
    return entry


# ==================== DIAGNÓSTICO v3 ====================
def _clasifica(err):
    e = err.lower()
    if '50124' in err:
        return 'KEY SIN PERMISO (50124)'
    if '50123' in err:
        return 'KEY SIN PERMISO CRIPTO (50123)'
    if '51155' in err:
        return 'CUENTA BLOQUEA (51155)'
    codigos_fondos = ['51119', '51124', '51125', '51159', '51169', '51170',
                      '51095', '51203', '51186', '51185']
    if any(c in err for c in codigos_fondos) or any(k in e for k in
            ['insufficient', 'margin', 'balance', 'funds', 'equity', 'leverage']):
        return 'PERMISO OK (falta margen/fondos)'
    return err[:100]


def run_test(exchange):
    """v3: (A) reconfirma spot DOGE/USD, (B) SONDA CLAVE: futuro con vencimiento,
    (C) convert DOGE->USD (para sembrar margen si el futuro es operable)."""
    exchange.load_markets()
    print(f"[{now()}] ===== DIAGNOSTICO {DIAG_VERSION} =====")

    print(f"[{now()}] --- SONDA A: SPOT DOGE/USD (vender 10 DOGE) ---")
    try:
        amt = exchange.amount_to_precision('DOGE/USD', 10)
        o = exchange.create_order('DOGE/USD', 'market', 'sell', amt)
        print(f"[{now()}] SONDA A OK: vendidos {amt} DOGE. ID: {o.get('id')}")
        print(f"  -> Clasificacion: SPOT OPERABLE")
    except Exception as e:
        err = str(e)
        print(f"[{now()}] SONDA A FALLO -> {err[:150]}")
        print(f"  -> Clasificacion: {_clasifica(err)}")

    print(f"[{now()}] --- SONDA B (CLAVE): FUTURO DOGE con vencimiento (buy 1) ---")
    fut_sym = None
    for m in exchange.markets.values():
        if m.get('base') == 'DOGE' and m.get('future') and m.get('active'):
            fut_sym = m['symbol']
            break
    fut_estado = 'SIN INSTRUMENTO'
    if fut_sym:
        print(f"[{now()}] Futuro: {fut_sym} | ctVal={exchange.market(fut_sym)['info'].get('ctVal')}")
        try:
            o = exchange.create_order(fut_sym, 'market', 'buy', 1, params={'tdMode': TD_MODE})
            print(f"[{now()}] SONDA B OK: buy 1 contrato. ID: {o.get('id')}")
            fut_estado = 'OPERABLE'
            time.sleep(3)
            side, contracts = get_position(exchange, fut_sym)
            if side:
                close_position(exchange, side, contracts, fut_sym)
        except Exception as e:
            err = str(e)
            print(f"[{now()}] SONDA B FALLO -> {err[:150]}")
            fut_estado = _clasifica(err)
    else:
        print(f"[{now()}] No hay futuros DOGE activos.")

    print(f"[{now()}] --- SONDA C: CONVERT DOGE->USD (solo cotizacion, no ejecuta) ---")
    try:
        q = exchange.privatePostTradeConvertQuote({
            'ccy1': 'DOGE', 'ccy2': 'USD', 'side': 'sell',
            'rfqSz': '10', 'rfqSzCcy': 'DOGE',
        })
        px = q.get('pxE8') or q.get('px') or q.get('quotePx')
        print(f"[{now()}] SONDA C OK: convert disponible. Cotizacion 10 DOGE -> USD (px={px})")
        print(f"  -> Convert puede sembrar margen USD si el futuro es operable.")
    except Exception as e:
        print(f"[{now()}] SONDA C FALLO -> {str(e)[:150]}")

    print(f"[{now()}] ================= LECTURA FINAL =================")
    print(f"  SPOT DOGE/USD        : (ver SONDA A arriba)")
    print(f"  FUTURO con vencimiento: {fut_estado}")
    if fut_estado == 'OPERABLE' or 'PERMISO OK' in fut_estado:
        print("  ==> CAMINO A: bot sobre FUTUROS (long/short).")
        print("      Necesitamos margen USD: si SONDA C es OK, convertimos ~15 DOGE->USD;")
        print("      si no, deposita USD o usa el convert manual de la app.")
    elif 'CUENTA BLOQUEA' in fut_estado or '50124' in fut_estado:
        print("  ==> Futuros tambien bloqueados. Ruta: crear la key desde PC")
        print("      buscando la casilla Perpetuos/Futuros, o soporte OKX con el 50124.")
        print("      Alternativa mientras: Plan B spot DOGE/USDC long-only (si USDC pasa).")
    else:
        print("  ==> Revisar salida de las sondas arriba.")


# ==================== CICLO ====================
def run_cycle(exchange):
    signal, price = get_signal(exchange)
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
    print(f"[{now()}] Modo ciclo unico (GitHub Actions) | diag {DIAG_VERSION}.")
    exchange = detect_exchange()
    if os.getenv('TEST_MODE') == '1':
        run_test(exchange)
        return
    entered = run_cycle(exchange)
    if entered:
        print(f"[{now()}] Espera post-entrada: {WAIT_AFTER_ENTRY}s")
        time.sleep(WAIT_AFTER_ENTRY)


def main():
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
