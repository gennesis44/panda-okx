# backtest-ada.py — GSCSI · BACKTEST ADA · LEY EMA4/17 EN 6H
#   [A] NUEVA LEY:   6H EMA4/17 · LONG SL3.5/TP5 · SHORT SL2.5/TP4
#       (geometria: SL absorbe ruido 6h 2-4% + retroceso ADA tipico 1.5-1.6%
#        en 15m ≈ 3-4% en 6h · asimetria heredada: ADA sube facil)
#   [B] CONTROL:     marco actual de produccion (15m EMA7/21 SL1/TP1.5)
#   [C] RUIDO:       high-low por vela 6h y 15m
#   FEES: 0.13% r/t descontados por trade
#   ADVERTENCIA estructural: velas 6h retienen ~1-2 meses → muestra corta
#     esperada (2-4 cruces/mes). Marco swing puro: posiciones de dias.
#   Solo lectura publica · my.okx.com · veto USDT · exit 1 en error
import os
import sys
import time
import json
import pathlib
import ccxt
import pandas as pd

# AXIOMAS DEL CARBONO
BASE_ASSET    = 'ADA'
TIMEFRAME_A   = '6h'
EMA_FAST      = 4
EMA_SLOW      = 17
SL_LONG       = 0.035
TP_LONG       = 0.050
SL_SHORT      = 0.025
TP_SHORT      = 0.040
WINDOW_A      = (-2, -3)
WARMUP_A      = 30
TIMEFRAME_B   = '15m'
SL_B          = 0.010
TP_B          = 0.015
WINDOW_B      = (-2, -3)
WARMUP_B      = 30
FEE_RT        = 0.0013
BACKTEST_DAYS = 90
MAX_PAGES     = 40

HOST = 'https://my.okx.com'
SALIDA = pathlib.Path('data/ada-backtest.jsonl')

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
    sys.exit("ABORTADO: sin ADA/USD activo, USDT vetado")


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


def simulate_cross(df, efast_v, eslow_v, sl_l, tp_l, sl_s, tp_s, window, warmup, fee_rt):
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


