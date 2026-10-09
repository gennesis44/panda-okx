# backtest-avax.py — AVAX VALIDADOR · geometrías EMA sobre XPERP AVAX/USD
#   DECRETO CARBONO (2026-10-09): base 4H · candidata EMA 8/21 · SL/TP 4/5.5
#   Provisional hasta que este validador confirme en Actions.
#
#   METODO GSCSI:
#     - velas CERRADAS (confirm==1) · entrada a cierre de vela de cruce
#     - salida: SL primero (conservador) -> TP -> cruce contrario a cierre
#     - fees 0.13% r/t (convención flota: validadores ADA/INJ)
#     - XPERP vencimiento mas lejano · USDT PROHIBIDO
#     - sin candado (decreto; en 4H la vela ya filtra ruido intradiario)
#
#   GUARDIAS ANTI-MINERIA (Ax3):
#     - split 70/30: aprobar EN muestra y VERIFICAR fuera de muestra
#     - clones excluidos: (3,15)=ley SUI · (3,21)=ley INJ/XRP
#     - (8,34) incluida: es la ley viva actual — se juzga contra la candidata
#     - recomendación restringida a 4H (decreto); 1H referencial
#
# Ax3: sin secrets (API pública) · exit 1 ante descarga incompleta · sin opinion

import sys
import time
from itertools import product

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://www.okx.com"
UNDERLYING = "AVAX-USD"
FEE_ROUNDTRIP = 0.0013

TIMEFRAMES = ["1H", "4H"]
BAR_MS = {"1H": 3600000, "4H": 14400000}
MIN_CANDLES = {"1H": 1500, "4H": 1000}
EMA_PAIRS = [(3, 34), (8, 15), (8, 21), (8, 34)]
SLTP_SET = [
    (0.03, 0.08),
    (0.04, 0.055),
    (0.02, 0.04),
    (0.05, 0.08),
    (0.06, 0.075),
]
RECOMMEND_TF = "4H"
SPLIT_IS = 0.70
WARM_PAD = 102


def get_farthest_future_instrument(uly=UNDERLYING):
    """FUTURES de AVAX-USD con vencimiento mas lejano, no-USDT."""
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
          " | XPERP mas lejano")
    return inst


def fetch_history_candles(inst_id, bar, min_count):
    """Pagina hacia atras con 'after' (OKX: records anteriores al ts).
    Fail-closed: <80% del objetivo = exit 1."""
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
              str(min_count) + " velas (" + inst_id + " " + bar +
              "). DETENIDO (fail-closed).")
        sys.exit(1)
    days = len(df) * BAR_MS[bar] / 86400000.0
    print("[CANDLES] " + inst_id + " " + bar + ": " + str(len(df)) +
          " velas cerradas (~" + format(days, '.0f') + " dias)")
    return df


def run_backtest(df, ema_fast, ema_slow, sl_pct, tp_pct, min_entry_ts=None):
    """Cruce EMA en velas cerradas. 1 posicion. SL primero (conservador),
    luego TP, o cruce contrario a cierre. Devuelve trades filtrables por ts."""
    close = df["c"].values
    high = df["h"].values
    low = df["l"].values
    ts = df["ts"].values
    ema_f = df["c"].ewm(span=ema_fast, adjust=False).mean().values
    ema_s = df["c"].ewm(span=ema_slow, adjust=False).mean().values

    warmup = max(3 * ema_slow, 100)
    n = len(df)
    trades = []
    position = None

    i = warmup
    while i < n:
        if position is None:
            crossed_up = ema_f[i - 1] <= ema_s[i - 1] and ema_f[i] > ema_s[i]
            crossed_down = ema_f[i - 1] >= ema_s[i - 1] and ema_f[i] < ema_s[i]
            if crossed_up:
                position = {"side": "long", "entry": close[i], "i_in": i}
            elif crossed_down:
                position = {"side": "short", "entry": close[i], "i_in": i}
            i += 1
            continue

        side = position["side"]
        entry = position["entry"]
        if side == "long":
            sl_px = entry * (1 - sl_pct)
            tp_px = entry * (1 + tp_pct)
            hit_sl = low[i] <= sl_px
            hit_tp = high[i] >= tp_px
        else:
            sl_px = entry * (1 + sl_pct)
            tp_px = entry * (1 - tp_pct)
            hit_sl = high[i] >= sl_px
            hit_tp = low[i] <= tp_px

        exit_price = None
        if hit_sl:
            exit_price = sl_px
        elif hit_tp:
            exit_price = tp_px
        else:
            opp = ((side == "long" and ema_f[i] < ema_s[i]) or
                   (side == "short" and ema_f[i] > ema_s[i]))
            if opp:
                exit_price = close[i]

        if exit_price is not None:
            gross = (exit_price - entry) / entry
            if side == "short":
                gross = -gross
            trades.append({"ts_in": int(ts[position["i_in"]]),
                           "net": gross - FEE_ROUNDTRIP})
            position = None

        i += 1

    if min_entry_ts is not None:
        trades = [t for t in trades if t["ts_in"] >= min_entry_ts]
    return trades


