# backtest-doge2.py — DOGE VALIDADOR v2 · candidata 4/40·3m + revalidacion ley viva
#   DECRETO CARBONO (2026-10-10): el perro sin separacion en 3m/5m —
#   ¿existe config que cace en este regimen?
#
#   GRID: 2m/3m/5m x EMA(3,21)[viva] + EMA(4,40)[candidata] x candado 0/0.10/0.15/0.25
#   GEOMETRIA (decreto del perro): LONG SL2/TP4 · SHORT SL3/TP4
#   FIEL AL BOT: sin flip (solo SL/TP) + cooldown 60m tras perdida
#   Ax3: IS/OOS 70/30 anti-mineria · fees 0.13% r/t · velas cerradas
#        fail-closed en descarga · sin secrets (API publica) · sin opinion

import sys
import time
from itertools import product

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://www.okx.com"
UNDERLYING = "DOGE-USD"
FEE_RT = 0.0013
TIMEFRAMES = ["2m", "3m", "5m"]
BAR_MS = {"2m": 120000, "3m": 180000, "5m": 300000}
MIN_CANDLES = {"2m": 48000, "3m": 32000, "5m": 19200}
EMA_PAIRS = [(3, 21), (4, 40)]
LOCKS = [0.0, 0.0010, 0.0015, 0.0025]
SL_L, TP_L = 0.020, 0.040
SL_S, TP_S = 0.030, 0.040
COOLDOWN_MS = 60 * 60 * 1000
SPLIT_IS = 0.70
WARM_PAD = 120


def get_farthest_future_instrument(uly=UNDERLYING):
    r = requests.get(
        f"{BASE_URL}/api/v5/public/instruments",
        params={"instType": "FUTURES", "uly": uly},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()["data"]
    candidates = [
        d for d in data
        if d.get("settleCcy", "").upper() != "USDT" and d.get("expTime")
    ]
    if not candidates:
        print("[ERROR] No hay FUTURES no-USDT para " + uly + ". DETENIDO.")
        sys.exit(1)
    candidates.sort(key=lambda d: int(d["expTime"]), reverse=True)
    inst = candidates[0]
    print("[INSTRUMENT] instId=" + inst["instId"] +
          " | ctVal=" + str(inst.get("ctVal")) + " " + str(inst.get("ctValCcy")) +
          " | settle=" + str(inst.get("settleCcy")) +
          " | minSz=" + str(inst.get("minSz")) +
          " | lotSz=" + str(inst.get("lotSz")) +
          " | tickSz=" + str(inst.get("tickSz")))
    return inst


def fetch_history_candles(inst_id, bar, min_count):
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
        new_after = rows[-1][0]
        if after and new_after >= after:
            break
        after = new_after
        time.sleep(0.15)
    if not all_rows:
        print("[ERROR] Sin velas para " + inst_id + " " + bar + ". DETENIDO.")
        sys.exit(1)
    cols = ["ts", "o", "h", "l", "c", "vol", "volCcy", "volCcyQuote", "confirm"]
    df = pd.DataFrame(all_rows, columns=cols)
    for col in ["o", "h", "l", "c"]:
        df[col] = df[col].astype(float)
    df["ts"] = df["ts"].astype(np.int64)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    if "confirm" in df.columns:
        df = df[df["confirm"] == "1"].reset_index(drop=True)
    if len(df) < min_count * 0.8:
        print("[ERROR] Descarga incompleta: " + str(len(df)) + "/" +
              str(min_count) + " (" + bar + "). DETENIDO (fail-closed).")
        sys.exit(1)
    days = len(df) * BAR_MS[bar] / 86400000.0
    print("[CANDLES] " + bar + ": " + str(len(df)) + " velas cerradas (~" +
          format(days, '.0f') + " dias)")
    return df


def simulate(df, ef, es, lock):
    close = df["c"].values
    high = df["h"].values
    low = df["l"].values
    ts = df["ts"].values
    e_f = df["c"].ewm(span=ef, adjust=False).mean().values
    e_s = df["c"].ewm(span=es, adjust=False).mean().values
    warmup = max(3 * es, 100)
    n = len(df)
    trades = []
    in_pos = False
    side = None
    entry = sl_px = tp_px = 0.0
    entry_i = 0
    last_loss_ts = 0
    for i in range(warmup, n):
        if in_pos:
            if side == "long":
                hit_sl = low[i] <= sl_px
                hit_tp = high[i] >= tp_px
            else:
                hit_sl = high[i] >= sl_px
                hit_tp = low[i] <= tp_px
            if hit_sl and hit_tp:
                hit_tp = False          # ambigua: SL primero (conservador)
            if hit_sl:
                exit_price = sl_px
            elif hit_tp:
                exit_price = tp_px
            else:
                continue
            gross = (exit_price - entry) / entry
            if side == "short":
                gross = -gross
            net = gross - FEE_RT
            trades.append({"ts_in": int(ts[entry_i]), "ts_out": int(ts[i]),
                           "side": side, "net": net, "lock": lock})
            if net < 0:
                last_loss_ts = int(ts[i])
            in_pos = False
            continue
        if int(ts[i]) - last_loss_ts < COOLDOWN_MS:
            continue
        sep = abs(e_f[i] - e_s[i]) / e_s[i] if e_s[i] else 0.0
        up = (e_f[i - 1] <= e_s[i - 1]) and (e_f[i] > e_s[i])
        down = (e_f[i - 1] >= e_s[i - 1]) and (e_f[i] < e_s[i])
        if (up or down) and sep >= lock:
            side = "long" if up else "short"
            entry = close[i]
            if side == "long":
                sl_px = entry * (1 - SL_L)
                tp_px = entry * (1 + TP_L)
            else:
                sl_px = entry * (1 + SL_S)
                tp_px = entry * (1 - TP_S)
            entry_i = i
            in_pos = True
    return trades


def stats(nets):
    n = len(nets)
    if n == 0:
        return None
    arr = np.array(nets)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    wr = len(wins) / n
    aw = wins.mean() if len(wins) else 0.0
    al = abs(losses.mean()) if len(losses) else 0.0
    be = al / (aw + al) if (aw + al) > 0 else 1.0
    gw = wins.sum()
    gl = abs(losses.sum())
    pf = gw / gl if gl > 0 else float("inf")
    eq = np.cumsum(arr)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak).min() if len(eq) else 0.0
    return {"trades": n, "wr": wr, "be": be, "pf": pf,
            "exp": arr.mean(), "dd": dd, "ret": eq[-1]}


