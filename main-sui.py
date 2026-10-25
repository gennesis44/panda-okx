# main-sui.py — Ax2-SUI · EL CATAMARAN · decreto Carbono (ratificado 21-sep)
#   Variante A BIDIRECCIONAL:
#     SENAL UNICA: cruce EMA3/21 en velas 1H CERRADAS (ventana -2/-3/-4)
#     FILTRO OBLIGATORIO: separation_pct = abs(EMA3-EMA21)/EMA21*100 >= 0.17
#                         (el cruce NO basta; sin separacion suficiente NO se opera)
#     LONG  (cruce alcista): SL 4.0% / TP 5.5%
#     SHORT (cruce bajista): SL 3.0% / TP 4.5%
#   Arquitectura temporal:
#     4D = referencia macro (informativa) | 1H = senal oficial | 30m = apoyo (informativo)
#     La entrada nace SOLO del marco 1H. Nunca velas abiertas.
#   NOTA DE VALIDACION: el backtest historico (60 dias: 17 trades, WR 47%, PF 1.21)
#     se hizo con EMA3/15. NO es valido para EMA3/21 + filtro 0.17% — REVALIDAR.
#   AMOUNT: 1 contrato = 1 SUI (ctVal=1.0 confirmado por API)
#     → 10 contratos = 10 SUI (~$9.00 nocional) · guardia $15 lo protege
# Ax3: HOST my.okx.com | clOrdId SUI | cooldown fail-closed | no entrar si NO CABE | 51016
# Ax3.1: unidades honestas (ctValCcy) | posicion primero | vela cerrada siempre
# Ax3.2: guardia de nocional — unidad sorpresa = bot bloqueado
# Ax3.3: nunca asumir datos — ctVal/ctValCcy de la API, posicion no verificable = bloqueo,
#        veto USDT por settle/quote/instId (alineado con CLAUDE_CONTEXT.md)
# INSTRUMENTO: XPERP SUI/USD (vencimiento) — USDT PROHIBIDO (MiCA/EEE)
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

# ==================== CONFIGURACIÓN (decreto Carbono) ====================
BASE_ASSET = 'SUI'
AMOUNT = 10         # 1 contrato = 1 SUI (~$0.90) → 10 ct = 10 SUI (~$9.00)
SL_LONG = 0.040     # 4.0%
TP_LONG = 0.055     # 5.5%
SL_SHORT = 0.030    # 3.0%
TP_SHORT = 0.045    # 4.5%
TIMEFRAME = '1h'            # marco oficial de senal
SUPPORT_TIMEFRAME = '30m'   # apoyo operacional (informativo, no bloquea)
MACRO_TIMEFRAME = '4d'      # referencia macro (informativa, no bloquea)
EMA_FAST = 3
EMA_SLOW = 21
MIN_SEPARATION_PCT = 0.17   # filtro obligatorio de separacion EMA3/EMA21 (%)
TD_MODE = 'cross'
LEVERAGE = 1
COOLDOWN_MIN = 60
TEST_MODE = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE = os.getenv('SINGLE_CYCLE') == '1'
MAX_NOTIONAL_USD = _f(os.getenv('OKX_SUI_MAX_NOTIONAL', '15.0')) or 15.0
SIGNAL_WINDOW = (-2, -3, -4)
WARMUP_1H = 30

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
def catalog_sui():
    exchange.load_markets()
    print("[" + time.strftime('%H:%M:%S') + "] --- CATALOGO SUI ---", flush=True)
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET:
            continue
        info = m.get('info') or {}
        estado = 'activo' if m.get('active') else 'INACTIVO'
        print("  " + str(m['symbol']) + " | " + str(info.get('instType') or '?') +
              " | " + estado + " | ctVal=" + str(info.get('ctVal')) +
              " " + str(info.get('ctValCcy') or '?') +
              " | settle=" + str(m.get('settle') or '?'), flush=True)

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
    # Nunca asumir el tamano del contrato: ctVal y ctValCcy salen SIEMPRE de la API.
    ctval = _f(info.get('ctVal') or market.get('contractSize'))
    ccy = str(info.get('ctValCcy') or '').upper()
    if ctval <= 0 or not ccy:
        raise RuntimeError("Contrato sin ctVal/ctValCcy validos en la API (ctVal=" +
                           str(info.get('ctVal')) + ", ctValCcy=" + str(info.get('ctValCcy')) +
                           "). NO SE OPERA.")
    settle = str(info.get('settleCcy') or market.get('settle') or '?').upper()
    if price is None:
        price = exchange.fetch_ticker(symbol).get('last') or 0
    price = _f(price)
    if ccy in ('USD', 'USDC'):
        usd = ctval
    elif ccy == BASE_ASSET:
        if price <= 0:
            raise RuntimeError("Precio no valido (" + str(price) +
                               ") para valorar contrato en " + ccy + ". NO SE OPERA.")
        usd = ctval * price
    else:
        raise RuntimeError("ctValCcy inesperada (" + ccy + ") — unidad sorpresa. NO SE OPERA.")
    return ctval, ccy, settle, price, usd

