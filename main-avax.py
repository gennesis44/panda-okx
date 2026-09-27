"""
════════════════════════════════════════════════════════════════════
  DECRETO DE DESPLIEGUE — BUQUE: AVAX, "El Octavo"   [v3 ley validada]
════════════════════════════════════════════════════════════════════
  Instrumento : XPERP AVAX/USD, futuro de vencimiento más lejano
                (fallback SWAP AVAX-USD si no hay futuro disponible)
  Host        : https://my.okx.com (MiCA/EEE — USDT PROHIBIDO)
  LEY VALIDADA (backtest-avax.py, run 27-sep — única que pasa los 4
  criterios de robustez de la rejilla completa):
      Timeframe : 1H
      EMA       : rápida 8 / lenta 34
      SL / TP   : 4.0% / 5.5%
      Backtest  : 27 trades · WR 37.0% (BE 27.3%) · PF 1.57 ·
                  expectativa +0.476%/trade · maxDD -10.33% · +12.86%
      Descartada la provisional 4H/8/21: WR 27.6%, PF 0.74, -15.3%
      (todo el marco 4H resultó tóxico para AVAX en backtest)
  Contrato    : ctVal = 10 AVAX (confirmado por API) · guardia $15
  Axiomas de la flota:
      - Solo velas CERRADAS (confirm==1)
      - Señal por cruce EMA, ventana -2/-3/-4 (cubre cadencia del cron)
      - Sin RSI. SIN candado anti-rango (extirpado por A/B en HBAR,
        confirmado en XRP: el filtro expulsa ganadoras. El aire ES el edge)
      - Posición abierta = no operar. SL/TP viven en el exchange
      - Fail-closed: lectura crítica dudosa = BLOQUEO
  v2/v3 FIXES (revisión Carbono-Silicio, previos al despliegue):
      FIX-1: unidades honestas — nocional = ctVal x PRECIO (ctValCcy
             es la moneda base, NO USD)
      FIX-2: position_open via /account/positions?instId= (sin mapeo
             ccxt) — elimina riesgo de re-entrada múltiple
      FIX-3: SL/TP redondeados al tickSz real del contrato
      FIX-4: idempotencia por vela restaurada (clOrdId=AVAX+candle_ts)
      FIX-5: precio de entrada desde ticker (no close de vela vieja)
════════════════════════════════════════════════════════════════════
"""

import os
import sys
import time
import math
import logging
import ccxt

# ══════════════════ LEY PARAMETRIZADA (VALIDADA) ══════════════════
TIMEFRAME = "1H"
EMA_FAST = 8
EMA_SLOW = 34
SL_PCT = {"long": 0.040, "short": 0.040}
TP_PCT = {"long": 0.055, "short": 0.055}
COOLDOWN_MINUTES = 60
TARGET_NOTIONAL_USD = 7.5     # collar ~6-9 USD
MAX_NOTIONAL_USD = 15.0       # guardia dura -> abortar si se supera
SIGNAL_LOOKBACK_OFFSETS = [2, 3, 4]  # ventana -2/-3/-4
CANDLES_NEEDED = max(EMA_SLOW * 4, 80)
CLORDID_PREFIX = "AVAX"

UNDERLYING = "AVAX-USD"
FALLBACK_SWAP_INSTID = "AVAX-USD-SWAP"

TEST_MODE = os.environ.get("TEST_MODE", "0") == "1"
SINGLE_CYCLE = os.environ.get("SINGLE_CYCLE", "0") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("avax-el-octavo")


