#!/usr/bin/env python3
"""GSCSI scalper — misma firma OKX que PANDA (FLAG=1 demo). Sin ccxt."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import sys
from datetime import datetime, timezone

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | SCALP | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scalp")

BASE = "https://www.okx.com"


def env(*names, default=""):
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip():
            return str(v).strip().strip('"').strip("'")
    return default


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sign(secret, timestamp, method, path, body=""):
    msg = timestamp + method + path + body
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()


class Okx:
    def __init__(self, key, secret, passphrase, flag="1"):
        self.key = key
        self.secret = secret
        self.passphrase = passphrase
        self.flag = flag

    def headers(self, timestamp, method, path, body=""):
        h = {
            "OK-ACCESS-KEY": self.key,
            "OK-ACCESS-SIGN": sign(self.secret, timestamp, method, path, body),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.flag == "1":
            h["x-simulated-trading"] = "1"
        return h

    def get(self, path):
        t = ts()
        r = requests.get(BASE + path, headers=self.headers(t, "GET", path), timeout=20)
        data = r.json()
        if str(data.get("code")) != "0":
            raise RuntimeError("OKX %s %s" % (path, data))
        return data.get("data")

    def public(self, path):
        r = requests.get(BASE + path, timeout=20)
        data = r.json()
        if str(data.get("code")) != "0":
            raise RuntimeError("OKX public %s %s" % (path, data))
        return data.get("data")


def rsi14(closes):
    if len(closes) < 15:
        return None
    gains, losses = [], []
    for i in range(len(closes) - 14, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    ag = sum(gains) / 14
    al = sum(losses) / 14
    if al == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + ag / al))


def main():
    key = env("OKX_API_KEY")
    secret = env("OKX_API_SECRET")
    passphrase = env("OKX_PASSPHRASE")
    flag = env("OKX_FLAG", default="1")
    inst = env("INST_ID", default="BTC-USDT-SWAP")
    live = env("LIVE_TRADING", default="0").lower() in ("1", "true", "yes")

    log.info("flag=%s live=%s inst=%s key_len=%s", flag, live, inst, len(key))
    if not key or not secret or not passphrase:
        log.error("Faltan secrets")
        return 2

    api = Okx(key, secret, passphrase, flag)

    candles = api.public("/api/v5/market/candles?instId=%s&bar=1m&limit=20" % inst)
    closes = [float(c[4]) for c in reversed(candles)]
    price = closes[-1]
    r = rsi14(closes)
    log.info("px=%.2f rsi=%s", price, ("%.1f" % r) if r is not None else "n/a")

    bal = api.get("/api/v5/account/balance")
    usdt = 0.0
    for acct in bal or []:
        for d in acct.get("details") or []:
            if d.get("ccy") == "USDT":
                usdt = float(d.get("eq") or d.get("cashBal") or 0)
    log.info("usdt_eq=%.2f DEMO=%s", usdt, flag == "1")

    pos = api.get("/api/v5/account/positions?instId=%s" % inst)
    contracts = 0.0
    for p in pos or []:
        contracts += abs(float(p.get("pos") or 0))
    log.info("pos=%.4f", contracts)

    if r is None:
        return 0
    if contracts > 0 and r >= 68:
        log.info("senal CLOSE rsi")
    elif contracts <= 0 and r <= 32:
        log.info("senal OPEN rsi")
    else:
        log.info("sin senal")
    log.info("PAPER — no orden")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log.exception("fallo: %s", exc)
        raise SystemExit(12)
