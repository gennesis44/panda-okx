# backtest-hbar2.py — HBAR VALIDADOR v2 · stack A [3/4/10/21/27] 30m · grid de TP
#   PREGUNTA DEL CARBONO (pendiente desde 7-oct): live usa SL2/TP5.5,
#   el comparador sugirio TP4.5 — resolver con metodo de la casa (IS/OOS).
#
#   FIEL AL BOT (bot_hbar.py v3):
#     - senal: formacion COMPLETA del stack en ambas direcciones
#       (3>4>10>21>27 alcista · 3<4<10<21<27 bajista) en velas 30m CERRADAS
#     - flip por senal: stack opuesto completo = cierre + apertura contraria
#     - entrada a cierre de la vela donde el stack se completa
#   GRID: SL fijo 2% (decreto) x TP [4.0/4.5/5.0/5.5/6.0]%
#   Ax3: IS/OOS 70/30 · fees 0.13% r/t · fail-closed descarga · sin secrets

import sys
import time

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://www.okx.com"
UNDERLYING = "HBAR-USD"
FEE_RT = 0.0013
BAR = "30m"
BAR_MS = 1800000
MIN_CANDLES = 2600
STACK = [3, 4, 10, 21, 27]
SL = 0.020
TPS = [0.040, 0.045, 0.050, 0.055, 0.060]
SPLIT_IS = 0.70


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


def fetch_candles(inst_id, min_count):
    rows_all = []
    after = ""
    while len(rows_all) < min_count:
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
        rows_all.extend(rows)
        na = rows[-1][0]
        if after and na >= after:
            break
        after = na
        time.sleep(0.15)
    if not rows_all:
        print("[ERROR] Sin velas. DETENIDO.")
        sys.exit(1)
    cols = ["ts", "o", "h", "l", "c", "vol", "vc", "vcq", "confirm"]
    df = pd.DataFrame(rows_all, columns=cols)
    for col in ["o", "h", "l", "c"]:
        df[col] = df[col].astype(float)
    df["ts"] = df["ts"].astype(np.int64)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    if "confirm" in df.columns:
        df = df[df["confirm"] == "1"].reset_index(drop=True)
    if len(df) < min_count * 0.8:
        print("[ERROR] Descarga incompleta " + str(len(df)) + "/" +
              str(min_count) + ". DETENIDO.")
        sys.exit(1)
    print("[CANDLES] " + BAR + ": " + str(len(df)) + " velas (~" +
          format(len(df) * BAR_MS / 86400000.0, '.0f') + " dias)")
    return df


def stacks(df):
    close = df["c"]
    e = [close.ewm(span=n, adjust=False).mean() for n in STACK]
    bull = ((e[0] > e[1]) & (e[1] > e[2]) & (e[2] > e[3]) & (e[3] > e[4])).values
    bear = ((e[0] < e[1]) & (e[1] < e[2]) & (e[2] < e[3]) & (e[3] < e[4])).values
    return bull, bear


def simulate(df, tp):
    close = df["c"].values
    high = df["h"].values
    low = df["l"].values
    ts = df["ts"].values
    bull, bear = stacks(df)
    warmup = max(STACK[4] * 3, 100)
    n = len(df)
    trades = []
    pos = None
    for i in range(warmup, n):
        if pos is None:
            if bull[i] and not bull[i - 1]:
                pos = {"side": "long", "entry": close[i], "i": i}
            elif bear[i] and not bear[i - 1]:
                pos = {"side": "short", "entry": close[i], "i": i}
            continue
        side, entry = pos["side"], pos["entry"]
        if side == "long":
            sl_px, tp_px = entry * (1 - SL), entry * (1 + tp)
            hit_sl, hit_tp = low[i] <= sl_px, high[i] >= tp_px
        else:
            sl_px, tp_px = entry * (1 + SL), entry * (1 - tp)
            hit_sl, hit_tp = high[i] >= sl_px, low[i] <= tp_px
        if hit_sl and hit_tp:
            hit_tp = False
        exit_px = None
        if hit_sl:
            exit_px = sl_px
        elif hit_tp:
            exit_px = tp_px
        elif (side == "long" and bear[i] and not bear[i - 1]) or \
             (side == "short" and bull[i] and not bull[i - 1]):
            exit_px = close[i]          # flip por stack opuesto
        if exit_px is not None:
            g = (exit_px - entry) / entry
            if side == "short":
                g = -g
            trades.append({"ts_in": int(ts[pos["i"]]), "net": g - FEE_RT})
            pos = None
            if exit_px == close[i] and not hit_sl and not hit_tp:
                if side == "long" and bear[i]:
                    pos = {"side": "short", "entry": close[i], "i": i}
                elif side == "short" and bull[i]:
                    pos = {"side": "long", "entry": close[i], "i": i}
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
    print("HBAR VALIDADOR v2 — stack [3/4/10/21/27] 30m · SL2 fijo · grid TP " +
          str([round(x * 100, 1) for x in TPS]) + " · flip fiel al bot · " +
          "IS/OOS 70/30 · fees " + format(FEE_RT * 100, '.2f') + "%")
    inst = get_instrument()
    df = fetch_candles(inst["instId"], MIN_CANDLES)
    b = int(len(df) * SPLIT_IS)
    is_df = df.iloc[:b].reset_index(drop=True)
    oos_df = df.iloc[max(0, b - 60):].reset_index(drop=True)
    b_ts = int(df["ts"].iloc[b])

    print("")
    print("=== GRID TP (SL2 fijo) | IS | OOS ===")
    results = []
    for tp in TPS:
        t_is = [t["net"] for t in simulate(is_df, tp)]
        t_oos = [t["net"] for t in simulate(oos_df, tp)
                 if t["ts_in"] >= b_ts]
        s_is, s_oos = stats(t_is), stats(t_oos)
        results.append((tp, s_is, s_oos))
        print("TP " + format(tp * 100, '.1f') + "% | IS  " + fmt(s_is) +
              " | OOS " + fmt(s_oos))

    ok = [r for r in results if r[1] and r[2]
          and r[1]["t"] >= 15 and r[1]["wr"] > r[1]["be"]
          and r[1]["pf"] > 1.5 and r[1]["exp"] > 0
          and r[2]["t"] >= 5 and r[2]["pf"] > 1.0 and r[2]["exp"] > 0]

    print("")
    print("=== RECOMENDACION ===")
    if ok:
        best = max(ok, key=lambda r: r[1]["exp"])
        print("TP ROBUSTO: " + format(best[0] * 100, '.1f') + "% (SL2)")
        print("  IS : " + fmt(best[1]))
        print("  OOS: " + fmt(best[2]))
        if abs(best[0] - 0.055) < 1e-9:
            print("  El TP live (5.5) queda REVALIDADO. Sin cambios.")
        else:
            print("  El live usa 5.5 — si el Carbono ratifica " +
                  format(best[0] * 100, '.1f') + ", es enmienda de bot_hbar.py.")
    else:
        print("NINGUN TP cumple IS+OOS. El live (5.5) queda por defecto —")
        print("el regimen de rango actual no deja certificar mejoras.")
    print("")
    print("ADVERTENCIA: pasado no es futuro. El Carbono decide.")


if __name__ == "__main__":
    main()
