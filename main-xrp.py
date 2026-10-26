# main-xrp.py v3.0 — XRP DESTROYER · ARMADO Y A ZARPAR
#   DECRETO CARBONO (26-oct-2026): "Debe estar combatiendo ya. No hace nada en Puerto. Armado y a zarpar"
#   Ley original (shadow v2) convertida a combate real:
#     EMA 3/21 en velas 4H CERRADAS
#     LONG  SL 6.0% / TP 7.5%
#     SHORT SL 4.5% / TP 6.0%
#     4 contratos
#   Ax3: HOST my.okx.com | USDT PROHIBIDO | fail-closed | clOrdId por vela | cooldown | guardia nocional
#   Una posición máxima. Sin opinión. El mercado emite el veredicto.

import os
import time
import logging
import sys

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
BASE_ASSET = 'XRP'
AMOUNT = 4              # 4 contratos (geometría original del shadow)
SL_LONG = 0.060         # 6.0%
TP_LONG = 0.075         # 7.5%
SL_SHORT = 0.045        # 4.5%
TP_SHORT = 0.060        # 6.0%
TIMEFRAME = '4h'
EMA_FAST = 3
EMA_SLOW = 21
TD_MODE = 'cross'
LEVERAGE = 1
COOLDOWN_MIN = 60
TEST_MODE = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE = os.getenv('SINGLE_CYCLE') == '1'
MAX_NOTIONAL_USD = _f(os.getenv('OKX_XRP_MAX_NOTIONAL', '25.0')) or 25.0
SIGNAL_WINDOW = (-2, -3, -4)
WARMUP = 40
TF_MS = 14400000        # 4h

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
    return settle != 'USD'

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
                 " | nocional \~$" + format(usd, '.2f') + " | " + sym +
                 " | " + _inst_type(sym))
    except Exception as e:
        log.warning("No se pudo leer el tamaño del contrato: " + str(e))

def resolve_symbol():
    exchange.load_markets()
    try:
        for p in exchange.fetch_positions():
            if abs(float(p.get('contracts') or 0)) > 0 and (p.get('symbol') or '').startswith(BASE_ASSET + '/'):
                if _is_forbidden(p['symbol']):
                    log.error("VETO: posición en instrumento NO-USD — prohibido.")
                    raise RuntimeError("Posición abierta en instrumento NO-USD prohibido.")
                log.info("Instrumento (posición abierta): " + str(p['symbol']))
                _log_contract_size(p['symbol'])
                return p['symbol']
    except RuntimeError:
        raise
    except Exception as e:
        log.warning("Posiciones no leídas al resolver símbolo: " + str(e))
    sym = pick_future()
    if sym:
        log.info("Instrumento: " + str(sym) + " (XPERP — vencimiento más lejano)")
        _log_contract_size(sym)
        return sym
    sym = pick_swap_usd()
    if sym:
        log.warning("Instrumento: " + str(sym) + " (fallback SWAP-USD)")
        _log_contract_size(sym)
        return sym
    raise RuntimeError("No hay XRP/USD (XPERP o SWAP-USD) activo. USDT prohibido.")

