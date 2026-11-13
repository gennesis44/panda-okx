#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EL APRENDIZ — main-1inj.py — v2.2-PROD
=======================================
Flota GSCSI-TRADE | nave: EL APRENDIZ
Instrumento: INJ-USD_UM_XPERP (OKX my.okx.com, universo EEE/XPERP, margen USD)
Ejecución: Cada 20 minutos | Lógica: EMA 4 / EMA 21 sobre velas de 1H

MODOS (env INJ_MODE):
  TEST     Papel. Sin órdenes, sin secretos, solo endpoints públicos.
  PREVUELA Autentica contra /account (GET), arma la orden y la IMPRIME (no envía).
  LIVE     PRODUCCIÓN. Orden market real con SL/TP adjuntos en el exchange.

LEYES A BORDO (guardas duras, fallan cerrado):
 L1. USDT VETADO: Whitelist de un solo instId; assert 'USDT' fuera y settleCcy == 'USD'.
 L2. EMA 4 / EMA 21: Cálculo sobre los cierres de las últimas velas cerradas de 1H.
 L3. CADENCIA 20M: Se ejecuta cada 20 min para vigilar y reaccionar con agilidad.
 L4. VIGENCIA: Reloj vs /public/time (30 s); libro con ts < 10 min.
 L5. TAMAÑO: Contratos enteros (~10 EUR nocionales equivalentes).
 L6. APALANCAMIENTO <= 20x (cap del contrato); default 1x.
 L7. UN SOLO PARTE ABIERTO por modo.
 L8. POSICIÓN EXTERNA: Si la cuenta tiene INJ que no es del bot, no se abre nada.
 L9. KILL-SWITCH: Si existe estado_inj/STOP, ninguna entrada.
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

VERSION = "2.2-PROD"

# ---------------- CONFIGURACIÓN ----------------
BASE = os.environ.get("INJ_BASE", "https://my.okx.com")
INST = "INJ-USD_UM_XPERP"                      # L1: Whitelist INJ sin USDT
BAR = "1H"
HORA_MS = 3_600_000
MAX_CERRADAS = 25                               # Necesitamos al menos 21 velas para EMA 21
LIMIT_FETCH = MAX_CERRADAS + 2
LIMIT_LIQ = 48
SEP_MIN = Decimal("0.0015")                     # Peaje mínimo de separación EMA
RR = Decimal("1.8")                             # Ratio Riesgo / Beneficio
TICKS_BUFFER = 2
TICK = Decimal("0.001")                         # Tick size para INJ perpetuo USD
LEV_CAP = 20                                    # L6
LEV = max(1, int(os.environ.get("INJ_LEV", "1")))
SIZE_CT = min(max(int(os.environ.get("INJ_SIZE_CT", "1")), 1), 5)
TD_MODE = os.environ.get("INJ_TD_MODE", "isolated")
MODE = os.environ.get("INJ_MODE", "TEST").upper()
ESTADO_DIR = os.environ.get("INJ_ESTADO_DIR", "estado_inj")
PARTES_PATH = os.path.join(ESTADO_DIR, "partes_test.json")
DECISIONES_PATH = os.path.join(ESTADO_DIR, "decisiones.jsonl")
STOP_PATH = os.path.join(ESTADO_DIR, "STOP")
TOLERANCIA_RELOJ_S = 30
MAX_VIGENCIA_PRECIO_MS = 600_000

# ---------------- UTILIDADES ----------------
def http_get(url, reintentos=3):
    ultimo = None
    for i in range(reintentos):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": f"main-1inj/{VERSION} (GSCSI-TRADE)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            ultimo = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET falló tras {reintentos} intentos: {url} ({ultimo})")


