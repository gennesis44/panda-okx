#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main-inj.py
=====================================
Flota GSCSI-TRADE | nave: EL APRENDIZ
Instrumento: INJ-USD_UM_XPERP-* (OKX my.okx.com, universo EEE/XPERP, margen USD)
  El sufijo numerico del instId se AUTODESCUBRE (prefijo INJ-USD_UM_XPERP-, un solo
  contrato live). Si hubiera ambiguedad: fija INJ_INST explicitamente.

PARAMETROS DEL CAPITAN
  Exposicion maxima ..... 7 USD de nocional (cap duro; a 1x = 7 USD de margen)
  Cadencia .............. cada 20 min (cron en inj-main.yml)
  Candado (peaje) ....... 0.15 % de separacion entre las 2 velas cerradas
  Stop Loss ............. 2.5 % desde la entrada
  Take Profit ........... 4.0 % desde la entrada
  USDT .................. VETADO

MODOS (env INJ_MODE):
  TEST     papel. Sin ordenes, sin secretos, solo endpoints publicos.
  PREVUELA autentica (GET inocuos), arma la orden y la IMPRIME. No envia nada.
  LIVE     PRODUCCION. Market con SL/TP adjuntos en el exchange (attachAlgoOrds).
           Si SL/TP no se confirma, cierra la posicion (failsafe).

LEYES A BORDO (fallan cerrado):
 L1  USDT VETADO: whitelist por prefijo; settleCcy == USD; 'USDT' fuera.
 L2  MAXIMO 2 VELAS COMPLETAS: limit=3, se descarta la vela en formacion.
 L3  VELAS CONTIGUAS 1H y c2 = ultima hora completa.
 L4  VIGENCIA: reloj vs /public/time (30 s); libro con ts < 10 min.
 L5  TAMANO: respeta minSz/lotSz; precios en multiplos de tickSz (Decimal).
 L6  APALANCAMIENTO <= 20x; default 1x.
 L7  Memoria = libro de partes (estado_inj/); sin historia larga.
 L8  UN SOLO PARTE ABIERTO por canal (papel y real independientes).
 L9  POSICION EXTERNA: si la cuenta tiene INJ que NO es del bot, no se abre nada.
 L10 KILL-SWITCH: si existe estado_inj/STOP, ninguna entrada en ningun modo.
 L11 NOCIONAL <= 7 USD: contratos = floor(7 / (precio*ctVal)); si < minSz, no opera.
 L12 SPREAD: si spread/mid > 0.30 % no entra.

Secretos (PREVUELA/LIVE; nunca se imprimen):
  OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN

VERSION = "1.0-PROD"

# ---------------- CONFIG ----------------
BASE = os.environ.get("INJ_BASE", "https://my.okx.com")
INST_PREFIX = "INJ-USD_UM_XPERP-"               # L1: whitelist por prefijo
INST = os.environ.get("INJ_INST", "").strip()   # se resuelve en runtime
BAR = "1H"
HORA_MS = 3_600_000
MAX_CERRADAS = 2                                # L2
LIMIT_FETCH = MAX_CERRADAS + 1
LIMIT_LIQ = 48
SEP_MIN = Decimal("0.0015")                     # candado 0.15 %
SL_PCT = Decimal("0.025")                       # SL 2.5 %
TP_PCT = Decimal("0.04")                        # TP 4 %
MAX_USD = min(Decimal(os.environ.get("INJ_MAX_USD", "7")), Decimal("7"))   # cap duro 7 USD
MAX_SPREAD_PCT = Decimal("0.003")               # L12
LEV_CAP = 20                                    # L6
LEV = max(1, int(os.environ.get("INJ_LEV", "1")))
TD_MODE = os.environ.get("INJ_TD_MODE", "isolated")
MODE = os.environ.get("INJ_MODE", "TEST").upper()
FAILSAFE_CLOSE = os.environ.get("INJ_FAILSAFE_CLOSE", "1") == "1"
ESTADO_DIR = os.environ.get("INJ_ESTADO_DIR", "estado_inj")
PARTES_PATH = os.path.join(ESTADO_DIR, "partes_inj.json")
DECISIONES_PATH = os.path.join(ESTADO_DIR, "decisiones.jsonl")
STOP_PATH = os.path.join(ESTADO_DIR, "STOP")
TOLERANCIA_RELOJ_S = 30
MAX_VIGENCIA_PRECIO_MS = 600_000
MAX_PARTES_GUARDADOS = 200
MAX_LINEAS_DECISIONES = 1500
SIN_FILL_TIMEOUT_MS = 600_000


