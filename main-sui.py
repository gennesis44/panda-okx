# main-sui.py — Ax2-D · 15m detonante + 4H brujula + cooldown · SL 1.5% / TP 2.5% · 1x · 2 contratos
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
BASE_ASSET  = 'SUI'
AMOUNT      = 2.0             # 2 contratos = 2 SUI (~$1.57) — medido
SL_PCT      = 0.015           # 1.5% SL (volatilidad SUI)
TP_PCT      = 0.025           # 2.5% TP (ratio 1:1.67, breakeven 37.5%)
TIMEFRAME    = '15m'          # vela madre: detonante
TF_FILTER    = '4h'           # brujula: solo direccion (gate)
TD_MODE     = 'cross'
SIGNAL_ON_CLOSE = True
COOLDOWN_MIN = 60             # tras LOSS: sin nuevas entradas en este par durante 60 min
TEST_MODE       = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE    = os.getenv('SINGLE_CYCLE') == '1'

SCAN_CANDLES  = 2             # ventana doble: cubre el hueco 20/15 sin duplicar
CYCLE_SECONDS = 20 * 60

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
def catalog_sui():
    exchange.load_markets()
    print(f"[{time.strftime('%H:%M:%S')}] --- CATALOGO SUI ---", flush=True)
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
        raise RuntimeError("No se encontro XPERP SUI/USD activo para esta cuenta.")
    candidatos.sort(reverse=True)
    sym = candidatos[0][1]
    log.info(f"Instrumento: {sym} (vencimiento mas lejano)")
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
    return f"SUI{int(candle_ts)}"

def execute_order(side: str, symbol: str, ref_price: float, amount: float, candle_ts: int):
    market = exchange.market(symbol)
    ctval = float(market.get('contractSize') or 1)
    log.info(f"Nocional: {amount} contratos x {ctval} SUI = {amount*ctval} SUI "
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
    log.info(f"SUI {side} ejecutada con SL/TP adjuntos. ordId: {d0.get('ordId')} | SL: {sl} | TP: {tp}")
    return True

# ==================== COOLDOWN POST-PÉRDIDA (anti-sierra) ====================
def cooldown_active(symbol):
    """True si el ultimo trade cerrado del par fue LOSS hace menos de COOLDOWN_MIN."""
    try:
        market_id = exchange.market(symbol)['id']
        hist = exchange.privateGetTradeFillsHistory({'instType': 'FUTURES', 'instId': market_id, 'limit': '3'})
        for fill in (hist.get('data') or []):
            if str(fill.get('reduceOnly', '0')) == 'true' or fill.get('subType') in ('3', '4', '5', '6'):
                ts_ms = int(fill.get('ts') or 0)
                age_min = (time.time() * 1000 - ts_ms) / 60000.0
                pnl = _f(fill.get('pnl'))
                if pnl < 0 and age_min < COOLDOWN_MIN:
                    log.info(f"COOLDOWN: ultima perdida hace {age_min:.0f} min (< {COOLDOWN_MIN}). "
                             f"Sin nuevas entradas en este par.")
                    return True
                return False
        return False
    except Exception as e:
        log.warning(f"No se pudo verificar cooldown ({e}); se permite la entrada.")
        return False

# ==================== SEÑAL Ax2-D: CRUCE 15m + GATE 4H ====================
def evaluate_signals(symbol):
    """Detonante: cruce EMA7/21 en las ultimas SCAN_CANDLES velas de 15m CERRADAS.
    Brujula: la ultima vela 4H CERRADA define direccion permitida."""
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

        offsets = [(-2, -3), (-3, -4)][:SCAN_CANDLES] if SIGNAL_ON_CLOSE else [(-1, -2)]

        for i_curr, i_prev in offsets:
            prev_7, prev_21 = e7.iloc[i_prev], e21.iloc[i_prev]
            curr_7, curr_21 = e7.iloc[i_curr], e21.iloc[i_curr]

            candle_ts = int(df['timestamp'].iloc[i_curr])
            if prev_7 <= prev_21 and curr_7 > curr_21:
                if trend_4h:
                    return 'LONG', price, candle_ts
                log.info("Cruce alcista VETADO: 4H bajista.")
            if prev_7 >= prev_21 and curr_7 < curr_21:
                if not trend_4h:
                    return 'SHORT', price, candle_ts
                log.info("Cruce bajista VETADO: 4H alcista.")
    except Exception as e:
        log.error(f"Error evaluando senal: {e}")
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

# ==================== CAPACIDAD (need CORREGIDO) ====================
def capacity_report(symbol):
    try:
        market = exchange.market(symbol)
        ctval = float(market.get('contractSize') or 1)
        price = exchange.fetch_ticker(symbol).get('last') or 0
        need = ctval * AMOUNT * price   # leverage 1x — CORREGIDO
    except Exception as e:
        log.warning(f"Capacidad: nocional no calculable: {e}")
        return
    try:
        raw = exchange.privateGetAccountBalance()
        details = ((raw or {}).get('data') or [{}])[0].get('details') or []
        total = sum(_f(d.get('eqUsd')) for d in details)
        verdict = 'CABE' if total >= need else 'NO CABE'
        log.info(f"Margen entrada ({AMOUNT} contratos): ~${need:.2f} | "
                 f"colateral real: ~${total:.2f} -> {verdict}")
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
    if existing >= AMOUNT:
        log.info(f"Posicion {existing} contratos. Tope {AMOUNT}. Esperando SL/TP.")
        return

    if cooldown_active(symbol):
        log.info("Vigilando (cooldown activo).")
        return

    signal, price, candle_ts = evaluate_signals(symbol)
    if not signal:
        log.info("Sin cruces EMA7/21 en 15m alineados con 4H. Sin operacion.")
        return

    if candle_already_traded(symbol, candle_ts):
        return

    log.info(f"Senal {signal} (4H a favor, vela {candle_ts}). Abriendo {AMOUNT} contrato(s)...")
    try:
        execute_order(signal, symbol, price, AMOUNT, candle_ts)
    except Exception as e:
        log.error(f"Entrada rechazada: {e}")

def run_once():
    log.info("Modo ciclo unico (GitHub Actions) | SUI Ax2-D: 15m + gate 4H + cooldown 60m.")
    verify_setup()
    if TEST_MODE:
        catalog_sui()
        return
    run_cycle()

def main_loop():
    log.info(f"Iniciando bot {BASE_ASSET} Ax2-D | {TIMEFRAME}+{TF_FILTER} | "
             f"SL {SL_PCT:.1%} / TP {TP_PCT:.1%} | cooldown {COOLDOWN_MIN}m")
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
