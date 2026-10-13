# grid-shadow.py rev.3 — LABORATORIO DE MALLAS · 5 configs paralelas · sombra
#   FIX rev.3 (acta #15): instrumento resuelto vía endpoint /instruments
#   (patrón probado de la casa) en vez de ID inventado. Diagnóstico de
#   respuesta cuando data viene vacía. Descarga: 300 velas recientes
#   (25h de 5m — sobra para cron horario). Fail-closed mantenido.

import json
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://www.okx.com"
UNDERLYING = "INJ"
BAR = "5m"
BAR_MS = 300000
FEE_RT = 0.0013
CONFIGS = [
    {"id": "A", "n_levels": 20, "step_pct": 0.005},
    {"id": "B", "n_levels": 20, "step_pct": 0.007},
    {"id": "C", "n_levels": 20, "step_pct": 0.010},
    {"id": "D", "n_levels": 15, "step_pct": 0.010},
    {"id": "E", "n_levels": 12, "step_pct": 0.010},
]
DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "grid-shadow.json")


def resolve_instrument():
    """Patrón de la casa: instruments → FUTURES no-USDT vencimiento lejano."""
    r = requests.get(
        f"{BASE_URL}/api/v5/public/instruments",
        params={"instType": "FUTURES", "uly": UNDERLYING + "-USD"},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    data = body.get("data") or []
    if not data:
        print("[ERROR] instruments respondió: code=" + str(body.get("code")) +
              " msg=" + str(body.get("msg")) + " — DETENIDO.")
        sys.exit(1)
    cands = [d for d in data
             if d.get("settleCcy", "").upper() != "USDT" and d.get("expTime")]
    if not cands:
        print("[ERROR] Sin FUTURES no-USDT para " + UNDERLYING + ". DETENIDO.")
        sys.exit(1)
    cands.sort(key=lambda d: int(d["expTime"]), reverse=True)
    inst = cands[0]
    print("[INSTRUMENT] " + inst["instId"] +
          " | ctVal=" + str(inst.get("ctVal")) + " " + str(inst.get("ctValCcy")) +
          " | minSz=" + str(inst.get("minSz")))
    return inst["instId"]


def fetch_candles(inst_id):
    """300 velas 5m más recientes (25h) — una llamada, sin paginación."""
    r = requests.get(
        f"{BASE_URL}/api/v5/market/history-candles",
        params={"instId": inst_id, "bar": BAR, "limit": "300"},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    rows = body.get("data") or []
    if not rows:
        print("[ERROR] API respondió vacía: code=" + str(body.get("code")) +
              " msg=" + str(body.get("msg")) + " — DETENIDO.")
        sys.exit(1)
    cols = ["ts", "o", "h", "l", "c", "vol", "vc", "vcq", "confirm"]
    df = pd.DataFrame(rows, columns=cols)
    for col in ["o", "h", "l", "c"]:
        df[col] = df[col].astype(float)
    df["ts"] = df["ts"].astype(np.int64)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    now_ms = int(time.time() * 1000)
    df = df[df["ts"] + BAR_MS <= now_ms].reset_index(drop=True)  # solo cerradas
    if len(df) < 20:
        print("[ERROR] Velas insuficientes (" + str(len(df)) + "). DETENIDO.")
        sys.exit(1)
    print("[CANDLES] " + BAR + ": " + str(len(df)) + " velas cerradas (~" +
          format(len(df) * BAR_MS / 3600000.0, '.1f') + " horas)")
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
    with open(os.path.join(DATA_DIR, "grid-" + cid + ".jsonl"), "w") as fh:
        for t in ledger:
            fh.write(json.dumps(t) + "\n")


def new_malla(cfg, anchor):
    step_abs = anchor * cfg["step_pct"]
    lines = {i: anchor + (cfg["n_levels"] - i) * step_abs
             for i in range(1, cfg["n_levels"] + 1)}
    return {"n": cfg["n_levels"], "step_abs": step_abs, "lines": lines,
            "positions": [], "closed_total": 0, "net_pct_sum": 0.0,
            "dead": False}


def process_malla(cid, cfg, malla, candles, ledger):
    events = []
    n = cfg["n_levels"]
    for _, row in candles.iterrows():
        h, l, c = row["h"], row["l"], row["c"]
        if malla["dead"]:
            break
        sl_line = malla["lines"][n] - malla["step_abs"]
        if l <= sl_line:
            loss = 0.0
            for lv in malla["positions"]:
                entry = malla["lines"][lv]
                loss += (sl_line - entry) / entry - FEE_RT
            ledger.append({"ts_out": int(row["ts"]), "event": "SL_RED",
                           "positions": len(malla["positions"]), "net_pct": loss})
            events.append("[SL RED] " + cid + " cerrada · pos " +
                          str(len(malla["positions"])) + " · net " +
                          format(loss * 100, '+.2f') + "%")
            malla["dead"] = True
            break
        for i in range(1, n + 1):
            lp = malla["lines"][i]
            if l <= lp and i not in malla["positions"] and c < lp:
                malla["positions"].append(i)
                events.append("[COMPRA] " + cid + " nivel " + str(i) +
                              " @ " + format(lp, '.5f'))
        sold = []
        for lv in malla["positions"]:
            if lv - 1 >= 1 and c >= malla["lines"][lv - 1]:
                entry = malla["lines"][lv]
                net = (malla["lines"][lv - 1] - entry) / entry - FEE_RT
                ledger.append({"ts_out": int(row["ts"]), "event": "COSECHA",
                               "level": lv, "net_pct": net})
                sold.append(lv)
                malla["net_pct_sum"] += net
                malla["closed_total"] += 1
        for lv in sold:
            malla["positions"].remove(lv)
        if c < malla["lines"][1]:
            shift = malla["step_abs"]
            malla["lines"] = {i: lp + shift for i, lp in malla["lines"].items()}
            events.append("[MIGRA] " + cid + " malla +1 · SL sigue 1 bajo el 20")
    return events


def report(state):
    print("")
    print("--- ESTADO DE LAS 5 MALLAS ---")
    for cfg in CONFIGS:
        m = state["mallas"][cfg["id"]]
        led = load_ledger(cfg["id"])
        cosechas = [t for t in led if t["event"] == "COSECHA"]
        wins = sum(1 for t in cosechas if t["net_pct"] > 0)
        ret = sum(t["net_pct"] for t in cosechas)
        status = ("MUERTA (SL red)" if m.get("dead")
                  else "posiciones " + str(len(m["positions"])))
        line = (cfg["id"] + " · " + str(cfg["n_levels"]) + "n · paso " +
                format(cfg["step_pct"] * 100, '.1f') + "% | " + status)
        if led:
            line += (" · cosechas " + str(len(cosechas)) + " (W" + str(wins) +
                     ") · net " + format(ret * 100, '+.2f') + "%")
        print("  " + line)


def main():
    print("=== GRID SHADOW LAB rev.3 · 5 mallas paralelas · sombra · fees " +
          format(FEE_RT * 100, '.2f') + "% r/t ===")
    inst_id = resolve_instrument()
    df = fetch_candles(inst_id)

    state = load_state()
    if state is None:
        anchor = float(df["c"].iloc[-1])
        st = {"last_ts": int(df["ts"].iloc[-1]), "anchor": anchor, "mallas": {}}
        for cfg in CONFIGS:
            st["mallas"][cfg["id"]] = new_malla(cfg, anchor)
        save_state(st)
        print("[INIT] 5 mallas creadas @ ancla " + format(anchor, '.5f') +
              " — cazando velas que cierren DE AHORA")
        report(st)
        return

    last_ts = int(state["last_ts"])
    new = df[df["ts"] > last_ts].reset_index(drop=True)
    if len(new) == 0:
        print("[IDLE] Sin velas nuevas desde el último ciclo.")
        report(state)
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
    report(state)


if __name__ == "__main__":
    main()
