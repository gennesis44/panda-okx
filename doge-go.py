#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# doge-backtest.py — Backtest DOGE 5m (estilo flota Carbon)
# Solo velas cerradas + candado de separación + SL/TP asimétricos
# Host: my.okx.com | USDT vetado

import os
import time
import logging
from datetime import datetime, timezone, timedelta
import ccxt
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("doge-backtest")

# ==================== CONFIG GENERAL ====================
BASE_ASSET = "DOGE"
TIMEFRAME = "5m"
HOST = "https://my.okx.com"
FEE_RT = 0.0014          # 0.14% round-trip (realista OKX)
INITIAL_BALANCE = 1000   # USDT virtual
MAX_BARS = 3000          # ~10-11 días de 5m (ajustar si quieres más)

# Configuraciones a probar
CONFIGS = [
    {"name": "3/21 + 0.15% (baseline)", "fast": 3, "slow": 21, "sep": 0.0015, "sl_long": 0.020, "tp_long": 0.040, "sl_short": 0.030, "tp_short": 0.040},
    {"name": "3/21 + 0.20%",            "fast": 3, "slow": 21, "sep": 0.0020, "sl_long": 0.020, "tp_long": 0.040, "sl_short": 0.030, "tp_short": 0.040},
    {"name": "3/15 + 0.15%",            "fast": 3, "slow": 15, "sep": 0.0015, "sl_long": 0.015, "tp_long": 0.030, "sl_short": 0.020, "tp_short": 0.035},
    {"name": "5/21 + 0.15%",            "fast": 5, "slow": 21, "sep": 0.0015, "sl_long": 0.020, "tp_long": 0.040, "sl_short": 0.030, "tp_short": 0.040},
    {"name": "3/21 + 0.15% (SL apretado)", "fast": 3, "slow": 21, "sep": 0.0015, "sl_long": 0.015, "tp_long": 0.030, "sl_short": 0.025, "tp_short": 0.035},
]

exchange = ccxt.okx({
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
    "urls": {"api": {"rest": HOST}},
})

def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def pick_symbol():
    exchange.load_markets()
    # Preferimos el futuro USD de vencimiento más lejano
    candidates = []
    for m in exchange.markets.values():
        if m.get("base") == BASE_ASSET and m.get("active"):
            settle = str(m.get("settle") or "").upper()
            if settle == "USD":
                exp = int((m.get("info") or {}).get("expTime") or 0)
                candidates.append((exp, m["symbol"], m.get("future", False)))
    if not candidates:
        raise RuntimeError("No se encontró instrumento DOGE/USD")
    # Priorizamos futuros
    futures = [c for c in candidates if c[2]]
    if futures:
        futures.sort(reverse=True)
        return futures[0][1]
    return candidates[0][1]

def fetch_ohlcv(symbol, timeframe, max_bars=3000):
    log.info(f"Descargando {max_bars} velas de {symbol} {timeframe}...")
    all_candles = []
    limit = 300
    since = None

    while len(all_candles) < max_bars:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        if not ohlcv:
            break
        all_candles = ohlcv + all_candles
        since = ohlcv[0][0] - 1
        time.sleep(exchange.rateLimit / 1000)
        if len(ohlcv) < limit:
            break

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    df = df.tail(max_bars).reset_index(drop=True)
    log.info(f"Velas descargadas: {len(df)}")
    return df

