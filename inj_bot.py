#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =====================================================================
# inj_bot.py — NAVIO INJ · LA FLOTA CARBONO · BUQUE: EL INYECTOR
# 100% AUTONOMO: solo stdlib + ccxt (pip). NO importa ningun otro archivo
# del repo — navega solo en un repo recien creado. Sin pandas, sin sandbox.
# Casco: blindaje certificado xpl v4.2 · Motor: EMA 3/21 · Marco: 1h
# Instrumento: INJ-USD XPERP (futuro fechado USD, vencimiento mas lejano)
#
# v2.0-1h-xperp — MICROSCOPIO DE BARRIDO sobre el inj_bot.py del capitan
# (2026-10-27, "mi moneda preferida"). Hallazgos y soldadura aplicada:
#
#  P0 · SYMBOL hardcodeado INJ/USD:USD-310711 -> clavo fijo: si OKX rota o
#       re-lista el vencimiento, el bot muere apuntando a un mercado muerto
#       sin dejar rastro. Verificado hoy contra my.okx.com:
#       INJ-USD_UM_XPERP-310711 | live | ctVal 0.1 INJ | tickSz 0.001 | settle USD.
#       FIX: pick_future reanclaje automatico al vencimiento mas lejano.
#  P0 · pandas como dependencia -> gordo y fragil en CI (version drift);
#       ley de flota: 100% autonomo (stdlib + ccxt). FIX: velas nativas ccxt.
#  P0 · DRY_RUN -> sandbox (host demo) -> el freno no puede cambiar de
#       planeta: host demo + llaves reales = ordenes a un mundo sin fondos.
#       FIX: DRY_RUN fuerza SOMBRA sobre el host real; jamas toca el sandbox.
#  P1 · run_forever + sleep -> quemador de minutos de Actions y bucle eterno.
#       FIX: ciclo unico SINGLE_RUN; el cron del workflow es el corazon.
#  P1 · blindaje v4.2 incompleto -> soldado a tope: attachAlgoOrds (SL/TP
#       dentro de la entrada), clOrdId sello de vela (OKX 51016),
#       candle_already_traded (fail-closed), anti-doble (posicion + algos
#       vivos), protection_confirmed + emergency_close, bodega ilegible =
#       DETENIDO (fail-closed Ax3), cooldown fillPnl fail-closed
#       (conservado del original del capitan: ya leia fillPnl bien).
#  OK · DISENO DEL CAPITAN RESPETADO y validado por el microscopio numerico:
#       marco 1h | candado sep 0.17% | ventana (-2,-3,-4) | nocional $12
#       (guardia $15) | 2x aislado | SL 2.0% / TP 3.0%.
#
# v2.1-1h-xperp — soldadura tras la 1a ronda REAL del FARO (06:06 UTC):
#   P0 · cooldown_active llamaba a ex.privateGetTradeFillPnl — ESE METODO
#        NO EXISTE en ccxt 4.5.90 (los fillPnl de OKX REST ya solo bajan
#        por WebSocket). AttributeError silencioso -> fail-closed ->
#        BLOQUEO PERMANENTE: con señal legal, el barco nunca dispara.
#        FIX: fuente REST certificada /api/v5/account/positions-history
#        (PnL realizado de la posicion cerrada, campo realizedPnl del
#        parser ccxt). Con SL/TP atomicos (cierre completo en una pieza)
#        la semantica de calma es identica. FAIL-CLOSED conservado.
#
# MICROSCOPIO NUMERICO (velas reales OKX INJ-USDT-SWAP publico, 90 dias,
# 2159 velas 1h, peaje 0.14% RT, entrada en open siguiente, SL-first):
#   · ATR14 1h: 1.632% — INJ aletea 4x mas que ADA en 15m (0.388%)
#   · 156 cruces EMA3/21 (1.7/dia) — sep en cruce: P50 0.196% / P75 0.378%
#   · SIN candado (SL 2.0/TP 3.0): 156 trades, +6.16%, PF 1.03 — el peaje
#     tritura; el candado es vida, aqui tambien
#   · CON CANDADO DEL CAPITAN 0.17: 86 trades | WR 50.0% | net +30.96%/90d
#     | PF 1.34 | maxCons 10 — la curva del candado hace pico en 0.15-0.17
#   · referencia auditor (NO adoptada): lock 0.40/SL 1.0/TP 1.5 -> PF 1.49
#     pero solo 36 trades y SL 0.6x ATR14 = cuchilla sobre mar de 1.6% ATR
#     (sobreajuste). Conservador: lock 0.20/SL 4.0/TP 6.0 +35.51% PF 1.23.
#   · maxCons 10 x riesgo/trade (~$0.24 con nocional $12) ~ -$2.4 de racha:
#     dentro de la guardia de flota
#
# BLINDAJE A BORDO (herencia xpl v4.2 + perro Ax, fixes de flota P0 soldados):
#   · pick_future            -> reanclaje automatico al vencimiento mas lejano
#   · veto USDT TRIPLE       -> quote USD + settle USD + futuro (no swap), fail-closed
#   · optimal_limit_ioc      -> entrada anti-deslizamiento
#   · attachAlgoOrds         -> SL/TP viajan DENTRO de la orden de entrada
#   · clOrdId sello de vela  -> idempotencia (OKX rechaza duplicado 51016)
#   · candle_already_traded  -> consulta historial antes de disparar
#                               (fail-closed: sin verificacion, no zarpa)
#   · anti-doble v4.2        -> posiciones + algos vivos = nave ocupada
#   · protection_confirmed   -> verifica SL/TP pendientes tras disparar
#   · emergency_close        -> si la coraza no aparece, cierre inmediato
#   · cooldown v2.1          -> PnL realizado via positions-history REST
#                               (fillPnl solo baja por WebSocket). FAIL-CLOSED:
#                               consulta rota = BLOQUEO
#   · bodega ilegible        -> totEq/eqUsd ilegible = DETENIDO (fail-closed Ax3)
#   · host resuelto          -> log sin plantilla cosmica {hostname}
#
# VARIABLES DE ENTORNO (todas opcionales):
#   SHADOW=1             sombra (defecto). 0 = municion real
#   DRY_RUN=1            freno de flota -> fuerza sombra (nunca sandbox)
#   INJ_TF=1h            marco temporal (diseno del capitan)
#   INJ_WINDOW=-2,-3,-4  ventana de vela del cruce (diseno del capitan:
#                        el cruce debe sobrevivir 1 vela de confirmacion;
#                        el orden define prioridad, gana el mas reciente)
#   INJ_SEP_LOCK=0.17    candado de separacion % (diseno del capitan, auditado)
#   INJ_SL_FLOOR=2.0     piso SL % (auditado 1h: 1.23 x ATR14)
#   INJ_SL_CAP=2.0       techo SL % (piso=techo -> SL fijo 2.0%)
#   INJ_TP_MULT=1.5      TP = x * SL -> 3.0%
#   INJ_NOTIONAL=12      nocional objetivo $ por operacion (capitan)
#   INJ_MAX_NOTIONAL=15  guardia de nocional $ (capitan)
#   INJ_LEVERAGE=2       apalancamiento (capitan) con margen aislado
#   INJ_MARGIN_MODE=isolated  margen aislado (capitan)
#   INJ_N_CANDLES=200    velas cerradas para las EMAs
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

