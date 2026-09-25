"""
=====================================================
 Backtest EMAs (3,4,10,21,27) — HBAR en OKX (EEE/MiCA)
 Versión robusta: explica en español cada error posible
 Requisitos:  pip install requests pandas pyyaml
=====================================================
"""

import os
import sys
import time
import math

# ---------- 1) Comprobar dependencias ----------
try:
    import requests
    import yaml
    import pandas as pd
except ImportError as e:
    sys.exit(f"[ERROR] Falta la librería '{e.name}'.\n"
             "        Solución:  pip install requests pandas pyyaml")

# ---------- 2) Cargar config.yml desde la carpeta del script ----------
BASE     = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.yml")

if not os.path.exists(CFG_PATH):
    sys.exit(f"[ERROR] No encuentro config.yml en:\n        {BASE}\n"
             "        Debe estar en la MISMA carpeta que backtest_hbar.py\n"
             "        (en Windows puede llamarse config.yml.txt sin que lo veas)")

with open(CFG_PATH, encoding="utf-8-sig") as f:
    texto = f.read()

if "\t" in texto:
    sys.exit("[ERROR] config.yml contiene tabuladores. YAML solo admite espacios.")

try:
    CFG = yaml.safe_load(texto)
except yaml.YAMLError as e:
    sys.exit(f"[ERROR] config.yml mal formateado:\n{e}")

API        = CFG["exchange"]["url_api"].rstrip("/")
BASE_ASSET = CFG["par"]["base"].upper()
QUOTE_PREF = CFG["par"]["quote_preferido"].upper()
TIMEFRAMES = CFG["backtest"]["timeframes"]
TOTAL      = int(CFG["backtest"]["velas"])
CAPITAL    = float(CFG["backtest"]["capital_por_operacion"])
COMISION   = float(CFG["backtest"]["comision"])
EMAS       = CFG["estrategia"]["emas"]
EXPORTAR   = bool(CFG["salida"]["exportar_csv"])
CARPETA    = CFG["salida"]["carpeta"]
SL         = CFG["riesgo"]["stop_loss_pct"]
TP         = CFG["riesgo"]["take_profit_pct"]

# ---------- 3) HTTP con reintentos y errores explicados ----------
def peticion(url, params=None):
    for intento in range(3):
        try:
            r = requests.get(url, params=params, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 403:
                print("[ERROR] OKX devolvio 403: acceso bloqueado "
                      "(region/red/VPN/firewall).")
                return None
            if r.status_code == 429:
                time.sleep(2)
                continue
            if r.status_code != 200:
                time.sleep(1)
                continue
            return r.json()
        except requests.exceptions.RequestException as e:
            print(f"[AVISO] Error de red ({e.__class__.__name__}), reintento...")
            time.sleep(1 + intento)
        except ValueError:
            print("[ERROR] La respuesta no es JSON (¿proxy?).")
            return None
    return None

# ---------- 4) Buscar par HBAR disponible en OKX spot ----------
def buscar_par():
    datos = peticion(f"{API}/api/v5/public/instruments", {"instType": "SPOT"})
    if not datos or datos.get("code") != "0":
        sys.exit("[ERROR] No se pudo consultar la lista de instrumentos de OKX.")
    disponibles = [d["instId"] for d in datos["data"]
                   if d["instId"].split("-")[0] == BASE_ASSET]
    if not disponibles:
        sys.exit(f"[ERROR] OKX no lista ningun par {BASE_ASSET} en spot.")
    preferido = f"{BASE_ASSET}-{QUOTE_PREF}"
    if preferido in disponibles:
        return preferido
    print(f"[AVISO] {preferido} NO existe en OKX spot.")
    print(f"        Pares encontrados: {', '.join(disponibles)}")
    print(f"        Se usa {disponibles[0]} (precios casi identicos).")
    return disponibles[0]

def obtener_specs(inst_id):
    datos = peticion(f"{API}/api/v5/public/instruments",
                     {"instType": "SPOT", "instId": inst_id})
    if not datos or datos.get("code") != "0" or not datos.get("data"):
        return None
    d = datos["data"][0]
    return {"minSz": float(d["minSz"]), "lotSz": float(d["lotSz"])}

# ---------- 5) Velas (limit max 100 en history-candles) ----------
def descargar_velas(inst_id, bar, total):
    url = f"{API}/api/v5/market/history-candles"
    velas, after = [], None
    while len(velas) < total:
        params = {"instId": inst_id, "bar": bar, "limit": 100}
        if after:
            params["after"] = after
        datos = peticion(url, params)
        if not datos or datos.get("code") != "0":
            msg = datos.get("msg") if datos else "sin respuesta"
            print(f"[ERROR] Velas {bar}: {msg}")
            return None
        lote = datos["data"]
        if not lote:
            break
        velas += lote
        after = lote[-1][0]
        time.sleep(0.25)
    if len(velas) < 300:
        print(f"[ERROR] Solo {len(velas)} velas para {bar}. Insuficiente.")
        return None
    df = pd.DataFrame(velas, columns=["ts", "open", "high", "low", "close",
                                      "vol", "vccy", "vquote", "confirm"])
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)
    return df.sort_values("ts").reset_index(drop=True)[
        ["ts", "open", "high", "low", "close", "vol"]]

