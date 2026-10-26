#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# main-xpl.py — EL ATUNERO · Nave Autónoma XPL
#
# v5.1-atunero-ctx — alineado ESTRICTAMENTE con CLAUDE_CONTEXT.md.
#   "XPL fue el ÚNICO que zarpó y trajo atún."
#
# LINAJE: xpl.py v4.2-xperp, resucitado de su propio ADN forense.
#
# LEY DEL ATUNERO (conservada):
#   · X-Perp de XPL en OKX (UM, margen USDC). USDT y EUR prohibidos.
#   · 25 contratos por entrada (1 contrato = 10 XPL -> 250 XPL).
#   · SL = 1.2 × distancia del precio a la EMA lenta, acotado 1.5 %–3 %.
#   · TP = SL × 1.5. Margen cruzado. Fail-closed: sin SL/TP confirmados
#     en OKX, la posición se cierra de emergencia. Punto.
#
# ALINEACIÓN CON CLAUDE_CONTEXT.md (v5.1):
#   · EMA3/EMA21 en 1H, SOLO velas cerradas (verificadas por timestamp).
#   · SEÑAL = CRUCE en ventana -2/-3/-4 (ya no basta el estado de las EMA).
#   · FILTRO: separation_pct = abs(EMA3-EMA21)/EMA21*100 >= 0.17 (antes 0.15).
#   · clOrdId derivado del timestamp de la vela + chequeo anti-duplicados.
#   · Cooldown post-pérdida (fail-closed).
#   · Guardia de nocional y control de capacidad.
#   · ctVal / ctValCcy siempre de la API; nunca se asume el tamaño.
#   · Posición primero; veto USDT re-verificado antes de ordenar.
#   · 4D = referencia macro, 30m = apoyo: SOLO informativos, no generan entradas.
#
# LECCIONES DEL CASO FANTASMA (soldadas al casco):
#   1) Barrido de huérfanos: los SL/TP viven en los servidores de OKX
#      aunque el barco muera. Si NO hay posición y SÍ hay algos pendientes,
#      son torpedos fantasma -> se cancelan antes de decidir.
#   2) SHADOW por defecto: el primer viaje es en sombra (cero munición).
#      Armar = SHADOW: '0' en el workflow.
#
# NOTA: la lógica de SL/TP dinámico y los 25 contratos son comportamiento
#   validado previo; CLAUDE_CONTEXT.md no los define y se preservan.

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


def _px_match(a, b):
    """Compara dos precios con 0,5 % de tolerancia."""
    a, b = _f(a), _f(b)
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / b < 0.005


# ───────────── PARÁMETROS (la ley del atunero + contexto) ─────────────
VERSION = 'v5.1-atunero-ctx'
BASE_ASSET = 'XPL'
TIMEFRAME = '1h'
SUPPORT_TIMEFRAME = '30m'    # apoyo operacional (informativo, no bloquea)
MACRO_TIMEFRAME = '4d'       # referencia macro (informativa, no bloquea)
EMA_FAST = 3
EMA_SLOW = 21
MIN_SEPARATION_PCT = 0.17    # filtro obligatorio (%), CLAUDE_CONTEXT.md
SIGNAL_WINDOW = (-2, -3, -4)
WARMUP = 30
CONTRACT_AMOUNT = _f(os.getenv('XPL_CONTRACTS')) or 25.0   # 1 ct = 10 XPL
MAX_NOTIONAL_USD = _f(os.getenv('OKX_XPL_MAX_NOTIONAL', '300')) or 300.0
COOLDOWN_MIN = 60
RR_RATIO = 1.5
SL_MIN, SL_MAX = 0.015, 0.03
MARGIN_MODE = 'cross'
HOST = 'my.okx.com'
CANDLES = 150
SHADOW = os.getenv('SHADOW', '1') == '1'   # Sombra por defecto: freno de flota
LEVERAGE = os.getenv('OKX_LEVERAGE', '')


