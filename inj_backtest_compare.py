#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inj_backtest_compare.py — mismo motor que inj_bot.py v3.1, dos formas de entrar:

  LIVE   : ventana (0,-1,-2) con la vela de 1h EN FORMACION (como el bot real)
  CLOSED : ventana (-1,-2) usando SOLO velas de 1h cerradas

Simula el cron de 15 min sobre velas de 15m. Mismas reglas en ambos modos:
EMA3/21, candado de separacion, persistencia, deriva maxima, SL por ATR14 +
estructura, TP = 1.5 x SL, peaje ida y vuelta, cooldown tras perdida, una
posicion a la vez y una entrada por vela de cruce (clOrdId).

USO:   pip install ccxt && python inj_backtest_compare.py --days 120
NOTAS: datos publicos de OKX (sin claves). Usa el perpetuo INJ-USDT-SWAP como
       proxy: el XPERP USD de INJ no tiene historial largo. Si SL y TP caen en
       la misma vela de 15m se asume SL primero (conservador).
"""
import argparse
import json
import os
import sys
from bisect import bisect_left
from datetime import datetime, timezone

# ---- parametros espejo de inj-bot.yml ----
FAST, SLOW = 3, 21
SEP_LOCK = 0.17
ATR_N, ATR_MULT = 14, 1.3
SL_MULT, SL_FLOOR, SL_CAP, TP_MULT = 1.2, 1.5, 3.0, 1.5
MAX_DRIFT = 1.0
N_CANDLES = 200
COOLDOWN_MS = 60 * 60 * 1000
H, Q = 3600000, 900000


def ema(vals, span):
    k = 2.0 / (span + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1.0 - k)
        out.append(e)
    return out


def atr_pct(closed):
    trs = []
    for i in range(1, len(closed)):
        h, l, pc = closed[i][2], closed[i][3], closed[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    last = trs[-ATR_N:]
    return (sum(last) / len(last)) / closed[-1][4] * 100.0


def signal(closed, forming, window):
    full = closed + ([forming] if forming else [])
    c = [x[4] for x in full]
    m, n = len(c), len(closed)
    ef, es = ema(c, FAST), ema(c, SLOW)
    stance = [1 if ef[i] > es[i] else -1 for i in range(m)]
    for off in window:
        i = n + off
        if i < 1 or i >= m:
            continue
        if stance[i] != stance[i - 1] and all(stance[j] == stance[i] for j in range(i, m)):
            return {"dir": stance[i], "off": off, "stance": stance[-1], "px": c[-1],
                    "ref": closed[-1][4], "e_cross": es[i], "bar_ts": full[i][0],
                    "sep": abs(ef[i] - es[i]) / es[i] * 100.0,
                    "atr": atr_pct(closed)}
    return None


def sl_tp_pct(px, e_cross, atr):
    d_atr = ATR_MULT * atr
    d_str = abs(px - e_cross) / px * 100.0 * SL_MULT
    sl = min(max(SL_FLOOR, d_atr, d_str), SL_CAP)
    return sl, sl * TP_MULT


def fetch_15m(symbol, days, cache, host):
    if cache and os.path.exists(cache):
        data = json.load(open(cache))
        print("cache: %d velas de %s" % (len(data), cache))
        return data
    import ccxt
    ex = ccxt.okx({"enableRateLimit": True, "hostname": host})
    ex.hostname = host
    now = ex.milliseconds()
    since = (now - days * 86400000) // Q * Q
    out, seen = [], set()
    while since < now:
        batch = ex.fetch_ohlcv(symbol, "15m", since=since, limit=100)
        for c in batch:
            if c[0] not in seen:
                seen.add(c[0])
                out.append([c[0], c[1], c[2], c[3], c[4]])
        since += 100 * Q
        sys.stdout.write("\rdescargando... %d velas" % len(out))
        sys.stdout.flush()
    print()
    out.sort(key=lambda c: c[0])
    out = [c for c in out if c[0] + Q <= now]          # solo 15m cerradas
    if cache:
        json.dump(out, open(cache, "w"))
    return out


def run(c15, window, use_forming, slip, fee_rt):
    hours = {}
    for c in c15:
        hours.setdefault(c[0] - c[0] % H, []).append(c)
    full_hours = sorted(k for k, v in hours.items() if len(v) == 4)
    h1 = {}
    for k in full_hours:
        v = hours[k]
        h1[k] = [k, v[0][1], max(x[2] for x in v), min(x[3] for x in v), v[-1][4]]

    trades, traded = [], set()
    cooldown_until, idx = 0, 0
    while idx < len(c15):
        bar = c15[idx]
        hs = bar[0] - bar[0] % H
        pos_in_hour = (bar[0] - hs) // Q
        p = bisect_left(full_hours, hs)
        if p < SLOW * 2:
            idx += 1
            continue
        closed = [h1[k] for k in full_hours[max(0, p - N_CANDLES):p]]
        if closed[-1][0] != hs - H:                     # hueco de datos
            idx += 1
            continue
        forming = None
        if use_forming:
            part = hours[hs][:pos_in_hour + 1]
            forming = [hs, part[0][1], max(x[2] for x in part),
                       min(x[3] for x in part), bar[4]]
        sig = signal(closed, forming, window)
        t_close = bar[0] + Q
        ok = (sig and sig["sep"] >= SEP_LOCK
              and sig["dir"] == sig["stance"]
              and abs(bar[4] - sig["ref"]) / sig["ref"] * 100.0 <= MAX_DRIFT
              and sig["bar_ts"] not in traded
              and t_close >= cooldown_until)
        if not ok:
            idx += 1
            continue

        entry, d = bar[4], sig["dir"]
        sl_p, tp_p = sl_tp_pct(entry, sig["e_cross"], sig["atr"])
        sl = entry * (1 - d * sl_p / 100.0)
        tp = entry * (1 + d * tp_p / 100.0)
        traded.add(sig["bar_ts"])
        j, res, exit_px = idx + 1, None, None
        while j < len(c15):
            hi, lo = c15[j][2], c15[j][3]
            hit_sl = lo <= sl if d > 0 else hi >= sl
            hit_tp = hi >= tp if d > 0 else lo <= tp
            if hit_sl:                                  # SL primero si hay duda
                res, exit_px = "SL", sl * (1 - d * slip / 100.0)
                break
            if hit_tp:
                res, exit_px = "TP", tp
                break
            j += 1
        if res is None:
            break                                       # sin cierre antes del fin de datos
        pnl = d * (exit_px - entry) / entry * 100.0 - fee_rt
        end_ts = c15[j][0] + Q
        trades.append({"t": bar[0], "dir": "LONG" if d > 0 else "SHORT",
                       "off": sig["off"], "sep": round(sig["sep"], 3),
                       "sl%": round(sl_p, 2), "res": res, "pnl%": round(pnl, 3)})
        if pnl < 0:
            cooldown_until = end_ts + COOLDOWN_MS
        idx = j + 1
    return trades


def report(name, trades):
    n = len(trades)
    if not n:
        print("%-7s sin operaciones" % name)
        return
    w = [t["pnl%"] for t in trades if t["pnl%"] > 0]
    l = [t["pnl%"] for t in trades if t["pnl%"] <= 0]
    tot = sum(t["pnl%"] for t in trades)
    pf = (sum(w) / -sum(l)) if l and sum(l) < 0 else float("inf")
    print("%-7s n=%-3d  WR %.1f%% (%dW/%dL)  neto %+.2f%%  esperanza %+.3f%%/trade  PF %.2f"
          % (name, n, 100.0 * len(w) / n, len(w), len(l), tot, tot / n, pf))
    for off in sorted(set(t["off"] for t in trades), reverse=True):
        sub = [t for t in trades if t["off"] == off]
        sw = sum(1 for t in sub if t["pnl%"] > 0)
        print("          ventana %+d: n=%-3d WR %.1f%%  neto %+.2f%%"
              % (off, len(sub), 100.0 * sw / len(sub), sum(t["pnl%"] for t in sub)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="INJ/USDT:USDT")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--fee", type=float, default=0.14, help="peaje ida y vuelta, %%")
    ap.add_argument("--slip", type=float, default=0.05, help="deslizamiento extra en SL, %%")
    ap.add_argument("--cache", default="inj_15m_cache.json")
    ap.add_argument("--host", default="www.okx.com", help="p.ej. my.okx.com")
    ap.add_argument("--out", default="", help="guardar operaciones en JSONL")
    a = ap.parse_args()

    c15 = fetch_15m(a.symbol, a.days, a.cache, a.host)
    if len(c15) < 2000:
        sys.exit("pocas velas (%d)" % len(c15))
    f = lambda ts: datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    print("%s | %s -> %s | %d velas 15m | peaje %.2f%% | slip SL %.2f%%\n"
          % (a.symbol, f(c15[0][0]), f(c15[-1][0]), len(c15), a.fee, a.slip))

    live = run(c15, (0, -1, -2), True, a.slip, a.fee)
    closed = run(c15, (-1, -2), False, a.slip, a.fee)
    report("LIVE", live)
    print()
    report("CLOSED", closed)
    print("\nLectura: si CLOSED supera a LIVE de forma clara, entrar en la vela en\n"
          "formacion (ventana 0) esta restando; prueba INJ_WINDOW='-1,-2'.\n"
          "Con n<30 por modo, trata la diferencia como pista, no como prueba.")
    if a.out:
        with open(a.out, "w") as fh:
            for mode, tr in (("LIVE", live), ("CLOSED", closed)):
                for t in tr:
                    fh.write(json.dumps(dict(t, mode=mode)) + "\n")


if __name__ == "__main__":
    main()
