# main-fet.py — Ax2-D-FET · REGLA CARBONO (decreto 20-sep):
#   "Entrada en el cruce entre EMA5/100 de vela 1H"
#   LONG : SL 6%  / TP 7.5%   (aire amplio — lección del fondo 0.1675)
#   SHORT: SL 3%  / TP 4%     (ajustado — los shorts de FET exigen precisión)
#   Ratio breakeven: LONG >44.4% WR · SHORT >42.9% WR
#   Ventana señal: -2/-3/-4 velas 1H CERRADAS (cubre cadencia ~3h del cron)
#   NOTA: variante SIN backtest aún — el backtest-xlm se extenderá si el
#     Carbono ordena. Sustituye a la variante 30m (17% WR, −0.1972).
# Ax3: HOST my.okx.com | clOrdId FET | cooldown fail-closed | no entrar si NO CABE | 51016
# Ax3.1: unidades honestas (ctValCcy) | guardia velas | posicion primero
# Ax3.2: guardia de nocional — unidad sorpresa = bot bloqueado
# INSTRUMENTO: XPERP FET/USD (vencimiento) — USDT PROHIBIDO (MiCA/EEE)
import os
import sys
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

# ==================== CONFIGURACIÓN (decreto Carbono) ====================
BASE_ASSET  = 'FET'
AMOUNT      = 2.0        # 2 contratos = 20 FET (~$3.5)
SL_LONG     = 0.060      # 6.0%
TP_LONG     = 0.075      # 7.5%
SL_SHORT    = 0.030      # 3.0%
TP_SHORT    = 0.040      # 4.0%
TIMEFRAME   = '1h'       # ÚNICO marco: cruce EMA5/100
EMA_FAST    = 5
EMA_SLOW    = 100
TD_MODE     = 'cross'
LEVERAGE    = 1
COOLDOWN_MIN = 60
TEST_MODE       = os.getenv('TEST_MODE') == '1'    # semántica estandarizada (bug invertido corregido)
SINGLE_CYCLE    = os.getenv('SINGLE_CYCLE') == '1'
MAX_NOTIONAL_USD = _f(os.getenv('OKX_FET_MAX_NOTIONAL', '7.0')) or 7.0
SIGNAL_WINDOW   = (-2, -3, -4)   # cubre gap de cadencia ~3h entre runs
WARMUP_1H       = 130            # EMA100 honesta necesita >= 100 velas cerradas

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
                    log.error(f"VETO: posicion abierta en {p['symbol']} (USDT) — "
                              f"prohibido. Intervencion manual requerida.")
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
    raise RuntimeError("No hay FET/USD (XPERP o SWAP-USD) activo. USDT prohibido.")

# ==================== INDICADORES ====================
def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()

def fetch_data(symbol, timeframe, limit=200):
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

# ==================== ENTRADA CON SL/TP ADJUNTOS (asimétricos por lado) ==========
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
            log.info(f"Vela ya operada (clOrdId {cl}). Duplicado bloqueado.")
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

    # SL/TP ASIMÉTRICOS — decreto Carbono
    if side == 'LONG':
        oside  = 'buy'
        sl_raw = ref_price * (1 - SL_LONG)
        tp_raw = ref_price * (1 + TP_LONG)
    else:
        oside  = 'sell'
        sl_raw = ref_price * (1 + SL_SHORT)
        tp_raw = ref_price * (1 - TP_SHORT)

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
    log.info(f"FET {side} ejecutada. SL: {sl} | TP: {tp} | ordId: {d0.get('ordId')}")
    return True

# ==================== COOLDOWN POST-PÉRDIDA ====================
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

