#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# doge-go.py — EL PERRO v4.0 · EMA 5/21 · 1H · Max $5
# Listo para producción
# Host: my.okx.com | USDT vetado | fail-closed

import os
import time
import logging
import ccxt
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("doge-go")

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

# ==================== CONFIGURACIÓN PRODUCCIÓN ====================
BASE_ASSET = "DOGE"
TIMEFRAME = "1h"
EMA_FAST = 5
EMA_SLOW = 21
SEP_LOCK = 0.0015                  # 0.15%

MAX_NOTIONAL_USD = 5.0             # Máximo 5 dólares
AMOUNT = 7                         # ≈ $5 (1 contrato ≈ $0.71)

SL_LONG = 0.020
TP_LONG = 0.040
SL_SHORT = 0.030
TP_SHORT = 0.040

TD_MODE = "cross"
LEVERAGE = 1
COOLDOWN_MIN = 60
TEST_MODE = os.getenv("TEST_MODE") == "1"
SINGLE_CYCLE = os.getenv("SINGLE_CYCLE") == "1"
SIGNAL_WINDOW = (-2, -3, -4)
WARMUP = 40
TF_MS = {"1h": 3_600_000}

HOST = "https://my.okx.com"

exchange = ccxt.okx({
    "apiKey":    os.getenv("OKX_API_KEY", ""),
    "secret":    os.getenv("OKX_SECRET_KEY", ""),
    "password":  os.getenv("OKX_PASSWORD", ""),
    "enableRateLimit": True,
    "options":   {"defaultType": "swap"},
    "urls":      {"api": {"rest": HOST}},
})

# ==================== INSTRUMENTO (solo USD) ====================
def _is_forbidden(symbol):
    settle = str(exchange.market(symbol).get("settle") or "").upper()
    return settle != "USD"

def pick_future():
    exchange.load_markets()
    candidatos = []
    for m in exchange.markets.values():
        if (m.get("base") == BASE_ASSET and m.get("future") and m.get("active")
                and not _is_forbidden(m["symbol"])):
            exp = int((m.get("info") or {}).get("expTime") or 0)
            candidatos.append((exp, m["symbol"]))
    if candidatos:
        candidatos.sort(reverse=True)
        return candidatos[0][1]
    return None

def pick_swap_usd():
    exchange.load_markets()
    for m in exchange.markets.values():
        if (m.get("base") == BASE_ASSET and m.get("active")
                and (m.get("swap") or (m.get("info") or {}).get("instType") == "SWAP")
                and not _is_forbidden(m["symbol"])):
            return m["symbol"]
    return None

def resolve_symbol():
    exchange.load_markets()
    try:
        for p in exchange.fetch_positions():
            if abs(_f(p.get("contracts"))) > 0 and (p.get("symbol") or "").startswith(BASE_ASSET + "/"):
                if _is_forbidden(p["symbol"]):
                    raise RuntimeError("Posición en instrumento no-USD prohibido")
                log.info(f"Instrumento (posición abierta): {p['symbol']}")
                return p["symbol"]
    except Exception as e:
        log.warning(f"Error leyendo posiciones: {e}")

    sym = pick_future()
    if sym:
        log.info(f"Instrumento: {sym} (XPERP)")
        return sym
    sym = pick_swap_usd()
    if sym:
        log.warning(f"Instrumento: {sym} (SWAP-USD)")
        return sym
    raise RuntimeError("No hay DOGE/USD disponible")

def _contract_meta(symbol, price=None):
    market = exchange.market(symbol)
    info = market.get("info") or {}
    ctval = float(market.get("contractSize") or info.get("ctVal") or 1)
    ccy = str(info.get("ctValCcy") or BASE_ASSET).upper()
    if price is None:
        price = _f(exchange.fetch_ticker(symbol).get("last"))
    usd = ctval if ccy in ("USD", "USDC") else ctval * price
    return ctval, ccy, usd

# ==================== INDICADORES ====================
def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def fetch_data(symbol, timeframe, limit=100):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])

# ==================== POSICIÓN ====================
def get_open_position(symbol):
    try:
        for pos in exchange.fetch_positions([symbol]):
            if pos.get("symbol") == symbol and abs(_f(pos.get("contracts"))) > 0:
                return pos
        inst = exchange.market(symbol)["id"]
        for ot in ("oco", "conditional"):
            resp = exchange.private_get_trade_orders_algo_pending({"instId": inst, "ordType": ot})
            if (resp or {}).get("data"):
                return {"symbol": symbol, "contracts": 1, "side": "desconocido"}
        return None
    except Exception as e:
        log.error(f"Posiciones ilegibles: {e} — BLOQUEO")
        return {"symbol": symbol, "contracts": 1, "side": "ilegible"}

# ==================== ORDEN ====================
def _cl_order_id(candle_ts):
    return f"DOGE{int(candle_ts)}"

def candle_already_traded(symbol, candle_ts):
    try:
        inst = exchange.market(symbol)["id"]
        resp = exchange.private_get_trade_order({"instId": inst, "clOrdId": _cl_order_id(candle_ts)})
        data = resp.get("data") or []
        return bool(data and str(data[0].get("sCode", "0")) == "0")
    except Exception:
        return True  # fail-closed

