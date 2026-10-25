# main-doge.py — Ax2-DOGE · EL PERRO · ley v3.3 (25-oct: MISIÓN DUAL + Ley Carbono lista blanca)
#   v3.3 (decreto del Capitán: "doge busca su bolsa de huesos y AL MISMO TIEMPO observa
#     el mar de INJ y SUI — para que Atalaya tenga un ayudante"):
#     C4 VIGÍA AUXILIAR: bitácora INJ/SUI (1h EMA3/21, ventana -2/-3/-4) tras cada ciclo.
#       SOLO LECTURA — cero munición (no toca órdenes ni posiciones), fail-safe por nave:
#       si el radar falla, la caza ni se entera. La munición la deciden Atalaya y las
#       destructoras; el Perro solo deja inteligencia escrita en el log.
#     C2 LEY CARBONO LISTA BLANCA: _is_forbidden = settle != 'USD'. En v3.2 vetaba solo
#       USDT (lista negra) — los inversores coin-margined (DOGE/USD:DOGE, settle=DOGE)
#       y los swaps EUR/TRY/USDC (settle vacío) PASABAN el filtro: la misma mina E1
#       que se encontró en Atalaya v4.2 y se desactivó en Atalaya v4.3.
#     C3 PATRÓN CERTIFICADO DE FLOTA: abs() sobre contracts en resolve_symbol y
#       get_open_position — un corto (-n) sin abs() es invisible (lección Atalaya P3;
#       hoy latente por el parseo string_abs de ccxt 4.5.90, blindado para siempre).
#   LEY: cruce EMA3/21 en velas 5m CERRADAS (ventana -2/-8 — A565G)
#   v3.1 ENMIENDA (decreto Carbono 12-oct): ventana -2/-3/-4 → -2/-8
#     MOTIVO: dos trenes perdidos (ruptura 11-oct, recruce 12-oct) —
#     cron real de GitHub ~20-40 min vs ventana vieja ~20 min.
#     El backtest v2 validó la LEY, no la ventana: los cruces capturados
#     tarde entran con SL/TP recalculados desde precio real — riesgo intacto.
#   LONG  (cruce alcista): SL 2.0% / TP 4.0%   (R:R 2.0 · breakeven ~33%)
#   SHORT (cruce bajista): SL 3.0% / TP 4.0%   (R:R 1.33 · breakeven ~43%)
#   CANDADO v2 FAIL-CLOSED: separación EMA3/21 < 0.15% = veto (senial=ruido)
#   Reimplantado 10-oct cumpliendo la lección documentada: .iloc, índice
#     positivo, fail-closed.
#   Validación: triple (8-oct original + 10-oct v2 IS/OOS + 10-oct v2 71d)
#     — 5m/3/21/0.15: ret +60.5% · PF 2.36 · DD 6.1% · la mejor de la flota
#   PRIMERA CAZA LIVE (v1): SL −$0.1594. Registro: perro neto +0.22 tras TP v2.
# Ax3: HOST my.okx.com | clOrdId DOGE | cooldown fail-closed | no entrar si
#      NO CABE | 51016 idempotencia | guardia de nocional — unidad sorpresa
#      = bot bloqueado | USDT PROHIBIDO (MiCA/EEE)
# Ax4 (panda 🐼): set_position_mode(False, symbol) añadido para blindaje
#      defensivo anti-hedge. Si la cuenta está en one-way (normal EEE),
#      es no-op benigna. Si algún día OKX pasa a hedge, previene SL/TP
#      adheridos al lado equivocado.
# Ax5 (18-oct): FIX COOLDOWN — fill.get('pnl') no existe en OKX
#      fills-history; el campo real es fillPnl. Sin el fix, pnl siempre
#      era 0.0 y el cooldown post-pérdida NUNCA disparaba. Restaurado.
# Ax6 (24-oct): BLINDAJE v3.2 — get_open_position FAIL-CLOSED (mar ilegible
#      = posición fantasma = no se opera; SL/TP vivos = posición asumida,
#      mismo patrón que xpl v4.2) y set_leverage fallido = BLOQUEO (nunca
#      zarpar con apalancamiento ajeno heredado de la cuenta).
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
BASE_ASSET = 'DOGE'
AMOUNT = 10         # 10 ct = 100 DOGE (~$8.5-8.8) — collar ratificado
SL_LONG = 0.020     # 2.0%
TP_LONG = 0.040     # 4.0%
SL_SHORT = 0.030    # 3.0%
TP_SHORT = 0.040    # 4.0%
TIMEFRAME = '1h'   # v3.4 (decreto 25-oct): 5m -> 1h — cazador suelto con candado
EMA_FAST = 3
EMA_SLOW = 21
TD_MODE = 'cross'
LEVERAGE = 1
COOLDOWN_MIN = 60
TEST_MODE = os.getenv('TEST_MODE') == '0'
SINGLE_CYCLE = os.getenv('SINGLE_CYCLE') == '1'
MAX_NOTIONAL_USD = _f(os.getenv('OKX_DOGE_MAX_NOTIONAL', '15.0')) or 15.0
SIGNAL_WINDOW = (-2, -3, -4)   # v3.4: ley 1h — 3 velas bastan con cron */10
WARMUP_5M = 40

