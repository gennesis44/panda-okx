# main-fet.py — Ax3.1-parity · FET/USD XPERP · cruce EMA7x21 · vela unica 30m
import os
import time
import logging

import ccxt
import pandas as pd

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

# ==================== CONFIGURACIÓN ====================
BASE_ASSET  = 'FET'
AMOUNT      = 1.0             # ARRANQUE PRUDENTE: 1 contrato. Subir a 2 cuando el log muestre Tamano
SL_PCT      = 0.010           # 1.0% SL
TP_PCT      = 0.015           # 1.5% TP
TD_MODE     = 'cross'
SIGNAL_ON_CLOSE = True
TEST_MODE       = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE    = os.getenv('SINGLE_CYCLE') == '1'

TIMEFRAME     = '30m'
CYCLE_SECONDS = 20 * 60

HOST = 'https://my.okx.com'

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== INSTRUMENTO ====================
def catalog_fet():
    exchange.load_markets()
    print(f"[{time.strftime('%H:%M:%S')}] --- CATALOGO FET ---", flush=True)
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET:
            continue
        info = m.get('info') or {}
        estado = 'activo' if m.get('active') else 'INACTIVO'
        print(f"  {m['symbol']} | {info.get('instType') or '?'} | {estado} | "
              f"ctVal={info.get('ctVal')}", flush=True)

def _log_contract_size(sym):
    try:
        market = exchange.market(sym)
        ctval = float(market.get('contractSize') or 1)
        price = exchange.fetch_ticker(sym).get('last') or 0
        log.info(f"Tamano: 1 contrato = {ctval} {BASE_ASSET} (~${ctval * price:.2f})")
    except Exception as e:
        log.warning(f"No se pudo leer el tamano del contrato: {e}")

def resolve_symbol():
    exchange.load_markets()
    try:
        for p in exchange.fetch_positions():
            if (p.get('contracts') or 0) > 0 and (p.get('symbol') or '').startswith(f'{BASE_ASSET}/'):
                log.info(f"Instrumento (posicion abierta): {p['symbol']}")
                _log_contract_size(p['symbol'])
                return p['symbol']
    except Exception:
        pass
    candidatos = []
    for m in exchange.markets.values():
        if m.get('base') == BASE_ASSET and m.get('future') and m.get('active'):
            info = m.get('info') or {}
            try:
                exp = int(info.get('expTime') or 0)
            except (TypeError, ValueError):
                exp = 0
            candidatos.append((exp, m['symbol']))
    if not candidatos:
        raise RuntimeError("No se encontro XPERP FET/USD activo para esta cuenta.")
    candidatos.sort(reverse=True)
    sym = candidatos[0][1]
    log.info(f"Instrumento: {sym} (vencimiento mas lejano)")
    _log_contract_size(sym)
    return sym

# ==================== INDICADORES ====================
def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()

