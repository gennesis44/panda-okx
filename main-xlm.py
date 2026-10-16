"""
xlm_bot.py — Bot EMA 3/21 LONG+SHORT con idempotencia, cooldown y guardia de nocional
====================================================================================
Correcciones aplicadas:
  - BUG-01: _post_trade_order fallback incorrecto → private_post_trade_order
  - BUG-02: candle_already_traded demasiado conservador → log + return False
  - BUG-03: Falta set_position_mode → forzar one-way mode
  - BUG-04: except Exception: pass silencioso → logging
  - BUG-05: tf_ms fallback frágil → parser regex
  - BUG-06: camelCase/snake_case mezclado → todo snake_case
  - MEJ-01: logging estructurado con timestamps
  - MEJ-02: DRY_RUN con sandbox OKX
  - MEJ-03: parámetros principales como env vars
Autor: panda-trading-desk 🐼  |  Licencia: MIT
"""

from __future__ import annotations

import os
import re
import sys
import time
import logging
from typing import Optional, Tuple, Any, Dict

import ccxt
import pandas as pd
from ccxt import ExchangeError, NetworkError


# ─────────────────────────────────────────────────────────────────────────────
# Logging estructurado
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("xlm_bot")


# ─────────────────────────────────────────────────────────────────────────────
# Config (env vars con defaults sensatos)
# ─────────────────────────────────────────────────────────────────────────────
HOST          = os.getenv("OKX_HOST", "https://my.okx.com")   # EEE por defecto
BASE_ASSET    = os.getenv("BASE_ASSET", "XLM")
AMOUNT        = int(os.getenv("AMOUNT", "1"))                 # contratos

# Asimétricos: SL corto más ajustado (riesgo cuadrado)
SL_LONG       = float(os.getenv("SL_LONG",  "0.035"))        # 3.5%
TP_LONG       = float(os.getenv("TP_LONG",  "0.050"))        # 5.0%
SL_SHORT      = float(os.getenv("SL_SHORT", "0.025"))        # 2.5%
TP_SHORT      = float(os.getenv("TP_SHORT", "0.040"))        # 4.0%

TIMEFRAME     = os.getenv("BOT_TIMEFRAME", "1h")
EMA_FAST      = int(os.getenv("EMA_FAST",  "3"))
EMA_SLOW      = int(os.getenv("EMA_SLOW",  "21"))
TD_MODE       = os.getenv("TD_MODE", "cross")                 # cross | isolated
LEVERAGE      = int(os.getenv("BOT_LEVERAGE", "1"))
COOLDOWN_MIN  = int(os.getenv("COOLDOWN_MIN", "60"))
MAX_NOTIONAL  = float(os.getenv("MAX_NOTIONAL_USD", "15.0"))
SIGNAL_WINDOW = tuple(int(x) for x in os.getenv("SIGNAL_WINDOW", "-2,-3,-4").split(","))
WARMUP        = int(os.getenv("WARMUP", "65"))                # 21*3+2

TEST_MODE     = os.getenv("TEST_MODE", "0") == "1"
DRY_RUN       = os.getenv("DRY_RUN", "0") == "1"
SINGLE_CYCLE  = os.getenv("SINGLE_CYCLE", "0") == "1"        # alias de SINGLE_RUN


# ─────────────────────────────────────────────────────────────────────────────
# Exchange
# ─────────────────────────────────────────────────────────────────────────────
exchange = ccxt.okx({
    "apiKey":         os.getenv("OKX_API_KEY", ""),
    "secret":         os.getenv("OKX_SECRET_KEY", ""),
    "password":       os.getenv("OKX_PASSWORD", ""),
    "enableRateLimit": True,
    "options":        {"defaultType": "swap"},
    "urls":           {"api": {"rest": HOST}},
})

if DRY_RUN:
    exchange.set_sandbox_mode(True)
    log.warning("DRY_RUN=1 → sandbox OKX (paper trading).")


