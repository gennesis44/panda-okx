# main-fet.py — Ax2-D · 30m detonante + 4H brújula + cooldown · SL 1% / TP 1.5% · 1x · 2 contratos
# Ax3: HOST my.okx.com | clOrdId FET | cooldown fail-closed | no entrar si NO CABE | 51016
# Ax3.1: unidades honestas (ctValCcy) | warmup 4H 120 velas | guardia velas | posicion primero
# Ax3.2: guardia de nocional (MAX_NOTIONAL_USD) — unidad de contrato sorpresa = bot bloqueado
# INSTRUMENTO: XPERP FET/USD (vencimiento) — USDT/SWAP PROHIBIDO en esta cuenta (colateral no-USDT)
import os
import time
import logging

import ccxt
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

# ==================== CONFIGURACIÓN (Ax2-D + guardias) ====================
BASE_ASSET  = 'FET'
AMOUNT      = 2.0        # 2 contratos = 20 FET (~$3.44)
SL_PCT      = 0.010      # 1.0%
TP_PCT      = 0.015      # 1.5%
TIMEFRAME   = '30m'      # vela madre: detonante
TF_FILTER   = '4h'       # brujula: solo direccion (gate)
TD_MODE     = 'cross'
LEVERAGE    = 1
COOLDOWN_MIN = 60
TEST_MODE       = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE    = os.getenv('SINGLE_CYCLE') == '1'
# Guardia anti-unidad: 2 contratos = 20 FET (~$3.4). Tope $7.0 da holgura
# y veta cualquier unidad sorpresa (p.ej. 1000 FET/contrato = $170).
MAX_NOTIONAL_USD = _f(os.getenv('OKX_FET_MAX_NOTIONAL', '7.0')) or 7.0

CYCLE_SECONDS = 20 * 60   # ciclo cada 20 min (< 30m: ninguna vela se pierde)

HOST = 'https://my.okx.com'

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== INSTRUMENTO (XPERP USD — NUNCA USDT) ====================
def catalog_fet():
    exchange.load_markets()
    print(f"[{time.strftime('%H:%M:%S')}] --- CATALOGO FET ---", flush=True)
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET:
            continue
        info = m.get('info') or {}
        estado = 'activo' if m.get('active') else 'INACTIVO'
        print(f"  {m['symbol']} | {info.get('instType') or '?'} | {estado} | "
              f"ctVal={info.get('ctVal')} {info.get('ctValCcy') or '?'} | "
              f"settle={m.get('settle') or '?'}", flush=True)

def _inst_type(symbol):
    try:
        t = str((exchange.market(symbol).get('info') or {}).get('instType') or '').upper()
        if t in ('SWAP', 'FUTURES'):
            return t
    except Exception:
        pass
    m = exchange.market(symbol)
    return 'SWAP' if (m.get('swap') or m.get('type') == 'swap') else 'FUTURES'

def _contract_meta(symbol, price=None):
    """ctVal + ccy reales. Linear (FET): usd = ctVal * price. Inverse/USD-quoted: usd = ctVal."""
    market = exchange.market(symbol)
    info = market.get('info') or {}
    ctval = float(market.get('contractSize') or info.get('ctVal') or 1)
    ccy = str(info.get('ctValCcy') or BASE_ASSET).upper()
    settle = str(info.get('settleCcy') or market.get('settle') or '?').upper()
    if price is None:
        price = exchange.fetch_ticker(symbol).get('last') or 0
    price = _f(price)
    usd = ctval if ccy in ('USD', 'USDT', 'USDC') else ctval * price
    return ctval, ccy, settle, price, usd

def _is_forbidden(symbol):
    """VETO: cualquier asentamiento en USDT queda prohibido en esta cuenta."""
    settle = str(exchange.market(symbol).get('settle') or '').upper()
    return settle == 'USDT'

def pick_future():
    """XPERP con vencimiento mas lejano, SIEMPRE en USD (nunca USDT)."""
    exchange.load_markets()
    candidatos = []
    for m in exchange.markets.values():
        if (m.get('base') == BASE_ASSET and m.get('future') and m.get('active')
                and not _is_forbidden(m['symbol'])):
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