# ---------------- utilidades ----------------
def http_get(url, reintentos=3):
    ultimo = None
    for i in range(reintentos):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": f"inj-aprendiz/{VERSION} (GSCSI-TRADE)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            ultimo = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET fallo tras {reintentos} intentos: {url} ({ultimo})")


def _private_once(method: str, path: str, body=None):
    key = os.environ.get("OKX_API_KEY", "")
    sec = os.environ.get("OKX_API_SECRET", "")
    pas = os.environ.get("OKX_API_PASSPHRASE", "")
    if not (key and sec and pas):
        raise RuntimeError("FALTAN_SECRETOS: define OKX_API_KEY, OKX_API_SECRET, "
                           "OKX_API_PASSPHRASE (GitHub Secrets)")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    body_str = "" if body is None else json.dumps(body)
    msg = f"{ts}{method}{path}{body_str}".encode()
    sign = base64.b64encode(hmac.new(sec.encode(), msg, hashlib.sha256).digest()).decode()
    req = urllib.request.Request(BASE + path,
                                 data=None if body is None else body_str.encode(),
                                 method=method)
    req.add_header("OK-ACCESS-KEY", key)
    req.add_header("OK-ACCESS-SIGN", sign)
    req.add_header("OK-ACCESS-TIMESTAMP", ts)
    req.add_header("OK-ACCESS-PASSPHRASE", pas)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", f"inj-aprendiz/{VERSION} (GSCSI-TRADE)")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detalle = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code} en {path}: {detalle}") from e


def http_private(method: str, path: str, body=None):
    """OKX v5 autenticado. GET reintenta; POST NUNCA (doble orden)."""
    intentos = 3 if method == "GET" else 1
    ultimo = None
    for i in range(intentos):
        try:
            return _private_once(method, path, body)
        except RuntimeError as e:
            if "FALTAN_SECRETOS" in str(e):
                raise
            ultimo = e
            if i < intentos - 1:
                time.sleep(2 * (i + 1))
    raise ultimo


