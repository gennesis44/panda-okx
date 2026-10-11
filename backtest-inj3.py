# backtest-inj3.py — INJ CUCHILLO PROFUNDO · buscar ley superviviente
#   DECRETO CARBONO (2026-10-11): "terminar de acuchillar INJ hasta que
#   quede corregido" — el grid busca si existe alguna geometria que
#   apruebe doble puerta en el regimen actual.
#
#   GRID (estructurado, anti-mineria):
#     - TF: 1H (marco del bot) · 4H (menos ruido, hipotesis de regimen)
#     - EMAs: (3,21)[viva] · (8,34)[probada en AVAX] · (4,40)[candidata DOGE]
#     - Candado: 0.00 / 0.10 / 0.15 / 0.25 %
#     - Salida: cruce-contrario (fiel al bot) — SL/TP NO incluidos:
#       son otra enmienda, este cuchillo juzga LA FAMILIA de ley
#   Ax3: IS/OOS 70/30 · fees 0.13% r/t · fail-closed · sin secrets
#   NOTA: 4H con 1500 velas = 250 dias — la descarga puede traer menos
#   (limitacion del endpoint history-candles); el fail-closed lo vigilara.

import sys
import time
from itertools import product

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://www.okx.com"
UNDERLYING = "INJ-USD"
FEE_RT = 0.0013
TIMEFRAMES = ["1H", "4H"]
BAR_MS = {"1H": 3600000, "4H": 14400000}
MIN_CANDLES = {"1H": 1500, "4H": 700}
EMA_PAIRS = [(3, 21), (8, 34), (4, 40)]
LOCKS = [0.0, 0.0010, 0.0015, 0.0025]
SPLIT_IS = 0.70
WARM_PAD = 60


