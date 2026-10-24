#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =====================================================================
# main-icp.py — NAVIO ICP · LA FLOTA CARBONO · BUQUE: EL PORTAAVIONES
# 100% AUTONOMO: solo stdlib + ccxt (pip). NO importa atakaya.py ni
# ningun otro archivo del repo — navega solo en un repo recien creado.
# Casco: blindaje certificado del xpl v4.2 · Motor: EMA 3/21
# Instrumento: ICP-USD XPERP (futuro fechado USD, vencimiento lejano)
#
# v1.1 — certificado autonomo para EL PORTAAVIONES (repo sin atakaya.py).
# Nace en SOMBRA (SHADOW=1): bitacora de senales, sin municion.
#
# BLINDAJE A BORDO (herencia xpl v4.2 + perro Ax):
#   · pick_future            -> reanclaje automatico al vencimiento mas lejano
#   · veto USDT              -> solo settle USD (Ley Carbono), fail-closed
#   · optimal_limit_ioc      -> entrada anti-deslizamiento
#   · attachAlgoOrds         -> SL/TP viajan DENTRO de la orden de entrada
#   · clOrdId sello de vela  -> idempotencia (OKX rechaza duplicado 51016)
#   · candle_already_traded  -> consulta historial antes de disparar
#                               (fail-closed: sin verificacion, no zarpa)
#   · anti-doble v4.2        -> posiciones + algos vivos = nave ocupada
#   · protection_confirmed   -> verifica SL/TP pendientes tras disparar
#   · emergency_close        -> si la coraza no aparece, cierre inmediato
#   · cooldown fillPnl       -> 60 min de calma tras perdida (herencia Ax5)
# ADAPTACIONES POR CARACTER ICP (auditoria 30d velas 1H reales OKX):
#   · SL = 1.2 x distancia EMA21, PISO 2.0% / TECHO 4.0%
#     (rango diario mediano 6.25%: el techo 3% del xpl se queda corto)
#   · TP = 1.5 x SL
#   · ventana de cruce: ultimas 2 velas cerradas (cron */20)
#   · candado de separacion >= 0.15%
# VARIABLES DE ENTORNO (todas opcionales):
#   SHADOW=1             sombra (defecto). 0 = municion real
#   DRY_RUN=1            freno de flota -> fuerza sombra
#   ICP_TF=1h            marco temporal
#   ICP_CONTRACTS=6      6 ct = 6 ICP ~ $16 (ctVal=1, minSz=1)
#   ICP_SEP_LOCK=0.15    candado de separacion %
#   ICP_SL_MULT=1.2      multiplicador distancia EMA21
#   ICP_SL_FLOOR=2.0     piso SL %
#   ICP_SL_CAP=4.0       techo SL %
#   ICP_TP_MULT=1.5      TP = x * SL
#   ICP_MAX_NOTIONAL=20  guardia de nocional $
#   COOLDOWN_MIN=60      calma tras perdida
# =====================================================================

import os
import sys
import time
import logging
from datetime import datetime, timezone

try:
    import ccxt
except ImportError:
    ccxt = None

VERSION = "v1.1"
TF_MS = {"5m": 300000, "15m": 900000, "30m": 1800000,
         "1h": 3600000, "2h": 7200000, "4h": 14400000, "6h": 21600000}

