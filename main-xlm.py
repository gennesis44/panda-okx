# main-xlm.py — Ax3 · 15m · solo EMA7/21 · SL 1% / TP 1.5% · 1x · 1 contrato
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

# ==================== CONFIGURACIÓN (Ax3) ====================
BASE_ASSET   = 'XLM'
AMOUNT       = 1        # 1 contrato = 100 XLM (minimo tecnico OKX)
SL_PCT       = 0.010    # 1.0%
TP_PCT       = 0.015    # 1.5%
TIMEFRAME    = '15m'    # UNICA vela madre Ax3
TD_MODE      = 'cross'
LEVERAGE     = 1        # Ax3: 1x
SIGNAL_ON_CLOSE = True  # velas CERRADAS
TEST_MODE       = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE    = os.getenv('SINGLE_CYCLE') == '1'

HOST = 'https://my.okx.com'   # instancia EEA — colateral MM en USDC

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== INSTRUMENTO ====================
def catalog_xlm():
    exchange.load_markets()
    print(f"[{time.strftime('%H:%M:%S')}] --- CATALOGO XLM ---", flush=True)
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET:
            continue
        info = m.get('info') or {}
        estado = 'activo' if m.get('active') else 'INACTIVO'
        print(f"  {m['symbol']} | {info.get('instType') or '?'} | {estado} | "
              f"ctVal={info.get('ctVal')}", flush=True)

def pick_future():
    exchange.load_markets()
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
        return None
    candidatos.sort(reverse=True)
    return candidatos[0][1]

def _log_contract_size(sym):
    try:
        market = exchange.market(sym)
        ctval = float(market.get('contractSize') or 1)
        price = exchange.fetch_ticker(sym).get('last') or 0
        log.info(f"Tamano: 1 contrato = {ctval} {BASE_ASSET} (~${ctval * price:.2f})")
    except Exception as e:
        log.warning(f"No se pudo leer el tamano del contrato: {e}")

def resolve_symbol():
    try:
        for p in exchange.fetch_positions():
            if (p.get('contracts') or 0) > 0 and (p.get('symbol') or '').startswith(f'{BASE_ASSET}/'):
                log.info(f"Instrumento (posicion abierta): {p['symbol']}")
                _log_contract_size(p['symbol'])
                return p['symbol']
    except Exception:
        pass
    sym = pick_future()
    log.info(f"Instrumento: {sym} (futuro con vencimiento mas lejano)")
    _log_contract_size(sym)
    return sym

# ==================== INDICADOR Ax3: SOLO EMA ====================
def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()

def fetch_data(symbol, timeframe, limit=60):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

# ==================== POSICIÓN ====================
def get_open_position(symbol):
    try:
        for pos in exchange.fetch_positions([symbol]):
            if pos.get('symbol') == symbol and float(pos.get('contracts') or 0) > 0:
                return pos
    except Exception as e:
        log.error(f"Error consultando posiciones: {e}")
    return None

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
        log.warning(f"Posicion {side} cerrada (giro Ax3).")
    except Exception as e:
        log.error(f"FALLO GRAVE cerrando posicion: {e} — cerrar MANUALMENTE en OKX.")