# ==================== INDICADORES ====================
def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def fetch_data(symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

# ==================== POSICIÓN ====================
def get_open_position(symbol):
    """Fail-closed: mar ilegible = posición fantasma = no operar."""
    try:
        for pos in exchange.fetch_positions([symbol]):
            if pos.get('symbol') == symbol and abs(float(pos.get('contracts') or 0)) > 0:
                return pos
        # Plan B: SL/TP vivos = posición asumida
        inst = exchange.market(symbol)['id']
        for ord_type in ('oco', 'conditional'):
            resp = exchange.private_get_trade_orders_algo_pending(
                {'instId': inst, 'ordType': ord_type})
            if (resp or {}).get('data'):
                log.info("Sin posición visible pero hay SL/TP vivos: asumo posición.")
                return {'symbol': symbol, 'contracts': 1, 'side': 'desconocido'}
        return None
    except Exception as e:
        log.error("Posiciones ilegibles (" + str(e) + ") — BLOQUEO fail-closed.")
        return {'symbol': symbol, 'contracts': 1, 'side': 'ilegible'}

# ==================== ENTRADA CON SL/TP ASIMÉTRICOS ====================
def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def _cl_order_id(candle_ts):
    return "XRP" + str(int(candle_ts))

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
             " (\~$" + format(nocional, '.2f') + ")")

    if nocional > MAX_NOTIONAL_USD:
        raise RuntimeError("GUARDIA: nocional $" + format(nocional, '.2f') +
                           " > máximo $" + format(MAX_NOTIONAL_USD, '.2f') +
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
        raise ccxt.ExchangeError("OKX rechazó la entrada: " + str(d0.get('sMsg') or resp.get('msg')))
    log.info("XRP " + side + " ejecutada con SL/TP adjuntos. ordId: " +
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
                pnl = _f(fill.get('fillPnl') or fill.get('pnl'))
                if pnl < 0 and age_min < COOLDOWN_MIN:
                    log.info("COOLDOWN: última pérdida hace " + format(age_min, '.0f') +
                             " min (< " + str(COOLDOWN_MIN) + ").")
                    return True
                return False
        return False
    except Exception as e:
        log.warning("No se pudo verificar cooldown (" + str(e) + "); BLOQUEO fail-closed.")
        return True

# ==================== SEÑAL: CRUCE EMA3/21 4H CERRADA ====================
def evaluate_signal(symbol):
    try:
        df = fetch_data(symbol, TIMEFRAME, limit=100)
        if len(df) < WARMUP + 10:
            log.error("4H insuficiente (" + str(len(df)) + " velas). Sin señal.")
            return None, None, None

        now_ms = int(time.time() * 1000)
        if int(df['timestamp'].iloc[-1]) + TF_MS > now_ms:
            df = df.iloc[:-1].reset_index(drop=True)
            if len(df) < WARMUP + 10:
                log.error("4H insuficiente tras descartar vela en formación.")
                return None, None, None

        e_fast = ema(df['close'], EMA_FAST)
        e_slow = ema(df['close'], EMA_SLOW)
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        log.info("Precio: " + str(price) + " | 4H EMA3/21: " +
                 format(e_fast.iloc[-2], '.5f') + "/" + format(e_slow.iloc[-2], '.5f') +
                 " | EMA3 " + (">" if e_fast.iloc[-2] > e_slow.iloc[-2] else "<") + " EMA21")

        for i_curr in SIGNAL_WINDOW:
            i_prev = i_curr - 1
            idx = len(df) + i_curr
            if abs(i_prev) > len(df) - 1 or idx < WARMUP:
                continue
            candle_ts = int(df['timestamp'].iloc[i_curr])

            up = (e_fast.iloc[i_prev] <= e_slow.iloc[i_prev]
                  and e_fast.iloc[i_curr] > e_slow.iloc[i_curr])
            down = (e_fast.iloc[i_prev] >= e_slow.iloc[i_prev]
                    and e_fast.iloc[i_curr] < e_slow.iloc[i_curr])

            if up:
                log.info("Cruce ALCISTA EMA3/21 detectado. Señal LONG.")
                return 'LONG', price, candle_ts
            if down:
                log.info("Cruce BAJISTA EMA3/21 detectado. Señal SHORT.")
                return 'SHORT', price, candle_ts

        log.info("Sin cruce en ventana reciente.")
        return None, None, None
    except Exception as e:
        log.error("Error evaluando señal: " + str(e))
        return None, None, None

# ==================== CICLO PRINCIPAL ====================
def main_loop():
    log.info("=== XRP DESTROYER v3.0 | EMA3/21 4H | ARMADO ===")
    if TEST_MODE:
        log.info("TEST_MODE activo — solo lectura / sin órdenes reales")

    try:
        symbol = resolve_symbol()
    except Exception as e:
        log.error("No se pudo resolver instrumento: " + str(e))
        sys.exit(1)

    # Ajuste de apalancamiento (best-effort)
    try:
        exchange.set_leverage(LEVERAGE, symbol, params={'mgnMode': 'cross'})
    except Exception as e:
        log.warning("No se pudo fijar leverage: " + str(e))

    pos = get_open_position(symbol)
    if pos:
        log.info("Posición abierta detectada — no se abre nueva.")
        return

    if cooldown_active(symbol):
        log.info("Cooldown activo — no se opera.")
        return

    side, price, candle_ts = evaluate_signal(symbol)
    if side is None:
        log.info("Sin señal válida en este ciclo.")
        return

    if candle_already_traded(symbol, candle_ts):
        return

    if TEST_MODE:
        log.info("[TEST] Señal " + side + " detectada @ " + str(price) + " — no se envía orden.")
        return

    try:
        execute_order(side, symbol, price, AMOUNT, candle_ts)
    except Exception as e:
        log.error("Fallo al ejecutar orden: " + str(e))
        sys.exit(1)

if __name__ == "__main__":
    try:
        main_loop()
    except Exception as e:
        log.error("Fallo crítico: " + str(e))
        sys.exit(1)
