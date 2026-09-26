"""
=====================================================
 Backtest EMAs (3,4,10,21,27) — HBAR-USD Futuros OKX
 my.okx.com (EEE/MiCA) | 30m/1H/4H | 50 EUR/operación
 - Elige vencimiento más lejano (o perpetuo SWAP)
 - Exporta todos los cruces históricos de las EMAs
 - Optimiza SL/TP (rejilla) y recomienda timeframe
 Requisitos: pip install requests pandas pyyaml
=====================================================
"""

import requests, time, math, os, yaml
import pandas as pd

# ---------------- CONFIG (tolerante: funciona con config nuevo o antiguo) ----------------
try:
    with open("config.yml", "r", encoding="utf-8") as f:
        CFG = yaml.safe_load(f) or {}
except FileNotFoundError:
    print("config.yml no encontrado: usando valores por defecto.")
    CFG = {}

def cfg_get(ruta, default=None):
    nodo = CFG
    for clave in ruta.split("."):
        if not isinstance(nodo, dict) or clave not in nodo:
            return default
        nodo = nodo[clave]
    return nodo

API        = cfg_get("exchange.url_api", "https://www.okx.com")
INST_TYPE  = str(cfg_get("mercado.tipo", "SWAP")).upper()
ULY        = cfg_get("mercado.uly", "HBAR-USD")
MAS_LEJANO = bool(cfg_get("mercado.contrato_mas_lejano", False))
TIMEFRAMES = cfg_get("backtest.timeframes", ["30m", "1H", "4H"])
VELAS      = cfg_get("backtest.velas", 1000)
CAPITAL    = cfg_get("backtest.capital_por_operacion", 50)
COMISION   = cfg_get("backtest.comision", 0.0005)
EMAS       = cfg_get("estrategia.emas", [3, 4, 10, 21, 27])
CORTO      = bool(cfg_get("estrategia.permitir_corto", True))
OPT_ACTIVA = bool(cfg_get("optimizacion.activar", True))
SL_GRID    = cfg_get("optimizacion.sl_pct", [1, 2, 3])
TP_GRID    = cfg_get("optimizacion.tp_pct", [2, 4, 6])
EXPORTAR   = bool(cfg_get("salida.exportar_csv", True))
CARPETA    = cfg_get("salida.carpeta", "resultados")
SL_DEF     = cfg_get("riesgo.stop_loss_pct", 2)
TP_DEF     = cfg_get("riesgo.take_profit_pct", 4)

# inst_id: admite config nuevo (mercado.inst_id) o antiguo (par.inst_id estilo spot)
INST_ID = cfg_get("mercado.inst_id", "")
if not INST_ID:
    viejo = cfg_get("par.inst_id", "") or ""
    if viejo.endswith("-USDT"):
        INST_ID = viejo.replace("-USDT", "") + "-USD-SWAP"
    elif viejo.endswith("-USD"):
        INST_ID = viejo + "-SWAP"
    else:
        INST_ID = "HBAR-USD-SWAP"

# ---------------- CONTRATOS Y SPECS ----------------
def listar_futuros(uly):
    r = requests.get(f"{API}/api/v5/public/instruments",
                     params={"instType": "FUTURES", "uly": uly}, timeout=10)
    return r.json().get("data", [])

def obtener_specs(inst_type, inst_id):
    r = requests.get(f"{API}/api/v5/public/instruments",
                     params={"instType": inst_type, "instId": inst_id}, timeout=10)
    d = r.json().get("data", [])
    if not d:
        return None
    d = d[0]
    return {"minSz": float(d["minSz"]), "lotSz": float(d["lotSz"]),
            "tickSz": float(d["tickSz"]), "ctVal": float(d.get("ctVal", 1))}

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
        time.sleep(0.15)
    if not velas:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "vol"])
    df = pd.DataFrame(velas, columns=[
        "ts", "open", "high", "low", "close", "vol", "vccy", "vquote", "confirm"])
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
    df[["open", "high", "low", "close", "vol"]] = df[
        ["open", "high", "low", "close", "vol"]].astype(float)
    return df.sort_values("ts").reset_index(drop=True)[
        ["ts", "open", "high", "low", "close", "vol"]]

