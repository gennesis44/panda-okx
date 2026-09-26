"""
xrp_sma_bot.py — Bot de trading para OKX, par XRP/USDC.
Versión corregida: detecta posición por balance + manejo seguro si falta la passphrase.
Solo 1 posición activa de 3 contratos.
"""

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from enum import Enum

import ccxt
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ====================================================================== #
# 1. CONFIGURACIÓN
# ====================================================================== #
def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default


@dataclass
class Config:
    api_key: str = os.getenv("OKX_API_KEY", "")
    api_secret: str = os.getenv("OKX_API_SECRET", "")
    api_password: str = os.getenv("OKX_API_PASSWORD", "")

    symbol: str = os.getenv("SYMBOL", "XRP/USDC")
    timeframe: str = os.getenv("TIMEFRAME", "15m")

    sma_fast: int = int(os.getenv("SMA_FAST", "9"))
    sma_slow: int = int(os.getenv("SMA_SLOW", "21"))

    risk_per_trade_pct: float = _get_float("RISK_PER_TRADE_PCT", 2.0)
    stop_loss_pct: float = _get_float("STOP_LOSS_PCT", 2.0)
    take_profit_pct: float = _get_float("TAKE_PROFIT_PCT", 4.0)
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "1"))

    contract_amount: float = _get_float("CONTRACT_AMOUNT", 3.0)
    min_position_threshold: float = 1.5

    poll_seconds: int = int(os.getenv("POLL_SECONDS", "60"))
    paper_trading: bool = _get_bool("PAPER_TRADING", True)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


config = Config()

logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("okx_bot")


