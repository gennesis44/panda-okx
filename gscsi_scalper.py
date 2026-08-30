#!/usr/bin/env python3
"""GSCSI scalper BTC-USDT-SWAP. Firma OKX. Prueba key 32 y UUID. FLAG 1 y 0."""

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


def clean(v):
    if v is None:
        return ""
    s = str(v).replace("\ufeff", "")
    s = s.strip().strip('"').strip("'")
    return "".join(s.split())


def env(name, default=""):
    return clean(os.getenv(name, default)) or default


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sign(secret, timestamp, method, path, body=""):
    msg = timestamp + method + path + body
    return base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()


def call(key, secret, passphrase, flag, path):
    t = ts()
    h = {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": sign(secret, t, "GET", path),
        "OK-ACCESS-TIMESTAMP": t,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    if flag == "1":
        h["x-simulated-trading"] = "1"
    r = requests.get(BASE + path, headers=h, timeout=20)
    return r.json()


def variants(raw):
    raw = raw.replace("-", "")
    out = [raw]
    if len(raw) == 32:
        out.append(
            "%s-%s-%s-%s-%s"
            % (raw[0:8], raw[8:12], raw[12:16], raw[16:20], raw[20:32])
        )
    return out


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
    secret = env("OKX_API_SECRET").replace("-", "")
    passphrase = env("OKX_PASSPHRASE")
    inst = env("INST_ID") or "BTC-USDT-SWAP"

    log.info(
        "key_len=%s secret_len=%s pass_len=%s inst=%s",
        len(key.replace("-", "")),
        len(secret),
        len(passphrase),
        inst,
    )
    if not key or not secret or not passphrase:
        log.error("Faltan secrets")
        return 2

    pub = requests.get(
        BASE + "/api/v5/market/candles?instId=%s&bar=1m&limit=20" % inst,
        timeout=20,
    ).json()
    if str(pub.get("code")) != "0":
        log.error("velas: %s", pub)
        return 3
    closes = [float(c[4]) for c in reversed(pub["data"])]
    price = closes[-1]
    r = rsi14(closes)
    log.info("px=%.2f rsi=%s", price, ("%.1f" % r) if r is not None else "n/a")

    ok = None
    used = None
    for k in variants(key):
        for flag in ("1", "0"):
            data = call(k, secret, passphrase, flag, "/api/v5/account/balance")
            log.info(
                "try key_len=%s flag=%s code=%s msg=%s",
                len(k),
                flag,
                data.get("code"),
                data.get("msg"),
            )
            if str(data.get("code")) == "0":
                ok = data
                used = flag
                break
        if ok is not None:
            break

    if ok is None:
        log.error("Ningun intento AUTH. Trio GitHub != API viva en OKX.")
        log.info("PAPER publico sigue OK")
        return 0

    usdt = 0.0
    for acct in ok.get("data") or []:
        for d in acct.get("details") or []:
            if d.get("ccy") == "USDT":
                usdt = float(d.get("eq") or 0)
    log.info("AUTH OK flag=%s usdt_eq=%.2f", used, usdt)
    log.info("PAPER — no orden")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log.exception("fallo: %s", exc)
        raise SystemExit(12)
