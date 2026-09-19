# main-xlm.py — Ax2-D · 15m detonante + 4H brujula (cruce fresco EVENTO) + cooldown · SL 1% / TP 1.5% · 1x · 1 contrato
# MERGE Ax2-D-FINAL: logica Ax2-D ratificada + sintaxis limpia. Archivo unico autoritativo.
#   - ESTRATEGIA: Ax2-D EVENTO — brujula = cruce FRESCO EMA7/21 en 4H (velas -2/-3 CERRADAS).
#     La variante "gate estado persistente" fue DESCARTADA por el analisis ratificado:
#     entrar por estado = entrada tardia = degrada el ratio SL1/TP1.5.
#   - SINTAXIS: sin escapes invalidos — compila limpio (validar con: python -m py_compile main-xlm.py).
#   - ELIMINADO: close_position() (codigo muerto; el cierre es exclusivamente SL/TP adjuntos).
# Ax3: HOST my.okx.com | clOrdId XLM | cooldown fail-closed | no entrar si NO CABE | 51016
# Ax3.1: unidades honestas (ctValCcy) | warmup 4H 120 velas | guardia velas | posicion primero
# Ax3.2: guardia de nocional (MAX_NOTIONAL_USD) — unidad de contrato sorpresa = bot bloqueado
# Ax3.3: RSI-BANDA [30-70] (solo XLM — decreto Carbono; NO afecta a ADA/DOGE/FET/SUI):
#        RSI(14) 15m fuera de [30-70] = entrada VETADA, igual para LONG y SHORT.
# INSTRUMENTO: XPERP XLM/USD (vencimiento) — USDT PROHIBIDO (colateral no-USDT, MiCA/EEE)
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
BASE_ASSET = 'XLM'
AMOUNT = 1            # 1 contrato = 100 XLM (~$18.2)
SL_PCT = 0.010        # 1.0%
TP_PCT = 0.015        # 1.5%
TIMEFRAME = '15m'     # detonante
TF_FILTER = '4h'      # brujula (cruce fresco EVENTO)
TD_MODE = 'cross'
LEVERAGE = 1
COOLDOWN_MIN = 60
TEST_MODE = os.getenv('TEST_MODE') == '1'
SINGLE_CYCLE = os.getenv('SINGLE_CYCLE') == '1'
MAX_NOTIONAL_USD = _f(os.getenv('OKX_XLM_MAX_NOTIONAL', '25.0')) or 25.0
# Ax3.3 RSI-BANDA (decreto Carbono [30-70])
RSI_LEN = 14
RSI_HI = 70.0         # RSI > 70 -> VETADO (LONG y SHORT)
RSI_LO = 30.0         # RSI < 30 -> VETADO (LONG y SHORT)

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
def catalog_xlm():
    exchange.load_markets()
    print(f"[{time.strftime('%H:%M:%S')}] --- CATALOGO XLM ---", flush=True)
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
                              f"instrumento prohibido. Intervencion manual requerida.")
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
    raise RuntimeError("No hay XLM/USD (XPERP o SWAP-USD) activo. USDT prohibido en esta cuenta.")

# ==================== INDICADORES ====================
def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()

def rsi(s: pd.Series, length: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/length, adjust=False).mean()
    rs = gain / loss.replace(0, float('nan'))
    return 100 - (100 / (1 + rs))

def fetch_data(symbol, timeframe, limit=60):
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

# ==================== ENTRADA CON SL/TP ADJUNTOS ====================
def _post_trade_order(req):
    method = getattr(exchange, 'privatePostTradeOrder', None) or exchange.private_post_trade_order
    return method(req)

def _cl_order_id(candle_ts: int) -> str:
    return f"XLM{int(candle_ts)}"

def candle_already_traded(symbol, candle_ts: int) -> bool:
    cl = _cl_order_id(candle_ts)
    try:
        inst = exchange.market(symbol)['id']
        resp = exchange.private_get_trade_order({'instId': inst, 'clOrdId': cl})
        data = resp.get('data') or []
        if data and str(data[0].get('sCode', '0')) == '0':
            log.info(f"Vela ya operada (clOrdId {cl}). Entrada duplicada bloqueada.")
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

