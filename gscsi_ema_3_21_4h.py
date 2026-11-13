#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =====================================================================
# inj_bot.py — NAVIO INJ · EL INYECTOR · v3.1-prod
# Autonomo: stdlib + ccxt. Host: my.okx.com (X-Perp). Colateral USD.
# VETO USDT: solo INJ/USD futuro fechado, quote USD, settle USD.
# Ciclo unico (el cron del workflow es el corazon). Modo PRODUCCION:
# SHADOW=0 por defecto. SHADOW=1 o DRY_RUN=1 -> sombra (nunca sandbox).
#
# v3.1-prod: ventana 0,-1,-2 (vela EN FORMACION + 2 cerradas, precio vivo),
#   apalancamiento 1x, contrato minimo, SL/TP por ATR14 (volatilidad) +
#   estructura (distancia a EMA lenta), R:R 1.5. Peaje 0.14% RT cubierto.
# CAMBIOS v3.0 sobre v2.2 (revision de produccion):
#  P0 · Senal sin persistencia: el cruce de la ventana (-2,-3,-4) podia
#       haberse REVERTIDO antes de entrar (entrabas contra la postura
#       actual). Ahora la postura debe mantenerse en todas las velas
#       posteriores al cruce (confirmacion real).
#  P0 · SL/TP y tamano se calculaban con el cierre de la vela vieja, no
#       con el precio vivo. Ahora ticker en vivo + guardia de deriva
#       (INJ_MAX_DRIFT %) -> si el precio ya se fue, no se persigue.
#  P0 · IOC sin fill => sin algos => el bot disparaba emergency_close
#       reduceOnly sin posicion (falsa alarma "intervencion manual").
#       Ahora: primero se verifica posicion; sin posicion = sin fill, ok.
#       Con posicion y sin algos (reintentos) -> cierre con tamano REAL.
#  P1 · set_leverage tolerado: con isolated, un fallo dejaba el apalan-
#       camiento por defecto de la cuenta. Ahora fail-closed, y se aplica
#       DESPUES de todas las compuertas (cooldown / ya navegada).
#  P1 · chequeo de margen "nocional*3" arbitrario -> margen real =
#       nocional/apalancamiento con colchon x2.
#  P1 · Veto USDT reforzado: id/simbolo con "USDT" = DETENIDO; hostname
#       verificado = my.okx.com.
#  NEW · EMA rapida/lenta parametrizables (INJ_EMA_FAST / INJ_EMA_SLOW)
#       para usar el ganador de sweep_ema.py. Defecto 3/21.
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

VERSION = "v3.1-prod"
HOST = "my.okx.com"
TF_MS = {"5m": 300000, "15m": 900000, "30m": 1800000,
         "1h": 3600000, "2h": 7200000, "4h": 14400000, "6h": 21600000}

