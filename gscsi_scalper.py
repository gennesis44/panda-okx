#!/usr/bin/env python3
"""GSCSI scalper BTC/USDT OKX spot. Keys solo por Secrets. Un ciclo por run."""

from __future__ import annotations

import os
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | SCALP | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scalp")


def first(*names):
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def flag(name, default="0"):
    return (first(name) or default) == "1"


def main():
    try:
        import ccxt
    except ImportError:
        log.error("Falta ccxt. El workflow debe hacer: pip install ccxt")
        return 1

    key = first("OKX_API_KEY", "OKX_KEY", "API_KEY", "EXCHANGE_API_KEY")
    secret = first(
        "OKX_API_SECRET",
        "OKX_SECRET",
        "OKX_SECRET_KEY",
        "API_SECRET",
        "EXCHANGE_API_SECRET",
    )
    password = first(
        "OKX_PASSPHRASE",
        "OKX_PASSWORD",
        "PASSPHRASE",
        "EXCHANGE_PASSPHRASE",
    )

    bits = []
    if key:
        bits.append("key")
    if secret:
        bits.append("secret")
    if password:
        bits.append("passphrase")
    log.info("secrets visibles: %s", ",".join(bits) or "NINGUNA")

    if not key or not secret or not password:
        log.error("OKX necesita 3 secrets. Usa los mismos nombres que el yml de futuros.")
        return 2

    symbol = first("SYMBOL") or "BTC/USDT"
    quote = float(first("QUOTE_AMOUNT") or "20")
    rsi_len = int(first("RSI_PERIOD") or "14")
    rsi_buy = float(first("RSI_OVERSOLD") or "32")
    rsi_sell = float(first("RSI_OVERBOUGHT") or "68")
    live = flag("LIVE_TRADING")
    testnet = flag("TESTNET")

    exchange = ccxt.okx(
        {
            "apiKey": key,
            "secret": secret,
            "password": password,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )
    if testnet:
        exchange.set_sandbox_mode(True)
        log.info("sandbox ON")

    exchange.load_markets()
    if symbol not in exchange.markets:
        log.error("Par no existe: %s", symbol)
        return 3

    ticker = exchange.fetch_ticker(symbol)
    price = float(ticker["last"])
    candles = exchange.fetch_ohlcv(symbol, first("TIMEFRAME") or "1m", limit=rsi_len + 6)
    closes = [c[4] for c in candles]

    rsi = None
    if len(closes) >= rsi_len + 1:
        gains = []
        losses = []
        for i in range(len(closes) - rsi_len, len(closes)):
            d = closes[i] - closes[i - 1]
            gains.append(d if d > 0 else 0.0)
            losses.append(-d if d < 0 else 0.0)
        ag = sum(gains) / rsi_len
        al = sum(losses) / rsi_len
        rsi = 100.0 if al == 0 else 100.0 - (100.0 / (1.0 + ag / al))

    bal = exchange.fetch_balance()
    btc = float((bal.get("total") or {}).get("BTC") or 0)
    usdt = float((bal.get("total") or {}).get("USDT") or 0)
    in_pos = btc >= 0.00005

    log.info(
        "BTC/USDT px=%.2f rsi=%s btc=%.6f usdt=%.2f live=%s",
        price,
        ("%.1f" % rsi) if rsi is not None else "n/a",
        btc,
        usdt,
        live,
    )

    if rsi is None:
        log.info("sin RSI, fin")
        return 0

    side = None
    if in_pos and rsi >= rsi_sell:
        side = "sell"
    elif (not in_pos) and rsi <= rsi_buy:
        side = "buy"
    else:
        log.info("sin senal")
        return 0

    amount = float(exchange.amount_to_precision(symbol, quote / price))
    log.info("senal %s amount=%s", side, amount)
    if not live:
        log.info("PAPER — no se envia orden")
        return 0

    order = exchange.create_market_order(symbol, side, amount)
    log.info("LIVE id=%s", order.get("id"))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:
        log.exception("fallo: %s", exc)
        code = 12
    sys.exit(code)
