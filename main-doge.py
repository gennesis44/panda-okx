# main-doge.py — DOGE/USD futuros OKX · version relajada (idéntica al XLM validado)
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
SL_PCT      = 0.010           # 1.0%
TP_PCT      = 0.015           # 1.5%
MAX_ENTRIES = 2               # TOPE TOTAL: 2 contratos
TD_MODE     = 'cross'
SIGNAL_ON_CLOSE = True        # velas CERRADAS: senal-evento
SINGLE_CYCLE    = os.getenv('SINGLE_CYCLE') == '1'

HOST = 'https://my.okx.com'

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY') or os.getenv('OKX_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY') or os.getenv('OKX_API_SECRET', ''),
    'password':  os.getenv('OKX_PASSWORD') or os.getenv('OKX_PASSPHRASE', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== INSTRUMENTO ====================
def pick_future():
    """Futuro DOGE/USD activo con vencimiento MAS LEJANO (instrumento probado en esta cuenta)."""
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
        for m in exchange.markets.values():
            if m.get('base') == BASE_ASSET and m.get('swap') and m.get('active'):
                return m['symbol']
        return None
    candidatos.sort(reverse=True)
    return candidatos[0][1]

def resolve_symbol():
    """Si hay posicion abierta, sigue ese contrato; si no, el mas lejano."""
    try:
        for p in exchange.fetch_positions():
            if (p.get('contracts') or 0) > 0 and (p.get('symbol') or '').startswith(f'{BASE_ASSET}/'):
                log.info(f"Instrumento (posicion abierta): {p['symbol']}")
                return p['symbol']
    except Exception:
        pass
    sym = pick_future()
    log.info(f"Instrumento: {sym} (futuro con vencimiento mas lejano)")
    return sym

# ==================== INDICADORES ====================
def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()

def rsi(s: pd.Series, length: int = 21) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/length, adjust=False).mean()
    rs = gain / loss.replace(0, float('nan'))
    return 100 - (100 / (1 + rs))

def macd(s: pd.Series, fast=12, slow=26, signal=9):
    line = ema(s, fast) - ema(s, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig

def fetch_data(symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

def calculate_indicators(df):
    df['EMA_7']  = ema(df['close'], 7)
    df['EMA_21'] = ema(df['close'], 21)
    df['RSI_21'] = rsi(df['close'], 21)
    df['MACD'], df['MACD_signal'], df['MACD_hist'] = macd(df['close'])
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

# ==================== SEÑAL RELAJADA (3 filtros, velas cerradas) ====================
def evaluate_multi_timeframe(symbol):
    try:
        df_1h = calculate_indicators(fetch_data(symbol, '1h', limit=100))
        df_4h = calculate_indicators(fetch_data(symbol, '4h', limit=100))

        i_prev, i_curr = (-3, -2) if SIGNAL_ON_CLOSE else (-2, -1)

        prev_7, prev_21 = df_1h['EMA_7'].iloc[i_prev], df_1h['EMA_21'].iloc[i_prev]
        curr_7, curr_21 = df_1h['EMA_7'].iloc[i_curr], df_1h['EMA_21'].iloc[i_curr]
        rsi_1h          = df_1h['RSI_21'].iloc[i_curr]
        price = exchange.fetch_ticker(symbol).get('last') or df_1h['close'].iloc[-1]

        trend_4h = df_4h['EMA_7'].iloc[-2] > df_4h['EMA_21'].iloc[-2]

        log.info(f"Precio: {price} | 1H EMA7/21: {curr_7:.5f}/{curr_21:.5f} | "
                 f"RSI: {rsi_1h:.1f} | 4H alcista: {trend_4h}")

        cross_up   = (prev_7 <= prev_21) and (curr_7 > curr_21)
        cross_down = (prev_7 >= prev_21) and (curr_7 < curr_21)

        # RELAJADA: cruce nuevo + RSI amplio + tendencia 4H
        is_long  = cross_up   and (40 < rsi_1h < 80) and trend_4h
        is_short = cross_down and (20 < rsi_1h < 60) and (not trend_4h)

        if is_long:
            return 'LONG', price
        if is_short:
            return 'SHORT', price
    except Exception as e:
        log.error(f"Error en evaluacion multi-temporalidad: {e}")
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
            log.info(f"Posicion {existing} contratos ({entries_done} entradas). Maximo (2). Esperando SL/TP.")
            return

    signal, price = evaluate_multi_timeframe(symbol)
    if not signal:
        log.info("Sin senales claras en este ciclo.")
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
    log.info("Modo ciclo unico (GitHub Actions) | DOGE bot v3-relajado.")
    verify_setup()
    run_cycle()

def main_loop():
    log.info(f"Iniciando bot {BASE_ASSET}-USD futuros | SL {SL_PCT:.1%} / TP {TP_PCT:.1%}")
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
        time.sleep(480)

if __name__ == "__main__":
    if SINGLE_CYCLE:
        run_once()
    else:
        main_loop()
