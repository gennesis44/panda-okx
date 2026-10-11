# main-xrp.py v2 — XRP SHADOW SURFER · modo sombra (decreto: JAMAS en vivo)
#   DECRETO CARBONO (2026-10-11): nave preparada para surfear olas y
#   recaudar datos — CERO ordenes reales, CERO secrets, CERO riesgo.
#
#   QUE HACE:
#     - ejecuta la ley EMA3/21 4H (calidad certificada: WR 75/60 · PF 3.24/1.78)
#     - abre/cierra POSICIONES VIRTUALES con la geometria real del barco
#       (LONG SL6/TP7.5 · SHORT SL4.5/TP6 · 4 ct)
#     - acumula ledger virtual en data/xrp-shadow.jsonl (commit por ciclo)
#     - estado persistente en data/xrp-shadow.json (sobrevive entre runs)
#   QUE NO HACE:
#     - no autentica (sin secrets) · no consulta cuenta · no envia ordenes
#   Ax3: velas CERRADAS · fail-closed · un evento por vela · sin opinion
#   DESTINO DE LOS DATOS: certificacion plena (o entierro) de la ley cuando
#   la muestra virtual alcance tamano juicioso — decreto del Carbono.

import json
import os
import time

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://www.okx.com"
UNDERLYING = "XRP-USD"
BAR = "4H"
BAR_MS = 14400000
FETCH = 300
EMA_FAST, EMA_SLOW = 3, 21
CONTRACTS = 4
SL_L, TP_L = 0.060, 0.075
SL_S, TP_S = 0.045, 0.060
FEE_RT = 0.0013
DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "xrp-shadow.json")
LEDGER_FILE = os.path.join(DATA_DIR, "xrp-shadow.jsonl")


def get_instrument():
    r = requests.get(
        f"{BASE_URL}/api/v5/public/instruments",
        params={"instType": "FUTURES", "uly": UNDERLYING},
        timeout=15,
    )
    r.raise_for_status()
    cands = [d for d in r.json()["data"]
             if d.get("settleCcy", "").upper() != "USDT" and d.get("expTime")]
    if not cands:
        print("[ERROR] Sin FUTURES no-USDT. DETENIDO.")
        sys_exit()
    cands.sort(key=lambda d: int(d["expTime"]), reverse=True)
    inst = cands[0]
    print("[INSTRUMENT] " + inst["instId"] + " | ctVal=" +
          str(inst.get("ctVal")) + " " + str(inst.get("ctValCcy")))
    return inst


def sys_exit():
    raise SystemExit(1)


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
        sys_exit()
    cols = ["ts", "o", "h", "l", "c", "vol", "vc", "vcq", "confirm"]
    df = pd.DataFrame(all_rows, columns=cols)
    for col in ["o", "h", "l", "c"]:
        df[col] = df[col].astype(float)
    df["ts"] = df["ts"].astype(np.int64)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    now_ms = int(time.time() * 1000)
    df = df[df["ts"] + BAR_MS <= now_ms].reset_index(drop=True)
    if len(df) < 150:
        print("[ERROR] Velas insuficientes (" + str(len(df)) + "). DETENIDO.")
        sys_exit()
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
        json.dump(st, fh)


def load_ledger():
    try:
        with open(LEDGER_FILE) as fh:
            return [json.loads(x) for x in fh if x.strip()]
    except Exception:
        return []


def save_ledger(ledger):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LEDGER_FILE, "w") as fh:
        for t in ledger:
            fh.write(json.dumps(t) + "\n")


def report(pos, df, ledger):
    e_f = df["c"].ewm(span=EMA_FAST, adjust=False).mean().values
    e_s = df["c"].ewm(span=EMA_SLOW, adjust=False).mean().values
    px = df["c"].iloc[-1]
    print("--- ESTADO SHADOW ---")
    print("Precio: " + format(px, '.5f') + " | EMA" + str(EMA_FAST) + "/" +
          str(EMA_SLOW) + ": " + format(e_f[-1], '.5f') + "/" +
          format(e_s[-1], '.5f') + " | " +
          ("ALCISTA" if e_f[-1] > e_s[-1] else "BAJISTA") +
          " | sep " + format(abs(e_f[-1] - e_s[-1]) / e_s[-1] * 100, '.2f') + "%")
    if pos:
        print("Posicion VIRTUAL abierta: " + pos["side"] + " " +
              str(CONTRACTS) + " ct @ " + format(pos["entry"], '.5f') +
              " | SL " + format(pos["sl"], '.5f') + " · TP " +
              format(pos["tp"], '.5f'))
    else:
        print("Posicion VIRTUAL: ninguna — cazando cruce")
    if ledger:
        last = ledger[-5:]
        for t in last:
            print("  [" + time.strftime('%m-%d %H:%M', time.gmtime(t["ts_out"] / 1000)) +
                  "] " + t["side"] + " " + ("WIN" if t["net_pct"] > 0 else "LOSS") +
                  " " + format(t["net_pct"] * 100, '+.2f') + "%")
        wins = sum(1 for t in ledger if t["net_pct"] > 0)
        ret = sum(t["net_pct"] for t in ledger)
        print("LEDGER VIRTUAL: " + str(len(ledger)) + " trades · WR " +
              format(wins / len(ledger) * 100, '.0f') + "% · ret acumulado " +
              format(ret * 100, '+.2f') + "%")
    else:
        print("LEDGER VIRTUAL: vacio — recolectando desde hoy")


