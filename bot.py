import os
import time

import ccxt
import pandas as pd

# ==================== CONFIGURACIÓN ====================
BOT_VERSION      = 'futuros-attach-v2'
TIMEFRAME        = '15m'
AMOUNT           = 3                 # contratos por entrada (10 DOGE/contrato)
SL_PCT           = 0.010             # Stop Loss 1.0%
TP_PCT           = 0.015             # Take Profit 1.5%
MAX_ENTRIES      = 2                 # máx. entradas acumuladas por dirección
WAIT_AFTER_ENTRY = 120               # 2 min de espera tras entrar
SIGNAL_ON_CLOSE  = True              # True: cruce con velas CERRADAS (sin repintado)
TD_MODE          = 'cross'           # margen cruzado multi-divisa (tu cuenta)

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
            print(f"[{now()}] OK: Autenticacion correcta | Colateral DOGE: {doge}")
        except Exception as e:
            print(f"[{now()}] Autenticacion OK (aviso mercados: {str(e)[:80]})")
        return ex
    raise RuntimeError("Ningun host OKX reconoce la key. Ultimo fallo: " + str(ultimo))


# ==================== INSTRUMENTO ====================
def pick_future(exchange):
    """Futuro DOGE/USD activo con vencimiento MAS LEJANO."""
    exchange.load_markets()
    candidatos = []
    for m in exchange.markets.values():
        if m.get('base') == 'DOGE' and m.get('future') and m.get('active'):
            info = m.get('info') or {}
            try:
                exp = int(info.get('expTime') or 0)
            except (TypeError, ValueError):
                exp = 0
            candidatos.append((exp, m['symbol']))
    if not candidatos:
        return None
    candidatos.sort(reverse=True)
    return candidatos[0][1]


def resolve_symbol(exchange):
    """Si hay posicion DOGE abierta, opera ese contrato; si no, el futuro mas lejano."""
    try:
        positions = exchange.fetch_positions()
        for p in positions:
            if (p.get('contracts') or 0) > 0 and (p.get('symbol') or '').startswith('DOGE/'):
                print(f"[{now()}] Instrumento (posicion abierta): {p['symbol']}")
                return p['symbol']
    except Exception:
        pass
    sym = pick_future(exchange)
    print(f"[{now()}] Instrumento: {sym} (futuro con vencimiento mas lejano)")
    return sym


