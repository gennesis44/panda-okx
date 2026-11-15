#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# bot_hbar.py — Ax2-HBAR · EL DESTRUCTOR v4 · chasis Catamaran (SUI)
#   ENMIENDA v4 (orden del Capitán, 15-11-2026, dique seco):
#     1) VENTANA: 2 velas 30m CERRADAS (-1, -2) + la que se FORMA.
#        La formación en la vela más reciente (-1) dispara YA (v3 escaneaba
#        -2/-3/-4 y la formación fresca tardaba una vela extra en sonar).
#        La vela en formación NO da señal: es la ventana de entrada.
#     2) PERRO (SL) POR ESTRUCTURA — candados según análisis y lógica:
#        ancla = extremo de las 2 cerradas + la vela en formación (hi/lo
#        provisionales) ± 2 ticks — MISMA métrica que el aprendiz SUI.
#        Candado inferior (piso): max(ATR14 30m en vivo, 0.5%) — el perro
#        nunca muerde más cerca que un ATR de ruido ni por debajo de fees.
#        Candado superior (techo): 2.0% — doctrina backtest rev.3; si la
#        estructura pide más riesgo, el SL se cierra al techo (se registra).
#        Medido en vivo (15-11): ATR14 30m = 1.45% del px; estructura LONG
#        0.73% (se ensancha a piso ATR), estructura SHORT 1.73% (pasa).
#     3) CAÑONERA (TP) = 1.5 x riesgo (RR FLOTA 1.5 -> breakeven WR 40.0%).
#        MISMAS métricas para el perro y la flota (SUI/FANAL/DESTRUCTOR).
#        AVISO honesto: el WR 32.9% del backtest se midió con TP 5.5/SL 2.0;
#        con TP 1.5R el TP queda más cerca y el WR real SUBE (valor a medir).
#        Si WR vivo < 40%, doctrina flota manda igual: tamaños de centavos.
#     4) PRESUPUESTO: guardia $15 -> $9.00 ("hasta 9$"). 1 ct = 100 HBAR
#        = $5.59 al px de calibración — CABE con margen hasta px 0.09.
#   Señal (v3 intacta): FORMACION de stack COMPLETO 3/4/10/21/27 en 30m
#     cerradas — LONG = 3>4>10>21>27 recién formado; SHORT = 3<4<10<21<27.
#   Ax4: cierre por señal contraria (flip) · Ax5 (panda): one-way forzado.
#   TAMANO: 1 contrato = 100 HBAR · guardia $9 -> max 1 ct
#   HOST my.okx.com | clOrdId HBAR | cooldown fail-closed | USDT PROHIBIDO
import os
import time
import logging

import ccxt
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

VERSION = "4"

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

# ==================== CONFIGURACIÓN (enmienda v4) ====================
BASE_ASSET = 'HBAR'
AMOUNT = 1          # 1 contrato = 100 HBAR (~$5.59 en calibración v4)
SL_CAP = 0.020      # TECHO de doctrina backtest rev.3 (candado superior)
RR_FLOTA = 1.5      # TP = 1.5 x riesgo — métrica de la flota (breakeven WR 40%)
FLOOR_ABS = 0.005   # piso absoluto: fees + deslizamiento nunca se comen al perro
TIMEFRAME = '30m'
EMAS = [3, 4, 10, 21, 27]
TD_MODE = 'cross'
LEVERAGE = 1
COOLDOWN_MIN = 60
TEST_MODE = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE = os.getenv('SINGLE_CYCLE') == '1'
MAX_NOTIONAL_USD = _f(os.getenv('OKX_HBAR_MAX_NOTIONAL', '9.0')) or 9.0
SIGNAL_WINDOW = (-1, -2)   # v4: 2 velas CERRADAS + la que se FORMA (entrada)
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

