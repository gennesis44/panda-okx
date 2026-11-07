#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =====================================================================
# avax-go.py — NAVIO AVAX · LA FLOTA CARBONO · BUQUE: EL OCTAVO
# REBAUTIZO 07-11-2026: pabellon avax-go.py — casco IDENTICO al certificado
# 35/35 (avax_bot.py v2.2-1h-xperp-soldaduras-avax). Cero cambios de logica.
# 100% AUTONOMO: solo stdlib + ccxt (pip). NO importa ningun otro archivo
# del repo — navega solo en un repo recien creado. Sin pandas, sin sandbox.
# Casco: chasis Catamaran del capitan (Ax2) reblindado a v2.2 · Motor: EMA 3/21
# Marco: 1h · Instrumento: AVAX-USD XPERP (futuro fechado USD, venc. mas lejano)
#
# v2.2-1h-xperp-soldaduras-avax — DOCTORADO EN TRADING Y FORENSE sobre el
# main-avax.py del capitan (Ax2 "EL OCTAVO", chasis Catamaran SUI 1:1).
# El informe certificado hallazgos P0 que aqui quedan soldados, y el
# capitan pidio una cosa mas: SABER EL WIN RATE. Telemetria de flota nueva.
#
#  P0 · COOLDOWN FAIL-OPEN (hallazgo forense #1): el cooldown del Ax2 leia
#       fill.get('pnl') — ESE CAMPO NO EXISTE en fills-history de OKX (el
#       campo real es fillPnl). pnl = 0.0 siempre -> 'pnl < 0' jamas se
#       cumple -> tras una perdida el barco VOLVIA A DISPARAR en la misma
#       hora. Con SL 4%/TP 5.5% y 1h, un mercado que aletea puede encadenar
#       perdidas sin calma: ES EL PEAJE OCULTO. FIX v2.2 (heredado del
#       INYECTOR certificado): DOBLE FUENTE — 1) positions-history
#       (realizedPnl, con pnl de reserva) -> 2) fills-history (fillPnl,
#       limit 40) -> ambas rotas: BLOQUEO fail-closed. Certificado contra
#       el cuerpo pelado '0' (KeyError :: 0) de my.okx.com.
#  P0 · set_position_mode(False, symbol) (Ax4 panda) ELIMINADO: muta el
#       posMode de la cuenta ENTERA (toda la flota de golpe). Sustituido
#       por pos_mode_check: lee /api/v5/account/config; si no es net_mode
#       -> DETENIDO fail-closed. El codigo JAMAS toca el interruptor.
#  P0 · pandas ELIMINADO (gordo y fragil en CI): velas nativas ccxt +
#       EMAs stdlib. Ley de flota: 100% autonomo (stdlib + ccxt).
#  P1 · SL 4.0% / TP 5.5% del Ax2 estaban validados para EMA8/34 (backtest
#       27-sep del capitan: WR 37.0%, PF 1.57) y su propia nota decia
#       REVALIDAR para 3/21. FIX: calibracion de flota 1h con candado:
#       SL fijo 2.0% / TP 3.0% (R:R 1.5 -> WR equilibrio 40.0%), el mismo
#       motor auditado que el INYECTOR (86 trades, WR 50.0%, PF 1.34/90d
#       con candado 0.17). La telemetria WIN RATE mide el WR REAL de
#       AVAX contra ese 40%: si el WR real no lo cubre, se calibra de nuevo.
#  P1 · TARGET_NOTIONAL 7.5 < minimo tradable: 1 ct = 10 AVAX (~$86.7 al
#       fichaje) -> ticket minimo 0.1 ct (~$8.67). FIX: tallado honesto
#       Ax3.3 (ctVal/lotSz/minSz SIEMPRE de la API), objetivo $10 -> 0.1 ct,
#       y guardia $15 fail-closed (Ax3.2: unidad sorpresa = barco parado).
#  MINOR · sort defensivo de velas por ts; colateral enmascarado en log
#       (repo publica; AVAX_DEBUG=1 para diagnostico puntual).
#
#  TELEMETRIA WIN RATE (nueva, peticion del capitan): cada vigilia imprime
#  la bitacora WIN RATE de AVAX con datos REALES de OKX:
#    · neto por cierre = realizedPnl + fee + fundingFee + liqPenalty
#      (el peaje DENTRO del resultado — sin contabilidad de humo)
#    · WR %, net acumulado, peaje acumulado, avg win/loss, expectancia
#      $/trade y WR de equilibrio teorico (R:R 1.5 -> 40.0%)
#    · doble fuente: positions-history -> fills-history (aprox por fill).
#      Ambas rotas -> aviso y sigue: la telemetria es BITACORA, jamas
#      puerta de disparo (la puerta es cooldown_active, fail-closed aparte).
#
# BLINDAJE A BORDO (herencia Ax2/Ax3 del capitan + soldaduras de flota):
#   · pick_future            -> reanclaje automatico al vencimiento mas lejano
#   · veto USDT TRIPLE       -> quote USD + settle USD + futuro fechado (MiCA/EEE)
#   · optimal_limit_ioc      -> entrada anti-deslizamiento
#   · attachAlgoOrds         -> SL/TP viajan DENTRO de la orden de entrada
#   · clOrdId sello de vela  -> idempotencia (OKX rechaza duplicado 51016)
#   · candle_already_traded  -> consulta historial antes de disparar (fail-closed)
#   · anti-doble             -> posiciones + algos vivos = nave ocupada
#   · protection_confirmed   -> verifica SL/TP pendientes tras disparar
#   · emergency_close        -> si la coraza no aparece, cierre inmediato
#   · cooldown doble fuente  -> positions-history + fills-history. Ambas
#                               rotas = BLOQUEO fail-closed (banco verde)
#   · posMode check          -> se COMPRUEBA net_mode, jamas se muta la cuenta
#   · bodega ilegible        -> totEq/eqUsd ilegible = DETENIDO (fail-closed Ax3)
#
# VARIABLES DE ENTORNO (todas opcionales):
#   SHADOW=1             sombra (defecto). 0 = municion real
#   DRY_RUN=1            freno de flota -> fuerza sombra (nunca sandbox)
#   AVAX_TF=1h           marco temporal (diseno del capitan)
#   AVAX_WINDOW=-2,-3,-4 ventana de vela del cruce (capitan: el cruce debe
#                        sobrevivir 1 vela de confirmacion; gana el mas reciente)
#   AVAX_SEP_LOCK=0.17   candado de separacion % (capitan, el cruce NO basta)
#   AVAX_SL_FLOOR=2.0    piso SL % (calibracion de flota 1h)
#   AVAX_SL_CAP=2.0      techo SL % (piso=techo -> SL fijo 2.0%)
#   AVAX_TP_MULT=1.5     TP = x * SL -> 3.0% (R:R 1.5)
#   AVAX_NOTIONAL=10     nocional objetivo $ (ticket real: 0.1 ct minimo)
#   AVAX_MAX_NOTIONAL=15 guardia de nocional $ (Ax3.2)
#   AVAX_LEVERAGE=2      apalancamiento con margen aislado
#   AVAX_MARGIN_MODE=isolated  margen aislado (Ax3)
#   AVAX_N_CANDLES=200   velas cerradas para las EMAs
#   COOLDOWN_MIN=60      calma tras perdida
#   AVAX_DEBUG=1         muestra el colateral real en el log (defecto: oculto,
#                        repo publica). Solo diagnostico, nunca opera
# =====================================================================

