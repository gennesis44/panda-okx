#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# doge-go.py — Backtest DOGE multi-timeframe (30m + 1h)
# Ahorro de energía: solo timeframes medios/altos
# Estilo flota Carbon | USDT vetado | my.okx.com

import time
import logging
import ccxt
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("doge-go")

# ==================== CONFIG ====================
BASE_ASSET = "DOGE"
HOST = "https://my.okx.com"
FEE_RT = 0.0014
MAX_BARS = 2000                # suficiente para 30m y 1h

# Timeframes a probar
TIMEFRAMES = ["30m", "1h"]

# Configuraciones de EMAs + candado
CONFIGS = [
    {"name": "3/21 + 0.15%", "fast": 3, "slow": 21, "sep": 0.0015, "sl_long": 0.020, "tp_long": 0.040, "sl_short": 0.030, "tp_short": 0.040},
    {"name": "3/21 + 0.12%", "fast": 3, "slow": 21, "sep": 0.0012, "sl_long": 0.020, "tp_long": 0.040, "sl_short": 0.030, "tp_short": 0.040},
    {"name": "3/15 + 0.15%", "fast": 3, "slow": 15, "sep": 0.0015, "sl_long": 0.018, "tp_long": 0.035, "sl_short": 0.025, "tp_short": 0.040},
    {"name": "5/21 + 0.15%", "fast": 5, "slow": 21, "sep": 0.0015, "sl_long": 0.020, "tp_long": 0.040, "sl_short": 0.030, "tp_short": 0.040},
    {"name": "3/21 SL apretado", "fast": 3, "slow": 21, "sep": 0.0015, "sl_long": 0.015, "tp_long": 0.030, "sl_short": 0.025, "tp_short": 0.035},
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
    candidates = []
    for m in exchange.markets.values():
        if m.get("base") == BASE_ASSET and m.get("active"):
            settle = str(m.get("settle") or "").upper()
            if settle == "USD":
                exp = int((m.get("info") or {}).get("expTime") or 0)
                candidates.append((exp, m["symbol"], bool(m.get("future"))))
    if not candidates:
        raise RuntimeError("No se encontró DOGE/USD")
    futures = [c for c in candidates if c[2]]
    if futures:
        futures.sort(reverse=True)
        return futures[0][1]
    return candidates[0][1]

def fetch_ohlcv(symbol, timeframe, max_bars=2000):
    log.info(f"Descargando {timeframe}...")
    all_candles = []
    limit = 300
    tf_minutes = {"30m": 30, "1h": 60}.get(timeframe, 30)

    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    all_candles = ohlcv
    time.sleep(0.3)

    while len(all_candles) < max_bars:
        oldest_ts = all_candles[0][0]
        since = oldest_ts - (limit * tf_minutes * 60 * 1000)
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except Exception as e:
            log.warning(f"Error fetch {timeframe}: {e}")
            break
        if not ohlcv:
            break
        new_candles = [c for c in ohlcv if c[0] < oldest_ts]
        if not new_candles:
            break
        all_candles = new_candles + all_candles
        time.sleep(0.3)
        if len(ohlcv) < 100:
            break

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    df = df.tail(max_bars).reset_index(drop=True)
    log.info(f"  → {len(df)} velas {timeframe}")
    return df

def run_backtest(df, config):
    df = df.copy()
    df["ema_fast"] = ema(df["close"], config["fast"])
    df["ema_slow"] = ema(df["close"], config["slow"])
    df["sep"] = (df["ema_fast"] - df["ema_slow"]).abs() / df["ema_slow"]

    trades = []
    position = None
    entry_price = sl_price = tp_price = 0.0

    start_i = max(config["slow"] + 10, 30)

    for i in range(start_i, len(df) - 1):
        prev_fast = df["ema_fast"].iloc[i-1]
        prev_slow = df["ema_slow"].iloc[i-1]
        curr_fast = df["ema_fast"].iloc[i]
        curr_slow = df["ema_slow"].iloc[i]
        sep = df["sep"].iloc[i]

        cross_up = prev_fast <= prev_slow and curr_fast > curr_slow and sep >= config["sep"]
        cross_down = prev_fast >= prev_slow and curr_fast < curr_slow and sep >= config["sep"]

        row = df.iloc[i]

        if position:
            exit_price = None
            if position == "LONG":
                if row["low"] <= sl_price:
                    exit_price = sl_price
                elif row["high"] >= tp_price:
                    exit_price = tp_price
            else:
                if row["high"] >= sl_price:
                    exit_price = sl_price
                elif row["low"] <= tp_price:
                    exit_price = tp_price

            if exit_price is not None:
                pnl = (exit_price / entry_price - 1) if position == "LONG" else (entry_price / exit_price - 1)
                trades.append(pnl - FEE_RT)
                position = None

        if position is None and (cross_up or cross_down):
            position = "LONG" if cross_up else "SHORT"
            entry_price = df["open"].iloc[i+1]
            if position == "LONG":
                sl_price = entry_price * (1 - config["sl_long"])
                tp_price = entry_price * (1 + config["tp_long"])
            else:
                sl_price = entry_price * (1 + config["sl_short"])
                tp_price = entry_price * (1 - config["tp_short"])

    if not trades:
        return {"trades": 0}

    pnls = np.array(trades)
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    winrate = len(wins) / len(pnls) * 100
    total_return = pnls.sum() * 100
    pf = abs(wins.sum() / losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float("inf")
    equity = np.cumsum(pnls)
    max_dd = (np.maximum.accumulate(equity) - equity).max() * 100 if len(equity) > 0 else 0
    expectancy = (winrate/100 * wins.mean() + (1 - winrate/100) * losses.mean()) * 100 if len(pnls) else 0

    return {
        "trades": len(trades),
        "winrate": winrate,
        "total_return": total_return,
        "pf": pf,
        "max_dd": max_dd,
        "expectancy": expectancy
    }

def main():
    log.info("=== DOGE Backtest 30m + 1h (ahorro de energía) ===")
    symbol = pick_symbol()
    log.info(f"Instrumento: {symbol}")

    all_results = []

    for tf in TIMEFRAMES:
        df = fetch_ohlcv(symbol, tf, max_bars=MAX_BARS)
        for config in CONFIGS:
            res = run_backtest(df, config)
            res["tf"] = tf
            res["name"] = config["name"]
            all_results.append(res)
            log.info(f"  {tf} | {config['name']} → {res['trades']} trades")

    # Mostrar resultados agrupados por timeframe
    print("\n" + "="*110)
    print(f"{'TF':<6} {'CONFIGURACIÓN':<25} {'Trades':>7} {'WR%':>7} {'Retorno%':>10} {'PF':>7} {'MaxDD%':>8} {'Expect%':>8}")
    print("="*110)

    for tf in TIMEFRAMES:
        print(f"\n--- {tf.upper()} ---")
        for r in all_results:
            if r["tf"] != tf:
                continue
            if r["trades"] == 0:
                print(f"{r['tf']:<6} {r['name']:<25} {0:>7}")
            else:
                print(f"{r['tf']:<6} {r['name']:<25} {r['trades']:>7} {r['winrate']:>6.1f}% "
                      f"{r['total_return']:>9.2f}% {r['pf']:>6.2f} {r['max_dd']:>7.2f}% {r['expectancy']:>7.3f}%")

    print("="*110)
    print("Fee RT = 0.14% | Solo velas cerradas | Entrada en apertura siguiente vela")

if __name__ == "__main__":
    main()