def get_instrument():
    r = requests.get(
        f"{BASE_URL}/api/v5/public/instruments",
        params={"instType": "FUTURES", "uly": UNDERLYING},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()["data"]
    cands = [d for d in data
             if d.get("settleCcy", "").upper() != "USDT" and d.get("expTime")]
    if not cands:
        print("[ERROR] Sin FUTURES no-USDT. DETENIDO.")
        sys.exit(1)
    cands.sort(key=lambda d: int(d["expTime"]), reverse=True)
    inst = cands[0]
    print("[INSTRUMENT] " + inst["instId"] +
          " | ctVal=" + str(inst.get("ctVal")) + " " + str(inst.get("ctValCcy")) +
          " | minSz=" + str(inst.get("minSz")) +
          " | lotSz=" + str(inst.get("lotSz")) +
          " | tickSz=" + str(inst.get("tickSz")))
    return inst


def fetch_candles(inst_id, bar, min_count):
    all_rows = []
    after = ""
    while len(all_rows) < min_count:
        params = {"instId": inst_id, "bar": bar, "limit": "300"}
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
        print("[ERROR] Sin velas para " + inst_id + " " + bar + ". DETENIDO.")
        sys.exit(1)
    cols = ["ts", "o", "h", "l", "c", "vol", "vc", "vcq", "confirm"]
    df = pd.DataFrame(all_rows, columns=cols)
    for col in ["o", "h", "l", "c"]:
        df[col] = df[col].astype(float)
    df["ts"] = df["ts"].astype(np.int64)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    if "confirm" in df.columns:
        df = df[df["confirm"] == "1"].reset_index(drop=True)
    if len(df) < min_count * 0.8:
        print("[WARN] " + bar + ": descarga incompleta " + str(len(df)) +
              "/" + str(min_count) + " — se continua con lo disponible.")
    if len(df) < 300:
        print("[ERROR] Velas insuficientes (" + str(len(df)) + "). DETENIDO.")
        sys.exit(1)
    print("[CANDLES] " + bar + ": " + str(len(df)) + " velas cerradas (~" +
          format(len(df) * BAR_MS[bar] / 86400000.0, '.0f') + " dias)")
    return df


def simulate(df, ef, es, lock):
    close = df["c"].values
    ts = df["ts"].values
    e_f = df["c"].ewm(span=ef, adjust=False).mean().values
    e_s = df["c"].ewm(span=es, adjust=False).mean().values
    warmup = max(3 * es, 100)
    n = len(df)
    trades = []
    pos = None
    for i in range(warmup, n):
        if pos is None:
            sep = abs(e_f[i] - e_s[i]) / e_s[i] if e_s[i] else 0.0
            up = (e_f[i - 1] <= e_s[i - 1]) and (e_f[i] > e_s[i])
            down = (e_f[i - 1] >= e_s[i - 1]) and (e_f[i] < e_s[i])
            if (up or down) and sep >= lock:
                pos = {"side": "long" if up else "short",
                       "entry": close[i], "i": i}
            continue
        side, entry = pos["side"], pos["entry"]
        opp = (side == "long" and e_f[i] < e_s[i]) or \
              (side == "short" and e_f[i] > e_s[i])
        if opp:
            g = (close[i] - entry) / entry
            if side == "short":
                g = -g
            trades.append({"ts_in": int(ts[pos["i"]]), "net": g - FEE_RT})
            pos = None
    return trades


def stats(nets):
    n = len(nets)
    if n == 0:
        return None
    a = np.array(nets)
    w, l = a[a > 0], a[a <= 0]
    wr = len(w) / n
    aw = w.mean() if len(w) else 0.0
    al = abs(l.mean()) if len(l) else 0.0
    be = al / (aw + al) if (aw + al) > 0 else 1.0
    pf = w.sum() / abs(l.sum()) if l.sum() != 0 else float("inf")
    eq = np.cumsum(a)
    dd = (eq - np.maximum.accumulate(eq)).min() if len(eq) else 0.0
    return {"t": n, "wr": wr, "be": be, "pf": pf,
            "exp": a.mean(), "dd": dd, "ret": eq[-1]}


def fmt(s):
    if s is None or s["t"] == 0:
        return "t=0"
    return ("t=" + str(s["t"]) +
            " WR=" + format(s["wr"] * 100, '.1f') + "%" +
            " BE=" + format(s["be"] * 100, '.1f') + "%" +
            " PF=" + format(s["pf"], '.2f') +
            " exp=" + format(s["exp"] * 100, '+.3f') + "%" +
            " DD=" + format(s["dd"] * 100, '.1f') + "%")


def main():
    print("INJ CUCHILLO PROFUNDO — grid 1H/4H x 3 pares EMA x 4 candados | " +
          "salida cruce-contrario | fees " + format(FEE_RT * 100, '.2f') +
          "% r/t | IS/OOS 70/30 | USDT prohibido")
    inst = get_instrument()

    splits = {}
    for bar in TIMEFRAMES:
        df = fetch_candles(inst["instId"], bar, MIN_CANDLES[bar])
        b = int(len(df) * SPLIT_IS)
        splits[bar] = {
            "is": df.iloc[:b].reset_index(drop=True),
            "oos": df.iloc[max(0, b - WARM_PAD):].reset_index(drop=True),
            "b_ts": int(df["ts"].iloc[b]),
        }

    results = []
    for bar, (ef, es), lock in product(TIMEFRAMES, EMA_PAIRS, LOCKS):
        sp = splits[bar]
        tr_is = simulate(sp["is"], ef, es, lock)
        tr_oos = [t for t in simulate(sp["oos"], ef, es, lock)
                  if t["ts_in"] >= sp["b_ts"]]
        results.append({
            "tf": bar, "ema": str(ef) + "/" + str(es),
            "lock": format(lock * 100, '.2f') + "%",
            "is": stats([t["net"] for t in tr_is]),
            "oos": stats([t["net"] for t in tr_oos]),
        })

    results.sort(key=lambda r: (r["is"]["exp"] if r["is"] else -9),
                 reverse=True)

    print("")
    print("=== TABLA (IS | OOS) — 24 configs ===")
    for r in results:
        print("[" + r["tf"] + "] EMA " + r["ema"] + " candado " + r["lock"] +
              " | IS  " + fmt(r["is"]) +
              " | OOS " + fmt(r["oos"]))

    robust = [r for r in results if r["is"] and r["oos"]
              and r["is"]["t"] >= 15 and r["is"]["wr"] > r["is"]["be"]
              and r["is"]["pf"] > 1.5 and r["is"]["exp"] > 0
              and r["oos"]["t"] >= 5 and r["oos"]["pf"] > 1.0
              and r["oos"]["exp"] > 0]

    print("")
    print("=== RECOMENDACION (doble puerta) ===")
    if robust:
        best = max(robust, key=lambda r: r["is"]["exp"])
        print("LEY SUPERVIVIENTE: TF=" + best["tf"] + " EMA=" + best["ema"] +
              " candado=" + best["lock"])
        print("  IS : " + fmt(best["is"]))
        print("  OOS: " + fmt(best["oos"]))
        print("  Ruta: enmienda de main-inj.py (decreto Carbono) + push + run test")
    else:
        print("NINGUNA geometria sobrevive el regimen actual en INJ.")
        print("  Conclusion: INJ queda en amarre con acta — sin ley que cace.")
        print("  El mar XPERP INJ sigue siendo trituradora (25% rango/34h).")

    print("")
    print("ADVERTENCIA: pasado no es futuro. El Carbono decide.")


if __name__ == "__main__":
    main()
