#!/usr/bin/env python3
"""
PANDA GSCSI - BTC-USDT-SWAP OKX futures engine
Gran Script Carbono Silicio
Collaborator and protector of the operator.

Dual engine:
  - SCALP SHORT: fast continuous probes while 1W is indecisive.
  - REGIME LONG: only if weekly close sits above MA10 AND MA50 (Mar-2023 analog,
    operator rule 2026-08-28). 15m/1H/4H cannot override that gate.

Default: dry-run / demo. Live trading is an explicit opt-in.
Not financial advice. No guaranteed hit-rate.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


INST_ID = "BTC-USDT-SWAP"
BASE_URL = "https://www.okx.com"
# Carbon 17:50: SL 1.00% both sides. TP 1.5R = 1.50% both sides.
STOP_PCT = float(os.getenv("STOP_PCT", "0.01"))
TP_R = float(os.getenv("TP_R", "1.5"))
LONG_TP_PCT = float(os.getenv("LONG_TP_PCT", "0.015"))
SHORT_STOP_PCT = float(os.getenv("SHORT_STOP_PCT", "0.01"))
SHORT_TP_PCT = float(os.getenv("SHORT_TP_PCT", "0.015"))
MAX_LEVERAGE = 5
DEFAULT_LEVERAGE = 3
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.04"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "6.0"))
MIN_SCORE_SHORT = float(os.getenv("MIN_SCORE_SHORT", "5.5"))
CHOP_RANGE_ATR_MULT = 1.2
ATR_PCT_HARD_CAP = 0.022
FUNDING_EXTREME = 0.0008
MARK_LAST_DIVERGENCE_CAP = 0.0015
CONTRACT_CTVAL_FALLBACK = 0.01
BARS = {"exec": "15m", "struct": "1H", "bias": "4H", "macro": "1W"}
OKX_BAR = {"15m": "15m", "1H": "1H", "4H": "4H", "5m": "5m", "1D": "1D", "1W": "1W"}

WEEKLY_MA_PERIODS = (10, 50, 100, 200)
# Axiom 3: full_pack is law. ma10_ma50 only if Carbon explicitly overrides.
WEEKLY_LONG_MODE = os.getenv("WEEKLY_LONG_MODE", "full_pack").strip().lower()
ENABLE_SCALP_SHORT = os.getenv("ENABLE_SCALP_SHORT", "true").lower() in ("1", "true", "yes")
ENABLE_REGIME_LONG = os.getenv("ENABLE_REGIME_LONG", "true").lower() in ("1", "true", "yes")
MAX_SCALP_SHORTS_PER_DAY = int(os.getenv("MAX_SCALP_SHORTS_PER_DAY", "4"))
SHORT_COOLDOWN_MIN = int(os.getenv("SHORT_COOLDOWN_MIN", "45"))
# Block squeeze-shorts when 4H is strongly trending up
BLOCK_SHORT_IN_4H_SQUEEZE = os.getenv("BLOCK_SHORT_IN_4H_SQUEEZE", "true").lower() in ("1", "true", "yes")

# Motor 3 - ANALYSIS LONG (momentum/confluencia): filosofia distinta al
# regime_long (pack semanal de medias). Independiente del gate semanal
# por diseno - confirma impulso de corto/medio plazo, no tendencia macro.
# Exige que TODOS los siguientes se cumplan a la vez (AND estricto, no
# suma de puntos como los otros motores):
#   - 4H con sesgo alcista (mismo bull_bias que usa regime_long)
#   - 4H ADX >= umbral (tendencia con fuerza real, no plana)
#   - 1H Supertrend en largo
#   - MACD hist 15m subiendo y positivo (momentum activo, no agotado)
#   - RSI 15m en zona de impulso sano (ni sobrecomprado ni plano)
ENABLE_ANALYSIS_LONG = os.getenv("ENABLE_ANALYSIS_LONG", "true").lower() in ("1", "true", "yes")
ANALYSIS_LONG_MIN_ADX_4H = float(os.getenv("ANALYSIS_LONG_MIN_ADX_4H", "25"))
ANALYSIS_LONG_RSI_LO = float(os.getenv("ANALYSIS_LONG_RSI_LO", "45"))
ANALYSIS_LONG_RSI_HI = float(os.getenv("ANALYSIS_LONG_RSI_HI", "65"))
ANALYSIS_LONG_STOP_PCT = float(os.getenv("ANALYSIS_LONG_STOP_PCT", "0.01"))
ANALYSIS_LONG_TP_PCT = float(os.getenv("ANALYSIS_LONG_TP_PCT", "0.02"))

SIGNALS_LOG_PATH = Path(os.getenv("SIGNALS_LOG_PATH", "signals_log.json"))
DAILY_STATE_PATH = Path(os.getenv("DAILY_STATE_PATH", "daily_state.json"))


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
    f_upper = upper.copy()
    f_lower = lower.copy()
    for i in range(1, len(df)):
        f_lower[i] = max(lower[i], f_lower[i - 1]) if direction[i - 1] == 1 else lower[i]
        f_upper[i] = min(upper[i], f_upper[i - 1]) if direction[i - 1] == -1 else upper[i]
        if direction[i - 1] == 1:
            direction[i] = -1 if close[i] < f_lower[i] else 1
        else:
            direction[i] = 1 if close[i] > f_upper[i] else -1
    return pd.Series(direction, index=df.index)


class OkxClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("OKX_API_KEY", "")
        self.api_secret = os.getenv("OKX_API_SECRET", "")
        self.passphrase = os.getenv("OKX_PASSPHRASE", "")
        self.flag = os.getenv("OKX_FLAG", "1")
        self.base = BASE_URL

    def _ts(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        msg = f"{ts}{method}{path}{body}"
        mac = hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def public(self, path: str, params: Optional[dict] = None) -> dict:
        url = self.base + path
        r = requests.get(url, params=params or {}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if str(data.get("code")) != "0":
            raise RuntimeError(f"OKX public error {path}: {data}")
        return data

    def private(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        if not (self.api_key and self.api_secret and self.passphrase):
            raise RuntimeError("Private OKX call requested but keys are missing.")
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
            raise RuntimeError(f"OKX private error {path}: {data}")
        return data

    def candles(self, bar: str, limit: int = 200) -> pd.DataFrame:
        raw = self.public(
            "/api/v5/market/candles",
            {"instId": INST_ID, "bar": bar, "limit": str(limit)},
        )["data"]
        rows = []
        for x in raw:
            rows.append(
                {
                    "ts": pd.to_datetime(int(x[0]), unit="ms", utc=True),
                    "open": float(x[1]),
                    "high": float(x[2]),
                    "low": float(x[3]),
                    "close": float(x[4]),
                    "volume": float(x[5]),
                    # OKX v5: [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]
                    # confirm=1 vela cerrada, 0 = vela aun en curso.
                    "confirm": int(x[8]) if len(x) > 8 else 1,
                }
            )
        df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
        return df

    def ticker(self) -> dict:
        return self.public("/api/v5/market/ticker", {"instId": INST_ID})["data"][0]

    def funding(self) -> dict:
        return self.public("/api/v5/public/funding-rate", {"instId": INST_ID})["data"][0]

    def open_interest(self) -> dict:
        return self.public(
            "/api/v5/public/open-interest",
            {"instType": "SWAP", "instId": INST_ID},
        )["data"][0]

    def instrument(self) -> dict:
        data = self.public(
            "/api/v5/public/instruments",
            {"instType": "SWAP", "instId": INST_ID},
        )["data"]
        return data[0] if data else {}

    def positions(self) -> List[dict]:
        data = self.private("GET", "/api/v5/account/positions", {"instId": INST_ID})
        return data.get("data", [])

    def positions_history(self, limit: int = 100) -> List[dict]:
        data = self.private(
            "GET",
            "/api/v5/account/positions-history",
            {"instType": "SWAP", "instId": INST_ID, "limit": str(limit)},
        )
        return data.get("data", [])

    def place_order(self, payload: dict) -> dict:
        return self.private("POST", "/api/v5/trade/order", payload)

    def set_leverage(self, lever: str, mgn_mode: str = "cross", pos_side: str = "") -> dict:
        body = {"instId": INST_ID, "lever": lever, "mgnMode": mgn_mode}
        if pos_side:
            body["posSide"] = pos_side
        return self.private("POST", "/api/v5/account/set-leverage", body)


def has_open_position(ox: OkxClient) -> bool:
    try:
        pos = ox.positions()
    except Exception as exc:
        print(f"PROTECTOR: no se pudo verificar posiciones abiertas ({exc}). Bloqueando por precaucion.")
        return True
    for p in pos:
        try:
            if abs(float(p.get("pos", "0") or 0)) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


@dataclass
class Signal:
    side: str
    score: float
    engine: str = "none"
    reasons: List[str] = field(default_factory=list)
    vetoes: List[str] = field(default_factory=list)
    price: float = 0.0
    mark: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    atr_pct: float = 0.0
    funding: float = 0.0
    oi: float = 0.0
    snapshot: Dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        if self.vetoes:
            return False
        if self.side == "LONG" and self.engine == "analysis_long":
            # Ya se valido con AND estricto (todas las confluencias) en
            # score_market antes de llegar aqui - no usa umbral de score.
            return True
        if self.side == "LONG":
            return self.score >= MIN_SCORE
        if self.side == "SHORT":
            return self.score >= MIN_SCORE_SHORT
        return False


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema21"] = ema(out["close"], 21)
    out["ema55"] = ema(out["close"], 55)
    out["ema200"] = ema(out["close"], 200) if len(out) >= 200 else ema(out["close"], min(200, len(out)))
    out["rsi"] = rsi(out["close"], 14)
    out["atr"] = atr(out, 14)
    out["atr_pct"] = out["atr"] / out["close"]
    out["adx"] = adx(out, 14)
    macd_l, macd_s, hist = macd(out["close"])
    out["macd"] = macd_l
    out["macd_sig"] = macd_s
    out["macd_hist"] = hist
    mid, up, lo = bollinger(out["close"])
    out["bb_mid"] = mid
    out["bb_up"] = up
    out["bb_lo"] = lo
    out["vol_sma"] = sma(out["volume"], 20)
    try:
        out["st_dir"] = supertrend(out, 10, 3.0)
    except Exception:
        out["st_dir"] = 0
    return out


def weekly_ma_pack(weekly_df: pd.DataFrame) -> Dict[str, Any]:
    # FIX operador 2026-08-28: la regla es "cierre semanal confirmado"
    # (analogia marzo-2023), no el precio flotante de la semana en curso.
    # Si la ultima vela aun no cerro (confirm=0), se descarta para el gate.
    df = weekly_df
    if "confirm" in df.columns and len(df) > 1 and int(df["confirm"].iloc[-1]) == 0:
        df = df.iloc[:-1]

    close = df["close"]
    last = float(close.iloc[-1])
    mas: Dict[str, Optional[float]] = {}
    above: Dict[str, Optional[bool]] = {}
    missing: List[int] = []
    for n in WEEKLY_MA_PERIODS:
        key = f"MA{n}"
        if len(close) < n:
            mas[key] = None
            above[key] = None
            missing.append(n)
            continue
        val = float(sma(close, n).iloc[-1])
        mas[key] = val
        above[key] = last > val

    complete = not missing and all(bool(v) for v in above.values())
    below = [k for k, v in above.items() if v is False]
    if WEEKLY_LONG_MODE == "full_pack":
        gate_need = (10, 50, 100, 200)
    else:
        gate_need = (10, 50)
    long_gate = all(n not in missing and bool(above.get(f"MA{n}")) for n in gate_need)
    return {
        "tf": "1W",
        "close": last,
        "bars": int(len(close)),
        "mas": mas,
        "above": above,
        "missing_periods": missing,
        "full_stack": complete,
        "below": below,
        "long_gate": long_gate,
        "long_gate_mode": WEEKLY_LONG_MODE,
        "long_gate_need": [f"MA{n}" for n in gate_need],
    }


def weekly_long_ok(pack: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    if not pack:
        return False, "1W pack unavailable - fail-closed LONG"
    missing = pack.get("missing_periods") or []
    if WEEKLY_LONG_MODE == "full_pack":
        need = [10, 50, 100, 200]
    else:
        need = [10, 50]
    still_missing = [n for n in need if n in missing]
    if still_missing:
        return False, f"1W missing MA{still_missing} - fail-closed LONG"
    above = pack.get("above") or {}
    failed = [f"MA{n}" for n in need if not above.get(f"MA{n}")]
    if failed:
        return False, f"1W LONG gate closed - close {pack['close']:.1f} not above {'+'.join(failed)}"
    return True, f"1W LONG gate OPEN - close {pack['close']:.1f} above {'+'.join(f'MA{n}' for n in need)}"


def load_daily_state() -> Dict[str, Any]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blank = {
        "utc_date": today,
        "scalp_shorts": 0,
        "last_short_ts": None,
        "realized_pnl_pct": 0.0,
    }
    if DAILY_STATE_PATH.exists():
        try:
            st = json.loads(DAILY_STATE_PATH.read_text())
            if st.get("utc_date") == today:
                return st
        except Exception:
            pass
    return blank


def save_daily_state(st: Dict[str, Any]) -> None:
    DAILY_STATE_PATH.write_text(json.dumps(st, indent=2, default=str))


def compute_daily_realized_pnl_pct(log: Dict[str, Any], utc_date: str) -> float:
    """
    FIX operador 2026-08-28: el circuit-breaker de perdida diaria leia
    'realized_pnl_pct' pero nada lo escribia nunca (quedaba en 0.0 para
    siempre). Aqui se calcula de verdad, sumando el % real capturado por
    cada senal WIN/LOSS resuelta HOY (UTC) en signals_log.json, usando el
    precio de entrada vs. el precio al que se resolvio.
    """
    total_pct = 0.0
    for entry in log.get("signals", []):
        if entry.get("status") not in ("WIN", "LOSS"):
            continue
        resolved_utc = entry.get("resolved_utc") or ""
        if not resolved_utc.startswith(utc_date):
            continue
        try:
            entry_px = float(entry["entry"])
            resolved_px = float(entry["resolved_price"])
        except (KeyError, TypeError, ValueError):
            continue
        if entry_px <= 0:
            continue
        if entry["side"] == "LONG":
            pct = (resolved_px - entry_px) / entry_px
        else:
            pct = (entry_px - resolved_px) / entry_px
        total_pct += pct * 100.0
    return round(total_pct, 4)


def short_budget_ok(st: Dict[str, Any]) -> Tuple[bool, str]:
    if st.get("realized_pnl_pct", 0.0) <= -abs(MAX_DAILY_LOSS_PCT) * 100:
        return False, f"daily loss circuit {st.get('realized_pnl_pct')}%"
    if int(st.get("scalp_shorts", 0)) >= MAX_SCALP_SHORTS_PER_DAY:
        return False, f"max scalp shorts/day {MAX_SCALP_SHORTS_PER_DAY} reached"
    last = st.get("last_short_ts")
    if last:
        try:
            ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
            if age_min < SHORT_COOLDOWN_MIN:
                return False, f"short cooldown {SHORT_COOLDOWN_MIN - age_min:.0f}m left"
        except Exception:
            pass
    return True, "short budget open"


def inspect_indicators(
    exec_df: pd.DataFrame,
    struct_df: pd.DataFrame,
    bias_df: pd.DataFrame,
    weekly_pack: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    e, s, b = exec_df.iloc[-1], struct_df.iloc[-1], bias_df.iloc[-1]
    rank = [
        {"rank": 0, "name": "1W full_pack gate", "value": None if not weekly_pack else weekly_pack.get("long_gate"), "use": "Regime LONG only if 1W close > MA10+50+100+200"},
        {"rank": 1, "name": "ATR regime", "value": float(e["atr_pct"]), "use": "Gate 2% SL validity"},
        {"rank": 2, "name": "EMA200 bias 4H", "value": float(b["close"] / b["ema200"] - 1), "use": "Direction lock (subordinate to 1W)"},
        {"rank": 3, "name": "ADX 4H", "value": float(b["adx"]), "use": "Trend-on switch"},
        {"rank": 4, "name": "Supertrend 1H", "value": int(s["st_dir"]), "use": "Structure bias"},
        {"rank": 5, "name": "EMA21 pullback 15m", "value": float(e["close"] / e["ema21"] - 1), "use": "Execution"},
        {"rank": 6, "name": "RSI 15m", "value": float(e["rsi"]), "use": "Pullback-in-trend only"},
        {"rank": 7, "name": "MACD hist 15m", "value": float(e["macd_hist"]), "use": "Momentum confirm"},
        {"rank": 8, "name": "BB position 15m", "value": float((e["close"] - e["bb_lo"]) / max(e["bb_up"] - e["bb_lo"], 1e-9)), "use": "Mean-reversion in trend"},
        {"rank": 9, "name": "Volume vs SMA20", "value": float(e["volume"] / e["vol_sma"]) if e["vol_sma"] else None, "use": "Participation"},
        {"rank": 10, "name": "Oscillator-only packs", "value": None, "use": "REJECTED as primary"},
    ]
    return {
        "exec_tf": BARS["exec"],
        "last_close": float(e["close"]),
        "atr_pct_15m": float(e["atr_pct"]),
        "rsi_15m": float(e["rsi"]),
        "adx_15m": float(e["adx"]),
        "adx_4h": float(b["adx"]),
        "ema_stack_4h": {
            "close": float(b["close"]),
            "ema21": float(b["ema21"]),
            "ema55": float(b["ema55"]),
            "ema200": float(b["ema200"]),
        },
        "weekly_ma_pack": weekly_pack,
        "ranked_tools": rank,
    }


def score_market(
    exec_df: pd.DataFrame,
    struct_df: pd.DataFrame,
    bias_df: pd.DataFrame,
    ticker: dict,
    funding: dict,
    oi: dict,
    weekly_pack: Optional[Dict[str, Any]] = None,
) -> Signal:
    e = exec_df.iloc[-1]
    prev = exec_df.iloc[-2]
    s = struct_df.iloc[-1]
    b = bias_df.iloc[-1]

    last = float(ticker.get("last", e["close"]))
    mark = float(ticker.get("markPx", last) or last)
    fr = float(funding.get("fundingRate", 0.0) or 0.0)
    oi_val = float(oi.get("oiCcy", oi.get("oi", 0.0)) or 0.0)

    reasons_l: List[str] = []
    reasons_s: List[str] = []
    vetoes: List[str] = []
    long_pts = 0.0
    short_pts = 0.0

    long_gate, long_gate_msg = weekly_long_ok(weekly_pack)
    daily = load_daily_state()
    short_ok, short_budget_msg = short_budget_ok(daily)
    # FIX operador 2026-08-29: no anadir long_gate_msg aqui tambien -
    # ya se inyecta explicitamente mas abajo (lineas can_long / no-fire).
    # Anadirlo en los dos sitios duplicaba el mensaje en el log.

    if float(e["atr_pct"]) > ATR_PCT_HARD_CAP:
        vetoes.append(f"ATR% {e['atr_pct']:.3%} > {ATR_PCT_HARD_CAP:.2%} - 2% stop sits inside noise")

    if last > 0 and abs(mark - last) / last > MARK_LAST_DIVERGENCE_CAP:
        vetoes.append("Mark vs last divergence - possible wick / dislocation")

    rng20 = float(exec_df["high"].tail(20).max() - exec_df["low"].tail(20).min())
    if rng20 < CHOP_RANGE_ATR_MULT * float(e["atr"]):
        vetoes.append("Chop veto: 20-bar range compressed vs ATR")

    bull_bias = b["close"] > b["ema200"] or (b["ema55"] > b["ema200"] and b["ema200"] >= bias_df["ema200"].iloc[-5])
    bear_bias = b["close"] < b["ema200"] or (b["ema55"] < b["ema200"] and b["ema200"] <= bias_df["ema200"].iloc[-5])
    if bull_bias:
        long_pts += 2.0
        reasons_l.append("4H bullish EMA regime")
    if bear_bias:
        short_pts += 2.0
        reasons_s.append("4H bearish EMA regime")
    if b["adx"] >= 20:
        if bull_bias:
            long_pts += 1.0
            reasons_l.append(f"4H ADX {b['adx']:.1f} trend-on")
        if bear_bias:
            short_pts += 1.0
            reasons_s.append(f"4H ADX {b['adx']:.1f} trend-on")
    else:
        if bull_bias:
            reasons_l.append(f"4H ADX {b['adx']:.1f} weak - size down")
        if bear_bias:
            reasons_s.append(f"4H ADX {b['adx']:.1f} weak - size down")

    if int(s["st_dir"]) == 1:
        long_pts += 1.0
        reasons_l.append("1H Supertrend long")
    elif int(s["st_dir"]) == -1:
        short_pts += 1.0
        reasons_s.append("1H Supertrend short")

    near_ema = abs(e["close"] / e["ema21"] - 1) <= 0.004
    at_bb_lo = e["close"] <= e["bb_lo"] * 1.004
    at_bb_up = e["close"] >= e["bb_up"] * 0.996
    rsi_l = 35 <= e["rsi"] <= 52
    rsi_s = 48 <= e["rsi"] <= 65

    if (near_ema or at_bb_lo) and rsi_l:
        long_pts += 2.0
        reasons_l.append(f"15m pullback RSI {e['rsi']:.1f}")
    if (near_ema or at_bb_up) and rsi_s:
        short_pts += 2.0
        reasons_s.append(f"15m rally-into-trend RSI {e['rsi']:.1f}")

    if e["macd_hist"] > prev["macd_hist"] and e["macd_hist"] > 0:
        long_pts += 1.0
        reasons_l.append("MACD histogram rising > 0")
    if e["macd_hist"] < prev["macd_hist"] and e["macd_hist"] < 0:
        short_pts += 1.0
        reasons_s.append("MACD histogram falling < 0")

    if e["vol_sma"] and e["volume"] >= e["vol_sma"]:
        long_pts += 0.5
        short_pts += 0.5
        reasons_l.append("Volume >= SMA20")
        reasons_s.append("Volume >= SMA20")

    if fr >= FUNDING_EXTREME:
        short_pts += 1.5
        reasons_s.append(f"Crowded longs funding {fr:.4%}")
        if fr > FUNDING_EXTREME * 1.5 and b["adx"] < 28:
            long_pts -= 1.5
            reasons_l.append("Funding veto-lite against long")
    elif fr <= -FUNDING_EXTREME:
        long_pts += 1.5
        reasons_l.append(f"Crowded shorts funding {fr:.4%}")
        if fr < -FUNDING_EXTREME * 1.5 and b["adx"] < 28:
            short_pts -= 1.5
            reasons_s.append("Funding veto-lite against short")
    else:
        reasons_l.append(f"Funding neutral {fr:.4%}")
        reasons_s.append(f"Funding neutral {fr:.4%}")

    if BLOCK_SHORT_IN_4H_SQUEEZE and bull_bias and float(b["adx"]) >= 28:
        reasons_s.append("4H squeeze guard - short size/score watched")
        short_pts -= 1.5

    # --- Motor 3: ANALYSIS LONG (momentum/confluencia, AND estricto) ---
    analysis_checks = {
        "4H sesgo alcista": bool(bull_bias),
        f"4H ADX >= {ANALYSIS_LONG_MIN_ADX_4H:.0f}": float(b["adx"]) >= ANALYSIS_LONG_MIN_ADX_4H,
        "1H Supertrend long": int(s["st_dir"]) == 1,
        "MACD hist 15m subiendo y > 0": bool(e["macd_hist"] > prev["macd_hist"] and e["macd_hist"] > 0),
        f"RSI 15m en {ANALYSIS_LONG_RSI_LO:.0f}-{ANALYSIS_LONG_RSI_HI:.0f}": ANALYSIS_LONG_RSI_LO <= e["rsi"] <= ANALYSIS_LONG_RSI_HI,
    }
    analysis_long_confluence = all(analysis_checks.values())
    reasons_analysis = [f"{'OK' if v else 'no'}: {k}" for k, v in analysis_checks.items()]
    can_analysis_long = ENABLE_ANALYSIS_LONG and analysis_long_confluence

    side = "FLAT"
    score = 0.0
    reasons: List[str] = []
    engine = "none"

    can_long = (
        ENABLE_REGIME_LONG
        and long_gate
        and long_pts >= MIN_SCORE
        and long_pts >= short_pts
    )
    can_short = (
        ENABLE_SCALP_SHORT
        and short_ok
        and short_pts >= MIN_SCORE_SHORT
        and short_pts > long_pts
    )

    # Prioridad cuando varios motores calificarian a la vez: el regimen
    # semanal (macro) manda sobre el momentum (medio plazo), que a su vez
    # manda sobre el scalp corto.
    if can_long:
        side, score, reasons, engine = "LONG", long_pts, reasons_l, "regime_long"
        reasons = [long_gate_msg, *reasons]
    elif can_analysis_long:
        side, score, reasons, engine = "LONG", float(sum(analysis_checks.values())), reasons_analysis, "analysis_long"
    elif can_short:
        side, score, reasons, engine = "SHORT", short_pts, reasons_s, "scalp_short"
        reasons = [short_budget_msg, *reasons]
    else:
        score = max(long_pts, short_pts)
        reasons = [
            f"No fire - long {long_pts:.1f}/{MIN_SCORE} short {short_pts:.1f}/{MIN_SCORE_SHORT}",
            long_gate_msg,
            short_budget_msg,
            f"analysis_long: {sum(analysis_checks.values())}/{len(analysis_checks)} confluencias",
            *reasons_l[:2],
            *reasons_s[:2],
        ]
        if not ENABLE_SCALP_SHORT:
            reasons.insert(1, "scalp SHORT disabled by env")
        if not ENABLE_REGIME_LONG:
            reasons.insert(1, "regime LONG disabled by env")
        if not ENABLE_ANALYSIS_LONG:
            reasons.insert(1, "analysis LONG disabled by env")

    sl = tp = 0.0
    if side == "LONG" and engine == "analysis_long":
        sl = last * (1 - ANALYSIS_LONG_STOP_PCT)
        tp = last * (1 + ANALYSIS_LONG_TP_PCT)
    elif side == "LONG":
        sl = last * (1 - STOP_PCT)
        tp = last * (1 + LONG_TP_PCT)
    elif side == "SHORT":
        sl = last * (1 + SHORT_STOP_PCT)
        tp = last * (1 - SHORT_TP_PCT)

    snap = {
        "engine": engine,
        "long_points": round(long_pts, 2),
        "short_points": round(short_pts, 2),
        "rsi_15m": float(e["rsi"]),
        "adx_15m": float(e["adx"]),
        "adx_4h": float(b["adx"]),
        "st_1h": int(s["st_dir"]),
        "weekly_full_stack": None if not weekly_pack else weekly_pack.get("full_stack"),
        "weekly_long_gate": long_gate,
        "weekly_below": None if not weekly_pack else weekly_pack.get("below"),
        "long_tp_pct": LONG_TP_PCT,
        "short_stop_pct": SHORT_STOP_PCT,
        "short_tp_pct": SHORT_TP_PCT,
        "short_budget": short_budget_msg,
        "analysis_long_confluence": f"{sum(analysis_checks.values())}/{len(analysis_checks)}",
        "analysis_long_checks": analysis_checks,
        "analysis_long_stop_pct": ANALYSIS_LONG_STOP_PCT,
        "analysis_long_tp_pct": ANALYSIS_LONG_TP_PCT,
    }

    return Signal(
        side=side,
        score=round(float(score), 2),
        engine=engine,
        reasons=reasons,
        vetoes=vetoes,
        price=last,
        mark=mark,
        sl=round(sl, 1) if sl else 0.0,
        tp=round(tp, 1) if tp else 0.0,
        atr_pct=float(e["atr_pct"]),
        funding=fr,
        oi=oi_val,
        snapshot=snap,
    )


def position_contracts(equity: float, risk_pct: float, price: float, ct_val: float, stop_pct: float = STOP_PCT) -> Tuple[float, int]:
    risk_usdt = equity * (risk_pct / 100.0)
    loss_per_contract = price * ct_val * stop_pct
    if loss_per_contract <= 0:
        return 0.0, 0
    raw = risk_usdt / loss_per_contract
    n = max(1, int(np.floor(raw))) if raw >= 1 else 0
    return raw, n


def load_signals_log() -> Dict[str, Any]:
    if SIGNALS_LOG_PATH.exists():
        try:
            return json.loads(SIGNALS_LOG_PATH.read_text())
        except Exception as exc:
            print(f"WARNING: no se pudo leer {SIGNALS_LOG_PATH} ({exc}). Empezando log nuevo.")
    return {"signals": []}


def save_signals_log(log: Dict[str, Any]) -> None:
    SIGNALS_LOG_PATH.write_text(json.dumps(log, indent=2, default=str))


def resolve_open_signals(log: Dict[str, Any], current_price: float) -> None:
    for entry in log["signals"]:
        if entry["status"] != "OPEN":
            continue
        side = entry["side"]
        tp, sl = entry["tp"], entry["sl"]
        if side == "LONG":
            if current_price >= tp:
                entry["status"] = "WIN"
            elif current_price <= sl:
                entry["status"] = "LOSS"
        elif side == "SHORT":
            if current_price <= tp:
                entry["status"] = "WIN"
            elif current_price >= sl:
                entry["status"] = "LOSS"
        if entry["status"] != "OPEN":
            entry["resolved_utc"] = datetime.now(timezone.utc).isoformat()
            entry["resolved_price"] = current_price


def record_signal(log: Dict[str, Any], sig: Signal) -> None:
    if sig.side not in ("LONG", "SHORT"):
        return
    for entry in log["signals"]:
        if entry["status"] == "OPEN" and entry["side"] == sig.side:
            return
    log["signals"].append(
        {
            "id": len(log["signals"]) + 1,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "side": sig.side,
            "score": sig.score,
            "entry": sig.price,
            "sl": sig.sl,
            "tp": sig.tp,
            "allowed": sig.allowed,
            "status": "OPEN" if sig.allowed else "SKIPPED",
        }
    )


def compute_stats(log: Dict[str, Any]) -> Dict[str, Any]:
    signals = log["signals"]
    wins = sum(1 for s in signals if s["status"] == "WIN")
    losses = sum(1 for s in signals if s["status"] == "LOSS")
    open_ = sum(1 for s in signals if s["status"] == "OPEN")
    skipped = sum(1 for s in signals if s["status"] == "SKIPPED")
    resolved = wins + losses
    winrate = (wins / resolved * 100.0) if resolved else None
    return {
        "total_signals": len(signals),
        "wins": wins,
        "losses": losses,
        "open": open_,
        "skipped": skipped,
        "resolved": resolved,
        "winrate_pct": round(winrate, 2) if winrate is not None else None,
    }


def compute_okx_track_record(closed_positions: List[dict]) -> Dict[str, Any]:
    trades = []
    wins = losses = breakeven = 0
    gross_win = gross_loss = 0.0
    for p in closed_positions:
        try:
            pnl = float(p.get("pnl", p.get("realizedPnl", 0)) or 0)
        except (TypeError, ValueError):
            continue
        trades.append(
            {
                "posId": p.get("posId"),
                "posSide": p.get("posSide"),
                "openAvgPx": p.get("openAvgPx"),
                "closeAvgPx": p.get("closeAvgPx"),
                "pnl": pnl,
                "pnlRatio": p.get("pnlRatio"),
                "fee": p.get("fee"),
                "fundingFee": p.get("fundingFee"),
                "closed_utc": p.get("uTime"),
            }
        )
        if pnl > 0:
            wins += 1
            gross_win += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += pnl
        else:
            breakeven += 1
    resolved = wins + losses
    winrate = round(wins / resolved * 100.0, 2) if resolved else None
    profit_factor = round(gross_win / abs(gross_loss), 2) if gross_loss else None
    return {
        "source": "OKX positions-history (realizedPnl exacto)",
        "total_closed": len(trades),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "winrate_pct": winrate,
        "gross_win_usdt": round(gross_win, 4),
        "gross_loss_usdt": round(gross_loss, 4),
        "net_pnl_usdt": round(gross_win + gross_loss, 4),
        "profit_factor": profit_factor,
        "trades": trades,
    }


def run_stats(ox: OkxClient) -> Dict[str, Any]:
    print_banner()
    print("Consultando historial real de posiciones cerradas en OKX...")
    try:
        closed = ox.positions_history()
    except Exception as exc:
        print(f"ERROR: no se pudo obtener positions-history ({exc}).")
        return {"status": "error", "reason": str(exc)}
    stats = compute_okx_track_record(closed)
    print("\n" + "=" * 72)
    print("           PANDA GSCSI - TRACK RECORD REAL (fuente: OKX)")
    print("=" * 72)
    print(f"Posiciones cerradas : {stats['total_closed']}")
    print(f"Wins / Losses       : {stats['wins']} / {stats['losses']}  (breakeven: {stats['breakeven']})")
    if stats["winrate_pct"] is not None:
        print(f"WIN RATE            : {stats['winrate_pct']}%")
    else:
        print("WIN RATE            : sin operaciones resueltas todavia")
    print(f"PnL neto            : {stats['net_pnl_usdt']:.4f} USDT")
    print("=" * 72)
    print("\n--- FULL JSON ---")
    print(json.dumps(stats, indent=2, default=str))
    return stats


def print_banner() -> None:
    print("=" * 72)
    print("PANDA GSCSI  |  Gran Script Carbono Silicio")
    print("Collaborator and protector of the operator")
    print(f"Instrument: {INST_ID}   Hard SL: {STOP_PCT:.2%} both sides   TP: {TP_R:.1f}R")
    print(f"LONG gate: 1W {WEEKLY_LONG_MODE}  SL {STOP_PCT:.2%} TP {LONG_TP_PCT:.2%} ({LONG_TP_PCT/STOP_PCT:.1f}R)")
    print(f"SHORT scalp: {'ON' if ENABLE_SCALP_SHORT else 'OFF'}  SL {SHORT_STOP_PCT:.2%} TP {SHORT_TP_PCT:.2%}  max/day {MAX_SCALP_SHORTS_PER_DAY}")
    print(f"ANALYSIS LONG: {'ON' if ENABLE_ANALYSIS_LONG else 'OFF'}  SL {ANALYSIS_LONG_STOP_PCT:.2%} TP {ANALYSIS_LONG_TP_PCT:.2%}  ADX4H>={ANALYSIS_LONG_MIN_ADX_4H:.0f}")
    print("=" * 72)


def run_inspect(ox: OkxClient) -> Dict[str, Any]:
    print_banner()
    print("Fetching OKX public market data...")

    exec_df = enrich(ox.candles(OKX_BAR[BARS["exec"]], 200))
    struct_df = enrich(ox.candles(OKX_BAR[BARS["struct"]], 200))
    bias_df = enrich(ox.candles(OKX_BAR[BARS["bias"]], 200))
    weekly_df = ox.candles(OKX_BAR[BARS["macro"]], 300)
    weekly_pack = weekly_ma_pack(weekly_df)

    ticker = ox.ticker()
    funding = ox.funding()
    oi = ox.open_interest()

    inst = {}
    try:
        inst = ox.instrument()
    except Exception as exc:
        print(f"Instrument metadata warning: {exc}")

    inspection = inspect_indicators(exec_df, struct_df, bias_df, weekly_pack)
    sig = score_market(exec_df, struct_df, bias_df, ticker, funding, oi, weekly_pack)

    equity = float(os.getenv("ACCOUNT_EQUITY_USDT", "100"))
    risk_pct = float(os.getenv("RISK_PER_TRADE_PCT", "1.0"))
    ct_val = float(inst.get("ctVal", CONTRACT_CTVAL_FALLBACK) or CONTRACT_CTVAL_FALLBACK)
    if sig.side == "SHORT":
        stop_for_size = SHORT_STOP_PCT
    elif sig.side == "LONG" and sig.snapshot.get("engine") == "analysis_long":
        stop_for_size = ANALYSIS_LONG_STOP_PCT
    else:
        stop_for_size = STOP_PCT
    raw, n = position_contracts(equity, risk_pct, sig.price, ct_val, stop_pct=stop_for_size)

    log = load_signals_log()
    resolve_open_signals(log, sig.price)
    record_signal(log, sig)
    save_signals_log(log)
    stats = compute_stats(log)

    # FIX operador 2026-08-28: actualizar el circuit-breaker de perdida
    # diaria con el PnL real calculado, no dejarlo muerto en 0.0.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_state = load_daily_state()
    daily_state["realized_pnl_pct"] = compute_daily_realized_pnl_pct(log, today)
    save_daily_state(daily_state)

    print("\n" + "=" * 72)
    print("                    PANDA GSCSI - INSPECT RESULT")
    print("=" * 72)
    print(f"Time (UTC)     : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Instrument     : {INST_ID}")
    print(f"Price (last)   : {sig.price:,.1f}")
    print(f"Mark           : {sig.mark:,.1f}")
    print(f"Funding        : {sig.funding:.4%}")
    print(f"ATR% (15m)     : {sig.atr_pct:.3%}")
    print("-" * 72)
    print("1W CARBON PACK:")
    print(f"   close       : {weekly_pack['close']:,.1f}   bars={weekly_pack['bars']}")
    for period in WEEKLY_MA_PERIODS:
        key = f"MA{period}"
        val = weekly_pack["mas"].get(key)
        flag = weekly_pack["above"].get(key)
        if val is None:
            print(f"   {key:<8}    : n/a")
        else:
            print(f"   {key:<8}    : {val:,.1f}   {'ABOVE' if flag else 'BELOW'}")
    print(f"   full_stack  : {weekly_pack['full_stack']}")
    print(f"   LONG gate   : {weekly_pack.get('long_gate')}  need={weekly_pack.get('long_gate_need')}")
    print("-" * 72)

    decision = f">>> {sig.side}  |  Score: {sig.score}  |  {'ALLOWED' if sig.allowed else 'BLOCKED'} <<<"
    print(f"\nDECISION FINAL : {decision}")

    if sig.vetoes:
        print("\nVETOES ACTIVOS:")
        for v in sig.vetoes:
            print(f"   - {v}")
    else:
        print("\nSin vetoes")

    print("\nRazones principales:")
    for r in sig.reasons[:8]:
        print(f"   - {r}")

    if sig.side in ("LONG", "SHORT"):
        engine_now = sig.snapshot.get("engine")
        if sig.side == "SHORT":
            slp, tpp = SHORT_STOP_PCT, SHORT_TP_PCT
        elif engine_now == "analysis_long":
            slp, tpp = ANALYSIS_LONG_STOP_PCT, ANALYSIS_LONG_TP_PCT
        else:
            slp, tpp = STOP_PCT, LONG_TP_PCT
        print(f"\nEngine         : {engine_now}")
        print(f"Entry          : {sig.price:,.1f}")
        print(f"Stop Loss      : {sig.sl:,.1f}  ({slp:.2%})")
        print(f"Take Profit    : {sig.tp:,.1f}  ({tpp:.2%})")

    print("\nSizing (paper):")
    print(f"   Equity       : {equity:.0f} USDT")
    print(f"   Risk/trade   : {risk_pct}%")
    print(f"   Contratos    : {n}  (raw: {raw:.2f})")

    print("\n" + "-" * 72)
    print("TRACK RECORD ACUMULADO")
    print(f"   Senales totales : {stats['total_signals']}")
    print(f"   Resueltas       : {stats['resolved']}  (WIN {stats['wins']} / LOSS {stats['losses']})")
    print(f"   Abiertas        : {stats['open']}   Ignoradas (score bajo): {stats['skipped']}")
    if stats["winrate_pct"] is not None:
        print(f"   WIN RATE        : {stats['winrate_pct']}%")
    else:
        print("   WIN RATE        : aun sin senales resueltas")
    print("-" * 72)

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "instrument": INST_ID,
        "stop_pct": STOP_PCT,
        "tp_r": TP_R,
        "ticker": {
            "last": float(ticker.get("last", 0)),
            "mark": sig.mark,
            "bid": float(ticker.get("bidPx", 0) or 0),
            "ask": float(ticker.get("askPx", 0) or 0),
            "vol24h": ticker.get("vol24h"),
        },
        "funding": {
            "rate": float(funding.get("fundingRate", 0) or 0),
            "next_time": funding.get("nextFundingTime"),
        },
        "open_interest": oi,
        "contract_value_btc": ct_val,
        "inspection": inspection,
        "signal": {
            "side": sig.side,
            "score": sig.score,
            "allowed": sig.allowed,
            "reasons": sig.reasons,
            "vetoes": sig.vetoes,
            "entry": sig.price,
            "sl": sig.sl,
            "tp": sig.tp,
            "atr_pct": sig.atr_pct,
            "snapshot": sig.snapshot,
        },
        "sizing": {
            "equity_usdt": equity,
            "risk_pct_account": risk_pct,
            "raw_contracts": raw,
            "suggested_contracts": n,
        },
        "track_record": stats,
        "protector": {
            "max_leverage": MAX_LEVERAGE,
            "default_leverage": DEFAULT_LEVERAGE,
            "live_default": False,
            "weekly_long_mode": WEEKLY_LONG_MODE,
            "enable_scalp_short": ENABLE_SCALP_SHORT,
            "enable_regime_long": ENABLE_REGIME_LONG,
            "long_tp_pct": LONG_TP_PCT,
            "short_stop_pct": SHORT_STOP_PCT,
            "short_tp_pct": SHORT_TP_PCT,
            "max_scalp_shorts_per_day": MAX_SCALP_SHORTS_PER_DAY,
        },
    }

    print("\n--- FULL JSON REPORT ---")
    print(json.dumps(report, indent=2, default=str))
    return report


def run_trade(ox: OkxClient, report: Optional[dict] = None) -> dict:
    live = os.getenv("LIVE_TRADING", "false").lower() in ("1", "true", "yes")
    if report is None:
        report = run_inspect(ox)
    sig = report["signal"]

    if not sig["allowed"]:
        print("PROTECTOR: no allowed signal. No order sent.")
        return {"status": "blocked", "reason": "signal_not_allowed"}
    if sig["vetoes"]:
        print("PROTECTOR: veto active. No order sent.")
        return {"status": "blocked", "reason": sig["vetoes"]}

    if live and has_open_position(ox):
        print("PROTECTOR: ya existe una posicion abierta en BTC-USDT-SWAP. No se apila una nueva.")
        return {"status": "blocked", "reason": "position_already_open"}

    n = int(report["sizing"]["suggested_contracts"])
    if n < 1:
        print("PROTECTOR: sized contracts < 1. Increase equity or lower contract requirement.")
        return {"status": "blocked", "reason": "size_zero"}

    lever = min(int(os.getenv("LEVERAGE", str(DEFAULT_LEVERAGE))), MAX_LEVERAGE)
    pos_mode = os.getenv("POS_MODE", "long_short")
    side = "buy" if sig["side"] == "LONG" else "sell"
    pos_side = "long" if sig["side"] == "LONG" else "short"

    payload = {
        "instId": INST_ID,
        "tdMode": "cross",
        "side": side,
        "ordType": "market",
        "sz": str(n),
        "slTriggerPx": str(sig["sl"]),
        "slOrdPx": "-1",
        "slTriggerPxType": "mark",
        "tpTriggerPx": str(sig["tp"]),
        "tpOrdPx": "-1",
        "tpTriggerPxType": "mark",
        "tag": "PANDAGSCSI",
    }
    if pos_mode != "net":
        payload["posSide"] = pos_side

    preview = {"live": live, "leverage": lever, "payload": payload}
    print("ORDER PREVIEW:")
    print(json.dumps(preview, indent=2))
    if sig["side"] == "SHORT":
        st = load_daily_state()
        st["scalp_shorts"] = int(st.get("scalp_shorts", 0)) + 1
        st["last_short_ts"] = datetime.now(timezone.utc).isoformat()
        save_daily_state(st)

    if not live:
        print("PROTECTOR: LIVE_TRADING is false. Dry-run only. No order sent to OKX.")
        return {"status": "dry_run", "preview": preview}

    if ox.flag != "0":
        print("NOTE: OKX_FLAG is not 0 - demo/simulated header is on.")

    try:
        ox.set_leverage(str(lever), "cross", pos_side if pos_mode != "net" else "")
    except Exception as exc:
        print(f"Leverage set warning (continuing): {exc}")

    result = ox.place_order(payload)
    print("OKX RESPONSE:")
    print(json.dumps(result, indent=2))
    return {"status": "sent", "result": result}


def main() -> None:
    p = argparse.ArgumentParser(description="PANDA GSCSI OKX BTC-USDT-SWAP engine")
    p.add_argument("--mode", choices=["inspect", "signal", "trade", "stats"], default="inspect")
    p.add_argument("--interval", type=int, default=0,
                   help="Segundos entre cada pasada dentro de una misma ejecucion. 0 = una sola pasada.")
    p.add_argument("--max-minutes", type=float, default=0.0,
                   help="Duracion maxima del bucle en minutos. 0 = una sola pasada.")
    args = p.parse_args()
    ox = OkxClient()

    if args.mode == "stats":
        run_stats(ox)
        return

    action = run_trade if args.mode == "trade" else run_inspect

    if args.interval <= 0 or args.max_minutes <= 0:
        action(ox)
        return

    deadline = time.monotonic() + args.max_minutes * 60
    pass_num = 0
    while True:
        pass_num += 1
        print(f"\n########## PASADA {pass_num} ##########")
        try:
            action(ox)
        except Exception as exc:
            print(f"ERROR en pasada {pass_num}: {exc}")
        remaining = deadline - time.monotonic()
        if remaining <= args.interval:
            break
        time.sleep(args.interval)
    print(f"\nFin del bucle: {pass_num} pasadas en {args.max_minutes} min.")


if __name__ == "__main__":
    main()
