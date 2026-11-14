#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =====================================================================
# avax-go.py — NAVIO AVAX · LA FLOTA CARBONO · BUQUE: EL OCTAVO
# VERSIÓN PRODUCCIÓN CICLO ÚNICO (GitHub Actions v2.2)
# =====================================================================

import math
import os
import sys
import time
import logging
from datetime import datetime, timezone
from functools import wraps

try:
    import ccxt
except ImportError:
    ccxt = None

VERSION = "v2.2-1h-xperp-prod-avax"
TF_MS = {"5m": 300000, "15m": 900000, "30m": 1800000,
         "1h": 3600000, "2h": 7200000, "4h": 14400000, "6h": 21600000}
BASE_ASSET = "AVAX"

# ---------------- CONFIG ----------------
SHADOW = os.environ.get("SHADOW", "1") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
AVAX_TF = os.environ.get("AVAX_TF", "1h")
AVAX_WINDOW = tuple(int(x) for x in os.environ.get("AVAX_WINDOW", "-2,-3,-4").split(",") if x.strip())
SEP_LOCK = float(os.environ.get("AVAX_SEP_LOCK", "0.17"))
SL_MULT = float(os.environ.get("AVAX_SL_MULT", "1.2"))
SL_FLOOR = float(os.environ.get("AVAX_SL_FLOOR", "2.0"))
SL_CAP = float(os.environ.get("AVAX_SL_CAP", "2.0"))
TP_MULT = float(os.environ.get("AVAX_TP_MULT", "1.5"))
AVAX_NOTIONAL = float(os.environ.get("AVAX_NOTIONAL", "10.0"))
MAX_NOTIONAL = float(os.environ.get("AVAX_MAX_NOTIONAL", "15.0"))
LEVERAGE = int(float(os.environ.get("AVAX_LEVERAGE", "1")))
MARGIN_MODE = os.environ.get("AVAX_MARGIN_MODE", "isolated").strip().lower()
COOLDOWN_MIN = float(os.environ.get("COOLDOWN_MIN", "60"))
N_CANDLES = int(os.environ.get("AVAX_N_CANDLES", "200"))
AVAX_DEBUG = os.environ.get("AVAX_DEBUG", "0") == "1"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("avax")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fail(msg):
    log.error(msg)
    sys.exit(1)


