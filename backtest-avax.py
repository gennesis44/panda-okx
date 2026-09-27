# backtest-avax.py
"""
Backtest de geometrías EMA para XPERP AVAX/USD (futuro USD más lejano, OKX).
Solo API pública (sin auth). Requiere: requests, pandas.

Descarga velas 1H y 4H, corre una rejilla de EMAs rápidas/lentas y SL/TP
simétricos/asimétricos, simula 1 posición a la vez sobre velas CERRADAS
(confirm==1), cierre por SL/TP intravela (SL primero, conservador) o por
cruce contrario, y emite tabla comparativa + recomendación.

Criterios de robustez (anti-sobreajuste, lección STX EMA20/21):
  - >= 15 trades
  - WR > breakeven WR (fees taker 0.05% x2 lados = 0.10% round-trip)
  - Profit Factor > 1.5
  - Expectativa neta por trade > 0
"""

import time
import requests
import pandas as pd
import numpy as np
from itertools import product

BASE_URL = "https://www.okx.com"  # API pública de mercado, sin auth
UNDERLYING = "AVAX-USD"
FEE_TAKER = 0.0005          # 0.05% por lado
FEE_ROUNDTRIP = FEE_TAKER * 2  # 0.10%

TIMEFRAMES = ["1H", "4H"]
EMA_FAST_SET = [3, 8]
EMA_SLOW_SET = [15, 21, 34]
# (SL%, TP%) — incluye simétricos y asimétricos pedidos
SLTP_SET = [
    (0.03, 0.08),
    (0.04, 0.055),
    (0.02, 0.04),
    (0.05, 0.08),
    (0.06, 0.075),
]
MIN_CANDLES = 1000


def get_farthest_future_instrument(uly=UNDERLYING):
    """Instrumento FUTURES de AVAX-USD con vencimiento más lejano, no-USDT."""
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
        raise RuntimeError(f"No hay FUTURES no-USDT para {uly}")
    candidates.sort(key=lambda d: int(d["expTime"]), reverse=True)
    inst = candidates[0]
    print(
        f"[INSTRUMENT] instId={inst['instId']} expTime={inst['expTime']} "
        f"ctVal={inst['ctVal']} ctValCcy={inst['ctValCcy']} "
        f"settleCcy={inst['settleCcy']}"
    )
    return inst


def fetch_history_candles(inst_id, bar, min_count=MIN_CANDLES):
    """Pagina hacia atrás con 'after', limit=300, sleep 0.15s."""
    all_rows = []
    after = ""
    while len(all_rows) < min_count:
        params = {"instId": inst_id, "bar": bar, "limit": "300"}
        if after:
            params["after"] = after
        r = requests.get(
            f"{BASE_URL}/api/v5/market/history-candles",
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json().get("data", [])
        if not rows:
            break
        all_rows.extend(rows)
        after = rows[-1][0]  # ts más antiguo del lote -> siguiente página
        time.sleep(0.15)
    if not all_rows:
        raise RuntimeError(f"Sin velas para {inst_id} {bar}")

    cols = ["ts", "o", "h", "l", "c", "vol", "volCcy", "volCcyQuote", "confirm"]
    df = pd.DataFrame(all_rows, columns=cols)
    for col in ["o", "h", "l", "c", "vol"]:
        df[col] = df[col].astype(float)
    df["ts"] = df["ts"].astype(np.int64)
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    df = df[df["confirm"] == "1"].reset_index(drop=True)  # solo cerradas
    print(f"[CANDLES] {inst_id} {bar}: {len(df)} velas cerradas descargadas")
    return df


def run_backtest(df, ema_fast, ema_slow, sl_pct, tp_pct):
    close = df["c"].values
    high = df["h"].values
    low = df["l"].values
    ema_f = df["c"].ewm(span=ema_fast, adjust=False).mean().values
    ema_s = df["c"].ewm(span=ema_slow, adjust=False).mean().values
    sign = np.sign(ema_f - ema_s)

    warmup = ema_slow + 2
    trades = []
    position = None  # dict: side, entry_price, entry_idx

    i = warmup
    n = len(df)
    while i < n:
        if position is None:
            crossed_up = sign[i - 1] <= 0 and sign[i] > 0
            crossed_down = sign[i - 1] >= 0 and sign[i] < 0
            if crossed_up:
                position = {"side": "long", "entry": close[i]}
            elif crossed_down:
                position = {"side": "short", "entry": close[i]}
            i += 1
            continue

        # posición abierta: buscar salida en la siguiente vela cerrada
        j = i
        entry = position["entry"]
        side = position["side"]
        if side == "long":
            sl_px = entry * (1 - sl_pct)
            tp_px = entry * (1 + tp_pct)
        else:
            sl_px = entry * (1 + sl_pct)
            tp_px = entry * (1 - tp_pct)

        exited = False
        if side == "long":
            if low[j] <= sl_px:            # SL primero (conservador)
                pnl = -sl_pct
                exited = True
            elif high[j] >= tp_px:
                pnl = tp_pct
                exited = True
        else:
            if high[j] >= sl_px:
                pnl = -sl_pct
                exited = True
            elif low[j] <= tp_px:
                pnl = tp_pct
                exited = True

        if not exited:
            # cierre por cruce contrario (a precio de cierre de la vela)
            opp = (side == "long" and sign[j] < 0) or (side == "short" and sign[j] > 0)
            if opp:
                raw = (close[j] - entry) / entry
                pnl = raw if side == "long" else -raw
                exited = True

        if exited:
            net = pnl - FEE_ROUNDTRIP
            trades.append(net)
            position = None
        i += 1

    return trades


def stats_from_trades(trades):
    n = len(trades)
    if n == 0:
        return None
    arr = np.array(trades)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    wr = len(wins) / n
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = abs(losses.mean()) if len(losses) else 0.0
    breakeven_wr = avg_loss / (avg_win + avg_loss) if (avg_win + avg_loss) > 0 else 1.0
    gross_win = wins.sum()
    gross_loss = abs(losses.sum())
    pf = gross_win / gross_loss if gross_loss > 0 else np.inf
    expectancy = arr.mean()
    equity = np.cumsum(arr)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak)
    max_dd = dd.min() if len(dd) else 0.0
    total_return = equity[-1] if len(equity) else 0.0
    return {
        "trades": n, "wr": wr, "breakeven_wr": breakeven_wr,
        "expectancy": expectancy, "pf": pf, "max_dd": max_dd,
        "total_return": total_return,
    }


