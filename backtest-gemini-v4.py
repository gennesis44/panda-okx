# backtest-gemini-v4.py — CICLO 4 · C4 VOLUME SURGE (Gemini)
#   C4-SURGE : solo entra si Vol_vela > 1.5 x SMA(Vol,20) previa
#   C4-PLANO : inverso estricto (Vol_vela < 1.5 x SMA(Vol,20) previa)
#   Estandar: 3 ventanas deslizantes. Baseline como referencia.
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


def run(df, modo=None):
    """modo: None (baseline) | 'surge' (vol>1.5*sma20 previa) | 'plano' (vol<1.5x)."""
    c, h, l = df["c"].values, df["h"].values, df["l"].values
    vol = df["vol"].values
    sma_vol = df["vol"].rolling(20).mean().shift(1).values  # previas, sin la propia vela
    sign = np.sign(df["ema_f"] - df["ema_s"]).values
    n = len(df)
    trades, pos = [], None
    i = 200
    while i < n:
        if pos is None:
            up = sign[i-1] <= 0 and sign[i] > 0
            dn = sign[i-1] >= 0 and sign[i] < 0
            if up or dn:
                entra = True
                if modo == "surge":
                    entra = not np.isnan(sma_vol[i]) and vol[i] > 1.5 * sma_vol[i]
                elif modo == "plano":
                    entra = not np.isnan(sma_vol[i]) and vol[i] < 1.5 * sma_vol[i]
                if entra:
                    pos = ("long" if up else "short", c[i], i)
            i += 1
            continue
        side, entry, i0 = pos
        slp = entry * 0.96 if side == "long" else entry * 1.04
        tpp = entry * 1.055 if side == "long" else entry * 0.945
        pnl, out = None, False
        if side == "long":
            if l[i] <= slp: pnl, out = -0.04, True
            elif h[i] >= tpp: pnl, out = 0.055, True
        else:
            if h[i] >= slp: pnl, out = -0.04, True
            elif l[i] <= tpp: pnl, out = 0.055, True
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

    base = run(df)
    surge = run(df, "surge")
    plano = run(df, "plano")
    print(f"[REPARTO] baseline={len(base)} | surge={len(surge)} | "
          f"plano={len(plano)} (surge+plano={len(surge)+len(plano)})")

    variantes = [("BASELINE (sin filtro)", base),
                 ("C4-SURGE vol>1.5x", surge),
                 ("C4-PLANO vol<1.5x (inverso)", plano)]

    print("\n=== CICLO 4: C4 VOLUME SURGE — 3 VENTANAS ===")
    for nombre, trades in variantes:
        print(f"\n-- {nombre} --")
        total, ok_todas = [], True
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
            print(f"   >>> {'SOBREVIVE (3/3)' if ok_todas else 'CAE'}")


if __name__ == "__main__":
    main()
