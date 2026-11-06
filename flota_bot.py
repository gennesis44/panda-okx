#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =====================================================================
# flota_bot.py — LA FLOTA CARBONO · BUQUE: EL FANAL (vimiendo de la flota)
# 100% AUTONOMO: solo stdlib + ccxt (pip). Sin pandas, sin sandbox.
#
#  QUE ES: el farol de la flota. Un buque SOLO-LECTOR que vigila cada nave
#  (AVAX EL OCTAVO, INJ EL INYECTOR — la lista crece con la flota) y
#  reporta en cada pasada:
#    1. RADAR      -> precio, EMA3/21, stance y separacion vs candado 0.17%
#                     (mercado PUBLICO, sin credenciales)
#    2. NAVE       -> posicion abierta (lado, ct, uPnL) y SL/TP vivos
#                     (lectura privada, jamas escritura)
#    3. WIN RATE   -> bitacora REAL de OKX por nave: cierres, WR neto con
#                     el peaje DENTRO (fee + funding + liquidacion), net
#                     acumulado, peaje acumulado, expectancia $/trade y
#                     WR de equilibrio teorico por R:R de la nave
#    4. RESUMEN    -> totales de flota: trades, WR combinado, bruto -> neto
#                     (el peaje a la vista), naves legibles
#
#  VOTO DE PAZ (garantia certificada en banco): este buque NUNCA opera.
#  No lleva canon, no fija palanca, no muta modo de cuenta, no abre ni
#  cierra nada: SOLO consultas de lectura privada (privateGet*) y mercado
#  publico. El banco de certificacion verifica en el codigo fuente que no
#  existe ninguna via de disparo ni escritura. Si manana la flota crece
#  (HBAR, ADA...), se anade la base a FLOTA_SHIPS y el fanal la vigila.
#
#  DOBLE FUENTE win rate (heredada de los buques v2.2, bitacora JAMAS
#  puerta): 1) positions-history (una fila por cierre; net = realizedPnl
#  o pnl de reserva + fee + fundingFee + liqPenalty) -> 2) fills-history
#  (cada fill con fillPnl != 0 + fee; aproximacion por fill, etiquetada).
#  Ambas rotas -> nave marcada ILEGIBLE y el fanal sigue con el resto.
#  Solo si TODAS las naves son ilegibles con credenciales a bordo, el run
#  se pinta rojo para que lo veas. Fallo parcial: verde con avisos.
#
#  SIN CREDENCIALES: radar publico de todas las naves + aviso. Los secrets
#  son los mismos de la flota (OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSWORD).
#
# VARIABLES DE ENTORNO (todas opcionales):
#   FLOTA_SHIPS=AVAX,INJ    bases a vigilar (coma separada)
#   FANAL_<BASE>_INST_ID    fija el instrumento de una nave (defecto:
#                           reanclaje automatico al XPERP USD mas lejano)
#   FANAL_<BASE>_RR=1.5     R:R teorico de la nave -> WR equilibrio 40.0%
#   FANAL_TF=1h             marco del radar (diseno del capitan)
#   FANAL_N_CANDLES=200     velas para el radar
#   FANAL_SEP_LOCK=0.17     candado de referencia del radar
#   FANAL_DEBUG=1           diagnostico puntual
# =====================================================================

import os
import sys
import logging
from datetime import datetime, timezone

try:
    import ccxt
except ImportError:
    ccxt = None

VERSION = "v1.0-fanal-flota"
TF_MS = {"5m": 300000, "15m": 900000, "30m": 1800000,
         "1h": 3600000, "2h": 7200000, "4h": 14400000, "6h": 21600000}
SHIP_NAMES = {"AVAX": "EL OCTAVO", "INJ": "EL INYECTOR"}

FLOTA_SHIPS = [s.strip().upper() for s in os.environ.get("FLOTA_SHIPS", "AVAX,INJ").split(",") if s.strip()]
FANAL_TF = os.environ.get("FANAL_TF", "1h")
FANAL_N_CANDLES = int(os.environ.get("FANAL_N_CANDLES", "200"))
FANAL_SEP_LOCK = float(os.environ.get("FANAL_SEP_LOCK", "0.17"))
FANAL_DEBUG = os.environ.get("FANAL_DEBUG", "0") == "1"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("flota")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def ship_rr(base):
    try:
        return float(os.environ.get("FANAL_%s_RR" % base, "1.5"))
    except ValueError:
        return 1.5


