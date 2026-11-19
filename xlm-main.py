#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =====================================================================
# xlm-main.py — NAVIO XLM · LA FLOTA CARBONO · BUQUE: LA ESTELAR
# 100% AUTONOMO: solo stdlib + ccxt (pip). NO importa ningun otro
# archivo del repo — navega solo en un repo recien creado.
# Casco: blindaje certificado del xpl v4.2 · Motor: EMA 3/21 · 1h
# Instrumento: XLM-USD XPERP (futuro fechado USD, vencimiento lejano)
#
# v1.0 — botadura desde el dique seco (decreto del capitan, 25-oct).
# PERITAJE FORENSE DEL MAR (60 dias, 1440 velas 1H reales OKX):
#   · rango diario mediano 5.40% | media 5.70% | p75 7.00% | max 13.88%
#   · 86 cruces EMA3/21 en 60d | sep mediana en el cruce 0.152%
#   · candado 0.15%: 43 legales de 86 (50%) — discrimina en la mediana
#   · estudio: PISO 1.5% -> PF 1.19, winrate 44%, avgR +0.10
#   · dist cruda 1.2x EMA21 mediana 0.68% — techo defensivo 3.5%
#
# AJUSTE FLOTA (Capitán): SINGLE_CYCLE explicito + privacidad colateral
#   + fix cooldown v2.1 (positions-history + fills-history fallback).
#   MAX_NOTIONAL $20 respetado como decreto específico XLM
#   (1 ct = 100 XLM ≈ $17 a precio actual — no cabe en $12 regla flota).
# NOTA TÉCNICA: "2 velas + forming" no aplica a XLM porque su SL es
#   porcentual (px × 1.5%-3.5%), no estructural. La vela en formación no aporta.
#
# BLINDAJE A BORDO (herencia xpl v4.2 + perro Ax + leccion Portaaviones):
#   · pick_future            -> reanclaje automatico al vencimiento mas lejano
#   · veto USDT/EUR/etc      -> solo settle USD (Ley Carbono), fail-closed
#   · optimal_limit_ioc      -> entrada anti-deslizamiento
#   · attachAlgoOrds         -> SL/TP viajan DENTRO de la orden de entrada
#   · clOrdId sello de vela  -> idempotencia (OKX rechaza duplicado 51016)
#   · candle_already_traded  -> consulta historial antes de disparar (fail-closed)
#   · anti-doble v4.2        -> posiciones + algos vivos = nave ocupada
#   · protection_confirmed   -> verifica SL/TP pendientes tras disparar
#   · emergency_close        -> si la coraza no aparece, cierre inmediato
#   · cooldown v2.1          -> calma 60 min tras perdida via
#     /api/v5/account/positions-history (realizedPnl). FAIL-CLOSED.
#   · cascada triple de colateral (leccion Portaaviones 15:35)
#
# VARIABLES DE ENTORNO (todas opcionales):
#   SHADOW=0             0 = REAL (default flota). 1 = sombra
#   DRY_RUN=1            freno de flota -> fuerza sombra
#   SINGLE_CYCLE=1       1 ciclo y salir (modo GitHub Actions)
#   XLM_TF=1h            marco temporal
#   XLM_CONTRACTS=0      0 = tamano AUTOMATICO por nocional; >0 = fijo
#   XLM_SEP_LOCK=0.15    candado de separacion %
#   XLM_SL_MULT=1.2      multiplicador distancia EMA21
#   XLM_SL_FLOOR=1.5     piso SL %
#   XLM_SL_CAP=3.5       techo SL %
#   XLM_TP_MULT=1.5      TP = x * SL
#   XLM_MAX_NOTIONAL=20  guardia de nocional $ (decreto XLM)
#   COOLDOWN_MIN=60      calma tras perdida
#   XLM_DEBUG=0          1 = muestra colateral real
# =====================================================================

import os
import sys
import time
import logging
import math
from datetime import datetime, timezone

try:
    import ccxt
except ImportError:
    ccxt = None

