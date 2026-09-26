"""
=====================================================
 Backtest EMAs (3,4,10,21,27) — HBAR-USD OKX (EEE/MiCA)
 VERSION 5: experimento CANDADO ANTI-RANGO
   separacion EMA3-EMA27 vs entrada (A/B por umbral)
 Requisitos: pip install requests pandas pyyaml
=====================================================
"""

import requests, time, math, os, yaml
import pandas as pd

VERSION = "5"

# ------------- CONFIG (tolerante) -------------
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
ULY        = cfg_get("mercado.uly", "HBAR-USD")
INST_ID    = cfg_get("mercado.inst_id", "") or cfg_get("par.inst_id", "") or ""
TIMEFRAMES = cfg_get("backtest.timeframes", ["30m", "1H", "4H"])
VELAS      = int(cfg_get("backtest.velas", 1000))
CAPITAL    = float(cfg_get("backtest.capital_por_operacion", 5))
COMISION   = float(cfg_get("backtest.comision", 0.0005))
EMAS       = cfg_get("estrategia.emas", [3, 4, 10, 21, 27])
CORTO      = bool(cfg_get("estrategia.permitir_corto", True))
OPT_ACTIVA = bool(cfg_get("optimizacion.activar", True))
SL_GRID    = cfg_get("optimizacion.sl_pct", [2, 3, 4, 5])
TP_GRID    = cfg_get("optimizacion.tp_pct", [2, 3, 4, 6])
SEP_GRID   = cfg_get("optimizacion.sep_grid",
                     [0.0, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0])
EXPORTAR   = bool(cfg_get("salida.exportar_csv", True))
CARPETA    = cfg_get("salida.carpeta", "resultados")
SL_DEF     = cfg_get("riesgo.stop_loss_pct", 3)
TP_DEF     = cfg_get("riesgo.take_profit_pct", 2)

# ------------- HELPERS DE API -------------
def instrumentos(inst_type, **params):
    try:
        r = requests.get(f"{API}/api/v5/public/instruments",
                         params={"instType": inst_type, **params}, timeout=10)
        return r.json().get("data") or []
    except Exception:
        return []

def vols_24h(inst_type, uly=None):
    params = {"instType": inst_type}
    if uly:
        params["uly"] = uly
    try:
        r = requests.get(f"{API}/api/v5/market/tickers", params=params, timeout=10)
        return {t["instId"]: float(t.get("volCcy24h") or t.get("vol24h") or 0)
                for t in (r.json().get("data") or [])}
    except Exception:
        return {}

def obtener_precio(inst_id):
    try:
        r = requests.get(f"{API}/api/v5/market/ticker",
                         params={"instId": inst_id}, timeout=10)
        d = r.json().get("data") or []
        return float(d[0]["last"]) if d else None
    except Exception:
        return None

def obtener_specs(inst_type, inst_id):
    d = instrumentos(inst_type, instId=inst_id)
    if not d:
        return None
    d = d[0]
    return {"minSz": float(d["minSz"]), "lotSz": float(d["lotSz"]),
            "tickSz": float(d["tickSz"]),
            "ctVal": float(d.get("ctVal") or 1),
            "listTime": d.get("listTime") or ""}

def elegir_instrumento():
    if INST_ID and "-USD" in INST_ID:
        for t in ("SWAP", "FUTURES", "SPOT"):
            if instrumentos(t, instId=INST_ID):
                return t, INST_ID
    d = instrumentos("SWAP", uly=ULY)
    if d:
        return "SWAP", d[0]["instId"]
    d = instrumentos("FUTURES", uly=ULY)
    if d:
        vols = vols_24h("FUTURES", ULY)
        par = max((c["instId"] for c in d), key=lambda i: vols.get(i, 0))
        return "FUTURES", par
    if instrumentos("SPOT", instId=ULY):
        return "SPOT", ULY
    if instrumentos("SWAP", instId="HBAR-USDT-SWAP"):
        return "SWAP", "HBAR-USDT-SWAP"
    return None, None