# ---------------- SEÑALES Y CRUCES (CORREGIDO) ----------------
def generar_senales(df):
    for p in EMAS:
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()

    # Stack alcista: cada EMA por encima de la siguiente (orden dinámico)
    alcista = pd.Series(True, index=df.index)
    for a, b in zip(EMAS, EMAS[1:]):
        alcista &= df[f"ema{a}"] > df[f"ema{b}"]

    df["compra"] = alcista & ~alcista.shift(1, fill_value=False)
    df["venta"]  = ~alcista & alcista.shift(1, fill_value=False)

    cols = ["ts", "direccion", "close"] + [f"ema{p}" for p in EMAS]
    cruces = df[df["compra"] | df["venta"]].copy()
    if cruces.empty:
        return df, pd.DataFrame(columns=cols)
    cruces["direccion"] = ["alcista" if c else "bajista" for c in cruces["compra"]]
    return df, cruces[cols]

# ---------------- BACKTEST (largos y cortos) ----------------
def backtest(df, specs, sl, tp, permitir_corto):
    ctval, lot_sz, min_sz = specs["ctVal"], specs["lotSz"], specs["minSz"]
    pos, contratos = 0, 0.0
    entrada, sl_p, tp_p = None, None, None
    trades, equity, pnl_cerrado, descartadas = [], [], 0.0, 0

    def abrir(direccion, f):
        nonlocal contratos, pos, entrada, sl_p, tp_p, descartadas
        raw = CAPITAL * (1 - COMISION) / (f["close"] * ctval)
        c = math.floor(raw / lot_sz) * lot_sz
        if c < min_sz:
            descartadas += 1
            return
        contratos = c
        pos = direccion
        entrada = {"ts": f["ts"], "precio": f["close"], "contratos": c}
        sl_p = f["close"] * (1 - sl/100) if sl else None
        tp_p = f["close"] * (1 + tp/100) if tp else None

    def cerrar(precio_sal, ts, motivo):
        nonlocal pos, contratos, entrada, sl_p, tp_p, pnl_cerrado
        ratio = precio_sal / entrada["precio"]
        eur = CAPITAL * (ratio if pos == 1 else 2 - ratio) * (1 - COMISION) ** 2
        pnl = eur - CAPITAL
        pnl_cerrado += pnl
        trades.append({
            "dir": "largo" if pos == 1 else "corto",
            "entrada": entrada["ts"], "salida": ts,
            "p_entrada": round(entrada["precio"], 5),
            "p_salida": round(precio_sal, 5),
            "contratos": round(contratos, 2),
            "pnl_eur": round(pnl, 2),
            "pnl_%": round(pnl / CAPITAL * 100, 2),
            "motivo": motivo})
        pos, entrada, sl_p, tp_p = 0, None, None, None

    for _, f in df.iterrows():
        p = f["close"]

        # SL/TP intravela (SL primero: conservador)
        if pos == 1:
            if sl_p and f["low"] <= sl_p:    cerrar(sl_p, f["ts"], "SL")
            elif tp_p and f["high"] >= tp_p: cerrar(tp_p, f["ts"], "TP")
        elif pos == -1:
            if sl_p and f["high"] >= sl_p:   cerrar(sl_p, f["ts"], "SL")
            elif tp_p and f["low"] <= tp_p:  cerrar(tp_p, f["ts"], "TP")

        # Señales de cruce
        if f["compra"]:
            if pos == -1: cerrar(p, f["ts"], "señal")
            if pos == 0:  abrir(1, f)
        elif f["venta"]:
            if pos == 1:  cerrar(p, f["ts"], "señal")
            if pos == 0 and permitir_corto: abrir(-1, f)

        # Equity marca a mercado
        flot = 0.0
        if pos == 1:    flot = CAPITAL * (p / entrada["precio"] - 1)
        elif pos == -1: flot = CAPITAL * (1 - p / entrada["precio"])
        equity.append({"ts": f["ts"], "eq": CAPITAL + pnl_cerrado + flot})

    return trades, pd.DataFrame(equity).set_index("ts"), descartadas

# ---------------- INFORME ----------------
def informe(par, bar, df, trades, eq, descartadas):
    pnl = sum(t["pnl_eur"] for t in trades)
    wins = sum(1 for t in trades if t["pnl_eur"] > 0)
    flot = eq["eq"].iloc[-1] - CAPITAL - pnl
    dd = ((eq["eq"].cummax() - eq["eq"]) / eq["eq"].cummax()).max() * 100
    bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100

    print(f"\n{'='*62}\n {par} | {bar}\n{'='*62}")
    print(f" Periodo        : {df['ts'].iloc[0]}  ->  {df['ts'].iloc[-1]}")
    print(f" Operaciones    : {len(trades)}   (descartadas minSz: {descartadas})")
    print(f" Win rate       : {wins/len(trades)*100 if trades else 0:.1f} %")
    print(f" PnL cerrado    : {pnl:+.2f} EUR")
    print(f" Abierta        : {flot:+.2f} EUR")
    print(f" Retorno total  : {eq['eq'].iloc[-1] - CAPITAL:+.2f} EUR")
    print(f" Buy & Hold     : {bh:+.2f} %")
    print(f" Max drawdown   : {dd:.2f} %")
    if trades:
        tdf = pd.DataFrame(trades)
        print(f" Mejor trade    : {tdf['pnl_%'].max():+.2f} %   "
              f"Peor: {tdf['pnl_%'].min():+.2f} %")
        return tdf, eq.reset_index()
    return pd.DataFrame(), eq.reset_index()

