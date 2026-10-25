#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# main-xpl.py — EL ATUNERO · Nave Autónoma XPL
#
# v5.0-atunero — Reclutamiento restaurado por decreto del capitán:
#   "XPL fue el ÚNICO que zarpó y trajo atún."
#
# LINAJE: xpl.py v4.2-xperp, resucitado de su propio ADN forense
#   (clon del repositorio, commit previo a su funeral). Este casco
#   NO deriva de ningún otro destructor: la ley es la del atunero.
#
# LEY QUE TRAJO ATÚN (intacta, palabra por palabra):
#   · X-Perp de XPL en OKX (UM, margen USDC). USDT y EUR prohibidos.
#   · EMA 3/21 en velas de 1h, candado de separación 0.15 %.
#   · 25 contratos por entrada (1 contrato = 10 XPL -> 250 XPL).
#   · SL = 1.2 × distancia del precio a la EMA lenta, acotado 1.5 %–3 %.
#   · TP = SL × 1.5. Margen cruzado. Fail-closed: sin SL/TP confirmados
#     en OKX, la posición se cierra de emergencia. Punto.
#
# LECCIONES DEL CASO FANTASMA (soldadas al casco):
#   1) Barrido de huérfanos: los SL/TP viven en los servidores de OKX
#      aunque el barco muera o se desmantelen su workflow. Cada ciclo,
#      si NO hay posición y SÍ hay algos pendientes, son torpedos
#      fantasma -> se cancelan antes de tomar cualquier decisión.
#   2) SHADOW por defecto: el primer viaje es en sombra (solo bitácora,
#      cero munición). Armar = SHADOW: '0' en el workflow.
#
# PD: este casco no lleva "humans.txt" — eso era del forense.

import os
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


# ───────────── PARÁMETROS (la ley del atunero) ─────────────
VERSION = 'v5.0-atunero'
BASE_ASSET = 'XPL'
TIMEFRAME = '1h'
EMA_FAST = 3
EMA_SLOW = 21
MIN_SEPARATION = 0.0015      # 0.15 % — el candado original
CONTRACT_AMOUNT = _f(os.getenv('XPL_CONTRACTS')) or 25.0   # 1 ct = 10 XPL
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


def execute_trade(ex, sym, side, amount, ref_price, e_slow):
    sl, tp, sl_pct, tp_pct = calculate_sl_tp(side, ref_price, e_slow)
    sl = float(ex.price_to_precision(sym, sl))
    tp = float(ex.price_to_precision(sym, tp))
    amount = float(ex.amount_to_precision(sym, amount))
    log.info(f"Riesgo | ref {ref_price:.5f} | SL {sl_pct*100:.2f}% ({sl}) | TP {tp_pct*100:.2f}% ({tp})")

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
        'stopLoss': {'triggerPrice': sl, 'type': 'market'},
        'takeProfit': {'triggerPrice': tp, 'type': 'market'},
    }
    try:
        order = ex.create_order(sym, 'market', side, amount, params=params)
        log.info(f"Orden enviada id={order.get('id')} con SL/TP adjuntos.")
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


def main():
    modo = 'SOMBRA' if SHADOW else 'REAL'
    log.info(f"Zarpando ciclo de XPL — EL ATUNERO [{VERSION}] | {modo}")
    ex = build_exchange()
    sym = pick_future(ex)
    if not sym:
        log.info("XPL: no hay X-Perp activo fuera de USDT. Saliendo.")
        return
    mk = ex.market(sym)
    ct_val = _f(mk.get('contractSize')) or 1.0
    log.info(f"Instrumento: {sym} | id={mk.get('id')} | 1 contrato = {ct_val} XPL")
    log.info(f"Carga de pesca: {CONTRACT_AMOUNT} contratos = {CONTRACT_AMOUNT*ct_val:.0f} XPL por entrada.")

    log_equity(ex, sym)

    # Lección del fantasma: barrer torpedos huérfanos ANTES de cualquier decisión.
    if not sweep_orphans(ex, sym):
        log.error("[FALLO-CERRADO] Cubierta sin barrer. No se zarpa este ciclo.")
        return

    ohlcv = ex.fetch_ohlcv(sym, TIMEFRAME, limit=CANDLES)
    if len(ohlcv) < EMA_SLOW * 3:
        log.error("[FALLO-CERRADO] Histórico insuficiente.")
        return
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    fast = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
    slow = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()

    i = len(df) - 2          # vela cerrada
    close = _f(df['close'].iloc[i])
    e_f, e_s = _f(fast.iloc[i]), _f(slow.iloc[i])
    sep = (e_f - e_s) / e_s if e_s else 0.0
    log.info(f"EMA{EMA_FAST}/{EMA_SLOW}: {e_f:.5f}/{e_s:.5f} | sep {sep*100:.3f}% | cierre {close:.5f}")

    if abs(sep) < MIN_SEPARATION:
        log.info(f"Separación < {MIN_SEPARATION*100:.2f}%: sin señal. Observando.")
        return

    side = 'buy' if sep > 0 else 'sell'

    if has_open_position(ex, sym):
        log.info("Posición activa (o no verificable). Omitiendo entrada.")
        return

    ref = _f(ex.fetch_ticker(sym).get('last')) or close
    log.info(f"Señal {'LONG' if side == 'buy' else 'SHORT'} confirmada. Ejecutando...")
    execute_trade(ex, sym, side, CONTRACT_AMOUNT, ref, e_s)
    log.info("Ciclo completado.")


if __name__ == '__main__':
    main()
