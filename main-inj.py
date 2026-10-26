"""
inj_bot.py — Bot EMA 3/21 (1H) para OKX Future sobre INJ/USD:USD-310711
====================================================================================

ALINEADO ESTRICTAMENTE con CLAUDE_CONTEXT.md (v2):

Estrategia oficial:
    - EMA(3) / EMA(21) en velas 1H CERRADAS (nunca la vela en formación)
    - Señal = CRUCE en ventana -2 / -3 / -4 (cruces anteriores descartados)
    - Filtro obligatorio: separation_pct = abs(EMA3-EMA21)/EMA21*100 >= 0.17
      (el cruce NO basta; sin separación suficiente NO se opera)
    - Golden Cross → LONG. Death Cross → observación (BOT_ALLOW_SHORT=1 lo habilita)
    - SL/TP atómicos adheridos via attachAlgoOrds (OKX nativo)

Arquitectura temporal:
    4D = referencia macro (informativa) | 1H = señal oficial | 30m = apoyo (informativo)
    La entrada nace SOLO del marco 1H.

Fail-closed / protecciones (CLAUDE_CONTEXT.md):
    - Host fijo https://my.okx.com (EEE / MiCA). USDT PROHIBIDO (veto).
    - ctVal / ctValCcy / tickSz siempre de la API; nunca se asume el tamaño.
    - Posición primero (una posición por activo) → capacidad → cooldown → señal.
    - clOrdId por vela + anti-duplicados fail-closed (error de verificación = bloqueo).
    - Guardia de nocional (BOT_MAX_NOTIONAL) y control de capacidad (equity USD).
    - Ante cualquier duda: NO OPERAR.

Parámetros operativos (override vía env vars):
    - BOT_SYMBOL (INJ/USD:USD-310711), BOT_LEVERAGE (2), BOT_NOTIONAL (12),
      BOT_SL_PCT (1.0), BOT_TP_PCT (1.5), BOT_MAX_NOTIONAL (15), COOLDOWN_MIN (60),
      BOT_LOOP_SEC (900), BOT_ALLOW_SHORT (0), DRY_RUN, SINGLE_RUN, TEST_ONLY.
    - El marco temporal y las EMA ya NO se pueden cambiar por entorno.

NOTA DE VALIDACION: SL 1.0% / TP 1.5% y el nocional se calibraron para EMA 7/21 en 15m.
    NO están validados para EMA 3/21 en 1H con filtro 0.17% — REVALIDAR.

Autor: panda-trading-desk 🐼
Licencia: MIT
"""

from __future__ import annotations

import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import ccxt
import pandas as pd
from ccxt import ExchangeError, NetworkError


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Configuración (env vars con defaults seguros para EEE)
# ─────────────────────────────────────────────────────────────────────────────

BASE_ASSET      = "INJ"
SYMBOL          = os.getenv("BOT_SYMBOL",        "INJ/USD:USD-310711")
LEVERAGE        = int(os.getenv("BOT_LEVERAGE",    "2"))
NOTIONAL_USD    = float(os.getenv("BOT_NOTIONAL",  "12"))
SL_PCT          = float(os.getenv("BOT_SL_PCT",    "1.0"))  # 1% pérdida
TP_PCT          = float(os.getenv("BOT_TP_PCT",    "1.5"))  # 1.5% ganancia
LOOP_SECONDS    = int(os.getenv("BOT_LOOP_SEC",    "900"))   # 15 min
DRY_RUN         = os.getenv("DRY_RUN", "0") == "1"
MAX_NOTIONAL_USD = _f(os.getenv("BOT_MAX_NOTIONAL", "15")) or 15.0
ALLOW_SHORT     = os.getenv("BOT_ALLOW_SHORT", "0") == "1"

# Tipo de mercado: 'swap' (perp) o 'future' (con vencimiento)
DEFAULT_TYPE    = os.getenv("BOT_DEFAULT_TYPE",    "future")

# Modos de ejecución
SINGLE_RUN      = os.getenv("SINGLE_RUN", "0") == "1"
TEST_ONLY       = os.getenv("TEST_ONLY", "0") == "1"

# Host OBLIGATORIO (EEE / MiCA). No configurable.
HOST            = "my.okx.com"
HOST_URL        = "https://" + HOST
MARGIN_MODE     = "isolated"