def http_private(method: str, path: str, body=None):
    key = os.environ.get("OKX_API_KEY", "")
    sec = os.environ.get("OKX_API_SECRET", "")
    pas = os.environ.get("OKX_API_PASSPHRASE", "")
    if not (key and sec and pas):
        raise RuntimeError("FALTAN_SECRETOS: define OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE")
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
    req.add_header("User-Agent", f"main-1inj/{VERSION} (GSCSI-TRADE)")
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


# ---------------- FUENTES PÚBLICAS Y GUARDIAS ----------------
def servidor() -> int:
    return int(http_get(f"{BASE}/api/v5/public/time")["data"][0]["ts"])


def instrumento() -> dict:
    r = http_get(f"{BASE}/api/v5/public/instruments?instType=FUTURES&instId={INST}")
    assert r.get("code") == "0" and r.get("data"), "Instrumento no respondió"
    d = r["data"][0]
    assert "USDT" not in d["instId"], "L1 VIOLADA: instId contiene USDT"
    assert d["settleCcy"] == "USD", "L1 VIOLADA: settleCcy no es USD"
    assert d["state"] == "live", "Instrumento no live"
    assert d["ctType"] == "linear", "Se esperaba contrato lineal"
    assert Decimal(d["minSz"]) == 1 and Decimal(d["lotSz"]) == 1, "L5: granularidad inesperada"
    assert Decimal(d["tickSz"]) == TICK, "L5: tick inesperado"
    assert int(d["lever"]) >= LEV, "L6: apalancamiento pedido supera el contrato"
    assert LEV <= LEV_CAP, "L6 VIOLADA: apalancamiento por encima del cap"
    return d


def calcular_ema(precios: list[Decimal], periodo: int) -> list[Decimal]:
    k = Decimal(2) / Decimal(periodo + 1)
    emas = [precios[0]]
    for precio in precios[1:]:
        ema_curr = (precio * k) + (emas[-1] * (Decimal(1) - k))
        emas.append(ema_curr)
    return emas


def analizar_mercado() -> tuple[str | None, Decimal, dict]:
    r = http_get(f"{BASE}/api/v5/market/candles?instId={INST}&bar={BAR}&limit={LIMIT_FETCH}")
    rows = r.get("data") or []
    cerradas = [x for x in rows if x[8] == "1"]
    # Ordenar cronológicamente de más antiguo a más reciente
    cerradas = sorted(cerradas, key=lambda x: int(x[0]))
    assert len(cerradas) >= 21, "L2: se necesitan al menos 21 velas cerradas para EMA 21"
    
    precios_cierre = [Decimal(x[4]) for x in cerradas]
    ema4_list = calcular_ema(precios_cierre, 4)
    ema21_list = calcular_ema(precios_cierre, 21)
    
    e4_actual = ema4_list[-1]
    e21_actual = ema21_list[-1]
    sep = (e4_actual - e21_actual) / e21_actual
    
    ultima_vela = cerradas[-1]
    penultima_vela = cerradas[-2]
    
    velas_info = {
        "c1": {"ts": int(penultima_vela[0]), "l": penultima_vela[3], "h": penultima_vela[2], "c": penultima_vela[4]},
        "c2": {"ts": int(ultima_vela[0]), "l": ultima_vela[3], "h": ultima_vela[2], "c": ultima_vela[4]}
    }
    
    if e4_actual > e21_actual and abs(sep) >= SEP_MIN:
        return "LONG", sep, velas_info
    elif e4_actual < e21_actual and abs(sep) >= SEP_MIN:
        return "SHORT", sep, velas_info
    return None, sep, velas_info


def libro(server_ms: int) -> dict:
    r = http_get(f"{BASE}/api/v5/market/books?instId={INST}&sz=5")
    d = r["data"][0]
    ts = int(d["ts"])
    assert server_ms - ts < MAX_VIGENCIA_PRECIO_MS, "L4: Libro no vigente"
    bid = Decimal(d["bids"][0][0])
    ask = Decimal(d["asks"][0][0])
    return {"ts": ts, "bid": bid, "ask": ask, "spread": ask - bid}