# ------------- DESCARGA DE VELAS -------------
def descargar_velas(inst_id, bar, total):
    url = f"{API}/api/v5/market/history-candles"
    velas, after = [], None
    for _ in range(40):
        if len(velas) >= total:
            break
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

# ------------- SEÑALES Y CRUCES -------------
def generar_senales(df):
    for p in EMAS:
        df[f"ema{p}"] = df["close"].ewm(span=p, adjust=False).mean()
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

# ------------- BACKTEST (con candado opcional) -------------
def backtest(df, specs, sl, tp, permitir_corto, sep_min=0.0):
    ctval, lot_sz, min_sz = specs["ctVal"], specs["lotSz"], specs["minSz"]

    # CANDADO ANTI-RANGO (v5): separacion EMA rapida - lenta en % del precio
    if sep_min > 0:
        ef, es = df[f"ema{EMAS[0]}"], df[f"ema{EMAS[-1]}"]
        sep = (ef - es) / df["close"] * 100
        # LONG: el stack formado debe tener fuerza >= sep_min
        # SHORT: el stack QUE SE ROMPE debia tener fuerza >= sep_min
        compra = df["compra"] & (sep >= sep_min)
        venta  = df["venta"] & (sep.shift(1, fill_value=0.0) >= sep_min)
    else:
        compra, venta = df["compra"], df["venta"]

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
            "pnl_eur": round(pnl, 2),
            "pnl_%": round(pnl / CAPITAL * 100, 2),
            "motivo": motivo})
        pos, entrada, sl_p, tp_p = 0, None, None, None

    for i, f in df.iterrows():
        p = f["close"]
        if pos == 1:
            if sl_p and f["low"] <= sl_p:    cerrar(sl_p, f["ts"], "SL")
            elif tp_p and f["high"] >= tp_p: cerrar(tp_p, f["ts"], "TP")
        elif pos == -1:
            if sl_p and f["high"] >= sl_p:   cerrar(sl_p, f["ts"], "SL")
            elif tp_p and f["low"] <= tp_p:  cerrar(tp_p, f["ts"], "TP")

        if compra.loc[i]:
            if pos == -1: cerrar(p, f["ts"], "señal")
            if pos == 0:  abrir(1, f)
        elif venta.loc[i]:
            if pos == 1:  cerrar(p, f["ts"], "señal")
            if pos == 0 and permitir_corto: abrir(-1, f)

        flot = 0.0
        if pos == 1:    flot = CAPITAL * (p / entrada["precio"] - 1)
        elif pos == -1: flot = CAPITAL * (1 - p / entrada["precio"])
        equity.append({"ts": f["ts"], "eq": CAPITAL + pnl_cerrado + flot})

    return trades, pd.DataFrame(equity).set_index("ts"), descartadas

# ------------- INFORME -------------
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
    print(f" Retorno total  : {eq['eq'].iloc[-1] - CAPITAL:+.2f} EUR")
    print(f" Buy & Hold     : {bh:+.2f} %")
    print(f" Max drawdown   : {dd:.2f} %")

