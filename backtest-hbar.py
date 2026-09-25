"""
=====================================================
 Backtest EMAs (3,4,10,21,27) — HBAR en OKX (EEE/MiCA)
 Lee config.yml | Timeframes 30m/1H/4H | 50 EUR/operación
 Consulta minSz/lotSz del contrato antes de operar
 Requisitos: pip install requests pandas pyyaml
=====================================================
"""

import requests
import time
import math
import os
import yaml
import pandas as pd

# ---------------- CARGA DE CONFIGURACIÓN ----------------
with open("config.yml", "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

API            = CFG["exchange"]["url_api"]
INST_ID        = CFG["par"]["inst_id"]
FALLBACK       = CFG["par"]["fallback"]
TIMEFRAMES     = CFG["backtest"]["timeframes"]
TOTAL_VELAS    = CFG["backtest"]["velas"]
CAPITAL        = CFG["backtest"]["capital_por_operacion"]
COMISION       = CFG["backtest"]["comision"]
EMAS           = CFG["estrategia"]["emas"]
EXPORTAR       = CFG["salida"]["exportar_csv"]
CARPETA        = CFG["salida"]["carpeta"]
SL             = CFG["riesgo"]["stop_loss_pct"]
TP             = CFG["riesgo"]["take_profit_pct"]

# ---------------- SPECS DEL CONTRATO ----------------
def obtener_specs(inst_id):
    """Consulta minSz, lotSz y tickSz del par. Devuelve None si no existe."""
    r = requests.get(f"{API}/api/v5/public/instruments",
                     params={"instType": "SPOT", "instId": inst_id}, timeout=10)
    d = r.json().get("data", [])
    if not d:
        return None
    d = d[0]
    return {"minSz": float(d["minSz"]), "lotSz": float(d["lotSz"]),
            "tickSz": float(d["tickSz"])}

# ---------------- DESCARGA DE VELAS ----------------
def descargar_velas(inst_id, bar, total):
    url = f"{API}/api/v5/market/history-candles"
    velas, after = [], None
    while len(velas) < total:
        params = {"instId": inst_id, "bar": bar, "limit": 300}
        if after:
            params["after"] = after
        r = requests.get(url, params=params, timeout=10)
        datos = r.json()
        if datos.get("code") != "0" or not datos.get("data"):
            break
        lote = datos["data"]
        velas += lote
        after = lote[-1][0]
        time.sleep(0.15)                      # rate limit OKX
    df = pd.DataFrame(velas, columns=[
        "ts", "open", "high", "low", "close", "vol", "vccy", "vquote", "confirm"])
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
    df[["open", "high", "low", "close", "vol"]] = df[
        ["open", "high", "low", "close", "vol"]].astype(float)
    return df.sort_values("ts").reset_index(drop=True)[
        ["ts", "open", "high", "low", "close", "vol"]]

# ---------------- SEÑALES ----------------
def generar_senales(df):
    for p in EMAS:
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    alcista = (
        (df["ema3"] > df["ema4"]) & (df["ema4"] > df["ema10"]) &
        (df["ema10"] > df["ema21"]) & (df["ema21"] > df["ema27"])
    )
    df["compra"] = alcista & ~alcista.shift(1, fill_value=False)
    df["venta"]  = ~alcista & alcista.shift(1, fill_value=False)
    return df

# ---------------- BACKTEST ----------------
def backtest(df, specs):
    min_sz, lot_sz = specs["minSz"], specs["lotSz"]
    hbar, entrada, trades, equity = 0.0, None, [], []
    descartadas = 0
    sl_precio = tp_precio = None

    for _, f in df.iterrows():
        precio = f["close"]

        # ---- cierres por SL/TP (intravela) ----
        if hbar > 0 and sl_precio and f["low"] <= sl_precio:
            eur = hbar * sl_precio * (1 - COMISION)
            trades.append(_trade(entrada, sl_precio, f["ts"], eur, "SL"))
            hbar, entrada, sl_precio, tp_precio = 0.0, None, None, None
        elif hbar > 0 and tp_precio and f["high"] >= tp_precio:
            eur = hbar * tp_precio * (1 - COMISION)
            trades.append(_trade(entrada, tp_precio, f["ts"], eur, "TP"))
            hbar, entrada, sl_precio, tp_precio = 0.0, None, None, None

        # ---- cierre por rotura de stack ----
        if f["venta"] and hbar > 0:
            eur = hbar * precio * (1 - COMISION)
            trades.append(_trade(entrada, precio, f["ts"], eur, "señal"))
            hbar, entrada, sl_precio, tp_precio = 0.0, None, None, None

        # ---- apertura con 50 EUR frescos ----
        if f["compra"] and hbar == 0:
            qty = math.floor((CAPITAL * (1 - COMISION) / precio) / lot_sz) * lot_sz
            if qty >= min_sz:
                hbar = qty
                entrada = {"ts": f["ts"], "precio": precio, "hbar": qty}
                sl_precio = precio * (1 - SL/100) if SL else None
                tp_precio = precio * (1 + TP/100) if TP else None
            else:
                descartadas += 1          # 50 EUR no alcanzan el minSz

        equity.append({"ts": f["ts"], "eq": hbar * precio})

    return trades, pd.DataFrame(equity).set_index("ts"), descartadas

def _trade(entrada, salida_precio, salida_ts, eur, motivo):
    return {
        "entrada": entrada["ts"], "salida": salida_ts,
        "p_entrada": round(entrada["precio"], 5),
        "p_salida": round(salida_precio, 5),
        "hbar": round(entrada["hbar"], 2),
        "pnl_eur": round(eur - CAPITAL, 2),
        "pnl_%": round((eur / CAPITAL - 1) * 100, 2),
        "motivo": motivo}

# ---------------- INFORME ----------------
def informe(inst_id, bar, df, trades, eq, descartadas):
    abierta = eq["eq"].iloc[-1]
    pnl = sum(t["pnl_eur"] for t in trades)
    wins = sum(1 for t in trades if t["pnl_eur"] > 0)
    bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    dd = ((eq["eq"].cummax() - eq["eq"]) / eq["eq"].cummax()).max() * 100

    print(f"\n{'='*60}\n {inst_id} | {bar}\n{'='*60}")
    print(f" Periodo          : {df['ts'].iloc[0]}  ->  {df['ts'].iloc[-1]}")
    print(f" Operaciones      : {len(trades)}")
    print(f" Compras descart. : {descartadas} (por debajo de minSz)")
    print(f" Win rate         : {wins/len(trades)*100 if trades else 0:.1f} %")
    print(f" PnL cerrado      : {pnl:.2f} EUR")
    print(f" Posición abierta : {abierta:.2f} EUR")
    print(f" Retorno total    : {pnl + abierta - CAPITAL:.2f} EUR")
    print(f" Buy & Hold       : {bh:.2f} %")
    print(f" Max drawdown     : {dd:.2f} %")
    if trades:
        tdf = pd.DataFrame(trades)
        print(f" Mejor trade      : {tdf['pnl_%'].max():+.2f} %")
        print(f" Peor trade       : {tdf['pnl_%'].min():+.2f} %")
        return tdf, eq.reset_index()
    return pd.DataFrame(), eq.reset_index()

# ---------------- MAIN ----------------
def main():
    # 1) Specs del contrato (con fallback)
    print("Consultando specs del contrato en OKX...")
    specs = obtener_specs(INST_ID)
    par = INST_ID
    if specs is None and FALLBACK:
        print(f"  {INST_ID} no disponible, probando {FALLBACK}...")
        specs, par = obtener_specs(FALLBACK), FALLBACK
    if specs is None:
        raise SystemExit(f"Ni {INST_ID} ni {FALLBACK} existen en OKX spot.")

    print(f"  Par   : {par}")
    print(f"  minSz : {specs['minSz']}  (mínimo por orden)")
    print(f"  lotSz : {specs['lotSz']}  (incremento de cantidad)")

    if EXPORTAR:
        os.makedirs(CARPETA, exist_ok=True)

    # 2) Backtest por timeframe
    for bar in TIMEFRAMES:
        df = generar_senales(descargar_velas(par, bar, TOTAL_VELAS))
        trades, eq, descartadas = backtest(df, specs)
        tdf, eqdf = informe(par, bar, df, trades, eq, descartadas)

        if EXPORTAR:
            suf = bar.replace("m", "min").replace("H", "h")
            tdf.to_csv(f"{CARPETA}/trades_{par}_{suf}.csv", index=False)
            eqdf.to_csv(f"{CARPETA}/equity_{par}_{suf}.csv", index=False)
            print(f" Guardado en {CARPETA}/trades_{par}_{suf}.csv")

    print(f"\nConfiguración: 50 EUR por reposición | EMAs {EMAS} | "
          f"fee {COMISION*100:.2f}% | SL={SL} TP={TP}")

if __name__ == "__main__":
    main()