def fetch_data(symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

def calculate_indicators(df):
    df['EMA_7']  = ema(df['close'], 7)
    df['EMA_21'] = ema(df['close'], 21)
    return df

# ==================== POSICIÓN ====================
def get_open_position(symbol):
    try:
        for pos in exchange.fetch_positions([symbol]):
            if pos.get('symbol') == symbol and float(pos.get('contracts') or 0) > 0:
                return pos
    except Exception as e:
        log.error(f"Error consultando posiciones: {e}")
    return None

def get_position_contracts(symbol):
    try:
        for pos in exchange.fetch_positions([symbol]):
            if pos.get('symbol') == symbol:
                return float(pos.get('contracts') or 0)
    except Exception:
        pass
    return 0.0

def close_position(symbol):
    pos = get_open_position(symbol)
    if not pos:
        return
    side, contracts = pos['side'], pos['contracts']
    amount = exchange.amount_to_precision(symbol, contracts)
    close_side = 'sell' if side == 'long' else 'buy'
    try:
        exchange.create_order(symbol, 'market', close_side, amount,
                              params={'tdMode': TD_MODE, 'reduceOnly': True})
        log.warning(f"Posicion {side} cerrada (fail-safe).")
    except Exception as e:
        log.error(f"FALLO GRAVE cerrando posicion: {e} — cerrar MANUALMENTE en OKX.")

# ==================== ENTRADA CON SL/TP ADJUNTOS ====================
def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def _cl_order_id(candle_ts: int) -> str:
    return f"FET{int(candle_ts)}"

def execute_order(side: str, symbol: str, ref_price: float, amount: float, candle_ts: int):
    market = exchange.market(symbol)
    ctval = float(market.get('contractSize') or 1)
    log.info(f"Nocional: {amount} contratos x {ctval} FET = {amount*ctval} FET "
             f"(~${amount*ctval*ref_price:.2f})")

    if side == 'LONG':
        oside, sl_raw, tp_raw = 'buy', ref_price * (1 - SL_PCT), ref_price * (1 + TP_PCT)
    else:
        oside, sl_raw, tp_raw = 'sell', ref_price * (1 + SL_PCT), ref_price * (1 - TP_PCT)

    sl = exchange.price_to_precision(symbol, sl_raw)
    tp = exchange.price_to_precision(symbol, tp_raw)
    sz = exchange.amount_to_precision(symbol, amount)

    req = {
        'instId': market['id'],
        'tdMode': TD_MODE,
        'side': oside,
        'ordType': 'optimal_limit_ioc',
        'sz': sz,
        'clOrdId': _cl_order_id(candle_ts),
        'attachAlgoOrds': [{
            'tpTriggerPx': tp, 'tpOrdPx': '-1',
            'slTriggerPx': sl, 'slOrdPx': '-1',
        }],
    }
    resp = _post_trade_order(req)
    data = resp.get('data') or []
    d0 = data[0] if isinstance(data, list) and data else {}
    if str(d0.get('sCode', resp.get('code', '1'))) != '0':
        raise ccxt.ExchangeError(f"OKX rechazo la entrada: {d0.get('sMsg') or resp.get('msg')}")
    log.info(f"FET {side} ejecutada con SL/TP adjuntos. ordId: {d0.get('ordId')} | SL: {sl} | TP: {tp}")
    return True

# ==================== DETONANTE ====================
def evaluate_signal(symbol):
    try:
        df = calculate_indicators(fetch_data(symbol, TIMEFRAME, limit=100))
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        log.info(f"Precio: {price} | {TIMEFRAME} EMA7: {df['EMA_7'].iloc[-2]:.5f} / "
                 f"EMA21: {df['EMA_21'].iloc[-2]:.5f}")

        i_curr, i_prev = (-2, -3) if SIGNAL_ON_CLOSE else (-1, -2)
        prev_7, prev_21 = df['EMA_7'].iloc[i_prev], df['EMA_21'].iloc[i_prev]
        curr_7, curr_21 = df['EMA_7'].iloc[i_curr], df['EMA_21'].iloc[i_curr]

        candle_ts = int(df['timestamp'].iloc[i_curr])
        if prev_7 <= prev_21 and curr_7 > curr_21:
            return 'LONG', price, candle_ts
        if prev_7 >= prev_21 and curr_7 < curr_21:
            return 'SHORT', price, candle_ts
    except Exception as e:
        log.error(f"Error evaluando senal {TIMEFRAME}: {e}")
    return None, None, None

def candle_already_traded(symbol, candle_ts: int) -> bool:
    cl = _cl_order_id(candle_ts)
    try:
        inst = exchange.market(symbol)['id']
        resp = exchange.private_get_trade_order({'instId': inst, 'clOrdId': cl})
        data = resp.get('data') or []
        if data and str(data[0].get('sCode', '0')) == '0':
            log.info(f"Vela ya operada (clOrdId {cl}). Entrada duplicada bloqueada.")
            return True
        return False
    except ccxt.ExchangeError as e:
        msg = str(e)
        if '51603' in msg or 'does not exist' in msg.lower():
            return False
        log.warning(f"No se pudo verificar duplicado ({msg}); se permite la entrada.")
        return False
    except Exception as e:
        log.warning(f"No se pudo verificar duplicado ({e}); se permite la entrada.")
        return False

# ==================== CAPACIDAD (paridad Ax3.1) ====================
def capacity_report(symbol):
    try:
        market = exchange.market(symbol)
        ctval = float(market.get('contractSize') or 1)
        price = exchange.fetch_ticker(symbol).get('last') or 0
        need = ctval * price / 1   # leverage 1x
    except Exception as e:
        log.warning(f"Capacidad: nocional no calculable: {e}")
        return
    try:
        raw = exchange.privateGetAccountBalance()
        details = ((raw or {}).get('data') or [{}])[0].get('details') or []
        total = sum(_f(d.get('eqUsd')) for d in details)
        verdict = 'CABE' if total >= need else 'NO CABE'
        log.info(f"Margen 1 contrato: ~${need:.2f} | colateral real: ~${total:.2f} -> {verdict}")
    except Exception as e:
        log.warning(f"Capacidad: colateral no calculable: {e}")
    try:
        ms = exchange.privateGetAccountMaxSize({'instId': market['id'], 'tdMode': TD_MODE})
        d0 = (ms.get('data') or [{}])[0]
        mb, msz = d0.get('maxBuy'), d0.get('maxSell')
        if mb or msz:
            log.info(f"Capacidad OKX: maxBuy={mb} | maxSell={msz} contratos")
        else:
            log.warning(f"MAXSIZE crudo: {str(ms)[:300]}")
    except Exception as e:
        log.warning(f"MAXSIZE no disponible: {e}")

# ==================== ARRANQUE ====================
def verify_setup():
    raw = exchange.privateGetAccountBalance()
    details = ((raw or {}).get('data') or [{}])[0].get('details') or []
    total = sum(_f(d.get('eqUsd')) for d in details)
    log.info(f"Autenticacion OK | Colateral real (valor USD): ~{total:.2f}")

# ==================== CICLO ====================
def run_cycle():
    symbol = resolve_symbol()

    try:
        exchange.set_leverage(1, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.warning(f"No se pudo fijar apalancamiento (se usa el de OKX): {e}")

    capacity_report(symbol)

    existing = get_position_contracts(symbol)
    if existing > 0:
        log.info(f"Posicion abierta: {existing} contratos. Esperando SL/TP.")
        return

    signal, price, candle_ts = evaluate_signal(symbol)
    if not signal:
        log.info("Sin cruce EMA7/EMA21 en la ultima vela de 30m. Sin operacion.")
        return

    if candle_already_traded(symbol, candle_ts):
        return

    log.info(f"Senal confirmada: {signal} (vela {candle_ts}). Abriendo {AMOUNT} contrato(s)...")
    try:
        execute_order(signal, symbol, price, AMOUNT, candle_ts)
    except Exception as e:
        log.error(f"Entrada rechazada: {e}")

def run_once():
    log.info("Modo ciclo unico (GitHub Actions) | FET bot XPERP-30m (Ax3.1-parity).")
    verify_setup()
    if TEST_MODE:
        catalog_fet()
        return
    run_cycle()

def main_loop():
    log.info(f"Iniciando bot {BASE_ASSET}-USD | TF {TIMEFRAME} | "
             f"SL {SL_PCT:.1%} / TP {TP_PCT:.1%} | entrada {AMOUNT} contratos | ciclo {CYCLE_SECONDS//60} min")
    verify_setup()
    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("Detenido por el usuario.")
            break
        except Exception as e:
            log.error(f"Error en el ciclo principal: {e}")
            time.sleep(60)
        time.sleep(CYCLE_SECONDS)

if __name__ == "__main__":
    if SINGLE_CYCLE:
        run_once()
    else:
        main_loop()
