# btc-radar.py — RADAR DEL PORTAAVIONES · BTC cartografía de posición y régimen
#   DECRETO CARBONO (2026-10-12): "saber por donde anda ese portaaviones"
#   El mástil BTC decide la estela de toda la flota — esto lo mapea.
#
#   QUÉ ENTREGA (solo lectura, sin secrets, sin ordenes):
#     - posición: precio actual vs mástiles de referencia (80k/84k EUR-equivalente
#       en USD, mínimos/máximos de la semana)
#     - régimen por marco: 1H y 4H — tendencia, EMAs 3/21, separación, rango
#     - el estado del rango 80k-84k: dentro/roto arriba/roto abajo
#   Ax3: velas CERRADAS · fail-closed en descarga · sin opinion
#
#   USO: workflow_dispatch + push · run manual cuando el Carbono quiera mapa

import sys
import time

import numpy as np
import pandas as pd
import requests

BASE_URL = "https://www.okx.com"
UNDERLYING = "BTC-USD"
BAR_MS = {"1H": 3600000, "4H": 14400000}
MIN_CANDLES = {"1H": 500, "4H": 300}
# Mástiles de referencia del Carbono (EUR aprox sobre feed BTCUSD — orientativo)
# El radar reporta USD reales; la lectura EUR la hace el Carbono con su feed.
RANGE_NOTE = "Rango decreta el Carbono: mástil 84k→80k EUR observado en feed EUR"


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
        print("[ERROR] Sin FUTURES no-USDT para BTC. DETENIDO.")
        sys.exit(1)
    cands.sort(key=lambda d: int(d["expTime"]), reverse=True)
    inst = cands[0]
    print("[INSTRUMENT] " + inst["instId"] +
          " | ctVal=" + str(inst.get("ctVal")) + " " + str(inst.get("ctValCcy")) +
          " | XPERP mas lejano")
    return inst


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
    print("[CANDLES] " + bar + ": " + str(len(df)) + " velas cerradas")
    return df


def regime_block(df, label):
    c = df["c"]
    h = df["h"]
    l = df["l"]
    e3 = c.ewm(span=3, adjust=False).mean()
    e21 = c.ewm(span=21, adjust=False).mean()
    px = c.iloc[-1]
    hi = h.max()
    lo = l.min()
    pos_in_range = (px - lo) / (hi - lo) * 100 if hi > lo else 50.0
    trend = "ALCISTA" if e3.iloc[-1] > e21.iloc[-1] else "BAJISTA"
    sep = abs(e3.iloc[-1] - e21.iloc[-1]) / e21.iloc[-1] * 100
    # pendiente del régimen: precio ahora vs hace 24 velas
    back = min(24, len(c) - 1)
    slope = (px - c.iloc[-1 - back]) / c.iloc[-1 - back] * 100
    print("")
    print("--- MARCO " + label + " ---")
    print("  Precio:        " + format(px, '.1f'))
    print("  EMA3/21:       " + format(e3.iloc[-1], '.1f') + " / " +
          format(e21.iloc[-1], '.1f') + " | " + trend +
          " | sep " + format(sep, '.2f') + "%")
    print("  Rango ventana: " + format(lo, '.1f') + " — " + format(hi, '.1f') +
          " | posicion " + format(pos_in_range, '.0f') + "% del rango")
    print("  Pendiente 24v: " + format(slope, '+.2f') + "%")
    return px, hi, lo


def main():
    print("RADAR DEL PORTAAVIONES — BTC (XPERP OKX, USD) · solo lectura")
    inst = get_instrument()
    data = {}
    for bar in ["1H", "4H"]:
        data[bar] = fetch_candles(inst["instId"], bar, MIN_CANDLES[bar])

    px_1h, hi_1h, lo_1h = regime_block(data["1H"], "1H")
    px_4h, hi_4h, lo_4h = regime_block(data["4H"], "4H")

    # Mástiles de 7 días (aprox) en 4H
    week = data["4H"].iloc[-42:]   # 42 x 4H = 7 dias
    w_hi = week["h"].max()
    w_lo = week["l"].min()

    print("")
    print("=== POSICION vs MASTILES DE LA SEMANA (USD) ===")
    print("  Maximo 7d: " + format(w_hi, '.1f') + " | Minimo 7d: " + format(w_lo, '.1f'))
    mid = (w_hi + w_lo) / 2
    if px_4h > w_hi * 0.995:
        pos = "ROTO ARRIBA del maximo 7d"
    elif px_4h < w_lo * 1.005:
        pos = "ROTO ABAJO del minimo 7d"
    else:
        pos = "DENTRO del rango 7d (" + ("mitad superior" if px_4h > mid else "mitad inferior") + ")"
    print("  Estado: " + pos)
    print("  " + RANGE_NOTE)

    print("")
    print("=== SINTESIS PARA LA FLOTA ===")
    if pos.startswith("ROTO ARRIBA"):
        print("  Portaaviones rompe al alza: estela alcista para altcoins.")
        print("  Leyes largas favorablemente situadas; candados abriran de golpe.")
    elif pos.startswith("ROTO ABAJO"):
        print("  Portaaviones rompe a la baja: estela bajista — LA MAS PELIGROSA.")
        print("  SLs atomicos en guardia; esperables cierres y cruces bajistas.")
    else:
        print("  Portaaviones EN RANGO: altcoins sin estela — sierras y trenzadas.")
        print("  Candados cerrados probablemente correctos; paciencia.")

    print("")
    print("ADVERTENCIA: mapa de posicion, no prediccion. El Carbono decide.")


if __name__ == "__main__":
    main()
