# backtest-gemini-v3.py — CICLO 3 · FILTRO DE SESION (C3 Gemini)
#   C3-LN  : solo entradas 08:00-16:00 UTC (Londres-NY solapada)
#   C3-L   : solo 07:00-16:00 UTC        C3-NY: solo 12:00-21:00 UTC
#   C3-FUERA: solo FUERA de 08-16 (test de la premisa: debe ser MALO si
#   Gemini acierta). Mapa horario del baseline incluido.
#   Estandar: 3 ventanas deslizantes.
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


def run(df, horas=None):
    """Entradas EMA8/34 SL4/TP5.5. horas=(h1,h2) UTC: solo entra si
    h1 <= hora_vela < h2. horas=None: sin filtro."""
    c, h, l = df["c"].values, df["h"].values, df["l"].values
    sign = np.sign(df["ema_f"] - df["ema_s"]).values
    hora = ((df["ts"] // 3_600_000) % 24).astype(int)
    n = len(df)
    trades, pos = [], None
    i = 200
    while i < n:
        if pos is None:
            up = sign[i-1] <= 0 and sign[i] > 0
            dn = sign[i-1] >= 0 and sign[i] < 0
            if up or dn:
                entra = True
                if horas is not None:
                    entra = horas[0] <= hora[i] < horas[1]
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
    hora = ((df["ts"] // 3_600_000) % 24).astype(int)

    print("\n=== MAPA HORARIO DEL BASELINE (hora UTC de la vela de senal) ===")
    print(f"{'hora':<6}{'trades':<8}{'W':<4}{'L':<4}{'ret%':<9}")
    for hh in range(24):
        seg = [p for (i, p) in base if hora[i] == hh]
        if seg:
            a = np.array(seg)
            print(f"{hh:<6}{len(a):<8}{int((a>0).sum()):<4}{int((a<=0).sum()):<4}"
                  f"{a.sum()*100:+.2f}")

    variantes = [
        ("BASELINE (sin filtro)", None),
        ("C3-LN  08-16 UTC", (8, 16)),
        ("C3-L   07-16 UTC", (7, 16)),
        ("C3-NY  12-21 UTC", (12, 21)),
        ("C3-FUERA (premisa inversa)", None),
    ]

    print("\n=== CICLO 3: FILTRO DE SESION — 3 VENTANAS ===")
    for nombre, horas in variantes:
        if nombre.startswith("C3-FUERA"):
            trades = [(i, p) for (i, p) in base if not (8 <= hora[i] < 16)]
        else:
            trades = run(df, horas)
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