def pick_swap_usd():
    """Fallback: swap liquidado en USD (nunca USDT)."""
    exchange.load_markets()
    for m in exchange.markets.values():
        if (m.get('base') == BASE_ASSET and m.get('active')
                and (m.get('swap') or (m.get('info') or {}).get('instType') == 'SWAP')
                and not _is_forbidden(m['symbol'])):
            return m['symbol']
    return None

def _log_contract_size(sym):
    try:
        ctval, ccy, settle, _price, usd = _contract_meta(sym)
        log.info(f"Contrato: 1 = {ctval} {ccy} | settle={settle} | "
                 f"nocional ~${usd:.2f} | {sym} | {_inst_type(sym)}")
    except Exception as e:
        log.warning(f"No se pudo leer el tamano del contrato: {e}")

def resolve_symbol():
    exchange.load_markets()
    try:
        for p in exchange.fetch_positions():
            if (p.get('contracts') or 0) > 0 and (p.get('symbol') or '').startswith(f'{BASE_ASSET}/'):
                if _is_forbidden(p['symbol']):
                    log.error(f"VETO: posicion abierta en {p['symbol']} (USDT) — "
                              f"instrumento prohibido. Intervencion manual requerida.")
                    raise RuntimeError("Posicion abierta en instrumento USDT prohibido.")
                log.info(f"Instrumento (posicion abierta): {p['symbol']}")
                _log_contract_size(p['symbol'])
                return p['symbol']
    except RuntimeError:
        raise
    except Exception as e:
        log.warning(f"Posiciones no leidas al resolver simbolo: {e}")
    sym = pick_future()
    if sym:
        log.info(f"Instrumento: {sym} (XPERP — vencimiento mas lejano)")
        _log_contract_size(sym)
        return sym
    sym = pick_swap_usd()
    if sym:
        log.warning(f"Instrumento: {sym} (fallback SWAP-USD)")
        _log_contract_size(sym)
        return sym
    raise RuntimeError("No hay FET/USD (XPERP o SWAP-USD) activo. USDT prohibido en esta cuenta.")

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
    return f"FET{int(candle_ts)}"

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
        log.warning(f"No se pudo verificar duplicado ({msg}); BLOQUEO fail-closed.")
        return True
    except Exception as e:
        log.warning(f"No se pudo verificar duplicado ({e}); BLOQUEO fail-closed.")
        return True