def wilder_atr(df, period=14):
    """ATR de Wilder sobre velas CERRADAS (listas de floats)."""
    highs = [float(x) for x in df['high']]
    lows = [float(x) for x in df['low']]
    closes = [float(x) for x in df['close']]
    if len(closes) < period + 1:
        return 0.0
    trs = [max(highs[i] - lows[i],
               abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))]
    atr = sum(trs[:period]) / period
    for x in trs[period:]:
        atr = (atr * (period - 1) + x) / period
    return atr

def fetch_data(symbol, timeframe, limit=200):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

def tick_size(symbol):
    try:
        return _f(exchange.market(symbol)['info'].get('tickSz')) or 1e-5
    except Exception:
        return 1e-5

# ==================== POSICIÓN ====================
def get_open_position(symbol):
    try:
        for pos in exchange.fetch_positions([symbol]):
            if pos.get('symbol') == symbol and float(pos.get('contracts') or 0) > 0:
                return pos
    except Exception as e:
        log.error("Error consultando posiciones: " + str(e))
    return None

# ==================== CANDADOS v4 (puro, testeable offline) ====================
def perro_y_canonera(side, entry, win_hi, win_lo, atr, tick):
    """Perro (SL) por estructura + cañonera (TP) RR flota 1.5.
    Estructura: LONG -> min low de la ventana - 2 ticks; SHORT -> max high + 2 ticks.
    Ventana v4 = 2 cerradas + la vela en formación (hi/lo provisionales).
    Candados: piso = max(ATR14 30m, 0.5%) · techo = 2.0% (doctrina backtest).
    Devuelve (sl, tp, motivo) o (None, None, motivo) si la estructura rota en contra."""
    if side == 'LONG':
        sl_struct = win_lo - 2 * tick
        dist = entry - sl_struct
    else:
        sl_struct = win_hi + 2 * tick
        dist = sl_struct - entry
    if dist <= 0:
        return None, None, ("estructura rota en contra (entry %s fuera del extremo "
                            "de la ventana) — NO SE OPERA" % format(entry, '.5f'))
    piso = max(atr, entry * FLOOR_ABS)
    piso_origen = "ATR14" if atr >= entry * FLOOR_ABS else "0.5% absoluto"
    techo = entry * SL_CAP
    if dist < piso:
        dist_c = piso
        motivo = "perro ensanchado al piso %s (%.2f%% -> %.2f%%)" % (
            piso_origen, dist / entry * 100.0, dist_c / entry * 100.0)
    elif dist > techo:
        dist_c = techo
        motivo = "perro cerrado al techo doctrina (%.2f%% -> %.2f%%)" % (
            dist / entry * 100.0, dist_c / entry * 100.0)
    else:
        dist_c = dist
        motivo = "perro estructural puro (%.2f%%)" % (dist / entry * 100.0)
    if side == 'LONG':
        sl = entry - dist_c
        tp = entry + RR_FLOTA * dist_c
    else:
        sl = entry + dist_c
        tp = entry - RR_FLOTA * dist_c
    return sl, tp, motivo

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

