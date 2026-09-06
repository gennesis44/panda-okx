import os
import time
import logging

import ccxt
import pandas as pd

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# ==================== CONFIGURACIÓN ====================
BASE_ASSET  = 'DOGE'
AMOUNT      = 1.0             # contratos por entrada
SL_PCT      = 0.010           # 1.0% SL
TP_PCT      = 0.015           # 1.5% TP
MAX_ENTRIES = 2               # TOPE TOTAL: 2 contratos
TD_MODE     = 'cross'
SIGNAL_ON_CLOSE = True        # senal-evento en vela CERRADA
SINGLE_CYCLE    = os.getenv('SINGLE_CYCLE') == '1'

TIMEFRAME     = '15m'         # vela de 15 minutos
CYCLE_SECONDS = 20 * 60       # entra cada 20 minutos

HOST = 'https://my.okx.com'

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY') or os.getenv('OKX_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY') or os.getenv('OKX_API_SECRET', ''),
    'password':  os.getenv('OKX_PASSWORD') or os.getenv('OKX_PASSPHRASE', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== INSTRUMENTO (PERPETUO) ====================
def resolve_symbol():
    """Perpetuo DOGE/USD activo. Si hay posicion abierta, sigue ese instrumento."""
    exchange.load_markets()
    try:
        for p in exchange.fetch_positions():
            if (p.get('contracts') or 0) > 0 and (p.get('symbol') or '').startswith(f'{BASE_ASSET}/'):
                log.info(f"Instrumento (posicion abierta): {p['symbol']}")
                return p['symbol']
    except Exception:
        pass
    # Perpetuo coin-margined DOGE/USD (ej. DOGE/USD:DOGE-USD-SWAP)
    for m in exchange.markets.values():
        if (m.get('base') == BASE_ASSET and m.get('swap') and m.get('active')
                and m.get('settle') == BASE_ASSET):
            log.info(f"Instrumento: {m['symbol']} (perpetuo {BASE_ASSET}-margined)")
            return m['symbol']
    raise RuntimeError("No se encontro el perpetuo DOGE/USD activo.")

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

def execute_order(side: str, symbol: str, ref_price: float, amount: float):
    market = exchange.market(symbol)
    ctval = float(market.get('contractSize') or 1)
    log.info(f"Nocional: {amount} contratos x {ctval} DOGE = {amount*ctval} DOGE "
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
    log.info(f"DOGE {side} ejecutada con SL/TP adjuntos. ordId: {d0.get('ordId')} | SL: {sl} | TP: {tp}")
    return True

# ==================== DETONANTE: cruce EMA7 x EMA21 en UNA vela de 15m ====================
_last_signal_ts = None   # evita re-procesar la MISMA vela en ciclos de 20 min

def evaluate_signal(symbol):
    global _last_signal_ts
    try:
        df = calculate_indicators(fetch_data(symbol, TIMEFRAME, limit=100))

        i_prev, i_curr = (-3, -2) if SIGNAL_ON_CLOSE else (-2, -1)

        prev_7, prev_21 = df['EMA_7'].iloc[i_prev], df['EMA_21'].iloc[i_prev]
        curr_7, curr_21 = df['EMA_7'].iloc[i_curr], df['EMA_21'].iloc[i_curr]
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        log.info(f"Precio: {price} | {TIMEFRAME} EMA7: {curr_7:.5f} / EMA21: {curr_21:.5f}")

        # El cruce ocurre DENTRO de una sola vela de 15m
        cross_up   = (prev_7 <= prev_21) and (curr_7 > curr_21)
        cross_down = (prev_7 >= prev_21) and (curr_7 < curr_21)

        candle_ts = df['timestamp'].iloc[i_curr]
        if candle_ts == _last_signal_ts:
            log.info("Cruce ya procesado en esta vela. Esperando el proximo.")
            return None, None

        if cross_up:
            _last_signal_ts = candle_ts
            return 'LONG', price
        if cross_down:
            _last_signal_ts = candle_ts
            return 'SHORT', price
    except Exception as e:
        log.error(f"Error evaluando senal {TIMEFRAME}: {e}")
    return None, None

# ==================== ARRANQUE ====================
def verify_setup():
    bal = exchange.fetch_balance()
    doge = (bal.get('DOGE') or {}).get('free')
    log.info(f"Autenticacion OK | Colateral -> DOGE: {doge}")

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

    signal, price = evaluate_signal(symbol)
    if not signal:
        log.info("Sin cruce EMA7/EMA21 en 15m. Sin operacion.")
        return

    pos = get_open_position(symbol)
    if pos and pos['side'] != ('long' if signal == 'LONG' else 'short'):
        log.info("Cruce contrario: cerrando (flat) antes de girar.")
        close_position(symbol)
        time.sleep(2)

    log.info(f"Senal confirmada: {signal}. Abriendo posicion...")
    try:
        execute_order(signal, symbol, price, AMOUNT)
    except Exception as e:
        log.error(f"Entrada rechazada: {e}")
        close_position(symbol)

def run_once():
    log.info("Modo ciclo unico (GitHub Actions) | DOGE bot perpetuo-15m.")
    verify_setup()
    run_cycle()

def main_loop():
    log.info(f"Iniciando bot {BASE_ASSET}-USD PERPETUO | TF {TIMEFRAME} | "
             f"SL {SL_PCT:.1%} / TP {TP_PCT:.1%} | ciclo cada {CYCLE_SECONDS//60} min")
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