VERSION = "v1.1-flota"
TF_MS = {"5m": 300000, "15m": 900000, "30m": 1800000,
         "1h": 3600000, "2h": 7200000, "4h": 14400000, "6h": 21600000}

# ---------------- CONFIG ----------------
SHADOW = os.environ.get("SHADOW", "0") == "1"   # default REAL (cambio flota)
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
SINGLE_CYCLE = os.environ.get("SINGLE_CYCLE", "0") == "1"
XLM_TF = os.environ.get("XLM_TF", "1h")
XLM_CONTRACTS = float(os.environ.get("XLM_CONTRACTS", "0"))   # 0 = AUTO
TARGET_NOTIONAL = float(os.environ.get("XLM_NOTIONAL", "20.0"))
MAX_NOTIONAL = float(os.environ.get("XLM_MAX_NOTIONAL", "20.0"))   # decreto XLM
SEP_LOCK = float(os.environ.get("XLM_SEP_LOCK", "0.15"))
SL_MULT = float(os.environ.get("XLM_SL_MULT", "1.2"))
SL_FLOOR = float(os.environ.get("XLM_SL_FLOOR", "1.5"))
SL_CAP = float(os.environ.get("XLM_SL_CAP", "3.5"))
TP_MULT = float(os.environ.get("XLM_TP_MULT", "1.5"))
COOLDOWN_MIN = float(os.environ.get("COOLDOWN_MIN", "60"))
N_CANDLES = int(os.environ.get("XLM_N_CANDLES", "120"))
XLM_DEBUG = os.environ.get("XLM_DEBUG", "0") == "1"
MARGIN_MODE = "cross"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("xlm")


def fail(msg):
    log.error(msg)
    sys.exit(1)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


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


def pick_future(ex):
    ex.load_markets()
    cands = []
    for m in ex.markets.values():
        if m.get("base") != "XLM":
            continue
        if m.get("settle") != "USD":
            continue
        if m.get("swap") or not m.get("future"):
            continue
        cands.append(m)
    if not cands:
        fail("[ERROR] Sin futuros XLM/USD settle USD en OKX. DETENIDO (Ley Carbono).")
    best = max(cands, key=lambda m: int(m.get("expiry") or 0))
    if best.get("settle") != "USD":
        fail("[ERROR] Veto Ley Carbono: settle != USD. DETENIDO.")
    return best


def auth_and_collateral(ex, has_creds):
    if not has_creds:
        log.info("Sombra publica: sin credenciales — solo lectura de mercado")
        return None
    try:
        bal = ex.fetch_balance()
    except Exception as e:
        fail("[ERROR] Autenticacion/lectura de cuenta fallo: %s — DETENIDO" % str(e)[:200])

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
    if not tot_eq:
        try:
            suma = sum(_f((bal.get(c) or {}).get("total"))
                       for c in ("USD", "USDC", "USDT"))
            if suma > 0:
                tot_eq = suma
        except Exception:
            pass
    if tot_eq is None:
        fail("[ERROR] Bodega ilegible (totEq/eqUsd/bolsillos vacios) — DETENIDO (fail-closed)")
    try:
        host = str(ex.urls.get("api", {}).get("rest", "?"))
        if "{hostname}" in host:
            host = host.replace("{hostname}", getattr(ex, "hostname", "my.okx.com"))
    except Exception:
        host = "?"
    if XLM_DEBUG:
        log.info("Autenticacion OK | host=%s | Colateral real (USD): ~%.2f", host, tot_eq)
    else:
        log.info("Autenticacion OK | host=%s | Colateral: OK (XLM_DEBUG=1 para verlo)", host)
    return tot_eq


# ---------------- POSICION / ALGOS ----------------
def open_position_contracts(ex, sym):
    try:
        poss = ex.fetch_positions([sym])
    except Exception as e:
        fail("[ERROR] fetch_positions fallo: %s — DETENIDO (fail-closed)" % str(e)[:200])
    for p in poss:
        c = p.get("contracts")
        if c and abs(float(c)) > 0:
            return float(c)
    return 0.0


