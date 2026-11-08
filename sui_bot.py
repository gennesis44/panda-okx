#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EL APRENDIZ — sui_bot.py — v2.0-PROD
====================================
Flota GSCSI-TRADE | nave: EL APRENDIZ
Instrumento: SUI-USD_UM_XPERP-310404 (OKX my.okx.com, universo EEE/XPERP, margen USD)

MODOS (env SUI_MODE):
  TEST     papel. Sin ordenes, sin secretos, solo endpoints publicos.
  PREVUELA autentica contra /account (GET, inocuo), arma la orden y la IMPRIME.
           No envia nada. Prueba de llaves antes del moton de fuego.
  LIVE     PRODUCCION. Orden market real con SL/TP adjuntos (attachAlgoOrds,
           ejecucion market "-1"): si el bot muere, el stop sobrevive en el exchange.

ACTA DE PRODUCCION (orden del capitan, con 0 partes cerrados en papel):
  La certificacion pasa a ser EN VUELO a escala de centavos, con caps duros:
  1 ct (SUI_SIZE_CT<=5), 1x por defecto (SUI_LEV<=20, cap del contrato),
  UN solo parte abierto por canal, SL/TP exchange-side OBLIGATORIOS.
  Riesgo por parte ~ 0.5% del nocional (1 SUI ~ 0.85 USD) => centavos.
  La puerta a mayor tamano sigue cerrada: >=20-30 partes cerrados + FANAL.

LEYES A BORDO (guardas duras, fallan cerrado):
 L1. USDT VETADO: whitelist de un solo instId; assert 'USDT' fuera y settleCcy=='USD'.
 L2. MAXIMO 2 VELAS COMPLETAS ANTERIORES: fetch limit=3 -> descartar la vela en
     formacion (confirm=0) -> usar EXACTAMENTE 2. El liquidador usa velas SOLO
     para partes en papel; en LIVE la salida la resuelve el exchange.
 L3. VELAS CONTIGUAS 1H: c2 = la ultima hora completa; cadencia continua; si no -> no decide.
 L4. VIGENCIA: reloj vs /public/time (30 s); libro con ts < 10 min.
 L5. TAMANO: contratos enteros (minSz=1, lotSz=1); precios en multiplos de 0.0001 (Decimal).
 L6. APALANCAMIENTO <= 20x (cap del contrato); default 1x.
 L7. La memoria del barco es su libro de partes (estado_sui/); prohibida la historia larga.
 L8. UN SOLO PARTE ABIERTO por modo (papel y real son canales independientes).
 L9. POSICION EXTERNA: si la cuenta tiene SUI que NO es del bot, no se abre nada
     (guarda anti-injerencia: nada de encimar ordenes sobre posiciones manuales).
 L10. KILL-SWITCH: si existe estado_sui/STOP, ninguna entrada en ningun modo.

Secretos (SOLO PREVUELA/LIVE; nunca se imprimen):
  OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE

Estado: estado_sui/partes_test.json (libro oficial; partes de papel y de LIVE)
        estado_sui/decisiones.jsonl  (bitacora de decisiones)
Uso:    python3 sui_bot.py   (local default TEST)
Repo:   raiz + .github/workflows/sui-aprendiz.yml; ignicion LIVE = variable de repo
        SUI_MODE=LIVE (Settings > Actions > Variables) + los 3 secretos.
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
from decimal import Decimal, ROUND_HALF_UP

VERSION = "2.0-PROD"

