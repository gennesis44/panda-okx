# backtest-xrp2.py — XRP VALIDADOR v2 · LEY VIVA EMA3/21 4H bajo el cuchillo
#   DECRETO CARBONO (2026-10-11): certificar la ley del barco anclado.
#   Dato previo: 0% WR en la Tabla GSCSI (2 trades) — la ley nunca fue
#   validada. Este cuchillo decide: habia ley o habia supersticion.
#
#   FIEL AL BOT (main-xrp.py observado en logs):
#     - ley: EMA3/21 · 4H · sin candado (extirpado)
#     - LONG SL6/TP7.5 · SHORT SL4.5/TP6 (asimetricas)
#     - 4 ct (~$5.5 nocional) · velas 4H CERRADAS
#     - salida: SL/TP atomicos por direccion
#   Ax3: IS/OOS 70/30 · fees 0.13% r/t · XPERP venc. mas lejano
#        · USDT PROHIBIDO · fail-closed · sin secrets
#
#   MODO DESTINO (decreto): pase o no pase, XRP va a patrulla TEST_MODE
#   (recaudador de datos — jamas en vivo hasta decreto expreso).

import sys
import time

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://www.okx.com"
UNDERLYING = "XRP-USD"
FEE_RT = 0.0013
BAR = "4H"
BAR_MS = 14400000
MIN_CANDLES = 700
EMA_FAST, EMA_SLOW = 3, 21
SL_L, TP_L = 0.060, 0.075
SL_S, TP_S = 0.045, 0.060
SPLIT_IS = 0.70
WARM_PAD = 25


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
        print("[ERROR] Sin FUTURES no-USDT para " + UNDERLYING + ". DETENIDO.")
        sys.exit(1)
    cands.sort(key=lambda d: int(d["expTime"]), reverse=True)
    inst = cands[0]
    print("[INSTRUMENT] " + inst["instId"] +
          " | ctVal=" + str(inst.get("ctVal")) + " " + str(inst.get("ctValCcy")) +
          " | settle=" + str(inst.get("settleCcy")) +
          " | minSz=" + str(inst.get("minSz")) +
          " | lotSz=" + str(inst.get("lotSz")) +
          " | tickSz=" + str(inst.get("tickSz")))
    return inst


def fetch_candles(inst_id, min_count):
    all_rows = []
    after = ""
    while len(all_rows) < min_count:
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
        print("[ERROR] Sin velas para " + inst_id + ". DETENIDO.")
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
        print("[ERROR] Descarga incompleta: " + str(len(df)) + "/" +
              str(min_count) + " velas. DETENIDO (fail-closed).")
        sys.exit(1)
    print("[CANDLES] " + BAR + ": " + str(len(df)) + " velas cerradas (~" +
          format(len(df) * BAR_MS / 86400000.0, '.0f') + " dias)")
    return df


def simulate(df):
    close = df["c"].values
    high = df["h"].values
    low = df["l"].values
    ts = df["ts"].values
    e_f = df["c"].ewm(span=EMA_FAST, adjust=False).mean().values
    e_s = df["c"].ewm(span=EMA_SLOW, adjust=False).mean().values
    warmup = max(EMA_SLOW * 3, 100)
    n = len(df)
    trades = []
    pos = None
    for i in range(warmup, n):
        if pos is None:
            up = e_f[i - 1] <= e_s[i - 1] and e_f[i] > e_s[i]
            down = e_f[i - 1] >= e_s[i - 1] and e_f[i] < e_s[i]
            if up:
                pos = {"side": "long", "entry": close[i], "i": i,
                       "sl": close[i] * (1 - SL_L), "tp": close[i] * (1 + TP_L)}
            elif down:
                pos = {"side": "short", "entry": close[i], "i": i,
                       "sl": close[i] * (1 + SL_S), "tp": close[i] * (1 - TP_S)}
            continue
        side, entry = pos["side"], pos["entry"]
        if side == "long":
            hit_sl, hit_tp = low[i] <= pos["sl"], high[i] >= pos["tp"]
        else:
            hit_sl, hit_tp = high[i] >= pos["sl"], low[i] <= pos["tp"]
        if hit_sl and hit_tp:
            hit_tp = False
        if hit_sl:
            exit_px = pos["sl"]
        elif hit_tp:
            exit_px = pos["tp"]
        else:
            continue
        g = (exit_px - entry) / entry
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
    print("XRP VALIDADOR v2 — LEY VIVA EMA3/21 4H · L SL6/TP7.5 · " +
          "S SL4.5/TP6 | fees " + format(FEE_RT * 100, '.2f') +
          "% r/t | IS/OOS 70/30 | USDT prohibido")
    inst = get_instrument()
    df = fetch_candles(inst["instId"], MIN_CANDLES)
    b = int(len(df) * SPLIT_IS)
    is_df = df.iloc[:b].reset_index(drop=True)
    oos_df = df.iloc[max(0, b - WARM_PAD):].reset_index(drop=True)
    b_ts = int(df["ts"].iloc[b])

    t_is = [t["net"] for t in simulate(is_df)]
    t_oos = [t["net"] for t in simulate(oos_df) if t["ts_in"] >= b_ts]
    s_is, s_oos = stats(t_is), stats(t_oos)

    print("")
    print("=== VEREDICTO LEY VIVA (EMA3/21 4H · SL/TP atomicos asimetricos) ===")
    print("IS  : " + fmt(s_is))
    print("OOS : " + fmt(s_oos))

    ok = (s_is and s_oos
          and s_is["t"] >= 15 and s_is["wr"] > s_is["be"]
          and s_is["pf"] > 1.5 and s_is["exp"] > 0
          and s_oos["t"] >= 5 and s_oos["pf"] > 1.0 and s_oos["exp"] > 0)

    print("")
    if ok:
        print("REVALIDADA: la ley de XRP aprueba doble puerta.")
        print("  El 0% WR historico fue mala suerte, no mala ley.")
        print("  XRP habilitable cuando el Carbono lo disponga.")
    else:
        print("NO REVALIDADA: la ley de XRP no aprueba doble puerta.")
        print("  El 0% WR en vivo era sintoma, no accidente.")
        print("  Destino decretado: patrulla TEST_MODE (recaudador de datos).")
    print("")
    print("DECRETO PREVIO: pase o no pase — XRP va a patrulla TEST_MODE")
    print("(nave de datos, jamas en vivo hasta decreto expreso del Carbono).")
    print("")
    print("ADVERTENCIA: pasado no es futuro. El Carbono decide.")


if __name__ == "__main__":
    main()
