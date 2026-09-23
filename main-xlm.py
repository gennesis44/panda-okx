# main-xlm.py — Ax2-XLM · EL PRIMER DESTRUCTOR · DIQUE SECO (MS-0 TOTAL)
# ⚰️ ESTADO: RETIRADO DEL MAR POR DECRETO MS-0 (23-sep-2026)
#   Motivo: CUATRO MARES CONDENADOS por backtest:
#     15m EMA7/21 → −22.2% neto (44 trades · WR 25%)
#     30m EMA3/10 → −3.5% neto  (27 trades · WR 37%)
#     4H  EMA3/15 → −3.1% neto  (16 trades · WR 38%)
#     LIVE        → 12% WR      · −0.79 real
#   🔒 XLM_HALTED = True: el bot NO OPERA — ni cron, ni dispatch.
#     Reapertura SOLO si el Carbono encarga un backtest NUEVO con
#     resultado positivo y cambia esta constante a False.
#   HONOR: primer destructor · trajo el candado RSI (padre del
#     anti-rango) · la ley de pausa previa · y la lección final:
#     EL FAVORITISMO NO CREA EDGE.
#   Este archivo existe como archivo de archivo — el barco en dique.
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

# ==================== ⚰️ MS-0 — EL CANDADO MAESTRO ====================
XLM_HALTED = True        # ← MS-0 TOTAL (23-sep-2026). True = JAMAS opera.
# Reapertura: solo con backtest nuevo POSITIVO + decreto Carbono
# que cambie esta constante a False.

BASE_ASSET = 'XLM'
AMOUNT = 10              # 10 ct = 100 XLM (~$9.40) — collar historico
SL_LONG = 0.025
TP_LONG = 0.040
SL_SHORT = 0.020
TP_SHORT = 0.035
TIMEFRAME = '4h'         # el ultimo marco probado (condenado)
EMA_FAST = 3
EMA_SLOW = 15
TD_MODE = 'cross'
LEVERAGE = 1
COOLDOWN_MIN = 60
TEST_MODE = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE = os.getenv('SINGLE_CYCLE') == '1'
MAX_NOTIONAL_USD = _f(os.getenv('OKX_XLM_MAX_NOTIONAL', '15.0')) or 15.0
SIGNAL_WINDOW = (-2, -3)
WARMUP_4H = 30
RANGO_UMBRAL_PCT = 0.15

HOST = 'https://my.okx.com'

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== ⚰️ EL DIQUE SECO ====================
def halted_check():
    """El candado maestro del MS-0. Si XLM_HALTED, el destructor
    no navega — solo reporta su estado de archivo."""
    if XLM_HALTED:
        log.warning("+" + "-" * 54 + "+")
        log.warning("| XLM EN DIQUE SECO — MS-0 TOTAL (23-sep-2026)      |")
        log.warning("| Cuatro mares condenados por backtest.           |")
        log.warning("| Reapertura: backtest positivo + decreto Carbono |")
        log.warning("+" + "-" * 54 + "+")
        return True
    return False

# ==================== INSTRUMENTO (XPERP USD — NUNCA USDT) ====================
def catalog_xlm():
    exchange.load_markets()
    print("[" + time.strftime('%H:%M:%S') + "] --- CATALOGO XLM ---", flush=True)
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
    raise RuntimeError("No hay XLM/USD (XPERP o SWAP-USD) activo. USDT prohibido.")

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
            if pos.get('symbol') == symbol and float(pos.get('contracts') or 0) > 0:
                return pos
    except Exception as e:
        log.error("Error consultando posiciones: " + str(e))
    return None

# ==================== ENTRADA (BLOQUEADA POR MS-0) ====================
def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def _cl_order_id(candle_ts):
    return "XLM" + str(int(candle_ts))

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
    except Exception as e:
        log.warning("No se pudo verificar duplicado (" + str(e) + "); BLOQUEO fail-closed.")
        return True

def execute_order(side, symbol, ref_price, amount, candle_ts):
    """BLOQUEADA POR MS-0 — existe para documentacion, no para operar."""
    raise RuntimeError("MS-0: XLM en dique seco. Operacion BLOQUEADA "
                       "hasta reapertura por decreto Carbono.")

# ==================== COOLDOWN ====================
def cooldown_active(symbol):
    return False   # irrelevante en dique seco

# ==================== 🔒 CANDADO ANTI-RANGO v2 ====================
def rango_activo(efast, eslow, idx_pos):
    try:
        ef = float(efast.iloc[idx_pos])
        es = float(eslow.iloc[idx_pos])
        if es <= 0:
            return True, 0.0
        sep = abs(ef - es) / es * 100.0
        return sep < RANGO_UMBRAL_PCT, sep
    except Exception:
        return True, 0.0    # fail-closed

# ==================== SEÑAL (INACTIVA — MS-0) ====================
def evaluate_signal(symbol):
    """Inactiva por MS-0. La ley 4H EMA3/15 existe como documentacion
    del ultimo territorio probado (condenado -3.1%)."""
    log.info("Senal INACTIVA: XLM en dique seco (MS-0).")
    return None, None, None

# ==================== CAPACIDAD ====================
def capacity_ok(symbol):
    return False   # irrelevante en dique seco

# ==================== ARRANQUE ====================
def verify_setup():
    raw = exchange.privateGetAccountBalance()
    details = ((raw or {}).get('data') or [{}])[0].get('details') or []
    total = sum(_f(d.get('eqUsd')) for d in details)
    log.info("Autenticacion OK | host=" + HOST + " | Colateral real (USD): ~" + format(total, '.2f'))

# ==================== CICLO (EN DIQUE SECO) ====================
def run_cycle():
    symbol = resolve_symbol()

    pos = get_open_position(symbol)
    if pos:
        log.warning("ATENCION: posicion XLM abierta (" + str(pos['side']) +
                    ") en dique seco — intervencion manual requerida.")
        return

    log.info("XLM en dique seco (MS-0). Sin operaciones. Solo custodia.")
    return

def run_once():
    log.info("MODO DIQUE SECO | XLM MS-0 TOTAL | Sin operaciones hasta reapertura.")
    verify_setup()
    if TEST_MODE:
        catalog_xlm()
        return
    run_cycle()

def main_loop():
    log.info("Bot " + BASE_ASSET + " EN DIQUE SECO | MS-0 TOTAL | Sin operaciones.")
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
        time.sleep(3600)

if __name__ == "__main__":
    if SINGLE_CYCLE:
        run_once()
    else:
        main_loop()