def _is_forbidden(symbol):
    m = exchange.market(symbol)
    settle = str(m.get('settle') or '').upper()
    quote = str(m.get('quote') or '').upper()
    inst_id = str(m.get('id') or '').upper()
    return settle == 'USDT' or quote == 'USDT' or 'USDT' in inst_id

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
        log.info("Contrato: 1 = " + str(ctval) + " " + ccy + " | settle=" + settle +
                 " | nocional ~$" + format(usd, '.2f') + " | " + sym +
                 " | " + _inst_type(sym))
    except Exception as e:
        log.warning("No se pudo leer el tamano del contrato: " + str(e))

def resolve_symbol():
    exchange.load_markets()
    try:
        for p in exchange.fetch_positions():
            if (p.get('contracts') or 0) > 0 and (p.get('symbol') or '').startswith(BASE_ASSET + '/'):
                if _is_forbidden(p['symbol']):
                    log.error("VETO: posicion abierta en USDT — prohibido.")
                    raise RuntimeError("Posicion abierta en instrumento USDT prohibido.")
                log.info("Instrumento (posicion abierta): " + str(p['symbol']))
                _log_contract_size(p['symbol'])
                return p['symbol']
    except RuntimeError:
        raise
    except Exception as e:
        log.error("Posiciones no leidas al resolver simbolo: " + str(e) + " — BLOQUEO fail-closed.")
        raise RuntimeError("No se pudieron leer las posiciones: " + str(e))
    sym = pick_future()
    if sym:
        log.info("Instrumento: " + str(sym) + " (XPERP — vencimiento mas lejano)")
        _log_contract_size(sym)
        return sym
    sym = pick_swap_usd()
    if sym:
        log.warning("Instrumento: " + str(sym) + " (fallback SWAP-USD)")
        _log_contract_size(sym)
        return sym
    raise RuntimeError("No hay SUI/USD (XPERP o SWAP-USD) activo. USDT prohibido.")

# ==================== INDICADORES ====================
def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def fetch_data(symbol, timeframe, limit=200):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

def separation_pct(fast_val, slow_val):
    """Separacion porcentual: abs(EMA3 - EMA21) / EMA21 * 100. None si no calculable."""
    slow_val = _f(slow_val)
    if slow_val == 0:
        return None
    return abs(_f(fast_val) - slow_val) / slow_val * 100.0

# ==================== POSICIÓN ====================
class PositionCheckError(Exception):
    """La posicion no pudo verificarse: NUNCA se interpreta como 'sin posicion'."""

def get_open_position(symbol):
    try:
        for pos in exchange.fetch_positions([symbol]):
            if pos.get('symbol') == symbol and float(pos.get('contracts') or 0) > 0:
                return pos
    except Exception as e:
        log.error("Error consultando posiciones: " + str(e))
        raise PositionCheckError(str(e))
    return None

# ==================== ENTRADA CON SL/TP ASIMÉTRICOS ====================
def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def _cl_order_id(candle_ts):
    return "SUI" + str(int(candle_ts))