def build_exchange():
    k = os.getenv('OKX_API_KEY', '')
    s = os.getenv('OKX_SECRET_KEY', '')
    p = os.getenv('OKX_PASSWORD', '')
    if not (k and s and p):
        log.error("[FALLO-CERRADO] Faltan claves API de OKX en las variables de entorno.")
        raise SystemExit(1)
    return ccxt.okx({
        'apiKey': k, 'secret': s, 'password': p,
        'enableRateLimit': True,
        'hostname': HOST,
        'options': {'defaultType': 'swap'},
        'urls': {'api': {'rest': f'https://{HOST}'}},
    })


# ───────────── INSTRUMENTO (USDT PROHIBIDO) ─────────────
def _is_forbidden(ex, sym):
    m = ex.market(sym)
    settle = str(m.get('settle') or '').upper()
    quote = str(m.get('quote') or '').upper()
    inst_id = str(m.get('id') or '').upper()
    return settle == 'USDT' or quote == 'USDT' or 'USDT' in inst_id


def pick_future(ex):
    """X-Perp de XPL (UM, USD/USDC). Excluye siempre cualquier mercado USDT."""
    ex.load_markets()
    log.info(f"Mercados cargados: {len(ex.markets)}")
    now_ms = ex.milliseconds()
    elegido = None
    for m in ex.markets.values():
        if str(m.get('base') or '').upper() != BASE_ASSET:
            continue
        if not (m.get('swap') or m.get('future')):
            continue
        mid = str(m.get('id') or '').upper()
        settle = str(m.get('settle') or '').upper()
        quote = str(m.get('quote') or '').upper()
        log.info(f"DERIVADO {BASE_ASSET}: {m.get('symbol')} | id={m.get('id')} | tipo={m.get('type')} | "
                 f"settle={settle} | active={m.get('active')} | contractSize={m.get('contractSize')} | "
                 f"expiry={m.get('expiryDatetime')}")
        if 'USDT' in (settle, quote) or 'USDT' in mid:
            continue
        if m.get('active') is False:
            continue
        exp = m.get('expiry')
        if exp and exp < now_ms:
            continue
        if 'XPERP' in mid or settle == 'USDC' or quote == 'USDC':
            if elegido is None:
                elegido = m
    return elegido['symbol'] if elegido else None


def _inst_type(ex, sym):
    try:
        t = str((ex.market(sym).get('info') or {}).get('instType') or '').upper()
        if t in ('SWAP', 'FUTURES'):
            return t
    except Exception:
        pass
    m = ex.market(sym)
    return 'SWAP' if (m.get('swap') or m.get('type') == 'swap') else 'FUTURES'


def _contract_meta(ex, sym, price=None):
    """Nunca asumir el tamaño del contrato: ctVal y ctValCcy salen SIEMPRE de la API.
    Devuelve (ctval, ccy, settle, price, usd_por_contrato)."""
    market = ex.market(sym)
    info = market.get('info') or {}
    ctval = _f(info.get('ctVal') or market.get('contractSize'))
    ccy = str(info.get('ctValCcy') or '').upper()
    if ctval <= 0 or not ccy:
        raise RuntimeError("Contrato sin ctVal/ctValCcy validos en la API (ctVal=" +
                           str(info.get('ctVal')) + ", ctValCcy=" + str(info.get('ctValCcy')) +
                           "). NO SE OPERA.")
    settle = str(info.get('settleCcy') or market.get('settle') or '?').upper()
    if price is None:
        price = ex.fetch_ticker(sym).get('last') or 0
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


# ───────────── ALGOS / HUÉRFANOS ─────────────
def list_pending_algos(ex, inst_id):
    """Lista los algos pendientes (OCO + condicionales) del instrumento."""
    algos = []
    for ord_type in ('oco', 'conditional'):
        resp = ex.private_get_trade_orders_algo_pending(
            {'instId': inst_id, 'ordType': ord_type})
        algos.extend((resp or {}).get('data', []))
    return algos


