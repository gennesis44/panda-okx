#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# inj-main.py — Ax2-INJ · EL APRENDIZ · ley v3.4 (adaptado a producción)
#   - Instrumento: INJ X-Perp (Universo EEE/XPERP, OKX my.okx.com, margen USD)
#   - VETO ABSOLUTO a USDT (Lista blanca settle == 'USD')
#   - Estrategia: EMA 4 / EMA 21 en velas 1H CERRADAS
#   - Nocional estricto: ≤ 6 USD por orden (decreto Capitán — específico de INJ)
#   - SL/TP asimétricos (LONG 2/4, SHORT 3/4) con attachAlgoOrds adjuntos.
#   AJUSTE FLOTA: SIGNAL_WINDOW (-1,-2) = 2 velas cerradas + privacidad colateral.
#   NOTA TÉCNICA: "2 velas + forming" no aplica a INJ porque su SL es porcentual,
#     no estructural. Por eso no se incorpora la vela en formación al cálculo.

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

# ==================== CONFIGURACIÓN ====================
BASE_ASSET = 'INJ'
SL_LONG = 0.020     # 2.0%
TP_LONG = 0.040     # 4.0%
SL_SHORT = 0.030    # 3.0%
TP_SHORT = 0.040    # 4.0%
TIMEFRAME = '1h'
EMA_FAST = 4        # Solicitado: EMA 4
EMA_SLOW = 21       # Solicitado: EMA 21
TD_MODE = 'cross'
LEVERAGE = 1
COOLDOWN_MIN = 60
TEST_MODE = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE = os.getenv('SINGLE_CYCLE') == '1'
MAX_NOTIONAL_USD = _f(os.getenv('OKX_INJ_MAX_NOTIONAL', '6.00')) or 6.00   # decreto Capitán
INJ_DEBUG = os.getenv('INJ_DEBUG', '0') == '1'   # 1 = muestra colateral
SIGNAL_WINDOW = (-1, -2)   # 2 velas cerradas (SL porcentual: forming no aporta)
WARMUP = 40
TF_MS = {'5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000, '6h': 21600000}

HOST = 'https://my.okx.com'

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', '') or os.getenv('OKX_API_SECRET', ''),
    'password':  os.getenv('OKX_PASSWORD', '') or os.getenv('OKX_API_PASSPHRASE', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== INSTRUMENTO (XPERP USD — NUNCA USDT) ====================
def _is_forbidden(symbol):
    settle = str(exchange.market(symbol).get('settle') or '').upper()
    info = exchange.market(symbol).get('info') or {}
    inst_id = str(info.get('instId') or symbol).upper()
    if 'USDT' in inst_id or settle == 'USDT':
        return True
    return settle != 'USD'

def resolve_symbol():
    exchange.load_markets()
    try:
        for p in exchange.fetch_positions():
            if abs(float(p.get('contracts') or 0)) > 0 and (p.get('symbol') or '').startswith(BASE_ASSET + '/'):
                if _is_forbidden(p['symbol']):
                    log.error("VETO: posicion en instrumento NO-USD — prohibido.")
                    raise RuntimeError("Posicion abierta en instrumento NO-USD prohibido.")
                log.info("Instrumento (posicion abierta): " + str(p['symbol']))
                return p['symbol']
    except RuntimeError:
        raise
    except Exception as e:
        log.warning("Posiciones no leidas al resolver simbolo: " + str(e))

    # Búsqueda exclusiva de X-Perp / Swap USD sin USDT
    candidatos = []
    for m in exchange.markets.values():
        if m.get('base') == BASE_ASSET and m.get('active') and not _is_forbidden(m['symbol']):
            candidatos.append(m['symbol'])

    if not candidatos:
        raise RuntimeError("No hay INJ/USD (XPERP o SWAP-USD) activo. USDT prohibido por ley EEE.")

    sym = candidatos[0]
    log.info("Instrumento resuelto: " + str(sym))
    return sym

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

# ==================== INDICADORES ====================
def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def fetch_data(symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

# ==================== POSICIÓN ====================
def get_open_position(symbol):
    try:
        for pos in exchange.fetch_positions([symbol]):
            if pos.get('symbol') == symbol and abs(float(pos.get('contracts') or 0)) > 0:
                return pos
        inst = exchange.market(symbol)['id']
        for ord_type in ('oco', 'conditional'):
            resp = exchange.private_get_trade_orders_algo_pending(
                {'instId': inst, 'ordType': ord_type})
            if (resp or {}).get('data'):
                log.info("Sin posicion visible pero hay SL/TP vivos: asumo posicion.")
                return {'symbol': symbol, 'contracts': 1, 'side': 'desconocido'}
        return None
    except Exception as e:
        log.error("Posiciones ilegibles (" + str(e) + ") — BLOQUEO fail-closed.")
        return {'symbol': symbol, 'contracts': 1, 'side': 'ilegible'}

# ==================== ENTRADA Y CÁLCULO DE CONTRATOS ====================
def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def _cl_order_id(candle_ts):
    return "INJ" + str(int(candle_ts))

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
    except Exception:
        return False

def calculate_amount(symbol, ref_price):
    ctval, ccy, _settle, _p, usd = _contract_meta(symbol, ref_price)
    if usd <= 0:
        return 1
    # Cuántos contratos caben en el nocional máximo estricto (≤ 6 USD)
    amount = int(MAX_NOTIONAL_USD // usd)
    return max(1, amount)

def execute_order(side, symbol, ref_price, candle_ts):
    amount = calculate_amount(symbol, ref_price)
    ctval, ccy, _settle, _p, usd = _contract_meta(symbol, ref_price)
    nocional = amount * usd
    log.info("Nocional: " + str(amount) + " contrato x " + str(ctval) + " " + ccy +
             " (~$" + format(nocional, '.2f') + ")")

    if nocional > MAX_NOTIONAL_USD:
        raise RuntimeError("GUARDIA: nocional $" + format(nocional, '.2f') +
                           " > maximo $" + format(MAX_NOTIONAL_USD, '.2f') + " — STOP.")

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
    log.info("INJ " + side + " ejecutada con SL/TP adjuntos. ordId: " + str(d0.get('ordId')) +
             " | SL: " + str(sl) + " | TP: " + str(tp))
    return True

# ==================== COOLDOWN ====================
def cooldown_active(symbol):
    try:
        market_id = exchange.market(symbol)['id']
        hist = exchange.privateGetTradeFillsHistory({
            'instType': 'SWAP',
            'instId': market_id,
            'limit': '5',
        })
        for fill in (hist.get('data') or []):
            if str(fill.get('reduceOnly', '0')) == 'true' or fill.get('subType') in ('3', '4', '5', '6'):
                ts_ms = int(fill.get('ts') or 0)
                age_min = (time.time() * 1000 - ts_ms) / 60000.0
                pnl = _f(fill.get('fillPnl'))
                if pnl < 0 and age_min < COOLDOWN_MIN:
                    return True
                return False
        return False
    except Exception:
        return False

# ==================== SEÑAL: EMA 4 / 21 EN 1H + CANDADO ====================
def evaluate_signal(symbol):
    try:
        df = fetch_data(symbol, TIMEFRAME, limit=100)
        if len(df) < WARMUP + 10:
            log.error("1h insuficiente. Sin senal.")
            return None, None, None

        now_ms = int(time.time() * 1000)
        tf_ms = TF_MS[TIMEFRAME]
        if int(df['timestamp'].iloc[-1]) + tf_ms > now_ms:
            df = df.iloc[:-1].reset_index(drop=True)
            if len(df) < WARMUP + 10:
                return None, None, None

        e4 = ema(df['close'], EMA_FAST)
        e21 = ema(df['close'], EMA_SLOW)
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        log.info("Precio: " + str(price) + " | 1H EMA4/21: " +
                 format(e4.iloc[-2], '.5f') + "/" + format(e21.iloc[-2], '.5f') +
                 " | EMA4 " + (">" if e4.iloc[-2] > e21.iloc[-2] else "<") + " EMA21 (estado)")

        for i_curr in SIGNAL_WINDOW:
            i_prev = i_curr - 1
            idx = len(df) + i_curr
            if abs(i_prev) > len(df) - 1 or idx < WARMUP:
                continue
            candle_ts = int(df['timestamp'].iloc[i_curr])

            sep = abs(e4.iloc[idx] - e21.iloc[idx]) / e21.iloc[idx] if e21.iloc[idx] else 0.0
            if sep < 0.0015:
                continue # Candado v2

            up = (e4.iloc[i_prev] <= e21.iloc[i_prev] and e4.iloc[idx] > e21.iloc[idx])
            down = (e4.iloc[i_prev] >= e21.iloc[i_prev] and e4.iloc[idx] < e21.iloc[idx])

            if up:
                log.info("Cruce ALCISTA EMA 4/21 detectado (sep " +
                         format(sep * 100, '.2f') + "%). Senal LONG.")
                return 'LONG', price, candle_ts

            if down:
                log.info("Cruce BAJISTA EMA 4/21 detectado (sep " +
                         format(sep * 100, '.2f') + "%). Senal SHORT.")
                return 'SHORT', price, candle_ts

        log.info("Sin cruce EMA 4/21 legal en la ventana. Vigilando.")
    except Exception as e:
        log.error("Error en evaluacion: " + str(e))
    return None, None, None

# ==================== ARRANQUE ====================
def verify_setup():
    raw = exchange.privateGetAccountBalance()
    details = ((raw or {}).get('data') or [{}])[0].get('details') or []
    total = sum(_f(d.get('eqUsd')) for d in details)
    if INJ_DEBUG:
        log.info("Autenticacion OK | host=" + HOST + " | Colateral real (USD): ~" + format(total, '.2f'))
    else:
        log.info("Autenticacion OK | host=" + HOST + " | Colateral: OK (INJ_DEBUG=1 para verlo)")

# ==================== CICLO ====================
def run_cycle():
    symbol = resolve_symbol()
    try:
        exchange.set_position_mode(False, symbol)
    except Exception:
        pass
    try:
        exchange.set_leverage(LEVERAGE, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.error("No se pudo fijar apalancamiento: " + str(e))
        return

    pos = get_open_position(symbol)
    if pos:
        log.info("Posicion abierta. Esperando SL/TP.")
        return

    if cooldown_active(symbol):
        log.info("Cooldown activo.")
        return

    signal, price, candle_ts = evaluate_signal(symbol)
    if not signal:
        return

    if candle_already_traded(symbol, candle_ts):
        return

    amount = calculate_amount(symbol, price)
    log.info("Ejecutando senal " + signal + " para INJ (" + str(amount) + " ct)...")
    try:
        execute_order(signal, symbol, price, candle_ts)
    except Exception as e:
        log.error("Entrada rechazada: " + str(e))

def run_once():
    log.info("Modo ciclo unico | INJ EMA4/21 1H v3.4 + CANDADO v2 FAIL-CLOSED (XPERP USD) | AMOUNT dinamico | MAX $6")
    verify_setup()
    if TEST_MODE:
        log.info("TEST_MODE activo — solo catalogo.")
        return
    run_cycle()

def main_loop():
    log.info("Bot INJ | 1H EMA4/21 v3.4 | LONG SL2/TP4 · SHORT SL3/TP4 | " +
             "AMOUNT dinamico | ventana -1/-2 | cooldown " + str(COOLDOWN_MIN) + "m | " + HOST)
    verify_setup()
    while True:
        try:
            run_cycle()
        except Exception as e:
            log.error("Error en ciclo principal: " + str(e))
            time.sleep(60)
        time.sleep(1200)  # Cada 20 minutos exactos (modo VPS local)

if __name__ == "__main__":
    if SINGLE_CYCLE:
        run_once()
    else:
        main_loop()
