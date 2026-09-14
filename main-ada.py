# main-ada.py — Ax2-D · 15m detonante + 4H brujula + cooldown · SL 1% / TP 1.5% · 1x
# MODO TEST ACTIVO: recorre todo el pipeline PERO no envia ordenes.
# Imprime lo que HUBIERA hecho (senal, lado, SL, TP, tamano) para auditoria del fichaje.
# INSTRUMENTO: XPERP ADA/USD (vencimiento) — USDT/SWAP PROHIBIDO en esta cuenta.
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
BASE_ASSET  = 'ADA'
AMOUNT      = 1.0        # contratos por entrada (unidad real la reporta OKX)
SL_PCT      = 0.010      # 1.0%
TP_PCT      = 0.015      # 1.5%
TIMEFRAME   = '15m'
TF_FILTER   = '4h'
TD_MODE     = 'cross'
LEVERAGE    = 1
COOLDOWN_MIN = 60
TEST_MODE       = os.getenv('TEST_MODE') == '1' or True   # ← TEST FORZADO hasta auditoria OK
SINGLE_CYCLE    = os.getenv('SINGLE_CYCLE') == '1'
# Guardia anti-unidad: se calibrara con el Tamano real del primer run.
# Valor provisional generoso: si ADA/USD trae ctVal grande, el test lo gritara.
MAX_NOTIONAL_USD = _f(os.getenv('OKX_ADA_MAX_NOTIONAL', '25.0')) or 25.0

SCAN_CANDLES  = 2
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

# ==================== INSTRUMENTO (XPERP USD — NUNCA USDT) ====================
def catalog_ada():
    exchange.load_markets()
    print(f"[{time.strftime('%H:%M:%S')}] --- CATALOGO ADA ---", flush=True)
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
    settle = str(exchange.market(symbol).get('settle') or '').upper()
    return settle == 'USDT'

def pick_future():
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
    exchange.load_markets()
    for m in exchange.markets.values():
        if (m.get('base') == BASE_ASSET and m.get('active')
                and (m.get('swap') or (m.get('info') or {}).get('instType') == 'SWAP')
                and not _is_forbidden(m['symbol'])):
            return m['symbol']
    return None

def _log_contract_size(sym):
    try:
        ctval, ccy, settle, _p, usd = _contract_meta(sym)
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
    raise RuntimeError("No hay ADA/USD (XPERP o SWAP-USD) activo. USDT prohibido en esta cuenta.")

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

# ==================== COOLDOWN ====================
def cooldown_active(symbol):
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

# ==================== SEÑAL Ax2-D: CRUCE 15m + GATE 4H ====================
def evaluate_signal(symbol):
    try:
        df_4h = fetch_data(symbol, TF_FILTER, limit=120)
        if len(df_4h) < 25:
            log.error(f"4H insuficiente ({len(df_4h)} velas). Sin senal.")
            return None, None
        e7h, e21h = ema(df_4h['close'], 7), ema(df_4h['close'], 21)
        trend_4h = e7h.iloc[-2] > e21h.iloc[-2]

        df = fetch_data(symbol, TIMEFRAME, limit=60)
        if len(df) < 25:
            log.error(f"{TIMEFRAME} insuficiente ({len(df)} velas). Sin senal.")
            return None, None
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
        log.info(f"Margen 1 contrato: ~${need:.2f} | colateral real: ~${total:.2f} -> {verdict}")
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

# ==================== CICLO (MODO TEST: NUNCA EJECUTA) ====================
def run_cycle():
    symbol = resolve_symbol()

    try:
        exchange.set_leverage(LEVERAGE, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.warning(f"No se pudo fijar apalancamiento (se usa el de OKX): {e}")

    capacity_ok(symbol)
    _log_maxsize(symbol)

    if cooldown_active(symbol):
        log.info("Vigilando (cooldown activo).")
        return

    signal, price = evaluate_signal(symbol)
    if not signal:
        log.info("Sin cruces EMA7/21 en 15m alineados con 4H. Vigilando.")
        return

    # ══════ MODO TEST: el disparo se SIMULA, no se envia ══════
    ctval, ccy, _settle, _p, usd = _contract_meta(symbol, price)
    nocional = usd * AMOUNT
    if signal == 'LONG':
        sl_raw, tp_raw = price * (1 - SL_PCT), price * (1 + TP_PCT)
    else:
        sl_raw, tp_raw = price * (1 + SL_PCT), price * (1 - TP_PCT)
    log.info(f"[TEST] HUBIERA EJECUTADO: {signal} | {AMOUNT} contrato(s) | "
             f"nocional ~${nocional:.2f} | SL≈{sl_raw:.5f} | TP≈{tp_raw:.5f}")
    log.info(f"[TEST] Guardia de nocional: {'CABE (< $'+str(MAX_NOTIONAL_USD)+')' if nocional <= MAX_NOTIONAL_USD else 'VETADA (>'+str(MAX_NOTIONAL_USD)+')'}")
    log.info("[TEST] Sin orden enviada. El fichaje queda en auditoria.")

def run_once():
    log.info("Modo ciclo unico (GitHub Actions) | ADA TEST — pipeline completo, SIN ordenes.")
    verify_setup()
    catalog_ada()
    run_cycle()

def main_loop():
    log.info(f"Iniciando bot {BASE_ASSET} TEST | {TIMEFRAME}+{TF_FILTER} | SIN ordenes hasta auditoria")
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