def candle_already_traded(symbol, candle_ts):
    cl = _cl_order_id(candle_ts)
    try:
        inst = exchange.market(symbol)['id']
        resp = exchange.private_get_trade_order({'instId': inst, 'clOrdId': cl})
        data = resp.get('data') or []
        if data and str(data[0].get('sCode', '0')) == '0':
            log.info("Vela ya operada (clOrdId " + cl + "). Duplicado bloqueado.")
            return True
        return False
    except ccxt.ExchangeError as e:
        msg = str(e)
        if '51603' in msg or 'does not exist' in msg.lower():
            return False
        log.warning("No se pudo verificar duplicado (" + msg + "); BLOQUEO fail-closed.")
        return True
    except Exception as e:
        log.warning("No se pudo verificar duplicado (" + str(e) + "); BLOQUEO fail-closed.")
        return True

def execute_order(side, symbol, ref_price, amount, candle_ts):
    if _is_forbidden(symbol):
        raise RuntimeError("VETO: instrumento USDT prohibido (" + str(symbol) + "). NO SE OPERA.")
    ctval, ccy, _settle, _p, usd = _contract_meta(symbol, ref_price)
    nocional = amount * usd
    log.info("Nocional: " + str(amount) + " contrato x " + str(ctval) + " " + ccy +
             " (~$" + format(nocional, '.2f') + ")")

    if nocional <= 0:
        raise RuntimeError("GUARDIA: nocional no valido ($" + format(nocional, '.2f') +
                           "). NO SE OPERA.")
    if nocional > MAX_NOTIONAL_USD:
        raise RuntimeError("GUARDIA: nocional $" + format(nocional, '.2f') +
                           " > maximo $" + format(MAX_NOTIONAL_USD, '.2f') +
                           " — unidad inesperada. NO SE OPERA.")

    if side == 'LONG':
        oside = 'buy'
        sl_raw = ref_price * (1 - SL_LONG)
        tp_raw = ref_price * (1 + TP_LONG)
    else:
        oside = 'sell'
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
            log.info("OKX: clOrdId duplicado (51016) — idempotencia OK.")
            return True
        raise

    data = resp.get('data') or []
    d0 = data[0] if isinstance(data, list) and data else {}
    s_code = str(d0.get('sCode', resp.get('code', '1')))
    if s_code == '51016':
        log.info("OKX: clOrdId duplicado (51016) — idempotencia OK.")
        return True
    if s_code != '0':
        raise ccxt.ExchangeError("OKX rechazo la entrada: " + str(d0.get('sMsg') or resp.get('msg')))
    log.info("SUI " + side + " ejecutada con SL/TP adjuntos. ordId: " +
             str(d0.get('ordId')) + " | SL: " + str(sl) + " | TP: " + str(tp))
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
                    log.info("COOLDOWN: ultima perdida hace " + format(age_min, '.0f') +
                             " min (< " + str(COOLDOWN_MIN) + ").")
                    return True
                return False
        return False
    except Exception as e:
        log.warning("No se pudo verificar cooldown (" + str(e) + "); BLOQUEO fail-closed.")
        return True

# ==================== CONTEXTO MULTI-MARCO (INFORMATIVO, NO BLOQUEA) ====================
def log_context_frames(symbol):
    """4D = referencia macro, 30m = apoyo operacional.
    Solo velas CERRADAS. Puramente informativo: nunca bloquea ni genera senal.
    Cualquier fallo se registra y se ignora; la entrada nace SOLO del marco 1H."""
    for label, tf in (('MACRO', MACRO_TIMEFRAME), ('APOYO', SUPPORT_TIMEFRAME)):
        try:
            offered = getattr(exchange, 'timeframes', None) or {}
            if offered and tf not in offered:
                log.info(label + " " + tf + ": marco no ofrecido por OKX/ccxt — omitido (informativo).")
                continue
            df = fetch_data(symbol, tf, limit=100)
            if df.empty:
                log.info(label + " " + tf + ": sin datos (informativo).")
                continue
            tf_ms = int(exchange.parse_timeframe(tf)) * 1000
            now_ms = int(time.time() * 1000)
            if int(df['timestamp'].iloc[-1]) + tf_ms > now_ms:
                df = df.iloc[:-1].reset_index(drop=True)
            if len(df) < EMA_SLOW + 5:
                log.info(label + " " + tf + ": velas cerradas insuficientes (" + str(len(df)) +
                         ") (informativo).")
                continue
            f = ema(df['close'], EMA_FAST)
            s = ema(df['close'], EMA_SLOW)
            sep = separation_pct(f.iloc[-1], s.iloc[-1])
            log.info(label + " " + tf + " (cerrada) EMA" + str(EMA_FAST) + "/" + str(EMA_SLOW) +
                     ": EMA" + str(EMA_FAST) + (" > " if f.iloc[-1] > s.iloc[-1] else " < ") +
                     "EMA" + str(EMA_SLOW) + " | separacion " +
                     (format(sep, '.3f') + "%" if sep is not None else "n/d") + " (informativo)")
        except Exception as e:
            log.warning(label + " " + tf + ": contexto no disponible (" + str(e) + ") — se ignora.")