def cancel_algo(ex, inst_id, algo_id):
    ex.private_post_trade_cancel_algo([{'instId': inst_id, 'algoId': algo_id}])


def sweep_orphans(ex, sym):
    """Lección del fantasma: si NO hay posición y SÍ hay SL/TP pendientes,
    son torpedos huérfanos que sobrevivieron a su barco -> se cancelan.
    Devuelve True solo si la cubierta quedó verificablemente limpia.
    Fail-closed: si no se puede verificar la posición, NO se toca nada."""
    try:
        pos_abierta = False
        for p in ex.fetch_positions([sym]):
            if abs(_f(p.get('contracts'))) > 0:
                pos_abierta = True
                break
        if pos_abierta:
            log.info("Cubierta: posición viva — los algos son su coraza, no se tocan.")
            return True
        inst_id = ex.market(sym)['id']
        huerfanos = list_pending_algos(ex, inst_id)
        if not huerfanos:
            log.info("Cubierta limpia: cero posición, cero algos huérfanos.")
            return True
        log.warning(f"TORPEDOS FANTASMA: {len(huerfanos)} algo(s) huérfano(s) sin posición. Cancelando...")
        fallos = 0
        for algo in huerfanos:
            try:
                cancel_algo(ex, inst_id, algo.get('algoId'))
                log.info(f"  Torpedo cancelado algoId={algo.get('algoId')} "
                         f"SL={algo.get('slTriggerPx')} TP={algo.get('tpTriggerPx')}")
            except Exception as e:
                fallos += 1
                log.error(f"  No se pudo cancelar algoId={algo.get('algoId')}: {e}")
        return fallos == 0
    except Exception as e:
        log.error(f"[FALLO-CERRADO] Barrido de huérfanos falló: {e}")
        return False


def protection_confirmed(ex, sym, sl, tp):
    """True SOLO si hay un algo vivo en OKX con esos triggers de SL y TP.
    Ante cualquier duda devuelve False (fail-closed -> cierre)."""
    try:
        inst_id = ex.market(sym)['id']
        for algo in list_pending_algos(ex, inst_id):
            log.info(f"Algo vivo id={algo.get('algoId')} "
                     f"SL={algo.get('slTriggerPx')} TP={algo.get('tpTriggerPx')}")
            if (_px_match(algo.get('slTriggerPx'), sl)
                    and _px_match(algo.get('tpTriggerPx'), tp)):
                return True
    except Exception as e:
        log.error(f"[FALLO-CERRADO] Consulta de algos falló: {e}")
    return False


# ───────────── POSICIÓN ─────────────
def has_open_position(ex, sym):
    """Fail-closed: ante cualquier duda devuelve True (no abrir).
    Anti-doble-entrada: si hay SL/TP vivos en el instrumento, asume posición."""
    try:
        for p in ex.fetch_positions([sym]):
            if abs(_f(p.get('contracts'))) > 0:
                return True
        if list_pending_algos(ex, ex.market(sym)['id']):
            log.info("Sin posición visible pero hay SL/TP vivos: asumo posición abierta.")
            return True
        return False
    except Exception as e:
        log.error(f"[FALLO-CERRADO] No se pudo verificar posiciones: {e}")
        return True


# ───────────── LASTRE / CAPACIDAD ─────────────
def log_equity(ex, sym):
    """Bitácora de lastre: el capitán debe ver con qué saldo navega."""
    try:
        settle = str(ex.market(sym).get('settle') or 'USDC').upper()
        bal = ex.fetch_balance()
        pocket = bal.get(settle) or {}
        total = _f(pocket.get('total')) or _f(pocket.get('free'))
        if total:
            log.info(f"Lastre verificado: {total:.2f} {settle} en la bodega.")
        else:
            log.info(f"Lastre {settle}: sin cifra legible en la bodega.")
    except Exception as e:
        log.warning(f"No se pudo leer el lastre: {e}")


