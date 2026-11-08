#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EL APRENDIZ — sui_bot.py — v1.0-TEST
====================================
Flota GSCSI-TRADE | nave: EL APRENDIZ
Instrumento: SUI-USD_UM_XPERP-310404 (OKX, universo EEE/XPERP, margen USD)

MODO TEST: este barco NO envia ordenes. Cada pasada:
  1) revisa guardias (leyes a bordo)
  2) lee EXACTAMENTE 2 velas 1H completas
  3) evalua la senal escalera de 2 cierres con peaje
  4) si hay senal y no hay parte abierto: anota un PARTE DE PRUEBA
  5) liquida los partes abiertos contra velas cerradas nuevas (liquidador)
Puerta para zarpar en LIVE: >= 20-30 partes cerrados + revision del FANAL.
Hasta entonces place_order() esta soldado con candado.

Casco construido desde cero sobre el datapack verificado en vivo 2026-11-07
(datapack_sui_bot_test_v1.txt). Sin herencia del astillero viejo.
Sin secretos en modo TEST: solo endpoints publicos.

LEYES A BORDO (guardas duras, fallan cerrado):
 L1. USDT VETADO: whitelist de un solo instId; assert 'USDT' fuera y settleCcy=='USD'.
 L2. MAXIMO 2 VELAS COMPLETAS ANTERIORES (enmienda del capitan: quita 1 vela):
     fetch limit=3 -> descartar la vela en formacion (confirm=0) -> usar EXACTAMENTE 2.
     El liquidador usa velas SOLO para resolver partes abiertos (high/low);
     jamas para decidir entradas.
 L3. VELAS CONTIGUAS 1H: c2.ts-c1.ts == 3600000 ms; c2 = la ultima hora completa;
     la vela en formacion arranca justo tras c2. Si no -> no decide.
 L4. VIGENCIA: reloj local vs /public/time (tolerancia 30 s); libro con ts < 10 min.
 L5. TAMANO: minSz=1, lotSz=1 -> contratos enteros; precios en multiplos de 0.0001 (Decimal).
 L6. APALANCAMIENTO <= 20x (cap del contrato).
 L7. La memoria del barco es su libro de partes (estado_sui/); prohibida la historia larga.