# ---------------- CONFIG ----------------
SHADOW = os.environ.get("SHADOW", "0") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
INJ_TF = os.environ.get("INJ_TF", "1h")
INJ_WINDOW = tuple(int(x) for x in os.environ.get("INJ_WINDOW", "0,-1,-2").split(",") if x.strip())
EMA_FAST = int(os.environ.get("INJ_EMA_FAST", "3"))
EMA_SLOW = int(os.environ.get("INJ_EMA_SLOW", "21"))
SEP_LOCK = float(os.environ.get("INJ_SEP_LOCK", "0.17"))
SL_MULT = float(os.environ.get("INJ_SL_MULT", "1.2"))
SL_FLOOR = float(os.environ.get("INJ_SL_FLOOR", "1.5"))
SL_CAP = float(os.environ.get("INJ_SL_CAP", "3.0"))
TP_MULT = float(os.environ.get("INJ_TP_MULT", "1.5"))
INJ_NOTIONAL = float(os.environ.get("INJ_NOTIONAL", "12.0"))
SIZE_MODE = os.environ.get("INJ_SIZE_MODE", "min").strip().lower()   # min = contrato minimo | notional
MAX_NOTIONAL = float(os.environ.get("INJ_MAX_NOTIONAL", "15.0"))
LEVERAGE = int(float(os.environ.get("INJ_LEVERAGE", "1")))
MARGIN_MODE = os.environ.get("INJ_MARGIN_MODE", "isolated").strip().lower()
COOLDOWN_MIN = float(os.environ.get("COOLDOWN_MIN", "60"))
N_CANDLES = int(os.environ.get("INJ_N_CANDLES", "200"))
MAX_DRIFT = float(os.environ.get("INJ_MAX_DRIFT", "1.0"))   # % max vs cierre de senal
ATR_N = int(os.environ.get("INJ_ATR_N", "14"))
ATR_MULT = float(os.environ.get("INJ_ATR_MULT", "1.3"))   # SL = 1.3 x ATR14 (1h)
INJ_DEBUG = os.environ.get("INJ_DEBUG", "0") == "1"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("inj")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fail(msg):
    log.error(msg)
    sys.exit(1)


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
        "hostname": HOST,
        "enableRateLimit": True,
        "options": {"defaultType": "futures"},
    })
    ex.hostname = HOST
    if getattr(ex, "hostname", None) != HOST:
        fail("[ERROR] hostname != %s — DETENIDO" % HOST)
    return ex, bool(api_key and secret and passwd)


def pick_future(ex):
    """Vencimiento mas lejano de INJ/USD settle USD. Veto USDT triple + id."""
    ex.load_markets()
    cands = []
    for m in ex.markets.values():
        if m.get("base") != "INJ" or m.get("quote") != "USD":
            continue
        if m.get("settle") != "USD":
            continue
        if m.get("swap") or not m.get("future"):
            continue
        if "USDT" in str(m.get("id")).upper() or "USDT" in str(m.get("symbol")).upper():
            continue
        cands.append(m)
    if not cands:
        fail("[ERROR] Sin futuros INJ/USD settle USD en OKX. DETENIDO (veto USDT).")
    best = max(cands, key=lambda m: int(m.get("expiry") or 0))
    if best.get("settle") != "USD" or best.get("quote") != "USD" \
            or "USDT" in str(best.get("id")).upper():
        fail("[ERROR] Veto USDT: instrumento no valido. DETENIDO.")
    return best


def auth_and_collateral(ex, has_creds):
    if not has_creds:
        log.info("Sin credenciales — solo lectura de mercado")
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
            suma = sum(_f((bal.get(c) or {}).get("total")) for c in ("USD", "USDC", "USDT"))
            if suma > 0:
                tot_eq = suma
        except Exception:
            pass
    if not tot_eq:
        fail("[ERROR] Bodega ilegible o vacia — DETENIDO (fail-closed)")
    if INJ_DEBUG:
        log.info("Autenticacion OK | host=%s | Colateral (USD): %.2f", HOST, tot_eq)
    else:
        log.info("Autenticacion OK | host=%s | Colateral: OK (oculto; INJ_DEBUG=1)", HOST)
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
            return abs(float(c))
    return 0.0