def execute_order(side, symbol, ref_price, amount, candle_ts, sl_raw, tp_raw, motivo):
    ctval, ccy, _settle, _p, usd = _contract_meta(symbol, ref_price)
    nocional = amount * usd
    log.info("Nocional: " + str(amount) + " contrato x " + str(ctval) + " " + ccy +
             " (~$" + format(nocional, '.2f') + ") | candado: " + motivo)

    if nocional > MAX_NOTIONAL_USD:
        raise RuntimeError("GUARDIA: nocional $" + format(nocional, '.2f') +
                           " > presupuesto $" + format(MAX_NOTIONAL_USD, '.2f') +
                           " — NO SE OPERA.")

    sl = exchange.price_to_precision(symbol, sl_raw)
    tp = exchange.price_to_precision(symbol, tp_raw)
    sz = exchange.amount_to_precision(symbol, amount)

    req = {
        'instId': exchange.market(symbol)['id'],
        'tdMode': TD_MODE,
        'side': 'buy' if side == 'LONG' else 'sell',
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
    log.info("HBAR " + side + " ejecutada con perro/cañonera adjuntos. ordId: " +
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

# ==================== SEÑAL v4: 2 CERRADAS + LA QUE SE FORMA ====================
def evaluate_signal(symbol):
    """Ventana v4: las 2 velas 30m CERRADAS dan la señal; la vela EN FORMACIÓN
    es la ventana de entrada y aporta su hi/lo provisional al perro."""
    try:
        df = fetch_data(symbol, TIMEFRAME, limit=200)
        if len(df) < WARMUP + 10:
            log.error("30m insuficiente (" + str(len(df)) + " velas). Sin senal.")
            return None

        now_ms = int(time.time() * 1000)
        bar_ms = 30 * 60 * 1000
        forming = None
        if int(df['timestamp'].iloc[-1]) + bar_ms > now_ms:
            forming = {'hi': float(df['high'].iloc[-1]), 'lo': float(df['low'].iloc[-1])}
            df = df.iloc[:-1].reset_index(drop=True)
            if len(df) < WARMUP + 10:
                log.error("30m insuficiente tras descartar vela en formacion.")
                return None

        emas = {p: ema(df['close'], p) for p in EMAS}

        def stack_up(i):
            vals = [emas[p].iloc[i] for p in EMAS]
            return all(vals[j] > vals[j + 1] for j in range(len(vals) - 1))

        def stack_down(i):
            vals = [emas[p].iloc[i] for p in EMAS]
            return all(vals[j] < vals[j + 1] for j in range(len(vals) - 1))

        price = exchange.fetch_ticker(symbol).get('last') or float(df['close'].iloc[-1])
        atr = wilder_atr(df)

        # extremo combinado de la ventana: 2 cerradas + la que se forma
        win_hi = max(float(df['high'].iloc[-1]), float(df['high'].iloc[-2]))
        win_lo = min(float(df['low'].iloc[-1]), float(df['low'].iloc[-2]))
        if forming:
            win_hi = max(win_hi, forming['hi'])
            win_lo = min(win_lo, forming['lo'])

        vals = [format(emas[p].iloc[-1], '.5f') for p in EMAS]
        if stack_up(-1):
            estado = "ALCISTA completo"
        elif stack_down(-1):
            estado = "BAJISTA completo"
        else:
            estado = "mixto"
        log.info("Precio: " + str(price) + " | 30m EMAs " + "/".join(vals) +
                 " (ult cerrada) | stack " + estado +
                 " | ventana v4: h=" + format(win_hi, '.5f') + " l=" + format(win_lo, '.5f') +
                 ("" if forming is None else " (+ formacion h=" + format(forming['hi'], '.5f') +
                  " l=" + format(forming['lo'], '.5f') + ")") +
                 " | ATR14 " + format(atr, '.5f'))

        for i_curr in SIGNAL_WINDOW:
            i_prev = i_curr - 1
            if abs(i_prev) > len(df) - 1 or abs(i_curr) > len(df) - 1:
                continue
            candle_ts = int(df['timestamp'].iloc[i_curr])

            up_now, up_prev = stack_up(i_curr), stack_up(i_prev)
            dn_now, dn_prev = stack_down(i_curr), stack_down(i_prev)

            if up_now and not up_prev:
                log.info("Stack ALCISTA COMPLETO formado en la vela cerrada " +
                         str(candle_ts) + " (3>4>10>21>27). Senal LONG.")
                return {'side': 'LONG', 'price': price, 'candle_ts': candle_ts,
                        'win_hi': win_hi, 'win_lo': win_lo, 'atr': atr}
            if dn_now and not dn_prev:
                log.info("Stack BAJISTA COMPLETO formado en la vela cerrada " +
                         str(candle_ts) + " (3<4<10<21<27). Senal SHORT.")
                return {'side': 'SHORT', 'price': price, 'candle_ts': candle_ts,
                        'win_hi': win_hi, 'win_lo': win_lo, 'atr': atr}

        log.info("Sin formacion de stack completo en las 2 cerradas. Vigilando.")
    except Exception as e:
        log.error("Error en evaluacion: " + str(e))
    return None

# ==================== CAPACIDAD ====================
def capacity_ok(symbol):
    try:
        _ctval, _ccy, _settle, _price, usd = _contract_meta(symbol)
        nocional = usd * AMOUNT
        if nocional > MAX_NOTIONAL_USD:
            log.error("GUARDIA: nocional $" + format(nocional, '.2f') +
                      " > presupuesto $" + format(MAX_NOTIONAL_USD, '.2f') + ". NO SE OPERA.")
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

    # Ax5 (panda): forzar one-way mode — CRÍTICO en HBAR por el flip.
    try:
        exchange.set_position_mode(False, symbol)
        log.info("Position mode → one-way para " + str(symbol) + ".")
    except ccxt.ExchangeError as e:
        log.debug("set_position_mode skip (probablemente ya one-way): " + str(e))
    except Exception as e:
        log.debug("set_position_mode skip: " + str(e))

    try:
        exchange.set_leverage(LEVERAGE, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.warning("No se pudo fijar apalancamiento (se usa el de OKX): " + str(e))

    sig = evaluate_signal(symbol)

    pos = get_open_position(symbol)
    flipped = False
    if pos:
        lado = str(pos.get('side'))
        contrario = (sig is not None and
                     ((lado == 'long' and sig['side'] == 'SHORT') or
                      (lado == 'short' and sig['side'] == 'LONG')))
        if not contrario:
            log.info("Posicion abierta (" + lado + "). Sin senal contraria. Esperando perro/cañonera.")
            return
        log.info("SENAL CONTRARIA (" + str(sig['side']) + ") — girando posicion " + lado + "...")
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

    if sig is None:
        return

    if not capacity_ok(symbol):
        log.info("Sin capacidad. Vigilando.")
        return

    if not flipped and cooldown_active(symbol):
        log.info("Vigilando (cooldown activo).")
        return

    if candle_already_traded(symbol, sig['candle_ts']):
        return

    sl_raw, tp_raw, motivo = perro_y_canonera(
        sig['side'], _f(sig['price']), sig['win_hi'], sig['win_lo'], sig['atr'], tick_size(symbol))
    if sl_raw is None:
        log.info("Candado rechaza la entrada: " + motivo)
        return

    log.info("Senal " + str(sig['side']) + " (stack completo 30m, 2 cerradas). Abriendo " +
             str(AMOUNT) + " contrato...")
    try:
        execute_order(sig['side'], symbol, sig['price'], AMOUNT, sig['candle_ts'],
                      sl_raw, tp_raw, motivo)
    except Exception as e:
        log.error("Entrada rechazada: " + str(e))

def run_once():
    log.info(">>> DESTRUCTOR v" + str(VERSION) +
             " | HBAR stack 3/4/10/21/27 30m | ventana: 2 cerradas + la que se forma | "
             "perro estructura [piso ATR, techo 2%] | TP RR1.5 (flota) | flip por senal | "
             "presupuesto $" + format(MAX_NOTIONAL_USD, '.2f') + " | XPERP USD.")
    verify_setup()
    if TEST_MODE:
        catalog_hbar()
        return
    run_cycle()

def main_loop():
    log.info("Bot " + BASE_ASSET + " DESTRUCTOR v" + str(VERSION) +
             " | 30m stack EMA " + str(EMAS) + " | 2 cerradas + formacion | "
             "perro estructura ATR/2% | TP RR1.5 | flip senal contraria | " +
             str(AMOUNT) + " ct | presupuesto $" + format(MAX_NOTIONAL_USD, '.2f') +
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
        time.sleep(1800)

if __name__ == "__main__":
    if SINGLE_CYCLE:
        run_once()
    else:
        main_loop()