# tickSize override explícito (solo si el usuario lo define; nunca se inventa uno)
TICK_SIZE_OVERRIDE = os.getenv("BOT_TICK_SIZE", "")

# Cooldown tras pérdida (minutos)
COOLDOWN_MIN    = int(os.getenv("COOLDOWN_MIN", "60"))

# Estrategia oficial (fija)
TIMEFRAME         = "1h"    # marco oficial de señal
SUPPORT_TIMEFRAME = "30m"   # apoyo operacional (informativo, no bloquea)
MACRO_TIMEFRAME   = "4d"    # referencia macro (informativa, no bloquea)
EMA_FAST = 3
EMA_SLOW = 21
MIN_SEPARATION_PCT = 0.17
SIGNAL_WINDOW = (-2, -3, -4)
WARMUP = 30
CANDLE_LIMIT = 100


# ─────────────────────────────────────────────────────────────────────────────
# Logging estructurado
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("inj_bot")

# Guardas de configuración (fail-closed): el host y el marco son ley.
_region = os.getenv("OKX_REGION", "eee").lower()
if _region != "eee":
    log.error("[FALLO-CERRADO] OKX_REGION=%s no permitido. Host obligatorio: %s", _region, HOST_URL)
    sys.exit(1)
if os.getenv("BOT_TIMEFRAME") not in (None, "", TIMEFRAME):
    log.warning("BOT_TIMEFRAME=%s ignorado: el marco oficial es %s.", os.getenv("BOT_TIMEFRAME"), TIMEFRAME)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tf_to_ms(tf: str) -> int:
    """Convierte '15m', '1h', '4h', '1d' a milisegundos. Default 15m."""
    m = re.match(r"(\d+)([mhd])", tf)
    if not m:
        return 900_000
    n, u = int(m.group(1)), m.group(2)
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[u]


def build_okx_exchange(private: bool = False) -> ccxt.okx:
    """
    Construye ccxt.okx con host FIJO my.okx.com (EEE) y defaultType correcto.
    ccxt-okx usa la clave urls.api.rest (no public/private).
    """
    cfg = {
        "enableRateLimit": True,
        "hostname": HOST,
        "options": {"defaultType": DEFAULT_TYPE},
        "urls": {"api": {"rest": HOST_URL}},
    }
    if private:
        cfg["apiKey"]    = os.getenv("OKX_API_KEY", "")
        cfg["secret"]    = os.getenv("OKX_SECRET_KEY", "")
        cfg["password"]  = os.getenv("OKX_PASSWORD", "")

    log.info("OKX host: %s  defaultType=%s", HOST_URL, DEFAULT_TYPE)
    return ccxt.okx(cfg)


@dataclass
class InstrumentSpec:
    """Metadatos del contrato necesarios para dimensionar y redondear órdenes (todo de la API)."""
    ct_val: float        # tamaño de 1 contrato (ctVal)
    ccy: str             # moneda del contrato (ctValCcy)
    tick_size: float     # paso mínimo de precio
    price_precision: int # decimales de precio


def _is_forbidden(ex: ccxt.okx, symbol: str) -> bool:
    """Veto USDT: settle, quote o instId con USDT."""
    m = ex.market(symbol)
    settle = str(m.get("settle") or "").upper()
    quote = str(m.get("quote") or "").upper()
    inst_id = str(m.get("id") or "").upper()
    return settle == "USDT" or quote == "USDT" or "USDT" in inst_id


def _inst_type(ex: ccxt.okx, symbol: str) -> str:
    try:
        t = str((ex.market(symbol).get("info") or {}).get("instType") or "").upper()
        if t in ("SWAP", "FUTURES"):
            return t
    except Exception:
        pass
    m = ex.market(symbol)
    return "SWAP" if (m.get("swap") or m.get("type") == "swap") else "FUTURES"