def algo_pending(ex, inst_id):
    out = []
    for ot in ("oco", "conditional"):
        try:
            r = ex.privateGetTradeOrdersAlgoPending({"ordType": ot, "instId": inst_id})
            out.extend(r.get("data") or [])
        except Exception as e:
            fail("[ERROR] consulta algo pending (%s) fallo: %s — DETENIDO (fail-closed)"
                 % (ot, str(e)[:200]))
    return out


# ---------------- SENAL ----------------
def ema(vals, span):
    k = 2.0 / (span + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1.0 - k)
        out.append(e)
    return out


def closed_candles(ex, sym, tf, n):
    if tf not in TF_MS:
        fail("[ERROR] marco temporal no soportado: %s" % tf)
    raw = ex.fetch_ohlcv(sym, tf, limit=n + 3)
    tfms = TF_MS[tf]
    now = ex.milliseconds()
    closed = [c for c in raw if c[0] + tfms <= now]
    if len(closed) < 40:
        fail("[ERROR] velas cerradas insuficientes (%d) — DETENIDO (fail-closed)" % len(closed))
    return closed[-n:]


def compute_signal(candles):
    c = [float(x[4]) for x in candles]
    n = len(c)
    e3 = ema(c, 3)
    e21 = ema(c, 21)
    stance = [1 if e3[i] > e21[i] else -1 for i in range(n)]
    cross_idx, direction = None, 0
    for i in range(max(1, n - 2), n):          # ventana: ultimas 2 cerradas
        if stance[i] != stance[i - 1]:
            cross_idx, direction = i, stance[i]
    sig = {
        "px": c[-1], "e3": e3[-1], "e21": e21[-1], "stance": stance[-1],
        "cross_idx": cross_idx, "dir": direction,
        "sep": None, "e21_cross": None, "bar_ts": None,
    }
    if cross_idx is not None:
        sig["sep"] = abs(e3[cross_idx] - e21[cross_idx]) / e21[cross_idx] * 100.0
        sig["e21_cross"] = e21[cross_idx]
        sig["bar_ts"] = int(candles[cross_idx][0])
    return sig


def px_decimals(market):
    p = (market.get("precision") or {}).get("price") or 0.0001
    d, x = 0, float(p)
    while x < 1.0 and d < 10:
        x *= 10.0
        d += 1
    return d


def sl_tp(px, e21_cross, long_dir):
    """SL = SL_MULT x dist EMA21 con piso/techo %; TP = TP_MULT x SL."""
    dist = abs(px - e21_cross) * SL_MULT
    dist_pct = dist / px * 100.0
    if dist_pct < SL_FLOOR:
        dist = px * SL_FLOOR / 100.0
    elif dist_pct > SL_CAP:
        dist = px * SL_CAP / 100.0
    if long_dir:
        return px - dist, px + dist * TP_MULT, dist / px * 100.0
    return px + dist, px - dist * TP_MULT, dist / px * 100.0


# ---------------- CÁLCULO DINÁMICO DE CONTRATOS ----------------
def size_contracts(market, px):
    """Tamano por nocional objetivo (decreto XLM: $20).
    1 ct = 100 XLM ≈ $17 a precio actual. Floor: nunca pasarse del tope.
    Respeta minSz y lotSz. Fail-closed: si ni 1 contrato cabe, NO zarpa."""
    info = market.get("info") or {}
    csize = _f(market.get("contractSize") or info.get("ctVal") or 0.0)
    if csize <= 0:
        fail("[ERROR] ctVal ilegible del mercado — DETENIDO (fail-closed)")
    val_ct = csize * px
    if val_ct <= 0:
        fail("[ERROR] valor por contrato ilegible — DETENIDO (fail-closed)")
    lot = _f(info.get("lotSz")) or 1.0
    min_sz = _f(info.get("minSz")) or lot
    if lot <= 0 or min_sz <= 0:
        fail("[ERROR] lotSz/minSz ilegibles de la API — DETENIDO (fail-closed)")
    steps = int(math.floor((TARGET_NOTIONAL / val_ct) / lot + 1e-9))
    n = steps * lot
    if n < min_sz:
        n = min_sz
    n = round(n, 10)
    notional = n * val_ct
    if notional > MAX_NOTIONAL:
        fail("[ERROR] tamano %s ct ~ $%.2f > guardia $%.2f — DETENIDO (fail-closed)"
             % (_fmt_contracts(n), notional, MAX_NOTIONAL))
    return n, notional


