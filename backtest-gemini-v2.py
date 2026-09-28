# backtest-gemini-v2.py — CICLO 2 · 3 VENTANAS DESLIZANTES
#   C2-AFINADA SL1.8/TP2.7 · ATR14 SL1.5x/TP2.25x vs BASELINE y C2
#   Solo sobrevive lo positivo en las 3 ventanas.
import time
import requests
import pandas as pd
import numpy as np

BASE_URL = "https://www.okx.com"
UNDERLYING = "AVAX-USD"
FEE_RT = 0.001
N_VENTANAS = 3


def get_instrument():
    r = requests.get(f"{BASE_URL}/api/v5/public/instruments",
                     params={"instType": "FUTURES", "uly": UNDERLYING}, timeout=15)
    cands = [d for d in r.json()["data"]
             if d.get("settleCcy", "").upper() != "USDT" and d.get("expTime")]
    cands.sort(key=lambda d: int(d["expTime"]), reverse=True)
    print(f"[INSTRUMENT] {cands[0]['instId']}")
    return cands[0]["instId"]


def fetch_candles(inst_id, bar, min_count):
    rows_all, after = [], ""
    while len(rows_all) < min_count:
        params = {"instId": inst_id, "bar": bar, "limit": "300"}
        if after:
            params["after"] = after
        r = requests.get(f"{BASE_URL}/api/v5/market/history-candles",
                         params=params, timeout=15)
        rows = r.json().get("data", [])
        if not rows:
            break
        rows_all.extend(rows)
        after = rows[-1][0]
        time.sleep(0.15)
    cols = ["ts", "o", "h", "l", "c", "vol", "v1", "v2", "confirm"]
    df = pd.DataFrame(rows_all, columns=cols)
    for col in ["o", "h", "l", "c", "vol"]:
        df[col] = df[col].astype(float)
    df["ts"] = df["ts"].astype(np.int64)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    df = df[df["confirm"] == "1"].reset_index(drop=True)
    print(f"[CANDLES] {bar}: {len(df)} velas cerradas")
    return df


def run(df, sl_spec, tp_spec):
    c, h, l = df["c"].values, df["h"].values, df["l"].values
    sign = np.sign(df["ema_f"] - df["ema_s"]).values
    atr = None
    if isinstance(sl_spec, tuple):
        tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
        tr[0] = h[0] - l[0]
        atr = pd.Series(tr).ewm(span=14, adjust=False).mean().values
    n = len(df)
    trades, pos = [], None
    i = 200
    while i < n:
        if pos is None:
            up = sign[i-1] <= 0 and sign[i] > 0
            dn = sign[i-1] >= 0 and sign[i] < 0
            if up or dn:
                side = "long" if up else "short"
                if atr is not None:
                    pos = (side, c[i], sl_spec[1] * atr[i] / c[i],
                           tp_spec[1] * atr[i] / c[i], i)
                else:
                    pos = (side, c[i], sl_spec, tp_spec, i)
            i += 1
            continue
        side, entry, slp_f, tpp_f, i0 = pos
        slp = entry * (1 - slp_f) if side == "long" else entry * (1 + slp_f)
        tpp = entry * (1 + tpp_f) if side == "long" else entry * (1 - tpp_f)
        pnl, out = None, False
        if side == "long":
            if l[i] <= slp: pnl, out = -slp_f, True
            elif h[i] >= tpp: pnl, out = tpp_f, True
        else:
            if h[i] >= slp: pnl, out = -slp_f, True
            elif l[i] <= tpp: pnl, out = tpp_f, True
        if not out:
            opp = (side == "long" and sign[i] < 0) or (side == "short" and sign[i] > 0)
            if opp:
                raw = (c[i] - entry) / entry
                pnl = raw if side == "long" else -raw
                out = True
        if out:
            trades.append((i0, pnl - FEE_RT))
            pos = None
        i += 1
    return trades


def stats(trades):
    if not trades:
        return None
    a = np.array([t[1] for t in trades])
    w, ls = a[a > 0], a[a <= 0]
    wr = len(w) / len(a)
    aw = w.mean() if len(w) else 0
    al = abs(ls.mean()) if len(ls) else 0
    be = al / (aw + al) if (aw + al) else 1
    pf = w.sum() / abs(ls.sum()) if ls.sum() else np.inf
    return {"trades": len(a), "wr": wr, "be": be, "pf": pf, "exp": a.mean(),
            "ret": a.sum()}


def main():
    inst = get_instrument()
    df = fetch_candles(inst, "1H", 1200)
    df["ema_f"] = df["c"].ewm(span=8, adjust=False).mean()
    df["ema_s"] = df["c"].ewm(span=34, adjust=False).mean()
    n = len(df)
    bordes = np.linspace(200, n, N_VENTANAS + 1).astype(int)
    print(f"[VENTANAS] {[(int(bordes[k]), int(bordes[k+1])) for k in range(N_VENTANAS)]}")

    variantes = [
        ("BASELINE  SL4.0/TP5.5", 0.040, 0.055),
        ("C2        SL2.0/TP3.0", 0.020, 0.030),
        ("C2-AFINADA SL1.8/TP2.7", 0.018, 0.027),
        ("ATR14     SL1.5x/TP2.25x", ("atr", 1.5), ("atr", 2.25)),
    ]

    print("\n=== CICLO 2: MAPA GEMINI v2 — 3 VENTANAS DESLIZANTES ===")
    for nombre, sl, tp in variantes:
        trades = run(df, sl, tp)
        print(f"\n-- {nombre} --")
        total = []
        ok_todas = True
        for k in range(N_VENTANAS):
            seg = [(i, p) for (i, p) in trades if bordes[k] <= i < bordes[k+1]]
            st = stats(seg)
            if st is None:
                print(f"   V{k+1}: sin trades")
                ok_todas = False
                continue
            ok = st["exp"] > 0
            ok_todas = ok_todas and ok
            print(f"   V{k+1}: tr={st['trades']:<3} wr={st['wr']*100:5.1f}% "
                  f"pf={st['pf']:5.2f} exp={st['exp']*100:+.3f}% ret={st['ret']*100:+.2f}%"
                  f"  {'+' if ok else '-'}")
            total.extend(seg)
        st = stats(total)
        if st:
            print(f"   TOTAL: tr={st['trades']} wr={st['wr']*100:.1f}% "
                  f"pf={st['pf']:.2f} ret={st['ret']*100:+.2f}%")
            print(f"   >>> {'SOBREVIVE (3/3 ventanas)' if ok_todas else 'CAE (falla alguna ventana)'}")


if __name__ == "__main__":
    main()
