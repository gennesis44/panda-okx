"""
inj_bot.py — Bot EMA 7/21 LONG para OKX Future sobre INJ/USD:USD-310711
====================================================================================

Estrategia:
    - Golden Cross EMA(7) sobre EMA(21) → entrada LONG
    - Death Cross EMA(7) bajo EMA(21) → modo observación
    - SL/TP atómicos adheridos via attachAlgoOrds (OKX nativo)

Parámetros operativos (override vía env vars):
    - Símbolo:        INJ/USD:USD-310711 (future USD-margined, vencimiento 2031)
    - Timeframe:      15m
    - SL:             -1.0% respecto a la entrada
    - TP:             +1.5% respecto a la entrada
    - Leverage:       1x (max EEE retail), margen aislado
    - Notional:       12 USD objetivo
    - Bucle:          cada 15 min (cron GitHub Actions)
    - Modo dry-run:   DRY_RUN=1 (sandbox OKX)
    - Región:         OKX_REGION=eee (my.okx.com) | global (www.okx.com)

Mejoras aplicadas (vs versión inicial):
    - OKX_REGION support (my.okx.com para EEE)
    - BOT_DEFAULT_TYPE support (swap|future)
    - BOT_TICK_SIZE override (cuando ccxt devuelve None)
    - Idempotencia clOrdId por vela (anti-órdenes duplicadas)
    - Capacity check (balance ≥ margen requerido)
    - Cooldown 60min tras pérdida (anti revenge trading)
    - slOrdPx="-1" (market exit correcto en OKX)
    - Recorte de última vela si no cerró (anti falsos cruces)
    - Logging estructurado
    - Default leverage 2x (max EEE retail)
    - Default symbol INJ/USD:USD-310711 (símbolo real USD-margined)

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


# ─────────────────────────────────────────────────────────────────────────────
# Configuración (env vars con defaults seguros para EEE)
# ─────────────────────────────────────────────────────────────────────────────

SYMBOL          = os.getenv("BOT_SYMBOL",        "INJ/USD:USD-310711")
TIMEFRAME       = os.getenv("BOT_TIMEFRAME",     "15m")
LEVERAGE        = int(os.getenv("BOT_LEVERAGE",    "2"))    # max EEE retail
NOTIONAL_USD    = float(os.getenv("BOT_NOTIONAL",  "12"))
SL_PCT          = float(os.getenv("BOT_SL_PCT",    "1.0"))  # 1% pérdida
TP_PCT          = float(os.getenv("BOT_TP_PCT",    "1.5"))  # 1.5% ganancia
LOOP_SECONDS    = int(os.getenv("BOT_LOOP_SEC",    "900"))   # 15 min
DRY_RUN         = os.getenv("DRY_RUN", "0") == "1"

# Tipo de mercado: 'swap' (perp) o 'future' (con vencimiento)
DEFAULT_TYPE    = os.getenv("BOT_DEFAULT_TYPE",    "future")

# Modos de ejecución
SINGLE_RUN      = os.getenv("SINGLE_RUN", "0") == "1"
TEST_ONLY       = os.getenv("TEST_ONLY", "0") == "1"

# Región OKX: 'global' (www.okx.com) o 'eee' (my.okx.com)
OKX_REGION      = os.getenv("OKX_REGION", "global").lower()

# tickSize override (algunos futuros USD-margined no devuelven tickSize vía ccxt)
TICK_SIZE_OVERRIDE = os.getenv("BOT_TICK_SIZE", "")

# Cooldown tras pérdida (minutos)
COOLDOWN_MIN    = int(os.getenv("COOLDOWN_MIN", "60"))

EMA_FAST = 7
EMA_SLOW = 21
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
    Construye ccxt.okx con región y defaultType correctos.
    - global → www.okx.com
    - eee    → my.okx.com (Europa / EEA)
    """
    cfg = {
        "enableRateLimit": True,
        "options": {"defaultType": DEFAULT_TYPE},
    }
    if private:
        cfg["apiKey"]    = os.getenv("OKX_API_KEY", "")
        cfg["secret"]    = os.getenv("OKX_SECRET_KEY", "")
        cfg["password"]  = os.getenv("OKX_PASSWORD", "")

    if OKX_REGION == "eee":
        cfg["urls"] = {
            "api": {
                "public":  "https://my.okx.com",
                "private": "https://my.okx.com",
            }
        }
        log.info("OKX región: EEE (my.okx.com)  defaultType=%s", DEFAULT_TYPE)
    else:
        log.info("OKX región: global (www.okx.com)  defaultType=%s", DEFAULT_TYPE)

    return ccxt.okx(cfg)