def ship_inst_env(base):
    return os.environ.get("FANAL_%s_INST_ID" % base, "").strip()


# ---------------- EXCHANGE ----------------
def build_exchange():
    if ccxt is None:
        log.error("[ERROR] Falta ccxt: pip install ccxt")
        sys.exit(1)
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


def anchor(ex, base):
    """Reanclaje del radar: vencimiento mas lejano de <base>/USD settle USD.
    Veto triple heredado de la flota: quote USD, settle USD, futuro fechado
    (MiCA/EEE: USDT prohibido a bordo, tambien en el fanal)."""
    ex.load_markets()
    cands = []
    for m in ex.markets.values():
        if m.get("base") != base:
            continue
        if m.get("quote") != "USD":
            continue
        if m.get("settle") != "USD":
            continue
        if m.get("swap") or not m.get("future"):
            continue
        cands.append(m)
    if not cands:
        return None
    return max(cands, key=lambda m: int(m.get("expiry") or 0))


def resolve_ship(ex, base):
    """Instrumento de la nave: env si lo fija el capitan, si no reanclaje."""
    fixed = ship_inst_env(base)
    if fixed:
        ex.load_markets()
        for m in ex.markets.values():
            if m.get("id") == fixed:
                return m
        log.warning("FANAL_%s_INST_ID=%s no existe en el catalogo — reanclando", base, fixed)
    return anchor(ex, base)


# ---------------- RADAR (publico) ----------------
def ema(vals, span):
    k = 2.0 / (span + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1.0 - k)
        out.append(e)
    return out


def ship_radar(ex, sym):
    """Estado EMA3/21 en velas CERRADAS. Publico: sin credenciales tambien."""
    if FANAL_TF not in TF_MS:
        return {"error": "marco invalido %s" % FANAL_TF}
    try:
        raw = ex.fetch_ohlcv(sym, FANAL_TF, limit=FANAL_N_CANDLES + 3)
    except Exception as e:
        return {"error": "velas ilegibles (%s)" % str(e)[:120]}
    raw.sort(key=lambda c: c[0])
    now = ex.milliseconds()
    closed = [c for c in raw if c[0] + TF_MS[FANAL_TF] <= now]
    if len(closed) < 25:
        return {"error": "velas cerradas insuficientes (%d)" % len(closed)}
    closes = [float(x[4]) for x in closed]
    e3 = ema(closes, 3)[-1]
    e21 = ema(closes, 21)[-1]
    sep = abs(e3 - e21) / e21 * 100.0 if e21 else 0.0
    return {
        "px": closes[-1], "e3": e3, "e21": e21,
        "stance": "LONG" if e3 > e21 else "SHORT",
        "sep": sep,
        "bar_h": datetime.fromtimestamp(closed[-1][0] / 1000, tz=timezone.utc).strftime("%d-%m %H:%M"),
    }


# ---------------- NAVE (lectura privada) ----------------
def ship_status(ex, sym, inst_id):
    """Posicion abierta + SL/TP vivos. SOLO LECTURA: cualquier fallo se
    reporta y sigue — el fanal jamas interviene en la nave."""
    out = {"pos": None, "algos": None}
    try:
        for p in ex.fetch_positions([sym]):
            c = p.get("contracts")
            if c and abs(float(c)) > 0:
                out["pos"] = {
                    "side": p.get("side") or "?",
                    "contracts": abs(float(c)),
                    "upnl": _f(p.get("unrealizedPnl")),
                }
                break
    except Exception as e:
        out["pos"] = {"error": str(e)[:120]}
    algos = 0
    try:
        for ot in ("oco", "conditional"):
            r = ex.privateGetTradeOrdersAlgoPending({"ordType": ot, "instId": inst_id})
            algos += len(r.get("data") or [])
        out["algos"] = algos
    except Exception:
        out["algos"] = None
    return out


