# backtest-gemini.py — VEREDICTO SOBRE EL MAPA GEMINI (inter-nodos)
#   Baseline: AVAX 1H EMA8/34 SL4/TP5.5 (ganadora del 27-sep: +12.86%)
#   Cada parametro de Gemini corre AISLADO contra el baseline:
#     A1: veto EMA200 4H binario        A2: banda EMA200 +-0.4%
#     C1: SL1.5/TP4.5 (1:3)             C2: SL2/TP3 (1:1.5)
#     D1: veto VWAP diario (no cortos > +0.5%)
#     D2: veto funding >0.04% (si la API publica lo da; si no: NO TESTEABLE)
#   Criterios de robustez: >=15 trades · WR>BE · PF>1.5 · expect>0
import time
import requests
import pandas as pd
import numpy as np

BASE_URL = "https://www.okx.com"
UNDERLYING = "AVAX-USD"
FEE_RT = 0.001  # 0.05% x 2 lados


def get_instrument():
    r = requests.get(f"{BASE_URL}/api/v5/public/instruments",
                     params={"instType": "FUTURES", "uly": UNDERLYING}, timeout=15)
    cands = [d for d in r.json()["data"]
             if d.get("settleCcy", "").upper() != "USDT" and d.get("expTime")]
    cands.sort(key=lambda d: int(d["expTime"]), reverse=True)
    print(f"[INSTRUMENT] {cands[0]['instId']} ctVal={cands[0]['ctVal']}")
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


def fetch_funding_history(inst_id):
    """Intenta historial de funding (puede no existir para XPERP FUTURES)."""
    try:
        r = requests.get(f"{BASE_URL}/api/v5/public/funding-rate-history",
                         params={"instId": inst_id, "limit": "100"}, timeout=15)
        data = r.json().get("data", [])
        print(f"[FUNDING] {len(data)} registros para {inst_id}")
        return pd.DataFrame(data) if data else None
    except Exception as e:
        print(f"[FUNDING] no disponible: {e}")
        return None


def ema(s, span):
    return s.ewm(span=span, adjust=False).mean()


