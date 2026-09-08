# main-xlm.py — XLM/USD OKX XPERP · cruce EMA7 x EMA21 en vela de 30m · gapless
import os
import time
import logging

import ccxt
import pandas as pd

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# ==================== CONFIGURACIÓN ====================
BASE_ASSET  = 'XLM'
AMOUNT      = 1.0             # 1 contrato = 100 XLM (~$18.6) — VERIFICAR nocional en 1a corrida
SL_PCT      = 0.010           # 1.0% SL
TP_PCT      = 0.015           # 1.5% TP
MAX_ENTRIES = 2               # TOPE TOTAL: 2 contratos (~200 XLM)
TD_MODE     = 'cross'
SIGNAL_ON_CLOSE = True        # senal-evento en vela CERRADA
TEST_MODE       = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE    = os.getenv('SINGLE_CYCLE') == '1'

TIMEFRAME     = '30m'         # vela unica de 30 minutos
CYCLE_SECONDS = 20 * 60       # ciclo cada 20 minutos
SCAN_CANDLES  = 2             # cubre el hueco 20/30 sin duplicar

HOST = 'https://my.okx.com'

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== INSTRUMENTO (XPERP / vencimiento mas lejano) ====================
def catalog_xlm():
    exchange.load_markets()
    print(f"[{time.strftime('%H:%M:%S')}] --- CATALOGO XLM ---", flush=True)
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET:
            continue
        info = m.get('info') or {}
        itype = info.get('instType') or '?'
        estado = 'activo' if m.get('active') else 'INACTIVO'
        print(f"  {m['symbol']} | {itype} | {estado} | ctVal={info.get('ctVal')} | "
              f"settle={info.get('settleCcy') or info.get('quoteCcy')}", flush=True)

def resolve_symbol():
    """Si hay posicion abierta, sigue ese contrato; si no, XPERP con vencimiento mas lejano."""
    exchange.load_markets()
    try:
        for p in exchange.fetch_positions():
            if (p.get('contracts') or 0) > 0 and (p.get('symbol') or '').startswith(f'{BASE_ASSET}/'):
                log.info(f"Instrumento (posicion abierta): {p['symbol']}")
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
        raise RuntimeError("No se encontro XPERP XLM/USD activo para esta cuenta.")
    candidatos.sort(reverse=True)
    sym = candidatos[0][1]
    log.info(f"Instrumento: {sym} (vencimiento mas lejano)")
    return sym

# ==================== INDICADORES (solo EMAs) ====================
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
        log.warning(f"Posicion {side} cerrada (fail-safe o giro).")
    except Exception as e:
        log.error(f"FALLO GRAVE cerrando posicion: {e} — cerrar MANUALMENTE en OKX.")

# ==================== ENTRADA CON SL/TP ADJUNTOS ====================
def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def _cl_order_id(candle_ts: int) -> str:
    return f"XLM{int(candle_ts)}"

def execute_order(side: str, symbol: str, ref_price: float, amount: float, candle_ts: int):
    market = exchange.market(symbol)
    ctval = float(market.get('contractSize') or 1)
    log.info(f"Nocional: {amount} contratos x {ctval} XLM = {amount*ctval} XLM "
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
    log.info(f"XLM {side} ejecutada con SL/TP adjuntos. ordId: {d0.get('ordId')} | SL: {sl} | TP: {tp}")
    return True

# ==================== DETONANTE: cruce EMA7 x EMA21 en vela(s) de 30m ====================
def evaluate_signals(symbol):
    """Escanea las ultimas SCAN_CANDLES velas cerradas (de mas nueva a mas vieja)."""
    try:
        df = calculate_indicators(fetch_data(symbol, TIMEFRAME, limit=100))
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        log.info(f"Precio: {price} | {TIMEFRAME} EMA7: {df['EMA_7'].iloc[-2]:.5f} / "
                 f"EMA21: {df['EMA_21'].iloc[-2]:.5f}")

        offsets = [(-2, -3), (-3, -4)][:SCAN_CANDLES] if SIGNAL_ON_CLOSE else [(-1, -2)]

        for i_curr, i_prev in offsets:
            prev_7, prev_21 = df['EMA_7'].iloc[i_prev], df['EMA_21'].iloc[i_prev]
            curr_7, curr_21 = df['EMA_7'].iloc[i_curr], df['EMA_21'].iloc[i_curr]

            cross_up   = (prev_7 <= prev_21) and (curr_7 > curr_21)
            cross_down = (prev_7 >= prev_21) and (curr_7 < curr_21)

            candle_ts = int(df['timestamp'].iloc[i_curr])
            if cross_up:
                return 'LONG', price, candle_ts
            if cross_down:
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

# ==================== ARRANQUE ====================
def verify_setup():
    bal = exchange.fetch_balance()
    xlm = (bal.get('XLM') or {}).get('free')
    log.info(f"Autenticacion OK | Colateral -> XLM: {xlm}")

# ==================== CICLO ====================
def run_cycle():
    symbol = resolve_symbol()

    try:
        exchange.set_leverage(1, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.warning(f"No se pudo fijar apalancamiento (se usa el de OKX): {e}")

    existing = get_position_contracts(symbol)
    if existing >= AMOUNT:
        entries_done = round(existing / AMOUNT)
        if entries_done >= MAX_ENTRIES:
            log.info(f"Posicion {existing} contratos ({entries_done} entradas). Maximo ({MAX_ENTRIES}). Esperando SL/TP.")
            return

    signal, price, candle_ts = evaluate_signals(symbol)
    if not signal:
        log.info("Sin cruce EMA7/EMA21 en las ultimas velas de 30m. Sin operacion.")
        return

    if candle_already_traded
