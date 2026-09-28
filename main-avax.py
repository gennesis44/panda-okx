# main-avax.py — Ax2-AVAX · EL OCTAVO · chasis Catamaran SUI 1:1
#   LEY VALIDADA (backtest-avax.py run 27-sep, unica que pasa los 4 criterios):
#     cruce EMA8/34 en velas 1H CERRADAS (ventana -2/-3/-4)
#     LONG y SHORT (bidireccional): SL 4.0% / TP 5.5%
#     27 trades · WR 37.0% (BE 27.3%) · PF 1.57 · +0.476%/trade · DD -10.3% · +12.86%
#   SIN RSI. SIN candado anti-rango (extirpado por A/B HBAR, confirmado XRP).
#   TALLADO v2 (27-sep): 1 ct = 10 AVAX (~$109) > collar $7.5 → el tallado
#     usa el MINIMO TRADABLE (0.1 ct ~ $10.9) si cabe bajo el guardia $15.
#     Fail-closed si ni el minimo cabe. Unidad honesta: medir y decidir.
#   Ax3: HOST my.okx.com | clOrdId AVAX | cooldown fail-closed | no entrar si NO CABE | 51016
#   Ax3.1: unidades honestas (ctValCcy) | posicion primero | vela cerrada siempre
#   Ax3.2: guardia de nocional — unidad sorpresa = bot bloqueado
# INSTRUMENTO: XPERP AVAX/USD (vencimiento mas lejano) — USDT PROHIBIDO (MiCA/EEE)
import os
import time
import math
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
BASE_ASSET = 'AVAX'
TARGET_NOTIONAL = 7.5     # collar ideal ~6-9 USD (si el minimo lo permite)
SL_LONG = 0.040     # 4.0%
TP_LONG = 0.055     # 5.5%
SL_SHORT = 0.040    # 4.0%
TP_SHORT = 0.055    # 5.5%
TIMEFRAME = '1h'
EMA_FAST = 8
EMA_SLOW = 34
TD_MODE = 'cross'
LEVERAGE = 1
COOLDOWN_MIN = 60
TEST_MODE = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE = os.getenv('SINGLE_CYCLE') == '1'
MAX_NOTIONAL_USD = _f(os.getenv('OKX_AVAX_MAX_NOTIONAL', '15.0')) or 15.0
SIGNAL_WINDOW = (-2, -3, -4)
WARMUP_1H = 40

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
def catalog_avax():
    exchange.load_markets()
    print("[" + time.strftime('%H:%M:%S') + "] --- CATALOGO AVAX ---", flush=True)
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
        log.warning("Posiciones no leidas al resolver simbolo: " + str(e))
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
    raise RuntimeError("No hay AVAX/USD (XPERP o SWAP-USD) activo. USDT prohibido.")

# ==================== INDICADORES ====================
def ema(s, length):
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
        log.error("Error consultando posiciones: " + str(e))
    return None

# ==================== TALLADO v2 (minimo fraccional si cabe) ====================
def compute_amount(symbol):
    """Tallado del collar en USD REALES (ctVal x precio).
    v2: si 1 ct entero supera el collar pero el MINIMO tradable
    (minSz, multiplo de lotSz) cabe bajo el guardia $15 -> se usa el
    minimo con aviso. Fail-closed si ni el minimo cabe."""
    ctval, ccy, _settle, price, usd1 = _contract_meta(symbol)
    log.info("Contrato: 1 ct = " + str(ctval) + " " + ccy + " = ~$" +
             format(usd1, '.4f') + " (precio " + str(price) + ")")

    m = exchange.market(symbol)
    info = m.get('info') or {}
    lot = _f(info.get('lotSz')) or 0.1
    minsz = _f(info.get('minSz')) or 0.1

    c = math.floor((TARGET_NOTIONAL / usd1) / lot) * lot
    if c < minsz:
        c = minsz
    nocional = c * usd1

    if nocional > TARGET_NOTIONAL and nocional <= MAX_NOTIONAL_USD:
        log.warning("Collar ~$" + format(TARGET_NOTIONAL, '.2f') +
                    " no alcanzable: 1 ct entero = $" + format(usd1, '.2f') +
                    ". Se usa el MINIMO tradable: " + str(c) + " ct (~$" +
                    format(nocional, '.2f') + ", bajo guardia $" +
                    format(MAX_NOTIONAL_USD, '.2f') + ").")
    if nocional > MAX_NOTIONAL_USD:
        raise RuntimeError("GUARDIA: minimo tradable (" + str(c) + " ct = $" +
                           format(nocional, '.2f') + ") > maximo $" +
                           format(MAX_NOTIONAL_USD, '.2f') +
                           " — unidad inesperada. NO SE OPERA.")
    log.info("Tallado: " + str(c) + " ct | nocional REAL ~$" + format(nocional, '.2f') + " USD")
    return c