def execute_order(side: str, symbol: str, ref_price: float, amount: int, candle_ts: int):
    ctval, ccy, _settle, _p, usd = _contract_meta(symbol, ref_price)
    nocional = amount * usd
    log.info(f"Nocional: {amount} contrato x {ctval} {ccy} (~${nocional:.2f})")

    if nocional > MAX_NOTIONAL_USD:
        raise RuntimeError(
            f"GUARDIA: nocional ${nocional:.2f} > maximo ${MAX_NOTIONAL_USD:.2f} "
            f"(ctVal={ctval} {ccy}). Unidad inesperada — NO SE OPERA.")

    if side == 'LONG':
        oside, sl_raw, tp_raw = 'buy', ref_price * (1 - SL_PCT), ref_price * (1 + TP_PCT)
    else:
        oside, sl_raw, tp_raw = 'sell', ref_price * (1 + SL_PCT), ref_price * (1 - TP_PCT)

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
    log.info(f"{side} ejecutada con SL/TP adjuntos. ordId: {d0.get('ordId')} | "
             f"SL: {sl} | TP: {tp}")
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

# ==================== SEÑAL: CRUCE 15m + CRUCE FRESCO 4H + RSI-BANDA ==============
def evaluate_signal(symbol):
    """[Ax2-D ratificado] senal = EVENTO.
    Detonante: cruce EMA7/21 en velas 15m CERRADAS (ventana -2/-3).
    Brujula: cruce FRESCO EMA7/21 en 4H (tambien -2/-3) + direccion alineada.
    NO se entra por 'estado persistente' 4H: entrada tardia degrada SL1/TP1.5.
    Ax3.3 RSI-BANDA: RSI(14) fuera de [30-70] = VETO (LONG y SHORT)."""
    try:
        # ---------- 4H: cruce fresco (evento) ----------
        df_4h = fetch_data(symbol, TF_FILTER, limit=120)
        if len(df_4h) < 25:
            log.error(f"4H insuficiente ({len(df_4h)} velas). Sin senal.")
            return None, None, None

        e7h = ema(df_4h['close'], 7)
        e21h = ema(df_4h['close'], 21)

        cross_4h = None  # 'LONG' | 'SHORT' | None
        for i_curr in (-2, -3):
            i_prev = i_curr - 1
            if e7h.iloc[i_prev] <= e21h.iloc[i_prev] and e7h.iloc[i_curr] > e21h.iloc[i_curr]:
                cross_4h = 'LONG'
                break
            if e7h.iloc[i_prev] >= e21h.iloc[i_prev] and e7h.iloc[i_curr] < e21h.iloc[i_curr]:
                cross_4h = 'SHORT'
                break

        if cross_4h is None:
            log.info("Sin cruce fresco EMA7/21 en 4H. Vigilando.")
            return None, None, None

        # ---------- 15m: cruce fresco + alineacion con 4H + RSI ----------
        df = fetch_data(symbol, TIMEFRAME, limit=60)
        if len(df) < 25:
            log.error(f"{TIMEFRAME} insuficiente ({len(df)} velas). Sin senal.")
            return None, None, None

        df['EMA_7'] = ema(df['close'], 7)
        df['EMA_21'] = ema(df['close'], 21)
        df['RSI_14'] = rsi(df['close'], RSI_LEN)
        price = exchange.fetch_ticker(symbol).get('last') or df['close'].iloc[-1]

        e7, e21, r14 = df['EMA_7'], df['EMA_21'], df['RSI_14']
        rsi_now = r14.iloc[-2]

        log.info(f"Precio: {price} | {TIMEFRAME} EMA7/21: {e7.iloc[-2]:.5f}/{e21.iloc[-2]:.5f} | "
                 f"RSI: {rsi_now:.1f} | 4H cruce fresco: {cross_4h}")

        for i_curr in (-2, -3):
            i_prev = i_curr - 1
            candle_ts = int(df['timestamp'].iloc[i_curr])

            # Cruce alcista 15m
            if e7.iloc[i_prev] <= e21.iloc[i_prev] and e7.iloc[i_curr] > e21.iloc[i_curr]:
                if cross_4h != 'LONG':
                    log.info("Cruce alcista 15m VETADO: 4H no tiene cruce LONG fresco.")
                    continue
                if not (RSI_LO < rsi_now < RSI_HI):
                    log.info(f"Cruce alcista VETADO: RSI {rsi_now:.1f} fuera de "
                             f"[{RSI_LO:.0f}-{RSI_HI:.0f}]. [Ax3.3]")
                    continue
                return 'LONG', price, candle_ts

            # Cruce bajista 15m
            if e7.iloc[i_prev] >= e21.iloc[i_prev] and e7.iloc[i_curr] < e21.iloc[i_curr]:
                if cross_4h != 'SHORT':
                    log.info("Cruce bajista 15m VETADO: 4H no tiene cruce SHORT fresco.")
                    continue
                if not (RSI_LO < rsi_now < RSI_HI):
                    log.info(f"Cruce bajista VETADO: RSI {rsi_now:.1f} fuera de "
                             f"[{RSI_LO:.0f}-{RSI_HI:.0f}]. [Ax3.3]")
                    continue
                return 'SHORT', price, candle_ts

    except Exception as e:
        log.error(f"Error en evaluacion: {e}")
    return None, None, None

