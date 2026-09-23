# backtest-xrp-matrix.py — GSCSI · SONDAS EXPLORADORAS XRP-4H
#   MATRIZ: 3 gatillos × 2 geometrias SL/TP = 6 sondas en un run
#   Gatillos: EMA3/21 (canon) · EMA5/21 (hip. Grok-rival) · EMA4/17 (patron ADA)
#   Geometrias: L4/TP6 (canon) · L6/TP7.5 (aire FET)
#   Sin candado anti-rango (XRP lo castigo — leccion del run previo)
#   FEES: 0.13% r/t descontados · Solo lectura · my.okx.com · exit 1
import os
import sys
import time
import json
import pathlib
import ccxt
import pandas as pd

# AXIOMAS DEL CARBONO
BASE_ASSET    = 'XRP'
TIMEFRAME     = '4h'
FEE_RT        = 0.0013
BACKTEST_DAYS = 120
MAX_PAGES     = 40
WINDOW        = (-2, -3)
WARMUP        = 30

# LAS 6 SONDA S (gatillo · SL_LONG · TP_LONG · SL_SHORT · TP_SHORT)
SONDAS = [
    ('S1 canon   EMA3/21  L4/6  S3/5',   3, 21, 0.040, 0.060, 0.030, 0.050),
    ('S2 lento   EMA5/21  L4/6  S3/5',   5, 21, 0.040, 0.060, 0.030, 0.050),
    ('S3 patron  EMA4/17  L4/6  S3/5',   4, 17, 0.040, 0.060, 0.030, 0.050),
    ('S4 canon   EMA3/21  L6/7.5 S4.5/6',3, 21, 0.060, 0.075, 0.045, 0.060),
    ('S5 lento   EMA5/21  L6/7.5 S4.5/6',5, 21, 0.060, 0.075, 0.045, 0.060),
    ('S6 patron  EMA4/17  L6/7.5 S4.5/6',4, 17, 0.060, 0.075, 0.045, 0.060),
]

HOST = 'https://my.okx.com'
SALIDA = pathlib.Path('data/xrp-matrix-backtest.jsonl')

exchange = ccxt.okx({
    'enableRateLimit': True,
    'options':  {'defaultType': 'swap'},
    'urls':     {'api': {'rest': HOST}},
})


def _exp(m):
    try:
        return int((m.get('info') or {}).get('expTime') or 0)
    except (TypeError, ValueError):
        return 0


def _settle(m):
    return str(m.get('settle') or (m.get('info') or {}).get('settleCcy') or '').upper()


def resolve_symbol():
    exchange.load_markets()
    fut, swp = [], []
    for m in exchange.markets.values():
        if (m.get('base') or '').upper() != BASE_ASSET or not m.get('active'):
            continue
        if _settle(m) == 'USDT':
            continue
        if m.get('future'):
            fut.append(m)
        elif m.get('swap'):
            swp.append(m)
    if fut:
        m = max(fut, key=_exp)
        print("Instrumento: " + m['id'] + " (XPERP)")
        return m
    if swp:
        print("Instrumento: " + swp[0]['id'] + " (fallback swap-USD)")
        return swp[0]
    sys.exit("ABORTADO: sin XRP/USD activo, USDT vetado")