def niveles(lado: str, entrada: Decimal, velas_info: dict):
    v1, v2 = velas_info["c1"], velas_info["c2"]
    if lado == "LONG":
        sl = a_tick(min(Decimal(v1["l"]), Decimal(v2["l"])) - TICK * TICKS_BUFFER)
        riesgo = entrada - sl
        assert riesgo > 0, "Riesgo no positivo en LONG"
        tp = a_tick(entrada + RR * riesgo)
    else:
        sl = a_tick(max(Decimal(v1["h"]), Decimal(v2["h"])) + TICK * TICKS_BUFFER)
        riesgo = sl - entrada
        assert riesgo > 0, "Riesgo no positivo en SHORT"
        tp = a_tick(entrada - RR * riesgo)
    assert tp > 0, "TP no positivo"
    return sl, tp, riesgo


def construir_parte(modo: str, lado: str, entrada_ref: Decimal, sl: Decimal, tp: Decimal,
                    riesgo: Decimal, velas_info: dict, sep: Decimal, specs: dict) -> dict:
    ct_val = Decimal(specs["ctVal"])
    margen = (entrada_ref * ct_val * Decimal(SIZE_CT) / Decimal(LEV)).quantize(Decimal("0.0001"))
    return {
        "id": f"{velas_info['c2']['ts']}-{lado}-{modo}",
        "abierto_ts": ahora_iso(),
        "modo": modo,
        "inst": INST,
        "lado": lado,
        "velas": velas_info,
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


def liquidar_papel(partes: list, specs: dict) -> list:
    r = http_get(f"{BASE}/api/v5/market/candles?instId={INST}&bar={BAR}&limit={LIMIT_LIQ}")
    chron = sorted([x for x in r["data"] if x[8] == "1"], key=lambda x: int(x[0]))
    ct_val = Decimal(specs["ctVal"])
    cerrados = []
    for p in partes:
        if p["estado"] != "ABIERTO" or p.get("modo", "TEST") != "TEST":
            continue
        c2_ts = int(p["velas"]["c2"]["ts"])
        sl, tp = Decimal(p["sl"]), Decimal(p["tp"])
        for x in [y for y in chron if int(y[0]) > c2_ts]:
            low, high = Decimal(x[3]), Decimal(x[2])
            if p["lado"] == "LONG":
                hit_sl, hit_tp = low <= sl, high >= tp
            else:
                hit_sl, hit_tp = high >= sl, low <= tp
            if hit_sl:
                salida, tipo = sl, "SL"
            elif hit_tp:
                salida, tipo = tp, "TP"
            else:
                continue
            diff = (salida - Decimal(p["entrada_ref"])) if p["lado"] == "LONG" \
                else (Decimal(p["entrada_ref"]) - salida)
            pnl = (diff * ct_val * Decimal(p["size_ct"])).quantize(Decimal("0.0001"))
            p["estado"] = f"CERRADO_{tipo}"
            p["resultado"] = {"tipo": tipo, "salida": str(salida), "pnl_usd_bruto": str(pnl),
                              "cerrado_con_vela_ts": int(x[0]), "cerrado_con_vela_iso": iso(x[0])}
            cerrados.append(p["id"])
            break
    return cerrados


def cuenta_config() -> dict:
    r = http_private("GET", "/api/v5/account/config")
    if r.get("code") != "0":
        raise RuntimeError(f"Config error: {r.get('msg')}")
    d = r["data"][0]
    return {"posMode": d.get("posMode"), "acctLv": d.get("acctLv")}


def posicion_abierta() -> Decimal:
    r = http_private("GET", f"/api/v5/account/positions?instId={INST}")
    if r.get("code") != "0":
        return Decimal(0)
    total = Decimal(0)
    for p in r.get("data") or []:
        try:
            total += abs(Decimal(p.get("pos") or "0"))
        except Exception:
            pass
    return total


def arca_usd():
    try:
        r = http_private("GET", "/api/v5/account/balance?ccy=USD")
        for det in (r.get("data") or [{}])[0].get("details", []):
            if det.get("ccy") == "USD":
                for k in ("availEq", "availBal", "cashBal"):
                    if det.get(k):
                        return Decimal(det[k])
    except Exception:
        pass
    return None


def set_apalancamiento():
    http_private("POST", "/api/v5/account/set-leverage",
                 {"instId": INST, "lever": str(LEV), "mgnMode": TD_MODE})


def payload_orden(lado: str, sl: Decimal, tp: Decimal, config: dict) -> dict:
    return {
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


def enviar_orden(payload: dict) -> str:
    r = http_private("POST", "/api/v5/trade/order", payload)
    if r.get("code") != "0":
        raise RuntimeError(f"Orden rechazada: {r.get('msg')}")
    d = r["data"][0]
    if d.get("sCode") != "0":
        raise RuntimeError(f"Orden sCode error: {d.get('sMsg')}")
    return d["ordId"]


def fill_de_entrada(ord_id: str):
    r = http_private("GET", f"/api/v5/trade/fills-history?instType=FUTURES&instId={INST}&limit=20")
    for f in r.get("data") or []:
        if f.get("ordId") == ord_id:
            return {"avgPx": f["fillPx"], "sz": f["fillSz"], "fee": f.get("fee"), "ts": int(f["ts"])}
    return None


def verificar_algo_sltp(ord_id: str) -> bool:
    for algo_cl in (None, "oco", "conditional"):
        try:
            q = f"/api/v5/trade/orders-algo-pending?instId={INST}&ordId={ord_id}"
            if algo_cl:
                q += f"&algoCl={algo_cl}"
            r = http_private("GET", q)
            if r.get("code") == "0" and r.get("data"):
                return True
        except Exception:
            continue
    return False


def reconciliar_live(partes: list, specs: dict) -> list:
    ct_val = Decimal(specs["ctVal"])
    cerrados = []
    hay_live = any(p["estado"] == "ABIERTO" and p.get("modo") == "LIVE" for p in partes)
    pos = posicion_abierta() if hay_live else Decimal(0)
    fills = None
    for p in partes:
        if p["estado"] != "ABIERTO" or p.get("modo") != "LIVE":
            continue
        if p.get("entrada_fill") is None and p.get("ordId"):
            f = fill_de_entrada(p["ordId"])
            if f:
                p["entrada_fill"] = f
        if p.get("sltp_confirmado") is not True and p.get("ordId"):
            p["sltp_confirmado"] = verificar_algo_sltp(p["ordId"])
        if pos == 0:
            if fills is None:
                r = http_private("GET", f"/api/v5/trade/fills-history?instType=FUTURES&instId={INST}&limit=50")
                fills = r.get("data") or []
            lado_salida = "buy" if p["lado"] == "SHORT" else "sell"
            salida, fees = [], Decimal(0)
            for f in fills:
                if f.get("side") != lado_salida:
                    continue
                salida.append(f)
                fees += Decimal(f.get("fee") or "0")
                if sum(Decimal(x["fillSz"]) for x in salida) >= Decimal(p["size_ct"]):
                    break
            if not salida:
                continue
            sz_sal = sum(Decimal(x["fillPx"]) * Decimal(x["fillSz"]) for x in salida)
            n_sal = sum(Decimal(x["fillSz"]) for x in salida)
            salida_avg = (sz_sal / n_sal).quantize(Decimal("0.001"))
            entrada_px = Decimal((p.get("entrada_fill") or {}).get("avgPx", p["entrada_ref"]))
            bruto = ((salida_avg - entrada_px) if p["lado"] == "LONG" else (entrada_px - salida_avg)) * ct_val * Decimal(p["size_ct"])
            p["estado"] = "CERRADO_EXCHANGE"
            p["resultado"] = {
                "tipo": "CIERRE_EXCHANGE",
                "salida": str(salida_avg),
                "pnl_usd_bruto": str(bruto.quantize(Decimal("0.0001"))),
                "fees_usd": str((-fees).quantize(Decimal("0.0001"))),
                "pnl_usd_neto": str((bruto + fees).quantize(Decimal("0.0001")))
            }
            cerrados.append(p["id"])
    return cerrados


def main() -> int:
    bitacora("=" * 66)
    bitacora(f"EL APRENDIZ — main-1inj.py v{VERSION} — MODO {MODE}")
    bitacora(f"Estrategia: EMA 4 / EMA 21 (1H) | Cadencia: 20 min | {INST}")
    bitacora("=" * 66)

    if os.path.exists(STOP_PATH):
        bitacora("[STOP] Kill-switch activo.")
        return 0

    try:
        server_ms = servidor()
        specs = instrumento()
        lado, sep, velas_info = analizar_mercado()
        book = libro(server_ms)
    except AssertionError as e:
        bitacora(f"[SKIP] Guardia: {e}")
        return 0
    except Exception as e:
        bitacora(f"[ERROR] {e}")
        traceback.print_exc()
        return 1

    partes = cargar_partes()
    if MODE == "TEST":
        liquidar_papel(partes, specs)

    sensor = lado or "NEUTRO"
    bitacora(f"Sensor EMA 4/21: {sensor} | Separación: {(sep * 100).quantize(Decimal('0.0001'))}%")

    if MODE == "TEST":
        decision, parte_id = "SIN_SENAL", None
        if lado is not None and not any(p["estado"] == "ABIERTO" and p.get("modo") == "TEST" for p in partes):
            entrada = book["ask"] if lado == "LONG" else book["bid"]
            sl, tp, riesgo = niveles(lado, entrada, velas_info)
            p = construir_parte("TEST", lado, entrada, sl, tp, riesgo, velas_info, sep, specs)
            partes.append(p)
            parte_id, decision = p["id"], "PARTE_ABIERTO"
            bitacora(f"[TEST PARTE] {p['id']}: {lado} SL {sl} TP {tp}")
        guardar_partes(partes)
        registrar_decision({"ts_run": ahora_iso(), "modo": "TEST", "sensor": sensor, "decision": decision})
        return 0

    # LIVE / PREVUELA
    try:
        config = cuenta_config()
        reconciliar_live(partes, specs)
    except Exception as e:
        bitacora(f"[ERROR AUTH/RECON] {e}")
        return 1

    pos = posicion_abierta()
    live_abierto = any(p["estado"] == "ABIERTO" and p.get("modo") == "LIVE" for p in partes)

    decision, parte_id = "SIN_SENAL", None
    if lado is None:
        bitacora("[FIN] Sin señal.")
    elif live_abierto:
        bitacora("[SKIP] Ya hay un parte LIVE abierto.")
    elif pos > 0:
        bitacora("[SKIP] Posición externa detectada en la cuenta.")
    else:
        entrada = book["ask"] if lado == "LONG" else book["bid"]
        sl, tp, riesgo = niveles(lado, entrada, velas_info)
        if MODE == "PREVUELA":
            payload = payload_orden(lado, sl, tp, config)
            bitacora("[PREVUELA] Orden armada:\n" + json.dumps(payload, indent=2))
        else:
            try:
                set_apalancamiento()
                payload = payload_orden(lado, sl, tp, config)
                ord_id = enviar_orden(payload)
                p = construir_parte("LIVE", lado, entrada, sl, tp, riesgo, velas_info, sep, specs)
                p["ordId"] = ord_id
                partes.append(p)
                bitacora(f"[LIVE] Orden enviada: {ord_id}")
            except Exception as e:
                bitacora(f"[ERROR ORDEN] {e}")

    guardar_partes(partes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
