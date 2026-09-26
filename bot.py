"""
okx_bot.py — Bot de trading para OKX (my.okx.com), par por defecto XRP/USDC.
Todo en un solo archivo para que sea fácil de subir y mantener desde el móvil.

⚠️ ADVERTENCIA IMPORTANTE ⚠️
- Este bot opera con dinero real si PAPER_TRADING=False. Úsalo bajo tu
  propio riesgo. El trading algorítmico puede perder dinero rápidamente,
  especialmente con configuraciones sin probar.
- Por defecto arranca en modo PAPEL (simulado): no envía órdenes reales,
  solo registra en el log lo que HABRÍA hecho.
- Prueba primero en modo papel, revisa los logs varios días, y solo
  después considera activar dinero real con un monto pequeño.
- Este código es una plantilla educativa, no un sistema de trading
  probado ni una recomendación de inversión.

Requisitos:
    pip install -r requirements.txt   (ccxt, python-dotenv, pandas)

Configuración: copia .env.example a .env y completa tus datos
(o, si lo corres en GitHub Actions, usa Secrets del repo — ver README).

Ejecución:
    python okx_bot.py            # corre en loop infinito (uso local)
    python okx_bot.py --once     # un solo ciclo y termina (uso en cron/Actions)
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
from dotenv import load_dotenv

load_dotenv()  # si no hay archivo .env (ej. en GitHub Actions), simplemente no hace nada


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
    # --- Credenciales OKX ---
    api_key: str = os.getenv("OKX_API_KEY", "")
    api_secret: str = os.getenv("OKX_API_SECRET", "")
    api_password: str = os.getenv("OKX_API_PASSWORD", "")  # passphrase de la API

    # --- Mercado ---
    symbol: str = os.getenv("SYMBOL", "XRP/USDC")   # par a operar
    timeframe: str = os.getenv("TIMEFRAME", "15m")  # velas: 1m,5m,15m,1h,4h,1d...

    # --- Estrategia (cruce de medias móviles) ---
    sma_fast: int = int(os.getenv("SMA_FAST", "9"))
    sma_slow: int = int(os.getenv("SMA_SLOW", "21"))

    # --- Gestión de riesgo ---
    risk_per_trade_pct: float = _get_float("RISK_PER_TRADE_PCT", 2.0)   # % del balance en USDC por operación
    stop_loss_pct: float = _get_float("STOP_LOSS_PCT", 2.0)            # % por debajo del precio de entrada
    take_profit_pct: float = _get_float("TAKE_PROFIT_PCT", 4.0)        # % por encima del precio de entrada
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "1"))

    # --- Operación del bot ---
    poll_seconds: int = int(os.getenv("POLL_SECONDS", "60"))
    # ¡ojo! si el secret/variable queda vacío, esto usa el default seguro (True)
    paper_trading: bool = _get_bool("PAPER_TRADING", True)
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


config = Config()

logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("okx_bot")


# ====================================================================== #
# 2. ESTRATEGIA (cruce de medias móviles simples)
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
        return Signal.HOLD  # no hay suficientes velas todavía

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
# 3. GESTIÓN DE RIESGO (tamaño de posición, stop-loss, take-profit)
# ====================================================================== #
@dataclass
class PositionPlan:
    quote_amount: float   # cuánto USDC se va a usar en la compra
    base_amount: float    # cuánta cantidad del activo (ej. XRP) se compra
    stop_loss_price: float
    take_profit_price: float


def build_position_plan(
    entry_price: float,
    available_quote_balance: float,
    risk_per_trade_pct: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> PositionPlan:
    if entry_price <= 0:
        raise ValueError("entry_price debe ser mayor que 0")

    quote_amount = available_quote_balance * (risk_per_trade_pct / 100.0)
    base_amount = quote_amount / entry_price

    stop_loss_price = entry_price * (1 - stop_loss_pct / 100.0)
    take_profit_price = entry_price * (1 + take_profit_pct / 100.0)

    return PositionPlan(
        quote_amount=quote_amount,
        base_amount=base_amount,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
    )


# ====================================================================== #
# 4. BOT (conexión con OKX vía ccxt y loop principal)
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

    def _build_exchange(self) -> ccxt.okx:
        exchange = ccxt.okx(
            {
                "apiKey": config.api_key,
                "secret": config.api_secret,
                "password": config.api_password,  # passphrase de OKX
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

    def fetch_quote_balance(self) -> float:
        """Balance disponible en la moneda quote (USDC)."""
        if config.paper_trading:
            # En modo papel usamos un balance simulado fijo; cámbialo si quieres.
            return 1000.0
        balance = self.exchange.fetch_balance()
        quote_ccy = self.symbol.split("/")[1]
        return float(balance.get("free", {}).get(quote_ccy, 0.0))

    def place_buy(self, price: float):
        quote_balance = self.fetch_quote_balance()
        plan = build_position_plan(
            entry_price=price,
            available_quote_balance=quote_balance,
            risk_per_trade_pct=config.risk_per_trade_pct,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
        )

        log.info(
            "SEÑAL DE COMPRA | precio=%.6f | monto=%.2f USDC | cantidad=%.6f | "
            "stop_loss=%.6f | take_profit=%.6f",
            price, plan.quote_amount, plan.base_amount, plan.stop_loss_price, plan.take_profit_price,
        )

        if config.paper_trading:
            log.info("[PAPEL] Orden de compra simulada, no enviada a OKX.")
        else:
            order = self.exchange.create_order(
                symbol=self.symbol,
                type="market",
                side="buy",
                amount=plan.base_amount,
            )
            log.info("Orden de compra REAL enviada: %s", order.get("id"))

        self.in_position = True
        self.entry_price = price
        self.stop_loss_price = plan.stop_loss_price
        self.take_profit_price = plan.take_profit_price

    def place_sell(self, price: float, reason: str):
        log.info("SEÑAL DE VENTA (%s) | precio=%.6f", reason, price)

        if config.paper_trading:
            log.info("[PAPEL] Orden de venta simulada, no enviada a OKX.")
        else:
            balance = self.exchange.fetch_balance()
            base_ccy = self.symbol.split("/")[0]
            amount = float(balance.get("free", {}).get(base_ccy, 0.0))
            if amount > 0:
                order = self.exchange.create_order(
                    symbol=self.symbol, type="market", side="sell", amount=amount
                )
                log.info("Orden de venta REAL enviada: %s", order.get("id"))
            else:
                log.warning("No hay balance del activo base para vender.")

        self.in_position = False
        self.entry_price = None
        self.stop_loss_price = None
        self.take_profit_price = None

    def check_stop_loss_take_profit(self, current_price: float):
        if not self.in_position:
            return
        if current_price <= self.stop_loss_price:
            self.place_sell(current_price, reason="stop-loss")
        elif current_price >= self.take_profit_price:
            self.place_sell(current_price, reason="take-profit")

    def run_once(self):
        df = self.fetch_candles()
        current_price = float(df["close"].iloc[-1])

        # Primero revisa si hay que cerrar la posición abierta por SL/TP
        self.check_stop_loss_take_profit(current_price)

        if self.in_position:
            log.debug("En posición, precio actual=%.6f", current_price)
            return

        signal_ = compute_signal(df, config.sma_fast, config.sma_slow)
        log.info("Precio=%.6f | señal=%s", current_price, signal_.value)

        if signal_ == Signal.BUY:
            self.place_buy(current_price)
        # Señal SELL sin posición abierta no hace nada (nada que vender)

    def run(self):
        """Loop infinito — para uso local, NO para GitHub Actions."""
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        log.info(
            "Bot iniciado | símbolo=%s | timeframe=%s | paper_trading=%s",
            self.symbol, config.timeframe, config.paper_trading,
        )

        while self._running:
            try:
                self.run_once()
            except ccxt.NetworkError as e:
                log.warning("Error de red, reintentando: %s", e)
            except ccxt.ExchangeError as e:
                log.error("Error del exchange: %s", e)
            except Exception as e:  # noqa: BLE001
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
        help="Ejecuta un solo ciclo de revisión/trading y termina "
        "(ideal para GitHub Actions con cron, en vez de dejarlo corriendo).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    bot = OkxTradingBot()
    try:
        if args.once:
            log.info("Modo --once: un solo ciclo y salida (uso típico: GitHub Actions).")
            bot.run_once()
        else:
            bot.run()
    except KeyboardInterrupt:
        sys.exit(0)