# ==================== SEÑAL: CRUCE EMA5/100 EN 1H CERRADA ====================
def evaluate_signal(symbol):
    """[Decreto Carbono 20-sep]
    SENAL UNICA: cruce EMA5/EMA100 en velas 1H CERRADAS.
    LONG : EMA5 cruza ALCISTA  → SL 6% / TP 7.5%
    SHORT: EMA5 cruza BAJISTA  → SL 3% / TP 4%
    Ventana -2/-3/-4: cubre el gap de cadencia (~3h) del cron.
    Idempotencia por vela evita dobles entradas."""
    try:
        df = fetch_data(symbol, TIMEFRAME, limit=200)
        if len(df) < WARMUP_1H:
            log.error(f"1H insuficiente ({len(df)} velas < {WARMUP_1H}). Sin senal.")
            return None, None, None

        # descartar vela en formacion por si acaso
        now_ms = int(time.time() * 1000)
        if int(df['timestamp'].iloc[-1]) + 3600 * 1000 > now_ms:
            df = df.iloc[:-1].reset_index(drop=True)
            if len(df) < WARMUP_1H:
                log.error("1H insuficiente tras descartar vela en formacion.")
                return None, None, None

        e5  = ema(df['close'], EMA_FAST)
        e100 = ema(df['close'], EMA_SLOW)
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        log.info(f"Precio: {price} | 1H EMA5/100: {e5.iloc[-2]:.5f}/{e100.iloc[-2]:.5f} | "
                 f"EMA5 {'>' if e5.iloc[-2] > e100.iloc[-2] else '<'} EMA100 (estado)")

        for i_curr in SIGNAL_WINDOW:
            i_prev = i_curr - 1
            if abs(i_prev) > len(df) - 1:
                continue
            candle_ts = int(df['timestamp'].iloc[i_curr])

            up = (e5.iloc[i_prev] <= e100.iloc[i_prev]
                  and e5.iloc[i_curr] > e100.iloc[i_curr])
            down = (e5.iloc[i_prev] >= e100.iloc[i_prev]
                    and e5.iloc[i_curr] < e100.iloc[i_curr])

            if up:
                log.info(f"Cruce ALCISTA EMA5/100 detectado en vela {candle_ts}.")
                return 'LONG', price, candle_ts
            if down:
                log.info(f"Cruce BAJISTA EMA5/100 detectado en vela {candle_ts}.")
                return 'SHORT', price, candle_ts

        log.info("Sin cruce EMA5/100 en la ventana. Vigilando.")
    except Exception as e:
        log.error(f"Error en evaluacion: {e}")
    return None, None, None

# ==================== CAPACIDAD ====================
def capacity_ok(symbol):
    try:
        _ctval, _ccy, _settle, _price, usd = _contract_meta(symbol)
        nocional = usd * AMOUNT
        if nocional > MAX_NOTIONAL_USD:
            log.error(f"GUARDIA: nocional ${nocional:.2f} > maximo ${MAX_NOTIONAL_USD:.2f}. NO SE OPERA.")
            return False
        need = nocional / max(LEVERAGE, 1)
    except Exception as e:
        log.warning(f"Capacidad: nocional no calculable: {e}")
        return False
    try:
        raw = exchange.privateGetAccountBalance()
        details = ((raw or {}).get('data') or [{}])[0].get('details') or []
        total = sum(_f(d.get('eqUsd')) for d in details)
        log.info(f"Margen ({AMOUNT} ct): ~${need:.2f} | colateral: ~${total:.2f} -> "
                 f"{'CABE' if total >= need else 'NO CABE'}")
        return total >= need
    except Exception as e:
        log.warning(f"Capacidad: colateral no calculable: {e} — BLOQUEO.")
        return False

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

    pos = get_open_position(symbol)
    if pos:
        log.info(f"Posicion abierta ({pos['side']}). Esperando SL/TP.")
        return

    if not capacity_ok(symbol):
        log.info("Sin capacidad. Vigilando.")
        return

    if cooldown_active(symbol):
        log.info("Vigilando (cooldown activo).")
        return

    signal, price, candle_ts = evaluate_signal(symbol)
    if not signal:
        return

    if candle_already_traded(symbol, candle_ts):
        return

    log.info(f"Senal {signal} (cruce EMA5/100 1H). Abriendo {AMOUNT} contrato(s)...")
    try:
        execute_order(signal, symbol, price, AMOUNT, candle_ts)
    except Exception as e:
        log.error(f"Entrada rechazada: {e}")

def run_once():
    log.info("Modo ciclo unico | FET EMA5/100 1H | LONG SL6/TP7.5 | SHORT SL3/TP4 (XPERP USD).")
    verify_setup()
    if TEST_MODE:
        catalog_fet()
        return
    run_cycle()

def main_loop():
    log.info(f"Bot {BASE_ASSET} | 1H EMA{EMA_FAST}/EMA{EMA_SLOW} | "
             f"LONG SL{SL_LONG:.1%}/TP{TP_LONG:.1%} · SHORT SL{SL_SHORT:.1%}/TP{TP_SHORT:.1%} | "
             f"{AMOUNT} ct | cooldown {COOLDOWN_MIN}m | {HOST}")
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
        time.sleep(1200)

if __name__ == "__main__":
    if SINGLE_CYCLE:
        run_once()
    else:
        main_loop()