def main():
    inst = get_farthest_future_instrument()
    inst_id = inst["instId"]

    data_by_tf = {}
    for bar in TIMEFRAMES:
        data_by_tf[bar] = fetch_history_candles(inst_id, bar)

    results = []
    for bar, ema_fast, ema_slow, (sl, tp) in product(
        TIMEFRAMES, EMA_FAST_SET, EMA_SLOW_SET, SLTP_SET
    ):
        if ema_fast >= ema_slow:
            continue
        df = data_by_tf[bar]
        trades = run_backtest(df, ema_fast, ema_slow, sl, tp)
        st = stats_from_trades(trades)
        if st is None:
            continue
        results.append({
            "tf": bar, "ema": f"{ema_fast}/{ema_slow}", "sl_tp": f"{sl*100:.1f}/{tp*100:.1f}",
            **st,
        })

    df_res = pd.DataFrame(results)
    df_res = df_res.sort_values("expectancy", ascending=False)

    print("\n=== TABLA COMPARATIVA DE GEOMETRÍAS ===")
    print(
        f"{'TF':<4} {'EMA':<8} {'SL/TP%':<10} {'Trades':<7} {'WR%':<7} "
        f"{'BE_WR%':<8} {'Expect/trade':<13} {'PF':<7} {'MaxDD':<9} {'RetTotal':<9}"
    )
    for _, r in df_res.iterrows():
        print(
            f"{r['tf']:<4} {r['ema']:<8} {r['sl_tp']:<10} {r['trades']:<7} "
            f"{r['wr']*100:<7.1f} {r['breakeven_wr']*100:<8.1f} "
            f"{r['expectancy']*100:<13.3f} {r['pf']:<7.2f} "
            f"{r['max_dd']*100:<9.2f} {r['total_return']*100:<9.2f}"
        )

    robust = df_res[
        (df_res["trades"] >= 15)
        & (df_res["wr"] > df_res["breakeven_wr"])
        & (df_res["pf"] > 1.5)
        & (df_res["expectancy"] > 0)
    ]

    print("\n=== RECOMENDACIÓN ===")
    if len(robust) > 0:
        best = robust.sort_values("expectancy", ascending=False).iloc[0]
        print(
            f"Geometría robusta recomendada: TF={best['tf']} EMA={best['ema']} "
            f"SL/TP={best['sl_tp']}% | trades={best['trades']} WR={best['wr']*100:.1f}% "
            f"(BE={best['breakeven_wr']*100:.1f}%) PF={best['pf']:.2f} "
            f"expectativa/trade={best['expectancy']*100:.3f}% "
            f"maxDD={best['max_dd']*100:.2f}% retorno_total={best['total_return']*100:.2f}%"
        )
        print(
            "Justificación: cumple los 4 criterios de robustez (>=15 trades, "
            "WR>breakeven, PF>1.5, expectativa neta>0). Actualizar main-avax.py "
            "con estos parámetros tras revisar esta salida en Actions."
        )
    else:
        print(
            "NINGUNA geometría de la rejilla cumple los 4 criterios de robustez. "
            "No promover ninguna a producción sin ampliar la rejilla o el histórico. "
            "Revisar la fila con mejor expectativa como referencia, sin desplegarla:"
        )
        if len(df_res) > 0:
            top = df_res.iloc[0]
            print(
                f"  Mejor candidata (no validada): TF={top['tf']} EMA={top['ema']} "
                f"SL/TP={top['sl_tp']}% expectativa/trade={top['expectancy']*100:.3f}%"
            )


if __name__ == "__main__":
    main()