# ---------------- WIN RATE (bitacora, doble fuente) ----------------
def ship_telemetry(ex, inst_id, rr):
    """WR neto de la nave con el peaje DENTRO del resultado. Fuente 1:
    positions-history (una fila por cierre). Cuerpo pelado '0' (KeyError
    :: 0 en my.okx.com) -> fuente 2: fills-history (fillPnl != 0 + fee).
    Ambas rotas -> {'error': ...} y el fanal sigue. JAMAS bloquea ni dispara:
    este buque no lleva canon."""
    rows, src = [], "positions-history"
    try:
        r = ex.privateGetAccountPositionsHistory(
            {"instType": "FUTURES", "instId": inst_id, "limit": "100"})
        for rec in (r.get("data") or []):
            ts = int(_f(rec.get("uTime")) or _f(rec.get("closedTime"))
                     or _f(rec.get("cTime")))
            gross = (_f(rec.get("realizedPnl"))
                     if rec.get("realizedPnl") not in (None, "")
                     else _f(rec.get("pnl")))
            peaje = _f(rec.get("fee")) + _f(rec.get("fundingFee")) + _f(rec.get("liqPenalty"))
            if ts > 0:
                rows.append((ts, gross + peaje, peaje))
    except Exception as e:
        log.warning("%s: positions-history no abrio (%s) — probando fills-history",
                    inst_id, str(e)[:120])
        rows = []
        try:
            r = ex.privateGetTradeFillsHistory(
                {"instType": "FUTURES", "instId": inst_id, "limit": "100"})
            for x in (r.get("data") or []):
                fp = _f(x.get("fillPnl"))
                if fp == 0:
                    continue
                ts = int(_f(x.get("ts")) or _f(x.get("fillTime")))
                peaje = _f(x.get("fee"))
                if ts > 0:
                    rows.append((ts, fp + peaje, peaje))
            src = "fills-history (aprox por fill)"
        except Exception as e2:
            log.warning("%s: win rate ilegible por ambas fuentes (%s)", inst_id, str(e2)[:120])
            return {"error": "telemetria ilegible (%s / %s)" % (str(e)[:80], str(e2)[:80])}
    t = {"error": None, "src": src, "rr": rr, "recent": []}
    if not rows:
        t.update({"n": 0, "wins": 0, "losses": 0, "flats": 0, "wr": 0.0,
                  "net": 0.0, "peaje": 0.0, "gross": 0.0,
                  "avg_w": 0.0, "avg_l": 0.0, "expectancy": 0.0,
                  "be_wr": 100.0 / (1.0 + rr)})
        return t
    rows.sort(key=lambda x: x[0])
    wins = [x for x in rows if x[1] > 0]
    losses = [x for x in rows if x[1] < 0]
    flats = [x for x in rows if x[1] == 0]
    n = len(rows)
    wr = 100.0 * len(wins) / n
    net = sum(x[1] for x in rows)
    peaje = sum(x[2] for x in rows)
    avg_w = sum(x[1] for x in wins) / len(wins) if wins else 0.0
    avg_l = sum(x[1] for x in losses) / len(losses) if losses else 0.0
    t.update({
        "n": n, "wins": len(wins), "losses": len(losses), "flats": len(flats),
        "wr": wr, "net": net, "peaje": peaje, "gross": net - peaje,
        "avg_w": avg_w, "avg_l": avg_l,
        "expectancy": (wr / 100.0) * avg_w + ((100.0 - wr) / 100.0) * avg_l,
        "be_wr": 100.0 / (1.0 + rr),
        "recent": rows[-10:],
    })
    return t


# ---------------- INFORME ----------------
def fleet_summary(tel_by_ship):
    """Totales de flota solo con naves legibles (los errores no ensucian)."""
    ok = {k: v for k, v in tel_by_ship.items() if v and not v.get("error")}
    n = sum(t["n"] for t in ok.values())
    wins = sum(t["wins"] for t in ok.values())
    losses = sum(t["losses"] for t in ok.values())
    net = sum(t["net"] for t in ok.values())
    peaje = sum(t["peaje"] for t in ok.values())
    wr = 100.0 * wins / n if n else 0.0
    return {"ships_ok": len(ok), "ships_total": len(tel_by_ship),
            "n": n, "wins": wins, "losses": losses,
            "wr": wr, "net": net, "peaje": peaje, "gross": net - peaje}


def verdict(t):
    if t.get("error"):
        return "ILEGIBLE"
    if t["n"] == 0:
        return "SIN CIERRES TODAVIA"
    if t["n"] < 10:
        return "muestra pequena (n=%d, esperar >=10)" % t["n"]
    if t["wr"] < t["be_wr"]:
        return "WR %.1f%% BAJO equilibrio %.1f%% — no crecer; calibrar" % (t["wr"], t["be_wr"])
    return "WR %.1f%% cubre equilibrio %.1f%% — nave sana" % (t["wr"], t["be_wr"])