def stats_from_trades(nets):
    n = len(nets)
    if n == 0:
        return None
    arr = np.array(nets)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    wr = len(wins) / n
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = abs(losses.mean()) if len(losses) else 0.0
    be_wr = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else 1.0
    gw = wins.sum()
    gl = abs(losses.sum())
    pf = gw / gl if gl > 0 else float("inf")
    equity = np.cumsum(arr)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak).min() if len(equity) else 0.0
    return {"trades": n, "wr": wr, "breakeven_wr": be_wr, "pf": pf,
            "expectancy": arr.mean(), "max_dd": dd, "total_return": equity[-1]}


def fmt(s):
    if s is None or s["trades"] == 0:
        return "t=0"
    return ("t=" + str(s["trades"]) +
            " WR=" + format(s["wr"] * 100, '.1f') + "%" +
            " BE=" + format(s["breakeven_wr"] * 100, '.1f') + "%" +
            " PF=" + format(s["pf"], '.2f') +
            " exp=" + format(s["expectancy"] * 100, '+.3f') + "%" +
            " DD=" + format(s["max_dd"] * 100, '.1f') + "%")


def main():
    print("AVAX VALIDADOR — grid EMA x SL/TP sobre 1H/4H | fees " +
          format(FEE_ROUNDTRIP * 100, '.2f') + "% r/t | split IS/OOS " +
          format(SPLIT_IS * 100, '.0f') + "/" +
          format((1 - SPLIT_IS) * 100, '.0f') + " | USDT prohibido")
    inst = get_farthest_future_instrument()
    inst_id = inst["instId"]

    splits = {}
    for bar in TIMEFRAMES:
        df = fetch_history_candles(inst_id, bar, MIN_CANDLES[bar])
        b_idx = int(len(df) * SPLIT_IS)
        splits[bar] = {
            "is": df.iloc[:b_idx].reset_index(drop=True),
            "oos": df.iloc[max(0, b_idx - WARM_PAD):].reset_index(drop=True),
            "b_ts": int(df["ts"].iloc[b_idx]),
        }

    results = []
    for bar, (ef, es), (sl, tp) in product(TIMEFRAMES, EMA_PAIRS, SLTP_SET):
        sp = splits[bar]
        tr_is = run_backtest(sp["is"], ef, es, sl, tp)
        tr_oos = run_backtest(sp["oos"], ef, es, sl, tp,
                              min_entry_ts=sp["b_ts"])
        results.append({
            "tf": bar, "ema": str(ef) + "/" + str(es),
            "sl_tp": format(sl * 100, '.1f') + "/" + format(tp * 100, '.1f'),
            "is": stats_from_trades([t["net"] for t in tr_is]),
            "oos": stats_from_trades([t["net"] for t in tr_oos]),
        })

    results.sort(key=lambda r: (r["is"]["expectancy"] if r["is"] else -9),
                 reverse=True)

    print("")
    print("=== TABLA (IS=in-sample | OOS=out-of-sample) ===")
    for r in results:
        print("[" + r["tf"] + "] EMA " + r["ema"] + " SL/TP " + r["sl_tp"] +
              "% | IS  " + fmt(r["is"]) +
              " | OOS " + fmt(r["oos"]))

    robust = [r for r in results
              if r["tf"] == RECOMMEND_TF and r["is"] and r["oos"]
              and r["is"]["trades"] >= 15
              and r["is"]["wr"] > r["is"]["breakeven_wr"]
              and r["is"]["pf"] > 1.5
              and r["is"]["expectancy"] > 0
              and r["oos"]["trades"] >= 5
              and r["oos"]["pf"] > 1.0
              and r["oos"]["expectancy"] > 0]

    print("")
    print("=== RECOMENDACION (solo " + RECOMMEND_TF + ", decreto) ===")
    if robust:
        best = max(robust, key=lambda r: r["is"]["expectancy"])
        print("GEOMETRIA ROBUSTA: TF=" + best["tf"] + " EMA=" + best["ema"] +
              " SL/TP=" + best["sl_tp"] + "%")
        print("  IS : " + fmt(best["is"]))
        print("  OOS: " + fmt(best["oos"]))
        print("  Cumple decreto (>=15 trades, WR>BE, PF>1.5, exp>0) y verifica OOS.")
        print("  Si el Carbono ratifica: actualizar main-avax.py y re-inspeccion.")
    else:
        print("NINGUNA geometria cumple los criterios en IS y verifica en OOS.")
        if results and results[0]["is"]:
            top = results[0]
            print("  Mejor IS (referencia NO validada): TF=" + top["tf"] +
                  " EMA=" + top["ema"] + " SL/TP=" + top["sl_tp"] + "%")
        print("  No promover a produccion. El Carbono decide.")

    print("")
    print("ADVERTENCIA: pasado no es futuro. Muestras cortas en 4H por diseño.")
    print("El Carbono decide.")


if __name__ == "__main__":
    main()
