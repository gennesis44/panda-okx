#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
radar_flota.py - Radar de Flota Multi-Activo (OKX Perpetuos)
Monitorea múltiples pares, temporalidades y parámetros configurados via RADAR_SHIPS.
"""

import logging
import os
import sys
import time

import ccxt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("radar")

HOSTNAME = os.getenv("OKX_HOSTNAME", "my.okx.com")
RADAR_SHIPS_ENV = os.getenv("RADAR_SHIPS", "ADA-USDT-SWAP:15m:0.15,INJ-USDT-SWAP:1h:0.17")
N_CANDLES = int(os.getenv("RADAR_N_CANDLES", "40"))


def build_exchange():
    key = os.getenv("OKX_API_KEY", "")
    sec = os.getenv("OKX_SECRET_KEY", "") or os.getenv("OKX_SECRET", "")
    pwd = os.getenv("OKX_PASSWORD", "")
    cfg = {
        "enableRateLimit": True,
        "hostname": HOSTNAME,
        "options": {"defaultType": "swap"},
    }
    if key and sec and pwd:
        cfg.update({"apiKey": key, "secret": sec, "password": pwd})
    ex = ccxt.okx(cfg)
    ex.set_sandbox_mode(False)
    return ex


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def parse_ships():
    ships = []
    for item in RADAR_SHIPS_ENV.split(","):
        parts = item.strip().split(":")
        if len(parts) >= 2:
            symbol = parts[0].strip()
            tf = parts[1].strip()
            param = float(parts[2].strip()) if len(parts) > 2 else 0.0
            ships.append((symbol, tf, param))
    return ships


def main():
    ex = build_exchange()
    try:
        ex.load_markets()
    except Exception as e:
        log.error("Error cargando mercados: %s", str(e)[:200])
        sys.exit(1)

    ships = parse_ships()
    log.info("Iniciando radar de flota | Monitoreando %d naves", len(ships))

    for symbol, tf, param in ships:
        try:
            ohlcv = ex.fetch_ohlcv(symbol, tf, limit=N_CANDLES)
            if not ohlcv or len(ohlcv) < 25:
                log.warning("Poco o ningún dato para %s en %s", symbol, tf)
                continue
            
            closes = [c[4] for c in ohlcv]
            fast = ema(closes, 3)
            slow = ema(closes, 21)
            
            f1, s1 = fast[-1], slow[-1]
            f0, s0 = fast[-2], slow[-2]
            close_price = closes[-1]
            
            trend = "ALCISTA" if f1 > s1 else "BAJISTA"
            cruce = "SIN_CRUCE"
            if f0 <= s0 and f1 > s1:
                cruce = "CRUCE_LONG"
            elif f0 >= s0 and f1 < s1:
                cruce = "CRUCE_SHORT"

            log.info(
                "Nave [%s | %s] | Precio=%.4f | EMA3=%.4f | EMA21=%.4f | Tendencia=%s | Señal=%s",
                symbol, tf, close_price, f1, s1, trend, cruce
            )
        except Exception as e:
            log.warning("Fallo al escanear nave %s (%s): %s", symbol, tf, str(e)[:150])


if __name__ == "__main__":
    main()