def run_backtest(df, config):
    fast = config["fast"]
    slow = config["slow"]
    sep_lock = config["sep"]
    sl_long = config["sl_long"]
    tp_long = config["tp_long"]
    sl_short = config["sl_short"]
    tp_short = config["tp_short"]

    df = df.copy()
    df["ema_fast"] = ema(df["close"], fast)
    df["ema_slow"] = ema(df["close"], slow)
    df["sep"] = (df["ema_fast"] - df["ema_slow"]).abs() / df["ema_slow"]

    trades = []
    position = None  # None | "LONG" | "SHORT"
    entry_price = 0.0
    entry_time = None
    sl_price = 0.0
    tp_price = 0.0

    for i in range(slow + 5, len(df) - 1):
        # Señal solo con velas ya cerradas (miramos i como última cerrada)
        curr_fast = df["ema_fast"].iloc[i]
        curr_slow = df["ema_slow"].iloc[i]
        prev_fast = df["ema_fast"].iloc[i-1]
        prev_slow = df["ema_slow"].iloc[i-1]
        sep = df["sep"].iloc[i]

        cross_up = prev_fast <= prev_slow and curr_fast > curr_slow and sep >= sep_lock
        cross_down = prev_fast >= prev_slow and curr_fast < curr_slow and sep >= sep_lock

        row = df.iloc[i]
        next_open = df["open"].iloc[i+1]  # entrada en la apertura de la siguiente vela

        # Gestión de posición abierta
        if position:
            high = row["high"]
            low = row["low"]
            exit_price = None
            reason = None

            if position == "LONG":
                if low <= sl_price:
                    exit_price = sl_price
                    reason = "SL"
                elif high >= tp_price:
                    exit_price = tp_price
                    reason = "TP"
            else:  # SHORT
                if high >= sl_price:
                    exit_price = sl_price
                    reason = "SL"
                elif low <= tp_price:
                    exit_price = tp_price
                    reason = "TP"

            if exit_price:
                if position == "LONG":
                    pnl = (exit_price - entry_price) / entry_price
                else:
                    pnl = (entry_price - exit_price) / entry_price
                pnl_net = pnl - FEE_RT

                trades.append({
                    "dir": position,
                    "entry_time": entry_time,
                    "exit_time": row["timestamp"],
                    "entry": entry_price,
                    "exit": exit_price,
                    "reason": reason,
                    "pnl": pnl_net
                })
                position = None

        # Nueva entrada (solo si no hay posición)
        if position is None:
            if cross_up:
                position = "LONG"
                entry_price = next_open
                entry_time = df["timestamp"].iloc[i+1]
                sl_price = entry_price * (1 - sl_long)
                tp_price = entry_price * (1 + tp_long)
            elif cross_down:
                position = "SHORT"
                entry_price = next_open
                entry_time = df["timestamp"].iloc[i+1]
                sl_price = entry_price * (1 + sl_short)
                tp_price = entry_price * (1 - tp_short)

    # Resultados
    if not trades:
        return {"name": config["name"], "trades": 0}

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_return = np.sum(pnls)
    winrate = len(wins) / len(pnls) * 100
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")
    max_dd = 0
    equity = 0
    peak = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    return {
        "name": config["name"],
        "trades": len(trades),
        "winrate": winrate,
        "total_return": total_return * 100,
        "pf": pf,
        "avg_win": avg_win * 100,
        "avg_loss": avg_loss * 100,
        "max_dd": max_dd * 100,
        "expectancy": (winrate/100 * avg_win + (1 - winrate/100) * avg_loss) * 100
    }

def main():
    log.info("=== DOGE 5m Backtest (estilo flota) ===")
    symbol = pick_symbol()
    log.info(f"Instrumento: {symbol}")

    df = fetch_ohlcv(symbol, TIMEFRAME, max_bars=MAX_BARS)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

    results = []
    for config in CONFIGS:
        log.info(f"Probando: {config['name']}")
        res = run_backtest(df, config)
        results.append(res)

    print("\n" + "="*95)
    print(f"{'CONFIGURACIÓN':<35} {'Trades':>7} {'WR%':>7} {'Retorno%':>10} {'PF':>7} {'MaxDD%':>8} {'Expect%':>8}")
    print("="*95)
    for r in results:
        if r["trades"] == 0:
            print(f"{r['name']:<35} {0:>7}")
            continue
        print(f"{r['name']:<35} {r['trades']:>7} {r['winrate']:>6.1f}% {r['total_return']:>9.2f}% "
              f"{r['pf']:>6.2f} {r['max_dd']:>7.2f}% {r['expectancy']:>7.3f}%")
    print("="*95)
    print("Notas: Retorno y Expectancy son por operación promedio (no compuestos). Fee RT = 0.14%")
    print("Entrada en la apertura de la vela siguiente al cruce. Solo velas cerradas.")

if __name__ == "__main__":
    main()
