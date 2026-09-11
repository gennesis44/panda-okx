# main-xlm.py — Ax2-D · 15m detonante + 4H brújula + cooldown · SL 1% / TP 1.5% · 1x · 1 contrato
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

# ==================== CONFIGURACIÓN (Ax2-D) ====================
BASE_ASSET   = 'XLM'
AMOUNT       = 1        # 1 contrato = 100 XLM (minimo tecnico OKX)
SL_PCT       = 0.010    # 1.0%
TP_PCT       = 0.015    # 1.5%
TIMEFRAME    = '15m'    # vela madre: detonante
TF_FILTER    = '4h'     # brujula: solo direccion (gate)
TD_MODE      = 'cross'
LEVERAGE     = 1
SIGNAL_ON_CLOSE = True  # velas CERRADAS
COOLDOWN_MIN = 60       # tras LOSS: sin nuevas entradas en este par durante 60 min
TEST_MODE       = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE    = os.getenv('SINGLE_CYCLE') == '1'

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

# ==================== INDICADORES: SOLO EMA ====================
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
        log.warning(f"Posicion {side} cerrada (giro).")
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

# ==================== COOLDOWN POST-PÉRDIDA (anti-sierra) ====================
def cooldown_active(symbol):
    """True si el ultimo trade cerrado del par fue LOSS hace menos de COOLDOWN_MIN."""
    try:
        market_id = exchange.market(symbol)['id']
        hist = exchange.privateGetTradeFillsHistory({'instType': 'FUTURES', 'instId': market_id, 'limit': '3'})
        for fill in (hist.get('data') or []):
            # fills con reduceOnly: cierre de posicion -> decide cooldown
            if str(fill.get('reduceOnly', '0')) == 'true' or fill.get('subType') in ('3', '4', '5', '6'):
                ts_ms = int(fill.get('ts') or 0)
                age_min = (time.time() * 1000 - ts_ms) / 60000.0
                pnl = _f(fill.get('pnl'))
                if pnl < 0 and age_min < COOLDOWN_MIN:
                    log.info(f"COOLDOWN: ultima perdida hace {age_min:.0f} min (< {COOLDOWN_MIN}). "
                             f"Sin nuevas entradas en este par.")
                    return True
                return False   # el cierre mas reciente no es LOSS reciente
        return False
    except Exception as e:
        log.warning(f"No se pudo verificar cooldown ({e}); se permite la entrada.")
        return False

# ==================== SEÑAL Ax2-D: CRUCE 15m + GATE 4H ====================
def evaluate_signal(symbol):
    """Detonante: cruce EMA7/21 en velas de 15m CERRADAS (ventana doble).
    Brujula: la ultima vela 4H CERRADA define direccion permitida.
    LONG solo con 4H alcista. SHORT solo con 4H bajista."""
    try:
        df_4h = fetch_data(symbol, TF_FILTER, limit=30)
        e7h, e21h = ema(df_4h['close'], 7), ema(df_4h['close'], 21)
        trend_4h = e7h.iloc[-2] > e21h.iloc[-2]

        df = fetch_data(symbol, TIMEFRAME, limit=60)
        df['EMA_7']  = ema(df['close'], 7)
        df['EMA_21'] = ema(df['close'], 21)
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        e7, e21 = df['EMA_7'], df['EMA_21']
        log.info(f"Precio: {price} | {TIMEFRAME} EMA7/21: {e7.iloc[-2]:.5f}/{e21.iloc[-2]:.5f} | "
                 f"4H: {'ALCISTA' if trend_4h else 'BAJISTA'} (gate {'LONG' if trend_4h else 'SHORT'})")

        for i_curr in (-2, -3):
            i_prev = i_curr - 1
            if e7.iloc[i_prev] <= e21.iloc[i_prev] and e7.iloc[i_curr] > e21.iloc[i_curr]:
                if trend_4h:
                    return 'LONG', price
                log.info("Cruce alcista VETADO: 4H bajista.")
            if e7.iloc[i_prev] >= e21.iloc[i_prev] and e7.iloc[i_curr] < e21.iloc[i_curr]:
                if not trend_4h:
                    return 'SHORT', price
                log.info("Cruce bajista VETADO: 4H alcista.")
    except Exception as e:
        log.error(f"Error en evaluacion: {e}")
    return None, None

# ==================== CAPACIDAD (Ax3.1) ====================
def capacity_report(symbol):
    try:
        market = exchange.market(symbol)
        ctval = float(market.get('contractSize') or 1)
        price = exchange.fetch_ticker(symbol).get('last') or 0
        need = ctval * AMOUNT * price / max(LEVERAGE, 1)
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
        exchange.set_leverage(LEVERAGE, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.warning(f"No se pudo fijar apalancamiento (se usa el de OKX): {e}")

    capacity_report(symbol)

    pos = get_open_position(symbol)
    if pos:
        log.info(f"Posicion abierta ({pos['side']}). Esperando SL/TP.")
        return

    if cooldown_active(symbol):
        log.info("Vigilando (cooldown activo).")
        return

    signal, price = evaluate_signal(symbol)
    if not signal:
        log.info("Sin cruces EMA7/21 en 15m alineados con 4H. Vigilando.")
        return

    log.info(f"Senal {signal} (4H a favor). Abriendo 1 contrato (100 XLM)...")
    try:
        execute_order(signal, symbol, price, AMOUNT)
    except Exception as e:
        log.error(f"Entrada rechazada: {e}")

def run_once():
    log.info("Modo ciclo unico (GitHub Actions) | Ax2-D: 15m + gate 4H + cooldown 60m.")
    verify_setup()
    if TEST_MODE:
        catalog_xlm()
        return
    run_cycle()

def main_loop():
    log.info(f"Iniciando bot {BASE_ASSET} Ax2-D | {TIMEFRAME}+{TF_FILTER} | "
             f"SL {SL_PCT:.1%} / TP {TP_PCT:.1%} | {LEVERAGE}x | cooldown {COOLDOWN_MIN}m")
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
