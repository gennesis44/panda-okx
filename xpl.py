#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# xpl.py — Nave Autónoma XPL · Ley de la Flota (Carbono)
#   Operaciones sobre XPL estrictamente en USDC (USDT y EUR prohibidos).
#   Protección fail-closed: si falta configuración, credenciales o no se pueden
#   colocar SL/TP, el bot NO deja posiciones desprotegidas.

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


# ───────────── PARÁMETROS ─────────────
BASE_ASSET = 'XPL'
TIMEFRAME = '1h'
EMA_FAST = 3                 # EMA rápida 3
EMA_SLOW = 21                # EMA lenta 21
MIN_SEPARATION = 0.0015      # Separación mínima entre EMAs: 0.15 %
CONTRACT_AMOUNT = 25.0       # 25 contratos por operación
RR_RATIO = 1.5               # Riesgo / Beneficio 1 : 1.5
SL_MIN, SL_MAX = 0.015, 0.03 # SL entre 1.5 % y 3 %
MARGIN_MODE = 'cross'
HOST = 'my.okx.com'
CANDLES = 150                # histórico para que la EMA21 converja
DRY_RUN = os.getenv('DRY_RUN', '0') == '1'   # DRY_RUN=1 → no envía órdenes
LEVERAGE = os.getenv('OKX_LEVERAGE', '')     # opcional, p.ej. "5"


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
    """Perpetuo lineal de XPL liquidado en USDC. Nunca USDT."""
    ex.load_markets()
    for m in ex.markets.values():
        if m.get('base') != BASE_ASSET or not m.get('active'):
            continue
        if not m.get('swap'):
            continue
        settle = str(m.get('settle') or '').upper()
        quote = str(m.get('quote') or '').upper()
        if settle == 'USDC' and quote == 'USDC':
            return m['symbol']
    return None


def has_open_position(ex, sym):
    """Fail-closed: ante cualquier duda devuelve True (no abrir)."""
    try:
        for p in ex.fetch_positions([sym]):
            if abs(_f(p.get('contracts'))) > 0:
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
    log.info(f"Riesgo | ref {ref_price:.4f} | SL {sl_pct*100:.2f}% ({sl}) | TP {tp_pct*100:.2f}% ({tp})")

    if DRY_RUN:
        log.info(f"[DRY_RUN] {side.upper()} {amount} {sym} con SL {sl} / TP {tp}. No se envía nada.")
        return

    if LEVERAGE:
        try:
            ex.set_leverage(int(LEVERAGE), sym, params={'mgnMode': MARGIN_MODE})
        except Exception as e:
            log.error(f"[FALLO-CERRADO] No se pudo fijar apalancamiento: {e}")
            return

    # Entrada + SL + TP en UNA sola orden (OKX attachAlgoOrds): atómico.
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

    # Verificación: si la posición existe pero no hay protección confirmada, cerrar.
    try:
        if not has_open_position(ex, sym):
            log.warning("La orden no dejó posición abierta (revisar en OKX).")
    except Exception as e:
        log.error(f"Verificación post-orden falló: {e}")
        emergency_close(ex, sym, side, amount)


def main():
    log.info("Iniciando ciclo único de XPL...")
    ex = build_exchange()
    sym = pick_future(ex)
    if not sym or 'USDC' not in sym:
        log.info("XPL: no hay perpetuo activo en USDC. Saliendo.")
        return
    log.info(f"Instrumento: {sym}")

    ohlcv = ex.fetch_ohlcv(sym, TIMEFRAME, limit=CANDLES)
    if len(ohlcv) < EMA_SLOW * 3:
        log.error("[FALLO-CERRADO] Histórico insuficiente.")
        return
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

    fast = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
    slow = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()

    # Vela CERRADA (la última está en formación y repinta la señal)
    i = len(df) - 2
    close = _f(df['close'].iloc[i])
    e_f, e_s = _f(fast.iloc[i]), _f(slow.iloc[i])
    sep = (e_f - e_s) / e_s if e_s else 0.0
    log.info(f"{sym} | EMA{EMA_FAST}/{EMA_SLOW}: {e_f:.4f}/{e_s:.4f} | sep {sep*100:.3f}% | cierre {close:.4f}")

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
