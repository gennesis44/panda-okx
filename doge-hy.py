# doge-hy.py — GSCSI · BACKTEST DOGE · LEY EMA3/21 EN 2m
#   Decreto Carbono (pendiente de validacion): EMA3/21 velas 2m CERRADAS
#   Geometria propuesta (calculada con lupa sobre movimiento medido):
#     LONG : SL 2.0% / TP 3.0%   (sobrevive retroceso de 1.9% medido en rally)
#     SHORT: SL 1.5% / TP 2.5%   (ajustado — memecoin sube mas facil)
#   [B] CONTROL: marco actual de produccion (1h EMA7/21 SL1/TP1.5)
#   [C] RUIDO: high-low por vela 2m
#   Fees: 0.13% ida y vuelta DESCONTADOS en cada trade (critico en 2m)
#   Solo lectura publica · my.okx.com · veto USDT · exit 1 en error
import os
import sys
import time
import json
import pathlib
import ccxt
import pandas as pd

# AXIOMAS DEL CARBONO
BASE_ASSET    = 'DOGE'
TIMEFRAME_A   = '2m'
EMA_FAST      = 3
EMA_SLOW      = 21
SL_LONG       = 0.020
TP_LONG       = 0.030
SL_SHORT      = 0.015
TP_SHORT      = 0.025
WINDOW_A      = (-2, -3)
WARMUP_A      = 40
TIMEFRAME_B   = '1h'
SL_B          = 0.010
TP_B          = 0.015
WINDOW_B      = (-2, -3)
WARMUP_B      = 30
FEE_RT        = 0.0013          # 0.13% ida y vuelta (taker + slippage)
BACKTEST_DAYS = 30
MAX_PAGES     = 150

HOST = 'https://my.okx.com'
SALIDA = pathlib.Path('data/doge-backtest.jsonl')

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
    sys.exit("ABORTADO: sin DOGE/USD activo, USDT vetado")


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
        time.sleep(0.12)
    df = pd.DataFrame(out, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
    if len(df):
        df = df[df['timestamp'] >= target - 3600000].reset_index(drop=True)
    return df


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def simulate_cross(df, efast_v, eslow_v, sl_l, tp_l, sl_s, tp_s, window, warmup, fee_rt):
    """Cruces en ventana, entrada a apertura de vela siguiente,
    SL primero en misma vela, FEES descontados de cada trade."""
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
                       'gross': float(gross), 'net': float(net)})
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
    print("  BACKTEST DOGE - LEY EMA3/21 EN 2m")
    print("  [A] Nueva ley 2m EMA3/21 L: SL2/TP3 · S: SL1.5/TP2.5")
    print("  [B] Control 1h EMA7/21 SL1 TP1.5 (produccion)")
    print("  [C] Ruido high-low por vela 2m")
    print("  FEES 0.13% r/t descontados · my.okx.com · USDT VETADO")
    print("=" * 54)

    sym = resolve_symbol()
    print("Descargando 2m y 1h, objetivo " + str(BACKTEST_DAYS) + " dias...", flush=True)
    d2 = fetch_backward(sym['id'], '2m', BACKTEST_DAYS)
    d1h = fetch_backward(sym['id'], '1h', BACKTEST_DAYS)

    now_ms = int(time.time() * 1000)
    if len(d2) and int(d2['timestamp'].iloc[-1]) + 120000 > now_ms:
        d2 = d2.iloc[:-1].reset_index(drop=True)
    if len(d1h) and int(d1h['timestamp'].iloc[-1]) + 3600000 > now_ms:
        d1h = d1h.iloc[:-1].reset_index(drop=True)

    if len(d2) < WARMUP_A + 200:
        sys.exit("ABORTADO: 2m insuficiente (" + str(len(d2)) + " velas)")
    if len(d1h) < WARMUP_B + 50:
        sys.exit("ABORTADO: 1h insuficiente (" + str(len(d1h)) + " velas)")

    dias = (int(d2['timestamp'].iloc[-1]) - int(d2['timestamp'].iloc[0])) / 86400000
    t0 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d2['timestamp'].iloc[0] / 1000))
    t1 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d2['timestamp'].iloc[-1] / 1000))
    print("2m : " + str(len(d2)) + " velas · " + format(dias, '.1f') + " dias · " + t0 + " a " + t1)
    print("1h : " + str(len(d1h)) + " velas")

    print("")
    print("== [C] MEDICION DE RUIDO 2m ==")
    nz = noise_report(d2, "2m")
    print("  Referencias: SL LONG 2.0% · SL SHORT 1.5% · fee r/t 0.13%")

    e3v = ema(d2['close'], EMA_FAST).values
    e21v = ema(d2['close'], EMA_SLOW).values
    print("")
    print("Simulando [A] Nueva ley 2m EMA3/21 (con fees)...", flush=True)
    trA = simulate_cross(d2, e3v, e21v, SL_LONG, TP_LONG, SL_SHORT, TP_SHORT,
                         WINDOW_A, WARMUP_A, FEE_RT)
    sA = summary(trA, 'A: 2m EMA3/21 L2/3 S1.5/2.5')

    e7v = ema(d1h['close'], 7).values
    e21v = ema(d1h['close'], 21).values
    print("Simulando [B] Control 1h EMA7/21 SL1 TP1.5 (con fees)...", flush=True)
    trB = simulate_cross(d1h, e7v, e21v, SL_B, TP_B, SL_B, TP_B,
                         WINDOW_B, WARMUP_B, FEE_RT)
    sB = summary(trB, 'B: 1h EMA7/21 SL1 TP1.5')

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

    print("")
    print("VEREDICTO:")
    if sA['n'] >= 10:
        edge = sA['exp'] > 0 and sA['wr'] >= sA['be']
        if edge:
            print("  [A] Nueva ley: EXPECTATIVA POSITIVA tras fees")
        else:
            print("  [A] Nueva ley: SIN VENTAJA tras fees")
        print("      WR " + format(sA['wr'], '.0f') + "% vs breakeven " +
              format(sA['be'], '.0f') + "%")
    else:
        print("  [A] Nueva ley: muestra insuficiente (" + str(sA['n']) + " trades)")
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
    print("  ADVERTENCIA: 2m es territorio de micro-ruido y fees.")
    print("  El backtest ya los descuenta. Pasado no es futuro.")
    print("  El Carbono decide.")
    print("=" * 54)

except Exception as e:
    print("ERROR FATAL: " + str(e), flush=True)
    sys.exit(1)
