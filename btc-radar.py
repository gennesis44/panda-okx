# btc-radar.py v2 — RADAR DEL PORTAAVIONES · BTC-USD-SWAP (perpetuo, historia profunda)
#   CAMBIO v2 (decreto Carbono): el XPERP largo solo tiene ~2 meses de velas —
#   el radar se apunta al PERPETUO BTC-USD-SWAP de OKX (settle USD, anos de
#   historia) para cartografiar mástiles y regímenes reales de meses.
#   Solo lectura · sin secrets · sin órdenes · el veto USDT es para OPERAR.
#
#   QUÉ ENTREGA:
#     - régimen 1H y 4H: EMAs 3/21, tendencia, separación, pendiente 24v
#     - mástiles: máximos/mínimos de 7d, 30d y 90d + posición del precio
#     - síntesis naval para la flota

import sys
import time

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://www.okx.com"
INST_ID = "BTC-USD-SWAP"          # perpetuo · settle USD · historia profunda
BAR_MS = {"1H": 3600000, "4H": 14400000}
MIN_CANDLES = {"1H": 720, "4H": 540}   # 30d en 1H · 90d en 4H


def fetch_candles(inst_id, bar, min_count):
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
        na = rows[-1][0]
        if after and na >= after:
            break
        after = na
        time.sleep(0.15)
    if not all_rows:
        print("[ERROR] Sin velas " + bar + ". DETENIDO.")
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
        print("[ERROR] Descarga incompleta " + bar + ": " + str(len(df)) +
              "/" + str(min_count) + ". DETENIDO (fail-closed).")
        sys.exit(1)
    print("[CANDLES] " + bar + ": " + str(len(df)) + " velas cerradas (~" +
          format(len(df) * BAR_MS[bar] / 86400000.0, '.0f') + " dias)")
    return df


def regime_block(df, label):
    c = df["c"]
    h = df["h"]
    l = df["l"]
    e3 = c.ewm(span=3, adjust=False).mean()
    e21 = c.ewm(span=21, adjust=False).mean()
    px = c.iloc[-1]
    trend = "ALCISTA" if e3.iloc[-1] > e21.iloc[-1] else "BAJISTA"
    sep = abs(e3.iloc[-1] - e21.iloc[-1]) / e21.iloc[-1] * 100
    back = min(24, len(c) - 1)
    slope = (px - c.iloc[-1 - back]) / c.iloc[-1 - back] * 100
    print("")
    print("--- MARCO " + label + " ---")
    print("  Precio:        " + format(px, '.1f'))
    print("  EMA3/21:       " + format(e3.iloc[-1], '.1f') + " / " +
          format(e21.iloc[-1], '.1f') + " | " + trend +
          " | sep " + format(sep, '.2f') + "%")
    print("  Pendiente 24v: " + format(slope, '+.2f') + "%")
    return px


def mast_block(df, label, days):
    h = df["h"]
    l = df["l"]
    c = df["c"]
    win = df.iloc[-min(len(df), days * (24 if label == "1H" else 6)):]
    hi = win["h"].max()
    lo = win["l"].min()
    px = c.iloc[-1]
    print("  Mastil " + str(days) + "d: max " + format(hi, '.1f') +
          " | min " + format(lo, '.1f') + " | precio " +
          format(px, '.1f') + " (" +
          format((px - lo) / (hi - lo) * 100, '.0f') + "% del rango)")
    return hi, lo


def main():
    print("RADAR DEL PORTAAVIONES v2 — " + INST_ID + " (perpetuo USD) · solo lectura")
    data = {}
    for bar in ["1H", "4H"]:
        data[bar] = fetch_candles(INST_ID, bar, MIN_CANDLES[bar])

    px_1h = regime_block(data["1H"], "1H")
    px_4h = regime_block(data["4H"], "4H")

    print("")
    print("=== MASTILES (USD) ===")
    hi7, lo7 = mast_block(data["1H"], "1H", 7)
    hi30, lo30 = mast_block(data["1H"], "1H", 30)
    hi90, lo90 = mast_block(data["4H"], "4H", 90)

    print("")
    print("=== POSICION vs MASTILES ===")
    for name, hi, lo in [("7d", hi7, lo7), ("30d", hi30, lo30), ("90d", hi90, lo90)]:
        mid = (hi + lo) / 2
        if px_4h > hi * 0.995:
            st = "ROTO ARRIBA"
        elif px_4h < lo * 1.005:
            st = "ROTO ABAJO"
        else:
            st = "DENTRO (" + ("mitad sup" if px_4h > mid else "mitad inf") + ")"
        print("  " + name + ": " + st + " | max " + format(hi, '.0f') +
              " / min " + format(lo, '.0f'))

    print("")
    print("=== SINTESIS NAVAL ===")
    broke_up = px_4h > hi7 * 0.995
    broke_dn = px_4h < lo7 * 1.005
    if broke_up:
        print("  Portaaviones ROMPE ARRIBA del maximo 7d: estela alcista para la flota.")
    elif broke_dn:
        print("  Portaaviones ROMPE ABAJO del minimo 7d: estela bajista — LA MAS PELIGROSA.")
        print("  SLs atomicos en guardia; cierres y cruces bajistas esperables.")
    else:
        print("  Portaaviones EN RANGO 7d: sin estela clara — sierras/trenzadas en altcoins.")
        print("  Candados cerrados probablemente correctos; paciencia.")
    print("  Contexto 30d/90d arriba: el rango semanal dentro de la estructura mayor.")
    print("")
    print("ADVERTENCIA: mapa de posicion, no prediccion. El Carbono decide.")


if __name__ == "__main__":
    main()