# ══════════════════ INFRAESTRUCTURA OKX EEA ══════════════════
def _force_eea_host(obj):
    """Reemplaza recursivamente www.okx.com -> my.okx.com en exchange.urls."""
    if isinstance(obj, dict):
        return {k: _force_eea_host(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_force_eea_host(v) for v in obj]
    if isinstance(obj, str):
        return obj.replace("www.okx.com", "my.okx.com")
    return obj


def build_exchange():
    exchange = ccxt.okx({
        "apiKey": os.environ.get("OKX_API_KEY", ""),
        "secret": os.environ.get("OKX_SECRET_KEY", ""),
        "password": os.environ.get("OKX_PASSWORD", ""),
        "enableRateLimit": True,
    })
    exchange.urls["api"] = _force_eea_host(exchange.urls["api"])
    log.info("[HOST] API forzada a my.okx.com (MiCA/EEE, USDT prohibido)")
    return exchange


# ══════════════════ RESOLUCIÓN DE INSTRUMENTO ══════════════════
def resolve_symbol(exchange):
    """1) posición abierta en AVAX -> 2) futuro USD más lejano -> 3) SWAP fallback.
    Veta cualquier instrumento con settle=USDT."""
    try:
        for p in exchange.fetch_positions():
            info = p.get("info", {}) or {}
            inst_id = info.get("instId", "")
            if "AVAX" in inst_id and float(info.get("pos", 0) or 0) != 0:
                if info.get("settleCcy", "").upper() == "USDT":
                    raise RuntimeError(f"VETO: posición abierta en {inst_id} liquida en USDT")
                log.info(f"[RESOLVE] Posición abierta detectada: {inst_id}")
                return inst_id, info
    except RuntimeError:
        raise
    except Exception as e:
        log.warning(f"[RESOLVE] No se pudo leer posiciones abiertas: {e}")

    try:
        resp = exchange.public_get_public_instruments({
            "instType": "FUTURES", "uly": UNDERLYING
        })
        candidates = [
            d for d in resp.get("data", [])
            if d.get("settleCcy", "").upper() != "USDT" and d.get("expTime")
        ]
        if candidates:
            candidates.sort(key=lambda d: int(d["expTime"]), reverse=True)
            inst = candidates[0]
            log.info(f"[RESOLVE] Futuro más lejano: {inst['instId']} exp={inst['expTime']}")
            return inst["instId"], inst
    except Exception as e:
        log.warning(f"[RESOLVE] Fallo consultando FUTURES: {e}")

    try:
        resp = exchange.public_get_public_instruments({
            "instType": "SWAP", "instId": FALLBACK_SWAP_INSTID
        })
        data = resp.get("data", [])
        if data and data[0].get("settleCcy", "").upper() != "USDT":
            log.info(f"[RESOLVE] Fallback SWAP: {FALLBACK_SWAP_INSTID}")
            return FALLBACK_SWAP_INSTID, data[0]
    except Exception as e:
        log.warning(f"[RESOLVE] Fallo consultando SWAP fallback: {e}")

    raise RuntimeError("BLOQUEO: no se pudo resolver ningún instrumento AVAX válido (no-USDT)")


# ══════════════════ VELAS CERRADAS ══════════════════
def fetch_closed_candles(exchange, inst_id, bar, count):
    resp = exchange.public_get_market_candles({
        "instId": inst_id, "bar": bar, "limit": "100"
    })
    rows = resp.get("data", [])
    if not rows:
        raise RuntimeError(f"BLOQUEO: sin velas para {inst_id} {bar}")
    rows = list(reversed(rows))  # API devuelve más nueva primero -> orden asc
    closed = [r for r in rows if r[8] == "1"]  # confirm == '1'
    if len(closed) < EMA_SLOW + 5:
        raise RuntimeError("BLOQUEO: velas cerradas insuficientes para calcular EMAs")
    return closed[-count:]


def ema_series(closes, span):
    k = 2 / (span + 1)
    out = [closes[0]]
    for c in closes[1:]:
        out.append(c * k + out[-1] * (1 - k))
    return out


def detect_signal(closed_candles):
    """Cruce EMA en ventana -2/-3/-4. Devuelve (direccion, ts_vela) o (None, None)."""
    closes = [float(c[4]) for c in closed_candles]
    ema_f = ema_series(closes, EMA_FAST)
    ema_s = ema_series(closes, EMA_SLOW)
    sign = [1 if f > s else (-1 if f < s else 0) for f, s in zip(ema_f, ema_s)]

    n = len(sign)
    for offset in SIGNAL_LOOKBACK_OFFSETS:
        i = n - offset
        if i - 1 < 0:
            continue
        if sign[i - 1] <= 0 and sign[i] > 0:
            return "long", closed_candles[i][0]
        if sign[i - 1] >= 0 and sign[i] < 0:
            return "short", closed_candles[i][0]
    return None, None


# ══════════════════ UNIDADES HONESTAS (FIX-1) ══════════════════
def contract_meta(inst_info):
    """ctVal está en ctValCcy (moneda base en futures coin-margined), NO en USD."""
    ct_val = float(inst_info.get("ctVal", 0) or 0)
    ccy = str(inst_info.get("ctValCcy") or "AVAX").upper()
    tick = float(inst_info.get("tickSz") or "0.001")
    lot = float(inst_info.get("lotSz") or "1")
    minsz = float(inst_info.get("minSz") or "1")
    return ct_val, ccy, tick, lot, minsz


def usd_per_contract(ct_val, ccy, price):
    if ccy in ("USD", "USDT", "USDC"):
        return ct_val
    return ct_val * price


def compute_amount(ct_val, ccy, lot, minsz, price):
    """Contratos para nocional objetivo en USD REALES (FIX-1)."""
    usd1 = usd_per_contract(ct_val, ccy, price)
    if usd1 <= 0:
        raise RuntimeError("BLOQUEO: valor de contrato no calculable")
    log.info(f"[CONTRACT] 1 ct = {ct_val} {ccy} = ${usd1:.4f} USD (precio {price})")
    if usd1 > MAX_NOTIONAL_USD:
        raise RuntimeError(
            f"BLOQUEO (unidad sorpresa): 1 contrato vale ${usd1:.2f} > "
            f"MAX_NOTIONAL_USD=${MAX_NOTIONAL_USD:.2f}. Bot bloqueado."
        )
    raw = TARGET_NOTIONAL_USD / usd1
    c = math.floor(raw / lot) * lot
    if c < minsz:
        c = minsz
    notional = c * usd1
    while notional > MAX_NOTIONAL_USD and (c - lot) >= minsz - 1e-12:
        c = round(c - lot, 10)
        notional = c * usd1
    if notional > MAX_NOTIONAL_USD:
        raise RuntimeError(
            f"BLOQUEO: ni el mínimo ({c} ct = ${notional:.2f}) cabe bajo el guardia"
        )
    log.info(f"[AMOUNT] contracts={c} | nocional REAL=${notional:.2f} USD "
             f"(objetivo ${TARGET_NOTIONAL_USD})")
    return c, notional


# ══════════════════ GUARDAS ══════════════════
def position_open(exchange, inst_id):
    """FIX-2: consulta cruda por instId — sin mapeo de símbolos ccxt."""
    try:
        resp = exchange.private_get_account_positions({"instId": inst_id})
        for p in resp.get("data", []):
            if float(p.get("pos") or 0) != 0:
                return True
        return False
    except Exception as e:
        log.error(f"[GUARDA] No se pudo verificar posición -> fail-closed: {e}")
        return True


def cooldown_active(exchange, inst_id):
    try:
        fills = exchange.private_get_trade_fills({"instId": inst_id, "limit": "20"})
        for r in (fills.get("data") or []):
            pnl = r.get("fillPnl")
            if pnl is None:
                continue
            pnl = float(pnl)
            if pnl < 0:
                minutes_ago = (time.time() * 1000 - int(r.get("ts", 0))) / 60000
                if minutes_ago < COOLDOWN_MINUTES:
                    log.info(f"[COOLDOWN] Activo: última pérdida hace {minutes_ago:.1f} min")
                    return True
                return False
        return False
    except Exception as e:
        log.error(f"[COOLDOWN] No se pudo leer fills -> fail-closed (bloqueo): {e}")
        return True


def candle_already_traded(exchange, inst_id, candle_ts):
    """FIX-4: idempotencia por vela del chasis de la flota (fail-closed)."""
    cl = f"{CLORDID_PREFIX}{candle_ts}"[:32]
    try:
        resp = exchange.private_get_trade_order({"instId": inst_id, "clOrdId": cl})
        data = resp.get("data") or []
        if data and str(data[0].get("sCode", "0")) == "0":
            log.info(f"[DEDUP] Vela ya operada (clOrdId {cl}). Duplicado bloqueado.")
            return True
        return False
    except ccxt.ExchangeError as e:
        msg = str(e)
        if "51603" in msg or "does not exist" in msg.lower():
            return False
        log.warning(f"[DEDUP] No verificable ({msg}) -> BLOQUEO fail-closed.")
        return True
    except Exception as e:
        log.warning(f"[DEDUP] No verificable ({e}) -> BLOQUEO fail-closed.")
        return True


def capacity_ok(exchange, notional_usd):
    try:
        bal = exchange.private_get_account_balance()
        details = bal.get("data", [{}])[0]
        eq_usd = float(details.get("totalEq", 0) or 0)
        fits = eq_usd >= notional_usd * 1.1
        log.info(f"[CAPACITY] eqUsd={eq_usd:.2f} | nocional={notional_usd:.2f} -> "
                 f"{'CABE' if fits else 'NO CABE'}")
        return fits
    except Exception as e:
        log.error(f"[CAPACITY] No se pudo leer balance -> fail-closed (bloqueo): {e}")
        return False


# ══════════════════ PRECIOS (FIX-3 y FIX-5) ══════════════════
def _fmt_px(px, tick):
    """Redondea al tick real del contrato (FIX-3)."""
    ts = f"{tick:.10f}".rstrip("0")
    dec = len(ts.split(".")[1]) if "." in ts else 0
    steps = int(round(px / tick))
    return f"{steps * tick:.{dec}f}"


def last_price(exchange, inst_id, fallback):
    """FIX-5: precio vivo desde ticker crudo (sin mapeo ccxt)."""
    try:
        resp = exchange.public_get_market_ticker({"instId": inst_id})
        d = resp.get("data") or []
        if d and d[0].get("last"):
            return float(d[0]["last"])
    except Exception as e:
        log.warning(f"[PRICE] ticker no disponible ({e}); uso close de última vela cerrada")
    return float(fallback)


# ══════════════════ EJECUCIÓN ══════════════════
def set_leverage(exchange, inst_id):
    try:
        exchange.private_post_account_set_leverage({
            "instId": inst_id, "lever": "1", "mgnMode": "cross"
        })
    except Exception as e:
        log.warning(f"[LEVERAGE] No se pudo fijar leverage=1 explícitamente: {e}")


def place_order(exchange, inst_id, direction, contracts, entry_price, tick, candle_ts):
    side = "buy" if direction == "long" else "sell"
    if direction == "long":
        tp_px = entry_price * (1 + TP_PCT["long"])
        sl_px = entry_price * (1 - SL_PCT["long"])
    else:
        tp_px = entry_price * (1 - TP_PCT["short"])
        sl_px = entry_price * (1 + SL_PCT["short"])

    cl_ord_id = f"{CLORDID_PREFIX}{candle_ts}"[:32]   # FIX-4: idempotencia por vela
    tp_s = _fmt_px(tp_px, tick)                        # FIX-3: tick real
    sl_s = _fmt_px(sl_px, tick)

    params = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": side,
        "ordType": "optimal_limit_ioc",
        "sz": str(contracts),
        "clOrdId": cl_ord_id,
        "attachAlgoOrds": [{
            "tpTriggerPx": tp_s, "tpOrdPx": "-1",
            "slTriggerPx": sl_s, "slOrdPx": "-1",
        }],
    }

    if TEST_MODE:
        log.info(f"[TEST_MODE] Orden simulada, NO enviada: {params}")
        return "test-mode-no-op"

    try:
        resp = exchange.private_post_trade_order(params)
        if resp.get("code") == "0":
            log.info(f"[ORDER] Ejecutada OK clOrdId={cl_ord_id} | SL:{sl_s} TP:{tp_s} | {resp}")
            return "traded"
        raise ccxt.ExchangeError(str(resp))
    except Exception as e:
        msg = str(e)
        if "51016" in msg:
            log.info(f"[ORDER] 51016 (clOrdId duplicado) -> idempotencia OK: {msg}")
            return "traded"
        if "51603" in msg or "does not exist" in msg:
            log.warning(f"[ORDER] 51603/no-existe -> no-traded: {msg}")
            return "not-traded"
        log.error(f"[ORDER] Error no reconocido -> BLOQUEO fail-closed: {msg}")
        return "blocked"


