# bot_hbar.py — Ax2-HBAR · EL DESTRUCTOR v3 · chasis Catamaran (SUI)
#   ENMIENDA v3 ratificada por backtest rev.3 (comparador de stacks):
#     Stack A [3/4/10/21/27] 30m RATIFICADO sin cambios (no cortar EMAs):
#       73 trades · WR 32.9% · PF 1.22 · ret +14.80% · avg +0.228%/trade
#       TP/SL/CRUCE 15/22/36 · payoff 2.75 · breakeven 26.7%
#   SENAL (simetrica, fiel al backtest): FORMACION de stack COMPLETO
#     en velas 30m CERRADAS (ventana -2/-3/-4), desde cualquier estado previo:
#     LONG  = 3>4>10>21>27 recien formado
#     SHORT = 3<4<10<21<27 recien formado   <-- v2 NO veia esta senal (el freno)
#   SL/TP: 2.0% / 5.5% ambas direcciones (reemplaza SL3/TP2 de v2: payoff 0.67
#     exigia WR 60% — insostenible con WR real 32.9%)
#   Ax4: CIERRE POR SENAL CONTRARIA (flip) — igual que backtest rev.3
#   TAMANO: 1 contrato = 100 HBAR (~$9.35 nocional) · guardia $15 -> max 1 ct
#   HOST my.okx.com | clOrdId HBAR | cooldown fail-closed | USDT PROHIBIDO
import os
import time
import logging

import ccxt
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

VERSION = "3"

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

# ==================== CONFIGURACIÓN (enmienda v3) ====================
BASE_ASSET = 'HBAR'
AMOUNT = 1          # 1 contrato = 100 HBAR (~$9.35)
SL_LONG = 0.020     # 2.0% (backtest rev.3)
TP_LONG = 0.055     # 5.5% (backtest rev.3)
SL_SHORT = 0.020    # 2.0%
TP_SHORT = 0.055    # 5.5%
TIMEFRAME = '30m'
EMAS = [3, 4, 10, 21, 27]
TD_MODE = 'cross'
LEVERAGE = 1
COOLDOWN_MIN = 60
TEST_MODE = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE = os.getenv('SINGLE_CYCLE') == '1'
MAX_NOTIONAL_USD = _f(os.getenv('OKX_HBAR_MAX_NOTIONAL', '15.0')) or 15.0
SIGNAL_WINDOW = (-2, -3, -4)
WARMUP = 40

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
def catalog_hbar():
    exchange.load_markets()
    print("[" + time.strftime('%H:%M:%S') + "] --- CATALOGO HBAR ---", flush=True)
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
    raise RuntimeError("No hay HBAR/USD (XPERP o SWAP-USD) activo. USDT prohibido.")

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

# ==================== ÓRDENES ====================
def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def _cl_order_id(candle_ts):
    return "HBAR" + str(int(candle_ts))

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
    log.info("HBAR " + side + " ejecutada con SL/TP adjuntos. ordId: " +
             str(d0.get('ordId')) + " | SL: " + str(sl) + " | TP: " + str(tp))
    return True

# ==================== v3: CIERRE POR SENAL CONTRARIA ====================
def close_position(pos, symbol):
    """Cierra la posicion a mercado (reduceOnly). Fail-closed: lanza si OKX rechaza."""
    sz = exchange.amount_to_precision(symbol, abs(_f(pos.get('contracts'))))
    if _f(sz) <= 0:
        raise RuntimeError("Tamano de posicion ilegible — no cierro a ciegas.")
    req = {
        'instId': exchange.market(symbol)['id'],
        'tdMode': TD_MODE,
        'side': 'sell' if pos.get('side') == 'long' else 'buy',
        'ordType': 'optimal_limit_ioc',
        'sz': sz,
        'reduceOnly': 'true',
    }
    pos_side = str((pos.get('info') or {}).get('posSide') or 'net')
    if pos_side in ('long', 'short'):
        req['posSide'] = pos_side
    resp = _post_trade_order(req)
    data = resp.get('data') or []
    d0 = data[0] if isinstance(data, list) and data else {}
    s_code = str(d0.get('sCode', resp.get('code', '1')))
    if s_code != '0':
        raise ccxt.ExchangeError("OKX rechazo el cierre: " +
                                 str(d0.get('sMsg') or resp.get('msg')))
    log.info("HBAR posicion " + str(pos.get('side')) + " CERRADA por senal contraria. " +
             "ordId: " + str(d0.get('ordId')))
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