def fmt(s):
    if s is None or s["trades"] == 0:
        return "t=0"
    return ("t=" + str(s["trades"]) +
            " WR=" + format(s["wr"] * 100, '.1f') + "%" +
            " BE=" + format(s["be"] * 100, '.1f') + "%" +
            " PF=" + format(s["pf"], '.2f') +
            " exp=" + format(s["exp"] * 100, '+.3f') + "%" +
            " DD=" + format(s["dd"] * 100, '.1f') + "%")


def main():
    print("DOGE VALIDADOR v2 — grid 2m/3m/5m x (3,21)(4,40) x candado | " +
          "geometria L2/4 S3/4 | sin flip + cooldown 60m | fees " +
          format(FEE_RT * 100, '.2f') + "% r/t | IS/OOS 70/30")
    inst = get_farthest_future_instrument()

    splits = {}
    for bar in TIMEFRAMES:
        df = fetch_history_candles(inst["instId"], bar, MIN_CANDLES[bar])
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
    print("=== TABLA (IS | OOS) — ordenada por expectativa IS ===")
    for r in results:
        print("[" + r["tf"] + "] EMA " + r["ema"] + " candado " + r["lock"] +
              " | IS  " + fmt(r["is"]) +
              " | OOS " + fmt(r["oos"]))

    robust = [r for r in results if r["is"] and r["oos"]
              and r["is"]["trades"] >= 15
              and r["is"]["wr"] > r["is"]["be"]
              and r["is"]["pf"] > 1.5
              and r["is"]["exp"] > 0
              and r["oos"]["trades"] >= 5
              and r["oos"]["pf"] > 1.0
              and r["oos"]["exp"] > 0]

    print("")
    print("=== RECOMENDACION (aprueba IS y verifica OOS) ===")
    if robust:
        best = max(robust, key=lambda r: r["is"]["exp"])
        print("GEOMETRIA ROBUSTA: TF=" + best["tf"] + " EMA=" + best["ema"] +
              " candado=" + best["lock"])
        print("  IS : " + fmt(best["is"]))
        print("  OOS: " + fmt(best["oos"]))
        print("  Si coincide con la ley viva: revalidada. Si es 4/40: candidata")
        print("  para el Carbono — requeriria actualizar main-doge.py (decreto).")
    else:
        print("NINGUNA config aprueba IS y verifica OOS.")
        if results and results[0]["is"]:
            top = results[0]
            print("  Mejor IS (NO validada): TF=" + top["tf"] +
                  " EMA=" + top["ema"] + " candado=" + top["lock"])
        print("  Lectura probable: regimen de sierra — el perro vigila y punto.")

    print("")
    print("ADVERTENCIA: pasado no es futuro. El Carbono decide.")


if __name__ == "__main__":
    main()