def capacity_ok(ex, sym):
    """Control de capacidad (fail-closed): nocional válido y colateral suficiente."""
    try:
        _ct, _ccy, _settle, _price, usd = _contract_meta(ex, sym)
        nocional = usd * CONTRACT_AMOUNT
        if nocional <= 0:
            log.error(f"GUARDIA: nocional no valido (${nocional:.2f}). NO SE OPERA.")
            return False
        if nocional > MAX_NOTIONAL_USD:
            log.error(f"GUARDIA: nocional ${nocional:.2f} > maximo ${MAX_NOTIONAL_USD:.2f}. NO SE OPERA.")
            return False
        lev_txt = str(LEVERAGE).strip()
        lev = int(lev_txt) if lev_txt.isdigit() and int(lev_txt) > 0 else 1
        need = nocional / lev
    except Exception as e:
        log.warning(f"Capacidad: nocional no calculable: {e}")
        return False
    try:
        raw = ex.privateGetAccountBalance()
        details = ((raw or {}).get('data') or [{}])[0].get('details') or []
        total = sum(_f(d.get('eqUsd')) for d in details)
        log.info(f"Margen ({CONTRACT_AMOUNT} ct): ~${need:.2f} | colateral: ~${total:.2f} -> "
                 f"{'CABE' if total >= need else 'NO CABE'}")
        return total >= need
    except Exception as e:
        log.warning(f"Capacidad: colateral no calculable: {e} — BLOQUEO.")
        return False