# ==================== SEÑAL v3: FORMACION DE STACK COMPLETO (30M CERRADA) ====================
def evaluate_signal(symbol):
    try:
        df = fetch_data(symbol, TIMEFRAME, limit=200)
        if len(df) < WARMUP + 10:
            log.error("30m insuficiente (" + str(len(df)) + " velas). Sin senal.")
            return None, None, None

        now_ms = int(time.time() * 1000)
        bar_ms = 30 * 60 * 1000
        if int(df['timestamp'].iloc[-1]) + bar_ms > now_ms:
            df = df.iloc[:-1].reset_index(drop=True)
            if len(df) < WARMUP + 10:
                log.error("30m insuficiente tras descartar vela en formacion.")
                return None, None, None

        emas = {p: ema(df['close'], p) for p in EMAS}

        def stack_up(i):
            vals = [emas[p].iloc[i] for p in EMAS]
            return all(vals[j] > vals[j + 1] for j in range(len(vals) - 1))

        def stack_down(i):
            vals = [emas[p].iloc[i] for p in EMAS]
            return all(vals[j] < vals[j + 1] for j in range(len(vals) - 1))

        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        vals = [format(emas[p].iloc[-2], '.5f') for p in EMAS]
        if stack_up(-2):
            estado = "ALCISTA completo"
        elif stack_down(-2):
            estado = "BAJISTA completo"
        else:
            estado = "mixto"
        log.info("Precio: " + str(price) + " | 30m EMAs " + "/".join(vals) +
                 " | stack " + estado)

        for i_curr in SIGNAL_WINDOW:
            i_prev = i_curr - 1
            if abs(i_prev) > len(df) - 1:
                continue
            candle_ts = int(df['timestamp'].iloc[i_curr])

            up_now, up_prev = stack_up(i_curr), stack_up(i_prev)
            dn_now, dn_prev = stack_down(i_curr), stack_down(i_prev)

            if up_now and not up_prev:
                log.info("Stack ALCISTA COMPLETO formado (3>4>10>21>27). Senal LONG.")
                return 'LONG', price, candle_ts
            if dn_now and not dn_prev:
                log.info("Stack BAJISTA COMPLETO formado (3<4<10<21<27). Senal SHORT.")
                return 'SHORT', price, candle_ts

        log.info("Sin formacion de stack completo en la ventana. Vigilando.")
    except Exception as e:
        log.error("Error en evaluacion: " + str(e))
    return None, None, None

# ==================== CAPACIDAD ====================
def capacity_ok(symbol):
    try:
        _ctval, _ccy, _settle, _price, usd = _contract_meta(symbol)
        nocional = usd * AMOUNT
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
        log.info("Margen (1 ct): ~$" + format(need, '.2f') + " | colateral: ~$" +
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

# ==================== CICLO (evalua senal SIEMPRE, con o sin posicion) ====================
def run_cycle():
    symbol = resolve_symbol()

    try:
        exchange.set_leverage(LEVERAGE, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.warning("No se pudo fijar apalancamiento (se usa el de OKX): " + str(e))

    signal, price, candle_ts = evaluate_signal(symbol)

    pos = get_open_position(symbol)
    flipped = False
    if pos:
        lado = str(pos.get('side'))
        contrario = ((lado == 'long' and signal == 'SHORT') or
                     (lado == 'short' and signal == 'LONG'))
        if not contrario:
            log.info("Posicion abierta (" + lado + "). Sin senal contraria. Esperando SL/TP.")
            return
        log.info("SENAL CONTRARIA (" + str(signal) + ") — girando posicion " + lado + "...")
        try:
            close_position(pos, symbol)
        except Exception as e:
            log.error("Cierre por senal FALLO: " + str(e) + " — no abro nada encima. Fail-closed.")
            return
        residual = get_open_position(symbol)
        if residual:
            log.warning("Cierre parcial/pendiente (quedan " +
                        str(residual.get('contracts')) + " ct). No abro nada este ciclo.")
            return
        flipped = True

    if not signal:
        return

    if not capacity_ok(symbol):
        log.info("Sin capacidad. Vigilando.")
        return

    if not flipped and cooldown_active(symbol):
        log.info("Vigilando (cooldown activo).")
        return

    if candle_already_traded(symbol, candle_ts):
        return

    log.info("Senal " + signal + " (stack completo 30m). Abriendo " + str(AMOUNT) + " contrato...")
    try:
        execute_order(signal, symbol, price, AMOUNT, candle_ts)
    except Exception as e:
        log.error("Entrada rechazada: " + str(e))

def run_once():
    log.info(">>> DESTRUCTOR v" + str(VERSION) +
             " | HBAR stack 3/4/10/21/27 30m | senal: formacion COMPLETA (ambas direcciones) | "
             "SL2/TP5.5 | flip por senal | XPERP USD.")
    verify_setup()
    if TEST_MODE:
        catalog_hbar()
        return
    run_cycle()

def main_loop():
    log.info("Bot " + BASE_ASSET + " DESTRUCTOR v" + str(VERSION) +
             " | 30m stack EMA " + str(EMAS) + " | senal formacion completa | SL2/TP5.5 | "
             "flip senal contraria | " + str(AMOUNT) + " ct | cooldown " +
             str(COOLDOWN_MIN) + "m | " + HOST)
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
        time.sleep(1800)

if __name__ == "__main__":
    if SINGLE_CYCLE:
        run_once()
    else:
        main_loop()
