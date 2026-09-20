# backtest-fet.py — GSCSI · BACKTEST FET · 3 variantes
#   [A] NUEVA LEY:   1H EMA5/100 · LONG SL6/TP7.5 · SHORT SL3/TP4
#   [B] CONTROL:     30m EMA7/21 · SL1/TP1.5  (bot enterrado)
#   [C] RUIDO:       high-low por vela
# v2: paginacion backward desde el presente (serie XPERP FET joven)
# Solo lectura publica · my.okx.com · veto USDT · exit 1 en error
import os
import sys
import time
import json
import pathlib
import ccxt
import pandas as pd

# AXIOMAS DEL CARBONO
BASE_ASSET    = 'FET'
TIMEFRAME_A   = '1h'
EMA_FAST      = 5
EMA_SLOW      = 100
SL_LONG       = 0.060
TP_LONG       = 0.075
SL_SHORT      = 0.030
TP_SHORT      = 0.040
WINDOW_A      = (-2, -3, -4)
WARMUP_A      = 130
TIMEFRAME_B   = '30m'
SL_B          = 0.010
TP_B          = 0.015
WINDOW_B      = (-2, -3)
WARMUP_B      = 30
BACKTEST_DAYS = 60
MAX_PAGES     = 80

HOST = 'https://my.okx.com'
SALIDA = pathlib.Path('data/fet-backtest.jsonl')

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
    sys.exit("ABORTADO: sin FET/USD activo, USDT vetado")


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


def simulate_cross(df, efast_v, eslow_v, sl_pct, tp_pct, window, warmup,
                   asymmetric=False, sl_s=None, tp_s=None):
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
            sl = entry * (1 - sl_pct)
            tp = entry * (1 + tp_pct)
        else:
            sl_s2 = sl_s if sl_s is not None else sl_pct
            tp_s2 = tp_s if tp_s is not None else tp_pct
            sl = entry * (1 + sl_s2)
            tp = entry * (1 - tp_s2)
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
        trades.append({'dir': sig, 'signal_time': s_ts,
                       'entry_time': int(ts[entry_i]), 'entry': float(entry),
                       'exit': float(px), 'reason': reason,
                       'net': float(gross)})
        if reason == 'OPEN':
            break
        i = exit_i + 1
    return trades


