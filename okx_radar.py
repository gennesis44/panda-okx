#!/usr/bin/env python3
"""
OKX radar 30m — estado del mercado para la tesis short sistémica.
Pares: FET, XLM, DOGE, SUI (perpetuos USDT de OKX).
Solo endpoints públicos de OKX (no requiere API key).
"""

import time
from itertools import combinations

import numpy as np
import pandas as pd
import requests

BASE = "https://www.okx.com"

# --- EDITA TU WATCHLIST (formato OKX: XXX-USDT-SWAP = perp) ---
WATCHLIST = [
    "FET-USDT-SWAP",
    "XLM-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "SUI-USDT-SWAP",
]
REFERENCE = "BTC-USDT-SWAP"  # quién lidera el régimen

BAR = "30m"
LIMIT = 100            # velas a pedir (~2 días)
FARO_LOOKBACK = 12     # buscar vela faro en últimas N velas cerradas
FARO_MULT = 2.0        # rango >= 2x promedio de las 20 previas
CROSS_LOOKBACK = 3     # cruce EMA7/21 reciente


def okx_get(path, **params):
    r = requests.get(BASE + path, params=params, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "0":
        raise RuntimeError(f"OKX {path} -> {j}")
    return j["data"]


def fetch_candles(inst):
    rows = okx_get("/api/v5/market/candles", instId=inst, bar=BAR, limit=str(LIMIT))
    if not rows:
        raise RuntimeError(f"{inst}: sin velas (existe en OKX?)")
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "vol",
                                     "volCcy", "volQuote", "confirm"])
    df = df.iloc[::-1].reset_index(drop=True)  # OKX manda descendente -> cronológico
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
    for col in ("o", "h", "l", "c", "vol"):
        df[col] = df[col].astype(float)
    df["closed"] = df["confirm"].eq("1")
    df["ema7"] = df["c"].ewm(span=7, adjust=False).mean()
    df["ema21"] = df["c"].ewm(span=21, adjust=False).mean()
    return df


def fetch_funding(inst):
    d = okx_get("/api/v5/public/funding-rate", instId=inst)[0]
    return float(d["fundingRate"]), int(d["fundingTime"])


def detect_faro(dfc):
    n = len(dfc)
    for i in range(n - 1, max(0, n - FARO_LOOKBACK) - 1, -1):
        prev = dfc.iloc[max(0, i - 20):i]
        if len(prev) < 10:
            continue
        avg_rng = float((prev["h"] - prev["l"]).mean())
        rng = dfc.at[i, "h"] - dfc.at[i, "l"]
        if avg_rng <= 0 or rng < FARO_MULT * avg_rng:
            continue
        body_hi = max(dfc.at[i, "o"], dfc.at[i, "c"])
        wick_up_pct = (dfc.at[i, "h"] - body_hi) / rng
        bear = dfc.at[i, "c"] < dfc.at[i, "o"]
        kind = ("rechazo/mecha-sup" if wick_up_pct >= 0.40
                else ("cuerpo bajista" if bear else "cuerpo alcista"))
        return dict(ago=n - 1 - i, ts=dfc.at[i, "ts"], high=dfc.at[i, "h"],
                    rng_x=rng / avg_rng, wick_up_pct=wick_up_pct,
                    bear=bear, kind=kind)
    return None


def bearish_cross_recent(dfc):
    s = (dfc["ema7"] < dfc["ema21"]).tail(CROSS_LOOKBACK + 1).tolist()
    return len(s) >= 2 and s[-1] and 0 in s[:-1]


