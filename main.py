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
AMOUNT      = 1               # contratos — VER el nocional impreso antes de subir
SL_PCT      = 0.010           # 1.0%
TP_PCT      = 0.015           # 1.5%
MAX_ENTRIES = 2
TD_MODE     = 'cross'
SIGNAL_ON_CLOSE = True        # velas CERRADAS: senal-evento
TEST_MODE       = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE    = os.getenv('SINGLE_CYCLE') == '1'

HOST = 'https://my.okx.com'   # instancia real de tu cuenta (leccion 50119)

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
    """Lista todos los instrumentos XLM reales de la plataforma."""
    exchange.load_markets()
    print(f"[{time.strftime('%H:%M:%S')}] --- CATALOGO XLM ---", flush=True)
    futs, swaps, spots = [], [], []
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET:
            continue
        info = m.get('info') or {}
        itype = info.get('instType') or '?'
        estado = 'activo' if m.get('active') else 'INACTIVO'
        print(f"  {m['symbol']} | {itype} | {estado} | ctVal={info.get('ctVal')} | "
              f"settle={info.get('settleCcy') or info.get('quoteCcy')}", flush=True)
        if m.get('active') and m.get('future'):
            futs.append(m['symbol'])
        elif m.get('active') and m.get('swap'):
            swaps.append(m['symbol'])
        elif m.get('active') and m.get('spot'):
            spots.append(m['symbol'])
    return futs, swaps, spots

def pick_future():
    """Futuro XLM/USD activo con vencimiento MAS LEJANO."""
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

def resolve_symbol():
    """Si hay posicion XLM abierta, sigue ese contrato; si no, el mas lejano."""
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

# ==================== INDICADORES (pandas puro, sin pandas_ta) ====================
def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()

def rsi(s: pd.Series, length: int = 14) -> pd.Series:
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

# ==================== ENTRADA CON SL/TP ADJUNTOS (patron TEST COMPLETO) ====================
def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def execute_order(side: str, symbol: str, ref_price: float, amount: int):
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
        'ordType': 'optimal_limit_ioc',     # mercado para derivados en OKX
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

# ==================== DIAGNÓSTICO (SONDA B: abrir y cerrar 1 contrato) ====================
def run_test():
    """Cataloga XLM, prueba el futuro con 1 contrato y lo cierra.
    Clasifica: inexistente / sin permiso (50124) / permiso OK sin margen / OPERABLE."""
    log.info("=== DIAGNOSTICO XLM ===")
    futs, swaps, spots = catalog_xlm()

    if not futs:
        log.error("No hay futuros XLM activos. El camino DOGE no se replica para XLM.")
        return

    sym = pick_future()
    market = exchange.market(sym)
    ctval = float(market.get('contractSize') or 1)
    price = exchange.fetch_ticker(sym).get('last')
    log.info(f"SONDA: {sym} | 1 contrato x {ctval} XLM (~${ctval * (price or 0):.2f})")

    try:
        o = exchange.create_order(sym, 'market', 'buy', 1, params={'tdMode': TD_MODE})
        log.info(f"SONDA OK: buy 1 contrato. ID: {o.get('id')}")
        time.sleep(3)
        pos = get_open_position(sym)
        if pos:
            log.info(f"Posicion abierta: {pos['side']} x {pos['contracts']} — cerrando...")
            close_position(sym)
        log.info("DIAGNOSTICO: FUTUROS XLM OPERABLES — el script esta listo para produccion.")
    except Exception as e:
        err = str(e)
        log.error(f"SONDA FALLO -> {err[:150]}")
        if '50124' in err:
            log.error("Clasificacion: la key NO tiene permiso de futuros para XLM.")
        elif '51001' in err:
            log.error("Clasificacion: el futuro XLM no existe para esta cuenta.")
        elif any(k in err.lower() for k in ['margin', 'balance', 'funds', 'insufficient', '51119', '51124']):
            log.warning("Clasificacion: PERMISO OK — solo falta margen. Ajustar AMOUNT o colateral.")
        else:
            log.error(f"Clasificacion indeterminada: {err[:120]}")

# ==================== SEÑAL MULTI-TIMEFRAME (evento, velas cerradas) ====================
def evaluate_multi_timeframe(symbol):
    try:
        df_15m = calculate_indicators(fetch_data(symbol, '15m', limit=100))
        df_1h  = calculate_indicators(fetch_data(symbol, '1h', limit=100))
        df_4h  = calculate_indicators(fetch_data(symbol, '4h', limit=100))

        i_prev, i_curr = (-3, -2) if SIGNAL_ON_CLOSE else (-2, -1)

        prev_7, prev_21 = df_1h['EMA_7'].iloc[i_prev], df_1h['EMA_21'].iloc[i_prev]
        curr_7, curr_21 = df_1h['EMA_7'].iloc[i_curr], df_1h['EMA_21'].iloc[i_curr]
        rsi_1h          = df_1h['RSI_21'].iloc[i_curr]
        macd_hist_1h    = df_1h['MACD_hist'].iloc[i_curr]
        price = exchange.fetch_ticker(symbol).get('last') or df_1h['close'].iloc[-1]

        trend_4h     = df_4h['EMA_7'].iloc[-2] > df_4h['EMA_21'].iloc[-2]
        momentum_15m = df_15m['MACD_hist'].iloc[-2] > 0

        log.info(f"Precio: {price} | 1H EMA7/21: {curr_7:.5f}/{curr_21:.5f} | "
                 f"RSI: {rsi_1h:.1f} | MACDh: {macd_hist_1h:.4f} | 4H alcista: {trend_4h}")

        cross_up   = (prev_7 <= prev_21) and (curr_7 > curr_21)
        cross_down = (prev_7 >= prev_21) and (curr_7 < curr_21)

        is_long  = cross_up   and (macd_hist_1h > 0) and (45 < rsi_1h < 75) and trend_4h and momentum_15m
        is_short = cross_down and (macd_hist_1h < 0) and (25 < rsi_1h < 55) and (not trend_4h) and (not momentum_15m)

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
    usd  = (bal.get('USD') or {}).get('free')
    log.info(f"Autenticacion OK | Colateral -> DOGE: {doge} | USD: {usd}")

# ==================== CICLO ====================
def run_cycle():
    symbol = resolve_symbol()

    # NUEVO: fijar apalancamiento 3x en cross (antes usaba el valor de la web de OKX)
    try:
        exchange.set_leverage(3, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.warning(f"No se pudo fijar apalancamiento (se usa el de OKX): {e}")

    existing = get_position_contracts(symbol)
    if existing >= AMOUNT:
        entries_done = round(existing / AMOUNT)
        if entries_done >= MAX_ENTRIES:
            log.info(f"Posicion {existing} contratos ({entries_done} entradas). Maximo. Esperando SL/TP.")
            return
    else:
        log.info("Sin posicion. Ventana de observacion de 2 minutos...")
        time.sleep(120)

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
        close_position(symbol)   # fail-safe: jamas posicion sin proteccion

def run_once():
    log.info("Modo ciclo unico (GitHub Actions).")
    verify_setup()
    if TEST_MODE:
        run_test()
        return
    run_cycle()

def main_loop():
    log.info(f"Iniciando bot {BASE_ASSET}-USD futuros ({HOST}) | SL {SL_PCT:.1%} / TP {TP_PCT:.1%}")
    verify_setup()
    while True:
        try:
            if TEST_MODE:
                run_test()
                break
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
