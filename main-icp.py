#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
icp.py - Sombra ICP (OKX EEE, perpetuo margen USDC: ICP/USDC:USDC)

Entorno    : PRODUCCION, dominio EEE my.okx.com (USDT vetado en el EEE)
Estrategia : cruce EMA 3 / EMA 21 sobre velas de 1H CERRADAS
Ciclo      : cada 20 min (1200 s)
Salida     : calcula y registra SL/TP (ATR). NO envia ordenes.

Variables de entorno (opcionales; sin ellas corre en modo sombra publica):
    OKX_API_KEY, OKX_SECRET, OKX_PASSWORD
    OKX_HOSTNAME    (def. my.okx.com)
    ICP_SYMBOL      (def. ICP/USDC:USDC)
    ICP_RISK_PCT    (def. 1.0)  % del colateral arriesgado por operacion
    ICP_SL_ATR      (def. 1.5)  multiplo de ATR para el SL
    ICP_TP_ATR      (def. 3.0)  multiplo de ATR para el TP (RR = TP/SL)
"""

import logging
import os
import sys
import time

import ccxt

# ----------------------------------------------------------------- config
HOSTNAME = os.getenv("OKX_HOSTNAME", "my.okx.com")  # dominio EEE
SYMBOL = os.getenv("ICP_SYMBOL", "ICP/USDC:USDC")   # USDT vetado en el EEE
TIMEFRAME = "1h"
EMA_FAST = 3
EMA_SLOW = 21
ATR_PERIOD = 14
SL_ATR = float(os.getenv("ICP_SL_ATR", "1.5"))
TP_ATR = float(os.getenv("ICP_TP_ATR", "3.0"))
RISK_PCT = float(os.getenv("ICP_RISK_PCT", "1.0"))
CYCLE_SECONDS = 20 * 60
CANDLES = 200  # suficiente para estabilizar EMA21 y ATR14

TF_MS = 60 * 60 * 1000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("icp")


def fail(msg):
    """Registra el error y termina el proceso."""
    log.error(msg)
    sys.exit(1)


# ------------------------------------------------------------ indicadores
def ema(values, period):
    """EMA estandar, sembrada con la SMA de los primeros `period` valores.
    Devuelve lista alineada con `values` (None donde aun no hay dato)."""
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


def atr(highs, lows, closes, period):
    """ATR de Wilder optimizado y robusto."""
    if len(closes) <= period:
        return None
    
    trs = [max(highs[i] - lows[i], 
               abs(highs[i] - closes[i - 1]), 
               abs(lows[i] - closes[i - 1])) 
           for i in range(1, len(closes))]
    
    if len(trs) < period:
        return None
        
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
        
    return atr_val


# ---------------------------------------------------------------- exchange
def build_exchange():
    key = os.getenv("OKX_API_KEY", "")
    sec = os.getenv("OKX_SECRET", "")
    pwd = os.getenv("OKX_PASSWORD", "")
    has_creds = bool(key and sec and pwd)
    cfg = {
        "enableRateLimit": True,
        "hostname": HOSTNAME,  # my.okx.com (EEE)
        "options": {"defaultType": "swap"},
    }
    if has_creds:
        cfg.update({"apiKey": key, "secret": sec, "password": pwd})
    ex = ccxt.okx(cfg)
    ex.set_sandbox_mode(False)  # PRODUCCION: nunca demo
    return ex, has_creds


def validate_market(ex):
    """Exige un SWAP lineal (UM) margen USDC. Aborta si no existe o es USDT."""
    try:
        ex.load_markets()
    except Exception as e:
        fail("[ERROR] load_markets fallo en %s: %s — DETENIDO" % (HOSTNAME, str(e)[:200]))
        raise
    m = ex.markets.get(SYMBOL)
    if not m:
        fail("[ERROR] %s no existe en %s — DETENIDO" % (SYMBOL, HOSTNAME))
        raise SystemExit(1)
    if not (m.get("swap") and m.get("linear")):
        fail("[ERROR] %s no es un SWAP lineal (UM) — DETENIDO" % SYMBOL)
    if (m.get("settle") or "").upper() == "USDT":
        fail("[ERROR] %s liquida en USDT, vetado en el EEE — DETENIDO" % SYMBOL)
    if m.get("active") is False:
        fail("[ERROR] %s inactivo — DETENIDO" % SYMBOL)
    log.info("Mercado OK | %s | id=%s | settle=%s | contractSize=%s",
             SYMBOL, m.get("id"), m.get("settle"), m.get("contractSize"))
    return m


def auth_and_collateral(ex, has_creds):
    if not has_creds:
        return None
    try:
        bal = ex.fetch_balance()
    except Exception as e:
        log.warning("Lectura de cuenta fallo temporalmente: %s", str(e)[:150])
        return None

    def ff(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    data = (bal.get("info") or {}).get("data") or [{}]
    d0 = data[0] or {}

    # 1) totEq crudo de OKX (equidad total en USD)
    tot_eq = ff(d0.get("totEq")) or None

    # 2) Suma de eqUsd por divisa
    if not tot_eq:
        suma = sum(ff(d.get("eqUsd")) for d in (d0.get("details") or []))
        tot_eq = suma or None

    # 3) Ultimo recurso: USD/USDC
    if not tot_eq:
        suma = sum(ff((bal.get(c) or {}).get("total")) for c in ("USD", "USDC"))
        tot_eq = suma or None

    return tot_eq


# ------------------------------------------------------------------ logica
def fetch_closed_candles(ex):
    """Descarga velas 1H y descarta la vela en curso (no cerrada)."""
    ohlcv = ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=CANDLES)
    if not ohlcv:
        raise RuntimeError("fetch_ohlcv devolvio vacio")
    now_ms = int(time.time() * 1000)
    if ohlcv[-1][0] + TF_MS > now_ms:
        ohlcv = ohlcv[:-1]
    return ohlcv


def compute_levels(side, entry, atr_val):
    """SL/TP basados en ATR. side: 'LONG' o 'SHORT'."""
    sl_dist = SL_ATR * atr_val
    tp_dist = TP_ATR * atr_val
    if side == "LONG":
        return entry - sl_dist, entry + tp_dist, sl_dist
    return entry + sl_dist, entry - tp_dist, sl_dist


def analyze(ohlcv):
    """Devuelve dict con estado EMA, ATR y, si hay cruce en la ultima vela
    cerrada, la senal con SL/TP."""
    need = max(EMA_SLOW, ATR_PERIOD) + 2
    if len(ohlcv) < need:
        raise RuntimeError("Velas insuficientes: %d < %d" % (len(ohlcv), need))

    ts = [c[0] for c in ohlcv]
    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]
    closes = [c[4] for c in ohlcv]

    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)
    atr_val = atr(highs, lows, closes, ATR_PERIOD)

    f1, s1 = fast[-1], slow[-1]
    f0, s0 = fast[-2], slow[-2]
    if None in (f0, s0, f1, s1) or atr_val is None:
        raise RuntimeError("Indicadores sin datos suficientes")

    res = {
        "candle_ts": ts[-1],
        "close": closes[-1],
        "ema_fast": f1,
        "ema_slow": s1,
        "atr": atr_val,
        "trend": "ALCISTA" if f1 > s1 else "BAJISTA",
        "signal": None,
    }

    if f0 <= s0 and f1 > s1:
        res["signal"] = "LONG"
    elif f0 >= s0 and f1 < s1:
        res["signal"] = "SHORT"

    if res["signal"]:
        sl, tp, sl_dist = compute_levels(res["signal"], closes[-1], atr_val)
        res.update({"sl": sl, "tp": tp, "sl_dist": sl_dist})
    return res


def log_signal(r, collateral):
    side = r["signal"]
    entry = r["close"]
    rr = TP_ATR / SL_ATR
    log.info(
        "SENAL %s %s | entrada ref=%.4f | SL=%.4f | TP=%.4f | RR=1:%.2f | ATR=%.4f",
        side, SYMBOL, entry, r["sl"], r["tp"], rr, r["atr"],
    )
    if collateral:
        risk_usd = collateral * RISK_PCT / 100.0
        qty = risk_usd / r["sl_dist"]  # tamano en ICP
        log.info(
            "Sizing orientativo: riesgo %.1f%% = $%.2f | cantidad ~%.2f ICP (~$%.2f nocional)",
            RISK_PCT, risk_usd, qty, qty * entry,
        )


def run_cycle(ex, has_creds, last_signal_ts):
    collateral = auth_and_collateral(ex, has_creds)
    ohlcv = fetch_closed_candles(ex)
    r = analyze(ohlcv)

    log.info(
        "%s 1H | cierre=%.4f | EMA%d=%.4f EMA%d=%.4f | tendencia=%s | ATR=%.4f",
        SYMBOL, r["close"], EMA_FAST, r["ema_fast"], EMA_SLOW, r["ema_slow"],
        r["trend"], r["atr"],
    )

    if r["signal"] and r["candle_ts"] != last_signal_ts:
        log_signal(r, collateral)
        return r["candle_ts"]
    if r["signal"]:
        log.info("Senal %s ya reportada para esta vela", r["signal"])
    else:
        log.info("Sin cruce en la ultima vela cerrada")
    return last_signal_ts


def main():
    ex, has_creds = build_exchange()
    validate_market(ex)
    
    if has_creds:
        init_col = auth_and_collateral(ex, has_creds)
        try:
            host = str(ex.urls["api"]["private"]).replace("{hostname}", getattr(ex, "hostname", ""))
        except Exception:
            host = "?"
        if init_col is None:
            log.warning("Autenticacion OK | host=%s | NO se pudo determinar el colateral inicial", host)
        else:
            log.info("Autenticacion OK | host=%s | Colateral real (USD): ~%.2f", host, init_col)
    else:
        log.info("Sombra publica: sin credenciales — solo lectura de mercado")

    log.info(
        "Inicio PRODUCCION | host=%s | %s | EMA %d/%d | %s | ciclo=%d min | SL=%.1fxATR TP=%.1fxATR",
        HOSTNAME, SYMBOL, EMA_FAST, EMA_SLOW, TIMEFRAME, CYCLE_SECONDS // 60, SL_ATR, TP_ATR,
    )

    last_signal_ts = None
    errores_seguidos = 0
    while True:
        try:
            last_signal_ts = run_cycle(ex, has_creds, last_signal_ts)
            errores_seguidos = 0
        except KeyboardInterrupt:
            log.info("Detenido por el usuario")
            break
        except Exception as e:
            errores_seguidos += 1
            log.warning("Ciclo fallo (%d seguidos): %s", errores_seguidos, str(e)[:200])
            if errores_seguidos >= 5:
                fail("[ERROR] 5 ciclos seguidos fallando — DETENIDO")
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