def noise_report(df, label):
    hl = (df['high'] / df['low'] - 1) * 100
    print("  " + label + ": media=" + format(hl.mean(), '.2f') + "% · mediana=" +
          format(hl.median(), '.2f') + "% · P75=" + format(hl.quantile(.75), '.2f') +
          "% · P90=" + format(hl.quantile(.90), '.2f') + "% · max=" +
          format(hl.max(), '.2f') + "%")
    return {'mean': float(hl.mean()), 'p75': float(hl.quantile(.75))}


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
    print("  BACKTEST ADA - LEY EMA4/17 EN 6H")
    print("  [A] Nueva ley 6H EMA4/17 L: SL3.5/TP5 · S: SL2.5/TP4")
    print("  [B] Control 15m EMA7/21 SL1 TP1.5 (produccion)")
    print("  [C] Ruido high-low por vela 6h")
    print("  FEES 0.13% r/t descontados · my.okx.com · USDT VETADO")
    print("=" * 54)

    sym = resolve_symbol()
    print("Descargando 6h y 15m, objetivo " + str(BACKTEST_DAYS) + " dias...", flush=True)
    d6 = fetch_backward(sym['id'], '6h', BACKTEST_DAYS)
    d15 = fetch_backward(sym['id'], '15m', BACKTEST_DAYS)

    now_ms = int(time.time() * 1000)
    if len(d6) and int(d6['timestamp'].iloc[-1]) + 6 * 3600000 > now_ms:
        d6 = d6.iloc[:-1].reset_index(drop=True)
    if len(d15) and int(d15['timestamp'].iloc[-1]) + 900000 > now_ms:
        d15 = d15.iloc[:-1].reset_index(drop=True)

    if len(d6) < WARMUP_A + 30:
        sys.exit("ABORTADO: 6H insuficiente (" + str(len(d6)) + " velas)")
    if len(d15) < WARMUP_B + 200:
        sys.exit("ABORTADO: 15m insuficiente (" + str(len(d15)) + " velas)")

    dias = (int(d6['timestamp'].iloc[-1]) - int(d6['timestamp'].iloc[0])) / 86400000
    t0 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d6['timestamp'].iloc[0] / 1000))
    t1 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d6['timestamp'].iloc[-1] / 1000))
    print("6H : " + str(len(d6)) + " velas · " + format(dias, '.1f') + " dias · " + t0 + " a " + t1)
    print("15m: " + str(len(d15)) + " velas")

    print("")
    print("== [C] MEDICION DE RUIDO ==")
    n6 = noise_report(d6, "6H")
    n15 = noise_report(d15, "15m")
    print("  Referencias: SL LONG 3.5% · SL SHORT 2.5% · fee r/t 0.13%")

    e4v = ema(d6['close'], EMA_FAST).values
    e17v = ema(d6['close'], EMA_SLOW).values
    print("")
    print("Simulando [A] Nueva ley 6H EMA4/17 (con fees)...", flush=True)
    trA = simulate_cross(d6, e4v, e17v, SL_LONG, TP_LONG, SL_SHORT, TP_SHORT,
                         WINDOW_A, WARMUP_A, FEE_RT)
    sA = summary(trA, 'A: 6H EMA4/17 L3.5/5 S2.5/4')

    e7v = ema(d15['close'], 7).values
    e21v = ema(d15['close'], 21).values
    print("Simulando [B] Control 15m EMA7/21 SL1 TP1.5 (con fees)...", flush=True)
    trB = simulate_cross(d15, e7v, e21v, SL_B, TP_B, SL_B, TP_B,
                         WINDOW_B, WARMUP_B, FEE_RT)
    sB = summary(trB, 'B: 15m EMA7/21 SL1 TP1.5')

    print("")
    print("=" * 54)
    print("RESULTADOS, NETOS de fees (0.13% r/t):")
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

    longsA = [t for t in trA if t['reason'] != 'OPEN' and t['dir'] == 'LONG']
    shortsA = [t for t in trA if t['reason'] != 'OPEN' and t['dir'] == 'SHORT']
    if longsA or shortsA:
        print("")
        print("  Desglose [A] por direccion:")
        for nom, arr in (('LONG', longsA), ('SHORT', shortsA)):
            if arr:
                w = sum(1 for t in arr if t['net'] > 0)
                net = sum(t['net'] for t in arr)
                print("    " + nom + ": " + str(len(arr)) + " trades · " +
                      str(w) + "W/" + str(len(arr) - w) + "L · net " +
                      format(net * 100, '+.1f') + "%")
                for t in arr:
                    ts_str = time.strftime('%d %H:%M', time.gmtime(t['entry_time'] / 1000))
                    print("      " + ts_str + " " + t['dir'] + " " +
                          format(t['entry'], '.4f') + "→" + format(t['exit'], '.4f') +
                          " " + t['reason'] + " " + format(t['net'] * 100, '+.2f') + "%")

    print("")
    print("VEREDICTO:")
    if sA['n'] >= 5:
        edge = sA['exp'] > 0 and sA['wr'] >= sA['be']
        if edge:
            print("  [A] Nueva ley: EXPECTATIVA POSITIVA tras fees")
        else:
            print("  [A] Nueva ley: SIN VENTAJA tras fees")
        print("      WR " + format(sA['wr'], '.0f') + "% vs breakeven " +
              format(sA['be'], '.0f') + "%")
    else:
        print("  [A] Nueva ley: muestra insuficiente (" + str(sA['n']) +
              " trades — marco 6H genera pocas senales por diseño)")
    if sB['n'] >= 10:
        if sB['exp'] <= 0:
            print("  [B] Produccion actual: SIN ventaja tras fees (WR " +
                  format(sB['wr'], '.0f') + "%)")
        else:
            print("  [B] Produccion actual: ventaja positiva (WR " +
                  format(sB['wr'], '.0f') + "%, expect " +
                  format(sB['exp'] * 100, '+.2f') + "%)")
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
    print("  ADVERTENCIA: 6H = swing puro, posiciones de dias.")
    print("  Muestra corta por diseño. Pasado no es futuro.")
    print("  El Carbono decide.")
    print("=" * 54)

except Exception as e:
    print("ERROR FATAL: " + str(e), flush=True)
    sys.exit(1)