VERSION = "v2.1-1h-xperp-microscopio"
TF_MS = {"5m": 300000, "15m": 900000, "30m": 1800000,
         "1h": 3600000, "2h": 7200000, "4h": 14400000, "6h": 21600000}

# ---------------- CONFIG ----------------
SHADOW = os.environ.get("SHADOW", "1") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
INJ_TF = os.environ.get("INJ_TF", "1h")
INJ_WINDOW = tuple(int(x) for x in os.environ.get("INJ_WINDOW", "-2,-3,-4").split(",") if x.strip())
SEP_LOCK = float(os.environ.get("INJ_SEP_LOCK", "0.17"))
SL_MULT = float(os.environ.get("INJ_SL_MULT", "1.2"))
SL_FLOOR = float(os.environ.get("INJ_SL_FLOOR", "2.0"))
SL_CAP = float(os.environ.get("INJ_SL_CAP", "2.0"))
TP_MULT = float(os.environ.get("INJ_TP_MULT", "1.5"))
INJ_NOTIONAL = float(os.environ.get("INJ_NOTIONAL", "12.0"))
MAX_NOTIONAL = float(os.environ.get("INJ_MAX_NOTIONAL", "15.0"))
LEVERAGE = int(float(os.environ.get("INJ_LEVERAGE", "2")))
MARGIN_MODE = os.environ.get("INJ_MARGIN_MODE", "isolated").strip().lower()
COOLDOWN_MIN = float(os.environ.get("COOLDOWN_MIN", "60"))
N_CANDLES = int(os.environ.get("INJ_N_CANDLES", "200"))

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
    """Reanclaje: vencimiento mas lejano de INJ/USD settle USD (Ley Carbono).
    Veto USDT TRIPLE: quote USD, settle USD y futuro fechado (no perp swap).
    Sustituye al SYMBOL clavado del bot original (hallazgo P0 del microscopio)."""
    ex.load_markets()
    cands = []
    for m in ex.markets.values():
        if m.get("base") != "INJ":
            continue
        if m.get("quote") != "USD":
            continue                      # veto USDT (1): quote != USD
        if m.get("settle") != "USD":
            continue                      # veto USDT (2): settle != USD
        if m.get("swap") or not m.get("future"):
            continue                      # veto (3): solo futuro fechado
        cands.append(m)
    if not cands:
        fail("[ERROR] Sin futuros INJ/USD settle USD en OKX. DETENIDO (Ley Carbono).")
    best = max(cands, key=lambda m: int(m.get("expiry") or 0))
    if best.get("settle") != "USD" or best.get("quote") != "USD":
        fail("[ERROR] Veto Ley Carbono: quote/settle != USD. DETENIDO.")
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
    # 1) totEq del crudo OKX (equidad total en USD), si existe y es > 0
    try:
        d0 = bal.get("info", {}).get("data", [{}])[0]
        if d0.get("totEq"):
            tot_eq = _f(d0["totEq"])
    except Exception:
        pass
    # 2) METODO DEL PERRO: suma de eqUsd por divisa en details
    if not tot_eq:
        try:
            details = bal.get("info", {}).get("data", [{}])[0].get("details") or []
            suma = sum(_f(d.get("eqUsd")) for d in details)
            if suma > 0:
                tot_eq = suma
        except Exception:
            pass
    # 3) Ultimo recurso: bolsillos unificados reales (USD/USDC/USDT)
    if not tot_eq:
        try:
            suma = sum(_f((bal.get(c) or {}).get("total"))
                       for c in ("USD", "USDC", "USDT"))
            if suma > 0:
                tot_eq = suma
        except Exception:
            pass
    # 4) Decreto Ax3: bodega ilegible NO navega (fail-closed, nunca fail-open).
    if tot_eq is None:
        fail("[ERROR] Bodega ilegible (totEq/eqUsd/bolsillos vacios) — DETENIDO (fail-closed)")
    try:
        host = str(ex.urls.get("api", {}).get("rest", "?"))   # ccxt 4.x: 'rest'
        if "{hostname}" in host:                              # fix host cosmico
            host = host.replace("{hostname}", getattr(ex, "hostname", "my.okx.com"))
    except Exception:
        host = "?"
    log.info("Autenticacion OK | host=%s | Colateral real (USD): ~%.2f", host, tot_eq)
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
    """Velas CERRADAS (la vela en formacion se descarta siempre).
    Nota OKX: ccxt acepta '1h' y lo traduce a bar '1H' — el mapa TF_MS
    gobierna el descarte de vela viva."""
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
    """Cruce EMA3/21 dentro de la ventana del capitan (-2,-3,-4): el cruce
    debe sobrevivir 1 vela de confirmacion; gana el mas reciente legal."""
    c = [float(x[4]) for x in candles]
    n = len(c)
    e3 = ema(c, 3)
    e21 = ema(c, 21)
    stance = [1 if e3[i] > e21[i] else -1 for i in range(n)]
    cross_idx, direction, window_off = None, 0, None
    for off in INJ_WINDOW:                     # p.ej. -2,-3,-4 (mas reciente primero)
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
    """SL = SL_MULT x dist EMA21 con piso/techo %; TP = TP_MULT x SL.
    Microscopio 1h: piso = techo = 2.0% -> SL fijo 2.0% (1.23 x ATR14 1.632%),
    TP 3.0%. Con el candado del capitan 0.17: PF 1.34 / 90d."""
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
    """Calma tras perdida. FIX v2.1 (ronda real del FARO 06:06): los fills
    REST de OKX ya no traen PnL (fillPnl solo por WebSocket) y el metodo
    privateGetTradeFillPnl NO existe en ccxt 4.5.90 -> AttributeError
    silencioso = bloqueo permanente con senal legal. Fuente REST
    certificada: /api/v5/account/positions-history (PnL realizado de la
    posicion cerrada). Con SL/TP atomicos (cierre completo en una pieza)
    la semantica es identica a la de fills. FAIL-CLOSED: si la consulta
    falla, BLOQUEO — no se dispara con cooldown ilegible."""
    try:
        r = ex.privateGetAccountPositionsHistory(
            {"instType": "FUTURES", "instId": inst_id, "limit": "10"})
        now = ex.milliseconds()
        for rec in (r.get("data") or []):
            ft = int(_f(rec.get("uTime")) or _f(rec.get("closedTime"))
                     or _f(rec.get("cTime")))
            pnl = _f(rec.get("realizedPnl"))
            if pnl < 0 and ft > 0 and (now - ft) < COOLDOWN_MIN * 60 * 1000:
                return True, pnl, ft
    except Exception as e:
        log.warning("cooldown no verificable (%s) — BLOQUEO fail-closed", str(e)[:120])
        return True, 0.0, ex.milliseconds()
    return False, 0.0, 0