# ==================== SEÑAL: CRUCE EMA3/21 1H CERRADA + FILTRO DE SEPARACIÓN ====================
def evaluate_signal(symbol):
    try:
        df = fetch_data(symbol, TIMEFRAME, limit=200)
        if len(df) < WARMUP_1H + 10:
            log.error("1H insuficiente (" + str(len(df)) + " velas). Sin senal.")
            return None, None, None

        now_ms = int(time.time() * 1000)
        if int(df['timestamp'].iloc[-1]) + 3600 * 1000 > now_ms:
            df = df.iloc[:-1].reset_index(drop=True)
            if len(df) < WARMUP_1H + 10:
                log.error("1H insuficiente tras descartar vela en formacion.")
                return None, None, None

        e_fast = ema(df['close'], EMA_FAST)
        e_slow = ema(df['close'], EMA_SLOW)
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        # Tras descartar la vela en formacion, -1 es la ultima vela CERRADA.
        log.info("Precio: " + str(price) + " | 1H EMA" + str(EMA_FAST) + "/" + str(EMA_SLOW) + ": " +
                 format(e_fast.iloc[-1], '.5f') + "/" + format(e_slow.iloc[-1], '.5f') +
                 " | EMA" + str(EMA_FAST) + " " + (">" if e_fast.iloc[-1] > e_slow.iloc[-1] else "<") +
                 " EMA" + str(EMA_SLOW) + " (estado)")

        log_context_frames(symbol)

        for i_curr in SIGNAL_WINDOW:
            i_prev = i_curr - 1
            idx = len(df) + i_curr
            if abs(i_prev) > len(df) - 1 or idx < WARMUP_1H:
                continue
            candle_ts = int(df['timestamp'].iloc[i_curr])

            up = (e_fast.iloc[i_prev] <= e_slow.iloc[i_prev]
                  and e_fast.iloc[i_curr] > e_slow.iloc[i_curr])
            down = (e_fast.iloc[i_prev] >= e_slow.iloc[i_prev]
                    and e_fast.iloc[i_curr] < e_slow.iloc[i_curr])

            if not (up or down):
                continue

            direction = 'LONG' if up else 'SHORT'
            nombre = 'ALCISTA' if up else 'BAJISTA'

            # FILTRO OBLIGATORIO: el cruce NO es suficiente.
            sep = separation_pct(e_fast.iloc[i_curr], e_slow.iloc[i_curr])
            if sep is None:
                log.warning("Cruce " + nombre + " EMA" + str(EMA_FAST) + "/" + str(EMA_SLOW) +
                            " (vela " + str(i_curr) + "): separacion no calculable -> NO OPERAR (fail-closed).")
                continue
            if sep >= MIN_SEPARATION_PCT:
                log.info("Cruce " + nombre + " EMA" + str(EMA_FAST) + "/" + str(EMA_SLOW) +
                         " (vela " + str(i_curr) + ") | Separación EMA = " + format(sep, '.3f') +
                         "% -> válida (min " + format(MIN_SEPARATION_PCT, '.2f') + "%). Senal " + direction + ".")
                return direction, price, candle_ts
            log.info("Cruce " + nombre + " EMA" + str(EMA_FAST) + "/" + str(EMA_SLOW) +
                     " (vela " + str(i_curr) + ") | Separación EMA = " + format(sep, '.3f') +
                     "% -> rechazada (min " + format(MIN_SEPARATION_PCT, '.2f') + "%). NO OPERAR.")

        log.info("Sin cruce EMA" + str(EMA_FAST) + "/" + str(EMA_SLOW) +
                 " valido en la ventana. Vigilando.")
    except Exception as e:
        log.error("Error en evaluacion: " + str(e))
    return None, None, None

