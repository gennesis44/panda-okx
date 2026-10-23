#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# xpl.py — Nave Autónoma XPL · Ley de la Flota (Carbono)
#   Operaciones sobre XPL estrictamente en USDC (USDT y EUR prohibidos).
#   Protección fail-closed: si falta configuración o credenciales, se detiene ordenadamente.

import os
import sys
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

BASE_ASSET = 'XPL'
TIMEFRAME = '1h'          # Marco temporal de 1 Hora
EMA_FAST = 4              # EMA rápida 4
EMA_SLOW = 21             # EMA lenta 21
CONTRACT_AMOUNT = 25.0    # 25 contratos por operación (ajustado para superar el mínimo)
RR_RATIO = 1.5            # Ratio Riesgo / Beneficio (1 : 1.5)
MARGIN_MODE = 'cross'     # Modo de margen en OKX: 'cross' (cruzado) o 'isolated' (aislado)
HOST = 'https://my.okx.com'

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
        'options': {'defaultType': 'swap'},
        'urls': {'api': {'rest': HOST}},
    })

def pick_future(ex):
    ex.load_markets()
    for m in ex.markets.values():
        settle = str(m.get('settle') or '').upper()
        quote = str(m.get('quote') or '').upper()
        if m.get('base') == BASE_ASSET and m.get('active'):
            if settle == 'USDC' or quote == 'USDC':
                return m['symbol']
    return None

def check_existing_positions(ex, sym):
    try:
        positions = ex.fetch_positions([sym])
        for p in positions:
            if float(p.get('contracts', 0)) > 0:
                return True
    except Exception as e:
        log.error(f"Error al verificar posiciones activas: {e}")
    return False

def calculate_dynamic_sl_tp(side, entry_price, e4, e21):
    dist_ema = abs(entry_price - e21) / entry_price
    sl_pct = max(0.015, min(0.03, dist_ema * 1.2))
    tp_pct = sl_pct * RR_RATIO

    if side == 'buy':
        sl_price = entry_price * (1 - sl_pct)
        tp_price = entry_price * (1 + tp_pct)
    else:
        sl_price = entry_price * (1 + sl_pct)
        tp_price = entry_price * (1 - tp_pct)

    return sl_price, tp_price, sl_pct, tp_pct

def execute_trade_with_sl_tp(ex, sym, side, amount, current_price, e4, e21):
    try:
        log.info(f"Ejecutando orden de {side.upper()} para {sym} por {amount} contratos a mercado...")
        
        # 1. Orden de entrada principal con tdMode requerido por OKX
        order = ex.create_order(sym, 'market', side, amount, params={'tdMode': MARGIN_MODE})
        entry_price = _f(order.get('average') or order.get('price') or current_price)
        log.info(f"Orden ejecutada. Precio de entrada: {entry_price:.4f}")

        sl_price, tp_price, sl_pct, tp_pct = calculate_dynamic_sl_tp(side, entry_price, e4, e21)
        close_side = 'sell' if side == 'buy' else 'buy'

        log.info(f"Gestión de Riesgo | SL: {sl_pct*100:.2f}% ({sl_price:.4f}) | TP: {tp_pct*100:.2f}% ({tp_price:.4f})")

        # 2. Stop Loss condicional con tdMode
        ex.create_order(sym, 'conditional', close_side, amount, None, params={
            'triggerPrice': sl_price, 
            'stopPrice': sl_price, 
            'reduceOnly': True, 
            'tdMode': MARGIN_MODE
        })
        log.info(f"Stop Loss colocado en: {sl_price:.4f}")

        # 3. Take Profit límite con tdMode
        ex.create_order(sym, 'limit', close_side, amount, tp_price, params={
            'reduceOnly': True, 
            'tdMode': MARGIN_MODE
        })
        log.info(f"Take Profit colocado en: {tp_price:.4f}")

    except Exception as e:
        log.error(f"[ERROR CRÍTICO] Fallo al colocar la orden o protecciones SL/TP: {e}")

def main():
    log.info("Iniciando ciclo único de XPL en la nube...")
    ex = build_exchange()
    sym = pick_future(ex)
    
    if not sym:
        log.info("XPL: No se encontró instrumento activo en USDC. Saliendo a puerto.")
        return

    log.info(f"Instrumento detectado: {sym}")
    ohlcv = ex.fetch_ohlcv(sym, TIMEFRAME, limit=50)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    e4_series = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
    e21_series = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
    
    last = len(df) - 1
    current_close = _f(df['close'].iloc[last])
    e4_val = _f(e4_series.iloc[last])
    e21_val = _f(e21_series.iloc[last])
    
    estado = "ALZA" if e4_val > e21_val else "BAJA"
    log.info(f"XPL {sym} | EMA{EMA_FAST}/{EMA_SLOW}: {e4_val:.4f}/{e21_val:.4f} | Estado: {estado} | Precio: {current_close:.4f}")

    market = ex.market(sym)
    ct_val = _f(market.get('contractSize', 1.0))
    nocional_total = CONTRACT_AMOUNT * ct_val * current_close
    log.info(f"Tamaño de orden: {CONTRACT_AMOUNT} contratos (~{nocional_total:.2f} USDC nocionales)")

    if not check_existing_positions(ex, sym):
        if e4_val > e21_val:
            log.info(f"Condición alcista (EMA 4 > 21). Abriendo LONG de {CONTRACT_AMOUNT} contratos...")
            execute_trade_with_sl_tp(ex, sym, 'buy', CONTRACT_AMOUNT, current_close, e4_val, e21_val)
        elif e4_val < e21_val:
            log.info(f"Condición bajista (EMA 4 < 21). Abriendo SHORT de {CONTRACT_AMOUNT} contratos...")
            execute_trade_with_sl_tp(ex, sym, 'sell', CONTRACT_AMOUNT, current_close, e4_val, e21_val)
    else:
        log.info("Posición activa detectada en este instrumento. Omitiendo nueva entrada.")

    log.info("Ciclo único completado con éxito. Desconectando.")

if __name__ == '__main__':
    main()