def noise_report(df, label):
    hl = (df['high'] / df['low'] - 1) * 100
    print("  " + label + ": media=" + format(hl.mean(), '.2f') + "% · mediana=" +
          format(hl.median(), '.2f') + "% · P75=" + format(hl.quantile(.75), '.2f') +
          "% · P90=" + format(hl.quantile(.90), '.2f') + "% · max=" +
          format(hl.max(), '.2f') + "%")
    return {'p75': float(hl.quantile(.75)), 'p90': float(hl.quantile(.90)),
            'mean': float(hl.mean())}


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
    print("  BACKTEST FET - 3 variantes")
    print("  [A] Nueva ley 1H EMA5/100")
    print("  [B] Control 30m EMA7/21 SL1 TP1.5")
    print("  [C] Ruido high-low por vela")
    print("  my.okx.com - USDT VETADO")
    print("=" * 54)

    sym = resolve_symbol()
    print("Descargando 1h y 30m, objetivo " + str(BACKTEST_DAYS) + " dias...", flush=True)
    d1h = fetch_backward(sym['id'], '1h', BACKTEST_DAYS)
    d30 = fetch_backward(sym['id'], '30m', BACKTEST_DAYS)

    now_ms = int(time.time() * 1000)
    if len(d1h) and int(d1h['timestamp'].iloc[-1]) + 3600000 > now_ms:
        d1h = d1h.iloc[:-1].reset_index(drop=True)
    if len(d30) and int(d30['timestamp'].iloc[-1]) + 1800000 > now_ms:
        d30 = d30.iloc[:-1].reset_index(drop=True)

    if len(d1h) < WARMUP_A + 50:
        sys.exit("ABORTADO: 1H insuficiente (" + str(len(d1h)) + " velas)")
    if len(d30) < WARMUP_B + 50:
        sys.exit("ABORTADO: 30m insuficiente (" + str(len(d30)) + " velas)")

    dias = (int(d1h['timestamp'].iloc[-1]) - int(d1h['timestamp'].iloc[0])) / 86400000
    t0 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d1h['timestamp'].iloc[0] / 1000))
    t1 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d1h['timestamp'].iloc[-1] / 1000))
    print("1H : " + str(len(d1h)) + " velas · " + format(dias, '.1f') + " dias · " + t0 + " a " + t1)
    print("30m: " + str(len(d30)) + " velas")

    print("")
    print("== [C] MEDICION DE RUIDO ==")
    n1 = noise_report(d1h, "1H")
    d15 = fetch_backward(sym['id'], '15m', 30)
    if len(d15) > 100:
        n15 = noise_report(d15, "15m(30d)")
    else:
        n15 = {'p90': 0}
        print("  15m: sin cobertura suficiente")
    hip = (n1['p75'] > 2.2) or (n15['p90'] > 2.2)
    print("  Hipotesis Gemini/Grok sobre ruido FET: " +
          ("CONFIRMADA" if hip else "REFUTADA a este marco"))

    e5v = ema(d1h['close'], EMA_FAST).values
    e100v = ema(d1h['close'], EMA_SLOW).values
    print("")
    print("Simulando [A] Nueva ley 1H EMA5/100...", flush=True)
    trA = simulate_cross(d1h, e5v, e100v, SL_LONG, TP_LONG, WINDOW_A,
                         WARMUP_A, asymmetric=True, sl_s=SL_SHORT, tp_s=TP_SHORT)
    sA = summary(trA, 'A: 1H EMA5/100 SL6 TP7.5')

    e7v = ema(d30['close'], 7).values
    e21v = ema(d30['close'], 21).values
    print("Simulando [B] Control 30m EMA7/21 SL1 TP1.5...", flush=True)
    trB = simulate_cross(d30, e7v, e21v, SL_B, TP_B, WINDOW_B, WARMUP_B)
    sB = summary(trB, 'B: 30m EMA7/21 SL1 TP1.5')

    print("")
    print("=" * 54)
    print("RESULTADOS, brutos sin fees:")
    print("=" * 54)
    for s in (sA, sB):
        if s['n']:
            print("  " + s['label'])
            print("    trades:" + str(s['n']) + "  TP:" + str(s['tp']) +
                  " SL:" + str(s['sl']) +
                  "  WR:" + format(s['wr'], '.0f') + "%" +
                  "  breakeven~" + format(s['be'], '.0f') + "%" +
                  "  expect:" + format(s['exp'] * 100, '+.2f') + "%/trade" +
                  "  net:" + format(s['net'] * 100, '+.1f') + "%" +
                  "  PF:" + format(s['pf'], '.2f'))
        else:
            print("  " + s['label'] + ": sin trades cerrados")

    print("")
    print("VEREDICTO:")
    if sA['n'] >= 5:
        edge = sA['exp'] > 0 and sA['wr'] >= sA['be']
        if edge:
            print("  [A] Nueva ley: EXPECTATIVA POSITIVA")
        else:
            print("  [A] Nueva ley: SIN VENTAJA demostrada")
        print("      WR " + format(sA['wr'], '.0f') + "% vs breakeven " +
              format(sA['be'], '.0f') + "%")
    else:
        print("  [A] Nueva ley: muestra insuficiente, cruces EMA5/100 raros por diseño")
    if sB['n'] >= 5:
        if sB['exp'] <= 0:
            print("  [B] Bot enterrado: NO tenia ventaja (WR " +
                  format(sB['wr'], '.0f') + "%, expect " +
                  format(sB['exp'] * 100, '+.2f') + "%)")
            print("      El marco era el problema, no la ejecucion")
        else:
            print("  [B] Bot enterrado: tenia ventaja bruta (WR " +
                  format(sB['wr'], '.0f') + "%) - la ejecucion lo enterró")
    else:
        print("  [B] Control: muestra insuficiente")

    SALIDA.parent.mkdir(exist_ok=True)
    with open(SALIDA, 'w') as f:
        for t in trA:
            f.write(json.dumps(t) + '\n')
    print("")
    print("Trades [A] guardados en " + str(SALIDA))

    print("")
    print("=" * 54)
    print("  ADVERTENCIA: pasado no es futuro. Brutos sin fees.")
    print("  SL primero en misma vela = conservador.")
    print("  El Carbono decide.")
    print("=" * 54)

except Exception as e:
    print("ERROR FATAL: " + str(e), flush=True)
    sys.exit(1)