def execute_order(side, symbol, ref_price, amount, candle_ts):
    ctval, ccy, usd = _contract_meta(symbol, ref_price)
    nocional = amount * usd
    log.info(f"Nocional: {amount} ct × {ctval} {ccy} ≈ ${nocional:.2f}")

    if nocional > MAX_NOTIONAL_USD + 0.3:
        raise RuntimeError(f"GUARDIA: nocional ${nocional:.2f} > máximo ${MAX_NOTIONAL_USD}")

    if side == "LONG":
        oside = "buy"
        sl_raw = ref_price * (1 - SL_LONG)
        tp_raw = ref_price * (1 + TP_LONG)
    else:
        oside = "sell"
        sl_raw = ref_price * (1 + SL_SHORT)
        tp_raw = ref_price * (1 - TP_SHORT)

    sl = exchange.price_to_precision(symbol, sl_raw)
    tp = exchange.price_to_precision(symbol, tp_raw)
    sz = exchange.amount_to_precision(symbol, amount)

    req = {
        "instId": exchange.market(symbol)["id"],
        "tdMode": TD_MODE,
        "side": oside,
        "ordType": "optimal_limit_ioc",
        "sz": sz,
        "clOrdId": _cl_order_id(candle_ts),
        "attachAlgoOrds": [{
            "tpTriggerPx": tp, "tpOrdPx": "-1",
            "slTriggerPx": sl, "slOrdPx": "-1",
        }],
    }
    resp = exchange.private_post_trade_order(req)
    data = resp.get("data") or []
    d0 = data[0] if data else {}
    if str(d0.get("sCode", "1")) != "0":
        raise Exception(f"OKX rechazó: {d0.get('sMsg')}")
    log.info(f"DOGE {side} ejecutada | SL {sl} | TP {tp}")
    return True

# ==================== COOLDOWN ====================
def cooldown_active(symbol):
    try:
        inst = exchange.market(symbol)["id"]
        hist = exchange.privateGetTradeFillsHistory({
            "instType": "FUTURES",
            "instId": inst,
            "limit": "5"
        })
        for fill in (hist.get("data") or []):
            if str(fill.get("reduceOnly", "0")) == "true":
                age_min = (time.time() * 1000 - int(fill.get("ts") or 0)) / 60000
                if _f(fill.get("fillPnl")) < 0 and age_min < COOLDOWN_MIN:
                    return True
        return False
    except Exception:
        return True

# ==================== SEÑAL EMA 5/21 ====================
def evaluate_signal(symbol):
    df = fetch_data(symbol, TIMEFRAME, limit=100)
    if len(df) < WARMUP + 5:
        return None, None, None

    now_ms = int(time.time() * 1000)
    if int(df["timestamp"].iloc[-1]) + TF_MS[TIMEFRAME] > now_ms:
        df = df.iloc[:-1].reset_index(drop=True)

    e_fast = ema(df["close"], EMA_FAST)
    e_slow = ema(df["close"], EMA_SLOW)
    price = _f(exchange.fetch_ticker(symbol).get("last")) or df["close"].iloc[-1]

    log.info(f"Precio: {price:.5f} | EMA5/21: {e_fast.iloc[-2]:.5f}/{e_slow.iloc[-2]:.5f}")

    for i_curr in SIGNAL_WINDOW:
        i_prev = i_curr - 1
        idx = len(df) + i_curr
        if abs(i_prev) > len(df) - 1 or idx < WARMUP:
            continue

        sep = abs(e_fast.iloc[idx] - e_slow.iloc[idx]) / e_slow.iloc[idx]
        if sep < SEP_LOCK:
            continue

        up = e_fast.iloc[i_prev] <= e_slow.iloc[i_prev] and e_fast.iloc[idx] > e_slow.iloc[idx]
        down = e_fast.iloc[i_prev] >= e_slow.iloc[i_prev] and e_fast.iloc[idx] < e_slow.iloc[idx]

        candle_ts = int(df["timestamp"].iloc[i_curr])
        if up:
            log.info(f"Cruce ALCISTA EMA5/21 (sep {sep*100:.2f}%)")
            return "LONG", price, candle_ts
        if down:
            log.info(f"Cruce BAJISTA EMA5/21 (sep {sep*100:.2f}%)")
            return "SHORT", price, candle_ts

    log.info("Sin cruce EMA5/21 legal en la ventana")
    return None, None, None

# ==================== CICLO ====================
def run_cycle():
    symbol = resolve_symbol()

    try:
        exchange.set_position_mode(False, symbol)
    except Exception:
        pass

    try:
        exchange.set_leverage(LEVERAGE, symbol, params={"mgnMode": TD_MODE})
    except Exception as e:
        log.error(f"No se pudo fijar leverage: {e}")
        return

    if get_open_position(symbol):
        log.info("Posición abierta. Esperando SL/TP.")
        return

    if cooldown_active(symbol):
        log.info("Cooldown activo.")
        return

    signal, price, candle_ts = evaluate_signal(symbol)
    if not signal:
        return

    if candle_already_traded(symbol, candle_ts):
        log.info("Vela ya operada")
        return

    log.info(f"Señal {signal} | Abriendo {AMOUNT} contratos...")
    try:
        execute_order(signal, symbol, price, AMOUNT, candle_ts)
    except Exception as e:
        log.error(f"Entrada rechazada: {e}")

def main():
    log.info(f"DOGE-GO v4.0 | EMA {EMA_FAST}/{EMA_SLOW} | 1H | Max ${MAX_NOTIONAL_USD} | Candado 0.15%")
    if SINGLE_CYCLE or TEST_MODE:
        run_cycle()
    else:
        while True:
            try:
                run_cycle()
            except Exception as e:
                log.error(f"Error: {e}")
            time.sleep(600)

if __name__ == "__main__":
    main()