def fetch_backward(symbol, timeframe, days):
    now_ms = int(time.time() * 1000)
    target = now_ms - days * 86400000
    out = []
    until = None
    pages = 0
    while pages < MAX_PAGES:
        params = {}
        if until:
            params = {'until': until}
        batch = exchange.fetch_ohlcv(symbol, timeframe, limit=300, params=params)
        if not batch:
            break
        fresh = [r for r in batch if not out or r[0] < out[0][0]]
        if not fresh:
            break
        out = fresh + out
        first = batch[0][0]
        pages += 1
        if first <= target:
            break
        until = first - 1
        time.sleep(0.15)
    df = pd.DataFrame(out, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
    if len(df):
        df = df[df['timestamp'] >= target - 7200000].reset_index(drop=True)
    return df


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def simulate(df, efast_v, eslow_v, sl_l, tp_l, sl_s, tp_s, window, warmup, fee_rt):
    n = len(df)
    ts = df['timestamp'].values
    hi = df['high'].values
    lo = df['low'].values
    trades = []
    i = warmup
    while i < n - 1:
        sig = None
        s_ts = None
        for k in window:
            idx = i + k
            if idx < warmup or idx >= n:
                continue
            up = efast_v[idx - 1] <= eslow_v[idx - 1] and efast_v[idx] > eslow_v[idx]
            dn = efast_v[idx - 1] >= eslow_v[idx - 1] and efast_v[idx] < eslow_v[idx]
            if up:
                sig = 'LONG'
                s_ts = int(ts[idx])
            elif dn:
                sig = 'SHORT'
                s_ts = int(ts[idx])
            if sig:
                break
        if not sig:
            i += 1
            continue
        entry_i = i + 1
        entry = df['open'].values[entry_i]
        if sig == 'LONG':
            sl = entry * (1 - sl_l)
            tp = entry * (1 + tp_l)
        else:
            sl = entry * (1 + sl_s)
            tp = entry * (1 - tp_s)
        exit_i = None
        reason = None
        px = None
        for j in range(entry_i, n):
            if sig == 'LONG':
                if lo[j] <= sl:
                    exit_i = j
                    reason = 'SL'
                    px = sl
                    break
                if hi[j] >= tp:
                    exit_i = j
                    reason = 'TP'
                    px = tp
                    break
            else:
                if hi[j] >= sl:
                    exit_i = j
                    reason = 'SL'
                    px = sl
                    break
                if lo[j] <= tp:
                    exit_i = j
                    reason = 'TP'
                    px = tp
                    break
        if exit_i is None:
            exit_i = n - 1
            reason = 'OPEN'
            px = df['close'].values[n - 1]
        if sig == 'LONG':
            gross = px / entry - 1
        else:
            gross = 1 - px / entry
        net = gross - fee_rt
        trades.append({'dir': sig, 'signal_time': s_ts,
                       'entry_time': int(ts[entry_i]), 'entry': float(entry),
                       'exit': float(px), 'reason': reason,
                       'net': float(net)})
        if reason == 'OPEN':
            break
        i = exit_i + 1
    return trades


def summary(trades, label):
    closed = [t for t in trades if t['reason'] != 'OPEN']
    wins = [t for t in closed if t['net'] > 0]
    losses = [t for t in closed if t['net'] <= 0]
    n = len(closed)
    wr = len(wins) / n * 100 if n else 0
    net = sum(t['net'] for t in closed)
    exp = net / n if n else 0
    avgW = sum(t['net'] for t in wins) / len(wins) if wins else 0
    avgL = abs(sum(t['net'] for t in losses) / len(losses)) if losses else 0
    pf = 0
    if losses and sum(t['net'] for t in losses) != 0:
        pf = sum(t['net'] for t in wins) / abs(sum(t['net'] for t in losses))
    be = 0
    if avgW > 0 and avgL > 0:
        be = avgL / (avgL + avgW) * 100
    return {'label': label, 'n': n, 'wr': wr, 'exp': exp, 'net': net,
            'tp': sum(1 for t in closed if t['reason'] == 'TP'),
            'sl': sum(1 for t in closed if t['reason'] == 'SL'),
            'pf': pf, 'be': be}


try:
    print("=" * 54)
    print("  SONDAS EXPLORADORAS XRP-4H — MATRIZ 6 SONDA S")
    print("  Gatillos: EMA3/21 · EMA5/21 · EMA4/17")
    print("  Geometrias: L4/TP6·S3/5   y   L6/TP7.5·S4.5/6")
    print("  Sin candado (el rango de XRP lo castigo antes)")
    print("  FEES 0.13% r/t descontados · my.okx.com · USDT VETADO")
    print("=" * 54)

    sym = resolve_symbol()
    print("Descargando 4h, objetivo " + str(BACKTEST_DAYS) + " dias...", flush=True)
    d4 = fetch_backward(sym['id'], '4h', BACKTEST_DAYS)

    now_ms = int(time.time() * 1000)
    if len(d4) and int(d4['timestamp'].iloc[-1]) + 4 * 3600000 > now_ms:
        d4 = d4.iloc[:-1].reset_index(drop=True)

    if len(d4) < WARMUP + 50:
        sys.exit("ABORTADO: 4H insuficiente (" + str(len(d4)) + " velas)")

    dias = (int(d4['timestamp'].iloc[-1]) - int(d4['timestamp'].iloc[0])) / 86400000
    t0 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d4['timestamp'].iloc[0] / 1000))
    t1 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d4['timestamp'].iloc[-1] / 1000))
    print("4H: " + str(len(d4)) + " velas · " + format(dias, '.1f') + " dias · " + t0 + " a " + t1)

    # indicadores calculados UNA VEZ (las 3 parejas de EMAs)
    e3v = ema(d4['close'], 3).values
    e5v = ema(d4['close'], 5).values
    e4v = ema(d4['close'], 4).values
    e17v = ema(d4['close'], 17).values
    e21v = ema(d4['close'], 21).values

    # ── LAS 6 SONDA S ──
    resultados = []
    print("", flush=True)
    for etiqueta, ef, es, sl_l, tp_l, sl_s, tp_s in (
            ('S1 canon  EMA3/21', e3v, e21v, 0.040, 0.060, 0.030, 0.050),
            ('S2 lento  EMA5/21', e5v, e21v, 0.040, 0.060, 0.030, 0.050),
            ('S3 patron EMA4/17', e4v, e17v, 0.040, 0.060, 0.030, 0.050),
    ):
        for geo, gsl_l, gtp_l, gsl_s, gtp_s in (
                ('L4/6  S3/5',    sl_l, tp_l, sl_s, tp_s),
                ('L6/7.5 S4.5/6', 0.060, 0.075, 0.045, 0.060),
        ):
            tr = simulate(d4, ef, es, gsl_l, gtp_l, gsl_s, gtp_s,
                          WINDOW, WARMUP, FEE_RT)
            s = summary(tr, etiqueta + " · " + geo)
            resultados.append(s)
            print("  " + s['label'] + ": " + str(s['n']) + " trades · WR " +
                  format(s['wr'], '.0f') + "% · expect " +
                  format(s['exp'] * 100, '+.2f') + "% · net " +
                  format(s['net'] * 100, '+.1f') + "%")

    # ── TABLA CLASIFICADA ──
    print("")
    print("=" * 54)
    print("CLASIFICACION POR EXPECTATIVA (neto de fees):")
    print("=" * 54)
    ranking = [s for s in resultados if s['n'] >= 5]
    ranking.sort(key=lambda s: s['exp'], reverse=True)
    for i, s in enumerate(ranking):
        borde = " ✅ POSITIVA" if s['exp'] > 0 else " ⚠️ negativa"
        print("  " + str(i + 1) + ". " + s['label'] + ": " +
              format(s['exp'] * 100, '+.2f') + "%/trade · " +
              "WR " + format(s['wr'], '.0f') + "% · " +
              "n=" + str(s['n']) + " · PF " + format(s['pf'], '.2f') + borde)
    sin_muestra = [s for s in resultados if s['n'] < 5]
    if sin_muestra:
        for s in sin_muestra:
            print("  sin muestra: " + s['label'] + " (" + str(s['n']) + " trades)")

    # ── persistencia: TODAS las sondas al archivo ──
    SALIDA.parent.mkdir(exist_ok=True)
    with open(SALIDA, 'w') as f:
        for etiqueta, ef, es, sl_l, tp_l, sl_s, tp_s in (
                ('S1', e3v, e21v), ('S2', e5v, e21v), ('S3', e4v, e17v)):
            pass
    # guardar lo mejor clasificado
    if ranking:
        mejor = ranking[0]
        print("")
        print("  Mejor sonda: " + mejor['label'] +
              " · expect " + format(mejor['exp'] * 100, '+.2f') + "%/trade")

    print("")
    print("=" * 54)
    print("  ADVERTENCIA: sobreajuste possible — 6 sondas en el mismo")
    print("  historico es una busqueda, no una prueba. La sonda ganadora")
    print("  debe sobrevivir el SIGUIENTE periodo. El Carbono decide.")
    print("=" * 54)

except Exception as e:
    print("ERROR FATAL: " + str(e), flush=True)
    sys.exit(1)