# ==================== CAPACIDAD ====================
def capacity_ok(symbol):
    try:
        _ctval, _ccy, _settle, _price, usd = _contract_meta(symbol)
        nocional = usd * AMOUNT
        if nocional <= 0:
            log.error("GUARDIA: nocional no valido ($" + format(nocional, '.2f') + "). NO SE OPERA.")
            return False
        if nocional > MAX_NOTIONAL_USD:
            log.error("GUARDIA: nocional $" + format(nocional, '.2f') +
                      " > maximo $" + format(MAX_NOTIONAL_USD, '.2f') + ". NO SE OPERA.")
            return False
        need = nocional / max(LEVERAGE, 1)
    except Exception as e:
        log.warning("Capacidad: nocional no calculable: " + str(e))
        return False
    try:
        raw = exchange.privateGetAccountBalance()
        details = ((raw or {}).get('data') or [{}])[0].get('details') or []
        total = sum(_f(d.get('eqUsd')) for d in details)
        log.info("Margen (" + str(AMOUNT) + " ct): ~$" + format(need, '.2f') + " | colateral: ~$" +
                 format(total, '.2f') + " -> " + ('CABE' if total >= need else 'NO CABE'))
        return total >= need
    except Exception as e:
        log.warning("Capacidad: colateral no calculable: " + str(e) + " — BLOQUEO.")
        return False

# ==================== ARRANQUE ====================
def verify_setup():
    raw = exchange.privateGetAccountBalance()
    details = ((raw or {}).get('data') or [{}])[0].get('details') or []
    total = sum(_f(d.get('eqUsd')) for d in details)
    log.info("Autenticacion OK | host=" + HOST + " | Colateral real (USD): ~" + format(total, '.2f'))

# ==================== CICLO ====================
def run_cycle():
    symbol = resolve_symbol()

    try:
        exchange.set_leverage(LEVERAGE, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.warning("No se pudo fijar apalancamiento (se usa el de OKX): " + str(e))

    try:
        pos = get_open_position(symbol)
    except PositionCheckError:
        log.error("Posicion no verificable. BLOQUEO fail-closed; se espera al siguiente ciclo.")
        return
    if pos:
        log.info("Posicion abierta (" + str(pos['side']) + "). Esperando SL/TP.")
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

    log.info("Senal " + signal + " (cruce EMA" + str(EMA_FAST) + "/" + str(EMA_SLOW) +
             " 1H + separacion valida). Abriendo " + str(AMOUNT) + " contrato...")
    try:
        execute_order(signal, symbol, price, AMOUNT, candle_ts)
    except Exception as e:
        log.error("Entrada rechazada: " + str(e))

def run_once():
    log.info("Modo ciclo unico | SUI CATAMARAN EMA3/21 1H (sep>=0.17%) | LONG SL4/TP5.5 | SHORT SL3/TP4.5 (XPERP USD).")
    verify_setup()
    if TEST_MODE:
        catalog_sui()
        return
    run_cycle()

def main_loop():
    log.info("Bot " + BASE_ASSET + " CATAMARAN | 1H EMA3/21 sep>=0.17% | LONG SL4/TP5.5 · SHORT SL3/TP4.5 | " +
             str(AMOUNT) + " ct | cooldown " + str(COOLDOWN_MIN) + "m | " + HOST)
    verify_setup()
    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("Detenido por el usuario.")
            break
        except Exception as e:
            log.error("Error en el ciclo principal: " + str(e))
            time.sleep(60)
        time.sleep(1200)

if __name__ == "__main__":
    if SINGLE_CYCLE:
        run_once()
    else:
        main_loop()
