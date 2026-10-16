"""
inj_bot.py — Bot de trading EMA 7/21 para OKX Swap (futuros perp) sobre INJ/USD:INJ
====================================================================================

Estrategia:
    - Golden Cross EMA(7) sobre EMA(21) → entrada LONG
    - Death Cross EMA(7) bajo EMA(21) → señal short (modo observación por defecto)

Parámetros operativos:
    - Símbolo: INJ/USD:INJ (swap coin-margined, perp)
    - Timeframe: 15m
    - SL: -1.0% respecto a la entrada
    - TP: +1.5% respecto a la entrada
    - Leverage: 5x, margen aislado (isolated)
    - Notional: 50 USD objetivo
    - Bucle: cada 15 min (sleep 900 s)
    - Modo dry-run: activable con DRY_RUN=1 (no envía órdenes reales)

Bugs corregidos respecto al original:
    BUG-01 order['average'] puede ser None en OKX → fallback robusto
    BUG-02 OKX no acepta type=conditional con triggerPrice/stopLoss/takeProfit
            → se usa attachAlgoOrds con triggerPx/orderPx/ordType=conditional
    BUG-03 amount en contratos, no en unidades → cálculo con ctVal del instrument
    BUG-04 Falta set_leverage → se fija antes de cada entrada
    BUG-05 Posición en modo hedge → se fuerza one-way mode
    BUG-06 SL/TP sin redondear a tickSize → se carga tickSize y se redondea
    BUG-07 raise e mata el bot → se loggea y se reintenta en el siguiente ciclo

Autor: panda-trading-desk 🐼
Licencia: MIT
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import ccxt
import pandas as pd
from ccxt import ExchangeError, NetworkError


# ─────────────────────────────────────────────────────────────────────────────
# Configuración (todo vía variables de entorno con defaults sensatos)
# ─────────────────────────────────────────────────────────────────────────────

SYMBOL = os.getenv("BOT_SYMBOL", "INJ/USD:INJ")
TIMEFRAME = os.getenv("BOT_TIMEFRAME", "15m")
LEVERAGE = int(os.getenv("BOT_LEVERAGE", "5"))
NOTIONAL_USD = float(os.getenv("BOT_NOTIONAL", "50"))
SL_PCT = float(os.getenv("BOT_SL_PCT", "1.0")) # 1% de pérdida
TP_PCT = float(os.getenv("BOT_TP_PCT", "1.5")) # 1.5% de ganancia
LOOP_SECONDS = int(os.getenv("BOT_LOOP_SEC", "900")) # 15 min
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# Modos de ejecución rápida (para pruebas)
SINGLE_RUN = os.getenv("SINGLE_RUN", "0") == "1" # 1 = corre 1 ciclo y sale
TEST_ONLY = os.getenv("TEST_ONLY", "0") == "1" # 1 = solo lee velas, no opera

EMA_FAST = 7
EMA_SLOW = 21
CANDLE_LIMIT = 100


# ─────────────────────────────────────────────────────────────────────────────
# Logging (rotación simple, a stdout para que lo capture systemd/docker)
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

@dataclass
class InstrumentSpec:
    """Metadatos del contrato necesarios para dimensionar y redondear órdenes."""
    ct_val: float # tamaño de 1 contrato en moneda base
    tick_size: float # paso mínimo de precio
    price_precision: int # decimales de precio


def load_instrument_spec(ex: ccxt.okx, symbol: str) -> InstrumentSpec:
    """
    Carga ctVal, tickSize y price precision del mercado desde el exchange.

    BUG-03 fix: necesitamos ctVal para calcular correctamente el número
    de contratos a partir del notional objetivo en USD.

    BUG-06 fix: necesitamos tickSize y price precision para redondear
    SL/TP al grid válido y evitar rechazos del exchange.
    """
    markets = ex.load_markets(reload=True)
    mkt = markets[symbol]

    ct_val = float(mkt.get("contractSize") or mkt.get("ctVal") or 1.0)

    # tickSize viene como string en market info
    tick_raw = (
        mkt.get("tickSize")
        or mkt.get("precision", {}).get("price")
        or "0.0001"
    )
    tick_size = float(tick_raw)

    # price_precision: decimales derivados del tickSize
    # ej: tick 0.0001 → 4 decimales
    price_precision = max(0, -int(math.floor(math.log10(tick_size)))) if tick_size > 0 else 4

    log.info(
        "Instrument spec cargado: %s | ctVal=%s | tickSize=%s | pricePrecision=%d",
        symbol, ct_val, tick_size, price_precision,
    )
    return InstrumentSpec(ct_val=ct_val, tick_size=tick_size, price_precision=price_precision)


def round_to_tick(price: float, tick: float, precision: int) -> str:
    """
    Redondea un precio al tickSize del instrumento y lo devuelve como string
    con el número correcto de decimales (OKX exige string formateado).

    BUG-06 fix.
    """
    #_floor para SL (siempre un pelo más conservador) — aquí genérico
    stepped = math.floor(price / tick) * tick
    return f"{stepped:.{precision}f}"


def calc_contracts(notional_usd: float, price: float, ct_val: float) -> int:
    """
    Calcula el número entero de contratos a partir del notional objetivo.

    BUG-03 fix:
        contracts = notional_USD / (ct_val * price)
    En OKX swap, `amount` = número de contratos, no unidades de base.
    """
    if price <= 0 or ct_val <= 0:
        return 0
    raw = notional_usd / (ct_val * price)
    contracts = int(math.floor(raw))
    if contracts < 1:
        log.warning(
            "Notional %.2f USD demasiado pequeño para 1 contrato (raw=%.4f). "
            "Se usaría 1 contrato como mínimo si hay margen suficiente.",
            notional_usd, raw,
        )
        contracts = 1
    return contracts


def extract_entry_price(order: Dict[str, Any], fallback: float) -> float:
    """
    BUG-01 fix:
    OKX no siempre devuelve `average` en market fills (puede venir None).
    Aplicamos cadena de fallback: average → price → lastTradePrice → current.
    """
    for key in ("average", "price", "lastTradePrice"):
        val = order.get(key)
        if val:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return fallback
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
        self.ex_pub = ccxt.okx({
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })

        # Exchange privado (con credenciales) sólo se instancia si no es dry-run
        self.ex_priv: Optional[ccxt.okx] = None
        self.instrument: Optional[InstrumentSpec] = None

    # ── Exchange privado perezoso ─────────────────────────────────────────
    def _private_exchange(self) -> ccxt.okx:
        if self.ex_priv is not None:
            return self.ex_priv

        api_key = os.getenv("OKX_API_KEY")
        secret_key = os.getenv("OKX_SECRET_KEY")
        passphrase = os.getenv("OKX_PASSWORD")

        if not (api_key and secret_key and passphrase):
            raise RuntimeError(
                "Faltan credenciales OKX. Define OKX_API_KEY, OKX_SECRET_KEY "
                "y OKX_PASSWORD en el entorno (o usa DRY_RUN=1)."
            )

        self.ex_priv = ccxt.okx({
            "apiKey": api_key,
            "secret": secret_key,
            "password": passphrase,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })

        if self.dry_run:
            # OKX sandbox oficial (paper trading)
            self.ex_priv.set_sandbox_mode(True)
            log.warning("DRY_RUN activado → usando sandbox OKX (paper trading).")

        return self.ex_priv

    # ── Análisis de velas ────────────────────────────────────────────────
    def fetch_ema_state(self) -> Tuple[float, float, float, float, float]:
        """
        Devuelve (prev_ema7, prev_ema21, curr_ema7, curr_ema21, current_price).
        """
        ohlcv = self.ex_pub.fetch_ohlcv(self.symbol, self.timeframe, limit=CANDLE_LIMIT)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])

        df["ema7"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
        df["ema21"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

        prev_ema7 = df["ema7"].iloc[-2]
        prev_ema21 = df["ema21"].iloc[-2]
        curr_ema7 = df["ema7"].iloc[-1]
        curr_ema21 = df["ema21"].iloc[-1]
        current_price = float(df["close"].iloc[-1])

        return prev_ema7, prev_ema21, curr_ema7, curr_ema21, current_price

    # ── Configuración del exchange antes de operar ───────────────────────
    def _configure_account_for_trading(self, ex: ccxt.okx) -> None:
        """
        BUG-04 fix: set_leverage debe llamarse antes de create_order en swaps.
        BUG-05 fix: forzar one-way mode (no hedge) para que attachAlgoOrds
                      sepa a qué lado adherirse.
        """
        # One-way mode (long/short en la misma posición, no dual)
        try:
            ex.set_position_mode(False, True, self.symbol)
            log.info("Position mode → one-way para %s.", self.symbol)
        except ExchangeError as e:
            # OKX a veces ya está en one-way y devuelve error; es benigno.
            log.debug("set_position_mode benign skip: %s", e)

        # Set leverage (mgnMode=isolated)
        ex.set_leverage(
            self.leverage,
            self.symbol,
            params={"mgnMode": "isolated"},
        )
        log.info("Leverage fijado en %dx isolated para %s.", self.leverage, self.symbol)

    # ── Envío de orden con SL/TP adheridos ───────────────────────────────
    def _place_long_with_algo(self, ex: ccxt.okx, contracts: int, current_price: float) -> str:
        """
        BUG-02 fix:
        OKX NO soporta type='conditional' con triggerPrice/stopLoss/takeProfit
        en la API unificada de ccxt. En su lugar, hay que pasar attachAlgoOrds
        dentro de params al crear la orden market/limit principal.

        Estructura OKX nativa:
            attachAlgoOrds: [
              {
                slTriggerPx: "...", // precio que dispara el SL
                slOrdPx: "...", // precio límite del SL (market => "")
                tpTriggerPx: "...", // precio que dispara el TP
                tpOrdPx: "", // market => ""
              }
            ]
        """
        # Precios SL / TP
        sl_raw = current_price * (1.0 - self.sl_pct)
        tp_raw = current_price * (1.0 + self.tp_pct)

        sl_str = round_to_tick(sl_raw, self.instrument.tick_size, self.instrument.price_precision)
        tp_str = round_to_tick(tp_raw, self.instrument.tick_size, self.instrument.price_precision)

        attach_algo = [{
            # SL leg
            "slTriggerPx": sl_str,
            "slOrdPx": "", # market-style exit
            # TP leg
            "tpTriggerPx": tp_str,
            "tpOrdPx": "",
            # Cerrar toda la posición al ejecutarse el algo
            "closeFraction": "1",
        }]

        log.info(
            "Preparando orden LONG: contracts=%d | entry≈%.6f | SL=%s | TP=%s",
            contracts, current_price, sl_str, tp_str,
        )

        if self.dry_run:
            log.warning("[DRY-RUN] No se envía orden real. Configuración que se enviaría:")
            log.warning(" symbol=%s type=market side=buy amount=%d", self.symbol, contracts)
            log.warning(" attachAlgoOrds=%s", attach_algo)
            return "DRY_RUN_SKIPPED"

        order = ex.create_order(
            symbol=self.symbol,
            type="market",
            side="buy",
            amount=contracts,
            params={"attachAlgoOrds": attach_algo},
        )

        # BUG-01 fix: entry price con fallback robusto
        entry_price = extract_entry_price(order, current_price)

        log.info(
            "Orden LONG ejecutada. ID=%s | entry≈%.6f | SL=%s | TP=%s",
            order.get("id"), entry_price, sl_str, tp_str,
        )
        return str(order.get("id"))
    # ── Ciclo principal ──────────────────────────────────────────────────
    def cycle(self) -> None:
        """
        Un ciclo del bucle:
            1) Leer velas
            2) Calcular EMA 7/21
            3) Detectar cruce
            4) Si golden cross → abrir LONG con SL/TP adheridos
            5) Si death cross → log only (modo observación)
            6) Si no hay cruce → vigilar

        BUG-07 fix: cualquier excepción se captura y se loggea SIN relanzar,
        para que el bucle principal siga vivo.
        """
        try:
            log.info("─" * 60)
            log.info("Ciclo iniciado para %s @ %s", self.symbol, self.timeframe)

            prev_ema7, prev_ema21, curr_ema7, curr_ema21, price = self.fetch_ema_state()
            log.info(
                "Precio=%.6f | EMA7=%.6f | EMA21=%.6f | prevEMA7=%.6f | prevEMA21=%.6f",
                price, curr_ema7, curr_ema21, prev_ema7, prev_ema21,
            )

            # ── Golden Cross ─────────────────────────────────────────────
            if prev_ema7 <= prev_ema21 and curr_ema7 > curr_ema21:
                log.info("★ Golden Cross (7/21) detectado → entrada LONG")

                # Modo TEST_ONLY: NO operar, solo reportar
                if self.test_only:
                    log.info("[TEST_ONLY] Señal detectada pero NO se opera. Saliría LONG aquí.")
                    return

                ex = self._private_exchange()
                if self.instrument is None:
                    self.instrument = load_instrument_spec(ex, self.symbol)

                self._configure_account_for_trading(ex)

                contracts = calc_contracts(self.notional, price, self.instrument.ct_val)
                if contracts < 1:
                    log.warning(
                        "Notional %.2f USD insuficiente para abrir posición en %s a %.6f. Skip.",
                        self.notional, self.symbol, price,
                    )
                    return

                order_id = self._place_long_with_algo(ex, contracts, price)
                log.info("Ciclo de riesgo completo. order_id=%s", order_id)

            # ── Death Cross (modo observación) ───────────────────────────
            elif prev_ema7 >= prev_ema21 and curr_ema7 < curr_ema21:
                log.info("▼ Death Cross (7/21) detectado. Motor short desactivado → observación.")

            # ── Sin cruce nuevo ──────────────────────────────────────────
            else:
                log.info("Sin cruces nuevos en este ciclo. Vigilando.")

        except (NetworkError, ExchangeError) as e:
            # BUG-07 fix: no relanzar; el siguiente ciclo reintentará.
            log.error("Error de exchange/red en este ciclo (se continúa): %s", e)
        except Exception as e:
            # Cualquier otro error inesperado: loggear y seguir vivo.
            log.exception("Error inesperado en ciclo (no aborta el bot): %s", e)

    # ── Bucle infinito ───────────────────────────────────────────────────
    def run_forever(self) -> None:
        log.info(
            "╔══════════════════════════════════════════════════════════╗"
            "\n║ inj_bot arrancando "
            "\n║ symbol=%s tf=%s lev=%dx notional=%.2f USD "
            "\n║ SL=-%.2f%% TP=+%.2f%% loop=%ds dry_run=%s",
            self.symbol, self.timeframe, self.leverage, self.notional,
            self.sl_pct * 100, self.tp_pct * 100, LOOP_SECONDS, self.dry_run,
        )
        log.info("╚══════════════════════════════════════════════════════════╝")

        # Modo SINGLE_RUN: una sola pasada y salir (para tests)
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