def size_contracts(market, px):
    """Tamano por nocional objetivo $ (diseno del capitan): 1 contrato XPERP
    INJ = ctVal 0.1 INJ -> valor $ = 0.1 x px. Se redondea POR DEBAJO y la
    guardia INJ_MAX_NOTIONAL manda (fail-closed)."""
    csize = float(market.get("contractSize") or 0.0)
    if csize <= 0:
        fail("[ERROR] ctVal ilegible del mercado — DETENIDO (fail-closed)")
    val_ct = csize * px
    if val_ct <= 0:
        fail("[ERROR] valor por contrato ilegible — DETENIDO (fail-closed)")
    n = int(INJ_NOTIONAL / val_ct)
    if n < 1:
        n = 1
    min_sz = (((market.get("limits") or {}).get("amount") or {}).get("min")) or 1
    if n < float(min_sz):
        n = int(float(min_sz))
    notional = n * val_ct
    if notional > MAX_NOTIONAL:
        fail("[ERROR] tamano %d ct ~ $%.2f > guardia $%.2f — DETENIDO"
             % (n, notional, MAX_NOTIONAL))
    return n, notional


# ---------------- DISPARO ----------------
def emergency_close(ex, inst_id, entry_side, contracts):
    body = {
        "instId": inst_id, "tdMode": MARGIN_MODE, "posSide": "net",
        "side": "sell" if entry_side == "buy" else "buy",
        "ordType": "market", "sz": str(int(contracts)),
        "reduceOnly": True, "clOrdId": ("INJE%d" % ex.milliseconds())[:32],
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
    cl = ("INJ%d" % bar_ms)[:32]

    csize = float(market.get("contractSize") or 1.0)
    notional = contracts * csize * px
    if notional > MAX_NOTIONAL:
        fail("[ERROR] nocional ~$%.2f > guardia $%.2f — DETENIDO"
             % (notional, MAX_NOTIONAL))
    if tot_eq is not None and notional * 3.0 > tot_eq:
        fail("[ERROR] margen ~$%.2f (%dx) vs colateral ~$%.2f -> NO CABE. DETENIDO"
             % (notional / max(1, LEVERAGE), LEVERAGE, tot_eq))

    log.info("Disparo %s | %d ct | nocional ~$%.2f (%dx %s) | SL %s (dist %.2f%%) | "
             "TP %s | clOrdId %s",
             "LONG" if long_dir else "SHORT", contracts, notional, LEVERAGE,
             MARGIN_MODE, sl_s, dist_pct, tp_s, cl)
    body = {
        "instId": inst_id, "tdMode": MARGIN_MODE, "posSide": "net",
        "side": side, "ordType": "optimal_limit_ioc",
        "sz": str(int(contracts)), "clOrdId": cl,
        "attachAlgoOrds": [{
            "attachAlgoClOrdId": ("INJA%d" % bar_ms)[:32],
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
        log.info("Freno de flota DRY_RUN=1 -> forzando SOMBRA (nunca sandbox)")
    log.info("Modo ciclo unico | NAVIO INJ EL INYECTOR EMA3/21 %s %s + BLINDAJE xpl-v4.2 (XPERP USD) | %s",
             INJ_TF, VERSION, "SOMBRA" if shadow else "REAL")
    if INJ_TF not in TF_MS:
        fail("[ERROR] INJ_TF invalido: %s" % INJ_TF)
    if not INJ_WINDOW or any(o > -1 for o in INJ_WINDOW):
        fail("[ERROR] INJ_WINDOW invalido: %s (se esperaba p.ej. -2,-3,-4)" % (INJ_WINDOW,))
    if MARGIN_MODE not in ("isolated", "cross"):
        fail("[ERROR] INJ_MARGIN_MODE invalido: %s" % MARGIN_MODE)

    ex, has_creds = build_exchange()
    if not shadow and not has_creds:
        fail("[ERROR] modo REAL sin credenciales OKX — DETENIDO")
    market = pick_future(ex)
    inst_id = market["id"]
    sym = market["symbol"]
    log.info("Instrumento: %s (XPERP — vencimiento mas lejano | settle %s | ctVal %s %s)",
             inst_id, market.get("settle"), market.get("contractSize"),
             market.get("base"))
    tot_eq = auth_and_collateral(ex, has_creds)

    if has_creds:
        pos_c = open_position_contracts(ex, sym)
        algos = algo_pending(ex, inst_id)
        if pos_c or algos:
            log.info("Nave ocupada: %.0f ct en posicion | %d algos vivos — no se abre nada. Vigilando.",
                     pos_c, len(algos))
            return 0

    candles = closed_candles(ex, sym, INJ_TF, N_CANDLES)
    sig = compute_signal(candles)
    log.info("Precio: %.4f | EMA3/21: %.4f/%.4f | %s (estado)",
             sig["px"], sig["e3"], sig["e21"],
             "EMA3 > EMA21" if sig["stance"] > 0 else "EMA3 < EMA21")

    if sig["cross_idx"] is None:
        log.info("Sin cruce EMA3/21 legal en la ventana %s. Vigilando.", list(INJ_WINDOW))
        return 0

    if sig["sep"] < SEP_LOCK:
        log.info("Cruce con separacion %.3f%% < candado %.2f%% — descartado. Vigilando.",
                 sig["sep"], SEP_LOCK)
        return 0

    dir_txt = "LONG" if sig["dir"] > 0 else "SHORT"
    sl, tp, dist_pct = sl_tp(sig["px"], sig["e21_cross"], sig["dir"] > 0)
    bar_h = datetime.fromtimestamp(sig["bar_ts"] / 1000, tz=timezone.utc).strftime("%d-%m %H:%M")
    age = -sig["window_off"]
    contracts, est_notional = size_contracts(market, sig["px"])

    if shadow:
        log.info("[SOMBRA] Senal %s legal | cruce hace %d vela(s) (%s UTC) | sep %.2f%% | "
                 "SL ~%.4f (dist %.2f%%) | TP ~%.4f | %d ct ~ $%.2f — sin municion, solo bitacora",
                 dir_txt, age, bar_h, sig["sep"], sl, dist_pct, tp, contracts, est_notional)
        return 0

    # ---- REAL ----
    try:
        ex.set_position_mode(False)
        log.info("Position mode -> one-way para %s.", inst_id)
    except Exception as e:
        log.warning("set_position_mode tolerado: %s", str(e)[:120])
    try:
        ex.set_leverage(LEVERAGE, sym, params={"mgnMode": MARGIN_MODE})
        log.info("Apalancamiento: %dx (%s)", LEVERAGE, MARGIN_MODE)
    except Exception as e:
        log.warning("set_leverage tolerado: %s", str(e)[:120])

    active, pnl, ft = cooldown_active(ex, inst_id)
    if active:
        mins = int((ex.milliseconds() - ft) / 60000)
        log.info("Cooldown: perdida %.2f hace %d min — calma a bordo. Vigilando.", pnl, mins)
        return 0

    cl = ("INJ%d" % sig["bar_ts"])[:32]
    if already_traded(ex, inst_id, cl):
        log.info("Vela %s ya navegada (candle_already_traded) — descartado.", bar_h)
        return 0

    fire(ex, market, inst_id, sig, contracts, tot_eq)
    return 0


if __name__ == "__main__":
    sys.exit(main())