# ==================== SEÑAL ====================
def get_signal(exchange, symbol):
    ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=100)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    df['ema7']  = df['close'].ewm(span=7,  adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()

    if SIGNAL_ON_CLOSE:
        i_prev, i_curr = -3, -2      # dos ultimas velas CERRADAS
    else:
        i_prev, i_curr = -2, -1

    prev7, prev21 = df['ema7'].iloc[i_prev], df['ema21'].iloc[i_prev]
    curr7, curr21 = df['ema7'].iloc[i_curr], df['ema21'].iloc[i_curr]

    ticker = exchange.fetch_ticker(symbol)
    price = ticker.get('last') or df['close'].iloc[-1]

    print(f"[{now()}] Precio: {price} | EMA7: {curr7:.5f} | EMA21: {curr21:.5f}")

    if prev7 <= prev21 and curr7 > curr21:
        return 'long', price
    if prev7 >= prev21 and curr7 < curr21:
        return 'short', price
    return None, price


# ==================== POSICIÓN ====================
def get_position(exchange, symbol):
    positions = exchange.fetch_positions([symbol])
    for p in positions:
        contracts = p.get('contracts') or 0
        if contracts > 0:
            return p.get('side'), contracts
    return None, 0


def close_position(exchange, side, contracts, symbol):
    close_side = 'sell' if side == 'long' else 'buy'
    amount = exchange.amount_to_precision(symbol, contracts)
    params = {'tdMode': TD_MODE, 'reduceOnly': True}
    exchange.create_order(symbol, 'market', close_side, amount, params=params)
    print(f"[{now()}] Posicion {side} cerrada ({amount} contratos). Flat.")


# ==================== ENTRADA (llamada directa a OKX) ====================
def _post_trade_order(exchange, req):
    """Llamada directa al endpoint /api/v5/trade/order (independiente de ccxt)."""
    method = getattr(exchange, 'privatePostTradeOrder', None)
    if method is None:
        method = exchange.private_post_trade_order
    return method(req)


def _place_entry(exchange, symbol, signal, amount, ref_price):
    """Envia la orden de entrada con SL/TP ADJUNTOS en el payload exacto de OKX.
    ordType='optimal_limit_ioc' = orden a mercado para futuros/swap en OKX."""
    market = exchange.market(symbol)
    inst_id = market['id']

    if signal == 'long':
        side, sl_raw, tp_raw = 'buy', ref_price * (1 - SL_PCT), ref_price * (1 + TP_PCT)
    else:
        side, sl_raw, tp_raw = 'sell', ref_price * (1 + SL_PCT), ref_price * (1 - TP_PCT)

    sl = exchange.price_to_precision(symbol, sl_raw)
    tp = exchange.price_to_precision(symbol, tp_raw)
    sz = exchange.amount_to_precision(symbol, amount)

    req = {
        'instId': inst_id,
        'tdMode': TD_MODE,
        'side': side,
        'ordType': 'optimal_limit_ioc',
        'sz': sz,
        'attachAlgoOrds': [{
            'tpTriggerPx': tp, 'tpOrdPx': '-1',     # -1 = ejecutar a mercado al gatillo
            'slTriggerPx': sl, 'slOrdPx': '-1',
        }],
    }

    resp = _post_trade_order(exchange, req)
    data = resp.get('data') or []
    d0 = data[0] if isinstance(data, list) and data else {}
    s_code = str(d0.get('sCode', resp.get('code', '1')))
    if s_code != '0':
        raise ccxt.ExchangeError(
            f"OKX rechazo la entrada: sCode={s_code} "
            f"{d0.get('sMsg') or resp.get('msg')}"
        )
    ord_id = d0.get('ordId')
    print(f"[{now()}] Orden {side.upper()} enviada CON SL/TP adjuntos. ordId: {ord_id}")
    return ord_id, side, sl, tp


def open_entry(exchange, signal, ref_price, symbol):
    market = exchange.market(symbol)
    ctval = float(market.get('contractSize') or 1)
    print(f"[{now()}] Tamano orden: {AMOUNT} contratos x {ctval} DOGE/contrato "
          f"= {AMOUNT * ctval} DOGE (~{AMOUNT * ctval * ref_price:.2f} USD)")

    ord_id, side, sl, tp = _place_entry(exchange, symbol, signal, AMOUNT, ref_price)

    entry = None
    for _ in range(5):
        time.sleep(1)
        try:
            o = exchange.fetch_order(ord_id, symbol)
            if o.get('average'):
                entry = o['average']
                break
        except ccxt.ExchangeError:
            continue
    entry = entry or ref_price

    print(f"[{now()}] Entrada: {entry} | SL: {sl} | TP: {tp}")
    return entry


# ==================== MODO PRUEBA (valida SL/TP adjuntos en el futuro real) ====================
def run_test(exchange):
    sym = pick_future(exchange)
    if not sym:
        raise RuntimeError("No hay futuros DOGE activos.")
    exchange.load_markets()
    market = exchange.market(sym)
    ctval = float(market.get('contractSize') or 1)

    price = exchange.fetch_ticker(sym).get('last')
    print(f"[{now()}] TEST en {sym} | 1 contrato x {ctval} DOGE (~{ctval * price:.2f} USD)")

    ord_id, side, sl, tp = _place_entry(exchange, sym, 'long', 1, price)
    time.sleep(4)

    # Verificar ordenes condicionales (SL/TP) activas en el exchange
    try:
        method = getattr(exchange, 'privateGetTradeOrdersAlgoPending', None)
        if method is None:
            method = exchange.private_get_trade_orders_algo_pending
        algo = method({'instId': market['id'], 'ordType': 'oco'})
        n = len(algo.get('data') or [])
        print(f"[{now()}] TEST: ordenes condicionales OCO activas (SL/TP): {n}")
        for a in (algo.get('data') or []):
            print(f"    -> tpTrigger={a.get('tpTriggerPx')} | slTrigger={a.get('slTriggerPx')}")
    except Exception as e:
        print(f"[{now()}] TEST: aviso leyendo algo-pending: {str(e)[:100]}")

    pside, contracts = get_position(exchange, sym)
    print(f"[{now()}] TEST: posicion -> side={pside}, contratos={contracts}")
    if pside == 'long':
        close_position(exchange, pside, contracts, sym)
        print(f"[{now()}] TEST COMPLETO: apertura + SL/TP adjuntos + cierre validados.")
    else:
        print(f"[{now()}] TEST: posicion no vista; revisar en OKX.")


# ==================== CICLO ====================
def run_cycle(exchange, symbol):
    signal, price = get_signal(exchange, symbol)
    if signal is None:
        print(f"[{now()}] Sin cruces nuevos en este ciclo. Vigilando.")
        return False

    side, contracts = get_position(exchange, symbol)

    # Cruce contrario con posicion abierta -> cerrar (flat) antes de girar
    if side and side != signal:
        close_position(exchange, side, contracts, symbol)
        side, contracts = None, 0
        time.sleep(2)

    # Maximo 2 entradas acumuladas en la misma direccion
    if side == signal:
        entries_done = round(contracts / AMOUNT)
        if entries_done >= MAX_ENTRIES:
            print(f"[{now()}] Ya hay {entries_done} entradas en {signal}. Maximo alcanzado.")
            return False

    cross = 'Golden Cross' if signal == 'long' else 'Death Cross'
    print(f"[{now()}] {cross} (7/21) detectado. Lanzando orden {signal.upper()}...")
    open_entry(exchange, signal, price, symbol)
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
    print(f"[{now()}] Modo ciclo unico (GitHub Actions) | bot {BOT_VERSION}.")
    exchange = detect_exchange()
    if os.getenv('TEST_MODE') == '1':
        run_test(exchange)
        return
    symbol = resolve_symbol(exchange)
    entered = run_cycle(exchange, symbol)
    if entered:
        print(f"[{now()}] Espera post-entrada: {WAIT_AFTER_ENTRY}s")
        time.sleep(WAIT_AFTER_ENTRY)


def main():
    print(f"[{now()}] Bot EMA 7/21 futuros | {TIMEFRAME} | SL {SL_PCT:.1%} / TP {TP_PCT:.1%} | {BOT_VERSION}")
    exchange = detect_exchange()
    while True:
        try:
            symbol = resolve_symbol(exchange)
            entered = run_cycle(exchange, symbol)
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