import math
import os
import sys
import time
import logging
from datetime import datetime, timezone

try:
    import ccxt
except ImportError:
    ccxt = None

VERSION = "v2.2-1h-xperp-soldaduras-avax"
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
LEVERAGE = int(float(os.environ.get("AVAX_LEVERAGE", "2")))
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


def _fmt_contracts(c):
    """Tamano listo para OKX: 0.1 ct -> '0.1', 1.0 ct -> '1' (sin polvo float)."""
    if abs(c - round(c)) < 1e-9:
        return str(int(round(c)))
    s = ("%.4f" % c).rstrip("0").rstrip(".")
    return s or "0"


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
    """Reanclaje: vencimiento mas lejano de AVAX/USD settle USD (Ley Carbono).
    Veto USDT TRIPLE: quote USD, settle USD y futuro fechado (no perp swap).
    MiCA/EEE: USDT PROHIBIDO a bordo."""
    ex.load_markets()
    cands = []
    for m in ex.markets.values():
        if m.get("base") != BASE_ASSET:
            continue
        if m.get("quote") != "USD":
            continue                      # veto USDT (1): quote != USD
        if m.get("settle") != "USD":
            continue                      # veto USDT (2): settle != USD
        if m.get("swap") or not m.get("future"):
            continue                      # veto (3): solo futuro fechado
        cands.append(m)
    if not cands:
        fail("[ERROR] Sin futuros AVAX/USD settle USD en OKX. DETENIDO (Ley Carbono).")
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
    if AVAX_DEBUG:
        log.info("Autenticacion OK | host=%s | Colateral real (USD): %.2f", host, tot_eq)
    else:
        log.info("Autenticacion OK | host=%s | Colateral: OK (monto oculto en repo publica; AVAX_DEBUG=1 para verlo)", host)
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
    raw.sort(key=lambda c: c[0])          # v2.2: sort defensivo por ts
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
    for off in AVAX_WINDOW:                    # p.ej. -2,-3,-4 (mas reciente primero)
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
    Doctorado AVAX: el 4.0/5.5 del Ax2 era de la ley EMA8/34 (nota del
    propio capitan: REVALIDAR para 3/21). Calibracion de flota 1h con
    candado: piso = techo = 2.0% -> SL fijo 2.0%, TP 3.0% (R:R 1.5).
    El WIN RATE real que imprime este bot mide si AVAX cubre su 40.0%."""
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
    """Calma tras perdida. FIX P0 del doctorado (hallazgo forense #1): el
    Ax2 leia fill.get('pnl') — campo INEXISTENTE en fills-history -> cooldown
    FAIL-OPEN (tras perder, disparaba de nuevo). DOBLE FUENTE certificada
    (heredada del INYECTOR v2.2): 1) positions-history (realizedPnl, con pnl
    de reserva; en my.okx.com puede responder cuerpo pelado '0' -> KeyError
    :: 0) -> 2) fills-history (fillPnl, limit 40) -> ambas rotas: BLOQUEO
    fail-closed. Con SL/TP atomicos el cierre es una pieza: la semantica de
    calma es identica."""
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
        return True, 0.0, now


def pos_mode_check(ex):
    """P0 doctorado: COMPROBAR el posMode, NUNCA mutarlo. El set_position_mode
    del Ax4 (panda) cambia el modo de la cuenta ENTERA (toda la flota de
    golpe). Si la cuenta no esta en net_mode (one-way), DETENIDO fail-closed:
    el pin se fija UNA vez desde la app; el codigo no toca el interruptor."""
    try:
        cfg = ex.privateGetAccountConfig()
        pos_mode = ((cfg.get("data") or [{}])[0]).get("posMode")
    except Exception as e:
        fail("[ERROR] posMode ilegible (%s) — DETENIDO (fail-closed)" % str(e)[:120])
    if pos_mode != "net_mode":
        fail("[ERROR] Cuenta en %s — pin net_mode UNA vez a flota; el codigo no muta la cuenta. DETENIDO" % pos_mode)
    log.info("posMode verificado: net_mode (one-way) — cuenta intacta, sin mutacion.")
    return pos_mode


def size_contracts(market, px):
    """Tallado honesto AVAX (Ax3.3): ctVal/lotSz/minSz SIEMPRE de la API.
    1 ct = 10 AVAX -> valor $ = 10 x px XPERP (~$86.7 al fichaje 05-11-2026).
    Objetivo $10 redondeado POR DEBAJO al paso lotSz (0.1 ct); si no llega,
    el MINIMO tradable (minSz) si cabe bajo la guardia $15 (Ax3.2). Si ni el
    minimo cabe: DETENIDO — unidad sorpresa = barco parado."""
    info = market.get("info") or {}
    csize = _f(market.get("contractSize") or info.get("ctVal"))
    if csize <= 0:
        fail("[ERROR] ctVal ilegible del mercado — DETENIDO (fail-closed)")
    val_ct = csize * px
    if val_ct <= 0:
        fail("[ERROR] valor por contrato ilegible — DETENIDO (fail-closed)")
    lot = _f(info.get("lotSz")) or 1.0
    min_sz = _f(info.get("minSz")) or lot
    if lot <= 0 or min_sz <= 0:
        fail("[ERROR] lotSz/minSz ilegibles de la API — DETENIDO (fail-closed)")
    steps = int(math.floor((AVAX_NOTIONAL / val_ct) / lot + 1e-9))
    n = steps * lot
    if n < min_sz:
        n = min_sz                        # minimo tradable si cabe bajo guardia
    n = round(n, 10)
    notional = n * val_ct
    if notional > MAX_NOTIONAL:
        fail("[ERROR] tallado %s ct ~ $%.2f > guardia $%.2f — DETENIDO (unidad sorpresa)"
             % (_fmt_contracts(n), notional, MAX_NOTIONAL))
    return n, notional


# ---------------- TELEMETRIA WIN RATE (peticion del capitan) ----------------
def win_rate_report(ex, inst_id):
    """Bitacora WIN RATE con datos REALES de OKX, peaje incluido.
    Neto por cierre = realizedPnl + fee + fundingFee + liqPenalty (el peaje
    DENTRO del resultado). Fuente 1: positions-history (una fila por cierre).
    Si el cuerpo pelado '0' la tumba (KeyError :: 0): fuente 2 fills-history
    (cada fill con fillPnl != 0; aproximacion por fill, se etiqueta). Ambas
    rotas -> aviso y sigue: la telemetria es BITACORA, jamas puerta de
    disparo (la puerta es cooldown_active, fail-closed aparte)."""
    rows = []                       # (ts, net, peaje)
    src = "positions-history"
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
        log.warning("positions-history no abrio para win rate (%s) — probando fills-history",
                    str(e)[:120])
        rows = []
        try:
            r = ex.privateGetTradeFillsHistory(
                {"instType": "FUTURES", "instId": inst_id, "limit": "100"})
            for x in (r.get("data") or []):
                fp = _f(x.get("fillPnl"))
                if fp == 0:
                    continue                       # fills de entrada: sin PnL
                ts = int(_f(x.get("ts")) or _f(x.get("fillTime")))
                peaje = _f(x.get("fee"))
                if ts > 0:
                    rows.append((ts, fp + peaje, peaje))
            src = "fills-history (aprox por fill)"
        except Exception as e2:
            log.warning("win rate ilegible por ambas fuentes (%s) — sin telemetria esta ronda",
                        str(e2)[:120])
            return None
    if not rows:
        log.info("Win rate: sin cierres registrados todavia — la bitacora empezara al primer cierre.")
        return None
    rows.sort(key=lambda t: t[0])       # antiguo -> nuevo
    wins = [x for x in rows if x[1] > 0]
    losses = [x for x in rows if x[1] < 0]
    flats = [x for x in rows if x[1] == 0]
    n = len(rows)
    wr = 100.0 * len(wins) / n if n else 0.0
    net_sum = sum(x[1] for x in rows)
    peaje_sum = sum(x[2] for x in rows)
    avg_w = sum(x[1] for x in wins) / len(wins) if wins else 0.0
    avg_l = sum(x[1] for x in losses) / len(losses) if losses else 0.0
    expectancy = (wr / 100.0) * avg_w + ((100.0 - wr) / 100.0) * avg_l
    be_wr = 100.0 / (1.0 + TP_MULT)
    log.info("==== WIN RATE %s — ultimos %d cierre(s) ====", src, n)
    for ts, net, peaje in rows[-10:]:
        mark = "WIN " if net > 0 else ("LOSS" if net < 0 else "FLAT")
        log.info("  %s UTC | %s | net %+.4f USD (peaje %+.4f)",
                 datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%d-%m %H:%M"),
                 mark, net, peaje)
    log.info("---- Resumen: %d win / %d loss / %d flat | WR %.1f%% | net acum %+.4f USD | peaje acum %+.4f USD",
             len(wins), len(losses), len(flats), wr, net_sum, peaje_sum)
    log.info("---- avg win %+.4f | avg loss %+.4f | expectancia %+.4f USD/trade | R:R %.2f -> WR equilibrio %.1f%%",
             avg_w, avg_l, expectancy, TP_MULT, be_wr)
    if n >= 10 and expectancy <= 0:
        log.info("---- Veredicto: expectancia <= 0 en %d cierres — el peaje gana: calibrar SL/TP/candado antes de crecer.", n)
    elif net_sum > 0:
        log.info("---- Veredicto: cuenta positiva neta — el motor paga el peaje y sobra.")
    return {"n": n, "wins": len(wins), "losses": len(losses), "flats": len(flats),
            "wr": wr, "net": net_sum, "peaje": peaje_sum}


# ---------------- DISPARO ----------------
def emergency_close(ex, inst_id, entry_side, contracts):
    body = {
        "instId": inst_id, "tdMode": MARGIN_MODE, "posSide": "net",
        "side": "sell" if entry_side == "buy" else "buy",
        "ordType": "market", "sz": _fmt_contracts(contracts),
        "reduceOnly": True, "clOrdId": ("AVXE%d" % ex.milliseconds())[:32],
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
    cl = ("AVAX%d" % bar_ms)[:32]

    csize = float(market.get("contractSize") or 1.0)
    notional = contracts * csize * px
    if notional > MAX_NOTIONAL:
        fail("[ERROR] nocional ~$%.2f > guardia $%.2f — DETENIDO"
             % (notional, MAX_NOTIONAL))
    if tot_eq is not None and notional * 3.0 > tot_eq:
        fail("[ERROR] margen ~$%.2f (%dx) vs colateral ~$%.2f -> NO CABE. DETENIDO"
             % (notional / max(1, LEVERAGE), LEVERAGE, tot_eq))

    log.info("Disparo %s | %s ct | nocional ~$%.2f (%dx %s) | SL %s (dist %.2f%%) | "
             "TP %s | clOrdId %s",
             "LONG" if long_dir else "SHORT", _fmt_contracts(contracts), notional,
             LEVERAGE, MARGIN_MODE, sl_s, dist_pct, tp_s, cl)
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
    log.info("Modo ciclo unico | NAVIO AVAX EL OCTAVO EMA3/21 %s %s + BLINDAJE xpl-v4.2 (XPERP USD) | %s",
             AVAX_TF, VERSION, "SOMBRA" if shadow else "REAL")
    if AVAX_TF not in TF_MS:
        fail("[ERROR] AVAX_TF invalido: %s" % AVAX_TF)
    if not AVAX_WINDOW or any(o > -1 for o in AVAX_WINDOW):
        fail("[ERROR] AVAX_WINDOW invalido: %s (se esperaba p.ej. -2,-3,-4)" % (AVAX_WINDOW,))
    if MARGIN_MODE not in ("isolated", "cross"):
        fail("[ERROR] AVAX_MARGIN_MODE invalido: %s" % MARGIN_MODE)

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
        win_rate_report(ex, inst_id)   # telemetria de flota: bitacora, NO puerta

    if has_creds:
        pos_c = open_position_contracts(ex, sym)
        algos = algo_pending(ex, inst_id)
        if pos_c or algos:
            log.info("Nave ocupada: %s ct en posicion | %d algos vivos — no se abre nada. Vigilando.",
                     _fmt_contracts(pos_c), len(algos))
            return 0

    candles = closed_candles(ex, sym, AVAX_TF, N_CANDLES)
    sig = compute_signal(candles)
    log.info("Precio: %.4f | EMA3/21: %.4f/%.4f | %s (estado)",
             sig["px"], sig["e3"], sig["e21"],
             "EMA3 > EMA21" if sig["stance"] > 0 else "EMA3 < EMA21")

    if sig["cross_idx"] is None:
        log.info("Sin cruce EMA3/21 legal en la ventana %s. Vigilando.", list(AVAX_WINDOW))
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
                 "SL ~%.4f (dist %.2f%%) | TP ~%.4f | %s ct ~ $%.2f — sin municion, solo bitacora",
                 dir_txt, age, bar_h, sig["sep"], sl, dist_pct, tp,
                 _fmt_contracts(contracts), est_notional)
        return 0

    # ---- REAL ----
    pos_mode_check(ex)   # P0 doctorado: comprobar, NO mutar (la cuenta es de toda la flota)
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

    cl = ("AVAX%d" % sig["bar_ts"])[:32]
    if already_traded(ex, inst_id, cl):
        log.info("Vela %s ya navegada (candle_already_traded) — descartado.", bar_h)
        return 0

    fire(ex, market, inst_id, sig, contracts, tot_eq)
    return 0


if __name__ == "__main__":
    sys.exit(main())
