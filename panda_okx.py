#!/usr/bin/env python3
"""
PANDA GSCSI LIVE — modo agresivo paper/demo
Abre y cierra entradas en tiempo real.
NO usa capital real por defecto.

Modos:
  PAPER  = simula fills localmente y muestra actividad
  DEMO   = envía órdenes a OKX Simulated Trading (x-simulated-trading=1)
  LIVE   = solo si LIVE_TRADING=true y OKX_FLAG=0  (no recomendado)

Uso:
  python panda_gscsi_live.py
  python panda_gscsi_live.py --interval 60
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# ---------------------------------------------------------------------------
# Parámetros AGRESIVOS (abrir y cerrar rápido)
# ---------------------------------------------------------------------------
INST_ID = "BTC-USDT-SWAP"
STOP_PCT = 0.008          # 0.80%
TP_R = 1.00               # 0.80% de objetivo
MIN_SCORE = 4.5
MAX_HOLD_SECONDS = 60 * 60
CHECK_INTERVAL = 60
ATR_PCT_HARD_CAP = 0.020
CHOP_RANGE_ATR_MULT = 1.5
FEE_ROUNDTRIP = 0.0010    # 0.10% taker+taker estimado
DEFAULT_LEVERAGE = 3
MAX_LEVERAGE = 5
PAPER_EQUITY = float(os.getenv("ACCOUNT_EQUITY_USDT", "1000"))
RISK_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))
STATE_FILE = "panda_live_state.json"
TRADES_FILE = "panda_live_trades.csv"


# ---------------------------------------------------------------------------
# Indicadores
# ---------------------------------------------------------------------------
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()

def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = -d.clip(upper=0.0)
    au = up.ewm(alpha=1 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False).mean()
    rs = au / ad.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()

def macd(close: pd.Series, fast=12, slow=26, sig=9):
    m = ema(close, fast) - ema(close, slow)
    s = ema(m, sig)
    return m, s, m - s

def bollinger(close: pd.Series, n=20, k=2.0):
    mid = sma(close, n)
    sd = close.rolling(n).std()
    return mid, mid + k * sd, mid - k * sd

def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = atr(df, n)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()

def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.Series:
    a = atr(df, n).to_numpy()
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy()
    close = df["close"].to_numpy()
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    direction = np.ones(len(df), dtype=int)
    f_upper, f_lower = upper.copy(), lower.copy()
    for i in range(1, len(df)):
        f_lower[i] = max(lower[i], f_lower[i - 1]) if direction[i - 1] == 1 else lower[i]
        f_upper[i] = min(upper[i], f_upper[i - 1]) if direction[i - 1] == -1 else upper[i]
        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < f_lower[i] else 1
        else:
            direction[i] = 1 if close[i] > f_upper[i] else -1
    return pd.Series(direction, index=df.index)

def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema21"] = ema(out["close"], 21)
    out["ema55"] = ema(out["close"], 55)
    out["ema200"] = ema(out["close"], min(200, max(len(out), 2)))
    out["rsi"] = rsi(out["close"])
    out["atr"] = atr(out)
    out["atr_pct"] = out["atr"] / out["close"]
    out["adx"] = adx(out)
    _, _, hist = macd(out["close"])
    out["macd_hist"] = hist
    mid, up, lo = bollinger(out["close"])
    out["bb_mid"], out["bb_up"], out["bb_lo"] = mid, up, lo
    out["vol_sma"] = sma(out["volume"], 20)
    try:
        out["st_dir"] = supertrend(out)
    except Exception:
        out["st_dir"] = 0
    return out


# ---------------------------------------------------------------------------
# Cliente OKX
# ---------------------------------------------------------------------------
class OkxClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("OKX_API_KEY", "")
        self.api_secret = os.getenv("OKX_API_SECRET", "")
        self.passphrase = os.getenv("OKX_PASSPHRASE", "")
        self.flag = os.getenv("OKX_FLAG", "1")  # 1 = demo, 0 = live
        self.base = "https://www.okx.com"

    def has_keys(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)

    def _ts(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        import base64
        msg = f"{ts}{method}{path}{body}"
        mac = hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def public(self, path: str, params: Optional[dict] = None) -> dict:
        r = requests.get(self.base + path, params=params or {}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if str(data.get("code")) != "0":
            raise RuntimeError(f"OKX public {path}: {data}")
        return data

    def private(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        if not self.has_keys():
            raise RuntimeError("Faltan claves OKX")
        body_str = json.dumps(body) if body and method != "GET" else ""
        qs = ""
        if method == "GET" and body:
            qs = "?" + urlencode(body)
            body_str = ""
        ts = self._ts()
        sign = self._sign(ts, method, path + qs, body_str)
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "x-simulated-trading": self.flag,
        }
        url = self.base + path + qs
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=20)
        else:
            r = requests.post(url, headers=headers, data=body_str, timeout=20)
        r.raise_for_status()
        data = r.json()
        if str(data.get("code")) != "0":
            raise RuntimeError(f"OKX private {path}: {data}")
        return data

    def candles(self, bar: str, limit: int = 200) -> pd.DataFrame:
        raw = self.public("/api/v5/market/candles", {"instId": INST_ID, "bar": bar, "limit": str(limit)})["data"]
        rows = [{
            "ts": pd.to_datetime(int(x[0]), unit="ms", utc=True),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "volume": float(x[5]),
        } for x in raw]
        return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)

    def ticker(self) -> dict:
        return self.public("/api/v5/market/ticker", {"instId": INST_ID})["data"][0]

    def funding(self) -> dict:
        return self.public("/api/v5/public/funding-rate", {"instId": INST_ID})["data"][0]

    def instrument(self) -> dict:
        data = self.public("/api/v5/public/instruments", {"instType": "SWAP", "instId": INST_ID})["data"]
        return data[0] if data else {}

    def set_leverage(self, lever: str, pos_side: str = "") -> dict:
        body = {"instId": INST_ID, "lever": lever, "mgnMode": "cross"}
        if pos_side:
            body["posSide"] = pos_side
        return self.private("POST", "/api/v5/account/set-leverage", body)

    def place_order(self, payload: dict) -> dict:
        return self.private("POST", "/api/v5/trade/order", payload)

    def close_market(self, side: str, sz: str, pos_side: str) -> dict:
        payload = {
            "instId": INST_ID,
            "tdMode": "cross",
            "side": "sell" if side == "LONG" else "buy",
            "ordType": "market",
            "sz": sz,
            "reduceOnly": True,
            "posSide": pos_side,
            "tag": "PANDAGSCSI",
        }
        return self.private("POST", "/api/v5/trade/order", payload)


# ---------------------------------------------------------------------------
# Señal agresiva
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    side: str
    score: float
    reasons: List[str] = field(default_factory=list)
    vetoes: List[str] = field(default_factory=list)
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    atr_pct: float = 0.0
    funding: float = 0.0
    long_pts: float = 0.0
    short_pts: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.side in ("LONG", "SHORT") and self.score >= MIN_SCORE and not self.vetoes


def score_market(exec_df: pd.DataFrame, struct_df: pd.DataFrame, bias_df: pd.DataFrame, ticker: dict, funding: dict) -> Signal:
    e = exec_df.iloc[-1]
    prev = exec_df.iloc[-2]
    s = struct_df.iloc[-1]
    b = bias_df.iloc[-1]
    last = float(ticker.get("last", e["close"]))
    fr = float(funding.get("fundingRate", 0.0) or 0.0)

    reasons_l, reasons_s, vetoes = [], [], []
    long_pts = short_pts = 0.0

    if float(e["atr_pct"]) > ATR_PCT_HARD_CAP:
        vetoes.append(f"ATR% {e['atr_pct']:.3%}")
    rng20 = float(exec_df["high"].tail(20).max() - exec_df["low"].tail(20).min())
    if rng20 < CHOP_RANGE_ATR_MULT * float(e["atr"]):
        vetoes.append("Chop")

    if b["close"] > b["ema200"]:
        long_pts += 1.2
        reasons_l.append("4H bull")
    else:
        short_pts += 1.2
        reasons_s.append("4H bear")
    if b["adx"] >= 16:
        if b["close"] > b["ema200"]:
            long_pts += 0.6
        else:
            short_pts += 0.6

    if int(s["st_dir"]) == 1:
        long_pts += 0.8
        reasons_l.append("ST1H+")
    elif int(s["st_dir"]) == -1:
        short_pts += 0.8
        reasons_s.append("ST1H-")

    near_ema = abs(e["close"] / e["ema21"] - 1) <= 0.006
    rsi_ok_l = 36 <= e["rsi"] <= 58
    rsi_ok_s = 42 <= e["rsi"] <= 64
    if (near_ema or e["close"] <= e["bb_lo"] * 1.006) and rsi_ok_l:
        long_pts += 2.2
        reasons_l.append(f"PB RSI{e['rsi']:.0f}")
    if (near_ema or e["close"] >= e["bb_up"] * 0.994) and rsi_ok_s:
        short_pts += 2.2
        reasons_s.append(f"Rally RSI{e['rsi']:.0f}")

    if e["macd_hist"] > prev["macd_hist"]:
        long_pts += 0.8
        reasons_l.append("MACD+")
    if e["macd_hist"] < prev["macd_hist"]:
        short_pts += 0.8
        reasons_s.append("MACD-")

    if e["vol_sma"] and e["volume"] >= e["vol_sma"] * 0.80:
        long_pts += 0.4
        short_pts += 0.4

    if fr >= 0.0008:
        short_pts += 0.8
    elif fr <= -0.0008:
        long_pts += 0.8

    side, score, reasons = "FLAT", max(long_pts, short_pts), []
    if long_pts >= short_pts and long_pts >= MIN_SCORE:
        side, score, reasons = "LONG", long_pts, reasons_l
    elif short_pts > long_pts and short_pts >= MIN_SCORE:
        side, score, reasons = "SHORT", short_pts, reasons_s
    else:
        reasons = [f"L{long_pts:.1f}/S{short_pts:.1f} < {MIN_SCORE}"]

    sl = tp = 0.0
    if side == "LONG":
        sl = last * (1 - STOP_PCT)
        tp = last * (1 + STOP_PCT * TP_R)
    elif side == "SHORT":
        sl = last * (1 + STOP_PCT)
        tp = last * (1 - STOP_PCT * TP_R)

    return Signal(
        side=side, score=round(float(score), 2), reasons=reasons, vetoes=vetoes,
        price=last, sl=round(sl, 1), tp=round(tp, 1),
        atr_pct=float(e["atr_pct"]), funding=fr,
        long_pts=round(long_pts, 2), short_pts=round(short_pts, 2),
    )


# ---------------------------------------------------------------------------
# Estado paper
# ---------------------------------------------------------------------------
def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "equity": PAPER_EQUITY,
        "position": None,
        "trades": [],
        "wins": 0,
        "losses": 0,
    }

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)

def append_trade_csv(trade: Dict[str, Any]) -> None:
    header = not os.path.exists(TRADES_FILE)
    df = pd.DataFrame([trade])
    df.to_csv(TRADES_FILE, mode="a", header=header, index=False)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fmt(ts: Optional[datetime] = None) -> str:
    return (ts or now_utc()).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Motor vivo
# ---------------------------------------------------------------------------
def decide_exec_mode() -> str:
    live = os.getenv("LIVE_TRADING", "false").lower() in ("1", "true", "yes")
    flag = os.getenv("OKX_FLAG", "1")
    if live and flag == "0":
        return "LIVE"
    if os.getenv("OKX_API_KEY") and flag == "1":
        return "DEMO"
    return "PAPER"


def print_banner(mode: str, interval: int) -> None:
    print("=" * 74)
    print("PANDA GSCSI LIVE  |  modo agresivo  |  abrir y cerrar entradas")
    print(f"Instrumento : {INST_ID}")
    print(f"Modo        : {mode}   (PAPER=simulado, DEMO=OKX demo, LIVE=real)")
    print(f"Stop / TP   : {STOP_PCT:.2%} / {TP_R:.1f}R     Score min: {MIN_SCORE}")
    print(f"Max hold    : {MAX_HOLD_SECONDS//60} min     Intervalo: {interval}s")
    print("Capital real: NO se usa por defecto")
    print("=" * 74)


def manage_exit(pos: dict, last: float, high: float, low: float) -> Optional[str]:
    held = (now_utc() - datetime.fromisoformat(pos["entry_time"])).total_seconds()
    if pos["side"] == "LONG":
        if low <= pos["sl"]:
            return "SL"
        if high >= pos["tp"]:
            return "TP"
    else:
        if high >= pos["sl"]:
            return "SL"
        if low <= pos["tp"]:
            return "TP"
    if held >= MAX_HOLD_SECONDS:
        return "TIME"
    return None


def close_paper(state: dict, last: float, reason: str) -> dict:
    pos = state["position"]
    if pos["side"] == "LONG":
        pnl_pct = (last - pos["entry"]) / pos["entry"]
    else:
        pnl_pct = (pos["entry"] - last) / pos["entry"]
    pnl_pct -= FEE_ROUNDTRIP
    pnl_usdt = state["equity"] * (RISK_PCT / 100.0) * (pnl_pct / STOP_PCT)
    # más estable: aplicar pnl sobre notional paper
    notional = pos.get("notional", state["equity"] * DEFAULT_LEVERAGE)
    pnl_usdt = notional * pnl_pct
    state["equity"] += pnl_usdt
    trade = {
        "entry_time": pos["entry_time"],
        "exit_time": now_utc().isoformat(),
        "side": pos["side"],
        "entry": pos["entry"],
        "exit": last,
        "sl": pos["sl"],
        "tp": pos["tp"],
        "reason": reason,
        "pnl_pct": round(pnl_pct * 100, 3),
        "pnl_usdt": round(pnl_usdt, 2),
        "equity": round(state["equity"], 2),
    }
    if pnl_usdt >= 0:
        state["wins"] += 1
    else:
        state["losses"] += 1
    state["trades"].append(trade)
    state["position"] = None
    append_trade_csv(trade)
    save_state(state)
    return trade


def open_paper(state: dict, sig: Signal, last: float) -> dict:
    notional = state["equity"] * DEFAULT_LEVERAGE
    pos = {
        "side": sig.side,
        "entry": last,
        "sl": sig.sl,
        "tp": sig.tp,
        "entry_time": now_utc().isoformat(),
        "score": sig.score,
        "notional": notional,
        "sz": "1",
    }
    state["position"] = pos
    save_state(state)
    return pos


def cycle(ox: OkxClient, state: dict, mode: str) -> None:
    exec_df = enrich(ox.candles("15m", 200))
    struct_df = enrich(ox.candles("1H", 200))
    bias_df = enrich(ox.candles("4H", 150))
    ticker = ox.ticker()
    funding = ox.funding()
    last = float(ticker.get("last", exec_df.iloc[-1]["close"]))
    high = float(exec_df.iloc[-1]["high"])
    low = float(exec_df.iloc[-1]["low"])
    sig = score_market(exec_df, struct_df, bias_df, ticker, funding)

    print("\n" + "-" * 74)
    print(f"{fmt()}   precio {last:,.1f}   funding {sig.funding:.4%}   ATR% {sig.atr_pct:.3%}")
    print(f"Señal: {sig.side:5} | score {sig.score:.2f} | L{sig.long_pts} S{sig.short_pts} | allowed={sig.allowed}")
    if sig.vetoes:
        print("Vetoes:", "; ".join(sig.vetoes))
    print("Razones:", " | ".join(sig.reasons[:5]) if sig.reasons else "-")

    # Gestionar posición abierta
    if state.get("position"):
        pos = state["position"]
        reason = manage_exit(pos, last, high, low)
        held_min = (now_utc() - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 60
        print(f"POS ABIERTA {pos['side']} @ {pos['entry']:.1f} | SL {pos['sl']:.1f} | TP {pos['tp']:.1f} | {held_min:.0f} min")
        if reason:
            if mode in ("DEMO", "LIVE") and ox.has_keys():
                try:
                    ox.close_market(pos["side"], str(pos.get("sz", "1")), pos["side"].lower())
                    print(f"OKX {mode}: cierre enviado ({reason})")
                except Exception as exc:
                    print(f"Aviso cierre OKX: {exc}")
            trade = close_paper(state, last if reason == "TIME" else (pos["sl"] if reason == "SL" else pos["tp"]), reason)
            print(f">>> CIERRE {trade['side']} {reason} | exit {trade['exit']:.1f} | PnL {trade['pnl_usdt']:+.2f} USDT ({trade['pnl_pct']:+.2f}%)")
            print(f"Equity paper: {state['equity']:.2f} | W/L {state['wins']}/{state['losses']}")
        else:
            print("Mantiene posición. Sin cierre aún.")
        return

    # Buscar entrada
    if not sig.allowed:
        print("Sin entrada. Esperando siguiente ciclo.")
        print(f"Equity paper: {state['equity']:.2f} | trades {len(state['trades'])} | W/L {state['wins']}/{state['losses']}")
        return

    pos = open_paper(state, sig, last)
    print(f">>> ABRE {pos['side']} @ {pos['entry']:.1f} | SL {pos['sl']:.1f} | TP {pos['tp']:.1f} | score {sig.score}")

    if mode in ("DEMO", "LIVE") and ox.has_keys():
        side = "buy" if sig.side == "LONG" else "sell"
        pos_side = sig.side.lower()
        payload = {
            "instId": INST_ID,
            "tdMode": "cross",
            "side": side,
            "ordType": "market",
            "sz": "1",
            "posSide": pos_side,
            "slTriggerPx": str(sig.sl),
            "slOrdPx": "-1",
            "slTriggerPxType": "last",
            "tpTriggerPx": str(sig.tp),
            "tpOrdPx": "-1",
            "tpTriggerPxType": "last",
            "tag": "PANDAGSCSI",
        }
        try:
            ox.set_leverage(str(min(DEFAULT_LEVERAGE, MAX_LEVERAGE)), pos_side)
        except Exception as exc:
            print(f"Aviso leverage: {exc}")
        try:
            res = ox.place_order(payload)
            print(f"OKX {mode} orden enviada:", json.dumps(res, default=str)[:300])
            state["position"]["sz"] = "1"
            save_state(state)
        except Exception as exc:
            print(f"No se pudo enviar a OKX ({exc}). Se mantiene solo en PAPER.")


def main() -> None:
    parser = argparse.ArgumentParser(description="PANDA GSCSI LIVE agresivo paper/demo")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL)
    args = parser.parse_args()

    mode = decide_exec_mode()
    ox = OkxClient()
    state = load_state()
    print_banner(mode, args.interval)

    if mode == "LIVE":
        print("\nALERTA: LIVE_TRADING=true y OKX_FLAG=0 usaría capital real.")
        print("Este script no lo activa solo. Revisa variables de entorno.\n")

    if mode == "PAPER":
        print("Modo PAPER activo: abre/cierra en local y verás toda la actividad.")
        print("Para DEMO OKX crea claves de Simulated Trading y pon OKX_FLAG=1.\n")

    print(f"Estado inicial equity paper: {state['equity']:.2f} USDT")
    print("Ctrl+C para detener.\n")

    while True:
        try:
            cycle(ox, state, mode)
        except KeyboardInterrupt:
            print("\nDetenido por usuario. Estado guardado.")
            save_state(state)
            break
        except Exception as exc:
            print(f"Error de ciclo: {exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