def load_instrument_spec(ex: ccxt.okx, symbol: str) -> InstrumentSpec:
    """Carga ctVal, ctValCcy y tickSize desde la API. Nunca asume valores."""
    markets = ex.load_markets(reload=True)
    if symbol not in markets:
        raise RuntimeError("Simbolo %s no existe en OKX. NO SE OPERA." % symbol)
    mkt = markets[symbol]

    if _is_forbidden(ex, symbol):
        raise RuntimeError("VETO: %s es instrumento USDT — prohibido. NO SE OPERA." % symbol)
    if mkt.get("active") is False:
        raise RuntimeError("Instrumento %s inactivo. NO SE OPERA." % symbol)

    info = mkt.get("info") or {}
    ct_val = _f(info.get("ctVal") or mkt.get("contractSize"))
    ccy = str(info.get("ctValCcy") or "").upper()
    if ct_val <= 0 or not ccy:
        raise RuntimeError("Contrato sin ctVal/ctValCcy validos en la API (ctVal=%s, ctValCcy=%s). NO SE OPERA."
                           % (info.get("ctVal"), info.get("ctValCcy")))

    tick_raw = (
        TICK_SIZE_OVERRIDE
        or info.get("tickSz")
        or (mkt.get("precision") or {}).get("price")
    )
    tick_size = _f(tick_raw)
    if tick_size <= 0:
        raise RuntimeError("tickSize no disponible (ni API ni BOT_TICK_SIZE). NO SE OPERA.")

    price_precision = max(0, -int(math.floor(math.log10(tick_size))))

    log.info(
        "Instrument spec: %s | ctVal=%s %s | tickSize=%s | precision=%d | %s",
        symbol, ct_val, ccy, tick_size, price_precision, _inst_type(ex, symbol),
    )
    return InstrumentSpec(ct_val=ct_val, ccy=ccy, tick_size=tick_size, price_precision=price_precision)


def usd_per_contract(spec: InstrumentSpec, price: float) -> float:
    """Valor en USD de 1 contrato según ctValCcy. Unidad sorpresa = bloqueo."""
    if spec.ccy in ("USD", "USDC"):
        return spec.ct_val
    if spec.ccy == BASE_ASSET:
        if price <= 0:
            raise RuntimeError("Precio no valido (%s) para valorar contrato en %s. NO SE OPERA." % (price, spec.ccy))
        return spec.ct_val * price
    raise RuntimeError("ctValCcy inesperada (%s) — unidad sorpresa. NO SE OPERA." % spec.ccy)


def round_to_tick(price: float, tick: float, precision: int) -> str:
    """Redondea precio al tickSize. Devuelve string formateado (OKX exige string)."""
    stepped = math.floor(price / tick) * tick
    return f"{stepped:.{precision}f}"


def calc_contracts(notional_usd: float, usd_ct: float) -> int:
    """Calcula número de contratos a partir del notional objetivo en USD."""
    if usd_ct <= 0:
        return 0
    raw = notional_usd / usd_ct
    contracts = int(math.floor(raw))
    if contracts < 1:
        log.warning(
            "Notional %.2f USD pequeño para 1 contrato (raw=%.4f). Mínimo 1 (la guardia de nocional decide).",
            notional_usd, raw,
        )
        contracts = 1
    return contracts


def extract_entry_price(order: Dict[str, Any], fallback: float) -> float:
    """
    OKX no siempre devuelve 'average' en market fills (puede venir None).
    Cadena de fallback: average → price → lastTradePrice → current.
    """
    for key in ("average", "price", "lastTradePrice"):
        val = order.get(key)
        if val:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return fallback


def _cl_order_id(candle_ts: int) -> str:
    """Idempotencia: clOrdId determinista por vela cerrada."""
    return "INJ" + str(int(candle_ts))


def candle_already_traded(ex: ccxt.okx, symbol: str, candle_ts: int) -> bool:
    """
    Verifica si ya operamos esta vela (vía clOrdId).
    FAIL-CLOSED: si la verificación falla por cualquier motivo distinto de 'no existe'
    (51603), se BLOQUEA la entrada.
    """
    cl = _cl_order_id(candle_ts)
    try:
        inst = ex.market(symbol)["id"]
        resp = ex.private_get_trade_order({"instId": inst, "clOrdId": cl})
        data = resp.get("data") or []
        if data and str(data[0].get("sCode", "0")) == "0":
            log.info("Vela %d ya tradeada (clOrdId=%s). Duplicado bloqueado.", candle_ts, cl)
            return True
        return False
    except ExchangeError as e:
        if "51603" in str(e) or "does not exist" in str(e).lower():
            return False
        log.warning("No se pudo verificar duplicado (%s); BLOQUEO fail-closed.", e)
        return True
    except Exception as e:
        log.warning("No se pudo verificar duplicado (%s); BLOQUEO fail-closed.", e)
        return True