# ───────────── COOLDOWN POST-PÉRDIDA ─────────────
def cooldown_active(ex, sym):
    try:
        market_id = ex.market(sym)['id']
        hist = ex.privateGetTradeFillsHistory({
            'instType': _inst_type(ex, sym),
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


# ───────────── IDEMPOTENCIA (clOrdId por vela) ─────────────
def _cl_order_id(candle_ts):
    return "XPL" + str(int(candle_ts))


def candle_already_traded(ex, sym, candle_ts):
    cl = _cl_order_id(candle_ts)
    try:
        inst = ex.market(sym)['id']
        resp = ex.private_get_trade_order({'instId': inst, 'clOrdId': cl})
        data = resp.get('data') or []
        if data and str(data[0].get('sCode', '0')) == '0':
            log.info(f"Vela ya operada (clOrdId {cl}). Duplicado bloqueado.")
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


# ───────────── INDICADORES / SEÑAL ─────────────
def separation_pct(fast_val, slow_val):
    """abs(EMA3 - EMA21) / EMA21 * 100. None si no calculable."""
    slow_val = _f(slow_val)
    if slow_val == 0:
        return None
    return abs(_f(fast_val) - slow_val) / slow_val * 100.0


def _ema(s, length):
    return s.ewm(span=length, adjust=False).mean()


def _closed_df(ex, sym, timeframe, limit):
    """OHLCV con la vela en formación descartada (por timestamp). Solo velas cerradas."""
    ohlcv = ex.fetch_ohlcv(sym, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    if df.empty:
        return df
    tf_ms = int(ex.parse_timeframe(timeframe)) * 1000
    now_ms = int(time.time() * 1000)
    if int(df['timestamp'].iloc[-1]) + tf_ms > now_ms:
        df = df.iloc[:-1].reset_index(drop=True)
    return df


def log_context_frames(ex, sym):
    """4D = referencia macro, 30m = apoyo operacional.
    Solo velas CERRADAS. Puramente informativo: nunca bloquea ni genera señal."""
    for label, tf in (('MACRO', MACRO_TIMEFRAME), ('APOYO', SUPPORT_TIMEFRAME)):
        try:
            offered = getattr(ex, 'timeframes', None) or {}
            if offered and tf not in offered:
                log.info(f"{label} {tf}: marco no ofrecido por OKX/ccxt — omitido (informativo).")
                continue
            df = _closed_df(ex, sym, tf, 100)
            if len(df) < EMA_SLOW + 5:
                log.info(f"{label} {tf}: velas cerradas insuficientes ({len(df)}) (informativo).")
                continue
            f = _ema(df['close'], EMA_FAST)
            s = _ema(df['close'], EMA_SLOW)
            sep = separation_pct(f.iloc[-1], s.iloc[-1])
            log.info(f"{label} {tf} (cerrada) EMA{EMA_FAST}/{EMA_SLOW}: "
                     f"EMA{EMA_FAST} {'>' if f.iloc[-1] > s.iloc[-1] else '<'} EMA{EMA_SLOW} | "
                     f"separacion {f'{sep:.3f}%' if sep is not None else 'n/d'} (informativo)")
        except Exception as e:
            log.warning(f"{label} {tf}: contexto no disponible ({e}) — se ignora.")


def evaluate_signal(ex, sym):
    """Cruce EMA3/EMA21 en 1H CERRADA, ventana -2/-3/-4, con filtro de separación >= 0.17 %.
    Devuelve (side, candle_ts, e_slow_ultima_cerrada, close_ultima_cerrada) o None."""
    try:
        df = _closed_df(ex, sym, TIMEFRAME, CANDLES)
        if len(df) < EMA_SLOW * 3:
            log.error("[FALLO-CERRADO] Histórico insuficiente.")
            return None

        fast = _ema(df['close'], EMA_FAST)
        slow = _ema(df['close'], EMA_SLOW)
        close_last = _f(df['close'].iloc[-1])
        e_s_last = _f(slow.iloc[-1])
        log.info(f"1H EMA{EMA_FAST}/{EMA_SLOW}: {_f(fast.iloc[-1]):.5f}/{e_s_last:.5f} | "
                 f"EMA{EMA_FAST} {'>' if fast.iloc[-1] > slow.iloc[-1] else '<'} EMA{EMA_SLOW} (estado) | "
                 f"cierre {close_last:.5f}")

        log_context_frames(ex, sym)

        for i_curr in SIGNAL_WINDOW:
            i_prev = i_curr - 1
            idx = len(df) + i_curr
            if abs(i_prev) > len(df) - 1 or idx < WARMUP:
                continue
            candle_ts = int(df['timestamp'].iloc[i_curr])

            up = (fast.iloc[i_prev] <= slow.iloc[i_prev]
                  and fast.iloc[i_curr] > slow.iloc[i_curr])
            down = (fast.iloc[i_prev] >= slow.iloc[i_prev]
                    and fast.iloc[i_curr] < slow.iloc[i_curr])
            if not (up or down):
                continue

            side = 'buy' if up else 'sell'
            nombre = 'ALCISTA' if up else 'BAJISTA'

            # FILTRO OBLIGATORIO: el cruce NO es suficiente.
            sep = separation_pct(fast.iloc[i_curr], slow.iloc[i_curr])
            if sep is None:
                log.warning(f"Cruce {nombre} EMA{EMA_FAST}/{EMA_SLOW} (vela {i_curr}): "
                            f"separacion no calculable -> NO OPERAR (fail-closed).")
                continue
            if sep >= MIN_SEPARATION_PCT:
                log.info(f"Cruce {nombre} EMA{EMA_FAST}/{EMA_SLOW} (vela {i_curr}) | "
                         f"Separación EMA = {sep:.3f}% -> válida (min {MIN_SEPARATION_PCT:.2f}%).")
                return side, candle_ts, e_s_last, close_last
            log.info(f"Cruce {nombre} EMA{EMA_FAST}/{EMA_SLOW} (vela {i_curr}) | "
                     f"Separación EMA = {sep:.3f}% -> rechazada (min {MIN_SEPARATION_PCT:.2f}%). NO OPERAR.")

        log.info(f"Sin cruce EMA{EMA_FAST}/{EMA_SLOW} válido en la ventana. Observando.")
    except Exception as e:
        log.error(f"[FALLO-CERRADO] Error en evaluación: {e}")
    return None


# ───────────── RIESGO / EJECUCIÓN ─────────────
def calculate_sl_tp(side, entry, e_slow):
    """SL = 1.2 × distancia del precio a la EMA lenta, acotado 1.5 %–3 %. TP = SL × RR."""
    dist = abs(entry - e_slow) / entry
    sl_pct = max(SL_MIN, min(SL_MAX, dist * 1.2))
    tp_pct = sl_pct * RR_RATIO
    if side == 'buy':
        return entry * (1 - sl_pct), entry * (1 + tp_pct), sl_pct, tp_pct
    return entry * (1 + sl_pct), entry * (1 - tp_pct), sl_pct, tp_pct


def emergency_close(ex, sym, side, amount):
    close_side = 'sell' if side == 'buy' else 'buy'
    try:
        ex.create_order(sym, 'market', close_side, amount,
                        params={'tdMode': MARGIN_MODE, 'reduceOnly': True})
        log.warning("Posición cerrada de emergencia (SL/TP no se pudo garantizar).")
    except Exception as e:
        log.error(f"[CRÍTICO] CIERRE DE EMERGENCIA FALLÓ, REVISAR A MANO EN OKX: {e}")
    # Lección del fantasma: tras un cierre, no dejar torpedos a la deriva.
    try:
        inst_id = ex.market(sym)['id']
        for algo in list_pending_algos(ex, inst_id):
            try:
                cancel_algo(ex, inst_id, algo.get('algoId'))
            except Exception:
                pass
    except Exception as e:
        log.error(f"[AVISO] No se pudieron cancelar los algos tras el cierre: {e}")


def execute_trade(ex, sym, side, amount, ref_price, e_slow, candle_ts):
    # Veto USDT re-verificado + guardia de nocional ANTES de cualquier orden.
    try:
        if _is_forbidden(ex, sym):
            log.error(f"VETO: instrumento USDT prohibido ({sym}). NO SE OPERA.")
            return
        ctval, ccy, _settle, _p, usd = _contract_meta(ex, sym, ref_price)
    except Exception as e:
        log.error(f"[FALLO-CERRADO] Contrato no verificable: {e}")
        return
    nocional = amount * usd
    log.info(f"Nocional: {amount} contrato x {ctval} {ccy} (~${nocional:.2f})")
    if nocional <= 0:
        log.error(f"GUARDIA: nocional no valido (${nocional:.2f}). NO SE OPERA.")
        return
    if nocional > MAX_NOTIONAL_USD:
        log.error(f"GUARDIA: nocional ${nocional:.2f} > maximo ${MAX_NOTIONAL_USD:.2f} "
                  f"— unidad inesperada. NO SE OPERA.")
        return

    sl, tp, sl_pct, tp_pct = calculate_sl_tp(side, ref_price, e_slow)
    sl = float(ex.price_to_precision(sym, sl))
    tp = float(ex.price_to_precision(sym, tp))
    amount = float(ex.amount_to_precision(sym, amount))
    cl_id = _cl_order_id(candle_ts)
    log.info(f"Riesgo | ref {ref_price:.5f} | SL {sl_pct*100:.2f}% ({sl}) | TP {tp_pct*100:.2f}% ({tp}) | "
             f"clOrdId {cl_id}")

    if SHADOW:
        log.info(f"[SOMBRA] {side.upper()} {amount} {sym} con SL {sl} / TP {tp}. No se dispara nada.")
        return

    if LEVERAGE:
        try:
            ex.set_leverage(int(LEVERAGE), sym, params={'mgnMode': MARGIN_MODE})
        except Exception as e:
            log.error(f"[FALLO-CERRADO] No se pudo fijar apalancamiento: {e}")
            return

    params = {
        'tdMode': MARGIN_MODE,
        'clOrdId': cl_id,
        'stopLoss': {'triggerPrice': sl, 'type': 'market'},
        'takeProfit': {'triggerPrice': tp, 'type': 'market'},
    }
    try:
        order = ex.create_order(sym, 'market', side, amount, params=params)
        log.info(f"Orden enviada id={order.get('id')} con SL/TP adjuntos.")
    except ccxt.ExchangeError as e:
        if '51016' in str(e):
            log.info("OKX: clOrdId duplicado (51016) — idempotencia OK.")
            return
        log.error(f"[ERROR] Orden rechazada, no se abrió posición: {e}")
        return
    except Exception as e:
        log.error(f"[ERROR] Orden rechazada, no se abrió posición: {e}")
        return

    pos_ok = has_open_position(ex, sym)
    prot_ok = protection_confirmed(ex, sym, sl, tp)

    if not pos_ok and not prot_ok:
        log.warning("Orden aceptada pero sin posición ni protección verificables. Revisar OKX.")
        return
    if not prot_ok:
        log.error("[FALLO-CERRADO] Posición abierta pero SL/TP SIN confirmar. Cerrando por seguridad.")
        emergency_close(ex, sym, side, amount)
        return
    extra = "" if pos_ok else " (posición no verificable, protección sí)"
    log.info(f"Nave blindada: SL/TP confirmados en OKX{extra}")


# ───────────── CICLO ─────────────
def main():
    modo = 'SOMBRA' if SHADOW else 'REAL'
    log.info(f"Zarpando ciclo de XPL — EL ATUNERO [{VERSION}] | {modo} | host https://{HOST}")
    ex = build_exchange()
    sym = pick_future(ex)
    if not sym:
        log.info("XPL: no hay X-Perp activo fuera de USDT. Saliendo.")
        return

    try:
        if _is_forbidden(ex, sym):
            log.error(f"VETO: {sym} es USDT — prohibido. Saliendo.")
            return
        ct_val, ccy, settle, _p, usd1 = _contract_meta(ex, sym)
    except Exception as e:
        log.error(f"[FALLO-CERRADO] Contrato no verificable: {e}")
        return
    mk = ex.market(sym)
    log.info(f"Instrumento: {sym} | id={mk.get('id')} | 1 contrato = {ct_val} {ccy} | settle={settle} | "
             f"nocional ~${usd1:.2f}/ct | {_inst_type(ex, sym)}")
    log.info(f"Carga de pesca: {CONTRACT_AMOUNT} contratos = {CONTRACT_AMOUNT*ct_val:.0f} {ccy} por entrada.")

    log_equity(ex, sym)

    # Lección del fantasma: barrer torpedos huérfanos ANTES de cualquier decisión.
    if not sweep_orphans(ex, sym):
        log.error("[FALLO-CERRADO] Cubierta sin barrer. No se zarpa este ciclo.")
        return

    # Posición primero.
    if has_open_position(ex, sym):
        log.info("Posición activa (o no verificable). Omitiendo entrada.")
        return

    if not capacity_ok(ex, sym):
        log.info("Sin capacidad. Observando.")
        return

    if cooldown_active(ex, sym):
        log.info("Observando (cooldown activo).")
        return

    sig = evaluate_signal(ex, sym)
    if not sig:
        return
    side, candle_ts, e_slow, close = sig

    if candle_already_traded(ex, sym, candle_ts):
        return

    ref = _f(ex.fetch_ticker(sym).get('last')) or close
    log.info(f"Señal {'LONG' if side == 'buy' else 'SHORT'} confirmada "
             f"(cruce EMA{EMA_FAST}/{EMA_SLOW} 1H + separación válida). Ejecutando...")
    execute_trade(ex, sym, side, CONTRACT_AMOUNT, ref, e_slow, candle_ts)
    log.info("Ciclo completado.")


if __name__ == '__main__':
    main()