# C4 (v3.3 -> v3.4): misión dual CUMPLIDA — INJ y SUI tienen timón propio
# (main-inj.yml / main-sui.yml, 25-oct). El Perro vuelve a caza pura.
VIGIA_SHIPS = {}                     # sin naves asignadas: el radar vigía queda mudo
VIGIA_WINDOW = (-2, -3, -4)          # mismo estándar de flota que Atalaya
VIGIA_LOCK = 0.0015                  # mismo umbral de apertura (0.15%)
TF_MS = {'5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000, '6h': 21600000}

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
def catalog_doge():
    exchange.load_markets()
    print("[" + time.strftime('%H:%M:%S') + "] --- CATALOGO DOGE ---", flush=True)
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
    # C2 (v3.3): Ley Carbono LISTA BLANCA — solo settle USD navega.
    # v3.2 vetaba solo USDT: coin-margined (:DOGE, settle=DOGE) y swaps
    # EUR/TRY/USDC (settle vacío) pasaban el filtro — mina E1 latente.
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
                 " | nocional ~$" + format(usd, '.2f') + " | " + sym +
                 " | " + _inst_type(sym))
    except Exception as e:
        log.warning("No se pudo leer el tamano del contrato: " + str(e))

def resolve_symbol():
    exchange.load_markets()
    try:
        for p in exchange.fetch_positions():
            # C3 (v3.3): abs() — un corto (-n) sin abs() es invisible
            if abs(float(p.get('contracts') or 0)) > 0 and (p.get('symbol') or '').startswith(BASE_ASSET + '/'):
                if _is_forbidden(p['symbol']):
                    log.error("VETO: posicion en instrumento NO-USD — prohibido.")
                    raise RuntimeError("Posicion abierta en instrumento NO-USD prohibido.")
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
    raise RuntimeError("No hay DOGE/USD (XPERP o SWAP-USD) activo. USDT prohibido.")

