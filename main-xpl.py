
import os
import time
import math
import logging
import ccxt
import pandas as pd

HOST = 'https://my.okx.com'

# main-xpl.py — LA MAREA · casco XPL (reforgado 20-oct, linaje XLM v2)
# Doctrina de flota identica al main-xlm.py v2 (candado + guardia):
# - Ley: 1h EMA3/21, cruce fresco en la ventana [-2,-3,-4],
# CANDADO v2: separacion < 0.15% = veto (senal = ruido).
# - USDT prohibido (solo USD/USDC settle), XPERP de vencimiento
# lejano preferido.
# - Guardia de nocional (env OKX_XPL_MAX_NOTIONAL, default $20) +
# colateral suficiente; fail-closed en todo.
# - Cooldown 60 min tras perdida; idempotencia clOrdId "XPL"+vela.
# - SINGLE_CYCLE=1 cuando la catapulta lo lanza (ciclo unico).
# - SL/TP: decreto Carbono del linaje (LONG 3.5/5 · SHORT 2.5/4);
# ajustables aqui abajo si el Capitan manda otra cosa.

# ==================== VALORES (decreto Carbono) ====================
BASE_ASSET = 'XPL'
AMOUNT = 1 # ct — decidido por Carbono
SL_LONG = 0.035 # 3.5%
TP_LONG = 0.050 # 5.0%
SL_SHORT = 0.025 # 2.5%
TP_SHORT = 0.040 # 4.0%
TIMEFRAME = '1h'
EMA_FAST = 3
EMA_SLOW = 21
TD_MODE = 'cross'
LEVERAGE = 1
COOLDOWN_MIN = 60
TEST_MODE = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE = os.getenv('SINGLE_CYCLE') == '1'
# Guardia de nocional (mismo fix que el XLM v2: la guardia vieja de $15
# vetaba TODAS las entradas; se abre a 20 con mando por env).
try:
    MAX_NOTIONAL_USD = float(os.getenv('OKX_XPL_MAX_NOTIONAL', '20.0')) or 20.0
except ValueError:
    MAX_NOTIONAL_USD = 20.0
SIGNAL_WINDOW = (-2, -3, -4)
WARMUP = 65 # 21*3+2 — minimo de velas antes de senales

exchange = ccxt.okx({
    'apiKey': os.getenv('OKX_API_KEY', ''),
    'secret': os.getenv('OKX_SECRET_KEY', ''),
    'password': os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'urls': {'api': {'rest': HOST}},
})

# === INSTRUMENTO (veto USDT, XPERP vencimiento lejano) ===
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

def resolve_symbol():
    exchange.load_markets()
    try:
        for p in exchange.fetch_positions():
            if (p.get('contracts') or 0) > 0 and (p.get('symbol') or '').startswith(BASE_ASSET + '/'):
                if _is_forbidden(p['symbol']):
                    raise RuntimeError("Posicion abierta en USDT prohibido.")
                return p['symbol']
    except RuntimeError:
        raise
    except Exception:
        pass
    sym = pick_future()
    if sym:
        return sym
    sym = pick_swap_usd()
    if sym:
        return sym
    raise RuntimeError("No hay instrumento activo. USDT prohibido.")

# === POSICIÓN ===
def get_open_position(symbol):
    try:
        for pos in exchange.fetch_positions([symbol]):
            if pos.get('symbol') == symbol and float(pos.get('contracts') or 0) > 0:
                return pos
    except Exception:
        pass
    return None

# === GUARDIA DE NOCIONAL ===
def _contract_meta(symbol, price=None):
    market = exchange.market(symbol)
    info = market.get('info') or {}
    ctval = float(market.get('contractSize') or info.get('ctVal') or 1)
    ccy = str(info.get('ctValCcy') or BASE_ASSET).upper()
    settle = str(info.get('settleCcy') or market.get('settle') or '?').upper()
    if price is None:
        price = exchange.fetch_ticker(symbol).get('last') or 0
    price = float(price)
    usd = ctval if ccy in ('USD', 'USDT', 'USDC') else ctval * price
    return ctval, ccy, settle, price, usd

def capacity_ok(symbol, amount):
    try:
        _ctval, _ccy, _settle, _price, usd = _contract_meta(symbol)
        nocional = usd * amount
        if nocional > MAX_NOTIONAL_USD:
            return False
        need = nocional / max(LEVERAGE, 1)
    except Exception:
        return False
    try:
        raw = exchange.privateGetAccountBalance()
        details = ((raw or {}).get('data') or [{}])[0].get('details') or []
        total = sum(float(d.get('eqUsd') or 0) for d in details)
        return total >= need
    except Exception:
        return False

# === IDEMPOTENCIA (clOrdId por vela) ===
def _cl_order_id(candle_ts):
    return "XPL" + str(int(candle_ts))

def candle_already_traded(symbol, candle_ts):
    cl = _cl_order_id(candle_ts)
    try:
        inst = exchange.market(symbol)['id']
        resp = exchange.private_get_trade_order({'instId': inst, 'clOrdId': cl})
        data = resp.get('data') or []
        if data and str(data[0].get('sCode', '0')) == '0':
            return True
        return False
    except ccxt.ExchangeError as e:
        msg = str(e)
        if '51603' in msg or 'does not exist' in msg.lower():
            return False
        return True
    except Exception:
        return True