def prepare(df1h, df4h):
    df = df1h.copy()
    df["ema_f"] = ema(df["c"], 8)
    df["ema_s"] = ema(df["c"], 34)
    df["sign"] = np.sign(df["ema_f"] - df["ema_s"])

    # EMA200 4H alineada: ultimo valor 4H CERRADO con ts <= ts de la vela 1H
    d4 = df4h.copy()
    d4["ema200"] = ema(d4["c"], 200)
    d4 = d4[d4["ema200"].notna()]
    merged = pd.merge_asof(df.sort_values("ts"), d4[["ts", "ema200"]],
                           on="ts", direction="backward")
    df["ema200_4h"] = merged["ema200"].values

    # VWAP diaria acumulada (1H): tp=(h+l+c)/3, reinicia cada dia UTC
    day = (df["ts"] // 86_400_000)
    tp = (df["h"] + df["l"] + df["c"]) / 3
    pv = tp * df["vol"]
    df["vwap"] = (pv.groupby(day).cumsum() / df["vol"].groupby(day).cumsum())
    df["vwap_prev"] = df["vwap"].shift(1)
    return df


def run(df, sl_pct, tp_pct, veto=None):
    """veto(i, dir) -> True si la entrada queda vetada."""
    c, h, l, sign = df["c"].values, df["h"].values, df["l"].values, df["sign"].values
    n = len(df)
    trades, pos = [], None
    i = 200
    while i < n:
        if pos is None:
            up = sign[i-1] <= 0 and sign[i] > 0
            dn = sign[i-1] >= 0 and sign[i] < 0
            if up and not (veto and veto(i, "long")):
                pos = ("long", c[i]); 
            elif dn and not (veto and veto(i, "short")):
                pos = ("short", c[i])
            i += 1
            continue
        side, entry = pos
        slp = entry * (1 - sl_pct) if side == "long" else entry * (1 + sl_pct)
        tpp = entry * (1 + tp_pct) if side == "long" else entry * (1 - tp_pct)
        pnl, out = None, False
        if side == "long":
            if l[i] <= slp: pnl, out = -sl_pct, True
            elif h[i] >= tpp: pnl, out = tp_pct, True
        else:
            if h[i] >= slp: pnl, out = -sl_pct, True
            elif l[i] <= tpp: pnl, out = tp_pct, True
        if not out:
            opp = (side == "long" and sign[i] < 0) or (side == "short" and sign[i] > 0)
            if opp:
                raw = (c[i] - entry) / entry
                pnl = raw if side == "long" else -raw
                out = True
        if out:
            trades.append(pnl - FEE_RT)
            pos = None
        i += 1
    return trades


def stats(trades):
    if not trades:
        return None
    a = np.array(trades)
    w, ls = a[a > 0], a[a <= 0]
    wr = len(w) / len(a)
    aw = w.mean() if len(w) else 0
    al = abs(ls.mean()) if len(ls) else 0
    be = al / (aw + al) if (aw + al) else 1
    pf = w.sum() / abs(ls.sum()) if ls.sum() else np.inf
    eq = np.cumsum(a)
    dd = (eq - np.maximum.accumulate(eq)).min()
    return {"trades": len(a), "wr": wr, "be": be, "pf": pf,
            "exp": a.mean(), "dd": dd, "ret": eq[-1]}


def main():
    inst = get_instrument()
    df1h = fetch_candles(inst, "1H", 1100)
    df4h = fetch_candles(inst, "4H", 700)
    funding = fetch_funding_history(inst)
    df = prepare(df1h, df4h)

    ema200 = df["ema200_4h"].values
    vwap_prev = df["vwap_prev"].values
    close = df["c"].values

    variants = [
        ("BASELINE 8/34 SL4/TP5.5", 0.040, 0.055, None),
        ("A1 +veto EMA200_4H", 0.040, 0.055,
         lambda i, d: (close[i] <= ema200[i]) if d == "long" else (close[i] >= ema200[i])),
        ("A2 +banda EMA200 +-0.4%", 0.040, 0.055,
         lambda i, d: (close[i] <= ema200[i]*0.996) if d == "long" else (close[i] >= ema200[i]*1.004)),
        ("C1 SL1.5/TP4.5 (1:3)", 0.015, 0.045, None),
        ("C2 SL2/TP3 (1:1.5)", 0.020, 0.030, None),
        ("D1 +veto VWAP cortos", 0.040, 0.055,
         lambda i, d: (d == "short" and vwap_prev[i] and close[i] > vwap_prev[i]*1.005)),
    ]
    if funding is not None and "fundingRate" in funding.columns:
        funding["ts"] = funding["fundingTime"].astype(np.int64)
        funding["fr"] = funding["fundingRate"].astype(float)
        m = pd.merge_asof(df.sort_values("ts"), funding[["ts", "fr"]],
                          on="ts", direction="backward")
        fr = m["fr"].values
        variants.append(("D2 +veto funding>0.04%", 0.040, 0.055,
                         lambda i, d: (d == "long" and not np.isnan(fr[i]) and fr[i] > 0.0004)))
    else:
        print("[D2] Funding history no disponible publicamente para XPERP "
              "-> D2 queda NO TESTEABLE (documentado para Gemini).")

    print("\n=== VEREDICTO SOBRE EL MAPA GEMINI (vs baseline) ===")
    print(f"{'Variante':<28}{'Tr':<5}{'WR%':<7}{'BE%':<7}{'PF':<7}"
          f"{'Exp%':<9}{'Ret%':<9}{'MaxDD%':<8}{'Juicio'}")
    for name, sl, tp, veto in variants:
        st = stats(run(df, sl, tp, veto))
        if st is None:
            print(f"{name:<28}{'0':<5} - sin trades")
            continue
        robust = (st["trades"] >= 15 and st["wr"] > st["be"]
                  and st["pf"] > 1.5 and st["exp"] > 0)
        juicio = "ROBUSTA" if robust else "descartada"
        print(f"{name:<28}{st['trades']:<5}{st['wr']*100:<7.1f}{st['be']*100:<7.1f}"
              f"{st['pf']:<7.2f}{st['exp']*100:<9.3f}{st['ret']*100:<9.2f}"
              f"{st['dd']*100:<8.2f}{juicio}")
    print("\nProtocolo inter-nodos: esta tabla se devuelve a GEMINI tal cual.")
    print("Un filtro solo se implanta si ROBUSTA y supera al baseline en retorno.")


if __name__ == "__main__":
    main()
