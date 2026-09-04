import os
import time

import ccxt
import pandas as pd

# ==================== CONFIGURACIÓN ====================
SYMBOL           = 'DOGE/USD:DOGE'   # swap perpetuo DOGE (margen en DOGE)
TIMEFRAME        = '15m'
AMOUNT           = 3                 # contratos por entrada
SL_PCT           = 0.010             # Stop Loss 1.0%
TP_PCT           = 0.015             # Take Profit 1.5%
MAX_ENTRIES      = 2                 # máx. entradas acumuladas por dirección
WAIT_AFTER_ENTRY = 120               # 2 min de espera tras entrar
SIGNAL_ON_CLOSE  = True              # True: cruce con velas CERRADAS (sin repintado)
TD_MODE          = 'cross'           # 'cross' o 'isolated'
HEDGE_MODE       = False             # True solo si la cuenta OKX está en modo cobertura

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
    ultimo = None
    for host in HOST_CANDIDATES:
        try:
            ex = build_exchange(host)
            ex.private_get_account_balance()
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
            doge = (bal.get('DOGE') or {}).get('free')
            print(f"[{now()}] OK: Autenticacion correcta")
            print(f"[{now()}] Saldos (cuenta trading) -> USDT: {usdt} | DOGE: {doge}")
        except Exception as e:
            print(f"[{now()}] Autenticacion OK (aviso cargando mercados: {str(e)[:80]})")
        return ex
    raise RuntimeError(
        "Ningun host OKX reconoce la API key. Ultimo fallo: " + str(ultimo)
    )


# ==================== SEÑAL ====================
def get_signal(exchange):
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    df['ema7']  = df['close'].ewm(span=7,  adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()

    if SIGNAL_ON_CLOSE:
        i_prev, i_curr = -3, -2
    else:
        i_prev, i_curr = -2, -1

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
def open_entry(exchange, signal, ref_price):
    if signal == 'long':
        side, sl_raw, tp_raw = 'buy', ref_price * (1 - SL_PCT), ref_price * (1 + TP_PCT)
    else:
        side, sl_raw, tp_raw = 'sell', ref_price * (1 + SL_PCT), ref_price * (1 - TP_PCT)

    sl = exchange.price_to_precision(SYMBOL, sl_raw)
    tp = exchange.price_to_precision(SYMBOL, tp_raw)

    market = exchange.market(SYMBOL)
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


# ==================== DIAGNÓSTICO DE PERMISOS ====================
def run_test(exchange):
    """Diagnostico adaptativo: cataloga los instrumentos DOGE reales de la
    plataforma y lanza sondas minimas contra los que existan."""
    exchange.load_markets()

    print(f"[{now()}] --- CATALOGO DOGE en esta plataforma ---")
    spot_syms, swap_syms, fut_syms = [], [], []
    for m in exchange.markets.values():
        if m.get('base') != 'DOGE':
            continue
        info = m.get('info') or {}
        itype = info.get('instType') or ('SPOT' if m.get('spot') else '?')
        print(f"  {m['symbol']} | tipo={itype} | activo={m.get('active')} | "
              f"ctVal={info.get('ctVal')} | settle={info.get('settleCcy') or info.get('quoteCcy')}")
        if m.get('active'):
            if m.get('spot'):
                spot_syms.append(m['symbol'])
            elif m.get('swap'):
                swap_syms.append(m['symbol'])
            elif m.get('future'):
                fut_syms.append(m['symbol'])

    # --- SONDA SPOT (vender ~10 DOGE -> divisa de cotizacion, inofensivo) ---
    print(f"[{now()}] --- SONDA SPOT ---")
    spot_ok = False
    if not spot_syms:
        print("  No hay mercados spot DOGE en esta plataforma.")
    else:
        for sym in spot_syms:
            try:
                m = exchange.market(sym)
                min_amt = (m.get('limits', {}).get('amount') or {}).get('min') or 10
                amt = exchange.amount_to_precision(sym, max(float(min_amt), 10))
                o = exchange.create_order(sym, 'market', 'sell', amt)
                print(f"[{now()}] SPOT OK: vendidos {amt} DOGE en {sym}. ID: {o.get('id')}")
                spot_ok = True
                break
            except Exception as e:
                print(f"[{now()}] SPOT {sym} -> FALLO: {str(e)[:150]}")

    # --- SONDA SWAP (buy 1 contrato en cada swap DOGE existente) ---
    print(f"[{now()}] --- SONDA SWAP (1 contrato) ---")
    swap_resultados = {}
    if not swap_syms:
        print("  NO existe ningun swap perpetuo DOGE en esta plataforma (hipotesis C).")
    for sym in swap_syms:
        try:
            o = exchange.create_order(sym, 'market', 'buy', 1, params={'tdMode': TD_MODE})
            print(f"[{now()}] SWAP {sym} OK: buy 1 contrato. ID: {o.get('id')}")
            swap_resultados[sym] = 'OK'
            time.sleep(3)
            side, contracts = get_position(exchange, sym)
            if side:
                close_position(exchange, side, contracts, sym)
        except Exception as e:
            err = str(e)
            print(f"[{now()}] SWAP {sym} -> FALLO: {err[:150]}")
            swap_resultados[sym] = err

    # --- SONDA FUTUROS CON VENCIMIENTO (info) ---
    if fut_syms:
        print(f"[{now()}] Futuros con vencimiento DOGE disponibles: {fut_syms[:3]}")

    # --- INTERPRETACION AUTOMATICA ---
    print(f"[{now()}] ================= LECTURA DEL DIAGNOSTICO =================")
    if not spot_syms and not swap_syms:
        print("  Plataforma sin instrumentos DOGE aparte de datos de mercado. Plan B necesario.")
        return
    if spot_ok:
        print("  SPOT OK -> key con permiso y DOGE en whitelist. Cuenta operable en spot.")
    else:
        print("  SPOT fallo -> revisar whitelist/permisos de la key (raro, DOGE esta en la lista).")
    for sym, res in swap_resultados.items():
        if res == 'OK':
            print(f"  SWAP {sym} OK -> PERPETUOS OPERABLES. El bot puede funcionar con {sym}.")
            return
        if '50124' in res:
            print(f"  SWAP {sym}: 50124 -> el instrumento existe pero la KEY no tiene")
            print("  permiso de perpetuos. La creacion de keys de tu plataforma no lo ofrece")
            print("  por API -> Plan B (futuros con vencimiento o spot) o soporte OKX.")
        elif '51001' in res:
            print(f"  SWAP {sym}: 51001 -> no existe para trading en esta cuenta/plataforma.")
        else:
            print(f"  SWAP {sym}: otro error -> {res[:120]}")


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
    print(f"[{now()}] Modo ciclo unico (GitHub Actions).")
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