# === ENTRADA CON SL/TP ATÓMICOS ===
def execute_order(side, symbol, ref_price, amount, candle_ts):
    ctval, ccy, _settle, _p, usd = _contract_meta(symbol, ref_price)
    nocional = amount * usd
    if nocional > MAX_NOTIONAL_USD:
        raise RuntimeError("GUARDIA nocional")

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
            return True
        raise

    data = resp.get('data') or []
    d0 = data[0] if isinstance(data, list) and data else {}
    s_code = str(d0.get('sCode', resp.get('code', '1')))
    if s_code == '51016':
        return True
    if s_code != '0':
        raise ccxt.ExchangeError("OKX rechazo: " + str(d0.get('sMsg') or resp.get('msg')))
    return True

# === COOLDOWN ===
def cooldown_active(symbol):
    try:
        market = exchange.market(symbol)
        inst_type = str((market.get('info') or {}).get('instType') or 'FUTURES').upper()
        hist = exchange.private_get_trade_fills_history({
            'instType': inst_type,
            'instId': market['id'],
            'limit': '5',
        })
        for fill in (hist.get('data') or []):
            pnl = float(fill.get('fillPnl') or fill.get('pnl') or 0)
            ts_ms = int(fill.get('ts') or 0)
            age_min = (time.time() * 1000 - ts_ms) / 60000.0
            if pnl < 0 and age_min < COOLDOWN_MIN:
                return True
        return False
    except Exception:
        return True

# === SEÑAL ===
def evaluate_signal(symbol):
    df = fetch_data(symbol, TIMEFRAME, limit=100)
    if len(df) < WARMUP + 10:
        return None, None, None
    now_ms = int(time.time() * 1000)
    tf_ms = int(TIMEFRAME.replace('h','')) * 3600000 if 'h' in TIMEFRAME else 300000
    if int(df['timestamp'].iloc[-1]) + tf_ms > now_ms:
        df = df.iloc[:-1].reset_index(drop=True)

    e_f = ema(df['close'], EMA_FAST)
    e_s = ema(df['close'], EMA_SLOW)
    price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

    for i_curr in SIGNAL_WINDOW:
        i_prev = i_curr - 1
        idx = len(df) + i_curr
        if abs(i_prev) > len(df) - 1 or idx < WARMUP:
            continue
        candle_ts = int(df['timestamp'].iloc[i_curr])
        # CANDADO v2 FAIL-CLOSED — separacion EMA3/21 < 0.15% = veto
        sep = (abs(e_f.iloc[idx] - e_s.iloc[idx]) / e_s.iloc[idx]
               if e_s.iloc[idx] else 0.0)
        if sep < 0.0015:
            continue
        up = (e_f.iloc[i_prev] <= e_s.iloc[i_prev] and e_f.iloc[idx] > e_s.iloc[idx])
        down = (e_f.iloc[i_prev] >= e_s.iloc[i_prev] and e_f.iloc[idx] < e_s.iloc[idx])
        if up:
            return 'LONG', price, candle_ts
        if down:
            return 'SHORT', price, candle_ts
    return None, None, None

# ==================== FUNCIONES RESTANTES ====================
def fetch_data(symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def verify_setup():
    raw = exchange.privateGetAccountBalance()
    details = ((raw or {}).get('data') or [{}])[0].get('details') or []
    total = sum(float(d.get('eqUsd') or 0) for d in details)
    print("AUTH OK | colateral: ~$" + str(round(total, 2)))

def catalog():
    exchange.load_markets()
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET:
            continue
        info = m.get('info') or {}
        estado = 'activo' if m.get('active') else 'INACTIVO'
        print(" " + str(m['symbol']) + " | " + str(info.get('instType') or '?') +
              " | " + estado + " | settle=" + str(m.get('settle') or '?'))

# === CICLO ===
def run_cycle():
    symbol = resolve_symbol()
    # DIAG: bitacora de guardia — el silencio mata, esto canta siempre
    try:
        ctval, ccy, settle, price_d, usd = _contract_meta(symbol)
        nocional = AMOUNT * usd
        print("DIAG | " + symbol + " | 1 ct = " + str(ctval) + " " + ccy +
              " | precio ~$" + format(price_d, '.5f') +
              " | nocional " + str(AMOUNT) + " ct = $" + format(nocional, '.2f') +
              " / max $" + format(MAX_NOTIONAL_USD, '.2f') + " -> " +
              ("CABE" if nocional <= MAX_NOTIONAL_USD else "NO CABE (guardia)"), flush=True)
    except Exception as e:
        print("DIAG | contrato no legible: " + str(e), flush=True)
    try:
        exchange.set_leverage(LEVERAGE, symbol, params={'mgnMode': TD_MODE})
    except Exception:
        pass
    if get_open_position(symbol):
        print("DIAG | posicion abierta — esperando SL/TP", flush=True)
        return
    if not capacity_ok(symbol, AMOUNT):
        print("DIAG | GUARDIA: nocional > max o colateral insuficiente — NO SE OPERA", flush=True)
        return
    if cooldown_active(symbol):
        print("DIAG | cooldown activo — vigilando", flush=True)
        return
    signal, price, candle_ts = evaluate_signal(symbol)
    if not signal:
        print("DIAG | sin senal en la ventana", flush=True)
        return
    if candle_already_traded(symbol, candle_ts):
        print("DIAG | vela ya operada — duplicado bloqueado", flush=True)
        return
    try:
        execute_order(signal, symbol, price, AMOUNT, candle_ts)
    except Exception as e:
        print("DIAG | entrada rechazada: " + str(e), flush=True)

def run_once():
    verify_setup()
    if TEST_MODE:
        catalog()
        return
    run_cycle()

if __name__ == "__main__":
    run_once()