def retry_api(retries=3, delay=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    log.warning("Intento %d/%d fallido en %s: %s. Reintentando en %ds...",
                                attempt, retries, func.__name__, str(e)[:120], delay * attempt)
                    time.sleep(delay * attempt)
            raise last_err
        return wrapper
    return decorator


def _fmt_contracts(c):
    if abs(c - round(c)) < 1e-9:
        return str(int(round(c)))
    s = ("%.4f" % c).rstrip("0").rstrip(".")
    return s or "0"


# ---------------- EXCHANGE ----------------
def build_exchange():
    if ccxt is None:
        fail("[ERROR] Falta ccxt: pip install ccxt")
    api_key = os.environ.get("OKX_API_KEY", "")
    secret = os.environ.get("OKX_SECRET_KEY", "")
    passwd = os.environ.get("OKX_PASSWORD", "")
    ex = ccxt.okx({
        "apiKey": api_key or None,
        "secret": secret or None,
        "password": passwd or None,
        "hostname": "my.okx.com",
        "enableRateLimit": True,
        "options": {"defaultType": "futures"},
    })
    ex.hostname = "my.okx.com"
    return ex, bool(api_key and secret and passwd)


@retry_api(retries=3, delay=3)
def pick_future(ex):
    ex.load_markets()
    cands = []
    for m in ex.markets.values():
        if m.get("base") != BASE_ASSET:
            continue
        if m.get("quote") != "USD":
            continue
        if m.get("settle") != "USD":
            continue
        if m.get("swap") or not m.get("future"):
            continue
        cands.append(m)
    if not cands:
        fail("[ERROR] Sin futuros AVAX/USD settle USD en OKX. DETENIDO.")
    best = max(cands, key=lambda m: int(m.get("expiry") or 0))
    return best


@retry_api(retries=3, delay=2)
def auth_and_collateral(ex, has_creds):
    if not has_creds:
        log.info("Sombra publica: sin credenciales — solo lectura")
        return None
    bal = ex.fetch_balance()
    tot_eq = None
    try:
        d0 = bal.get("info", {}).get("data", [{}])[0]
        if d0.get("totEq"):
            tot_eq = _f(d0["totEq"])
    except Exception:
        pass
    if not tot_eq:
        try:
            details = bal.get("info", {}).get("data", [{}])[0].get("details") or []
            suma = sum(_f(d.get("eqUsd")) for d in details)
            if suma > 0:
                tot_eq = suma
        except Exception:
            pass
    if tot_eq is None:
        fail("[ERROR] Bodega ilegible — DETENIDO")
    log.info("Autenticacion OK | Colateral verificado")
    return tot_eq


@retry_api(retries=3, delay=2)
def open_position_contracts(ex, sym):
    poss = ex.fetch_positions([sym])
    for p in poss:
        c = p.get("contracts")
        if c and abs(float(c)) > 0:
            return float(c)
    return 0.0


@retry_api(retries=3, delay=2)
def algo_pending(ex, inst_id):
    out = []
    for ot in ("oco", "conditional"):
        r = ex.privateGetTradeOrdersAlgoPending({"ordType": ot, "instId": inst_id})
        out.extend(r.get("data") or [])
    return out


def ema(vals, span):
    k = 2.0 / (span + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1.0 - k)
        out.append(e)
    return out


@retry_api(retries=3, delay=2)
def closed_candles(ex, sym, tf, n):
    if tf not in TF_MS:
        fail("[ERROR] marco temporal no soportado: %s" % tf)
    raw = ex.fetch_ohlcv(sym, tf, limit=n + 3)
    raw.sort(key=lambda c: c[0])
    tfms = TF_MS[tf]
    now = ex.milliseconds()
    closed = [c for c in raw if c[0] + tfms <= now]
    if len(closed) < 40:
        fail("[ERROR] velas cerradas insuficientes (%d)" % len(closed))
    return closed[-n:]


def compute_signal(candles):
    c = [float(x[4]) for x in candles]
    n = len(c)
    e3 = ema(c, 3)
    e21 = ema(c, 21)
    stance = [1 if e3[i] > e21[i] else -1 for i in range(n)]
    cross_idx, direction, window_off = None, 0, None
    for off in AVAX_WINDOW:
        i = n + off
        if i < 1 or i >= n:
            continue
        if stance[i] != stance[i - 1]:
            cross_idx, direction, window_off = i, stance[i], off
            break
    sig = {
        "px": c[-1], "e3": e3[-1], "e21": e21[-1], "stance": stance[-1],
        "cross_idx": cross_idx, "dir": direction, "window_off": window_off,
        "sep": None, "e21_cross": None, "bar_ts": None,
    }
    if cross_idx is not None:
        sig["sep"] = abs(e3[cross_idx] - e21[cross_idx]) / e21[cross_idx] * 100.0
        sig["e21_cross"] = e21[cross_idx]
        sig["bar_ts"] = int(candles[cross_idx][0])
    return sig


def px_decimals(market):
    p = (market.get("precision") or {}).get("price") or 0.001
    d, x = 0, float(p)
    while x < 1.0 and d < 10:
        x *= 10.0
        d += 1
    return d


def sl_tp(px, e21_cross, long_dir):
    dist = abs(px - e21_cross) * SL_MULT
    dist_pct = dist / px * 100.0
    if dist_pct < SL_FLOOR:
        dist = px * SL_FLOOR / 100.0
    elif dist_pct > SL_CAP:
        dist = px * SL_CAP / 100.0
    if long_dir:
        return px - dist, px + dist * TP_MULT, dist / px * 100.0
    return px + dist, px - dist * TP_MULT, dist / px * 100.0


@retry_api(retries=3, delay=2)
def already_traded(ex, inst_id, cl_ord_id):
    r = ex.privateGetTradeOrdersHistory({"instType": "FUTURES", "instId": inst_id, "limit": "20"})
    for o in (r.get("data") or []):
        if o.get("clOrdId") == cl_ord_id:
            return True
    return False


@retry_api(retries=3, delay=2)
def cooldown_active(ex, inst_id):
    now = ex.milliseconds()
    try:
        r = ex.privateGetAccountPositionsHistory({"instType": "FUTURES", "instId": inst_id, "limit": "10"})
        for rec in (r.get("data") or []):
            ft = int(_f(rec.get("uTime")) or _f(rec.get("closedTime")) or _f(rec.get("cTime")))
            pnl = _f(rec.get("realizedPnl")) if rec.get("realizedPnl") not in (None, "") else _f(rec.get("pnl"))
            if pnl < 0 and ft > 0 and (now - ft) < COOLDOWN_MIN * 60 * 1000:
                return True, pnl, ft
        return False, 0.0, 0
    except Exception:
        return False, 0.0, 0


@retry_api(retries=3, delay=2)
def pos_mode_check(ex):
    cfg = ex.privateGetAccountConfig()
    pos_mode = ((cfg.get("data") or [{}])[0]).get("posMode")
    if pos_mode != "net_mode":
        fail("[ERROR] Cuenta en %s — se requiere net_mode" % pos_mode)
    return pos_mode


def size_contracts(market, px):
    info = market.get("info") or {}
    csize = _f(market.get("contractSize") or info.get("ctVal"))
    lot = _f(info.get("lotSz")) or 1.0
    min_sz = _f(info.get("minSz")) or lot
    val_ct = csize * px
    steps = int(math.floor((AVAX_NOTIONAL / val_ct) / lot + 1e-9))
    n = max(min_sz, steps * lot)
    return round(n, 10), n * val_ct


@retry_api(retries=2, delay=2)
def win_rate_report(ex, inst_id):
    try:
        r = ex.privateGetAccountPositionsHistory({"instType": "FUTURES", "instId": inst_id, "limit": "50"})
        rows = []
        for rec in (r.get("data") or []):
            ts = int(_f(rec.get("uTime")) or _f(rec.get("closedTime")) or _f(rec.get("cTime")))
            gross = _f(rec.get("realizedPnl")) if rec.get("realizedPnl") not in (None, "") else _f(rec.get("pnl"))
            if ts > 0:
                rows.append((ts, gross))
        if rows:
            log.info("Win rate analizado correctamente (%d cierres)", len(rows))
    except Exception:
        pass


@retry_api(retries=3, delay=1)
def fire(ex, market, inst_id, sig, contracts, tot_eq):
    px = sig["px"]
    long_dir = sig["dir"] > 0
    side = "buy" if long_dir else "sell"
    sl, tp, dist_pct = sl_tp(px, sig["e21_cross"], long_dir)
    d = px_decimals(market)
    sl_s = ("%." + str(d) + "f") % sl
    tp_s = ("%." + str(d) + "f") % tp
    bar_ms = sig["bar_ts"]
    cl = ("AVAX%d" % bar_ms)[:32]

    body = {
        "instId": inst_id, "tdMode": MARGIN_MODE, "posSide": "net",
        "side": side, "ordType": "optimal_limit_ioc",
        "sz": _fmt_contracts(contracts), "clOrdId": cl,
        "attachAlgoOrds": [{
            "attachAlgoClOrdId": ("AVXA%d" % bar_ms)[:32],
            "slTriggerPx": sl_s, "slOrdPx": "-1",
            "tpTriggerPx": tp_s, "tpOrdPx": "-1",
        }],
    }
    r = ex.privatePostTradeOrder(body)
    if str(r.get("code")) != "0":
        fail("[ERROR] OKX rechazo orden: %s" % r.get("msg"))
    log.info("¡Orden ejecutada y blindada con éxito!")


def run_cycle():
    shadow = SHADOW or DRY_RUN
    log.info("Ejecutando ciclo NAVIO AVAX | TF: %s | Modo: %s", AVAX_TF, "SOMBRA" if shadow else "REAL")
    ex, has_creds = build_exchange()
    if not shadow and not has_creds:
        fail("[ERROR] Modo REAL sin credenciales.")
    
    market = pick_future(ex)
    inst_id = market["id"]
    sym = market["symbol"]
    tot_eq = auth_and_collateral(ex, has_creds)

    if has_creds:
        win_rate_report(ex, inst_id)
        pos_c = open_position_contracts(ex, sym)
        algos = algo_pending(ex, inst_id)
        if pos_c or algos:
            log.info("Nave ocupada (%s ct). Vigilando sin abrir nuevas posiciones.", _fmt_contracts(pos_c))
            return

    candles = closed_candles(ex, sym, AVAX_TF, N_CANDLES)
    sig = compute_signal(candles)
    log.info("Precio: %.4f | EMA3/21: %.4f/%.4f", sig["px"], sig["e3"], sig["e21"])

    if sig["cross_idx"] is None or sig["sep"] < SEP_LOCK:
        log.info("Sin cruce legal o separación insuficiente. Vigilando.")
        return

    contracts, est_notional = size_contracts(market, sig["px"])
    if shadow:
        log.info("[SOMBRA] Señal detectada — Simulación correcta, sin ejecución real.")
        return

    pos_mode_check(ex)
    active, _, _ = cooldown_active(ex, inst_id)
    if active:
        log.info("Cooldown activo. Vigilando.")
        return

    fire(ex, market, inst_id, sig, contracts, tot_eq)


def main():
    log.info("Iniciando NAVIO AVAX (Modo ciclo único)...")
    try:
        run_cycle()
        log.info("Ciclo finalizado con éxito. Cerrando proceso.")
        return 0
    except Exception as e:
        log.error("[CRITICAL ERROR] %s", str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