# ══════════════════ CICLO PRINCIPAL ══════════════════
def run_cycle():
    exchange = build_exchange()
    inst_id, inst_info = resolve_symbol(exchange)

    ct_val, ccy, tick, lot, minsz = contract_meta(inst_info)
    log.info(f"[INSTRUMENT] instId={inst_id} ctVal={ct_val} {ccy} "
             f"tick={tick} lot={lot} minSz={minsz}")

    if position_open(exchange, inst_id):
        log.info("[SKIP] Posición abierta -> no operar (SL/TP vive en el exchange)")
        return

    if cooldown_active(exchange, inst_id):
        log.info("[SKIP] Cooldown activo -> no entrar")
        return

    closed = fetch_closed_candles(exchange, inst_id, TIMEFRAME, CANDLES_NEEDED)
    direction, candle_ts = detect_signal(closed)
    if direction is None:
        log.info("[SIGNAL] Sin cruce EMA en ventana -2/-3/-4 -> no operar")
        return
    log.info(f"[SIGNAL] {direction.upper()} en vela ts={candle_ts}")

    if candle_already_traded(exchange, inst_id, candle_ts):
        return

    price = last_price(exchange, inst_id, closed[-1][4])
    contracts, notional = compute_amount(ct_val, ccy, lot, minsz, price)

    if not capacity_ok(exchange, notional):
        log.info("[SKIP] No cabe según capacity_ok() -> no entrar")
        return

    set_leverage(exchange, inst_id)
    result = place_order(exchange, inst_id, direction, contracts, price, tick, candle_ts)
    log.info(f"[RESULT] Ciclo finalizado con estado: {result}")


def main():
    log.info(f"=== AVAX 'El Octavo' v3 (1H EMA8/34 SL4/TP5.5) — inicio "
             f"(TEST_MODE={TEST_MODE}, SINGLE_CYCLE={SINGLE_CYCLE}) ===")
    try:
        run_cycle()
    except Exception as e:
        log.error(f"[FATAL] Ciclo abortado (fail-closed): {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