# ---------------- CONFIG ----------------
BASE = os.environ.get("SUI_BASE", "https://my.okx.com")
INST = "SUI-USD_UM_XPERP-310404"                # L1: whitelist
BAR = "1H"
HORA_MS = 3_600_000
MAX_CERRADAS = 2                                # L2 (enmienda "quita 1 vela")
LIMIT_FETCH = MAX_CERRADAS + 1
LIMIT_LIQ = 48
SEP_MIN = Decimal("0.0017")                     # peaje 0.17 % (casco certificado v2.2)
RR = Decimal("1.5")                             # PROPUESTA calibrable
TICKS_BUFFER = 2
TICK = Decimal("0.0001")
LEV_CAP = 20                                    # L6
LEV = max(1, int(os.environ.get("SUI_LEV", "1")))
SIZE_CT = min(max(int(os.environ.get("SUI_SIZE_CT", "1")), 1), 5)   # cap duro 5 ct
TD_MODE = os.environ.get("SUI_TD_MODE", "isolated")
MODE = os.environ.get("SUI_MODE", "TEST").upper()
ESTADO_DIR = os.environ.get("SUI_ESTADO_DIR", "estado_sui")
PARTES_PATH = os.path.join(ESTADO_DIR, "partes_test.json")
DECISIONES_PATH = os.path.join(ESTADO_DIR, "decisiones.jsonl")
STOP_PATH = os.path.join(ESTADO_DIR, "STOP")
TOLERANCIA_RELOJ_S = 30
MAX_VIGENCIA_PRECIO_MS = 600_000

# ---------------- utilidades ----------------
def http_get(url, reintentos=3):
    ultimo = None
    for i in range(reintentos):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": f"sui-aprendiz/{VERSION} (GSCSI-TRADE)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            ultimo = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET fallo tras {reintentos} intentos: {url} ({ultimo})")


def http_private(method: str, path: str, body=None):
    """Llamada autenticada OKX v5. SIN reintentos (un POST repetido = doble orden)."""
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
    req.add_header("User-Agent", f"sui-aprendiz/{VERSION} (GSCSI-TRADE)")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detalle = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code} en {path}: {detalle}") from e