# ==================== ENTRADA CON SL/TP ADJUNTOS ====================
def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def _cl_order_id(candle_ts):
    return "AVAX" + str(int(candle_ts))

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
    ctval, ccy, _settle, _p, usd = _contract_meta(symbol, ref_price)
    nocional = amount * usd
    log.info("Nocional: " + str(amount) + " contrato x " + str(ctval) + " " + ccy +
             " (~$" + format(nocional, '.2f') + ")")

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
    log.info("AVAX " + side + " ejecutada con SL/TP adjuntos. ordId: " +
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

# ==================== SEÑAL: CRUCE EMA8/34 1H CERRADA ====================
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

        ef = ema(df['close'], EMA_FAST)
        es = ema(df['close'], EMA_SLOW)
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        log.info("Precio: " + str(price) + " | 1H EMA8/34: " +
                 format(ef.iloc[-2], '.5f') + "/" + format(es.iloc[-2], '.5f') +
                 " | EMA8 " + (">" if ef.iloc[-2] > es.iloc[-2] else "<") + " EMA34 (estado)")

        for i_curr in SIGNAL_WINDOW:
            i_prev = i_curr - 1
            idx = len(df) + i_curr
            if abs(i_prev) > len(df) - 1 or idx < WARMUP_1H:
                continue
            candle_ts = int(df['timestamp'].iloc[i_curr])

            up = (ef.iloc[i_prev] <= es.iloc[i_prev]
                  and ef.iloc[i_curr] > es.iloc[i_curr])
            down = (ef.iloc[i_prev] >= es.iloc[i_prev]
                    and ef.iloc[i_curr] < es.iloc[i_curr])

            if up:
                log.info("Cruce ALCISTA EMA8/34 detectado. Senal LONG.")
                return 'LONG', price, candle_ts
            if down:
                log.info("Cruce BAJISTA EMA8/34 detectado. Senal SHORT.")
                return 'SHORT', price, candle_ts

        log.info("Sin cruce EMA8/34 en la ventana. Vigilando.")
    except Exception as e:
        log.error("Error en evaluacion: " + str(e))
    return None, None, None

# ==================== CAPACIDAD ====================
def capacity_ok(symbol, amount):
    try:
        _ctval, _ccy, _settle, _price, usd = _contract_meta(symbol)
        nocional = usd * amount
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
        log.info("Margen: ~$" + format(need, '.2f') + " | colateral: ~$" +
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

    pos = get_open_position(symbol)
    if pos:
        log.info("Posicion abierta (" + str(pos['side']) + "). Esperando SL/TP.")
        return

    try:
        amount = compute_amount(symbol)
    except Exception as e:
        log.error("Tallado/guardia: " + str(e))
        return

    if not capacity_ok(symbol, amount):
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

    log.info("Senal " + signal + " (cruce EMA8/34 1H). Abriendo " + str(amount) + " contrato...")
    try:
        execute_order(signal, symbol, price, amount, candle_ts)
    except Exception as e:
        log.error("Entrada rechazada: " + str(e))

def run_once():
    log.info("Modo ciclo unico | AVAX OCTAVO EMA8/34 1H | SL4/TP5.5 (XPERP USD).")
    verify_setup()
    if TEST_MODE:
        catalog_avax()
        return
    run_cycle()

def main_loop():
    log.info("Bot " + BASE_ASSET + " OCTAVO | 1H EMA8/34 | SL4/TP5.5 bidireccional | " +
             "minimo tradable bajo guardia $" + format(MAX_NOTIONAL_USD, '.2f') +
             " | cooldown " + str(COOLDOWN_MIN) + "m | " + HOST)
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