# ---------------- OPTIMIZADOR SL/TP ----------------
def optimizar(df, specs):
    filas = []
    for sl in SL_GRID:
        for tp in TP_GRID:
            trades, eq, _ = backtest(df, specs, sl, tp, CORTO)
            wins = sum(1 for t in trades if t["pnl_eur"] > 0)
            filas.append({"SL_%": sl, "TP_%": tp,
                          "retorno_eur": round(eq["eq"].iloc[-1] - CAPITAL, 2),
                          "trades": len(trades),
                          "win%": round(wins/len(trades)*100, 1) if trades else 0.0})
    return pd.DataFrame(filas).sort_values("retorno_eur", ascending=False)

# ---------------- MAIN ----------------
def main():
    if EXPORTAR:
        os.makedirs(CARPETA, exist_ok=True)

    # 1) Elegir instrumento
    if INST_TYPE == "FUTURES":
        contratos = listar_futuros(ULY)
        if not contratos:
            raise SystemExit(f"No hay futuros para {ULY} en OKX.")
        print(f"Vencimientos disponibles de {ULY}:")
        for c in sorted(contratos, key=lambda x: int(x["expTime"])):
            print(f"  {c['instId']} -> "
                  f"{pd.to_datetime(int(c['expTime']), unit='ms').date()}")
        ids = {c["instId"] for c in contratos}
        if INST_ID in ids:
            par = INST_ID
        else:
            clave = lambda c: int(c["expTime"])
            par = (max(contratos, key=clave) if MAS_LEJANO
                   else min(contratos, key=clave))["instId"]
    else:
        par = INST_ID or f"{ULY}-SWAP"

    specs = obtener_specs(INST_TYPE, par)
    if specs is None:
        raise SystemExit(f"{par} no existe como {INST_TYPE} en OKX.")

    print(f"\nInstrumento : {par} ({INST_TYPE})")
    print(f"  ctVal     : {specs['ctVal']} HBAR/contrato")
    print(f"  minSz     : {specs['minSz']} contratos | lotSz: {specs['lotSz']}")

    # 2) Backtest por timeframe
    mejores = []
    for bar in TIMEFRAMES:
        raw = descargar_velas(par, bar, VELAS)
        if len(raw) < 50:
            print(f"\n {par} {bar}: solo {len(raw)} velas — se omite (poco historial).")
            continue

        df, cruces = generar_senales(raw)
        print(f"\n {bar}: {len(df)} velas | {len(cruces)} cruces de EMAs")

        if EXPORTAR and not cruces.empty:
            fc = f"{CARPETA}/cruces_{par}_{bar}.csv"
            cruces.to_csv(fc, index=False)
            print(f" Cruces guardados: {fc}")

        trades, eq, desc = backtest(df, specs, SL_DEF, TP_DEF, CORTO)
        tdf, eqdf = informe(par, bar, df, trades, eq, desc)
        if EXPORTAR:
            suf = bar.replace("m", "min").replace("H", "h")
            tdf.to_csv(f"{CARPETA}/trades_{par}_{suf}.csv", index=False)
            eqdf.to_csv(f"{CARPETA}/equity_{par}_{suf}.csv", index=False)

        if OPT_ACTIVA:
            tabla = optimizar(df, specs)
            best = tabla.iloc[0]
            mejores.append({"tf": bar, **best.to_dict()})
            print(f"\n Optimización SL/TP en {bar} (top 3):")
            print(tabla.head(3).to_string(index=False))

    # 3) Recomendación global
    if OPT_ACTIVA and mejores:
        g = max(mejores, key=lambda m: m["retorno_eur"])
        print(f"\n{'='*62}")
        print(f" RECOMENDACIÓN: timeframe {g['tf']} | SL {g['SL_%']}% | "
              f"TP {g['TP_%']}% | {g['retorno_eur']} EUR")
        print(f"{'='*62}")
    print(f"\nConfig: {CAPITAL} EUR/op | EMAs {EMAS} | fee {COMISION*100:.3f}% | "
          f"cortos: {'sí' if CORTO else 'no'}")

if __name__ == "__main__":
    main()