# ─────────────────────────────────────────────────────────────────────────────
# Timeframe parser (BUG-05 fix)
# ─────────────────────────────────────────────────────────────────────────────
def _tf_to_ms(tf: str) -> int:
    """Convierte '15m', '1h', '4h', '1d' a milisegundos. Default 15m si no parsea."""
    m = re.match(r"(\d+)([mhd])", tf)
    if not m:
        log.warning("Timeframe '%s' no parseable, usando 15m default.", tf)
        return 900_000
    n, u = int(m.group(1)), m.group(2)
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[u]


# ─────────────────────────────────────────────────────────────────────────────
# Instrumento (veto USDT, fallback future → swap)
# ─────────────────────────────────────────────────────────────────────────────
def _is_forbidden(symbol: str) -> bool:
    """Veto USDT: cualquier settle USDT queda excluido."""
    try:
        settle = str(exchange.market(symbol).get("settle") or "").upper()
        return settle == "USDT"
    except Exception:
        return True  # si no podemos ver el settle, vetamos por seguridad


def pick_future() -> Optional[str]:
    """Elige el future USD-margined con vencimiento más lejano para BASE_ASSET."""
    exchange.load_markets()
    candidatos = []
    for m in exchange.markets.values():
        if (m.get("base") == BASE_ASSET and m.get("future") and m.get("active")
                and not _is_forbidden(m["symbol"])):
            info = m.get("info") or {}
            try:
                exp = int(info.get("expTime") or 0)
            except (TypeError, ValueError):
                exp = 0
            candidatos.append((exp, m["symbol"]))
    if not candidatos:
        return None
    candidatos.sort(reverse=True)
    return candidatos[0][1]


def pick_swap_usd() -> Optional[str]:
    """Fallback: swap USD-margined (coin-margined)."""
    exchange.load_markets()
    for m in exchange.markets.values():
        if (m.get("base") == BASE_ASSET and m.get("active")
                and (m.get("swap") or (m.get("info") or {}).get("instType") == "SWAP")
                and not _is_forbidden(m["symbol"])):
            return m["symbol"]
    return None


def resolve_symbol() -> str:
    """Devuelve el símbolo a operar. Si hay posición abierta, la respeta."""
    exchange.load_markets()

    # 1) ¿Hay posición abierta en BASE_ASSET?
    try:
        for p in exchange.fetch_positions():
            if (p.get("contracts") or 0) > 0 and (p.get("symbol") or "").startswith(BASE_ASSET + "/"):
                if _is_forbidden(p["symbol"]):
                    raise RuntimeError("Posición abierta en USDT prohibido: " + p["symbol"])
                log.info("Posición abierta detectada en %s", p["symbol"])
                return p["symbol"]
    except RuntimeError:
        raise
    except Exception as e:
        log.debug("fetch_positions skip (probablemente no hay posición): %s", e)

    # 2) Future USD-margined con vencimiento lejano
    sym = pick_future()
    if sym:
        log.info("Símbolo elegido (future): %s", sym)
        return sym

    # 3) Swap USD-margined (coin-margined)
    sym = pick_swap_usd()
    if sym:
        log.info("Símbolo elegido (swap): %s", sym)
        return sym

    raise RuntimeError("No hay instrumento activo para " + BASE_ASSET + " sin USDT.")