# ==================== INDICADORES ====================
def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def fetch_data(symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

# ==================== POSICIÓN ====================
def get_open_position(symbol):
    """Ax6 fail-closed: mar ilegible = posición fantasma = no operar.
    Plan B anti-doble: SL/TP vivos en el instrumento = posición asumida."""
    try:
        for pos in exchange.fetch_positions([symbol]):
            # C3 (v3.3): abs() — patrón certificado de flota
            if pos.get('symbol') == symbol and abs(float(pos.get('contracts') or 0)) > 0:
                return pos
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
    return "DOGE" + str(int(candle_ts))

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
                           " — unidad inesperada. NO SE OPERA. (ctVal=" +
                           str(ctval) + " " + ccy + ")")

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
    log.info("DOGE " + side + " ejecutada con SL/TP adjuntos. ordId: " +
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
                pnl = _f(fill.get('fillPnl'))   # Ax5: era fill.get('pnl') — nunca existia
                if pnl < 0 and age_min < COOLDOWN_MIN:
                    log.info("COOLDOWN: ultima perdida hace " + format(age_min, '.0f') +
                             " min (< " + str(COOLDOWN_MIN) + ").")
                    return True
                return False
        return False
    except Exception as e:
        log.warning("No se pudo verificar cooldown (" + str(e) + "); BLOQUEO fail-closed.")
        return True

# ==================== SEÑAL: CRUCE EMA3/21 5m + CANDADO v2 ====================
def evaluate_signal(symbol):
    """[El Perro — ley v3.1]
    SENAL UNICA: cruce EMA3/EMA21 en velas 5m CERRADAS.
    Ventana -2/-8 (v3.1): cubre cron real de GitHub (~40 min).
    Candado v2: separacion >= 0.15% en la vela del cruce — fail-closed.
    LONG  (alcista): SL 2% / TP 4%
    SHORT (bajista): SL 3% / TP 4%"""
    try:
        df = fetch_data(symbol, TIMEFRAME, limit=100)
        if len(df) < WARMUP_5M + 10:
            log.error("5m insuficiente (" + str(len(df)) + " velas). Sin senal.")
            return None, None, None

        now_ms = int(time.time() * 1000)
        if int(df['timestamp'].iloc[-1]) + 3600000 > now_ms:   # v3.4: 1h (era 300000 — mina de repintado)
            df = df.iloc[:-1].reset_index(drop=True)
            if len(df) < WARMUP_5M + 10:
                log.error("5m insuficiente tras descartar vela en formacion.")
                return None, None, None

        e3 = ema(df['close'], EMA_FAST)
        e21 = ema(df['close'], EMA_SLOW)
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        log.info("Precio: " + str(price) + " | 5m EMA3/21: " +
                 format(e3.iloc[-2], '.5f') + "/" + format(e21.iloc[-2], '.5f') +
                 " | EMA3 " + (">" if e3.iloc[-2] > e21.iloc[-2] else "<") + " EMA21 (estado)")

        for i_curr in SIGNAL_WINDOW:
            i_prev = i_curr - 1
            idx = len(df) + i_curr          # indice POSITIVO real
            if abs(i_prev) > len(df) - 1 or idx < WARMUP_5M:
                continue
            candle_ts = int(df['timestamp'].iloc[i_curr])

            sep = abs(e3.iloc[idx] - e21.iloc[idx]) / e21.iloc[idx] if e21.iloc[idx] else 0.0
            if sep < 0.0015:
                continue                     # candado v2: sin separacion, no es senal

            up = (e3.iloc[i_prev] <= e21.iloc[i_prev]
                  and e3.iloc[idx] > e21.iloc[idx])
            down = (e3.iloc[i_prev] >= e21.iloc[i_prev]
                    and e3.iloc[idx] < e21.iloc[idx])

            if up:
                log.info("Cruce ALCISTA EMA3/21 detectado (sep " +
                         format(sep * 100, '.2f') + "%). Senal LONG.")
                return 'LONG', price, candle_ts

            if down:
                log.info("Cruce BAJISTA EMA3/21 detectado (sep " +
                         format(sep * 100, '.2f') + "%). Senal SHORT.")
                return 'SHORT', price, candle_ts

        log.info("Sin cruce EMA3/21 legal en la ventana. Vigilando.")
    except Exception as e:
        log.error("Error en evaluacion: " + str(e))
    return None, None, None

# ==================== C4 (v3.3): VIGÍA AUXILIAR — INJ/SUI SOLO BITÁCORA ====================
def vigia_symbol(base):
    """Instrumento del mar vigiado: futuro XPERP settle USD con vencimiento mas
    lejano (mismo criterio de pick_future, parametrizado por base)."""
    exchange.load_markets()
    best = None
    best_exp = -1
    for m in exchange.markets.values():
        if (m.get('base') == base and m.get('future') and m.get('active')
                and str(m.get('settle') or '').upper() == 'USD'):
            info = m.get('info') or {}
            try:
                exp = int(info.get('expTime') or 0)
            except (TypeError, ValueError):
                exp = 0
            if exp > best_exp:
                best = m['symbol']
                best_exp = exp
    return best

def vigia_observa():
    """C4 (v3.3): misión dual del Perro — vigía auxiliar de INJ/SUI para Atalaya.
    SOLO BITÁCORA: lectura pública de velas + EMAs. CERO munición (no llama a
    execute_order, no toca posiciones, no cambia estado). Fail-safe por nave:
    si el radar falla, la caza ni se entera. Inteligencia para el registro de flota."""
    log.info("[VIGIA] El Perro observa el mar de INJ y SUI (bitacora — sin municion).")
    for name, cfg in VIGIA_SHIPS.items():
        try:
            sym = vigia_symbol(name)
            if not sym:
                log.warning("[VIGIA " + name + "] sin futuro USD activo. Vigilando otro dia.")
                continue
            df = fetch_data(sym, cfg['tf'], limit=100)
            now_ms = int(time.time() * 1000)
            tf_ms = TF_MS.get(cfg['tf'], 3600000)
            if int(df['timestamp'].iloc[-1]) + tf_ms > now_ms:
                df = df.iloc[:-1].reset_index(drop=True)   # vela en formacion fuera
            if len(df) < 30:
                log.warning("[VIGIA " + name + "] velas insuficientes (" + str(len(df)) + ").")
                continue
            ef = ema(df['close'], cfg['fast'])
            es = ema(df['close'], cfg['slow'])
            sep_now = abs(ef.iloc[-2] - es.iloc[-2]) / es.iloc[-2] if es.iloc[-2] else 0.0
            estado = 'ALZA' if ef.iloc[-2] > es.iloc[-2] else 'BAJA'
            fresco = 'NINGUNO'
            for i_curr in VIGIA_WINDOW:
                i_prev = i_curr - 1
                idx = len(df) + i_curr                     # indice positivo real
                if abs(i_prev) > len(df) - 1 or idx < 20:
                    continue
                sep_c = abs(ef.iloc[idx] - es.iloc[idx]) / es.iloc[idx] if es.iloc[idx] else 0.0
                if sep_c < VIGIA_LOCK:
                    continue                               # apertura sin fuerza = ruido
                up = (ef.iloc[i_prev] <= es.iloc[i_prev] and ef.iloc[idx] > es.iloc[idx])
                down = (ef.iloc[i_prev] >= es.iloc[i_prev] and ef.iloc[idx] < es.iloc[idx])
                if up:
                    fresco = 'ALZA (sep ' + format(sep_c * 100, '.2f') + '%, vela ' + str(i_curr) + ')'
                    break                                  # cruce fresco mas reciente
                if down:
                    fresco = 'BAJA (sep ' + format(sep_c * 100, '.2f') + '%, vela ' + str(i_curr) + ')'
                    break
            log.info("[VIGIA " + name + "] " + sym + " " + cfg['tf'] +
                     " | EMA" + str(cfg['fast']) + "/" + str(cfg['slow']) + ": " +
                     format(ef.iloc[-2], '.5f') + "/" + format(es.iloc[-2], '.5f') +
                     " | sep " + format(sep_now * 100, '.2f') + "% | estado " + estado +
                     " | cruce fresco: " + fresco)
        except Exception as e:
            log.warning("[VIGIA " + name + "] fallo de observacion: " + str(e))

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
        log.info("Margen (10 ct): ~$" + format(need, '.2f') + " | colateral: ~$" +
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

    # Ax4 (panda 🐼): forzar one-way mode — blindaje defensivo anti-hedge.
    # Si la cuenta ya está en one-way (normal en EEE retail), es no-op benigna.
    # Si algún día OKX migra a hedge, previene SL/TP adheridos al lado equivocado.
    try:
        exchange.set_position_mode(False, symbol)
        log.info("Position mode → one-way para " + str(symbol) + ".")
    except ccxt.ExchangeError as e:
        log.debug("set_position_mode skip (probablemente ya one-way): " + str(e))
    except Exception as e:
        log.debug("set_position_mode skip: " + str(e))

    # Ax6: apalancamiento fallido = no se zarpa con el ajeno heredado.
    try:
        exchange.set_leverage(LEVERAGE, symbol, params={'mgnMode': TD_MODE})
    except Exception as e:
        log.error("No se pudo fijar apalancamiento (" + str(e) + ") — BLOQUEO fail-closed.")
        return

    pos = get_open_position(symbol)
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

    log.info("Senal " + signal + " (cruce EMA3/21 5m). Abriendo " +
             str(AMOUNT) + " contrato...")
    try:
        execute_order(signal, symbol, price, AMOUNT, candle_ts)
    except Exception as e:
        log.error("Entrada rechazada: " + str(e))

def run_once():
    log.info("Modo ciclo unico | DOGE EMA3/21 5m v3.3 + CANDADO v2 FAIL-CLOSED + VIGIA INJ/SUI (XPERP USD).")
    verify_setup()
    if TEST_MODE:
        catalog_doge()
    else:
        run_cycle()
    # C4 (v3.3): la misión dual corre SIEMPRE — cazador y vigía en el mismo ciclo.
    vigia_observa()

def main_loop():
    log.info("Bot " + BASE_ASSET + " | 5m EMA3/21 v3.3 | LONG SL2/TP4 · SHORT SL3/TP4 | " +
             str(AMOUNT) + " ct | ventana -2/-8 | cooldown " + str(COOLDOWN_MIN) + "m | " + HOST)
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
        try:
            vigia_observa()   # C4: bitacora INJ/SUI en cada vuelta — corre aunque la caza falle
        except Exception as e:
            log.warning("Vigia fallo: " + str(e))
        time.sleep(300)   # 5 min — cada vela nueva es un chequeo

if __name__ == "__main__":
    if SINGLE_CYCLE:
        run_once()
    else:
        main_loop()