# ---------------- SEGURIDAD PRE-DISPARO ----------------
def already_traded(ex, inst_id, cl_ord_id):
    try:
        mk = (getattr(ex, "markets_by_id", {}) or {}).get(inst_id)
        if isinstance(mk, list):
            mk = mk[0] if mk else {}
        inst_type = ((mk or {}).get("info") or {}).get("instType") or "FUTURES"
        r = ex.privateGetTradeOrdersHistory(
            {"instType": inst_type, "instId": inst_id, "limit": "20"})
    except Exception as e:
        fail("[ERROR] historial de ordenes inaccesible: %s — DETENIDO (fail-closed)"
             % str(e)[:200])
    for o in (r.get("data") or []):
        if o.get("clOrdId") == cl_ord_id:
            return True
    return False


def cooldown_active(ex, inst_id):
    """FIX v2.1: usa /api/v5/account/positions-history (realizedPnl).
    Fallback /api/v5/trade/fills-history (fillPnl). FAIL-CLOSED."""
    now = ex.milliseconds()
    try:
        r = ex.privateGetAccountPositionsHistory(
            {"instType": "FUTURES", "instId": inst_id, "limit": "10"})
        for rec in (r.get("data") or []):
            ft = int(_f(rec.get("uTime")) or _f(rec.get("closedTime"))
                     or _f(rec.get("cTime")))
            pnl = (_f(rec.get("realizedPnl"))
                   if rec.get("realizedPnl") not in (None, "")
                   else _f(rec.get("pnl")))
            if pnl < 0 and ft > 0 and (now - ft) < COOLDOWN_MIN * 60 * 1000:
                return True, pnl, ft
        return False, 0.0, 0
    except Exception as e:
        log.warning("positions-history no abrio (%s) — probando fills-history",
                    str(e)[:120])
    try:
        r = ex.privateGetTradeFillsHistory(
            {"instType": "FUTURES", "instId": inst_id, "limit": "40"})
        for x in (r.get("data") or []):
            ts = int(_f(x.get("ts")) or _f(x.get("fillTime")))
            pnl = _f(x.get("fillPnl"))
            if pnl < 0 and ts > 0 and (now - ts) < COOLDOWN_MIN * 60 * 1000:
                return True, pnl, ts
        return False, 0.0, 0
    except Exception as e:
        log.warning("cooldown ilegible por ambas fuentes (%s) — BLOQUEO fail-closed",
                    str(e)[:120])
        return True, 0.0, ex.milliseconds()


# ---------------- DISPARO ----------------
def emergency_close(ex, inst_id, entry_side, contracts):
    body = {
        "instId": inst_id, "tdMode": MARGIN_MODE, "posSide": "net",
        "side": "sell" if entry_side == "buy" else "buy",
        "ordType": "market", "sz": _fmt_contracts(contracts),
        "reduceOnly": True, "clOrdId": ("XLME%d" % ex.milliseconds())[:32],
    }
    try:
        r = ex.privatePostTradeOrder(body)
        log.info("Cierre de emergencia enviado | code=%s", r.get("code"))
    except Exception as e:
        fail("[ERROR] cierre de emergencia fallo: %s — INTERVENCION MANUAL URGENTE"
             % str(e)[:200])