# ─────────────────────────────────────────────────────────────────────────────
# Posición abierta
# ─────────────────────────────────────────────────────────────────────────────
def get_open_position(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        for pos in exchange.fetch_positions([symbol]):
            if pos.get("symbol") == symbol and float(pos.get("contracts") or 0) > 0:
                return pos
    except Exception as e:
        log.debug("get_open_position skip: %s", e)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Guardia de nocional y balance
# ─────────────────────────────────────────────────────────────────────────────
def _contract_meta(symbol: str, price: Optional[float] = None) -> Tuple[float, str, str, float, float]:
    market = exchange.market(symbol)
    info = market.get("info") or {}
    ctval = float(market.get("contractSize") or info.get("ctVal") or 1)
    ccy   = str(info.get("ctValCcy") or BASE_ASSET).upper()
    settle = str(info.get("settleCcy") or market.get("settle") or "?").upper()
    if price is None:
        price = exchange.fetch_ticker(symbol).get("last") or 0
    price = float(price)
    usd = ctval if ccy in ("USD", "USDT", "USDC") else ctval * price
    return ctval, ccy, settle, price, usd


def capacity_ok(symbol: str, amount: int) -> bool:
    """Verifica (1) notional ≤ MAX_NOTIONAL y (2) balance ≥ margen requerido."""
    try:
        _ctval, _ccy, _settle, _price, usd = _contract_meta(symbol)
        nocional = usd * amount
        if nocional > MAX_NOTIONAL:
            log.warning("Nocional %.2f USD > cap %.2f USD. Skip.", nocional, MAX_NOTIONAL)
            return False
        need = nocional / max(LEVERAGE, 1)
    except Exception as e:
        log.error("capacity_ok: no pude leer contract meta: %s", e)
        return False

    try:
        raw = exchange.private_get_account_balance()
        details = ((raw or {}).get("data") or [{}])[0].get("details") or []
        total = sum(float(d.get("eqUsd") or 0) for d in details)
        log.info("Balance OKX: ~%.2f USD | Necesario: %.2f USD", total, need)
        return total >= need
    except Exception as e:
        log.error("capacity_ok: no pude leer balance: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Idempotencia (clOrdId por vela)
# ─────────────────────────────────────────────────────────────────────────────
def _cl_order_id(candle_ts: int) -> str:
    """Genera clOrdId determinista por vela: 'XLM' + timestamp_ms."""
    return BASE_ASSET + str(int(candle_ts))


def candle_already_traded(symbol: str, candle_ts: int) -> bool:
    """
    BUG-02 fix: si hay cualquier error no-51603, asumimos NO tradeado.
    Mejor pecar de optimista y dejar que OKX rechace con 51016 si ya existe.
    """
    cl = _cl_order_id(candle_ts)
    try:
        inst = exchange.market(symbol)["id"]
        resp = exchange.private_get_trade_order({"instId": inst, "clOrdId": cl})
        data = resp.get("data") or []
        if data and str(data[0].get("sCode", "0")) == "0":
            log.info("Vela %d ya tradeada (clOrdId=%s).", candle_ts, cl)
            return True
        return False
    except ExchangeError as e:
        msg = str(e)
        if "51603" in msg or "does not exist" in msg.lower():
            return False
        log.warning("Error no esperado en candle_already_traded: %s. Asumo NO tradeado.", e)
        return False
    except Exception as e:
        log.warning("Error de red en candle_already_traded: %s. Asumo NO tradeado.", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Entrada con SL/TP atómicos (LONG + SHORT)
# ─────────────────────────────────────────────────────────────────────────────
def execute_order(side: str, symbol: str, ref_price: float, amount: int, candle_ts: int) -> bool:
    ctval, ccy, _settle, _p, usd = _contract_meta(symbol, ref_price)
    nocional = amount * usd
    if nocional > MAX_NOTIONAL:
        raise RuntimeError("Guardia nocional: %.2f > %.2f" % (nocional, MAX_NOTIONAL))

    if side == "LONG":
        oside  = "buy"
        sl_raw = ref_price * (1 - SL_LONG)
        tp_raw = ref_price * (1 + TP_LONG)
    else:  # SHORT
        oside  = "sell"
        sl_raw = ref_price * (1 + SL_SHORT)
        tp_raw = ref_price * (1 - TP_SHORT)

    sl = exchange.price_to_precision(symbol, sl_raw)
    tp = exchange.price_to_precision(symbol, tp_raw)
    sz = exchange.amount_to_precision(symbol, amount)

    req = {
        "instId":       exchange.market(symbol)["id"],
        "tdMode":       TD_MODE,
        "side":         oside,
        "ordType":      "optimal_limit_ioc",
        "sz":           sz,
        "clOrdId":      _cl_order_id(candle_ts),
        "attachAlgoOrds": [{
            "tpTriggerPx": tp, "tpOrdPx": "-1",
            "slTriggerPx": sl, "slOrdPx": "-1",
        }],
    }

    log.info(
        "Preparando %s: contracts=%d | ref=%.6f | SL=%s | TP=%s | notional≈%.2f USD",
        side, amount, ref_price, sl, tp, nocional,
    )

    if DRY_RUN:
        log.warning("[DRY-RUN] No se envía orden real. Request que se enviaría:")
        log.warning("  %s", req)
        return True

    try:
        resp = _post_trade_order(req)
    except ExchangeError as e:
        if "51016" in str(e):
            log.info("clOrdId ya existente (51016). Asumo ya tradeado.")
            return True
        raise

    data = resp.get("data") or []
    d0 = data[0] if isinstance(data, list) and data else {}
    s_code = str(d0.get("sCode", resp.get("code", "1")))
    if s_code == "51016":
        log.info("clOrdId ya existente (51016). Asumo ya tradeado.")
        return True
    if s_code != "0":
        raise ExchangeError("OKX rechazo: " + str(d0.get("sMsg") or resp.get("msg")))
    log.info("Orden ejecutada: side=%s | ordId=%s", side, d0.get("ordId"))
    return True


def _post_trade_order(req: Dict[str, Any]) -> Dict[str, Any]:
    """
    BUG-01 fix: el fallback debe ser private_post_trade_order (POST), no _get_ (GET).
    """
    method = getattr(exchange, "privatePostTradeOrder", None) or exchange.private_post_trade_order
    return method(req)


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown tras pérdida
# ─────────────────────────────────────────────────────────────────────────────
def cooldown_active(symbol: str) -> bool:
    """Devuelve True si hubo una fill con pnl<0 en los últimos COOLDOWN_MIN minutos."""
    try:
        market = exchange.market(symbol)
        inst_type = str((market.get("info") or {}).get("instType") or "FUTURES").upper()
        hist = exchange.private_get_trade_fills_history({
            "instType": inst_type,
            "instId":   market["id"],
            "limit":    "5",
        })
        now_ms = time.time() * 1000
        for fill in (hist.get("data") or []):
            pnl = float(fill.get("fillPnl") or fill.get("pnl") or 0)
            ts_ms = int(fill.get("ts") or 0)
            age_min = (now_ms - ts_ms) / 60_000.0
            if pnl < 0 and age_min < COOLDOWN_MIN:
                log.warning("Cooldown activo: pérdida %.2f USD hace %.1f min.", pnl, age_min)
                return True
        return False
    except Exception as e:
        log.warning("cooldown_active: error al leer fills: %s. Asumo cooldown activo.", e)
        return True  # conservador: si no puedo leer, mejor no operar


# ─────────────────────────────────────────────────────────────────────────────
# Señal EMA 3/21 con signal window
# ─────────────────────────────────────────────────────────────────────────────
def fetch_data(symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    return pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])


def ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()


def evaluate_signal(symbol: str) -> Tuple[Optional[str], Optional[float], Optional[int]]:
    """
    Devuelve (side, price, candle_ts) si hay cruce en la signal_window.
    Recorta la última vela si aún no cerró.
    """
    df = fetch_data(symbol, TIMEFRAME, limit=100)
    if len(df) < WARMUP + 10:
        log.warning("Datos insuficientes: %d velas < %d warmup.", len(df), WARMUP + 10)
        return None, None, None

    now_ms = int(time.time() * 1000)
    tf_ms = _tf_to_ms(TIMEFRAME)
    # Si la última vela aún no cerró, la quitamos
    if int(df["timestamp"].iloc[-1]) + tf_ms > now_ms:
        df = df.iloc[:-1].reset_index(drop=True)

    e_f = ema(df["close"], EMA_FAST)
    e_s = ema(df["close"], EMA_SLOW)
    price = exchange.fetch_ticker(symbol).get("last") or float(df["close"].iloc[-1])

    for i_curr in SIGNAL_WINDOW:
        i_prev = i_curr - 1
        idx = len(df) + i_curr
        if abs(i_prev) > len(df) - 1 or idx < WARMUP:
            continue
        candle_ts = int(df["timestamp"].iloc[i_curr])
        up   = (e_f.iloc[i_prev] <= e_s.iloc[i_prev] and e_f.iloc[idx] > e_s.iloc[idx])
        down = (e_f.iloc[i_prev] >= e_s.iloc[i_prev] and e_f.iloc[idx] < e_s.iloc[idx])
        if up:
            log.info("★ Golden Cross detectado en vela %d (ts=%d)", i_curr, candle_ts)
            return "LONG", float(price), candle_ts
        if down:
            log.info("▼ Death Cross detectado en vela %d (ts=%d)", i_curr, candle_ts)
            return "SHORT", float(price), candle_ts
    return None, None, None


# ─────────────────────────────────────────────────────────────────────────────
# Setup y diagnóstico
# ─────────────────────────────────────────────────────────────────────────────
def verify_setup() -> None:
    raw = exchange.private_get_account_balance()
    details = ((raw or {}).get("data") or [{}])[0].get("details") or []
    total = sum(float(d.get("eqUsd") or 0) for d in details)
    log.info("AUTH OK | colateral: ~$%.2f", total)


def catalog() -> None:
    exchange.load_markets()
    log.info("Catálogo de mercados para %s:", BASE_ASSET)
    for m in exchange.markets.values():
        if m.get("base") != BASE_ASSET:
            continue
        info = m.get("info") or {}
        estado = "ACTIVO" if m.get("active") else "inactivo"
        log.info("  %s | %s | %s | settle=%s",
                 m["symbol"], info.get("instType") or "?", estado,
                 m.get("settle") or "?")


# ─────────────────────────────────────────────────────────────────────────────
# Ciclo principal
# ─────────────────────────────────────────────────────────────────────────────
def run_cycle() -> None:
    log.info("─" * 60)
    symbol = resolve_symbol()

    # BUG-03 fix: forzar one-way mode antes de operar
    try:
        exchange.set_position_mode(False, symbol)
        log.info("Position mode → one-way.")
    except ExchangeError as e:
        log.debug("set_position_mode skip (probablemente ya one-way): %s", e)
    except Exception as e:
        log.debug("set_position_mode skip: %s", e)

    # Leverage
    try:
        exchange.set_leverage(LEVERAGE, symbol, params={"mgnMode": TD_MODE})
        log.info("Leverage fijado en %dx (%s).", LEVERAGE, TD_MODE)
    except Exception as e:
        log.warning("set_leverage error: %s. Continuando.", e)

    # ¿Hay posición abierta?
    if get_open_position(symbol):
        log.info("Posición ya abierta en %s. Skip.", symbol)
        return

    # Capacidad
    if not capacity_ok(symbol, AMOUNT):
        log.warning("Sin capacidad para abrir. Skip.")
        return

    # Cooldown
    if cooldown_active(symbol):
        log.warning("Cooldown activo. Skip este ciclo.")
        return

    # Señal
    signal, price, candle_ts = evaluate_signal(symbol)
    if not signal:
        log.info("Sin señal nueva en este ciclo. Vigilando.")
        return

    # Idempotencia
    if candle_already_traded(symbol, candle_ts):
        log.info("Vela %d ya tradeada. Skip.", candle_ts)
        return

    # Ejecución (BUG-04 fix: no tragar excepciones en silencio)
    try:
        execute_order(signal, symbol, price, AMOUNT, candle_ts)
        log.info("Ciclo completado con éxito. signal=%s | candle_ts=%d", signal, candle_ts)
    except Exception as e:
        log.error("Fallo al ejecutar orden %s: %s", signal, e)


def run_once() -> None:
    verify_setup()
    if TEST_MODE:
        catalog()
        return
    run_cycle()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        run_once()
    except KeyboardInterrupt:
        log.info("Interrupción manual. Saliendo.")
        sys.exit(0)
    except Exception as e:
        log.exception("Error fatal en run_once: %s", e)
        sys.exit(1)