# ---------------- CONFIG ----------------
SHADOW = os.environ.get("SHADOW", "1") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
ICP_TF = os.environ.get("ICP_TF", "1h")
ICP_CONTRACTS = int(float(os.environ.get("ICP_CONTRACTS", "6")))
SEP_LOCK = float(os.environ.get("ICP_SEP_LOCK", "0.15"))
SL_MULT = float(os.environ.get("ICP_SL_MULT", "1.2"))
SL_FLOOR = float(os.environ.get("ICP_SL_FLOOR", "2.0"))
SL_CAP = float(os.environ.get("ICP_SL_CAP", "4.0"))
TP_MULT = float(os.environ.get("ICP_TP_MULT", "1.5"))
MAX_NOTIONAL = float(os.environ.get("ICP_MAX_NOTIONAL", "20.0"))
COOLDOWN_MIN = float(os.environ.get("COOLDOWN_MIN", "60"))
N_CANDLES = int(os.environ.get("ICP_N_CANDLES", "120"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("icp")


def fail(msg):
    log.error(msg)
    sys.exit(1)


# ---------------- EXCHANGE ----------------
def build_exchange():
    """ccxt OKX apuntando a la plataforma X-Perp (my.okx.com, colateral USD)."""
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
    ex.hostname = "my.okx.com"          # refuerzo del anclaje X-Perp
    return ex, bool(api_key and secret and passwd)


def pick_future(ex):
    """Reanclaje: vencimiento mas lejano de ICP/USD settle USD (Ley Carbono)."""
    ex.load_markets()
    cands = []
    for m in ex.markets.values():
        if m.get("base") != "ICP":
            continue
        if m.get("settle") != "USD":
            continue                      # veto USDT: solo USD puro
        if m.get("swap") or not m.get("future"):
            continue
        cands.append(m)
    if not cands:
        fail("[ERROR] Sin futuros ICP/USD settle USD en OKX. DETENIDO (Ley Carbono).")
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
            tot_eq = float(d0["totEq"])
    except Exception:
        pass
    if not tot_eq:
        tot_eq = float((bal.get("total") or {}).get("USD") or 0.0)
    try:
        host = str(ex.urls["api"]["private"]).replace("{hostname}", getattr(ex, "hostname", ""))
    except Exception:
        host = "?"
    log.info("Autenticacion OK | host=%s | Colateral real (USD): ~%.2f", host, tot_eq or 0.0)
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
    """SL/TP vivos (oco + conditional). Fail-closed: si no se puede mirar, DETENIDO."""
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
    p = (market.get("precision") or {}).get("price") or 0.001
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


# ---------------- SEGURIDAD PRE-DISPARO ----------------
def already_traded(ex, inst_id, cl_ord_id):
    try:
        r = ex.privateGetTradeOrdersHistory({"instId": inst_id, "limit": "20"})
    except Exception as e:
        fail("[ERROR] historial de ordenes inaccesible: %s — DETENIDO (fail-closed)"
             % str(e)[:200])
    for o in (r.get("data") or []):
        if o.get("clOrdId") == cl_ord_id:
            return True
    return False


def cooldown_active(ex, inst_id):
    """Calma tras perdida (fillPnl). Capa blanda: si falla, avisa y sigue."""
    try:
        r = ex.privateGetTradeFillPnl({"instId": inst_id})
        now = ex.milliseconds()
        for rec in (r.get("data") or []):
            ft = int(float(rec.get("fillTime") or 0))
            pnl = float(rec.get("pnl") or 0)
            if pnl < 0 and ft > 0 and (now - ft) < COOLDOWN_MIN * 60 * 1000:
                return True, pnl, ft
    except Exception as e:
        log.warning("cooldown no verificable (%s) — continuo [capa blanda]", str(e)[:120])
    return False, 0.0, 0


# ---------------- DISPARO ----------------
def emergency_close(ex, inst_id, entry_side, contracts):
    body = {
        "instId": inst_id, "tdMode": "cross", "posSide": "net",
        "side": "sell" if entry_side == "buy" else "buy",
        "ordType": "market", "sz": str(int(contracts)),
        "reduceOnly": True, "clOrdId": ("ICPE%d" % ex.milliseconds())[:32],
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
    cl = ("ICP%d" % bar_ms)[:32]

    csize = float(market.get("contractSize") or 1.0)
    notional = contracts * csize * px
    if notional > MAX_NOTIONAL:
        fail("[ERROR] nocional ~$%.2f > guardia $%.2f — DETENIDO"
             % (notional, MAX_NOTIONAL))
    if tot_eq is not None and notional * 3.0 > tot_eq:
        fail("[ERROR] margen ~$%.2f (1x) vs colateral ~$%.2f -> NO CABE. DETENIDO"
             % (notional, tot_eq))

    log.info("Disparo %s | %d ct | nocional ~$%.2f | SL %s (dist %.2f%%) | TP %s | clOrdId %s",
             "LONG" if long_dir else "SHORT", contracts, notional, sl_s, dist_pct, tp_s, cl)
    body = {
        "instId": inst_id, "tdMode": "cross", "posSide": "net",
        "side": side, "ordType": "optimal_limit_ioc",
        "sz": str(int(contracts)), "clOrdId": cl,
        "attachAlgoOrds": [{
            "attachAlgoClOrdId": ("ICPA%d" % bar_ms)[:32],
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
def main():
    shadow = SHADOW or DRY_RUN
    if DRY_RUN and not SHADOW:
        log.info("Freno de flota DRY_RUN=1 -> forzando SOMBRA")
    log.info("Modo ciclo unico | NAVIO ICP EMA3/21 %s %s + BLINDAJE xpl-v4.2 (XPERP USD) | %s",
             ICP_TF, VERSION, "SOMBRA" if shadow else "REAL")
    if ICP_TF not in TF_MS:
        fail("[ERROR] ICP_TF invalido: %s" % ICP_TF)

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

    candles = closed_candles(ex, sym, ICP_TF, N_CANDLES)
    sig = compute_signal(candles)
    log.info("Precio: %.3f | EMA3/21: %.3f/%.3f | %s (estado)",
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

    if shadow:
        log.info("[SOMBRA] Senal %s legal | vela %s UTC | sep %.2f%% | SL ~%.3f (dist %.2f%%) | "
                 "TP ~%.3f | %d ct — sin municion, solo bitacora",
                 dir_txt, bar_h, sig["sep"], sl, dist_pct, tp, ICP_CONTRACTS)
        return 0

    # ---- REAL ----
    try:
        ex.set_position_mode(False)
        log.info("Position mode -> one-way para %s.", inst_id)
    except Exception as e:
        log.warning("set_position_mode tolerado: %s", str(e)[:120])
    try:
        ex.set_leverage(1, sym, params={"mgnMode": "cross"})
        log.info("Apalancamiento: 1x (cross)")
    except Exception as e:
        log.warning("set_leverage tolerado: %s", str(e)[:120])

    active, pnl, ft = cooldown_active(ex, inst_id)
    if active:
        mins = int((ex.milliseconds() - ft) / 60000)
        log.info("Cooldown: perdida %.2f hace %d min — calma a bordo. Vigilando.", pnl, mins)
        return 0

    cl = ("ICP%d" % sig["bar_ts"])[:32]
    if already_traded(ex, inst_id, cl):
        log.info("Vela %s ya navegada (candle_already_traded) — descartado.", bar_h)
        return 0

    min_sz = (((market.get("limits") or {}).get("amount") or {}).get("min")) or 1
    if ICP_CONTRACTS < float(min_sz):
        fail("[ERROR] %d ct < minimo del mercado (%s) — DETENIDO" % (ICP_CONTRACTS, min_sz))

    fire(ex, market, inst_id, sig, ICP_CONTRACTS, tot_eq)
    return 0


if __name__ == "__main__":
    sys.exit(main())