def cooldown_active(ex: ccxt.okx, symbol: str) -> bool:
    """Devuelve True si hubo pérdida en los últimos COOLDOWN_MIN minutos (fail-closed)."""
    try:
        market = ex.market(symbol)
        hist = ex.private_get_trade_fills_history({
            "instType": _inst_type(ex, symbol),
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
        log.warning("cooldown_active error: %s. Asumo cooldown (fail-closed).", e)
        return True


def capacity_ok(ex: ccxt.okx, need: float, notional: float) -> bool:
    """Verifica equity en USD suficiente para el margen requerido (fail-closed)."""
    try:
        raw = ex.privateGetAccountBalance()
        details = ((raw or {}).get("data") or [{}])[0].get("details") or []
        total_usd = sum(_f(d.get("eqUsd")) for d in details)
        cabe = total_usd >= need
        log.info("Margen: ~$%.2f | colateral: ~$%.2f | Notional: $%.2f -> %s",
                 need, total_usd, notional, "CABE" if cabe else "NO CABE")
        return cabe
    except Exception as e:
        log.error("capacity_ok balance error: %s — BLOQUEO.", e)
        return False


class PositionCheckError(Exception):
    """La posición no pudo verificarse: NUNCA se interpreta como 'sin posición'."""


def get_open_position(ex: ccxt.okx, symbol: str) -> Optional[Dict[str, Any]]:
    try:
        for pos in ex.fetch_positions([symbol]):
            if pos.get("symbol") == symbol and _f(pos.get("contracts")) > 0:
                return pos
    except Exception as e:
        log.error("Error consultando posiciones: %s", e)
        raise PositionCheckError(str(e))
    return None


def separation_pct(fast_val: float, slow_val: float) -> Optional[float]:
    """abs(EMA3 - EMA21) / EMA21 * 100. None si no calculable."""
    slow_val = _f(slow_val)
    if slow_val == 0:
        return None
    return abs(_f(fast_val) - slow_val) / slow_val * 100.0


def closed_df(ex: ccxt.okx, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """OHLCV con la vela en formación descartada (por timestamp). Solo velas cerradas."""
    ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])
    if df.empty:
        return df
    now_ms = int(time.time() * 1000)
    if int(df["ts"].iloc[-1]) + _tf_to_ms(timeframe) > now_ms:
        df = df.iloc[:-1].reset_index(drop=True)
    return df


def log_context_frames(ex: ccxt.okx, symbol: str) -> None:
    """4D = referencia macro, 30m = apoyo operacional.
    Solo velas CERRADAS. Puramente informativo: nunca bloquea ni genera señal."""
    for label, tf in (("MACRO", MACRO_TIMEFRAME), ("APOYO", SUPPORT_TIMEFRAME)):
        try:
            offered = getattr(ex, "timeframes", None) or {}
            if offered and tf not in offered:
                log.info("%s %s: marco no ofrecido por OKX/ccxt — omitido (informativo).", label, tf)
                continue
            df = closed_df(ex, symbol, tf, 100)
            if len(df) < EMA_SLOW + 5:
                log.info("%s %s: velas cerradas insuficientes (%d) (informativo).", label, tf, len(df))
                continue
            f = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
            s = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
            sep = separation_pct(f.iloc[-1], s.iloc[-1])
            log.info("%s %s (cerrada) EMA%d/%d: EMA%d %s EMA%d | separacion %s (informativo)",
                     label, tf, EMA_FAST, EMA_SLOW, EMA_FAST,
                     ">" if f.iloc[-1] > s.iloc[-1] else "<", EMA_SLOW,
                     ("%.3f%%" % sep) if sep is not None else "n/d")
        except Exception as e:
            log.warning("%s %s: contexto no disponible (%s) — se ignora.", label, tf, e)


# ─────────────────────────────────────────────────────────────────────────────
# Bot principal
# ─────────────────────────────────────────────────────────────────────────────

class InjBot:
    def __init__(self) -> None:
        self.symbol = SYMBOL
        self.timeframe = TIMEFRAME
        self.leverage = LEVERAGE
        self.notional = NOTIONAL_USD
        self.sl_pct = SL_PCT / 100.0
        self.tp_pct = TP_PCT / 100.0
        self.dry_run = DRY_RUN
        self.single_run = SINGLE_RUN
        self.test_only = TEST_ONLY

        # Exchange público (sin credenciales) para leer velas
        self.ex_pub = build_okx_exchange(private=False)

        # Exchange privado (con credenciales) perezoso
        self.ex_priv: Optional[ccxt.okx] = None
        self.instrument: Optional[InstrumentSpec] = None

    def _private_exchange(self) -> ccxt.okx:
        if self.ex_priv is not None:
            return self.ex_priv

        api_key    = os.getenv("OKX_API_KEY")
        secret_key = os.getenv("OKX_SECRET_KEY")
        passphrase = os.getenv("OKX_PASSWORD")

        if not (api_key and secret_key and passphrase):
            raise RuntimeError(
                "Faltan credenciales OKX. Define OKX_API_KEY, OKX_SECRET_KEY, "
                "OKX_PASSWORD en el entorno (o usa DRY_RUN=1)."
            )

        self.ex_priv = build_okx_exchange(private=True)

        if self.dry_run:
            # OKX sandbox oficial (paper trading)
            self.ex_priv.set_sandbox_mode(True)
            log.warning("DRY_RUN activado → usando sandbox OKX (paper trading).")

        return self.ex_priv

    def evaluate_signal(self) -> Optional[Tuple[str, int, float]]:
        """
        Cruce EMA3/EMA21 en 1H CERRADA, ventana -2/-3/-4, con filtro de separación >= 0.17 %.
        Devuelve (side 'LONG'|'SHORT', candle_ts, cierre_ultima_cerrada) o None.
        """
        df = closed_df(self.ex_pub, self.symbol, self.timeframe, CANDLE_LIMIT)
        if len(df) < WARMUP + 10:
            log.error("1H insuficiente (%d velas cerradas). Sin señal.", len(df))
            return None

        ema_f = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
        ema_s = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
        close_last = float(df["close"].iloc[-1])

        # Tras descartar la vela en formación, -1 es la última vela CERRADA.
        log.info("1H EMA%d/%d: %.6f/%.6f | EMA%d %s EMA%d (estado) | cierre=%.6f",
                 EMA_FAST, EMA_SLOW, ema_f.iloc[-1], ema_s.iloc[-1], EMA_FAST,
                 ">" if ema_f.iloc[-1] > ema_s.iloc[-1] else "<", EMA_SLOW, close_last)

        log_context_frames(self.ex_pub, self.symbol)

        for i_curr in SIGNAL_WINDOW:
            i_prev = i_curr - 1
            idx = len(df) + i_curr
            if abs(i_prev) > len(df) - 1 or idx < WARMUP:
                continue
            candle_ts = int(df["ts"].iloc[i_curr])

            up = ema_f.iloc[i_prev] <= ema_s.iloc[i_prev] and ema_f.iloc[i_curr] > ema_s.iloc[i_curr]
            down = ema_f.iloc[i_prev] >= ema_s.iloc[i_prev] and ema_f.iloc[i_curr] < ema_s.iloc[i_curr]
            if not (up or down):
                continue

            side = "LONG" if up else "SHORT"
            nombre = "ALCISTA (Golden Cross)" if up else "BAJISTA (Death Cross)"

            # FILTRO OBLIGATORIO: el cruce NO es suficiente.
            sep = separation_pct(ema_f.iloc[i_curr], ema_s.iloc[i_curr])
            if sep is None:
                log.warning("Cruce %s EMA%d/%d (vela %d): separación no calculable -> NO OPERAR (fail-closed).",
                            nombre, EMA_FAST, EMA_SLOW, i_curr)
                continue
            if sep < MIN_SEPARATION_PCT:
                log.info("Cruce %s EMA%d/%d (vela %d) | Separación EMA = %.3f%% -> rechazada (min %.2f%%). NO OPERAR.",
                         nombre, EMA_FAST, EMA_SLOW, i_curr, sep, MIN_SEPARATION_PCT)
                continue

            log.info("Cruce %s EMA%d/%d (vela %d) | Separación EMA = %.3f%% -> válida (min %.2f%%).",
                     nombre, EMA_FAST, EMA_SLOW, i_curr, sep, MIN_SEPARATION_PCT)

            if side == "SHORT" and not ALLOW_SHORT:
                log.info("▼ Death Cross válido. Motor short desactivado (BOT_ALLOW_SHORT=0) → observación.")
                continue
            return side, candle_ts, close_last

        log.info("Sin cruce EMA%d/%d válido en la ventana. Vigilando.", EMA_FAST, EMA_SLOW)
        return None

    def _configure_account_for_trading(self, ex: ccxt.okx) -> None:
        """BUG-04 fix: set_leverage. BUG-05 fix: forzar one-way mode."""
        try:
            ex.set_position_mode(False, self.symbol)
            log.info("Position mode → one-way para %s.", self.symbol)
        except ExchangeError as e:
            log.debug("set_position_mode skip (probablemente ya one-way): %s", e)

        ex.set_leverage(
            self.leverage,
            self.symbol,
            params={"mgnMode": MARGIN_MODE},
        )
        log.info("Leverage fijado en %dx %s para %s.", self.leverage, MARGIN_MODE, self.symbol)

    def _place_order(self, ex: ccxt.okx, side: str, contracts: int,
                     ref_price: float, candle_ts: int) -> str:
        """
        Crea orden market con SL/TP adheridos via attachAlgoOrds.
        Estructura OKX nativa con slOrdPx='-1' (market exit correcto).
        Idempotencia vía clOrdId por vela. tdMode explícito = margen del leverage.
        """
        if _is_forbidden(ex, self.symbol):
            raise RuntimeError("VETO: instrumento USDT prohibido (%s). NO SE OPERA." % self.symbol)

        if side == "LONG":
            oside = "buy"
            sl_raw = ref_price * (1.0 - self.sl_pct)
            tp_raw = ref_price * (1.0 + self.tp_pct)
        else:
            oside = "sell"
            sl_raw = ref_price * (1.0 + self.sl_pct)
            tp_raw = ref_price * (1.0 - self.tp_pct)

        sl_str = round_to_tick(sl_raw, self.instrument.tick_size, self.instrument.price_precision)
        tp_str = round_to_tick(tp_raw, self.instrument.tick_size, self.instrument.price_precision)

        attach_algo = [{
            "slTriggerPx":   sl_str,
            "slOrdPx":       "-1",        # OKX: "-1" = market exit
            "tpTriggerPx":   tp_str,
            "tpOrdPx":       "-1",
            "closeFraction": "1",
        }]

        cl_ord = _cl_order_id(candle_ts)

        log.info(
            "Preparando %s: contracts=%d | entry≈%.6f | SL=%s | TP=%s | clOrdId=%s",
            side, contracts, ref_price, sl_str, tp_str, cl_ord,
        )

        if self.dry_run:
            log.warning("[DRY-RUN] No se envía orden. Configuración:")
            log.warning("  symbol=%s type=market side=%s amount=%d", self.symbol, oside, contracts)
            log.warning("  attachAlgoOrds=%s", attach_algo)
            log.warning("  clOrdId=%s tdMode=%s", cl_ord, MARGIN_MODE)
            return "DRY_RUN_SKIPPED"

        try:
            order = ex.create_order(
                symbol=self.symbol,
                type="market",
                side=oside,
                amount=contracts,
                params={
                    "tdMode": MARGIN_MODE,
                    "attachAlgoOrds": attach_algo,
                    "clOrdId": cl_ord,
                },
            )
        except ExchangeError as e:
            if "51016" in str(e):
                log.info("OKX: clOrdId duplicado (51016) — idempotencia OK.")
                return "DUPLICATE_OK"
            raise

        entry_price = extract_entry_price(order, ref_price)

        log.info(
            "Orden %s ejecutada. ID=%s | entry≈%.6f | SL=%s | TP=%s",
            side, order.get("id"), entry_price, sl_str, tp_str,
        )
        return str(order.get("id"))

    def cycle(self) -> None:
        """
        Un ciclo del bucle (orden fail-closed según CLAUDE_CONTEXT.md):
            1) Instrumento verificado desde la API (veto USDT, ctVal, tickSz)
            2) Posición primero → capacidad / guardia de nocional → cooldown
            3) Señal: cruce EMA3/21 1H cerrada, ventana -2/-3/-4, separación >= 0.17 %
            4) Anti-duplicados (clOrdId) → configurar cuenta → orden
        BUG-07 fix: cualquier excepción se captura y loggea SIN relanzar (= no opera).
        """
        try:
            log.info("─" * 60)
            log.info("Ciclo iniciado para %s @ %s", self.symbol, self.timeframe)

            if self.test_only:
                sig = self.evaluate_signal()
                if sig:
                    log.info("[TEST_ONLY] Señal %s detectada pero NO se opera.", sig[0])
                return

            ex = self._private_exchange()
            self.instrument = load_instrument_spec(ex, self.symbol)

            # Posición primero (una posición por activo).
            try:
                pos = get_open_position(ex, self.symbol)
            except PositionCheckError:
                log.error("Posición no verificable. BLOQUEO fail-closed; se espera al siguiente ciclo.")
                return
            if pos:
                log.info("Posición abierta (%s). Esperando SL/TP.", pos.get("side"))
                return

            # Capacidad + guardia de nocional.
            price = _f(ex.fetch_ticker(self.symbol).get("last"))
            if price <= 0:
                log.error("Ticker sin precio válido. BLOQUEO fail-closed.")
                return
            usd_ct = usd_per_contract(self.instrument, price)
            contracts = calc_contracts(self.notional, usd_ct)
            nocional = contracts * usd_ct
            log.info("Nocional: %d contrato(s) x %s %s (~$%.2f)",
                     contracts, self.instrument.ct_val, self.instrument.ccy, nocional)
            if nocional <= 0:
                log.error("GUARDIA: nocional no válido ($%.2f). NO SE OPERA.", nocional)
                return
            if nocional > MAX_NOTIONAL_USD:
                log.error("GUARDIA: nocional $%.2f > máximo $%.2f — unidad inesperada. NO SE OPERA.",
                          nocional, MAX_NOTIONAL_USD)
                return
            need = nocional / max(self.leverage, 1)
            if not capacity_ok(ex, need, nocional):
                log.warning("Sin capacidad (balance o notional). Skip.")
                return

            # Cooldown anti revenge trading.
            if cooldown_active(ex, self.symbol):
                log.warning("Cooldown activo. Skip este ciclo.")
                return

            # Señal (1H, velas cerradas).
            sig = self.evaluate_signal()
            if not sig:
                return
            side, candle_ts, _close = sig

            # Idempotencia: ¿ya operamos esta vela? (fail-closed)
            if candle_already_traded(ex, self.symbol, candle_ts):
                log.info("Vela %d ya tradeada. Skip.", candle_ts)
                return

            # Configurar cuenta SOLO tras superar todos los filtros.
            self._configure_account_for_trading(ex)

            log.info("Señal %s (cruce EMA%d/%d 1H + separación válida). Abriendo %d contrato(s)...",
                     side, EMA_FAST, EMA_SLOW, contracts)
            order_id = self._place_order(ex, side, contracts, price, candle_ts)
            log.info("Ciclo de riesgo completo. order_id=%s", order_id)

        except (NetworkError, ExchangeError) as e:
            log.error("Error de exchange/red (se continúa): %s", e)
        except Exception as e:
            log.exception("Error inesperado (no aborta el bot): %s", e)

    def run_forever(self) -> None:
        log.info(
            "╔══════════════════════════════════════════════════════════╗"
            "\n║ inj_bot arrancando"
            "\n║ symbol=%s tf=%s EMA%d/%d sep>=%.2f%% lev=%dx notional=%.2f USD (max %.2f)"
            "\n║ SL=-%.2f%% TP=+%.2f%% loop=%ds dry_run=%s host=%s type=%s short=%s",
            self.symbol, self.timeframe, EMA_FAST, EMA_SLOW, MIN_SEPARATION_PCT,
            self.leverage, self.notional, MAX_NOTIONAL_USD,
            self.sl_pct * 100, self.tp_pct * 100, LOOP_SECONDS, self.dry_run,
            HOST_URL, DEFAULT_TYPE, ALLOW_SHORT,
        )
        log.info("╚══════════════════════════════════════════════════════════╝")

        if self.single_run:
            log.info("SINGLE_RUN=1 → ejecutando 1 ciclo y saliendo.")
            self.cycle()
            log.info("Ciclo único completado. Saliendo.")
            return

        while True:
            self.cycle()
            log.info("Durmiendo %d segundos hasta el próximo ciclo…", LOOP_SECONDS)
            time.sleep(LOOP_SECONDS)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    bot = InjBot()
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        log.info("Interrupción manual (Ctrl+C). Saliendo limpiamente…")
        sys.exit(0)


if __name__ == "__main__":
    main()