def iso(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ahora_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ahora_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def a_tick(p: Decimal, tick: Decimal) -> Decimal:
    return (p / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick


def fmt(d: Decimal) -> str:
    return format(d, "f")


def bitacora(msg: str) -> None:
    print(msg, flush=True)


def recortar_lineas(path: str, maximo: int) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            lineas = f.readlines()
        if len(lineas) > maximo:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lineas[-maximo:])
    except OSError:
        pass


def registrar_decision(reg: dict) -> None:
    os.makedirs(ESTADO_DIR, exist_ok=True)
    with open(DECISIONES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    recortar_lineas(DECISIONES_PATH, MAX_LINEAS_DECISIONES)


def cargar_partes() -> list:
    if not os.path.exists(PARTES_PATH):
        return []
    with open(PARTES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_partes(partes: list) -> None:
    os.makedirs(ESTADO_DIR, exist_ok=True)
    abiertos = [p for p in partes if p["estado"] == "ABIERTO"]
    cerrados = [p for p in partes if p["estado"] != "ABIERTO"][-MAX_PARTES_GUARDADOS:]
    orden = {id(p): i for i, p in enumerate(partes)}
    final = sorted(abiertos + cerrados, key=lambda p: orden[id(p)])
    tmp = PARTES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PARTES_PATH)


# ---------------- fuentes publicas (guardias) ----------------
def servidor() -> int:
    return int(http_get(f"{BASE}/api/v5/public/time")["data"][0]["ts"])


def instrumento() -> dict:
    """Resuelve y valida el instId de INJ. L1/L5/L6."""
    global INST
    if INST:
        assert INST.startswith(INST_PREFIX), "L1 VIOLADA: INJ_INST fuera de la whitelist INJ-USD_UM_XPERP-"
        r = http_get(f"{BASE}/api/v5/public/instruments?instType=FUTURES&instId={INST}")
        assert r.get("code") == "0" and r.get("data"), "instrumento no respondio"
        d = r["data"][0]
    else:
        r = http_get(f"{BASE}/api/v5/public/instruments?instType=FUTURES")
        assert r.get("code") == "0" and r.get("data"), "lista de instrumentos no respondio"
        cand = [x for x in r["data"]
                if x["instId"].startswith(INST_PREFIX) and x.get("state") == "live"]
        assert len(cand) == 1, f"instId INJ ambiguo o inexistente ({len(cand)} candidatos); fija INJ_INST"
        d = cand[0]
        INST = d["instId"]
    assert "USDT" not in d["instId"], "L1 VIOLADA: instId con USDT"
    assert d["settleCcy"] == "USD", "L1 VIOLADA: settleCcy no es USD"
    assert d["state"] == "live", "instrumento no live"
    assert d["ctType"] == "linear", "se esperaba lineal"
    assert Decimal(d["tickSz"]) > 0 and Decimal(d["lotSz"]) > 0 and Decimal(d["minSz"]) > 0, \
        "L5: granularidad invalida"
    assert int(d["lever"]) >= LEV, "L6: apalancamiento pedido supera el del contrato"
    assert LEV <= LEV_CAP, "L6 VIOLADA: apalancamiento por encima del cap"
    return d


def velas(server_ms: int) -> dict:
    r = http_get(f"{BASE}/api/v5/market/candles?instId={INST}&bar={BAR}&limit={LIMIT_FETCH}")
    rows = r.get("data") or []
    cerradas = [x for x in rows if x[8] == "1"]
    formando = [x for x in rows if x[8] == "0"]
    assert len(cerradas) >= MAX_CERRADAS, "L2: no hay 2 velas cerradas disponibles"
    c2, c1 = cerradas[0], cerradas[1]
    assert int(c2[0]) - int(c1[0]) == HORA_MS, "L3: hueco entre c1 y c2"
    hora_actual = (server_ms // HORA_MS) * HORA_MS
    assert int(c2[0]) == hora_actual - HORA_MS, "L3: c2 no es la ultima hora completa"
    if formando:
        assert int(formando[0][0]) == int(c2[0]) + HORA_MS, "L3: cadencia 1H rota"
    return {
        "c1": {"ts": int(c1[0]), "o": c1[1], "h": c1[2], "l": c1[3], "c": c1[4]},
        "c2": {"ts": int(c2[0]), "o": c2[1], "h": c2[2], "l": c2[3], "c": c2[4]},
    }


def libro(server_ms: int) -> dict:
    r = http_get(f"{BASE}/api/v5/market/books?instId={INST}&sz=5")
    d = r["data"][0]
    ts = int(d["ts"])
    assert server_ms - ts < MAX_VIGENCIA_PRECIO_MS, "L4: libro NO VIGENTE (ts viejo)"
    assert d["bids"] and d["asks"], "libro vacio"
    bid = Decimal(d["bids"][0][0])
    ask = Decimal(d["asks"][0][0])
    assert ask >= bid > 0, "libro cruzado o invalido"
    mid = (ask + bid) / 2
    assert (ask - bid) / mid <= MAX_SPREAD_PCT, "L12: spread excesivo"
    return {"ts": ts, "bid": bid, "ask": ask, "spread": ask - bid}


# ---------------- senal / niveles / tamano ----------------
def senal(v1: dict, v2: dict):
    c1c, c2c = Decimal(v1["c"]), Decimal(v2["c"])
    sep = (c2c - c1c) / c1c
    if sep >= SEP_MIN:
        return "LONG", sep
    if sep <= -SEP_MIN:
        return "SHORT", sep
    return None, sep


def niveles(lado: str, entrada: Decimal, specs: dict):
    """SL 2.5 % / TP 4 % desde la entrada, a multiplos de tickSz."""
    tick = Decimal(specs["tickSz"])
    if lado == "LONG":
        sl = a_tick(entrada * (1 - SL_PCT), tick)
        tp = a_tick(entrada * (1 + TP_PCT), tick)
        assert sl < entrada < tp, "niveles LONG incoherentes"
    else:
        sl = a_tick(entrada * (1 + SL_PCT), tick)
        tp = a_tick(entrada * (1 - TP_PCT), tick)
        assert tp < entrada < sl, "niveles SHORT incoherentes"
    assert tp > 0 and sl > 0, "nivel no positivo"
    return sl, tp, abs(entrada - sl)


def calcular_size(entrada: Decimal, specs: dict) -> Decimal:
    """L11: contratos enteros de lotSz con nocional <= MAX_USD. 0 si no alcanza minSz."""
    ct_val = Decimal(specs["ctVal"])
    lot, minsz = Decimal(specs["lotSz"]), Decimal(specs["minSz"])
    por_ct = entrada * ct_val
    n = ((MAX_USD / por_ct) / lot).to_integral_value(rounding=ROUND_DOWN) * lot
    return n if n >= minsz else Decimal(0)


def construir_parte(modo, lado, entrada_ref, sl, tp, riesgo, vel, sep, specs, size) -> dict:
    ct_val = Decimal(specs["ctVal"])
    nocional = (entrada_ref * ct_val * size).quantize(Decimal("0.0001"))
    return {
        "id": f"{vel['c2']['ts']}-{lado}-{modo}",
        "abierto_ts": ahora_iso(),
        "abierto_ms": ahora_ms(),
        "modo": modo,
        "inst": INST,
        "lado": lado,
        "velas": {"c1": vel["c1"], "c2": vel["c2"]},
        "sep_pct": str((sep * 100).quantize(Decimal("0.0001"))),
        "entrada_ref": str(entrada_ref),
        "entrada_fill": None,
        "sl": str(sl),
        "tp": str(tp),
        "riesgo": str(riesgo.quantize(Decimal("0.0001"))),
        "size_ct": fmt(size),
        "lev": LEV,
        "td_mode": TD_MODE if modo == "LIVE" else None,
        "nocional_usd": str(nocional),
        "margen_usd": str((nocional / LEV).quantize(Decimal("0.0001"))),
        "ordId": None,
        "sltp_confirmado": None,
        "estado": "ABIERTO",
        "resultado": None,
    }


# ---------------- liquidador de PAPEL ----------------
def liquidar_papel(partes: list, specs: dict) -> list:
    r = http_get(f"{BASE}/api/v5/market/candles?instId={INST}&bar={BAR}&limit={LIMIT_LIQ}")
    chron = sorted([x for x in r["data"] if x[8] == "1"], key=lambda x: int(x[0]))
    ct_val = Decimal(specs["ctVal"])
    cerrados = []
    for p in partes:
        if p["estado"] != "ABIERTO" or p.get("modo", "TEST") != "TEST":
            continue
        c2_ts = int(p["velas"]["c2"]["ts"])
        if chron and c2_ts < int(chron[0][0]):
            p.setdefault("notas", []).append(f"ventana de liquidacion incompleta (max {LIMIT_LIQ}h)")
        sl, tp = Decimal(p["sl"]), Decimal(p["tp"])
        for x in [y for y in chron if int(y[0]) > c2_ts]:
            low, high = Decimal(x[3]), Decimal(x[2])
            if p["lado"] == "LONG":
                hit_sl, hit_tp = low <= sl, high >= tp
            else:
                hit_sl, hit_tp = high >= sl, low <= tp
            if hit_sl:
                salida, tipo = sl, ("SL_AMBIGUO" if hit_tp else "SL")
            elif hit_tp:
                salida, tipo = tp, "TP"
            else:
                continue
            diff = (salida - Decimal(p["entrada_ref"])) if p["lado"] == "LONG" \
                else (Decimal(p["entrada_ref"]) - salida)
            pnl = (diff * ct_val * Decimal(p["size_ct"])).quantize(Decimal("0.0001"))
            p["estado"] = f"CERRADO_{tipo}"
            p["resultado"] = {"tipo": tipo, "salida": str(salida), "pnl_usd_bruto": str(pnl),
                              "comisiones": "0 (papel)",
                              "cerrado_con_vela_ts": int(x[0]), "cerrado_con_vela_iso": iso(x[0])}
            cerrados.append(p["id"])
            break
    return cerrados


# ---------------- funciones privadas (PREVUELA/LIVE) ----------------
def cuenta_config() -> dict:
    r = http_private("GET", "/api/v5/account/config")
    if r.get("code") != "0":
        raise RuntimeError(f"auth/config rechazo: code={r.get('code')} msg={r.get('msg')}")
    d = r["data"][0]
    return {"posMode": d.get("posMode"), "acctLv": d.get("acctLv")}


def posicion_abierta() -> Decimal:
    r = http_private("GET", f"/api/v5/account/positions?instId={INST}")
    if r.get("code") != "0":
        raise RuntimeError(f"positions rechazo: {r.get('msg')}")
    total = Decimal(0)
    for p in r.get("data") or []:
        total += abs(Decimal(p.get("pos") or "0"))
    return total


def arca_usd():
    try:
        r = http_private("GET", "/api/v5/account/balance?ccy=USD")
        for det in (r.get("data") or [{}])[0].get("details", []):
            if det.get("ccy") == "USD":
                for k in ("availEq", "availBal", "cashBal"):
                    if det.get(k):
                        return Decimal(det[k])
    except Exception as e:  # noqa: BLE001
        bitacora(f"[aviso] arca ilegible: {e}")
    return None


def set_apalancamiento(config: dict, lado: str):
    body = {"instId": INST, "lever": str(LEV), "mgnMode": TD_MODE}
    if TD_MODE == "isolated" and config["posMode"] == "long_short_mode":
        body["posSide"] = "long" if lado == "LONG" else "short"
    r = http_private("POST", "/api/v5/account/set-leverage", body)
    if r.get("code") != "0":
        raise RuntimeError(f"set-leverage rechazo: code={r.get('code')} msg={r.get('msg')}")


def payload_orden(lado: str, sl: Decimal, tp: Decimal, size: Decimal, config: dict) -> dict:
    return {
        "instId": INST,
        "tdMode": TD_MODE,
        "posSide": ("long" if lado == "LONG" else "short")
                   if config["posMode"] == "long_short_mode" else "net",
        "side": "buy" if lado == "LONG" else "sell",
        "ordType": "market",
        "sz": fmt(size),
        "attachAlgoOrds": [{
            "tpTriggerPx": fmt(tp), "tpOrdPx": "-1",
            "slTriggerPx": fmt(sl), "slOrdPx": "-1",
        }],
    }


def enviar_orden(payload: dict) -> str:
    r = http_private("POST", "/api/v5/trade/order", payload)
    if r.get("code") != "0":
        raise RuntimeError(f"orden rechazada: code={r.get('code')} msg={r.get('msg')}")
    d = r["data"][0]
    if d.get("sCode") != "0":
        raise RuntimeError(f"orden rechazada: sCode={d.get('sCode')} sMsg={d.get('sMsg')}")
    return d["ordId"]


def fill_de_entrada(ord_id: str):
    r = http_private("GET", f"/api/v5/trade/fills-history?instType=FUTURES&instId={INST}&limit=50")
    for f in r.get("data") or []:
        if f.get("ordId") == ord_id:
            return {"avgPx": f["fillPx"], "sz": f["fillSz"], "fee": f.get("fee"),
                    "ts": int(f["ts"]), "iso": iso(f["ts"])}
    return None


def verificar_algo_sltp() -> bool:
    """Hay SL/TP vivo en el exchange para el instrumento (oco/conditional). Mejor esfuerzo."""
    for ord_type in ("oco", "conditional"):
        try:
            r = http_private("GET", f"/api/v5/trade/orders-algo-pending?ordType={ord_type}&instId={INST}")
            if r.get("code") == "0" and r.get("data"):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def cerrar_emergencia(lado: str, config: dict) -> bool:
    body = {"instId": INST, "mgnMode": TD_MODE, "autoCxl": True}
    if config["posMode"] == "long_short_mode":
        body["posSide"] = "long" if lado == "LONG" else "short"
    try:
        r = http_private("POST", "/api/v5/trade/close-position", body)
        ok = r.get("code") == "0"
        bitacora(f"[FAILSAFE] close-position {'OK' if ok else 'RECHAZADO: ' + str(r.get('msg'))}")
        return ok
    except Exception as e:  # noqa: BLE001
        bitacora(f"[CRITICO] failsafe close-position fallo: {e}")
        return False


def confirmar_sltp_o_cerrar(p: dict, config: dict, intentos: int = 3) -> None:
    for i in range(intentos):
        if verificar_algo_sltp():
            p["sltp_confirmado"] = True
            return
        time.sleep(2)
    p["sltp_confirmado"] = False
    bitacora(f"[CRITICO] {p['id']}: SL/TP NO confirmado en exchange")
    if FAILSAFE_CLOSE:
        p.setdefault("notas", []).append("failsafe: cierre de emergencia por SL/TP no confirmado")
        cerrar_emergencia(p["lado"], config)


def reconciliar_live(partes: list, specs: dict, config: dict) -> list:
    ct_val = Decimal(specs["ctVal"])
    cerrados = []
    abiertos = [p for p in partes if p["estado"] == "ABIERTO" and p.get("modo") == "LIVE"]
    if not abiertos:
        return cerrados
    pos = posicion_abierta()
    fills = None
    for p in abiertos:
        if p.get("entrada_fill") is None and p.get("ordId"):
            try:
                f = fill_de_entrada(p["ordId"])
                if f:
                    p["entrada_fill"] = f
                    bitacora(f"[FILL ENTRADA] {p['id']}: avgPx {f['avgPx']} (ref {p['entrada_ref']}) "
                             f"fee {f['fee']}")
            except Exception as e:  # noqa: BLE001
                bitacora(f"[aviso] fill entrada: {e}")
        if pos > 0 and p.get("sltp_confirmado") is not True:
            confirmar_sltp_o_cerrar(p, config, intentos=2)
        if pos == 0:
            if fills is None:
                r = http_private("GET", f"/api/v5/trade/fills-history?instType=FUTURES&instId={INST}&limit=100")
                fills = r.get("data") or []
            lado_salida = "buy" if p["lado"] == "SHORT" else "sell"
            ts_desde = int((p.get("entrada_fill") or {}).get("ts") or p.get("abierto_ms") or 0)
            salida, fees = [], Decimal(0)
            for f in sorted(fills, key=lambda y: int(y["ts"])):
                if f.get("side") != lado_salida or f.get("ordId") == p.get("ordId"):
                    continue
                if int(f["ts"]) < ts_desde:
                    continue
                salida.append(f)
                fees += Decimal(f.get("fee") or "0")
                if sum(Decimal(x["fillSz"]) for x in salida) >= Decimal(p["size_ct"]):
                    break
            if not salida:
                if p.get("entrada_fill") is None and ahora_ms() - int(p.get("abierto_ms") or 0) > SIN_FILL_TIMEOUT_MS:
                    p["estado"] = "CERRADO_SIN_FILL"
                    p["resultado"] = {"tipo": "SIN_FILL", "nota": "sin posicion ni fills tras timeout"}
                    cerrados.append(p["id"])
                continue
            n_sal = sum(Decimal(x["fillSz"]) for x in salida)
            salida_avg = (sum(Decimal(x["fillPx"]) * Decimal(x["fillSz"]) for x in salida) / n_sal)
            salida_avg = salida_avg.quantize(Decimal("0.0001"))
            entrada_px = Decimal((p.get("entrada_fill") or {}).get("avgPx", p["entrada_ref"]))
            bruto = ((salida_avg - entrada_px) if p["lado"] == "LONG"
                     else (entrada_px - salida_avg)) * ct_val * Decimal(p["size_ct"])
            fee_ent = Decimal((p.get("entrada_fill") or {}).get("fee") or "0")
            total_fees = fees + fee_ent
            p["estado"] = "CERRADO_EXCHANGE"
            p["resultado"] = {
                "tipo": "CIERRE_EXCHANGE (SL/TP reconstruidos por fills)",
                "salida": str(salida_avg),
                "pnl_usd_bruto": str(bruto.quantize(Decimal("0.0001"))),
                "fees_usd": str((-total_fees).quantize(Decimal("0.0001"))),
                "pnl_usd_neto": str((bruto + total_fees).quantize(Decimal("0.0001"))),
                "fills_salida": len(salida),
            }
            cerrados.append(p["id"])
    return cerrados


# ---------------- main ----------------
def main() -> int:
    bitacora("=" * 66)
    bitacora(f"EL APRENDIZ — main-inj.py v{VERSION} — MODO {MODE}")
    bitacora(f"Ley de velas: MAXIMO {MAX_CERRADAS} completas | whitelist {INST_PREFIX}*")
    bitacora(f"Candado {SEP_MIN * 100} % | SL {SL_PCT * 100} % | TP {TP_PCT * 100} % | "
             f"max {MAX_USD} USD | lev {LEV}x (cap {LEV_CAP}) | tdMode {TD_MODE}")
    if MODE == "LIVE":
        bitacora("PRODUCCION: ordenes REALES con SL/TP adjuntos en el exchange")
    elif MODE == "PREVUELA":
        bitacora("PREVUELA: autentica y arma la orden; NO la envia")
    else:
        bitacora("TEST: papel, sin ordenes, sin secretos")
    bitacora("=" * 66)

    if MODE not in ("TEST", "PREVUELA", "LIVE"):
        bitacora(f"[ERROR] INJ_MODE desconocido: {MODE}")
        return 1

    if os.path.exists(STOP_PATH):
        bitacora("[STOP] kill-switch presente — ninguna entrada")
        registrar_decision({"ts_run": ahora_iso(), "modo": MODE, "decision": "SKIP_STOP"})
        return 0

    notas, guardias = [], {}
    try:
        server_ms = servidor()
        deriva_s = abs(ahora_ms() - server_ms) / 1000
        guardias["reloj"] = "OK" if deriva_s <= TOLERANCIA_RELOJ_S else f"DERIVA {deriva_s:.0f}s"
        if guardias["reloj"] != "OK":
            notas.append("L4: reloj descuadrado — no decide")
            registrar_decision({"ts_run": ahora_iso(), "modo": MODE, "decision": "SKIP_RELOJ",
                                "guardias": guardias, "notas": notas})
            bitacora(f"[SKIP] {notas}")
            return 0
        specs = instrumento()
        bitacora(f"Instrumento: {INST} (ctVal {specs['ctVal']}, tick {specs['tickSz']}, "
                 f"lot {specs['lotSz']}, min {specs['minSz']})")
        guardias["leyes_L1_L5_L6"] = "OK"
        vel = velas(server_ms)
        guardias["velas_L2_L3"] = "OK"
        book = libro(server_ms)
        guardias["libro_L4_L12"] = "OK"
    except AssertionError as e:
        registrar_decision({"ts_run": ahora_iso(), "modo": MODE, "decision": "SKIP_GUARDIA",
                            "guardia": str(e)})
        bitacora(f"[SKIP] Guardia: {e}")
        return 0
    except Exception as e:  # noqa: BLE001
        registrar_decision({"ts_run": ahora_iso(), "modo": MODE, "decision": "ERROR",
                            "error": str(e)})
        bitacora("[ERROR] fuente de datos:")
        traceback.print_exc()
        return 1

    partes = cargar_partes()

    # ------- CANAL PAPEL -------
    cerrados_papel = liquidar_papel(partes, specs)
    if cerrados_papel:
        bitacora(f"Liquidador PAPEL: cerrados -> {', '.join(cerrados_papel)}")
        for p in partes:
            if p["id"] in cerrados_papel:
                bitacora(f"  {p['id']}: {p['resultado']['tipo']} salida {p['resultado']['salida']} "
                         f"pnl {p['resultado']['pnl_usd_bruto']} USD")

    lado, sep = senal(vel["c1"], vel["c2"])
    sensor = lado or "NEUTRO"
    bitacora(f"Velas (2 cerradas): c1={vel['c1']['c']} ({iso(vel['c1']['ts'])}) | "
             f"c2={vel['c2']['c']} ({iso(vel['c2']['ts'])})")
    bitacora(f"Sensor: {sensor} | separacion {(sep * 100).quantize(Decimal('0.0001'))} % "
             f"| candado {SEP_MIN * 100} %")
    bitacora(f"Libro: bid {book['bid']} / ask {book['ask']} "
             f"(spread {book['spread'].quantize(Decimal('0.0001'))})")

    # ------- MODO TEST -------
    if MODE == "TEST":
        decision, parte_id = "SIN_SENAL", None
        if lado is None:
            decision = "SIN_SENAL"
        elif any(p["estado"] == "ABIERTO" and p.get("modo", "TEST") == "TEST" for p in partes):
            decision = "PARTIDO_EN_MARCHA"
        elif any(p["id"] == f"{vel['c2']['ts']}-{lado}-TEST" for p in partes):
            decision = "DUPLICADO"
        else:
            entrada = book["ask"] if lado == "LONG" else book["bid"]
            size = calcular_size(entrada, specs)
            if size == 0:
                decision = "SKIP_TAMANO"
                bitacora(f"[SKIP] L11: {MAX_USD} USD no alcanza para minSz a {entrada} "
                         f"(ctVal {specs['ctVal']})")
            else:
                sl, tp, riesgo = niveles(lado, entrada, specs)
                p = construir_parte("TEST", lado, entrada, sl, tp, riesgo, vel, sep, specs, size)
                partes.append(p)
                parte_id, decision = p["id"], "PARTE_ABIERTO"
                bitacora(f"[PARTE DE PRUEBA] {p['id']}: {p['lado']} {p['size_ct']} ct ref {p['entrada_ref']} "
                         f"SL {p['sl']} TP {p['tp']}")
        guardar_partes(partes)
        registrar_decision({"ts_run": ahora_iso(), "modo": "TEST", "sensor": sensor,
                            "sep_pct": str((sep * 100).quantize(Decimal("0.0001"))),
                            "decision": decision, "parte_id": parte_id,
                            "guardias": guardias, "notas": notas})
        abiertos = sum(1 for p in partes if p["estado"] == "ABIERTO")
        bitacora(f"Libro: {len(partes)} partes ({abiertos} abiertos) -> {PARTES_PATH}")
        bitacora("[FIN] modo TEST — ninguna orden enviada")
        return 0

    # ------- PREVUELA / LIVE: autenticacion -------
    try:
        config = cuenta_config()
        guardias["auth"] = f"OK (posMode={config['posMode']}, acctLv={config['acctLv']})"
        bitacora(f"Auth OK: posMode={config['posMode']} acctLv={config['acctLv']}")
    except Exception as e:  # noqa: BLE001
        registrar_decision({"ts_run": ahora_iso(), "modo": MODE, "decision": "ERROR_AUTH",
                            "error": str(e)})
        bitacora(f"[CRITICO] autenticacion fallo: {e}")
        return 1

    try:
        cerrados_live = reconciliar_live(partes, specs, config)
        if cerrados_live:
            bitacora(f"Reconciliador LIVE: cerrados -> {', '.join(cerrados_live)}")
            for p in partes:
                if p["id"] in cerrados_live:
                    r_ = p["resultado"]
                    bitacora(f"  {p['id']}: {r_['tipo']} salida {r_.get('salida')} "
                             f"pnl neto {r_.get('pnl_usd_neto')} USD (fees {r_.get('fees_usd')})")
    except Exception as e:  # noqa: BLE001
        registrar_decision({"ts_run": ahora_iso(), "modo": MODE, "decision": "ERROR_RECON",
                            "error": str(e)})
        bitacora(f"[CRITICO] reconciliacion fallo: {e}")
        guardar_partes(partes)
        return 1

    try:
        pos = posicion_abierta()
    except Exception as e:  # noqa: BLE001
        registrar_decision({"ts_run": ahora_iso(), "modo": MODE, "decision": "ERROR_POS",
                            "error": str(e)})
        bitacora(f"[CRITICO] lectura de posicion fallo: {e}")
        guardar_partes(partes)
        return 1

    live_abierto = any(p["estado"] == "ABIERTO" and p.get("modo") == "LIVE" for p in partes)
    decision, parte_id = "SIN_SENAL", None

    if lado is None:
        bitacora("[FIN] sin senal")
    elif live_abierto:
        decision = "PARTIDO_EN_MARCHA"
        bitacora("[SKIP] ya hay un parte LIVE abierto — un solo barco por canal")
    elif pos > 0:
        decision = "SKIP_POSICION_EXTERNA"
        bitacora(f"[SKIP] L9: la cuenta ya tiene {pos} ct de INJ que NO son del bot — no se encima nada")
    elif any(p["id"] == f"{vel['c2']['ts']}-{lado}-LIVE" for p in partes):
        decision = "DUPLICADO"
        bitacora("[SKIP] esta vela ya produjo un parte LIVE (idempotencia)")
    else:
        entrada = book["ask"] if lado == "LONG" else book["bid"]
        size = calcular_size(entrada, specs)
        if size == 0:
            decision = "SKIP_TAMANO"
            bitacora(f"[SKIP] L11: {MAX_USD} USD no alcanza para minSz a {entrada} "
                     f"(ctVal {specs['ctVal']})")
        else:
            sl, tp, riesgo = niveles(lado, entrada, specs)
            margen = entrada * Decimal(specs["ctVal"]) * size / Decimal(LEV)
            arca = arca_usd()
            if arca is not None and arca < margen * Decimal("1.1"):
                decision = "SKIP_ARCA_INSUFICIENTE"
                bitacora(f"[SKIP] arca disponible {arca} USD < 1.1x margen {margen:.4f} USD")
            elif MODE == "PREVUELA":
                payload = payload_orden(lado, sl, tp, size, config)
                decision = "PREVUELA_ORDEN_ARMADA"
                bitacora("[PREVUELA] orden armada (NO enviada):")
                bitacora(json.dumps(payload, indent=2, ensure_ascii=False))
                bitacora(f"[PREVUELA] entrada_ref {entrada} | SL {sl} | TP {tp} | "
                         f"size {size} ct | margen ~{margen:.4f} USD")
                notas.append("preVuela: nada enviado")
            else:  # LIVE
                try:
                    set_apalancamiento(config, lado)
                    payload = payload_orden(lado, sl, tp, size, config)
                    ord_id = enviar_orden(payload)
                    p = construir_parte("LIVE", lado, entrada, sl, tp, riesgo, vel, sep, specs, size)
                    p["ordId"] = ord_id
                    partes.append(p)
                    guardar_partes(partes)      # persistir YA: la orden existe en el exchange
                    parte_id, decision = p["id"], "ORDEN_ENVIADA"
                    bitacora(f"[PRODUCCION] ORDEN ENVIADA: {lado} {size} ct | ordId {ord_id}")
                    time.sleep(2)
                    f = fill_de_entrada(ord_id)
                    if f:
                        p["entrada_fill"] = f
                        bitacora(f"[FILL ENTRADA] avgPx {f['avgPx']} (ref {entrada}) | fee {f['fee']}")
                    confirmar_sltp_o_cerrar(p, config)
                    if p["sltp_confirmado"]:
                        bitacora("[SL/TP] CONFIRMADO en exchange")
                except Exception as e:  # noqa: BLE001
                    decision = "ERROR_ORDEN"
                    registrar_decision({"ts_run": ahora_iso(), "modo": "LIVE",
                                        "decision": "ERROR_ORDEN", "error": str(e)})
                    bitacora(f"[CRITICO] orden fallo: {e}")

    guardar_partes(partes)
    registrar_decision({"ts_run": ahora_iso(), "modo": MODE, "sensor": sensor,
                        "sep_pct": str((sep * 100).quantize(Decimal("0.0001"))) if lado else None,
                        "decision": decision, "parte_id": parte_id,
                        "pos_cuenta": str(pos), "guardias": guardias, "notas": notas})
    abiertos = sum(1 for p in partes if p["estado"] == "ABIERTO")
    bitacora(f"Libro: {len(partes)} partes ({abiertos} abiertos) -> {PARTES_PATH}")
    bitacora(f"[FIN] modo {MODE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
