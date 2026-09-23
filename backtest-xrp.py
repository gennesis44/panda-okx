# backtest-xrp.py — GSCSI · BACKTEST XRP · LEY EMA3/21 4H + CANDADO
#   [A] NUEVA LEY:   4H EMA3/21 · LONG SL4/TP6 · SHORT SL3/TP5
#       + candado anti-rango 0.15% (estandar de flota, nacido en FET)
#   [B] CONTROL:     la misma señal SIN candado (mide el valor del lock)
#   [C] RUIDO:       high-low por vela 4h
#   FEES: 0.13% r/t descontados por trade
#   CONTEXTO: XRP post-SEC-clarity · ETFs spot con entradas activas
#     (fuente: Copilot piso 38) · $94B cap · liquidez ~$87M/dia
#   Solo lectura publica · my.okx.com · veto USDT · exit 1 en error
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
EMA_FAST      = 3
EMA_SLOW      = 21
SL_LONG       = 0.040
TP_LONG       = 0.060
SL_SHORT      = 0.030
TP_SHORT      = 0.050
RANGO_UMBRAL  = 0.15
WINDOW        = (-2, -3)
WARMUP        = 30
FEE_RT        = 0.0013
BACKTEST_DAYS = 120
MAX_PAGES     = 40

HOST = 'https://my.okx.com'
SALIDA = pathlib.Path('data/xrp-backtest.jsonl')

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


def rango_veto(efast_v, eslow_v, idx):
    """🔒 Candado anti-rango v2 — FAIL-CLOSED (.iloc, positivo).
    True (vetado) si separacion < umbral o no medible."""
    try:
        ef = float(efast_v[idx])
        es = float(eslow_v[idx])
        if es <= 0:
            return True, 0.0
        sep = abs(ef - es) / es * 100.0
        return sep < RANGO_UMBRAL, sep
    except Exception:
        return True, 0.0


def simulate_cross_lock(df, efast_v, eslow_v, sl_l, tp_l, sl_s, tp_s,
                        window, warmup, fee_rt, use_lock):
    n = len(df)
    ts = df['timestamp'].values
    hi = df['high'].values
    lo = df['low'].values
    trades = []
    vetos = 0
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
            if up or dn:
                if use_lock:
                    bloqueado, sep = rango_veto(efast_v, eslow_v, idx)
                    if bloqueado:
                        vetos += 1
                        continue
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
    return trades, vetos


def noise_report(df, label):
    hl = (df['high'] / df['low'] - 1) * 100
    print("  " + label + ": media=" + format(hl.mean(), '.2f') + "% · mediana=" +
          format(hl.median(), '.2f') + "% · P75=" + format(hl.quantile(.75), '.2f') +
          "% · P90=" + format(hl.quantile(.90), '.2f') + "% · max=" +
          format(hl.max(), '.2f') + "%")
    return {'p75': float(hl.quantile(.75)), 'mean': float(hl.mean())}


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
    print("  BACKTEST XRP - LEY EMA3/21 4H + CANDADO")
    print("  [A] CON candado 0.15% · L: SL4/TP6 · S: SL3/TP5")
    print("  [B] SIN candado (mide el valor del lock)")
    print("  [C] Ruido high-low por vela 4h")
    print("  FEES 0.13% r/t descontados · my.okx.com · USDT VETADO")
    print("  CONTEXTO: XRP post-SEC-clarity · ETFs spot activos")
    print("=" * 54)

    sym = resolve_symbol()
    print("Descargando 4h, objetivo " + str(BACKTEST_DAYS) + " dias...", flush=True)
    d4 = fetch_backward(sym['id'], '4h', BACKTEST_DAYS)

    now_ms = int(time.time() * 1000)
    if len(d4) and int(d4['timestamp'].iloc[-1]) + 4 * 3600000 > now_ms:
        d4 = d4.iloc[:-1].reset_index(drop=True)

    if len(d4) < WARMUP + 30:
        sys.exit("ABORTADO: 4H insuficiente (" + str(len(d4)) + " velas)")

    dias = (int(d4['timestamp'].iloc[-1]) - int(d4['timestamp'].iloc[0])) / 86400000
    t0 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d4['timestamp'].iloc[0] / 1000))
    t1 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d4['timestamp'].iloc[-1] / 1000))
    print("4H: " + str(len(d4)) + " velas · " + format(dias, '.1f') + " dias · " + t0 + " a " + t1)

    print("")
    print("== [C] MEDICION DE RUIDO ==")
    noise_report(d4, "4H")
    print("  Referencias: SL L 4% / S 3% · candado 0.15% · fee 0.13%")

    e3v = ema(d4['close'], EMA_FAST).values
    e21v = ema(d4['close'], EMA_SLOW).values

    print("")
    print("Simulando [A] CON candado anti-rango...", flush=True)
    trA, vetosA = simulate_cross_lock(d4, e3v, e21v, SL_LONG, TP_LONG,
                                      SL_SHORT, TP_SHORT, WINDOW, WARMUP,
                                      FEE_RT, use_lock=True)
    sA = summary(trA, 'A: 4H EMA3/21 +lock')

    print("Simulando [B] SIN candado (contraste)...", flush=True)
    trB, vetosB = simulate_cross_lock(d4, e3v, e21v, SL_LONG, TP_LONG,
                                      SL_SHORT, TP_SHORT, WINDOW, WARMUP,
                                      FEE_RT, use_lock=False)
    sB = summary(trB, 'B: SIN candado')

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
    print("  Vetos del candado [A]: " + str(vetosA) +
          " cruces vetados · [B] sin candado: " + str(len(trB)) + " entradas")
    if sA['n'] and sB['n'] and sB['n'] > sA['n']:
        bloqueados = sB['n'] - sA['n']
        print("  (el candado elimino " + str(bloqueados) +
              " entradas — su coste/beneficio se lee en la comparativa)")

    print("")
    print("VEREDICTO:")
    if sA['n'] >= 8:
        edge = sA['exp'] > 0 and sA['wr'] >= sA['be']
        if edge:
            print("  [A] XRP 4H: EXPECTATIVA POSITIVA tras fees")
            print("      → el sexto destructor tiene mar")
        else:
            print("  [A] XRP 4H: SIN VENTAJA tras fees")
            print("      → el candado no salvo este mar tampoco")
        print("      WR " + format(sA['wr'], '.0f') + "% vs breakeven " +
              format(sA['be'], '.0f') + "%")
    else:
        print("  [A] muestra insuficiente (" + str(sA['n']) + " trades)")

    SALIDA.parent.mkdir(exist_ok=True)
    with open(SALIDA, 'w') as f:
        for t in trA:
            f.write(json.dumps(t) + '\n')
    print("")
    print("Trades [A] guardados en " + str(SALIDA))

    print("")
    print("=" * 54)
    print("  ADVERTENCIA: XRP tiene flujos institucionales (ETFs)")
    print("  = gaps violentos posibles. Pasado no es futuro.")
    print("  El Carbono decide.")
    print("=" * 54)

except Exception as e:
    print("ERROR FATAL: " + str(e), flush=True)
    sys.exit(1)