# ==================== CAPACIDAD ====================
def capacity_ok(symbol):
    try:
        _ctval, _ccy, _settle, _price, usd = _contract_meta(symbol)
        nocional = usd * AMOUNT
        if nocional > MAX_NOTIONAL_USD:
            log.error(f"GUARDIA: nocional ${nocional:.2f} > maximo ${MAX_NOTIONAL_USD:.2f} "
                      f"— unidad/AMOUNT inesperado. NO SE OPERA.")
            return False
        need = nocional / max(LEVERAGE, 1)
    except Exception as e:
        log.warning(f"Capacidad: nocional no calculable: {e}")
        return False
    try:
        raw = exchange.privateGetAccountBalance()
        details = ((raw or {}).get('data') or [{}])[0].get('details') or []
        total = sum(_f(d.get('eqUsd')) for d in details)
        verdict = 'CABE' if total >= need else 'NO CABE'
        log.info(f"Margen 1 contrato: ~${need:.2f} | colateral real: ~${total:.2f} -> {verdict}")
        return total >= need
    except Exception as e:
        log.warning(f"Capacidad: colateral no calculable: {e} — BLOQUEO.")
        return False

def _log_maxsize(symbol):
    try:
        inst = exchange.market(symbol)['id']
        ms = exchange.privateGetAccountMaxSize({'instId': inst, 'tdMode': TD_MODE})
        d0 = (ms.get('data') or [{}])[0]
        mb, msz = d0.get('maxBuy'), d0.get('maxSell')
        if mb or msz:
            log.info(f"Capacidad OKX: maxBuy={mb} | maxSell={msz} contratos")
    except Exception as e:
        log.warning(f"MAXSIZE no disponible: {e}")

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

    _log_maxsize(symbol)

    if cooldown_active(symbol):
        log.info("Vigilando (cooldown activo).")
        return

    signal, price, candle_ts = evaluate_signal(symbol)
    if not signal:
        log.info("Sin cruces EMA7/21 en 15m alineados con cruce fresco 4H. Vigilando.")
        return

    if candle_already_traded(symbol, candle_ts):
        return

    log.info(f"Senal {signal} (cruce fresco 4H a favor, RSI en banda). Abriendo 1 contrato...")
    try:
        execute_order(signal, symbol, price, AMOUNT, candle_ts)
    except Exception as e:
        log.error(f"Entrada rechazada: {e}")

def run_once():
    log.info("Modo ciclo unico (GitHub Actions) | XLM Ax2-D (EVENTO) + RSI-BANDA [30-70] (XPERP USD).")
    verify_setup()
    if TEST_MODE:
        catalog_xlm()
        return
    run_cycle()

def main_loop():
    log.info(f"Iniciando bot {BASE_ASSET} Ax2-D | {TIMEFRAME}+{TF_FILTER} (cruce fresco EVENTO) | "
             f"SL {SL_PCT:.1%} / TP {TP_PCT:.1%} | {LEVERAGE}x | cooldown {COOLDOWN_MIN}m | "
             f"RSI-banda [{RSI_LO:.0f}-{RSI_HI:.0f}] | {HOST}")
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
        time.sleep(1800)  # 30 min — alineado con el privilegio

if __name__ == "__main__":
    if SINGLE_CYCLE:
        run_once()
    else:
        main_loop()