@dataclass
class InstrumentSpec:
    """Metadatos del contrato necesarios para dimensionar y redondear órdenes."""
    ct_val: float        # tamaño de 1 contrato
    tick_size: float     # paso mínimo de precio
    price_precision: int # decimales de precio


def load_instrument_spec(ex: ccxt.okx, symbol: str) -> InstrumentSpec:
    """Carga ctVal, tickSize y price precision del mercado."""
    markets = ex.load_markets(reload=True)
    mkt = markets[symbol]

    ct_val = float(mkt.get("contractSize") or mkt.get("ctVal") or 1.0)

    # tickSize puede venir None en algunos futuros USD-margined → override
    tick_raw = (
        mkt.get("tickSize")
        or TICK_SIZE_OVERRIDE
        or mkt.get("precision", {}).get("price")
        or "0.001"
    )
    tick_size = float(tick_raw)

    price_precision = max(0, -int(math.floor(math.log10(tick_size)))) if tick_size > 0 else 3

    log.info(
        "Instrument spec: %s | ctVal=%s | tickSize=%s | precision=%d",
        symbol, ct_val, tick_size, price_precision,
    )
    return InstrumentSpec(ct_val=ct_val, tick_size=tick_size, price_precision=price_precision)


def round_to_tick(price: float, tick: float, precision: int) -> str:
    """Redondea precio al tickSize. Devuelve string formateado (OKX exige string)."""
    stepped = math.floor(price / tick) * tick
    return f"{stepped:.{precision}f}"


