"""
=====================================================
 BOT EN VIVO — HBAR-USD X-Perp (OKX) | EMA 3/4/10/21/27
 30m | SL 3% | TP 2% | 1 contrato (~9.3 USD)
 - Solo velas CERRADAS (nunca opera la vela en curso)
 - Posición real consultada en OKX (fuente de verdad)
 - SL/TP adjuntos a la orden (OCO en el exchange)
 - Cierra por señal contraria (rotura de stack)
 Secrets: OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE
=====================================================
"""
import base64, hashlib, hmac, json, os, datetime
import requests, yaml

VERSION = "1"

with open("config.yml", "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f) or {}

API   = CFG.get("exchange", {}).get("url_api", "https://www.okx.com")
INST  = CFG.get("mercado", {}).get("inst_id", "HBAR-USD_UM_XPERP-310815")
EMAS  = CFG.get("estrategia", {}).get("emas", [3, 4, 10, 21, 27])
CORTO = CFG.get("estrategia", {}).get("permitir_corto", True)
SL    = float(CFG.get("riesgo", {}).get("stop_loss_pct", 3))
TP    = float(CFG.get("riesgo", {}).get("take_profit_pct", 2))
BOT   = CFG.get("bot", {})
BAR   = BOT.get("timeframe", "30m")
SZ    = str(BOT.get("contratos", 1))
LEV   = str(BOT.get("apalancamiento", 3))
TD    = BOT.get("margen", "isolated")
DEMO  = bool(BOT.get("demo", False))

KEY  = os.environ.get("OKX_API_KEY", "")
SEC  = os.environ.get("OKX_API_SECRET", "")
PASS = os.environ.get("OKX_API_PASSPHRASE", "")

def log(*a): print(*a, flush=True)

# ---------------- API PÚBLICA ----------------
def pub(path, params=None):
    r = requests.get(API + path, params=params or {}, timeout=10)
    return r.json()

def spec():
    d = pub("/api/v5/public/instruments",
            {"instType": "FUTURES", "instId": INST}).get("data", [])
    return d[0] if d else None

def velas_cerradas():
    d = pub("/api/v5/market/candles",
            {"instId": INST, "bar": BAR, "limit": 100}).get("data", [])
    cerradas = [x for x in d if x[8] == "1"]          # confirm == 1
    velas = [{"ts": int(x[0]), "close": float(x[4])} for x in cerradas]
    return velas[::-1]

def precio_actual():
    d = pub("/api/v5/market/ticker", {"instId": INST}).get("data", [])
    return float(d[0]["last"]) if d else None

# ---------------- API PRIVADA ----------------
def firma(ts, metodo, ruta, cuerpo):
    mac = hmac.new(SEC.encode(), (ts + metodo + ruta + cuerpo).encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def priv(metodo, ruta, query="", cuerpo=None):
    ts = datetime.datetime.now(datetime.timezone.utc
          ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    ruta_f = ruta + (f"?{query}" if query else "")
    cuerpo_s = json.dumps(cuerpo) if cuerpo else ""
    h = {"OK-ACCESS-KEY": KEY,
         "OK-ACCESS-SIGN": firma(ts, metodo, ruta_f, cuerpo_s),
         "OK-ACCESS-TIMESTAMP": ts,
         "OK-ACCESS-PASSPHRASE": PASS,
         "Content-Type": "application/json"}
    if DEMO:
        h["x-simulated-trading"] = "1"
    r = requests.request(metodo, API + ruta_f, headers=h,
                         data=cuerpo_s if cuerpo else None, timeout=10)
    try:
        return r.json()
    except Exception:
        return {"code": f"HTTP{r.status_code}", "msg": r.text[:300]}

def a_tick(px, tick):
    from decimal import Decimal, ROUND_HALF_UP
    return str(Decimal(str(px)).quantize(Decimal(str(tick)), rounding=ROUND_HALF_UP))

# ---------------- ESTRATEGIA ----------------
def ema(vals, p):
    k, e, out = 2 / (p + 1), vals[0], [vals[0]]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def stack_en(i, emas):
    v = [emas[p][i] for p in EMAS]
    return all(v[j] > v[j + 1] for j in range(len(v) - 1))

def senales(velas):
    closes = [v["close"] for v in velas]
    emas = {p: ema(closes, p) for p in EMAS}
    ayer, hoy = stack_en(-2, emas), stack_en(-1, emas)
    return (not ayer and hoy), (ayer and not hoy)   # (compra, venta)

# ---------------- POSICIONES ----------------
def posiciones(pos_mode):
    d = priv("GET", "/api/v5/account/positions", query=f"instId={INST}")
    out = []
    for p in d.get("data", []):
        pos = float(p.get("pos") or 0)
        if pos == 0:
            continue
        if pos_mode == "long_short_mode":
            lado, lado_api = p["posSide"], p["posSide"]
        else:
            lado = "long" if pos > 0 else "short"
            lado_api = "net"
        out.append({"lado": lado, "posSide": lado_api,
                    "sz": abs(pos), "avgPx": float(p.get("avgPx") or 0)})
    return out

# ---------------- ÓRDENES ----------------
def abrir(direccion, px, tick, pos_mode):
    largo = direccion == "long"
    body = {"instId": INST, "tdMode": TD, "ordType": "market", "sz": SZ,
            "side": "buy" if largo else "sell",
            "attachAlgoOrds": [{
                "tpTriggerPx": a_tick(px * (1 + TP/100) if largo else px * (1 - TP/100), tick),
                "tpOrdPx": "-1",
                "slTriggerPx": a_tick(px * (1 - SL/100) if largo else px * (1 + SL/100), tick),
                "slOrdPx": "-1"}]}
    if pos_mode == "long_short_mode":
        body["posSide"] = "long" if largo else "short"
    else:
        body["posSide"] = "net"
    return priv("POST", "/api/v5/trade/order", cuerpo=body)

def cerrar(p, pos_mode):
    body = {"instId": INST, "tdMode": TD, "ordType": "market",
            "sz": str(p["sz"]), "reduceOnly": "true",
            "side": "sell" if p["lado"] == "long" else "buy"}
    if pos_mode == "long_short_mode":
        body["posSide"] = p["posSide"]
    else:
        body["posSide"] = "net"
    return priv("POST", "/api/v5/trade/order", cuerpo=body)

# ---------------- MAIN ----------------
def main():
    log(f">>> BOT HBAR v{VERSION} | {INST} | {BAR} | {SZ} ct | SL {SL}% / TP {TP}%")
    if not KEY or not SEC or not PASS:
        raise SystemExit("Faltan secrets: OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE")

    s = spec()
    if not s:
        raise SystemExit(f"{INST} no existe en OKX.")
    tick = s["tickSz"]

    velas = velas_cerradas()
    if len(velas) < 30:
        raise SystemExit(f"Solo {len(velas)} velas cerradas — no opero con tan pocos datos.")
    px = precio_actual()
    compra, venta = senales(velas)
    log(f"Última vela cerrada: close={velas[-1]['close']} | precio actual={px}")
    log(f"Señales -> compra={compra} venta={venta}")

    acct = priv("GET", "/api/v5/account/config")
    if acct.get("code") != "0":
        raise SystemExit(f"Error de autenticación OKX: {acct.get('msg')} "
                         f"(¿keys/passphrase correctas y con permiso de trading?)")
    pos_mode = acct["data"][0]["posMode"]
    log(f"Modo de posición: {pos_mode}")

    lev = priv("POST", "/api/v5/account/set-leverage",
               cuerpo={"instId": INST, "lever": LEV, "mgnMode": TD})
    if lev.get("code") != "0":
        log(f"[aviso] set-leverage: {lev.get('msg')} (sigo; el apalancamiento ya puede estar fijado)")

    # 1) cerrar si hay señal contraria
    for p in posiciones(pos_mode):
        contrario = (p["lado"] == "long" and venta) or (p["lado"] == "short" and compra)
        if contrario:
            log(f"CERRANDO {p['lado']} {p['sz']} ct (señal contraria)...")
            r = cerrar(p, pos_mode)
            log(f"  -> code={r.get('code')} msg={r.get('msg')}")
        else:
            log(f"Mantengo {p['lado']} {p['sz']} ct @ {p['avgPx']} — SL/TP activos en OKX.")

    # 2) abrir si no hay posición y hay señal
    if not posiciones(pos_mode):
        if compra:
            log(f"ABRIENDO LARGO {SZ} ct | SL {a_tick(px*(1-SL/100), tick)} | "
                f"TP {a_tick(px*(1+TP/100), tick)}")
            r = abrir("long", px, tick, pos_mode)
        elif venta and CORTO:
            log(f"ABRIENDO CORTO {SZ} ct | SL {a_tick(px*(1+SL/100), tick)} | "
                f"TP {a_tick(px*(1-TP/100), tick)}")
            r = abrir("short", px, tick, pos_mode)
        else:
            r = None
            log("Sin señal nueva en la última vela cerrada. Nada que hacer.")
        if r:
            log(f"  -> code={r.get('code')} msg={r.get('msg')} "
                f"ordId={[o.get('ordId') for o in r.get('data', [])]}")
            if r.get("code") != "0":
                log("  [!] ORDEN RECHAZADA — revisar msg arriba (fondo, modo de posición, margen)")

if __name__ == "__main__":
    main()