def main():
    print("=== XRP SHADOW SURFER v2 | ley EMA3/21 4H | ORDENES: NINGUNA ===")
    print("=== modo sombra permanente por decreto — solo recauda datos ===")
    inst = get_instrument()
    df = fetch_candles(inst["instId"])

    state = load_state()
    if state is None:
        last_ts = int(df["ts"].iloc[-1])
        save_state({"last_ts": last_ts, "pos": None})
        print("[INIT] Estado creado — cazando solo velas que cierren DE AHORA")
        print("[INIT] last_ts=" + str(last_ts))
        report(None, df, load_ledger())
        return

    last_ts = int(state["last_ts"])
    pos = state["pos"]
    ledger = load_ledger()
    new = df[df["ts"] > last_ts].reset_index(drop=True)
    if len(new) == 0:
        print("[IDLE] Sin velas nuevas desde el ultimo ciclo.")
        report(pos, df, ledger)
        return

    e_f = df["c"].ewm(span=EMA_FAST, adjust=False).mean().values
    e_s = df["c"].ewm(span=EMA_SLOW, adjust=False).mean().values
    idx_map = {int(ts): i for i, ts in enumerate(df["ts"].values)}

    for _, row in new.iterrows():
        i = idx_map[int(row["ts"])]
        if i < 1:
            last_ts = int(row["ts"])
            continue
        if pos:
            side, entry = pos["side"], pos["entry"]
            if side == "long":
                hit_sl = row["l"] <= pos["sl"]
                hit_tp = row["h"] >= pos["tp"]
            else:
                hit_sl = row["h"] >= pos["sl"]
                hit_tp = row["l"] <= pos["tp"]
            if hit_sl and hit_tp:
                hit_tp = False
            if hit_sl or hit_tp:
                exit_px = pos["sl"] if hit_sl else pos["tp"]
                g = (exit_px - entry) / entry
                if side == "short":
                    g = -g
                net = g - FEE_RT
                ledger.append({"ts_in": pos["ts_in"], "ts_out": int(row["ts"]),
                               "side": side, "entry": entry, "exit": exit_px,
                               "net_pct": net, "contracts": CONTRACTS})
                print("[SHADOW CLOSE] " + side + " " +
                      ("WIN" if net > 0 else "LOSS") + " " +
                      format(net * 100, '+.2f') + "%")
                pos = None
        if not pos:
            up = e_f[i - 1] <= e_s[i - 1] and e_f[i] > e_s[i]
            down = e_f[i - 1] >= e_s[i - 1] and e_f[i] < e_s[i]
            if up:
                pos = {"side": "long", "entry": float(row["c"]),
                       "ts_in": int(row["ts"]),
                       "sl": float(row["c"]) * (1 - SL_L),
                       "tp": float(row["c"]) * (1 + TP_L)}
                print("[SHADOW OPEN] LONG " + str(CONTRACTS) + " ct @ " +
                      format(pos["entry"], '.5f'))
            elif down:
                pos = {"side": "short", "entry": float(row["c"]),
                       "ts_in": int(row["ts"]),
                       "sl": float(row["c"]) * (1 + SL_S),
                       "tp": float(row["c"]) * (1 - TP_S)}
                print("[SHADOW OPEN] SHORT " + str(CONTRACTS) + " ct @ " +
                      format(pos["entry"], '.5f'))
        last_ts = int(row["ts"])

    save_state({"last_ts": last_ts, "pos": pos})
    save_ledger(ledger)
    report(pos, df, ledger)


if __name__ == "__main__":
    main()
