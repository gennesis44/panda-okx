#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# xpl.py — Nave Autónoma XPL · Ley de la Flota (Carbono)
#   Opera el X-Perp XPLUSD (UM, margen en USDC). USDT y EUR prohibidos.
#   Protección fail-closed: si falta configuración, credenciales o no se pueden
#   colocar/confirmar SL/TP, el bot NO deja posiciones desprotegidas.

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


def protection_confirmed(ex, sym, sl, tp):
    """True SOLO si hay un algo vivo en OKX con esos triggers de SL y TP.
    Ante cualquier duda devuelve False (fail-closed -> cierre)."""
    try:
        inst_id = ex.market(sym)['id']
        for ord_type in ('oco', 'conditional'):
            resp = ex.private_get_trade_orders_algo_pending(
                {'instId': inst_id, 'ordType': ord_type})
            for algo in (resp or {}).get('data', []):
                log.info(f"Algo vivo [{ord_type}] id={algo.get('algoId')} "
                         f"SL={algo.get('slTriggerPx')} TP={algo.get('tpTriggerPx')}")
                if (_px_match(algo.get('slTriggerPx'), sl)
                        and _px_match(algo.get('tpTriggerPx'), tp)):
                    return True
    except Exception as e:
        log.error(f"[FALLO-CERRADO] Consulta de algos falló: {e}")
    return False


# ───────────── PARÁMETROS ─────────────
VERSION = 'v4.2-xperp'
BASE_ASSET = 'XPL'
TIMEFRAME = '1h'
EMA_FAST = 3
EMA_SLOW = 21
MIN_SEPARATION = 0.0015      # 0.15 %
CONTRACT_AMOUNT = 25.0       # contratos (1 contrato = 10 XPL)
RR_RATIO = 1.5
SL_MIN, SL_MAX = 0.015, 0.03
MARGIN_MODE = 'cross'
HOST = 'my.okx.com'
CANDLES = 150
DRY_RUN = os.getenv('DRY_RUN', '0') == '1'
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


def has_open_position(ex, sym):
    """Fail-closed: ante cualquier duda devuelve True (no abrir).
    Anti-doble-entrada: si hay SL/TP vivos en el instrumento, asume posición."""
    try:
        for p in ex.fetch_positions([sym]):
            if abs(_f(p.get('contracts'))) > 0:
                return True
        inst_id = ex.market(sym)['id']
        for ord_type in ('oco', 'conditional'):
            resp = ex.private_get_trade_orders_algo_pending(
                {'instId': inst_id, 'ordType': ord_type})
            if (resp or {}).get('data'):
                log.info("Sin posición visible pero hay SL/TP vivos: asumo posición abierta.")
                return True
        return False
    except Exception as e:
        log.error(f"[FALLO-CERRADO] No se pudo verificar posiciones: {e}")
        return True


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


def execute_trade(ex, sym, side, amount, ref_price, e_slow):
    sl, tp, sl_pct, tp_pct = calculate_sl_tp(side, ref_price, e_slow)
    sl = float(ex.price_to_precision(sym, sl))
    tp = float(ex.price_to_precision(sym, tp))
    amount = float(ex.amount_to_precision(sym, amount))
    log.info(f"Riesgo | ref {ref_price:.5f} | SL {sl_pct*100:.2f}% ({sl}) | TP {tp_pct*100:.2f}% ({tp})")

    if DRY_RUN:
        log.info(f"[DRY_RUN] {side.upper()} {amount} {sym} con SL {sl} / TP {tp}. No se envía nada.")
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
    log.info(f"Iniciando ciclo único de XPL... [{VERSION}]")
    ex = build_exchange()
    sym = pick_future(ex)
    if not sym:
        log.info("XPL: no hay X-Perp activo fuera de USDT. Saliendo.")
        return
    mk = ex.market(sym)
    ct_val = _f(mk.get('contractSize')) or 1.0
    log.info(f"Instrumento: {sym} | id={mk.get('id')} | 1 contrato = {ct_val} XPL")

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
    log.info(f"Tamaño: {CONTRACT_AMOUNT} contratos = {CONTRACT_AMOUNT*ct_val:.0f} XPL "
             f"(~{CONTRACT_AMOUNT*ct_val*close:.2f} USD nocionales)")

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