# ==================== ENTRADA CON SL/TP ADJUNTOS ====================
def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def execute_order(side: str, symbol: str, ref_price: float, amount: int):
    market = exchange.market(symbol)
    ctval = float(market.get('contractSize') or 1)
    log.info(f"Nocional: {amount} contrato x {ctval} {BASE_ASSET} = {amount*ctval} "
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
    log.info(f"{side} ejecutada con SL/TP adjuntos. ordId: {d0.get('ordId')} | SL: {sl} | TP: {tp}")
    return True

# ==================== SEÑAL Ax3: SOLO EMA7/21 EN 15M ====================
def evaluate_signal(symbol):
    """Cruce EMA7/21 en las 2 ultimas velas de 15m CERRADAS.
    Ventana doble: con cron cada 20 min, ningun cruce se revisa dos veces
    ni queda sin mirar."""
    try:
        df = fetch_data(symbol, TIMEFRAME, limit=60)
        df['EMA_7']  = ema(df['close'], 7)
        df['EMA_21'] = ema(df['close'], 21)
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        e7, e21 = df['EMA_7'], df['EMA_21']
        log.info(f"Precio: {price} | 15m EMA7/21: {e7.iloc[-2]:.5f}/{e21.iloc[-2]:.5f} "
                 f"(prev {e7.iloc[-3]:.5f}/{e21.iloc[-3]:.5f})")

        for i_curr in (-2, -3):
            i_prev = i_curr - 1
            if e7.iloc[i_prev] <= e21.iloc[i_prev] and e7.iloc[i_curr] > e21.iloc[i_curr]:
                return 'LONG', price
            if e7.iloc[i_prev] >= e21.iloc[i_prev] and e7.iloc[i_curr] < e21.iloc[i_curr]:
                return 'SHORT', price
    except Exception as e:
        log.error(f"Error en evaluacion: {e}")
    return None, None

# ==================== ARRANQUE: DESGLOSE REAL DEL COLATERAL ====================
def verify_setup():
    raw = exchange.privateGetAccountBalance()
    details = ((raw or {}).get('data') or [{}])[0].get('details') or []
    total = 0.0
    for d in details:
        eq_usd = _f(d.get('eqUsd'))
        total += eq_usd
        avail = d.get('availEq') or d.get('availBal') or '0'
        log.info(f"Trading | {d.get('ccy')}: unidades={d.get('eq')} | "
                 f"libre={avail} | valor=${eq_usd:.2f}")
    log.info(f"Colateral total (valor USD): ~{total:.2f}  <- lo que ve tu app de futuros")
    try:
        fund = exchange.privateGetAssetBalances()
        for d in (fund or {}).get('data') or []:
            log.info(f"Financiera | {d.get('ccy')}: {_f(d.get('availBal')):.6f}")
    except Exception as e:
        log.warning(f"No se pudo leer Financiera: {e}")

# ==================== CICLO ====================
def run_cycle():
    symbol = resolve_symbol()

    try:
        exchange.set_leverage(LEVERAGE, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.warning(f"No se pudo fijar apalancamiento (se usa el de OKX): {e}")

    # Verdad de OKX: cuantos contratos permite abrir el colateral REAL
    try:
        market = exchange.market(symbol)
        ms = exchange.privateGetAccountMaxSize({'instId': market['id'], 'tdMode': TD_MODE})
        d0 = (ms.get('data') or [{}])[0]
        log.info(f"Capacidad OKX: maxBuy={d0.get('maxBuySz')} | "
                 f"maxSell={d0.get('maxSellSz')} contratos")
    except Exception as e:
        log.warning(f"No se pudo consultar capacidad: {e}")

    signal, price = evaluate_signal(symbol)
    if not signal:
        log.info("Sin cruces EMA7/21 en 15m. Vigilando.")
        return

    pos = get_open_position(symbol)
    if pos:
        same = pos['side'] == ('long' if signal == 'LONG' else 'short')
        if same:
            log.info("Posicion ya en esa direccion. Esperando SL/TP.")
            return
        log.info(f"Cruce contrario ({signal}): cerrando y girando...")
        close_position(symbol)
        time.sleep(2)

    log.info(f"Senal {signal}. Abriendo 1 contrato (100 XLM)...")
    try:
        execute_order(signal, symbol, price, AMOUNT)
    except Exception as e:
        log.error(f"Entrada rechazada: {e}")

def run_once():
    log.info("Modo ciclo unico (GitHub Actions) | Ax3: 15m, EMA7/21, 1x, 1 contrato.")
    verify_setup()
    if TEST_MODE:
        catalog_xlm()
        return
    run_cycle()

def main_loop():
    log.info(f"Iniciando bot {BASE_ASSET} Ax3 | {TIMEFRAME} | SL {SL_PCT:.1%} / TP {TP_PCT:.1%} | {LEVERAGE}x")
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
        time.sleep(1200)   # 20 min

if __name__ == "__main__":
    if SINGLE_CYCLE:
        run_once()
    else:
        main_loop()
