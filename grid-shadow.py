# grid-shadow.py — LABORATORIO DE MALLAS · 5 configs en paralelo, modo sombra
#   DECRETO CARBONO (2026-10-13): probar malla grid tendencial en papel —
#   JAMÁS en vivo hasta que el ledger certifique (mismo estándar que XRP).
#
#   EL ARTEFACTO (diseño del Carbono):
#     - 20 líneas horizontales numeradas de ARRIBA (1) a ABAJO (20)
#     - entrada LONG en un nivel medio (el precio llega)
#     - si el precio toca el nivel 1 (el más bajo visible):
#         la malla se desplaza +1 hacia arriba, nace nivel nuevo arriba,
#         el SL sube con ella — SIEMPRE 1 nivel por debajo del 20
#     - si el precio cae hasta el SL: CIERRA TODA LA MALLA (estática abajo,
#       sin re-tender, sin short — cierre y nueva decisión de Carbono)
#     - cosecha: cada nivel vendido al subir = +paso de ganancia
#
#   LAS 5 CONFIGS EN PARALELO (el mar decidirá cuál es universal):
#     A: 20 niveles · paso 0.5%   (densa)
#     B: 20 niveles · paso 0.7%   (media)
#     C: 20 niveles · paso 1.0%   (gruesa)
#     D: 15 niveles · paso 1.0%   (corta)
#     E: 12 niveles · paso 1.0%   (mínima)
#   FEES: 0.13% r/t POR EJECUCIÓN — el talón de Aquiles de toda grid.
#   CAPITAL: 100 por nivel (unidad virtual — el % es lo que cuenta).
#
#   Ax3: velas cerradas · estado persistente (data/grid-shadow.json)
#        ledgers por config (data/grid-<id>.jsonl) · fail-closed
#        · sin secrets · sin órdenes · sin opinión.

import json
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://www.okx.com"
INST_ID = "INJ-USD-SWAP"      # história profunda del subyacente (años)
BAR = "5m"
BAR_MS = 300000
FETCH = 300
FEE_RT = 0.0013
CAP_PER_LEVEL = 100.0
CONFIGS = [
    {"id": "A", "n_levels": 20, "step_pct": 0.005},
    {"id": "B", "n_levels": 20, "step_pct": 0.007},
    {"id": "C", "n_levels": 20, "step_pct": 0.010},
    {"id": "D", "n_levels": 15, "step_pct": 0.010},
    {"id": "E", "n_levels": 12, "step_pct": 0.010},
]
DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "grid-shadow.json")


def fetch_candles(inst_id):
    all_rows = []
    after = ""
    while len(all_rows) < FETCH:
        params = {"instId": inst_id, "bar": BAR, "limit": "300"}
        if after:
            params["after"] = after
        r = requests.get(
            f"{BASE_URL}/api/v5/market/history-candles",
            params=params, timeout=15,
        )
        r.raise_for_status()
        rows = r.json().get("data", [])
        if not rows:
            break
        all_rows.extend(rows)
        na = rows[-1][0]
        if after and na >= after:
            break
        after = na
        time.sleep(0.15)
    if not all_rows:
        print("[ERROR] Sin velas. DETENIDO.")
        sys.exit(1)
    cols = ["ts", "o", "h", "l", "c", "vol", "vc", "vcq", "confirm"]
    df = pd.DataFrame(all_rows, columns=cols)
    for col in ["o", "h", "l", "c"]:
        df[col] = df[col].astype(float)
    df["ts"] = df["ts"].astype(np.int64)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    now_ms = int(time.time() * 1000)
    df = df[df["ts"] + BAR_MS <= now_ms].reset_index(drop=True)  # solo cerradas
    if len(df) < 50:
        print("[ERROR] Velas insuficientes (" + str(len(df)) + "). DETENIDO.")
        sys.exit(1)
    print("[CANDLES] " + BAR + ": " + str(len(df)) + " velas cerradas")
    return df


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return None


def save_state(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(st, fh, indent=1)


def load_ledger(cid):
    path = os.path.join(DATA_DIR, "grid-" + cid + ".jsonl")
    try:
        with open(path) as fh:
            return [json.loads(x) for x in fh if x.strip()]
    except Exception:
        return []


def save_ledger(cid, ledger):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "grid-" + cid + ".jsonl")
    with open(path, "w") as fh:
        for t in ledger:
            fh.write(json.dumps(t) + "\n")


def new_malla(cfg, anchor_price):
    """Malla nueva: 20 líneas numeradas 1(arriba)..20(abajo).
    Nivel 20 = anchor - (n-20)*paso... ancla en el nivel 20.
    step = distancia entre líneas."""
    step_abs = anchor_price * cfg["step_pct"]
    # nivel 20 = ancla (precio actual); niveles 1..19 encima
    lines = {i: anchor_price + (20 - i) * step_abs for i in range(1, cfg["n_levels"] + 1)}
    return {
        "n": cfg["n_levels"], "step_abs": step_abs,
        "lines": lines, "positions": [],   # lista de niveles comprados
        "closed_total": 0, "net_pct_sum": 0.0,
        "dead": False,
    }


def level_of(price, malla):
    """Devuelve el nivel (entero) más cercano al precio según su línea base."""
    # líneas: nivel i tiene precio lines[i]; el precio "vive" entre líneas.
    best = None
    for i, lp in malla["lines"].items():
        d = abs(price - lp)
        if best is None or d < best[1]:
            best = (i, d)
    return best[0]