def algo_pending(ex, inst_id):
    out = []
    for ot in ("oco", "conditional"):
        try:
            r = ex.privateGetTradeOrdersAlgoPending({"ordType": ot, "instId": inst_id})
            out.extend(r.get("data") or [])
        except Exception as e:
            fail("[ERROR] algo pending (%s) fallo: %s — DETENIDO (fail-closed)"
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



def read_series(ex, sym, tf, n):
    """Devuelve (cerradas, serie_completa, px_vivo). La serie completa agrega la
    vela EN FORMACION con cierre provisional = precio vivo (offset 0 de la ventana)."""
    if tf not in TF_MS:
        fail("[ERROR] marco temporal no soportado: %s" % tf)
    raw = ex.fetch_ohlcv(sym, tf, limit=n + 3)
    raw.sort(key=lambda c: c[0])
    now = ex.milliseconds()
    tfms = TF_MS[tf]
    closed = [c for c in raw if c[0] + tfms <= now]
    if len(closed) < max(40, EMA_SLOW * 2):
        fail("[ERROR] velas cerradas insuficientes (%d) — DETENIDO" % len(closed))
    closed = closed[-n:]
    px = live_price(ex, sym)
    live = [c for c in raw if c[0] + tfms > now]
    if live:
        f = list(live[-1]); f[4] = px; f[2] = max(f[2], px); f[3] = min(f[3], px)
    else:
        f = [closed[-1][0] + tfms, px, px, px, px, 0]
    return closed, closed + [f], px



def atr_pct(closed, n=None):
    """ATR(n) simple de velas CERRADAS, en % del ultimo cierre."""
    n = n or ATR_N
    trs = []
    for i in range(1, len(closed)):
        h, l, pc = float(closed[i][2]), float(closed[i][3]), float(closed[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    last = trs[-n:]
    return (sum(last) / len(last)) / float(closed[-1][4]) * 100.0


def compute_signal(full, n_closed, fast=None, slow=None, window=None):
    """Cruce rapida/lenta en la ventana. Offset 0 = vela EN FORMACION (precio vivo),
    -1 = ultima cerrada, -2 = penultima. Gana el mas reciente. La postura debe
    PERSISTIR hasta la vela en formacion (si se revirtio, no hay senal)."""
    fast = fast or EMA_FAST
    slow = slow or EMA_SLOW
    window = window if window is not None else INJ_WINDOW
    c = [float(x[4]) for x in full]
    m = len(c)
    n = n_closed                       # indice de la vela en formacion = n
    ef, es = ema(c, fast), ema(c, slow)
    stance = [1 if ef[i] > es[i] else -1 for i in range(m)]
    cross_idx, direction, window_off = None, 0, None
    for off in window:
        i = n + off
        if i < 1 or i > n:
            continue
        if stance[i] != stance[i - 1] and all(stance[j] == stance[i] for j in range(i, m)):
            cross_idx, direction, window_off = i, stance[i], off
            break
    sig = {"px": c[-1], "ref_px": c[n - 1], "e3": ef[-1], "e21": es[-1], "stance": stance[-1],
           "cross_idx": cross_idx, "dir": direction, "window_off": window_off,
           "sep": None, "e21_cross": None, "bar_ts": None,
           "atr_pct": atr_pct(full[:n])}
    if cross_idx is not None:
        sig["sep"] = abs(ef[cross_idx] - es[cross_idx]) / es[cross_idx] * 100.0
        sig["e21_cross"] = es[cross_idx]
        sig["bar_ts"] = int(full[cross_idx][0])
    return sig


def px_decimals(market):
    p = (market.get("precision") or {}).get("price") or 0.001
    d, x = 0, float(p)
    while x < 1.0 and d < 10:
        x *= 10.0
        d += 1
    return d



def sl_tp(px, e_slow_cross, long_dir, atr):
    """SL% = max(ATR_MULT x ATR14%, SL_MULT x distancia a EMA lenta del cruce),
    acotado a [SL_FLOOR, SL_CAP]. TP = TP_MULT x SL (R:R 1.5).
    Logica: el stop debe quedar fuera del ruido horario (ATR) y mas alla de la
    estructura (EMA lenta); el techo evita stops absurdos."""
    d_atr = ATR_MULT * atr
    d_str = abs(px - e_slow_cross) / px * 100.0 * SL_MULT
    sl_pct = min(max(SL_FLOOR, d_atr, d_str), SL_CAP)
    dist = px * sl_pct / 100.0
    if long_dir:
        return px - dist, px + dist * TP_MULT, sl_pct
    return px + dist, px - dist * TP_MULT, sl_pct


def live_price(ex, sym):
    try:
        t = ex.fetch_ticker(sym)
        px = _f(t.get("last")) or _f(t.get("close"))
    except Exception as e:
        fail("[ERROR] ticker ilegible: %s — DETENIDO (fail-closed)" % str(e)[:200])
    if px <= 0:
        fail("[ERROR] precio vivo invalido — DETENIDO (fail-closed)")
    return px


# ---------------- SEGURIDAD PRE-DISPARO ----------------
def already_traded(ex, inst_id, cl_ord_id):
    try:
        r = ex.privateGetTradeOrdersHistory({"instType": "FUTURES", "instId": inst_id, "limit": "100"})
    except Exception as e:
        fail("[ERROR] historial de ordenes inaccesible: %s — DETENIDO" % str(e)[:200])
    return any(o.get("clOrdId") == cl_ord_id for o in (r.get("data") or []))


def cooldown_active(ex, inst_id):
    """Doble fuente: positions-history -> fills-history. Ambas rotas = BLOQUEO."""
    now = ex.milliseconds()
    lim = COOLDOWN_MIN * 60 * 1000
    try:
        r = ex.privateGetAccountPositionsHistory(
            {"instType": "FUTURES", "instId": inst_id, "limit": "10"})
        for rec in (r.get("data") or []):
            ft = int(_f(rec.get("uTime")) or _f(rec.get("closedTime")) or _f(rec.get("cTime")))
            pnl = (_f(rec.get("realizedPnl")) if rec.get("realizedPnl") not in (None, "")
                   else _f(rec.get("pnl")))
            if pnl < 0 and ft > 0 and (now - ft) < lim:
                return True, pnl, ft
        return False, 0.0, 0
    except Exception as e:
        log.warning("positions-history no abrio (%s) — probando fills-history", str(e)[:120])
    try:
        r = ex.privateGetTradeFillsHistory(
            {"instType": "FUTURES", "instId": inst_id, "limit": "40"})
        for x in (r.get("data") or []):
            ts = int(_f(x.get("ts")) or _f(x.get("fillTime")))
            pnl = _f(x.get("fillPnl"))
            if pnl < 0 and ts > 0 and (now - ts) < lim:
                return True, pnl, ts
        return False, 0.0, 0
    except Exception as e:
        log.warning("cooldown ilegible por ambas fuentes (%s) — BLOQUEO", str(e)[:120])
        return True, 0.0, now


def pos_mode_check(ex):
    try:
        cfg = ex.privateGetAccountConfig()
        pos_mode = ((cfg.get("data") or [{}])[0]).get("posMode")
    except Exception as e:
        fail("[ERROR] posMode ilegible (%s) — DETENIDO" % str(e)[:120])
    if pos_mode != "net_mode":
        fail("[ERROR] Cuenta en %s — se requiere net_mode (se fija UNA vez en la app). DETENIDO" % pos_mode)
    return pos_mode


def size_contracts(market, px):
    csize = float(market.get("contractSize") or 0.0)
    if csize <= 0 or px <= 0:
        fail("[ERROR] ctVal/precio ilegible — DETENIDO")
    val_ct = csize * px
    min_sz = (((market.get("limits") or {}).get("amount") or {}).get("min")) or 1
    if SIZE_MODE == "min":
        n = max(1, int(float(min_sz)))          # contrato MINIMO del mercado
    else:
        n = max(1, int(INJ_NOTIONAL / val_ct))
        n = max(n, int(float(min_sz)))
    notional = n * val_ct
    if notional > MAX_NOTIONAL:
        fail("[ERROR] tamano %d ct ~ $%.2f > guardia $%.2f — DETENIDO" % (n, notional, MAX_NOTIONAL))
    return n, notional


# ---------------- DISPARO ----------------
def emergency_close(ex, inst_id, entry_side, contracts):
    body = {"instId": inst_id, "tdMode": MARGIN_MODE, "posSide": "net",
            "side": "sell" if entry_side == "buy" else "buy",
            "ordType": "market", "sz": str(int(contracts)),
            "reduceOnly": True, "clOrdId": ("INJE%d" % ex.milliseconds())[:32]}
    try:
        r = ex.privatePostTradeOrder(body)
        log.info("Cierre de emergencia enviado | code=%s", r.get("code"))
    except Exception as e:
        fail("[ERROR] cierre de emergencia fallo: %s — INTERVENCION MANUAL URGENTE" % str(e)[:200])


def fire(ex, market, sym, inst_id, sig, tot_eq):
    long_dir = sig["dir"] > 0
    side = "buy" if long_dir else "sell"

    px = live_price(ex, sym)
    drift = abs(px - sig["ref_px"]) / sig["ref_px"] * 100.0
    if drift > MAX_DRIFT:
        log.info("Precio vivo %.4f a %.2f%% del ultimo cierre (> %.2f%%) — no se persigue.",
                 px, drift, MAX_DRIFT)
        return 0
    if (sig["dir"] > 0) != (sig["stance"] > 0):
        log.info("La postura se revirtio al disparar — descartado.")
        return 0

    contracts, notional = size_contracts(market, px)
    sl, tp, dist_pct = sl_tp(px, sig["e21_cross"], long_dir, sig["atr_pct"])
    d = px_decimals(market)
    sl_s, tp_s = ("%." + str(d) + "f") % sl, ("%." + str(d) + "f") % tp
    bar_ms = sig["bar_ts"]
    cl = ("INJ%d" % bar_ms)[:32]

    margin = notional / max(1, LEVERAGE)
    if tot_eq is not None and margin * 2.0 > tot_eq:
        fail("[ERROR] margen ~$%.2f (%dx) x2 vs colateral NO CABE. DETENIDO" % (margin, LEVERAGE))

    try:
        ex.set_leverage(LEVERAGE, sym, params={"mgnMode": MARGIN_MODE})
        log.info("Apalancamiento: %dx (%s)", LEVERAGE, MARGIN_MODE)
    except Exception as e:
        fail("[ERROR] set_leverage fallo: %s — DETENIDO (fail-closed)" % str(e)[:160])

    log.info("Disparo %s | %d ct | nocional ~$%.2f (%dx %s) | px %.4f | SL %s (%.2f%%, ATR %.2f%%) | TP %s | %s",
             "LONG" if long_dir else "SHORT", contracts, notional, LEVERAGE,
             MARGIN_MODE, px, sl_s, dist_pct, sig["atr_pct"], tp_s, cl)
    body = {"instId": inst_id, "tdMode": MARGIN_MODE, "posSide": "net", "side": side,
            "ordType": "optimal_limit_ioc", "sz": str(int(contracts)), "clOrdId": cl,
            "attachAlgoOrds": [{"attachAlgoClOrdId": ("INJA%d" % bar_ms)[:32],
                                "slTriggerPx": sl_s, "slOrdPx": "-1",
                                "tpTriggerPx": tp_s, "tpOrdPx": "-1"}]}
    try:
        r = ex.privatePostTradeOrder(body)
    except Exception as e:
        fail("[ERROR] envio de orden fallo: %s — DETENIDO" % str(e)[:200])
    if str(r.get("code")) != "0":
        fail("[ERROR] OKX rechazo: code=%s msg=%s — DETENIDO" % (r.get("code"), r.get("msg")))
    od = (r.get("data") or [{}])[0]
    if str(od.get("sCode")) != "0":
        fail("[ERROR] OKX rechazo (sCode=%s sMsg=%s) — DETENIDO" % (od.get("sCode"), od.get("sMsg")))
    log.info("Orden aceptada | ordId=%s", od.get("ordId"))

    time.sleep(2)
    pos = open_position_contracts(ex, sym)
    if pos <= 0:
        log.info("IOC sin fill (sin posicion) — nada que proteger. La vela queda sellada por clOrdId.")
        return 0
    algos = []
    for _ in range(3):
        algos = [a for a in algo_pending(ex, inst_id) if a.get("instId") == inst_id]
        if algos:
            break
        time.sleep(2)
    if not algos:
        log.error("ALARMA: posicion %.0f ct SIN SL/TP pendientes", pos)
        emergency_close(ex, inst_id, side, pos)
        fail("Nave sin coraza — cerrada por seguridad.")
    a0 = algos[0]
    log.info("Nave blindada: %.0f ct | SL trig %s | TP trig %s",
             pos, a0.get("slTriggerPx"), a0.get("tpTriggerPx"))
    return 0


# ---------------- MAIN ----------------
def main():
    shadow = SHADOW or DRY_RUN
    log.info("Ciclo unico | EL INYECTOR EMA%d/%d %s %s (XPERP USD, veto USDT) | %s",
             EMA_FAST, EMA_SLOW, INJ_TF, VERSION, "SOMBRA" if shadow else "REAL")
    if INJ_TF not in TF_MS:
        fail("[ERROR] INJ_TF invalido: %s" % INJ_TF)
    if not INJ_WINDOW or any(o > 0 for o in INJ_WINDOW):
        fail("[ERROR] INJ_WINDOW invalido: %s" % (INJ_WINDOW,))
    if not (1 <= EMA_FAST < EMA_SLOW):
        fail("[ERROR] EMA invalidas: %d/%d" % (EMA_FAST, EMA_SLOW))
    if MARGIN_MODE not in ("isolated", "cross"):
        fail("[ERROR] INJ_MARGIN_MODE invalido: %s" % MARGIN_MODE)

    ex, has_creds = build_exchange()
    if not shadow and not has_creds:
        fail("[ERROR] modo REAL sin credenciales OKX — DETENIDO")
    market = pick_future(ex)
    inst_id, sym = market["id"], market["symbol"]
    log.info("Instrumento: %s | settle %s | ctVal %s %s",
             inst_id, market.get("settle"), market.get("contractSize"), market.get("base"))
    tot_eq = auth_and_collateral(ex, has_creds)

    if has_creds:
        pos_c = open_position_contracts(ex, sym)
        algos = algo_pending(ex, inst_id)
        if pos_c or algos:
            log.info("Nave ocupada: %.0f ct | %d algos vivos — vigilando.", pos_c, len(algos))
            return 0

    closed, full, _px = read_series(ex, sym, INJ_TF, N_CANDLES)
    sig = compute_signal(full, len(closed))
    log.info("Precio vivo: %.4f | ATR14 1h: %.2f%% | EMA%d/%d: %.4f/%.4f | %s", sig["px"], sig["atr_pct"], EMA_FAST, EMA_SLOW,
             sig["e3"], sig["e21"], "rapida > lenta" if sig["stance"] > 0 else "rapida < lenta")

    if sig["cross_idx"] is None:
        log.info("Sin cruce legal y persistente en la ventana %s. Vigilando.", list(INJ_WINDOW))
        return 0
    if sig["sep"] < SEP_LOCK:
        log.info("Separacion %.3f%% < candado %.2f%% — descartado.", sig["sep"], SEP_LOCK)
        return 0

    dir_txt = "LONG" if sig["dir"] > 0 else "SHORT"
    bar_h = datetime.fromtimestamp(sig["bar_ts"] / 1000, tz=timezone.utc).strftime("%d-%m %H:%M")

    if shadow:
        contracts, est = size_contracts(market, sig["px"])
        sl, tp, dist_pct = sl_tp(sig["px"], sig["e21_cross"], sig["dir"] > 0, sig["atr_pct"])
        log.info("[SOMBRA] %s legal | cruce %s (%s UTC) | sep %.2f%% | SL ~%.4f TP ~%.4f | %d ct ~$%.2f",
                 dir_txt, "EN FORMACION" if sig["window_off"] == 0 else "hace %d cerrada(s)" % -sig["window_off"], bar_h, sig["sep"], sl, tp, contracts, est)
        return 0

    pos_mode_check(ex)
    active, pnl, ft = cooldown_active(ex, inst_id)
    if active:
        log.info("Cooldown: perdida %.2f hace %d min — calma a bordo.",
                 pnl, int((ex.milliseconds() - ft) / 60000))
        return 0
    if already_traded(ex, inst_id, ("INJ%d" % sig["bar_ts"])[:32]):
        log.info("Vela %s ya navegada — descartado.", bar_h)
        return 0

    log.info("Senal %s | cruce %s UTC | sep %.2f%%", dir_txt, bar_h, sig["sep"])
    return fire(ex, market, sym, inst_id, sig, tot_eq)


if __name__ == "__main__":
    sys.exit(main())