def execute_order(side: str, symbol: str, ref_price: float, amount: float, candle_ts: int):
    ctval, ccy, _settle, _p, usd = _contract_meta(symbol, ref_price)
    nocional = amount * usd
    log.info(f"Nocional: {amount} contratos x {ctval} {ccy} (~${nocional:.2f})")

    if nocional > MAX_NOTIONAL_USD:
        raise RuntimeError(
            f"GUARDIA: nocional ${nocional:.2f} > maximo ${MAX_NOTIONAL_USD:.2f} "
            f"(ctVal={ctval} {ccy}). Unidad inesperada — NO SE OPERA.")

    if side == 'LONG':
        oside, sl_raw, tp_raw = 'buy', ref_price * (1 - SL_PCT), ref_price * (1 + TP_PCT)
    else:
        oside, sl_raw, tp_raw = 'sell', ref_price * (1 + SL_PCT), ref_price * (1 - TP_PCT)

    sl = exchange.price_to_precision(symbol, sl_raw)
    tp = exchange.price_to_precision(symbol, tp_raw)
    sz = exchange.amount_to_precision(symbol, amount)

    req = {
        'instId': exchange.market(symbol)['id'],
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
    try:
        resp = _post_trade_order(req)
    except ccxt.ExchangeError as e:
        if '51016' in str(e):
            log.info(f"OKX: clOrdId duplicado (51016) en vela {candle_ts} — idempotencia OK.")
            return True
        raise

    data = resp.get('data') or []
    d0 = data[0] if isinstance(data, list) and data else {}
    s_code = str(d0.get('sCode', resp.get('code', '1')))
    if s_code == '51016':
        log.info(f"OKX: clOrdId duplicado (51016) en vela {candle_ts} — idempotencia OK.")
        return True
    if s_code != '0':
        raise ccxt.ExchangeError(f"OKX rechazo la entrada: {d0.get('sMsg') or resp.get('msg')}")
    log.info(f"FET {side} ejecutada con SL/TP adjuntos. ordId: {d0.get('ordId')} | "
             f"SL: {sl} | TP: {tp}")
    return True

# ==================== COOLDOWN POST-PÉRDIDA ====================
def cooldown_active(symbol):
    """True si el ultimo cierre del par fue LOSS hace menos de COOLDOWN_MIN. Fail-closed."""
    try:
        market_id = exchange.market(symbol)['id']
        hist = exchange.privateGetTradeFillsHistory({
            'instType': _inst_type(symbol),
            'instId': market_id,
            'limit': '5',
        })
        for fill in (hist.get('data') or []):
            if str(fill.get('reduceOnly', '0')) == 'true' or fill.get('subType') in ('3', '4', '5', '6'):
                ts_ms = int(fill.get('ts') or 0)
                age_min = (time.time() * 1000 - ts_ms) / 60000.0
                pnl = _f(fill.get('pnl'))
                if pnl < 0 and age_min < COOLDOWN_MIN:
                    log.info(f"COOLDOWN: ultima perdida hace {age_min:.0f} min (< {COOLDOWN_MIN}).")
                    return True
                return False
        return False
    except Exception as e:
        log.warning(f"No se pudo verificar cooldown ({e}); BLOQUEO fail-closed.")
        return True

# ==================== SEÑAL Ax2-D: CRUCE 30m + GATE 4H ====================
def evaluate_signal(symbol):
    """Detonante: cruce EMA7/21 en velas de 30m CERRADAS (ventana doble).
    Brujula: la ultima vela 4H CERRADA define direccion permitida."""
    try:
        df_4h = fetch_data(symbol, TF_FILTER, limit=120)   # warmup honesto EMA21 4H
        if len(df_4h) < 25:
            log.error(f"4H insuficiente ({len(df_4h)} velas). Sin senal.")
            return None, None, None
        e7h, e21h = ema(df_4h['close'], 7), ema(df_4h['close'], 21)
        trend_4h = e7h.iloc[-2] > e21h.iloc[-2]

        df = fetch_data(symbol, TIMEFRAME, limit=100)
        if len(df) < 25:
            log.error(f"{TIMEFRAME} insuficiente ({len(df)} velas). Sin senal.")
            return None, None, None
        df['EMA_7']  = ema(df['close'], 7)
        df['EMA_21'] = ema(df['close'], 21)
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        e7, e21 = df['EMA_7'], df['EMA_21']
        log.info(f"Precio: {price} | {TIMEFRAME} EMA7/21: {e7.iloc[-2]:.5f}/{e21.iloc[-2]:.5f} | "
                 f"4H: {'ALCISTA' if trend_4h else 'BAJISTA'} (gate {'LONG' if trend_4h else 'SHORT'})")

        for i_curr in (-2, -3):
            i_prev = i_curr - 1
            candle_ts = int(df['timestamp'].iloc[i_curr])
            if e7.iloc[i_prev] <= e21.iloc[i_prev] and e7.iloc[i_curr] > e21.iloc[i_curr]:
                if trend_4h:
                    return 'LONG', price, candle_ts
                log.info("Cruce alcista VETADO: 4H bajista.")
            if e7.iloc[i_prev] >= e21.iloc[i_prev] and e7.iloc[i_curr] < e21.iloc[i_curr]:
                if not trend_4h:
                    return 'SHORT', price, candle_ts
                log.info("Cruce bajista VETADO: 4H alcista.")
    except Exception as e:
        log.error(f"Error en evaluacion: {e}")
    return None, None, None

def candle_already_traded_check(symbol, candle_ts: int) -> bool:
    # alias por compatibilidad — usa candle_already_traded
    return candle_already_traded(symbol, candle_ts)

# ==================== CAPACIDAD (guardia + colateral) ====================
def capacity_ok(symbol):
    try:
        _ctval, _ccy, _settle, _price, usd = _contract_meta(symbol)
        nocional = usd * AMOUNT
        if nocional > MAX_NOTIONAL_USD:
            log.error(f"GUARDIA: nocional ${nocional:.2f} > maximo ${MAX_NOTIONAL_USD:.2f} "
                      f"— unidad/AMOUNT inesperado. NO SE OPERA.")
            return False
        need = nocional / max(LEVERAGE, 1)
    except Exception as e:
        log.warning(f"Capacidad: nocional no calculable: {e}")
        return False
    try:
        raw = exchange.privateGetAccountBalance()
        details = ((raw or {}).get('data') or [{}])[0].get('details') or []
        total = sum(_f(d.get('eqUsd')) for d in details)
        verdict = 'CABE' if total >= need else 'NO CABE'
        log.info(f"Margen entrada ({AMOUNT} contratos): ~${need:.2f} | "
                 f"colateral real: ~${total:.2f} -> {verdict}")
        return total >= need
    except Exception as e:
        log.warning(f"Capacidad: colateral no calculable: {e} — BLOQUEO.")
        return False

def _log_maxsize(symbol):
    try:
        inst = exchange.market(symbol)['id']
        ms = exchange.privateGetAccountMaxSize({'instId': inst, 'tdMode': TD_MODE})
        d0 = (ms.get('data') or [{}])[0]
        mb, msz = d0.get('maxBuy'), d0.get('maxSell')
        if mb or msz:
            log.info(f"Capacidad OKX: maxBuy={mb} | maxSell={msz} contratos")
    except Exception as e:
        log.warning(f"MAXSIZE no disponible: {e}")

# ==================== ARRANQUE ====================
def verify_setup():
    raw = exchange.privateGetAccountBalance()
    details = ((raw or {}).get('data') or [{}])[0].get('details') or []
    total = sum(_f(d.get('eqUsd')) for d in details)
    log.info(f"Autenticacion OK | host={HOST} | Colateral real (valor USD): ~{total:.2f}")

# ==================== CICLO ====================
def run_cycle():
    symbol = resolve_symbol()

    try:
        exchange.set_leverage(LEVERAGE, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.warning(f"No se pudo fijar apalancamiento (se usa el de OKX): {e}")

    existing = get_position_contracts(symbol)
    if existing > 0:
        log.info(f"Posicion abierta: {existing} contratos. Esperando SL/TP.")
        return

    if not capacity_ok(symbol):
        log.info("Sin capacidad. Vigilando.")
        return

    _log_maxsize(symbol)

    if cooldown_active(symbol):
        log.info("Vigilando (cooldown activo).")
        return

    signal, price, candle_ts = evaluate_signal(symbol)
    if not signal:
        log.info("Sin cruces EMA7/21 en 30m alineados con 4H. Sin operacion.")
        return

    if candle_already_traded(symbol, candle_ts):
        return

    log.info(f"Senal {signal} (4H a favor, vela {candle_ts}). Abriendo {AMOUNT} contrato(s)...")
    try:
        execute_order(signal, symbol, price, AMOUNT, candle_ts)
    except Exception as e:
        log.error(f"Entrada rechazada: {e}")

def run_once():
    log.info("Modo ciclo unico (GitHub Actions) | FET Ax2-D + guardias (XPERP USD, USDT prohibido).")
    verify_setup()
    if TEST_MODE:
        catalog_fet()
        return
    run_cycle()

def main_loop():
    log.info(f"Iniciando bot {BASE_ASSET} Ax2-D | {TIMEFRAME}+{TF_FILTER} | "
             f"SL {SL_PCT:.1%} / TP {TP_PCT:.1%} | entrada {AMOUNT} contratos | "
             f"cooldown {COOLDOWN_MIN}m | {HOST}")
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