# ====================================================================== #
# 2. ESTRATEGIA
# ====================================================================== #
class Signal(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


def ohlcv_to_dataframe(ohlcv: list) -> pd.DataFrame:
    df = pd.DataFrame(
        ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def compute_signal(df: pd.DataFrame, fast: int, slow: int) -> Signal:
    if len(df) < slow + 2:
        return Signal.HOLD

    df = df.copy()
    df["sma_fast"] = df["close"].rolling(fast).mean()
    df["sma_slow"] = df["close"].rolling(slow).mean()

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    crossed_up = prev["sma_fast"] <= prev["sma_slow"] and curr["sma_fast"] > curr["sma_slow"]
    crossed_down = prev["sma_fast"] >= prev["sma_slow"] and curr["sma_fast"] < curr["sma_slow"]

    if crossed_up:
        return Signal.BUY
    if crossed_down:
        return Signal.SELL
    return Signal.HOLD


# ====================================================================== #
# 3. PLAN DE POSICIÓN
# ====================================================================== #
@dataclass
class PositionPlan:
    quote_amount: float
    base_amount: float
    stop_loss_price: float
    take_profit_price: float


def build_fixed_position_plan(
    entry_price: float,
    contract_amount: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> PositionPlan:
    if entry_price <= 0:
        raise ValueError("entry_price debe ser mayor que 0")

    base_amount = contract_amount
    quote_amount = base_amount * entry_price

    stop_loss_price = entry_price * (1 - stop_loss_pct / 100.0)
    take_profit_price = entry_price * (1 + take_profit_pct / 100.0)

    return PositionPlan(
        quote_amount=quote_amount,
        base_amount=base_amount,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
    )


# ====================================================================== #
# 4. BOT
# ====================================================================== #
class OkxTradingBot:
    def __init__(self):
        self.exchange = self._build_exchange()
        self.symbol = config.symbol
        self.in_position = False
        self.entry_price = None
        self.stop_loss_price = None
        self.take_profit_price = None
        self._running = True
        self._can_check_balance = True  # se pone False si falta la passphrase

    def _build_exchange(self) -> ccxt.okx:
        exchange = ccxt.okx(
            {
                "apiKey": config.api_key,
                "secret": config.api_secret,
                "password": config.api_password,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        if config.paper_trading:
            log.info("Modo PAPEL activo: no se enviarán órdenes reales a OKX.")
        else:
            log.warning("Modo REAL activo: el bot puede colocar órdenes con dinero de verdad.")
        return exchange

    def stop(self, *_):
        log.info("Señal de apagado recibida. Cerrando el bot...")
        self._running = False

    def fetch_candles(self) -> pd.DataFrame:
        ohlcv = self.exchange.fetch_ohlcv(
            self.symbol, timeframe=config.timeframe, limit=max(config.sma_slow + 5, 50)
        )
        return ohlcv_to_dataframe(ohlcv)

    def fetch_base_balance(self) -> float:
        """Devuelve el balance libre de XRP. Si falla autenticación, devuelve 0."""
        if config.paper_trading:
            return 0.0
        try:
            balance = self.exchange.fetch_balance()
            base_ccy = self.symbol.split("/")[0]
            return float(balance.get("free", {}).get(base_ccy, 0.0))
        except ccxt.AuthenticationError as e:
            log.error(
                "Error de autenticación (falta OKX_API_PASSWORD o es incorrecta): %s", e
            )
            self._can_check_balance = False
            return 0.0
        except Exception as e:
            log.warning("No se pudo leer balance: %s", e)
            return 0.0

    def sync_position_from_exchange(self):
        """Detecta si hay posición abierta mirando el balance real."""
        if not self._can_check_balance:
            return

        free_base = self.fetch_base_balance()
        if free_base >= config.min_position_threshold:
            self.in_position = True
            log.info("Posición activa detectada por balance: %.4f XRP", free_base)
        else:
            self.in_position = False
            self.entry_price = None
            self.stop_loss_price = None
            self.take_profit_price = None

    def place_buy(self, price: float):
        if self.in_position:
            log.warning("Intento de compra bloqueado: ya hay una posición activa.")
            return

        plan = build_fixed_position_plan(
            entry_price=price,
            contract_amount=config.contract_amount,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
        )

        log.info(
            "SEÑAL DE COMPRA | precio=%.6f | cantidad=%.4f XRP | monto≈%.2f USDC | SL=%.6f | TP=%.6f",
            price, plan.base_amount, plan.quote_amount,
            plan.stop_loss_price, plan.take_profit_price,
        )

        if config.paper_trading:
            log.info("[PAPEL] Orden de compra simulada.")
        else:
            try:
                order = self.exchange.create_order(
                    symbol=self.symbol,
                    type="market",
                    side="buy",
                    amount=plan.base_amount,
                )
                log.info("Orden de compra REAL enviada: %s", order.get("id"))
            except ccxt.AuthenticationError as e:
                log.error("No se pudo enviar orden (falta passphrase): %s", e)
                return

        self.in_position = True
        self.entry_price = price
        self.stop_loss_price = plan.stop_loss_price
        self.take_profit_price = plan.take_profit_price

    def place_sell(self, price: float, reason: str):
        log.info("SEÑAL DE VENTA (%s) | precio=%.6f", reason, price)

        if config.paper_trading:
            log.info("[PAPEL] Orden de venta simulada.")
        else:
            amount = self.fetch_base_balance()
            if amount < 0.1:
                log.warning("No hay suficiente XRP para vender (%.4f).", amount)
                self.in_position = False
                return
            try:
                order = self.exchange.create_order(
                    symbol=self.symbol,
                    type="market",
                    side="sell",
                    amount=amount,
                )
                log.info("Orden de venta REAL enviada: %s | cantidad=%.4f", order.get("id"), amount)
            except ccxt.AuthenticationError as e:
                log.error("No se pudo enviar orden de venta (falta passphrase): %s", e)
                return

        self.in_position = False
        self.entry_price = None
        self.stop_loss_price = None
        self.take_profit_price = None

    def check_stop_loss_take_profit(self, current_price: float):
        if not self.in_position or self.stop_loss_price is None:
            return
        if current_price <= self.stop_loss_price:
            self.place_sell(current_price, reason="stop-loss")
        elif current_price >= self.take_profit_price:
            self.place_sell(current_price, reason="take-profit")

    def run_once(self):
        # Sincronizar posición (si es posible)
        self.sync_position_from_exchange()

        df = self.fetch_candles()
        current_price = float(df["close"].iloc[-1])

        self.check_stop_loss_take_profit(current_price)

        if self.in_position:
            log.info("Posición activa | precio=%.6f | esperando cierre", current_price)
            signal_ = compute_signal(df, config.sma_fast, config.sma_slow)
            if signal_ == Signal.SELL:
                self.place_sell(current_price, reason="cruce bajista")
            return

        signal_ = compute_signal(df, config.sma_fast, config.sma_slow)
        log.info("Precio=%.6f | señal=%s | sin posición", current_price, signal_.value)

        if signal_ == Signal.BUY:
            self.place_buy(current_price)

    def run(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        log.info(
            "Bot iniciado | símbolo=%s | timeframe=%s | paper=%s | contratos=%.1f",
            self.symbol, config.timeframe, config.paper_trading, config.contract_amount,
        )

        while self._running:
            try:
                self.run_once()
            except ccxt.NetworkError as e:
                log.warning("Error de red: %s", e)
            except ccxt.ExchangeError as e:
                log.error("Error del exchange: %s", e)
            except Exception as e:
                log.exception("Error inesperado: %s", e)

            time.sleep(config.poll_seconds)

        log.info("Bot detenido.")


# ====================================================================== #
# 5. PUNTO DE ENTRADA
# ====================================================================== #
def parse_args():
    parser = argparse.ArgumentParser(description="Bot de trading OKX")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Ejecuta un solo ciclo y termina.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    bot = OkxTradingBot()
    try:
        if args.once:
            log.info("Modo --once: un solo ciclo y salida.")
            bot.run_once()
        else:
            bot.run()
    except KeyboardInterrupt:
        sys.exit(0)