Uso local:  python3 sui_bot.py
Repo (GitHub Actions): colocar en la raiz + .github/workflows/sui-aprendiz.yml
Estado: estado_sui/partes_test.json (libro) + estado_sui/decisiones.jsonl (bitacora)
"""

import json
import os
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

VERSION = "1.0-TEST"

# ---------------- CONFIG (constantes de flota; nada entra por config libre) ----------------
BASE = os.environ.get("SUI_BASE", "https://my.okx.com")   # host exigido por el capitan
INST = "SUI-USD_UM_XPERP-310404"                           # L1: whitelist de un solo instId
BAR = "1H"
HORA_MS = 3_600_000
MAX_CERRADAS = 2            # L2: enmienda "quita 1 vela" (antes 3)
LIMIT_FETCH = MAX_CERRADAS + 1
LIMIT_LIQ = 48              # velas del liquidador (solo high/low; nunca senal)
SEP_MIN = Decimal("0.0017") # peaje 0.17 % — heredado del casco certificado v2.2 (calibrable)
RR = Decimal("1.5")         # PROPUESTA, a calibrar con el libro de partes
TICKS_BUFFER = 2            # SL tras el extremo de las 2 velas + 2 ticks
TICK = Decimal("0.0001")
LEV_CAP = 20                # L6
LEV = int(os.environ.get("SUI_LEV", "1"))   # default conservador 1x (margen = nocional)
ESTADO_DIR = os.environ.get("SUI_ESTADO_DIR", "estado_sui")
PARTES_PATH = os.path.join(ESTADO_DIR, "partes_test.json")
DECISIONES_PATH = os.path.join(ESTADO_DIR, "decisiones.jsonl")
TOLERANCIA_RELOJ_S = 30
MAX_VIGENCIA_PRECIO_MS = 600_000   # L4: 10 minutos

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


# ---------------- guardias y fuentes ----------------
def servidor() -> int:
    r = http_get(f"{BASE}/api/v5/public/time")
    return int(r["data"][0]["ts"])


def instrumento() -> dict:
    r = http_get(f"{BASE}/api/v5/public/instruments?instType=FUTURES&instId={INST}")
    assert r.get("code") == "0" and r.get("data"), "instrumento no respondio"
    d = r["data"][0]
    # L1: veto USDT
    assert "USDT" not in d["instId"], "L1 VIOLADA: instId con USDT"
    assert d["settleCcy"] == "USD", "L1 VIOLADA: settleCcy no es USD"
    # L5/L6: granularidad y apalancamiento
    assert d["state"] == "live", "instrumento no live"
    assert d["ctType"] == "linear", "se esperaba lineal (datapack)"
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
    c2, c1 = cerradas[0], cerradas[1]           # la respuesta viene newest-first
    assert int(c2[0]) - int(c1[0]) == HORA_MS, "L3: hueco entre c1 y c2"
    hora_actual = (server_ms // HORA_MS) * HORA_MS
    assert int(c2[0]) == hora_actual - HORA_MS, \
        "L3: c2 no es la ultima hora completa (velas descuadradas)"
    if formando:
        assert int(formando[0][0]) == int(c2[0]) + HORA_MS, "L3: cadencia 1H rota"
    return {
        "c1": {"ts": int(c1[0]), "o": c1[1], "h": c1[2], "l": c1[3], "c": c1[4]},
        "c2": {"ts": int(c2[0]), "o": c2[1], "h": c2[2], "l": c2[3], "c": c2[4]},
    }


def libro(server_ms: int) -> dict:
    r = http_get(f"{BASE}/api/v5/market/books?instId={INST}&sz=5")
    d = r["data"][0]                            # data es LISTA con un dict
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


def construir_parte(lado: str, book: dict, vel: dict, specs: dict, sep: Decimal) -> dict:
    v1, v2 = vel["c1"], vel["c2"]
    if lado == "LONG":
        entrada = book["ask"]
        sl = a_tick(min(Decimal(v1["l"]), Decimal(v2["l"])) - TICK * TICKS_BUFFER)
        riesgo = entrada - sl
        assert riesgo > 0, "riesgo no positivo en LONG"
        tp = a_tick(entrada + RR * riesgo)
    else:
        entrada = book["bid"]
        sl = a_tick(max(Decimal(v1["h"]), Decimal(v2["h"])) + TICK * TICKS_BUFFER)
        riesgo = sl - entrada
        assert riesgo > 0, "riesgo no positivo en SHORT"
        tp = a_tick(entrada - RR * riesgo)
    assert tp > 0, "TP no positivo"
    size_ct = 1                                  # L5: contratos enteros, minimo 1
    ct_val = Decimal(specs["ctVal"])
    margen = (entrada * ct_val * Decimal(size_ct) / Decimal(LEV)).quantize(Decimal("0.0001"))
    return {
        "id": f"{v2['ts']}-{lado}",
        "abierto_ts": ahora_iso(),
        "modo": "TEST",
        "inst": INST,
        "lado": lado,
        "velas": {"c1": v1, "c2": v2},
        "sep_pct": str((sep * 100).quantize(Decimal("0.0001"))),
        "entrada_ref": str(entrada),
        "sl": str(sl),
        "tp": str(tp),
        "riesgo": str(riesgo.quantize(Decimal("0.0001"))),
        "size_ct": size_ct,
        "lev": LEV,
        "margen_usd": str(margen),
        "estado": "ABIERTO",
        "resultado": None,
    }


# ---------------- liquidador (solo high/low; nunca senal) ----------------
def liquidar(partes: list, specs: dict, server_ms: int) -> list:
    r = http_get(f"{BASE}/api/v5/market/candles?instId={INST}&bar={BAR}&limit={LIMIT_LIQ}")
    chron = sorted([x for x in r["data"] if x[8] == "1"], key=lambda x: int(x[0]))
    ct_val = Decimal(specs["ctVal"])
    cerrados = []
    for p in partes:
        if p["estado"] != "ABIERTO":
            continue
        c2_ts = int(p["velas"]["c2"]["ts"])
        if chron and c2_ts < int(chron[0][0]):
            p.setdefault("notas", []).append(
                f"ventana de liquidacion incompleta (max {LIMIT_LIQ}h)")
        sl, tp = Decimal(p["sl"]), Decimal(p["tp"])
        for x in [y for y in chron if int(y[0]) > c2_ts]:
            low, high = Decimal(x[3]), Decimal(x[2])
            if p["lado"] == "LONG":
                hit_sl, hit_tp = low <= sl, high >= tp
            else:
                hit_sl, hit_tp = high >= sl, low <= tp
            if hit_sl:      # SL primero: criterio conservador
                salida = sl
                tipo = "SL_AMBIGUO" if hit_tp else "SL"
            elif hit_tp:
                salida, tipo = tp, "TP"
            else:
                continue
            diff = (salida - Decimal(p["entrada_ref"])) if p["lado"] == "LONG" \
                else (Decimal(p["entrada_ref"]) - salida)
            pnl = (diff * ct_val * Decimal(p["size_ct"])).quantize(Decimal("0.0001"))
            p["estado"] = f"CERRADO_{tipo}"
            p["resultado"] = {
                "tipo": tipo, "salida": str(salida), "pnl_usd_bruto": str(pnl),
                "comisiones": "0 (desconocidas en TEST)",
                "cerrado_con_vela_ts": int(x[0]), "cerrado_con_vela_iso": iso(x[0]),
            }
            cerrados.append(p["id"])
            break
    return cerrados


# ---------------- candado de modo ----------------
def place_order(*_a, **_k):
    raise NotImplementedError(
        "MODO TEST: sin ordenes. Puerta de certificacion cerrada "
        "(>=20-30 partes cerrados + revision FANAL).")


# ---------------- main ----------------
def main() -> int:
    bitacora("=" * 64)
    bitacora(f"EL APRENDIZ — sui_bot.py v{VERSION} — MODO TEST (sin ordenes)")
    bitacora(f"Ley de velas: MAXIMO {MAX_CERRADAS} completas anteriores | inst {INST}")
    bitacora(f"Peaje {SEP_MIN} | RR {RR} | SL extremo-{TICKS_BUFFER}t | lev {LEV}x (cap {LEV_CAP})")
    bitacora("PUERTA DE CERTIFICACION: CERRADA")
    bitacora("=" * 64)
    notas, guardias = [], {}
    try:
        server_ms = servidor()
        deriva_s = abs(datetime.now(timezone.utc).timestamp() * 1000 - server_ms) / 1000
        guardias["reloj"] = "OK" if deriva_s <= TOLERANCIA_RELOJ_S else f"DERIVA {deriva_s:.0f}s"
        if guardias["reloj"] != "OK":
            notas.append("L4: reloj descuadrado — no decide")
            registrar_decision({"ts_run": ahora_iso(), "server_ts_ms": server_ms,
                                "decision": "SKIP_RELOJ", "guardias": guardias, "notas": notas})
            bitacora(f"[SKIP] {notas}")
            return 0

        specs = instrumento()
        guardias["leyes_L1_L5_L6"] = "OK"
        vel = velas(server_ms)
        guardias["velas_L2_L3"] = "OK"
        book = libro(server_ms)
        guardias["libro_L4"] = "OK"
    except AssertionError as e:
        registrar_decision({"ts_run": ahora_iso(), "decision": "SKIP_GUARDIA",
                            "guardia": str(e)})
        bitacora(f"[SKIP] Guardia: {e}")
        return 0
    except Exception as e:  # noqa: BLE001
        registrar_decision({"ts_run": ahora_iso(), "decision": "ERROR", "error": str(e)})
        bitacora("[ERROR] fuente de datos:")
        traceback.print_exc()
        return 1

    lado, sep = senal(vel["c1"], vel["c2"])
    sensor = lado or "NEUTRO"
    bitacora(f"Velas (2 cerradas): c1={vel['c1']['c']} ({iso(vel['c1']['ts'])}) | "
             f"c2={vel['c2']['c']} ({iso(vel['c2']['ts'])})")
    bitacora(f"Sensor: {sensor} | separacion {(sep * 100).quantize(Decimal('0.0001'))} % "
             f"| peaje {SEP_MIN}")
    bitacora(f"Libro: bid {book['bid']} / ask {book['ask']} "
             f"(spread {book['spread'].quantize(Decimal('0.0001'))})")

    partes = cargar_partes()
    cerrados_hoy = liquidar(partes, specs, server_ms)
    if cerrados_hoy:
        bitacora(f"Liquidador: partes cerrados -> {', '.join(cerrados_hoy)}")
        for p in partes:
            if p["id"] in cerrados_hoy:
                bitacora(f"  {p['id']}: {p['resultado']['tipo']} salida {p['resultado']['salida']} "
                         f"pnl {p['resultado']['pnl_usd_bruto']} USD (bruto)")

    decision, parte_id = "SIN_SENAL", None
    if lado is None:
        decision = "SIN_SENAL"
    elif any(p["estado"] == "ABIERTO" for p in partes):
        decision = "PARTIDO_EN_MARCHA"
        notas.append("ya hay un parte ABIERTO — un solo barco por nave")
    elif any(p["id"] == f"{vel['c2']['ts']}-{lado}" for p in partes):
        decision = "DUPLICADO"
        notas.append("esta vela ya produjo un parte (idempotencia)")
    else:
        p = construir_parte(lado, book, vel, specs, sep)
        partes.append(p)
        parte_id = p["id"]
        decision = "PARTE_ABIERTO"
        bitacora(f"[PARTE DE PRUEBA] {p['id']}: {p['lado']} entrada_ref {p['entrada_ref']} "
                 f"SL {p['sl']} TP {p['tp']} | size {p['size_ct']} ct | lev {p['lev']}x "
                 f"| margen {p['margen_usd']} USD")

    guardar_partes(partes)
    registrar_decision({"ts_run": ahora_iso(), "server_ts_ms": server_ms,
                        "sensor": sensor,
                        "sep_pct": str((sep * 100).quantize(Decimal("0.0001"))),
                        "decision": decision, "parte_id": parte_id,
                        "guardias": guardias, "notas": notas})
    abiertos = sum(1 for p in partes if p["estado"] == "ABIERTO")
    total = len(partes)
    bitacora(f"Libro: {total} partes ({abiertos} abiertos, {total - abiertos} cerrados) "
             f"-> {PARTES_PATH}")
    place_order() if False else None  # candado presente; nunca se invoca en TEST
    bitacora("[FIN] modo TEST — ninguna orden enviada")
    return 0


if __name__ == "__main__":
    sys.exit(main())