def analyze(df, funding, ft):
    dfc = df[df["closed"]].reset_index(drop=True)
    last = dfc.iloc[-1]
    price, e7, e21 = last["c"], last["ema7"], last["ema21"]
    live = df.iloc[-1]["c"]  # vela en formación

    order_bear = price < e7 < e21
    below_both = price < e7 and price < e21
    cross = bearish_cross_recent(dfc)
    faro = detect_faro(dfc)
    inv = dfc.tail(FARO_LOOKBACK)["h"].max()   # invalidación del short
    sup = dfc.tail(FARO_LOOKBACK)["l"].min()

    score, tags = 0, []
    if order_bear:
        score += 2; tags.append("precio<EMA7<EMA21")
    elif below_both:
        score += 1; tags.append("precio<EMA7/EMA21")
    elif price > e21:
        score -= 1; tags.append("precio>EMA21 (debil)")
    if cross:
        score += 1; tags.append(f"cruce bajista <={CROSS_LOOKBACK} velas")
    if faro and faro["bear"]:
        score += 1; tags.append("vela faro bajista")
    if funding >= 0.0003:
        tags.append("ATENCION: funding alto, riesgo short squeeze")
    elif funding <= -0.0003:
        tags.append("funding negativo: short concurrido")

    label = "SHORT OK" if score >= 3 else ("PARCIAL" if score >= 1 else "NO SHORT")
    return dict(label=label, score=score, price=price, live=live, ema7=e7,
                ema21=e21, tags=tags, faro=faro, inv=inv, sup=sup,
                funding=funding, ft_min=(ft - time.time() * 1000) / 60000)


def fmt(x):
    return f"{x:.6g}"


def print_block(inst, r):
    print("=" * 66)
    print(f"{inst}  ->  {r['label']}  (score {r['score']:+d})")
    print(f"  precio        {fmt(r['price'])}   (en vivo {fmt(r['live'])})")
    print(f"  EMA7 / EMA21  {fmt(r['ema7'])} / {fmt(r['ema21'])}")
    print(f"  señales       {' | '.join(r['tags']) if r['tags'] else '-'}")
    if r["faro"]:
        f = r["faro"]
        print(f"  vela faro     hace {f['ago']} velas ({f['ts']:%H:%M}) | "
              f"rango {f['rng_x']:.1f}x | mecha sup {f['wick_up_pct']:.0%} | {f['kind']}")
    else:
        print(f"  vela faro     no detectada en últimas {FARO_LOOKBACK} velas")
    print(f"  invalidación  short invalida > {fmt(r['inv'])}   |   soporte {fmt(r['sup'])}")
    print(f"  funding       {r['funding']*100:.4f}%   próximo en ~{r['ft_min']:.0f} min")


def correlations(data):
    rets = {inst: np.log(df[df["closed"]].set_index("ts")["c"]).diff()
            for inst, df in data.items()}
    corr = pd.DataFrame(rets).dropna().corr()
    vals = [corr.iloc[i, j] for i, j in combinations(range(len(corr)), 2)]
    return corr, float(np.mean(vals)) if vals else float("nan")


def main():
    print(f"OKX radar {BAR} — {time.strftime('%Y-%m-%d %H:%M:%S')}"
          f"  (señales sobre velas cerradas)\n")

    try:
        btc = fetch_candles(REFERENCE)
        lb = btc[btc["closed"]].iloc[-1]
        side = "DEBAJO" if lb["c"] < lb["ema21"] else "ARRIBA"
        print(f"[regimen] BTC: close {lb['c']:.0f} | EMA7 {lb['ema7']:.0f} / "
              f"EMA21 {lb['ema21']:.0f} -> precio {side} de EMA21\n")
    except Exception as e:
        print(f"[regimen] BTC no disponible: {e}\n")

    data = {}
    for inst in WATCHLIST:
        try:
            data[inst] = fetch_candles(inst)
        except Exception as e:
            print(f"[!] {inst}: {e}")
        time.sleep(0.2)

    for inst, df in data.items():
        try:
            funding, ft = fetch_funding(inst)
        except Exception:
            funding, ft = float("nan"), int(time.time() * 1000)
        print_block(inst, analyze(df, funding, ft))

    if len(data) >= 2:
        corr, avg = correlations(data)
        print("\n" + "=" * 66)
        print("CORRELACIÓN entre pares (retornos 30m):")
        print(corr.round(2).to_string())
        if avg >= 0.85:
            msg = "MISMA GRÁFICA CONFIRMADA — es UN trade con N patas, no N trades"
        elif avg >= 0.60:
            msg = "correlación media — tesis sistémica razonable, identifica al líder"
        else:
            msg = "los pares DIVERGEN — la tesis 'todos igual' se debilita"
        print(f"\n correlación promedio: {avg:.2f}  ->  {msg}")


if __name__ == "__main__":
    main()
