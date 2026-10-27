#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =====================================================================
# radar_flota.py — NAVIO RADAR · LA FLOTA CARBONO · BUQUE: EL FARO
# 100% AUTONOMO: solo stdlib + ccxt. NO dispara NADA — el faro no lleva
# municion: solo alumbra. Lectura pura (velas publicas + cuenta completa).
#
# MISION (decreto del capitan, 2026-10-27): "saber que andan haciendo la
# flota: buscando o capturando y a que distancia pueden estar el perro,
# la cajonera y los demas".
#
#   · CAPTURANDO  -> nave con posicion abierta: lado, contratos, entrada,
#                    precio vivo, PnL flotante y:
#      🐕 el perro   = su SL: a cuantos % esta de morder
#      cajonera      = su TP: a cuantos % esta el cajon del botin
#      (si una captura va SIN coraza -> ALARMA en el faro)
#   · BUSCANDO    -> naves declaradas sin posicion: stance EMA3/21, separacion
#                    actual vs su candado (a cuantos puntos de disparar) y
#                    si hay cruce en las ultimas 4 velas cerradas
#   · LOS DEMAS   -> cualquier captura en la cuenta de un instrumento NO
#                    declarado en el registro: tambien se alumbra (el capitan
#                    sabe quien es quien por su instId)
#   · BOTIN 24h   -> PnL realizado por nave (positions-history, PnL de la
#                    posicion cerrada) y calmas del perro Ax (perdida reciente
#                    dentro del COOLDOWN)
#
# REGISTRO DE NAVES (env RADAR_SHIPS): "instId:bar:lock,..." — instId de
# swap publico USDT como proxy de precio (metodo de flota: difiere del XPERP
# USD solo en basis; los audits certificados usan la misma fuente).
#   ADA-USDT-SWAP:15m:0.15  (TITAN)  INJ-USDT-SWAP:1h:0.17 (INYECTOR)
#   XPL-USDT-SWAP:30m:0.15  ... el capitan anade las demas (ICP, etc.)
#
# SIN CREDENCIALES: modo faro publico — solo radar de senales (BUSCANDO).
# CON CREDENCIALES: ademas bodega, capturas, botin y calmas. El faro NUNCA
# envia ordenes: fail-safe por diseno, no fail-closed (un faro apagado no
# hunde nada — solo deja de aluminbrar y lo dice).
#
# v1.1-faro — soldaduras tras la 1a ronda REAL en Actions (06:06 UTC):
#   F1 px 0.0000 en capturas -> las claves unificadas de ccxt son
#      markPrice/entryPrice/unrealizedPnl (markPx/avgPx/upl viven solo en
#      info); sin precio no hay distancias del perro ni de la cajonera.
#   F2 'privateGetTradeFillPnl' NO existe en ccxt 4.5.90 (AttributeError;
#      los fillPnl de OKX REST ya no bajan — solo WebSocket) -> botin y
#      calma via /api/v5/account/positions-history (PnL realizado por
#      posicion cerrada, campo realizedPnl certificado en el parser ccxt).
#   F3 capturas con 0 contratos pero PnL vivo no se ocultan (reserva
#      info.pos) y sin precio se AVISA (nada silencioso en el faro).
# =====================================================================

import os
import sys
import logging
from datetime import datetime, timezone

try:
    import ccxt
except ImportError:
    ccxt = None

VERSION = "v1.1-faro"
TF_MS = {"5m": 300000, "15m": 900000, "30m": 1800000,
         "1h": 3600000, "2h": 7200000, "4h": 14400000, "6h": 21600000}

# ---------------- CONFIG ----------------
RADAR_SHIPS_RAW = os.environ.get(
    "RADAR_SHIPS",
    "ADA-USDT-SWAP:15m:0.15,INJ-USDT-SWAP:1h:0.17,XPL-USDT-SWAP:30m:0.15,"
    "XLM-USDT-SWAP:1h:0.15,ICP-USDT-SWAP:1h:0.15")