def fire(ex, market, inst_id, sig, contracts, tot_eq):
    px = sig["px"]
    long_dir = sig["dir"] > 0
    side = "buy" if long_dir else "sell"
    sl, tp, dist_pct = sl_tp(px, sig["e21_cross"], long_dir)
    d = px_decimals(market)
    sl_s = ("%." + str(d) + "f") % sl
    tp_s = ("%." + str(d) + "f") % tp
    bar_ms = sig["bar_ts"]
    cl = ("XLM%d" % bar_ms)[:32]

    csize = float(market.get("contractSize") or 1.0)
    notional = contracts * csize * px
    if notional > MAX_NOTIONAL:
        fail("[ERROR] nocional ~$%.2f > guardia $%.2f — DETENIDO"
             % (notional, MAX_NOTIONAL))
    if tot_eq is not None and notional * 3.0 > tot_eq:
        fail("[ERROR] margen ~$%.2f (1x) vs colateral ~$%.2f -> NO CABE. DETENIDO"
             % (notional, tot_eq))

    log.info("Disparo %s | %s ct | nocional ~$%.2f | SL %s (dist %.2f%%) | TP %s | clOrdId %s",
             "LONG" if long_dir else "SHORT",
             _fmt_contracts(contracts), notional, sl_s, dist_pct, tp_s, cl)
    body = {
        "instId": inst_id, "tdMode": MARGIN_MODE, "posSide": "net",
        "side": side, "ordType": "optimal_limit_ioc",
        "sz": _fmt_contracts(contracts), "clOrdId": cl,
        "attachAlgoOrds": [{
            "attachAlgoClOrdId": ("XLMA%d" % bar_ms)[:32],
            "slTriggerPx": sl_s, "slOrdPx": "-1",
            "tpTriggerPx": tp_s, "tpOrdPx": "-1",
        }],
    }
    try:
        r = ex.privatePostTradeOrder(body)
    except Exception as e:
        fail("[ERROR] envio de orden fallo: %s — DETENIDO" % str(e)[:200])
    if str(r.get("code")) != "0":
        fail("[ERROR] OKX rechazo la orden: code=%s msg=%s — DETENIDO"
             % (r.get("code"), r.get("msg")))
    od = (r.get("data") or [{}])[0]
    if str(od.get("sCode")) != "0":
        fail("[ERROR] OKX rechazo (sCode=%s sMsg=%s) — DETENIDO"
             % (od.get("sCode"), od.get("sMsg")))
    log.info("Orden aceptada | ordId=%s", od.get("ordId"))

    time.sleep(2)
    algos = [a for a in algo_pending(ex, inst_id) if a.get("instId") == inst_id]
    if not algos:
        log.error("ALARMA: SL/TP no aparecen pendientes en OKX")
        emergency_close(ex, inst_id, side, contracts)
        fail("Nave sin coraza — cerrada por seguridad. Plan B al puente.")
    a0 = algos[0]
    log.info("Nave blindada: SL/TP confirmados en OKX (SL trig %s | TP trig %s)",
             a0.get("slTriggerPx"), a0.get("tpTriggerPx"))