# ------------- EXPERIMENTO CANDADO (v5) -------------
def experimento_candado(df, specs, bar, par):
    filas = []
    for um in SEP_GRID:
        trades, eq, _ = backtest(df, specs, SL_DEF, TP_DEF, CORTO, sep_min=float(um))
        wins = sum(1 for t in trades if t["pnl_eur"] > 0)
        dd = ((eq["eq"].cummax() - eq["eq"]) / eq["eq"].cummax()).max() * 100
        filas.append({"sep_min_%": float(um),
                      "trades": len(trades),
                      "win%": round(wins/len(trades)*100, 1) if trades else 0.0,
                      "retorno_eur": round(eq["eq"].iloc[-1] - CAPITAL, 2),
                      "maxDD%": round(dd, 2)})
    tabla = pd.DataFrame(filas)

    print(f"\n EXPERIMENTO CANDADO ANTI-RANGO en {bar} "
          f"(SL {SL_DEF}% / TP {TP_DEF}%):")
    print("   sep_min = separacion minima EMA3-EMA27 en % del precio")
    print("   0.0 = SIN candado (como el bot en vivo)")
    print(tabla.to_string(index=False))

    if EXPORTAR:
        tabla.to_csv(f"{CARPETA}/candado_{par}_{bar}.csv", index=False)

    base = tabla[tabla["sep_min_%"] == 0.0]
    base_ret = float(base.iloc[0]["retorno_eur"]) if not base.empty else 0.0
    best = tabla.loc[tabla["retorno_eur"].idxmax()]
    delta = float(best["retorno_eur"]) - base_ret

    print(f"\n VEREDICTO {bar}: mejor umbral = {best['sep_min_%']}% "
          f"({best['retorno_eur']} EUR vs {base_ret} EUR sin candado -> "
          f"{delta:+.2f} EUR, {best['trades']} trades)")
    if delta >= 1.0:
        print("   -> El CANDADO AYUDA: conviene implantarlo con ese umbral.")
    elif delta <= -1.0:
        print("   -> El candado PERJUDICA: extirpar (no filtrar).")
    else:
        print("   -> NEUTRO (diferencia < 1 EUR): no justifica el implante.")
    return tabla, best, delta

# ------------- MAIN -------------
def main():
    print(f">>> backtest_futuros.py VERSION {VERSION} (experimento candado)")
    if EXPORTAR:
        os.makedirs(CARPETA, exist_ok=True)

    print("Buscando instrumento HBAR-USD en OKX...")
    tipo, par = elegir_instrumento()
    if tipo is None:
        raise SystemExit("OKX no lista ningún instrumento HBAR-USD (ni proxy USDT).")

    specs = obtener_specs(tipo, par)
    if specs is None:
        raise SystemExit(f"{par} no existe como {tipo} en OKX.")
    print(f"\nInstrumento : {par} ({tipo}) | ctVal {specs['ctVal']} HBAR")

    verdictos = []
    for bar in TIMEFRAMES:
        raw = descargar_velas(par, bar, VELAS)
        if len(raw) < 50:
            print(f"\n {par} {bar}: solo {len(raw)} velas — se omite.")
            continue

        df, cruces = generar_senales(raw)
        print(f"\n {bar}: {len(df)} velas | {len(cruces)} cruces de EMAs")

        # Backtest base (sin candado) — igual que el bot vivo
        trades, eq, desc = backtest(df, specs, SL_DEF, TP_DEF, CORTO)
        informe(par, bar, df, trades, eq, desc)
        if EXPORTAR:
            suf = bar.replace("m", "min").replace("H", "h")
            pd.DataFrame(trades).to_csv(f"{CARPETA}/trades_{par}_{suf}.csv", index=False)
            eq.reset_index().to_csv(f"{CARPETA}/equity_{par}_{suf}.csv", index=False)

        # Experimento candado
        tabla, best, delta = experimento_candado(df, specs, bar, par)
        verdictos.append({"tf": bar, "umbral": best["sep_min_%"],
                          "retorno": best["retorno_eur"], "delta": delta})

    if verdictos:
        print(f"\n{'='*62}\n RESUMEN CANDADO ANTI-RANGO (flota HBAR)\n{'='*62}")
        for v in verdictos:
            print(f"  {v['tf']:<4} mejor umbral {v['umbral']}% -> "
                  f"{v['retorno']} EUR ({v['delta']:+.2f} vs sin candado)")
        ganador = max(verdictos, key=lambda v: v["retorno"])
        print(f"\n CONCLUSION: en {ganador['tf']} el candado optimo es "
              f"{ganador['umbral']}%.")
        print(" Si el umbral optimo es 0.0 en todos los marcos -> EXTIRPAR")
        print(" el implante en los destructores (el filtro solo cuesta trades).")

if __name__ == "__main__":
    main()