FILLS_H = float(os.environ.get("RADAR_FILLS_H", "24"))       # ventana del botin
COOLDOWN_MIN = float(os.environ.get("COOLDOWN_MIN", "60"))   # calma del perro Ax
N_CANDLES = int(os.environ.get("RADAR_N_CANDLES", "40"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("faro")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse_ships(raw):
    ships = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            log.warning("nave ilegible en RADAR_SHIPS: %s (se ignora)", chunk)
            continue
        inst, bar, lock = parts[0].strip().upper(), parts[1].strip(), _f(parts[2])
        if bar not in TF_MS or lock <= 0:
            log.warning("nave con bar/lock invalido: %s (se ignora)", chunk)
            continue
        ships.append({"inst": inst, "bar": bar, "lock": lock,
                      "base": inst.split("-")[0]})
    return ships


# ---------------- EXCHANGES ----------------
def build_auth_ex():
    """Faro con llaves (lectura). Solo se construye si hay credenciales."""
    api_key = os.environ.get("OKX_API_KEY", "")
    secret = os.environ.get("OKX_SECRET_KEY", "")
    passwd = os.environ.get("OKX_PASSWORD", "")
    if not (api_key and secret and passwd):
        return None
    ex = ccxt.okx({
        "apiKey": api_key, "secret": secret, "password": passwd,
        "hostname": "my.okx.com", "enableRateLimit": True,
        "options": {"defaultType": "futures"},
    })
    ex.hostname = "my.okx.com"
    return ex


def build_public_ex():
    """Radar de senales publico (www.okx.com, sin llaves)."""
    return ccxt.okx({"enableRateLimit": True})


# ---------------- RADAR DE SENALES ----------------
def ema(vals, span):
    k = 2.0 / (span + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1.0 - k)
        out.append(e)
    return out


def scan_signal(ex_pub, ship):
    """Estado BUSCANDO de una nave: stance, sep vs candado y cruces en las
    ultimas 4 velas cerradas. Nunca explota: cualquier fallo = SIN DATOS."""
    inst, bar, lock = ship["inst"], ship["bar"], ship["lock"]
    try:
        raw = ex_pub.fetch_ohlcv(inst, bar, limit=N_CANDLES + 3)
        tfms = TF_MS[bar]
        now = ex_pub.milliseconds()
        closed = [c for c in raw if c[0] + tfms <= now][-N_CANDLES:]
        if len(closed) < 25:
            return "SIN DATOS (%d velas cerradas)" % len(closed)
        c = [float(x[4]) for x in closed]
        n = len(c)
        e3 = ema(c, 3)
        e21 = ema(c, 21)
        stance = [1 if e3[i] > e21[i] else -1 for i in range(n)]
        sep_now = abs(e3[-1] - e21[-1]) / e21[-1] * 100.0
        mar = "ALCISTA" if stance[-1] > 0 else "BAJISTA"
        cross = None
        for i in range(n - 4, n):
            if i > 0 and stance[i] != stance[i - 1]:
                cross = (n - 1 - i, stance[i],
                         abs(e3[i] - e21[i]) / e21[i] * 100.0)
        if cross:
            age, dirn, sep = cross
            txt = "cruce %s hace %d vela(s), sep %.3f%%" % (
                "LONG" if dirn > 0 else "SHORT", age, sep)
            if sep >= lock:
                return "%s | SEÑAL CALIENTE — %s (>= candado %.2f%%)" % (mar, txt, lock)
            return "%s | %s — por debajo del candado %.2f%%" % (mar, txt, lock)
        if sep_now >= lock:
            return "%s | mar agitado (sep %.3f%% > candado %.2f%%) — a la espera de cruce" % (mar, sep_now, lock)
        return "%s | sep %.3f%% — a %.3f puntos del candado %.2f%%" % (
            mar, sep_now, lock - sep_now, lock)
    except Exception as e:
        return "SIN DATOS (%s)" % str(e)[:80]


# ---------------- CUENTA (CAPTURANDO / BOTIN / CALMA) ----------------
def read_equity(ex):
    """Bodega por el metodo del perro (3 capas). Devuelve None si ilegible."""
    try:
        bal = ex.fetch_balance()
    except Exception:
        return None
    try:
        d0 = bal.get("info", {}).get("data", [{}])[0]
        if d0.get("totEq"):
            return _f(d0["totEq"])
    except Exception:
        pass
    try:
        details = bal.get("info", {}).get("data", [{}])[0].get("details") or []
        suma = sum(_f(d.get("eqUsd")) for d in details)
        if suma > 0:
            return suma
    except Exception:
        pass
    try:
        suma = sum(_f((bal.get(c) or {}).get("total")) for c in ("USD", "USDC", "USDT"))
        if suma > 0:
            return suma
    except Exception:
        pass
    return None


def read_positions(ex):
    """F1 (ronda real 06:06): en ccxt las claves unificadas de una posicion
    son markPrice/entryPrice/unrealizedPnl — markPx/avgPx/upl solo viven en
    el crudo info. Reserva doble en info por si ccxt cambia de humor."""
    try:
        poss = ex.fetch_positions()
    except Exception as e:
        log.warning("posiciones ilegibles (%s) — capturas no se alumbran", str(e)[:100])
        return []
    out = []
    for p in poss:
        info = p.get("info", {}) or {}
        ct = abs(_f(p.get("contracts"))) or abs(_f(info.get("pos")))   # F3: reserva info.pos
        upl = _f(p.get("unrealizedPnl")) or _f(info.get("upl"))
        if ct <= 0 and abs(upl) < 1e-9:
            continue                        # ni contratos ni PnL: sombra vacia
        px = (_f(p.get("markPrice")) or _f(info.get("markPx"))
              or _f(p.get("lastPrice")) or _f(info.get("lastPx")))
        side = (p.get("side") or "").lower()
        if not side:
            side = "long" if (_f(p.get("contracts")) or _f(info.get("pos"))) > 0 else "short"
        out.append({
            "instId": info.get("instId") or p.get("symbol") or "?",
            "side": side, "contracts": ct,
            "entry": _f(p.get("entryPrice")) or _f(info.get("avgPx")) or _f(info.get("openAvgPx")),
            "px": px, "upl": upl,
            "lever": info.get("lever") or p.get("lever") or "?",
            "mgn": (info.get("mgnMode") or "?").lower(),
        })
    return out


def read_algolive(ex, inst_ids):
    """SL/TP vivos por instId. Primero global; si la llamada global se atraganta,
    nave por nave (mismo endpoint certificado del blindaje)."""
    alive = {}
    orders = []
    try:
        for ot in ("oco", "conditional"):
            r = ex.privateGetTradeOrdersAlgoPending({"ordType": ot})
            orders.extend(r.get("data") or [])
    except Exception:
        for inst in inst_ids:
            for ot in ("oco", "conditional"):
                try:
                    r = ex.privateGetTradeOrdersAlgoPending({"ordType": ot, "instId": inst})
                    orders.extend(r.get("data") or [])
                except Exception as e:
                    log.warning("algo pending ilegible (%s %s): %s", inst, ot, str(e)[:80])
    for a in orders:
        inst = a.get("instId")
        if not inst:
            continue
        slot = alive.setdefault(inst, {"sl": None, "tp": None})
        for k, tag in (("slTriggerPx", "sl"), ("tpTriggerPx", "tp")):
            v = _f(a.get(k))
            if v > 0:
                slot[tag] = v if slot[tag] is None else slot[tag]
    return alive


def read_spoils(ex):
    """F2 (ronda real 06:06): privateGetTradeFillPnl NO existe en ccxt 4.5.90
    y los fillPnl de OKX REST ya solo bajan por WebSocket. Fuente REST
    certificada: /api/v5/account/positions-history — una fila por posicion
    CERRADA con su PnL realizado (campo realizedPnl, leido asi por el parser
    de ccxt). Una sola llamada, sin dedupes."""
    try:
        r = ex.privateGetAccountPositionsHistory({"limit": "100"})
        return r.get("data") or []
    except Exception as e:
        log.warning("positions-history ilegible: %s", str(e)[:100])
        return []


def rec_time(rec):
    t = _f(rec.get("uTime")) or _f(rec.get("closedTime")) or _f(rec.get("cTime"))
    return int(t)


# ---------------- REPORTE ----------------
def main():
    if ccxt is None:
        log.error("[ERROR] Falta ccxt: pip install ccxt — el faro no se enciende")
        return 1
    ships = parse_ships(RADAR_SHIPS_RAW)
    now_utc = datetime.now(tz=timezone.utc).strftime("%d-%m-%Y %H:%M UTC")
    log.info("=" * 68)
    log.info("EL FARO · LA FLOTA CARBONO %s | ronda de las %s", VERSION, now_utc)
    log.info("=" * 68)

    ex_pub = build_public_ex()

    # ---- cuenta: capturas / botin / calma ----
    ex_auth = build_auth_ex()
    captures, captured_bases, botin = [], set(), {}
    equity = None
    if ex_auth is not None:
        equity = read_equity(ex_auth)
        if equity:
            log.info("BODEGA: ~$%.2f (colateral real)", equity)
        else:
            log.info("BODEGA: ilegible desde el faro (sin credenciales validas?)")
        captures = read_positions(ex_auth)
        alive = read_algolive(ex_auth, [c["instId"] for c in captures])
        no_reg = []
        for cap in captures:
            base = cap["instId"].split("-")[0]
            captured_bases.add(base)
            declared = any(base == s["base"] for s in ships)
            reg = alive.get(cap["instId"], {"sl": None, "tp": None})
            side_txt = "LONG" if cap["side"] == "long" else "SHORT"
            log.info("🎣 CAPTURANDO %s | %s %.0f ct %sx %s | entrada %.4f | px %.4f | uPnL %+.2f",
                     cap["instId"], side_txt, cap["contracts"], cap["lever"],
                     cap["mgn"], cap["entry"], cap["px"], cap["upl"])
            sl, tp, px = reg.get("sl"), reg.get("tp"), cap["px"]
            if px > 0:
                if sl:
                    log.info("   🐕 perro (SL) muerde a %.4f — a %.2f%% de distancia", sl, abs(px - sl) / px * 100.0)
                else:
                    log.info("   ⚠️ SIN PERRO (SL no visible) — nave sin coraza?")
                if tp:
                    log.info("   🧰 cajonera (TP) espera a %.4f — a %.2f%% de distancia", tp, abs(tp - px) / px * 100.0)
                else:
                    log.info("   🧰 cajonera (TP) no visible")
            else:
                log.info("   ⚠️ precio vivo ilegible desde OKX — distancias del perro/cajonera no alumbradas")
                if not sl and not tp:
                    log.info("   ⚠️ ademas SIN coraza visible (ni SL ni TP pendientes)")
            if not declared:
                no_reg.append(cap)
        if no_reg:
            log.info("   · los demas: %s (naves no declaradas en RADAR_SHIPS — capturando igualmente)",
                     ", ".join(c["instId"] for c in no_reg))

        # botin 24h (PnL realizado por posicion cerrada) + calma del perro Ax
        history = read_spoils(ex_auth)
        cutoff_24 = (ex_auth.milliseconds() - FILLS_H * 3600 * 1000)
        cutoff_cd = (ex_auth.milliseconds() - COOLDOWN_MIN * 60 * 1000)
        for rec in history:
            ft = rec_time(rec)
            if ft < cutoff_24:
                continue
            pnl = _f(rec.get("realizedPnl"))
            slot = botin.setdefault(rec.get("instId") or "?",
                                    {"sum": 0.0, "n": 0, "w": 0, "l": 0, "last_t": 0, "last_loss_t": 0})
            slot["sum"] += pnl
            slot["n"] += 1
            slot["w" if pnl > 0 else "l"] += 1
            slot["last_t"] = max(slot["last_t"], ft)
            if pnl < 0:
                slot["last_loss_t"] = max(slot["last_loss_t"], ft)
        if botin:
            total = sum(s["sum"] for s in botin.values())
            ntot = sum(s["n"] for s in botin.values())
            wtot = sum(s["w"] for s in botin.values())
            log.info("🧾 BOTIN %dh: %+.2f USD en %d posiciones cerradas (%dW/%dL)",
                     int(FILLS_H), total, ntot, wtot, ntot - wtot)
            for inst, s in sorted(botin.items(), key=lambda kv: -kv[1]["sum"]):
                log.info("   · %s: %+.2f USD (%d cerradas, %dW/%dL)", inst, s["sum"], s["n"], s["w"], s["l"])
        else:
            log.info("🧾 BOTIN %dh: sin capturas cerradas (mar en calma)", int(FILLS_H))
        calmas = [(inst, s) for inst, s in botin.items()
                  if s["last_loss_t"] >= cutoff_cd]
        if calmas:
            for inst, s in calmas:
                mins = int((ex_auth.milliseconds() - s["last_loss_t"]) / 60000)
                log.info("💤 CALMA (perro Ax): %s — perdida reciente hace %d min, cooldown a bordo", inst, mins)
        else:
            log.info("💤 CALMA (perro Ax): ninguna nave en cooldown")
    else:
        log.info("Modo FARO PUBLICO: sin credenciales — solo radar de senales (sin bodega/capturas)")

    # ---- radar de senales (naves declaradas sin posicion) ----
    buscando = [s for s in ships if s["base"] not in captured_bases]
    log.info("🔎 BUSCANDO (%d naves del registro):", len(buscando))
    for s in buscando:
        log.info("🔭 %s %s | candado %.2f%% | %s", s["base"], s["bar"], s["lock"],
                 scan_signal(ex_pub, s))
    if not buscando:
        log.info("🔭 Toda la flota declarada esta capturando o en calma — nada buscando")

    log.info("=" * 68)
    log.info("Ronda del faro completa — la flota sigue a lo suyo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