# ---------------- MAIN ----------------
def run_cycle():
    shadow = SHADOW or DRY_RUN
    if DRY_RUN and not SHADOW:
        log.info("Freno de flota DRY_RUN=1 -> forzando SOMBRA")
    log.info("NAVIO XLM LA ESTELAR EMA3/21 %s %s + BLINDAJE xpl-v4.2 (XPERP USD) | %s | MAX $%.0f",
             XLM_TF, VERSION, "SOMBRA" if shadow else "REAL", MAX_NOTIONAL)
    if XLM_TF not in TF_MS:
        fail("[ERROR] XLM_TF invalido: %s" % XLM_TF)

    ex, has_creds = build_exchange()
    if not shadow and not has_creds:
        fail("[ERROR] modo REAL sin credenciales OKX — DETENIDO")
    market = pick_future(ex)
    inst_id = market["id"]
    sym = market["symbol"]
    log.info("Instrumento: %s (XPERP — vencimiento mas lejano | settle %s | ct=%s)",
             inst_id, market.get("settle"), market.get("contractSize"))
    tot_eq = auth_and_collateral(ex, has_creds)

    if has_creds:
        pos_c = open_position_contracts(ex, sym)
        algos = algo_pending(ex, inst_id)
        if pos_c or algos:
            log.info("Nave ocupada: %.0f ct en posicion | %d algos vivos — no se abre nada. Vigilando.",
                     pos_c, len(algos))
            return 0

    candles = closed_candles(ex, sym, XLM_TF, N_CANDLES)
    sig = compute_signal(candles)
    log.info("Precio: %.5f | EMA3/21: %.5f/%.5f | %s (estado)",
             sig["px"], sig["e3"], sig["e21"],
             "EMA3 > EMA21" if sig["stance"] > 0 else "EMA3 < EMA21")

    if sig["cross_idx"] is None:
        log.info("Sin cruce EMA3/21 legal en la ventana. Vigilando.")
        return 0
    if sig["sep"] < SEP_LOCK:
        log.info("Cruce con separacion %.3f%% < candado %.2f%% — descartado. Vigilando.",
                 sig["sep"], SEP_LOCK)
        return 0

    dir_txt = "LONG" if sig["dir"] > 0 else "SHORT"
    sl, tp, dist_pct = sl_tp(sig["px"], sig["e21_cross"], sig["dir"] > 0)
    bar_h = datetime.fromtimestamp(sig["bar_ts"] / 1000, tz=timezone.utc).strftime("%d-%m %H:%M")

    if XLM_CONTRACTS > 0:
        contracts = int(XLM_CONTRACTS)
        est_notional = contracts * float(market.get("contractSize") or 0.0) * sig["px"]
        if est_notional > MAX_NOTIONAL:
            fail("[ERROR] XLM_CONTRACTS=%d ~ $%.2f > guardia $%.2f — DETENIDO"
                 % (contracts, est_notional, MAX_NOTIONAL))
    else:
        contracts, est_notional = size_contracts(market, sig["px"])

    if shadow:
        log.info("[SOMBRA] Senal %s legal | vela %s UTC | sep %.2f%% | SL ~%.5f (dist %.2f%%) | "
                 "TP ~%.5f | %s ct ~ $%.2f — sin municion, solo bitacora",
                 dir_txt, bar_h, sig["sep"], sl, dist_pct, tp,
                 _fmt_contracts(contracts), est_notional)
        return 0

    # ---- REAL ----
    try:
        ex.set_position_mode(False, sym)
        log.info("Position mode -> one-way para %s.", inst_id)
    except Exception as e:
        log.warning("set_position_mode tolerado: %s", str(e)[:120])
    try:
        ex.set_leverage(1, sym, params={"mgnMode": MARGIN_MODE})
        log.info("Apalancamiento: 1x (%s)", MARGIN_MODE)
    except Exception as e:
        log.warning("set_leverage tolerado: %s", str(e)[:120])

    active, pnl, ft = cooldown_active(ex, inst_id)
    if active:
        mins = int((ex.milliseconds() - ft) / 60000)
        log.info("Cooldown: perdida hace %d min — calma a bordo. Vigilando.", mins)
        return 0

    cl = ("XLM%d" % sig["bar_ts"])[:32]
    if already_traded(ex, inst_id, cl):
        log.info("Vela %s ya navegada (candle_already_traded) — descartado.", bar_h)
        return 0

    min_sz = (((market.get("limits") or {}).get("amount") or {}).get("min")) or 1
    if contracts < float(min_sz):
        fail("[ERROR] %s ct < minimo del mercado (%s) — DETENIDO"
             % (_fmt_contracts(contracts), min_sz))

    fire(ex, market, inst_id, sig, contracts, tot_eq)
    return 0


def main():
    if SINGLE_CYCLE:
        try:
            run_cycle()
        except SystemExit:
            raise
        except Exception as e:
            log.error("[CRITICAL] %s", str(e))
            sys.exit(1)
        log.info("Ciclo unico finalizado. Saliendo (lo retomara el cron).")
        return
    tf_ms = TF_MS.get(XLM_TF, 3600000)
    while True:
        try:
            start_t = time.time()
            run_cycle()
        except SystemExit:
            raise
        except Exception as e:
            log.error("[CRITICAL] %s — bucle sobrevive.", str(e))
        elapsed = (time.time() - start_t) * 1000
        sleep_ms = max(10000, tf_ms - (elapsed % tf_ms))
        log.info("Durmiendo %.1f min...", sleep_ms / 60000.0)
        time.sleep(sleep_ms / 1000.0)


if __name__ == "__main__":
    sys.exit(main())