def main():
    log.info("FANAL DE FLOTA %s | naves: %s | radar %s | SOLO LECTURA (sin canon)",
             VERSION, ",".join(FLOTA_SHIPS), FANAL_TF)
    if FANAL_TF not in TF_MS:
        log.error("[ERROR] FANAL_TF invalido: %s", FANAL_TF)
        return 1
    ex, has_creds = build_exchange()
    if not has_creds:
        log.info("Sin credenciales: radar publico de flota — win rate no disponible esta pasada")

    tel_by_ship = {}
    for base in FLOTA_SHIPS:
        ship_name = SHIP_NAMES.get(base, "BUQUE %s" % base)
        m = resolve_ship(ex, base)
        if not m:
            log.info("--- %s (%s) ---", ship_name, base)
            log.warning("  Sin futuro %s/USD settle USD — nave fuera de catalogo", base)
            tel_by_ship[base] = {"error": "sin instrumento"}
            continue
        inst_id = m["id"]
        sym = m["symbol"]
        log.info("--- %s (%s | %s) ---", ship_name, base, inst_id)

        radar = ship_radar(ex, sym)
        if radar.get("error"):
            log.warning("  Radar: %s", radar["error"])
        else:
            log.info("  Radar: %.4f | EMA3/21 %.4f/%.4f | %s | sep %.3f%% (candado %.2f%%) | vela %s UTC",
                     radar["px"], radar["e3"], radar["e21"], radar["stance"],
                     radar["sep"], FANAL_SEP_LOCK, radar["bar_h"])

        if not has_creds:
            log.info("  Win rate: sin credenciales esta pasada")
            tel_by_ship[base] = {"error": "sin credenciales"}
            continue

        st = ship_status(ex, sym, inst_id)
        if st["pos"]:
            if st["pos"].get("error"):
                log.warning("  Nave: posicion ilegible (%s)", st["pos"]["error"])
            else:
                log.info("  Nave: OCUPADA %s %s ct | uPnL %+.4f USD | algos vivos: %s",
                         st["pos"]["side"], ("%g" % st["pos"]["contracts"]),
                         st["pos"]["upnl"],
                         st["algos"] if st["algos"] is not None else "ilegibles")
        else:
            log.info("  Nave: sin posicion abierta | algos vivos: %s",
                     st["algos"] if st["algos"] is not None else "ilegibles")

        t = ship_telemetry(ex, inst_id, ship_rr(base))
        tel_by_ship[base] = t
        if t.get("error"):
            log.warning("  Win rate: %s", t["error"])
            continue
        if t["n"] == 0:
            log.info("  Win rate: sin cierres registrados — la bitacora empezara al primer cierre")
            continue
        log.info("  Win rate (%s): %d cierres | %dW/%dL/%dF | WR %.1f%% | net %+.4f | peaje %+.4f",
                 t["src"], t["n"], t["wins"], t["losses"], t["flats"],
                 t["wr"], t["net"], t["peaje"])
        log.info("    bruto %+.4f -> neto %+.4f (el peaje se ve) | avg win %+.4f | avg loss %+.4f | expectancia %+.4f USD/trade",
                 t["gross"], t["net"], t["avg_w"], t["avg_l"], t["expectancy"])
        log.info("    R:R %.2f -> WR equilibrio %.1f%% | VEREDICTO: %s",
                 t["rr"], t["be_wr"], verdict(t))
        for ts, net, peaje in t["recent"]:
            mark = "WIN " if net > 0 else ("LOSS" if net < 0 else "FLAT")
            log.info("    %s UTC | %s | net %+.4f (peaje %+.4f)",
                     datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%d-%m %H:%M"),
                     mark, net, peaje)

    if not has_creds:
        log.info("==== RESUMEN FLOTA: solo radar publico (sin credenciales) ====")
        return 0

    s = fleet_summary(tel_by_ship)
    log.info("==== RESUMEN FLOTA (%s) ====", VERSION)
    log.info("Naves legibles %d/%d | cierres %d | %dW/%dL | WR flota %.1f%%",
             s["ships_ok"], s["ships_total"], s["n"], s["wins"], s["losses"], s["wr"])
    log.info("Bruto flota %+.4f -> neto flota %+.4f | peaje total %+.4f USD",
             s["gross"], s["net"], s["peaje"])
    for base, t in tel_by_ship.items():
        log.info("  %-4s -> %s", base, verdict(t))
    if s["ships_ok"] == 0:
        log.error("NINGUNA nave legible con credenciales a bordo — revisar secrets/endpoint")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