def process_malla(cid, cfg, malla, candles, ledger):
    """Procesa velas nuevas sobre la malla. Devuelve eventos impresos."""
    events = []
    n = cfg["n_levels"]
    for _, row in candles.iterrows():
        h, l, c = row["h"], row["l"], row["c"]
        if malla["dead"]:
            break
        # 1) SL de red: si la vela toca (o cruza) por debajo del nivel 20 → cierre total
        sl_line = malla["lines"][n] - malla["step_abs"]  # 1 nivel bajo el 20
        if l <= sl_line:
            loss = 0.0
            for lv in malla["positions"]:
                entry = malla["lines"][lv]
                g = (sl_line - entry) / entry
                loss += g - FEE_RT
            ledger.append({"ts_out": int(row["ts"]), "event": "SL_RED",
                           "positions": len(malla["positions"]), "net_pct": loss})
            events.append("[SL RED] malla " + cid + " cerrada · posiciones " +
                          str(len(malla["positions"])) + " · net " +
                          format(loss * 100, '+.2f') + "%")
            malla["dead"] = True
            break

        # 2) Compras: cada nivel por debajo del precio que no tenga posición → comprar
        for i in range(1, n + 1):
            lp = malla["lines"][i]
            if l <= lp and i not in malla["positions"] and c < lp:
                malla["positions"].append(i)
                events.append("[COMPRA] malla " + cid + " nivel " + str(i) +
                              " @ " + format(lp, '.5f'))

        # 3) Ventas (cosecha): posición en nivel i se vende si el precio
        #    cierra por encima del nivel i-1 (el superior)
        sold = []
        for lv in malla["positions"]:
            if lv - 1 >= 1 and c >= malla["lines"][lv - 1]:
                entry = malla["lines"][lv]
                g = (malla["lines"][lv - 1] - entry) / entry
                net = g - FEE_RT
                ledger.append({"ts_out": int(row["ts"]), "event": "COSECHA",
                               "level": lv, "net_pct": net})
                sold.append(lv)
                malla["net_pct_sum"] += net
                malla["closed_total"] += 1
        for lv in sold:
            malla["positions"].remove(lv)

        # 4) Migración: si el precio cierra por DEBAJO del nivel 1 (el más bajo
        #    visible) → la malla se desplaza +1 (nace nivel nuevo arriba,
        #    SL sube 1: sigue siendo 1 nivel bajo el 20)
        if c < malla["lines"][1]:
            shift = malla["step_abs"]
            malla["lines"] = {i: lp + shift for i, lp in malla["lines"].items()}
            # renumerar: el viejo 21 virtual no existe — las posiciones
            # mantienen su nivel de precio, no su número
            events.append("[MIGRA] malla " + cid + " desplazada +1 · SL sigue 1 bajo el 20")
    return events


def main():
    print("=== GRID SHADOW LABORATORIO · 5 mallas en paralelo · modo sombra ===")
    print("=== fe: 0.13% r/t · capital 100/nivel · SL de red = 1 nivel bajo el 20 ===")
    df = fetch_candles(INST_ID)

    state = load_state()
    if state is None:
        anchor = float(df["c"].iloc[-1])
        st = {"last_ts": int(df["ts"].iloc[-1]), "anchor": anchor,
              "mallas": {}}
        for cfg in CONFIGS:
            st["mallas"][cfg["id"]] = new_malla(cfg, anchor)
            st["mallas"][cfg["id"]]["anchor"] = anchor
        save_state(st)
        print("[INIT] 5 mallas creadas @ ancla " + format(anchor, '.5f') +
              " — cazando solo velas que cierren DE AHORA")
        return

    last_ts = int(state["last_ts"])
    new = df[df["ts"] > last_ts].reset_index(drop=True)
    if len(new) == 0:
        print("[IDLE] Sin velas nuevas.")
        report(state, load_ledger)
        return

    print("[PROCESO] " + str(len(new)) + " velas nuevas × 5 mallas")
    for cfg in CONFIGS:
        malla = state["mallas"][cfg["id"]]
        if malla.get("dead"):
            continue
        ledger = load_ledger(cfg["id"])
        events = process_malla(cfg["id"], cfg, malla, new, ledger)
        save_ledger(cfg["id"], ledger)
        for ev in events:
            print("  " + ev)

    state["last_ts"] = int(new["ts"].iloc[-1])
    save_state(state)
    report(state, load_ledger)


def report(state, load_ledger):
    print("")
    print("--- ESTADO DE LAS 5 MALLAS ---")
    for cfg in CONFIGS:
        m = state["mallas"][cfg["id"]]
        led = load_ledger(cfg["id"])
        cosechas = [t for t in led if t["event"] == "COSECHA"]
        wins = sum(1 for t in cosechas if t["net_pct"] > 0)
        ret = sum(t["net_pct"] for t in cosechas)
        status = "MUERTA (SL red)" if m.get("dead") else \
            ("posiciones: " + str(len(m["positions"])) if m["positions"] else "cazando")
        line = (cfg["id"] + " · " + str(cfg["n_levels"]) + "n · paso " +
                format(cfg["step_pct"] * 100, '.1f') + "% | " + status)
        if led:
            line += (" · cosechas " + str(len(cosechas)) + " (W" + str(wins) +
                     ") · net " + format(ret * 100, '+.2f') + "%")
        print("  " + line)


if __name__ == "__main__":
    main()