def calc_contracts(notional_usd: float, price: float, ct_val: float) -> int:
    """Calcula número de contratos a partir del notional objetivo en USD."""
    if price <= 0 or ct_val <= 0:
        return 0
    raw = notional_usd / (ct_val * price)
    contracts = int(math.floor(raw))
    if contracts < 1:
        log.warning(
            "Notional %.2f USD pequeño para 1 contrato (raw=%.4f). Mínimo 1.",
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
    Si hay cualquier error no-51603, asumimos NO tradeado (mejor pecar de optimista
    y dejar que OKX rechace con 51016 si ya existe).
    """
    cl = _cl_order_id(candle_ts)
    try:
        inst = ex.market(symbol)["id"]
        resp = ex.private_get_trade_order({"instId": inst, "clOrdId": cl})
        data = resp.get("data") or []
        if data and str(data[0].get("sCode", "0")) == "0":
            log.info("Vela %d ya tradeada (clOrdId=%s).", candle_ts, cl)
            return True
        return False
    except ExchangeError as e:
        if "51603" in str(e) or "does not exist" in str(e).lower():
            return False
        log.warning("Error candle_already_traded: %s. Asumo NO tradeado.", e)
        return False
    except Exception as e:
        log.warning("Red error candle_already_traded: %s. Asumo NO tradeado.", e)
        return False


def cooldown_active(ex: ccxt.okx, symbol: str) -> bool:
    """Devuelve True si hubo pérdida en los últimos COOLDOWN_MIN minutos."""
    try:
        market = ex.market(symbol)
        inst_type = str((market.get("info") or {}).get("instType") or "FUTURES").upper()
        hist = ex.private_get_trade_fills_history({
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
        log.warning("cooldown_active error: %s. Asumo cooldown.", e)
        return True


def capacity_ok(ex: ccxt.okx, contracts: int, price: float,
                instr: Optional[InstrumentSpec]) -> bool:
    """Verifica balance suficiente para el notional objetivo con el leverage actual."""
    try:
        ct_val = instr.ct_val if instr else 1.0
        notional = contracts * ct_val * price
        need = notional / max(LEVERAGE, 1)
    except Exception as e:
        log.error("capacity_ok meta error: %s", e)
        return False

    try:
        bal = ex.fetch_balance()
        # Total equity en USD (sumando USDC, USDT, USD)
        total_usd = 0.0
        for asset in ("USDC", "USDT", "USD"):
            v = (bal.get("total") or {}).get(asset)
            if v:
                try:
                    total_usd += float(v)
                except (TypeError, ValueError):
                    pass
        log.info("Balance USD: ~$%.2f | Necesario: $%.2f | Notional: $%.2f",
                 total_usd, need, notional)
        return total_usd >= need
    except Exception as e:
        log.error("capacity_ok balance error: %s", e)
        return False


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

    def fetch_ema_state(self) -> Tuple[float, float, float, float, float, int]:
        """
        Devuelve (prev_ema7, prev_ema21, curr_ema7, curr_ema21, current_price, candle_ts).
        Recorta la última vela si aún no cerró (anti falsos cruces).
        """
        ohlcv = self.ex_pub.fetch_ohlcv(self.symbol, self.timeframe, limit=CANDLE_LIMIT)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])

        # Recortar última vela si aún está abierta
        now_ms = int(time.time() * 1000)
        tf_ms = _tf_to_ms(self.timeframe)
        if int(df["ts"].iloc[-1]) + tf_ms > now_ms:
            df = df.iloc[:-1].reset_index(drop=True)
            log.info("Última vela recortada (aún abierta). Trabajando con velas cerradas.")

        df["ema7"]  = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
        df["ema21"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

        prev_ema7  = df["ema7"].iloc[-2]
        prev_ema21 = df["ema21"].iloc[-2]
        curr_ema7  = df["ema7"].iloc[-1]
        curr_ema21 = df["ema21"].iloc[-1]
        current_price = float(df["close"].iloc[-1])
        candle_ts = int(df["ts"].iloc[-1])

        return prev_ema7, prev_ema21, curr_ema7, curr_ema21, current_price, candle_ts

    def _configure_account_for_trading(self, ex: ccxt.okx) -> None:
        """BUG-04 fix: set_leverage. BUG-05 fix: forzar one-way mode."""
        try:
            ex.set_position_mode(False, True, self.symbol)
            log.info("Position mode → one-way para %s.", self.symbol)
        except ExchangeError as e:
            log.debug("set_position_mode skip (probablemente ya one-way): %s", e)

        ex.set_leverage(
            self.leverage,
            self.symbol,
            params={"mgnMode": "isolated"},
        )
        log.info("Leverage fijado en %dx isolated para %s.", self.leverage, self.symbol)

    def _place_long_with_algo(self, ex: ccxt.okx, contracts: int,
                              current_price: float, candle_ts: int) -> str:
        """
        Crea orden LONG market con SL/TP adheridos via attachAlgoOrds.
        Estructura OKX nativa con slOrdPx='-1' (market exit correcto).
        Idempotencia vía clOrdId por vela.
        """
        sl_raw = current_price * (1.0 - self.sl_pct)
        tp_raw = current_price * (1.0 + self.tp_pct)

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
            "Preparando LONG: contracts=%d | entry≈%.6f | SL=%s | TP=%s | clOrdId=%s",
            contracts, current_price, sl_str, tp_str, cl_ord,
        )

        if self.dry_run:
            log.warning("[DRY-RUN] No se envía orden. Configuración:")
            log.warning("  symbol=%s type=market side=buy amount=%d", self.symbol, contracts)
            log.warning("  attachAlgoOrds=%s", attach_algo)
            log.warning("  clOrdId=%s", cl_ord)
            return "DRY_RUN_SKIPPED"

        order = ex.create_order(
            symbol=self.symbol,
            type="market",
            side="buy",
            amount=contracts,
            params={
                "attachAlgoOrds": attach_algo,
                "clOrdId": cl_ord,
            },
        )

        entry_price = extract_entry_price(order, current_price)

        log.info(
            "Orden LONG ejecutada. ID=%s | entry≈%.6f | SL=%s | TP=%s",
            order.get("id"), entry_price, sl_str, tp_str,
        )
        return str(order.get("id"))

    def cycle(self) -> None:
        """
        Un ciclo del bucle:
            1) Leer velas (recortando última si abierta)
            2) Calcular EMA 7/21
            3) Detectar cruce
            4) Si golden cross → checks (idempotencia, cooldown, capacity) → orden
            5) Si death cross → modo observación
            6) Si no hay cruce → vigilar

        BUG-07 fix: cualquier excepción se captura y loggea SIN relanzar.
        """
        try:
            log.info("─" * 60)
            log.info("Ciclo iniciado para %s @ %s", self.symbol, self.timeframe)

            prev_ema7, prev_ema21, curr_ema7, curr_ema21, price, candle_ts = self.fetch_ema_state()
            log.info(
                "Precio=%.6f | EMA7=%.6f | EMA21=%.6f | prevEMA7=%.6f | prevEMA21=%.6f | ts=%d",
                price, curr_ema7, curr_ema21, prev_ema7, prev_ema21, candle_ts,
            )

            # ── Golden Cross ─────────────────────────────────────────────
            if prev_ema7 <= prev_ema21 and curr_ema7 > curr_ema21:
                log.info("★ Golden Cross (7/21) detectado → entrada LONG")

                if self.test_only:
                    log.info("[TEST_ONLY] Señal detectada pero NO se opera. Saldría LONG aquí.")
                    return

                ex = self._private_exchange()
                if self.instrument is None:
                    self.instrument = load_instrument_spec(ex, self.symbol)

                # Idempotencia: ¿ya operamos esta vela?
                if candle_already_traded(ex, self.symbol, candle_ts):
                    log.info("Vela %d ya tradeada. Skip.", candle_ts)
                    return

                # Configurar cuenta
                self._configure_account_for_trading(ex)

                # Cooldown anti revenge trading
                if cooldown_active(ex, self.symbol):
                    log.warning("Cooldown activo. Skip este ciclo.")
                    return

                # Capacity check
                contracts = calc_contracts(self.notional, price, self.instrument.ct_val)
                if not capacity_ok(ex, contracts, price, self.instrument):
                    log.warning("Sin capacidad (balance o notional). Skip.")
                    return

                # Ejecución
                order_id = self._place_long_with_algo(ex, contracts, price, candle_ts)
                log.info("Ciclo de riesgo completo. order_id=%s", order_id)

            # ── Death Cross (modo observación) ───────────────────────────
            elif prev_ema7 >= prev_ema21 and curr_ema7 < curr_ema21:
                log.info("▼ Death Cross (7/21) detectado. Motor short desactivado → observación.")

            # ── Sin cruce nuevo ──────────────────────────────────────────
            else:
                log.info("Sin cruces nuevos en este ciclo. Vigilando.")

        except (NetworkError, ExchangeError) as e:
            log.error("Error de exchange/red (se continúa): %s", e)
        except Exception as e:
            log.exception("Error inesperado (no aborta el bot): %s", e)

    def run_forever(self) -> None:
        log.info(
            "╔══════════════════════════════════════════════════════════╗"
            "\n║ inj_bot arrancando"
            "\n║ symbol=%s tf=%s lev=%dx notional=%.2f USD"
            "\n║ SL=-%.2f%% TP=+%.2f%% loop=%ds dry_run=%s region=%s type=%s",
            self.symbol, self.timeframe, self.leverage, self.notional,
            self.sl_pct * 100, self.tp_pct * 100, LOOP_SECONDS, self.dry_run,
            OKX_REGION, DEFAULT_TYPE,
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