def iso(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ahora_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def a_tick(p: Decimal) -> Decimal:
    return p.quantize(TICK, rounding=ROUND_HALF_UP)


def bitacora(msg: str) -> None:
    print(msg, flush=True)


def registrar_decision(reg: dict) -> None:
    os.makedirs(ESTADO_DIR, exist_ok=True)
    with open(DECISIONES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(reg, ensure_ascii=False) + "\n")


def cargar_partes() -> list:
    if not os.path.exists(PARTES_PATH):
        return []
    with open(PARTES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_partes(partes: list) -> None:
    os.makedirs(ESTADO_DIR, exist_ok=True)
    with open(PARTES_PATH, "w", encoding="utf-8") as f:
        json.dump(partes, f, ensure_ascii=False, indent=2)


# ---------------- fuentes publicas (guardias) ----------------
def servidor() -> int:
    return int(http_get(f"{BASE}/api/v5/public/time")["data"][0]["ts"])


def instrumento() -> dict:
    r = http_get(f"{BASE}/api/v5/public/instruments?instType=FUTURES&instId={INST}")
    assert r.get("code") == "0" and r.get("data"), "instrumento no respondio"
    d = r["data"][0]
    assert "USDT" not in d["instId"], "L1 VIOLADA: instId con USDT"
    assert d["settleCcy"] == "USD", "L1 VIOLADA: settleCcy no es USD"
    assert d["state"] == "live", "instrumento no live"
    assert d["ctType"] == "linear", "se esperaba lineal"
    assert Decimal(d["minSz"]) == 1 and Decimal(d["lotSz"]) == 1, "L5: granularidad inesperada"
    assert Decimal(d["tickSz"]) == TICK, "L5: tick inesperado"
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
    bid = Decimal(d["bids"][0][0])
    ask = Decimal(d["asks"][0][0])
    return {"ts": ts, "bid": bid, "ask": ask, "spread": ask - bid}


# ---------------- senal (L2: solo 2 velas cerradas) ----------------
def senal(v1: dict, v2: dict):
    c1c, c2c = Decimal(v1["c"]), Decimal(v2["c"])
    sep = (c2c - c1c) / c1c
    if sep >= SEP_MIN:
        return "LONG", sep
    if sep <= -SEP_MIN:
        return "SHORT", sep
    return None, sep


def niveles(lado: str, entrada: Decimal, v1: dict, v2: dict):
    """SL/TP desde extremos de las 2 velas + buffer; TP = RR x riesgo. (Decimal, tick)"""
    if lado == "LONG":
        sl = a_tick(min(Decimal(v1["l"]), Decimal(v2["l"])) - TICK * TICKS_BUFFER)
        riesgo = entrada - sl
        assert riesgo > 0, "riesgo no positivo en LONG"
        tp = a_tick(entrada + RR * riesgo)
    else:
        sl = a_tick(max(Decimal(v1["h"]), Decimal(v2["h"])) + TICK * TICKS_BUFFER)
        riesgo = sl - entrada
        assert riesgo > 0, "riesgo no positivo en SHORT"
        tp = a_tick(entrada - RR * riesgo)
    assert tp > 0, "TP no positivo"
    return sl, tp, riesgo


def construir_parte(modo: str, lado: str, entrada_ref: Decimal, sl: Decimal, tp: Decimal,
                    riesgo: Decimal, vel: dict, sep: Decimal, specs: dict) -> dict:
    ct_val = Decimal(specs["ctVal"])
    margen = (entrada_ref * ct_val * Decimal(SIZE_CT) / Decimal(LEV)).quantize(Decimal("0.0001"))
    return {
        "id": f"{vel['c2']['ts']}-{lado}-{modo}",
        "abierto_ts": ahora_iso(),
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
        "size_ct": SIZE_CT,
        "lev": LEV,
        "td_mode": TD_MODE if modo == "LIVE" else None,
        "margen_usd": str(margen),
        "ordId": None,
        "sltp_confirmado": None,
        "estado": "ABIERTO",
        "resultado": None,
    }


# ---------------- liquidador de PAPEL (solo partes modo TEST) ----------------
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
    """Contratos SUI en la cuenta para este instId (0 si ninguno)."""
    r = http_private("GET", f"/api/v5/account/positions?instId={INST}")
    if r.get("code") != "0":
        raise RuntimeError(f"positions rechazo: {r.get('msg')}")
    total = Decimal(0)
    for p in r.get("data") or []:
        try:
            total += abs(Decimal(p.get("pos") or "0"))
        except Exception:  # noqa: BLE001
            pass
    return total


def arca_usd():
    """Saldo disponible USD (best-effort; None si no se puede leer)."""
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


def set_apalancamiento():
    r = http_private("POST", "/api/v5/account/set-leverage",
                     {"instId": INST, "lever": str(LEV), "mgnMode": TD_MODE})
    if r.get("code") != "0":
        raise RuntimeError(f"set-leverage rechazo: code={r.get('code')} msg={r.get('msg')}")


def payload_orden(lado: str, sl: Decimal, tp: Decimal, config: dict) -> dict:
    p = {
        "instId": INST,
        "tdMode": TD_MODE,
        "posSide": ("long" if lado == "LONG" else "short")
                   if config["posMode"] == "long_short_mode" else "net",
        "side": "buy" if lado == "LONG" else "sell",
        "ordType": "market",
        "sz": str(SIZE_CT),
        "attachAlgoOrds": [{
            "tpTriggerPx": str(tp), "tpOrdPx": "-1",
            "slTriggerPx": str(sl), "slOrdPx": "-1",
        }],
    }
    return p


def enviar_orden(payload: dict) -> str:
    r = http_private("POST", "/api/v5/trade/order", payload)
    if r.get("code") != "0":
        raise RuntimeError(f"orden rechazada: code={r.get('code')} msg={r.get('msg')}")
    d = r["data"][0]
    if d.get("sCode") != "0":
        raise RuntimeError(f"orden rechazada: sCode={d.get('sCode')} sMsg={d.get('sMsg')}")
    return d["ordId"]


def fill_de_entrada(ord_id: str):
    """Fill de la orden de entrada (por ordId). None si aun no aparece."""
    r = http_private("GET", f"/api/v5/trade/fills-history?instType=FUTURES&instId={INST}&limit=20")
    for f in r.get("data") or []:
        if f.get("ordId") == ord_id:
            return {"avgPx": f["fillPx"], "sz": f["fillSz"], "fee": f.get("fee"),
                    "ts": int(f["ts"]), "iso": iso(f["ts"])}
    return None


def verificar_algo_sltp(ord_id: str) -> bool:
    """Confirma que el SL/TP adjunto vive en el exchange. Mejor esfuerzo."""
    for algo_cl in (None, "oco", "conditional"):
        try:
            q = f"/api/v5/trade/orders-algo-pending?instId={INST}&ordId={ord_id}"
            if algo_cl:
                q += f"&algoCl={algo_cl}"
            r = http_private("GET", q)
            if r.get("code") == "0" and r.get("data"):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def reconciliar_live(partes: list, specs: dict) -> list:
    """Resuelve partes LIVE: busca fill de entrada y, si la posicion acabo, la salida."""
    ct_val = Decimal(specs["ctVal"])
    cerrados = []
    hay_live_abierto = any(p["estado"] == "ABIERTO" and p.get("modo") == "LIVE" for p in partes)
    pos = posicion_abierta() if hay_live_abierto else Decimal(0)
    fills = None
    for p in partes:
        if p["estado"] != "ABIERTO" or p.get("modo") != "LIVE":
            continue
        # 1) fill de entrada pendiente
        if p.get("entrada_fill") is None and p.get("ordId"):
            try:
                f = fill_de_entrada(p["ordId"])
                if f:
                    p["entrada_fill"] = f
                    bitacora(f"[FILL ENTRADA] {p['id']}: avgPx {f['avgPx']} (ref {p['entrada_ref']}) "
                             f"fee {f['fee']}")
            except Exception as e:  # noqa: BLE001
                bitacora(f"[aviso] fill entrada: {e}")
        # 2) confirmacion SL/TP
        if p.get("sltp_confirmado") is not True and p.get("ordId"):
            p["sltp_confirmado"] = verificar_algo_sltp(p["ordId"])
            if not p["sltp_confirmado"]:
                bitacora(f"[CRITICO] {p['id']}: SL/TP NO confirmado en exchange "
                         f"(se reintenta la proxima pasada)")
        # 3) cierre: posicion a cero -> reconstruccion por fills
        if pos == 0:
            if fills is None:
                r = http_private("GET",
                                 f"/api/v5/trade/fills-history?instType=FUTURES&instId={INST}&limit=50")
                fills = r.get("data") or []
            lado_salida = "buy" if p["lado"] == "SHORT" else "sell"
            salida, fees = [], Decimal(0)
            ts_desde = int((p.get("entrada_fill") or {}).get("ts", 0)) or None
            for f in fills:
                if f.get("side") != lado_salida:
                    continue
                if ts_desde and int(f["ts"]) < ts_desde:
                    continue
                salida.append(f)
                fees += Decimal(f.get("fee") or "0")
                if sum(Decimal(x["fillSz"]) for x in salida) >= Decimal(p["size_ct"]):
                    break
            if not salida:
                continue
            sz_sal = sum(Decimal(x["fillPx"]) * Decimal(x["fillSz"]) for x in salida)
            n_sal = sum(Decimal(x["fillSz"]) for x in salida)
            salida_avg = (sz_sal / n_sal).quantize(Decimal("0.0001"))
            entrada_px = Decimal((p.get("entrada_fill") or {}).get("avgPx", p["entrada_ref"]))
            bruto = ((salida_avg - entrada_px) if p["lado"] == "LONG"
                     else (entrada_px - salida_avg)) * ct_val * Decimal(p["size_ct"])
            p["estado"] = "CERRADO_EXCHANGE"
            p["resultado"] = {
                "tipo": "CIERRE_EXCHANGE (SL/TP reconstruidos por fills)",
                "salida": str(salida_avg),
                "pnl_usd_bruto": str(bruto.quantize(Decimal("0.0001"))),
                "fees_usd": str((-fees).quantize(Decimal("0.0001"))),
                "pnl_usd_neto": str((bruto + fees).quantize(Decimal("0.0001"))),
                "fills_salida": len(salida),
            }
            cerrados.append(p["id"])
    return cerrados


# ---------------- main ----------------
def main() -> int:
    bitacora("=" * 66)
    bitacora(f"EL APRENDIZ — sui_bot.py v{VERSION} — MODO {MODE}")
    bitacora(f"Ley de velas: MAXIMO {MAX_CERRADAS} completas | {INST}")
    bitacora(f"Peaje {SEP_MIN} | RR {RR} | SL extremo+{TICKS_BUFFER}t | size {SIZE_CT} ct | "
             f"lev {LEV}x (cap {LEV_CAP}) | tdMode {TD_MODE}")
    if MODE == "LIVE":
        bitacora("PRODUCCION: ordenes REALES con SL/TP adjuntos en el exchange")
    elif MODE == "PREVUELA":
        bitacora("PREVUELA: autentica y arma la orden; NO la envia")
    else:
        bitacora("TEST: papel, sin ordenes, sin secretos")
    bitacora("=" * 66)

    if MODE not in ("TEST", "PREVUELA", "LIVE"):
        bitacora(f"[ERROR] SUI_MODE desconocido: {MODE}")
        return 1

    # L10: kill-switch
    if os.path.exists(STOP_PATH):
        bitacora("[STOP] kill-switch presente — ninguna entrada hoy")
        registrar_decision({"ts_run": ahora_iso(), "modo": MODE, "decision": "SKIP_STOP"})
        return 0

    notas, guardias = [], {}
    try:
        server_ms = servidor()
        deriva_s = abs(datetime.now(timezone.utc).timestamp() * 1000 - server_ms) / 1000
        guardias["reloj"] = "OK" if deriva_s <= TOLERANCIA_RELOJ_S else f"DERIVA {deriva_s:.0f}s"
        if guardias["reloj"] != "OK":
            notas.append("L4: reloj descuadrado — no decide")
            registrar_decision({"ts_run": ahora_iso(), "modo": MODE, "decision": "SKIP_RELOJ",
                                "guardias": guardias, "notas": notas})
            bitacora(f"[SKIP] {notas}")
            return 0
        specs = instrumento()
        guardias["leyes_L1_L5_L6"] = "OK"
        vel = velas(server_ms)
        guardias["velas_L2_L3"] = "OK"
        book = libro(server_ms)
        guardias["libro_L4"] = "OK"
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

    # ------- CANAL PAPEL (siempre que haya partes TEST abiertos) -------
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
             f"| peaje {SEP_MIN}")
    bitacora(f"Libro: bid {book['bid']} / ask {book['ask']} "
             f"(spread {book['spread'].quantize(Decimal('0.0001'))})")

    # ------- MODO TEST: fin del flujo (papel) -------
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
            sl, tp, riesgo = niveles(lado, entrada, vel["c1"], vel["c2"])
            p = construir_parte("TEST", lado, entrada, sl, tp, riesgo, vel, sep, specs)
            partes.append(p)
            parte_id, decision = p["id"], "PARTE_ABIERTO"
            bitacora(f"[PARTE DE PRUEBA] {p['id']}: {p['lado']} ref {p['entrada_ref']} "
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

    # reconciliar canal LIVE (GETs inocuos)
    try:
        cerrados_live = reconciliar_live(partes, specs)
        if cerrados_live:
            bitacora(f"Reconciliador LIVE: cerrados -> {', '.join(cerrados_live)}")
            for p in partes:
                if p["id"] in cerrados_live:
                    r_ = p["resultado"]
                    bitacora(f"  {p['id']}: {r_['tipo']} salida {r_['salida']} "
                             f"pnl neto {r_.get('pnl_usd_neto')} USD (fees {r_.get('fees_usd')})")
    except Exception as e:  # noqa: BLE001
        registrar_decision({"ts_run": ahora_iso(), "modo": MODE, "decision": "ERROR_RECON",
                            "error": str(e)})
        bitacora(f"[CRITICO] reconciliacion fallo: {e}")
        guardar_partes(partes)
        return 1

    pos = posicion_abierta()
    live_abierto = any(p["estado"] == "ABIERTO" and p.get("modo") == "LIVE" for p in partes)
    sl, tp, riesgo = (None, None, None)
    if lado is not None:
        entrada = book["ask"] if lado == "LONG" else book["bid"]
        sl, tp, riesgo = niveles(lado, entrada, vel["c1"], vel["c2"])

    decision, parte_id = "SIN_SENAL", None
    if lado is None:
        decision = "SIN_SENAL"
        bitacora("[FIN] sin senal")
    elif live_abierto:
        decision = "PARTIDO_EN_MARCHA"
        bitacora("[SKIP] ya hay un parte LIVE abierto — un solo barco por canal")
    elif pos > 0:
        decision = "SKIP_POSICION_EXTERNA"
        bitacora(f"[SKIP] L9: la cuenta ya tiene {pos} ct de SUI que NO son del bot "
                 f"(posicion manual?) — no se encima nada. Resuelve esa nave para dejar "
                 f"navegar a EL APRENDIZ.")
    elif any(p["id"] == f"{vel['c2']['ts']}-{lado}-LIVE" for p in partes):
        decision = "DUPLICADO"
        bitacora("[SKIP] esta vela ya produjo un parte LIVE (idempotencia)")
    else:
        # ------- construccion de la orden -------
        entrada = book["ask"] if lado == "LONG" else book["bid"]
        margen = (entrada * Decimal(specs["ctVal"]) * Decimal(SIZE_CT) / Decimal(LEV))
        arca = arca_usd()
        if arca is not None and arca < margen * Decimal("2"):
            decision = "SKIP_ARCA_INSUFICIENTE"
            bitacora(f"[SKIP] arca disponible {arca} USD < 2x margen {margen:.4f} USD")
        elif MODE == "PREVUELA":
            payload = payload_orden(lado, sl, tp, config)
            decision = "PREVUELA_ORDEN_ARMADA"
            bitacora("[PREVUELA] orden armada (NO enviada):")
            bitacora(json.dumps(payload, indent=2, ensure_ascii=False))
            bitacora(f"[PREVUELA] entrada_ref {entrada} | SL {sl} | TP {tp} | "
                     f"riesgo {riesgo.quantize(Decimal('0.0001'))} | margen ~{margen:.4f} USD")
            notas.append("preVuela: nada enviado")
        else:  # LIVE
            try:
                set_apalancamiento()
                payload = payload_orden(lado, sl, tp, config)
                ord_id = enviar_orden(payload)
                p = construir_parte("LIVE", lado, entrada, sl, tp, riesgo, vel, sep, specs)
                p["ordId"] = ord_id
                partes.append(p)
                parte_id, decision = p["id"], "ORDEN_ENVIADA"
                bitacora(f"[PRODUCCION] ORDEN ENVIADA: {lado} {SIZE_CT} ct | ordId {ord_id}")
                time.sleep(2)
                f = fill_de_entrada(ord_id)
                if f:
                    p["entrada_fill"] = f
                    bitacora(f"[FILL ENTRADA] avgPx {f['avgPx']} (ref {entrada}) | fee {f['fee']}")
                time.sleep(2)
                p["sltp_confirmado"] = verificar_algo_sltp(ord_id)
                bitacora(f"[SL/TP] {'CONFIRMADO en exchange' if p['sltp_confirmado'] else 'CRITICO: sin confirmacion (reintento la proxima pasada)'}")
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