# ---------- 6) Señales ----------
def generar_senales(df):
    for p in EMAS:
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
    alcista = ((df["ema3"] > df["ema4"]) & (df["ema4"] > df["ema10"]) &
               (df["ema10"] > df["ema21"]) & (df["ema21"] > df["ema27"]))
    df["compra"] = alcista & ~alcista.shift(1, fill_value=False)
    df["venta"]  = ~alcista & alcista.shift(1, fill_value=False)
    return df

# ---------- 7) Backtest ----------
def _trade(entrada, p_sal, ts_sal, eur, motivo):
    return {"entrada": entrada["ts"], "salida": ts_sal,
            "p_entrada": round(entrada["precio"], 5),
            "p_salida": round(p_sal, 5),
            "hbar": round(entrada["hbar"], 2),
            "pnl_eur": round(eur - CAPITAL, 2),
            "pnl_%": round((eur / CAPITAL - 1) * 100, 2),
            "motivo": motivo}

def backtest(df, specs):
    min_sz, lot_sz = specs["minSz"], specs["lotSz"]
    hbar, entrada, trades, equity = 0.0, None, [], []
    descartadas = sl_p = tp_p = None, None, None, 0
    descartadas = 0

    for _, f in df.iterrows():
        precio = f["close"]

        if hbar > 0 and sl_p and f["low"] <= sl_p:
            eur = hbar * sl_p * (1 - COMISION)
            trades.append(_trade(entrada, sl_p, f["ts"], eur, "SL"))
            hbar, entrada, sl_p, tp_p = 0.0, None, None, None
        elif hbar > 0 and tp_p and f["high"] >= tp_p:
            eur = hbar * tp_p * (1 - COMISION)
            trades.append(_trade(entrada, tp_p, f["ts"], eur, "TP"))
            hbar, entrada, sl_p, tp_p = 0.0, None, None, None

        if f["venta"] and hbar > 0:
            eur = hbar * precio * (1 - COMISION)
            trades.append(_trade(entrada, precio, f["ts"], eur, "senal"))
            hbar, entrada, sl_p, tp_p = 0.0, None, None, None

        if f["compra"] and hbar == 0:
            qty = math.floor((CAPITAL * (1 - COMISION) / precio) / lot_sz) * lot_sz
            if qty >= min_sz:
                hbar = qty
                entrada = {"ts": f["ts"], "precio": precio, "hbar": qty}
                sl_p = precio * (1 - SL / 100) if SL else None
                tp_p = precio * (1 + TP / 100) if TP else None
            else:
                descartadas += 1

        equity.append({"ts": f["ts"], "eq": hbar * precio})

    return trades, pd.DataFrame(equity).set_index("ts"), descartadas

# ---------- 8) Informe ----------
def informe(par, bar, df, trades, eq, descartadas):
    pnl = sum(t["pnl_eur"] for t in trades)
    wins = sum(1 for t in trades if t["pnl_eur"] > 0)
    abierta = eq["eq"].iloc[-1]
    bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    dd = ((eq["eq"].cummax() - eq["eq"]) / eq["eq"].cummax()).max() * 100

    print(f"\n{'='*60}\n {par} | {bar}\n{'='*60}")
    print(f" Periodo          : {df['ts'].iloc[0]}  ->  {df['ts'].iloc[-1]}")
    print(f" Operaciones      : {len(trades)}")
    print(f" Compras descart. : {descartadas} (bajo minSz)")
    print(f" Win rate         : {wins/len(trades)*100 if trades else 0:.1f} %")
    print(f" PnL cerrado      : {pnl:.2f} EUR")
    print(f" Posicion abierta : {abierta:.2f} EUR")
    print(f" Retorno total    : {pnl + abierta - CAPITAL:.2f} EUR")
    print(f" Buy & Hold       : {bh:.2f} %")
    print(f" Max drawdown     : {dd:.2f} %")
    tdf = pd.DataFrame(trades)
    if not tdf.empty:
        print(f" Mejor trade      : {tdf['pnl_%'].max():+.2f} %  |  "
              f"Peor: {tdf['pnl_%'].min():+.2f} %")
    return tdf, eq.reset_index()

# ---------- 9) MAIN ----------
def main():
    print("=" * 60)
    print(f" Backtest EMA {EMAS} | capital {CAPITAL} EUR/operacion | OKX")
    print("=" * 60)

    par = buscar_par()
    specs = obtener_specs(par)
    if specs is None:
        sys.exit(f"[ERROR] No pude leer minSz/lotSz de {par}.")
    print(f" Par   : {par}")
    print(f" minSz : {specs['minSz']}   lotSz : {specs['lotSz']}")

    ruta_out = os.path.join(BASE, CARPETA)
    if EXPORTAR:
        os.makedirs(ruta_out, exist_ok=True)

    for bar in TIMEFRAMES:
        print(f"\nDescargando {par} {bar} ...")
        crudo = descargar_velas(par, bar, TOTAL)
        if crudo is None:
            print(f"[AVISO] Se salta {bar}.")
            continue
        trades, eq, desc = backtest(generar_senales(crudo), specs)
        tdf, eqdf = informe(par, bar, crudo, trades, eq, desc)
        if EXPORTAR:
            suf = bar.replace("m", "min").replace("H", "h")
            tdf.to_csv(os.path.join(ruta_out, f"trades_{par}_{suf}.csv"),
                       index=False)
            eqdf.to_csv(os.path.join(ruta_out, f"equity_{par}_{suf}.csv"),
                        index=False)
            print(f" CSV guardados en {ruta_out}")

    print("\nFin.")

if __name__ == "__main__":
    main()
