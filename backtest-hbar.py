"""
Backtest EMAs (3, 4, 10, 21, 27) — HBAR-USDC en OKX (EEE / MiCA)
Timeframes: 30m, 1H, 4H | Reposición: 50 EUR por operación
Verificación automática de tamaño mínimo de orden (minSz / lotSz)
Requisitos: pip install requests pandas
"""

import requests
import time
import math
import pandas as pd

# ---------------- CONFIGURACIÓN ----------------
INST_ID = "HBAR-USDC"           # USDT vetado en EEE → USDC
TIMEFRAMES = ["30m", "1H", "4H"]
TOTAL_VELAS = 3000
CAPITAL_OPERACION = 50.0        # EUR por reposición
COMISION = 0.001                # ~0.1% fee spot OKX
EMAS = [3, 4, 10, 21, 27]

# ---------------- SPECS DEL CONTRATO ----------------
def obtener_specs(inst_id):
    """Consulta minSz, lotSz y tickSz del par en OKX."""
    url = "https://www.okx.com/api/v5/public/instruments"
    r = requests.get(url, params={"instType": "SPOT", "instId": inst_id}, timeout=10)
    d = r.json()["data"]
    if not d:  # fallback si no existe el par USDC
        raise ValueError(f"Par {inst_id} no disponible en OKX")
    d = d[0]
    return {
        "minSz":  float(d["minSz"]),   # tamaño mínimo en HBAR
        "lotSz":  float(d["lotSz"]),   # incremento de cantidad
        "tickSz": float(d["tickSz"]),  # incremento de precio
    }

# ---------------- DATOS ----------------
def descargar_velas(inst_id, bar, total):
    url = "https://www.okx.com/api/v5/market/history-candles"
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
        time.sleep(0.15)
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
    df["venta"] = ~alcista & alcista.shift(1, fill_value=False)
    return df

# ---------------- BACKTEST ----------------
def backtest(df, specs):
    min_sz, lot_sz = specs["minSz"], specs["lotSz"]
    hbar, entrada, trades, equity = 0.0, None, [], []
    compras_descartadas = 0

    for _, f in df.iterrows():
        precio = f["close"]
        if f["venta"] and hbar > 0:
            eur = hbar * precio * (1 - COMISION)
            trades.append({
                "entrada": entrada["ts"], "salida": f["ts"],
                "p_entrada": round(entrada["precio"], 5),
                "p_salida": round(precio, 5),
                "hbar": round(entrada["hbar"], 2),
                "pnl_eur": round(eur - CAPITAL_OPERACION, 2),
                "pnl_%": round((eur / CAPITAL_OPERACION - 1) * 100, 2)})
            hbar, entrada = 0.0, None
        if f["compra"] and hbar == 0:
            # Cantidad ajustada al lote y validada contra el mínimo
            qty = math.floor((CAPITAL_OPERACION * (1 - COMISION) / precio) / lot_sz) * lot_sz
            if qty >= min_sz:
                hbar = qty
                entrada = {"ts": f["ts"], "precio": precio, "hbar": qty}
            else:
                compras_descartadas += 1  # 50€ no alcanzan el mínimo
        equity.append({"ts": f["ts"], "eq": hbar * precio})
    return trades, pd.DataFrame(equity).set_index("ts"), compras_descartadas

# ---------------- MAIN ----------------
def main():
    print("Consultando specs del contrato en OKX...")
    specs = obtener_specs(INST_ID)
    print(f"  Par     : {INST_ID}")
    print(f"  minSz   : {specs['minSz']} HBAR (mínimo por orden)")
    print(f"  lotSz   : {specs['lotSz']} HBAR (incremento)")
    print(f"  Aprox   : 50 EUR ≈ {CAPITAL_OPERACION / 0.20:.0f} HBAR a $0.20")

    for bar in TIMEFRAMES:
        df = generar_senales(descargar_velas(INST_ID, bar, TOTAL_VELAS))
        trades, eq, descartadas = backtest(df, specs)
        abierta = eq["eq"].iloc[-1]
        pnl = sum(t["pnl_eur"] for t in trades)
        wins = sum(1 for t in trades if t["pnl_eur"] > 0)
        bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        dd_max = ((eq["eq"].cummax() - eq["eq"]) / eq["eq"].cummax()).max() * 100

        print(f"\n{'='*55}\n {INST_ID} | {bar}\n{'='*55}")
        print(f" Periodo          : {df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}")
        print(f" Operaciones      : {len(trades)}")
        print(f" Compras descart. : {descartadas} (bajo minSz)")
        print(f" Win rate         : {wins/len(trades)*100 if trades else 0:.1f} %")
        print(f" PnL cerrado      : {pnl:.2f} EUR")
        print(f" Posición abierta : {abierta:.2f} EUR")
        print(f" Retorno total    : {pnl + abierta - CAPITAL_OPERACION:.2f} EUR")
        print(f" Buy & Hold       : {bh:.2f} %")
        print(f" Max drawdown     : {dd_max:.2f} %")
        if trades:
            print("\n Últimos trades:")
            print(pd.DataFrame(trades).tail(10).to_string(index=False))

if __name__ == "__main__":
    main()
