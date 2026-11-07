#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# doge-go.py — Extractor + Inspector de señal DOGE
# Solo muestra las 3 últimas velas CERRADAS
# Calcula EMA3 / EMA21 + separación
# Host: my.okx.com | USDT vetado | contratos mínimos = 1

import os
import time
import logging
from datetime import datetime, timezone

import ccxt
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("doge-go")

# ==================== CONFIG ====================
BASE_ASSET = "DOGE"
TIMEFRAME = os.getenv("DOGE_TF", "5m")          # por defecto 5m
LIMIT_FETCH = 80                               # suficiente para EMA21 estable
AMOUNT = 1                                     # mínimo de contratos
EMA_FAST = 3
EMA_SLOW = 21
SEP_LOCK = 0.0015                              # 0.15% (candado actual)
HOST = "https://my.okx.com"

exchange = ccxt.okx({
    "apiKey":    os.getenv("OKX_API_KEY", ""),
    "secret":    os.getenv("OKX_SECRET_KEY", ""),
    "password":  os.getenv("OKX_PASSWORD", ""),
    "enableRateLimit": True,
    "options":   {"defaultType": "swap"},
    "urls":      {"api": {"rest": HOST}},
})

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

def _is_forbidden(symbol):
    settle = str(exchange.market(symbol).get("settle") or "").upper()
    return settle != "USD"

def pick_instrument():
    exchange.load_markets()
    # Futuros USD de vencimiento más lejano
    futuros = []
    for m in exchange.markets.values():
        if (m.get("base") == BASE_ASSET and m.get("future") and m.get("active")
                and not _is_forbidden(m["symbol"])):
            exp = int((m.get("info") or {}).get("expTime") or 0)
            futuros.append((exp, m["symbol"]))
    if futuros:
        futuros.sort(reverse=True)
        return futuros[0][1]

    # Fallback SWAP USD
    for m in exchange.markets.values():
        if (m.get("base") == BASE_ASSET and m.get("active")
                and (m.get("swap") or (m.get("info") or {}).get("instType") == "SWAP")
                and not _is_forbidden(m["symbol"])):
            return m["symbol"]
    raise RuntimeError("No hay instrumento DOGE/USD disponible (USDT vetado)")

def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def get_closed_candles(symbol, timeframe, limit=80):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])

    now_ms = exchange.milliseconds()
    tf_ms = {
        "1m": 60_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000
    }.get(timeframe, 300_000)

    # Solo velas cerradas
    df = df[df["timestamp"] + tf_ms <= now_ms].reset_index(drop=True)
    return df

def main():
    log.info(f"doge-go.py | TF={TIMEFRAME} | EMA {EMA_FAST}/{EMA_SLOW} | candado {SEP_LOCK*100:.2f}%")
    log.info(f"Host: {HOST} | USDT vetado | contratos mínimos = {AMOUNT}")

    try:
        symbol = pick_instrument()
        log.info(f"Instrumento: {symbol}")

        market = exchange.market(symbol)
        ctval = float(market.get("contractSize") or (market.get("info") or {}).get("ctVal") or 1)
        log.info(f"1 contrato = {ctval} {BASE_ASSET} | settle = {market.get('settle')}")

        # Traemos historia suficiente para EMA
        df = get_closed_candles(symbol, TIMEFRAME, limit=LIMIT_FETCH)
        if len(df) < EMA_SLOW + 5:
            raise RuntimeError(f"Pocas velas cerradas: {len(df)}")

        # Calculamos EMAs
        df["ema3"] = ema(df["close"], EMA_FAST)
        df["ema21"] = ema(df["close"], EMA_SLOW)
        df["sep"] = (df["ema3"] - df["ema21"]).abs() / df["ema21"]
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

        # Nos quedamos solo con las 3 últimas cerradas
        last3 = df.tail(3).copy()

        print("\n" + "="*90)
        print(f"ÚLTIMAS 3 VELAS CERRADAS  →  {symbol}  |  {TIMEFRAME}")
        print("="*90)
        print(last3[["datetime", "open", "high", "low", "close", "ema3", "ema21", "sep"]].to_string(
            index=False,
            formatters={
                "ema3": "{:.6f}".format,
                "ema21": "{:.6f}".format,
                "sep": "{:.4%}".format
            }
        ))
        print("="*90)

        # Análisis rápido de la última vela
        last = last3.iloc[-1]
        prev = last3.iloc[-2]

        cross_up = prev["ema3"] <= prev["ema21"] and last["ema3"] > last["ema21"]
        cross_down = prev["ema3"] >= prev["ema21"] and last["ema3"] < last["ema21"]
        sep_ok = last["sep"] >= SEP_LOCK

        print("\nINSPECCIÓN DE SEÑAL (última vela cerrada):")
        print(f"  EMA3      : {last['ema3']:.6f}")
        print(f"  EMA21     : {last['ema21']:.6f}")
        print(f"  Separación: {last['sep']*100:.3f}%  {'✅' if sep_ok else '❌'} (candado {SEP_LOCK*100:.2f}%)")
        print(f"  Cruce UP  : {'✅ SÍ' if cross_up else '—'}")
        print(f"  Cruce DOWN: {'✅ SÍ' if cross_down else '—'}")

        if (cross_up or cross_down) and sep_ok:
            print(f"\n🟢 SEÑAL VÁLIDA → {'LONG' if cross_up else 'SHORT'}")
        else:
            print("\n⚪ Sin señal válida en la última vela")

        # Guardar
        out_file = f"doge-{TIMEFRAME}-last3.jsonl"
        last3.to_json(out_file, orient="records", lines=True, force_ascii=False)
        log.info(f"Datos guardados en: {out_file}")

    except Exception as e:
        log.error(f"Error: {e}")
        raise

if __name__ == "__main__":
    main()
