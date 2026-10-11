# backtest-inj2.py — INJ VALIDADOR v2 · LEY VIVA 3/21 1H bajo el cuchillo
#   DECRETO CARBONO (2026-10-11): validar la ley que INJ lleva semanas
#   ejecutando sin certificado (agujero del acta desde la reconstrucción).
#
#   FIEL AL BOT (main-inj.py observado en logs):
#     - ley: EMA3/21 · 1H · sin candado, sin RSI (extirpados por decreto)
#     - geometria: 5 ct · LONG y SHORT simetricos
#     - velas 1H CERRADAS · entrada a cierre de vela de cruce
#   Ax3: IS/OOS 70/30 anti-mineria · fees 0.13% r/t · XPERP vencimiento mas
#        lejano · USDT PROHIBIDO · fail-closed en descarga · sin secrets
#   FIX rev.2 (A565G): SPLIT_IS declarado — faltaba la constante (acta #9)
#
#   SALIDA: tabla IS/OOS + veredicto (>=15 trades, WR>BE, PF>1.5, exp>0
#           en IS · >=5 trades, PF>1.0, exp>0 en OOS)

import sys
import time

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://www.okx.com"
UNDERLYING = "INJ-USD"
FEE_RT = 0.0013
BAR = "1H"
BAR_MS = 3600000
MIN_CANDLES = 1500
EMA_FAST, EMA_SLOW = 3, 21
SPLIT_IS = 0.70
WARM_PAD = 30


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
          " | tickSz=" + str(inst.get("tickSz")) +
          " | XPERP mas lejano")
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
    """Cruce EMA3/21 1H sin candado — fiel al bot. Salida por cruce
    contrario (sin SL/TP atomicos observados en logs del bot)."""
    close = df["c"].values
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
                pos = {"side": "long", "entry": close[i], "i": i}
            elif down:
                pos = {"side": "short", "entry": close[i], "i": i}
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
    print("INJ VALIDADOR v2 — LEY VIVA EMA3/21 1H sin candado | fees " +
          format(FEE_RT * 100, '.2f') + "% r/t | IS/OOS 70/30 | USDT prohibido")
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
    print("=== VEREDICTO LEY VIVA (EMA3/21 1H, cruce-contrario como salida) ===")
    print("IS  : " + fmt(s_is))
    print("OOS : " + fmt(s_oos))

    ok = (s_is and s_oos
          and s_is["t"] >= 15 and s_is["wr"] > s_is["be"]
          and s_is["pf"] > 1.5 and s_is["exp"] > 0
          and s_oos["t"] >= 5 and s_oos["pf"] > 1.0 and s_oos["exp"] > 0)

    print("")
    if ok:
        print("REVALIDADA: la ley viva de INJ aprueba doble puerta.")
        print("  Zarpa con certificado cuando el Carbono lo disponga.")
    else:
        print("NO REVALIDADA: la ley viva no aprueba doble puerta.")
        print("  INJ navega sin certificado — el Carbono decide: interruptor,")
        print("  enmienda con nuevo backtest, o aceptar riesgo interino.")
    print("")
    print("NOTA: el bot live usa cruce-contrario como salida (sin SL/TP")
    print("atomicos observados en logs). Si el Carbono quiere SL/TP fijos,")
    print("es enmienda de main-inj.py — con nuevo cuchillo.")
    print("")
    print("ADVERTENCIA: pasado no es futuro. El Carbono decide.")


if __name__ == "__main__":
    main()
