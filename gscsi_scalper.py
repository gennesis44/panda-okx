#!/usr/bin/env python3
"""GSCSI scalper BTC/USDT — mismas Secrets que PANDA. Demo FLAG=1."""

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
            return str(v).strip().strip('"').strip("'")
    return ""


def main():
    import ccxt

    key = first("OKX_API_KEY")
    secret = first("OKX_API_SECRET")
    password = first("OKX_PASSPHRASE")
    flag = first("OKX_FLAG") or "1"
    live = (first("LIVE_TRADING") or "0").lower() in ("1", "true", "yes")
    symbol = first("SYMBOL") or "BTC/USDT:USDT"
    quote = float(first("QUOTE_AMOUNT") or "20")
    rsi_len = 14
    rsi_buy = 32.0
    rsi_sell = 68.0

    log.info("flag=%s live=%s symbol=%s secrets=%s", flag, live, symbol, bool(key and secret and password))
    if not key or not secret or not password:
        log.error("Faltan OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE")
        return 2

    headers = {}
    if flag == "1":
        headers["x-simulated-trading"] = "1"
        log.info("OKX DEMO header ON")

    exchange = ccxt.okx(
        {
            "apiKey": key,
            "secret": secret,
            "password": password,
            "enableRateLimit": True,
            "headers": headers,
            "options": {"defaultType": "swap"},
        }
    )
    exchange.fetch_currencies = lambda params={}: {}
    exchange.load_markets()

    if symbol not in exchange.markets:
        log.error("Par no existe: %s", symbol)
        return 3

    price = float(exchange.fetch_ticker(symbol)["last"])
    candles = exchange.fetch_ohlcv(symbol, "1m", limit=rsi_len + 6)
    closes = [c[4] for c in candles]
    rsi = None
    if len(closes) >= rsi_len + 1:
        gains, losses = [], []
        for i in range(len(closes) - rsi_len, len(closes)):
            d = closes[i] - closes[i - 1]
            gains.append(d if d > 0 else 0.0)
            losses.append(-d if d < 0 else 0.0)
        ag = sum(gains) / rsi_len
        al = sum(losses) / rsi_len
        rsi = 100.0 if al == 0 else 100.0 - (100.0 / (1.0 + ag / al))

    bal = exchange.fetch_balance()
    usdt = float((bal.get("total") or {}).get("USDT") or 0)
    log.info("px=%.2f rsi=%s usdt_demo=%.2f", price, ("%.1f" % rsi) if rsi is not None else "n/a", usdt)

    if rsi is None:
        return 0

    positions = exchange.fetch_positions([symbol])
    in_pos = False
    for p in positions:
        contracts = float(p.get("contracts") or 0)
        if contracts > 0:
            in_pos = True
            break

    side = None
    if in_pos and rsi >= rsi_sell:
        side = "sell"
    elif (not in_pos) and rsi <= rsi_buy:
        side = "buy"
    else:
        log.info("sin senal in_pos=%s", in_pos)
        return 0

    amount = float(exchange.amount_to_precision(symbol, quote / price))
    log.info("senal %s amount=%s", side, amount)
    if not live:
        log.info("PAPER — no se envia orden")
        return 0

    order = exchange.create_market_order(symbol, side, amount)
    log.info("LIVE-DEMO id=%s", order.get("id"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log.exception("fallo: %s", exc)
        raise SystemExit(12)
