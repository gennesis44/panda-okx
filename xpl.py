#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# xpl.py — Nave Autónoma XPL · Ley de la Flota (Carbono)
#   Operaciones sobre XPL estrictamente en USDC (USDT y EUR prohibidos).
#   Protección fail-closed: si falta configuración o credenciales, se detiene ordenadamente.

import os
import sys
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

BASE_ASSET = 'XPL'
TIMEFRAME = '30m'
EMA_FAST = 3
EMA_SLOW = 21
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
        
        # Filtro estricto: la moneda base debe ser XPL, el mercado activo y liquida/cotiza en USDC
        if m.get('base') == BASE_ASSET and m.get('active'):
            if settle == 'USDC' or quote == 'USDC':
                return m['symbol']
    return None

def main():
    log.info("Iniciando ciclo de XPL (X-Perp / Swap USDC)...")
    ex = build_exchange()
    sym = pick_future(ex)
    
    if not sym:
        log.info("XPL: No se encontró un instrumento activo con liquidación/cotización en USDC. Saliendo a puerto.")
        return

    log.info(f"Instrumento XPL detectado: {sym}")
    ohlcv = ex.fetch_ohlcv(sym, TIMEFRAME, limit=50)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    e3 = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
    e21 = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()
    
    last = len(df) - 1
    sep = abs(e3.iloc[last] - e21.iloc[last]) / e21.iloc[last]
    estado = "ALZA" if e3.iloc[last] > e21.iloc[last] else "BAJA"
    
    log.info(f"XPL {sym} {TIMEFRAME} | EMA{EMA_FAST}/{EMA_SLOW}: {e3.iloc[last]:.4f}/{e21.iloc[last]:.4f} | sep: {sep*100:.2f}% | estado: {estado}")
    log.info("Ciclo de XPL completado. De vuelta a puerto.")

if __name__ == '__main__':
    main()
