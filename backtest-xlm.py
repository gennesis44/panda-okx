# backtest-xlm.py — GSCSI · BACKTEST XLM · LEY NUEVA EMA3/10 30M
#   [A] NUEVA LEY:   30m EMA3/10 · LONG SL2.5/TP4 · SHORT SL2/TP3.5
#       + candado anti-rango 0.15% (implante quirurgico 21-sep)
#       EXTIRPACIONES: sin gate 4H · sin RSI [30-70] (radar caro jubilado)
#   [B] CONTROL:     marco viejo de produccion 15m EMA7/21 SL1/TP1.5
#   [C] RUIDO:       high-low por vela 30m y 15m
#   FEES: 0.13% r/t descontados por trade
#   NOTA: la ley nueva NO simula el gate 4H ni el RSI (extirpados por
#     decreto) — es cruce puro EMA3/10 30m con candado de separacion.
#   Solo lectura publica · my.okx.com · veto USDT · exit 1 en error
import os
import sys
import time
import json
import pathlib
import ccxt
import pandas as pd

# AXIOMAS DEL CARBONO
BASE_ASSET    = 'XLM'
TIMEFRAME_A   = '30m'
EMA_FAST      = 3
EMA_SLOW      = 10
SL_LONG       = 0.025
TP_LONG       = 0.040
SL_SHORT      = 0.020
TP_SHORT      = 0.035
RANGO_UMBRAL  = 0.15
WINDOW_A      = (-2, -3)
WARMUP_A      = 40
TIMEFRAME_B   = '15m'
SL_B          = 0.010
TP_B          = 0.015
WINDOW_B      = (-2, -3)
WARMUP_B      = 30
FEE_RT        = 0.0013
BACKTEST_DAYS = 90
MAX_PAGES     = 40

HOST = 'https://my.okx.com'
SALIDA = pathlib.Path('data/xlm-v2-backtest.jsonl')

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
    sys.exit("ABORTADO: sin XLM/USD activo, USDT vetado")


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
    """🔒 Candado anti-rango v2 — FAIL-CLOSED.
    True (vetado) si separacion < umbral o si no se puede medir."""
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
    """Cruces en ventana + candado anti-rango + fees descontados."""
    n = len(df)
    ts = df['timestamp'].values
    hi = df['high'].values
    lo = df['low'].values
    trades = []
    vetos_lock = 0
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
                        vetos_lock += 1
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
    return trades, vetos_lock


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
    print("  BACKTEST XLM - LEY NUEVA EMA3/10 30M")
    print("  [A] Nueva ley 30m EMA3/10 + candado 0.15%")
    print("      L: SL2.5/TP4 · S: SL2/TP3.5")
    print("      EXTIRPADO: gate 4H · RSI [30-70]")
    print("  [B] Control 15m EMA7/21 SL1 TP1.5 (produccion vieja)")
    print("  [C] Ruido high-low por vela")
    print("  FEES 0.13% r/t descontados · my.okx.com · USDT VETADO")
    print("=" * 54)

    sym = resolve_symbol()
    print("Descargando 30m y 15m, objetivo " + str(BACKTEST_DAYS) + " dias...", flush=True)
    d30 = fetch_backward(sym['id'], '30m', BACKTEST_DAYS)
    d15 = fetch_backward(sym['id'], '15m', BACKTEST_DAYS)

    now_ms = int(time.time() * 1000)
    if len(d30) and int(d30['timestamp'].iloc[-1]) + 1800000 > now_ms:
        d30 = d30.iloc[:-1].reset_index(drop=True)
    if len(d15) and int(d15['timestamp'].iloc[-1]) + 900000 > now_ms:
        d15 = d15.iloc[:-1].reset_index(drop=True)

    if len(d30) < WARMUP_A + 100:
        sys.exit("ABORTADO: 30m insuficiente (" + str(len(d30)) + " velas)")
    if len(d15) < WARMUP_B + 200:
        sys.exit("ABORTADO: 15m insuficiente (" + str(len(d15)) + " velas)")

    dias = (int(d30['timestamp'].iloc[-1]) - int(d30['timestamp'].iloc[0])) / 86400000
    t0 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d30['timestamp'].iloc[0] / 1000))
    t1 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d30['timestamp'].iloc[-1] / 1000))
    print("30m: " + str(len(d30)) + " velas · " + format(dias, '.1f') + " dias · " + t0 + " a " + t1)
    print("15m: " + str(len(d15)) + " velas")

    print("")
    print("== [C] MEDICION DE RUIDO ==")
    n30 = noise_report(d30, "30m")
    n15 = noise_report(d15, "15m")
    print("  Referencias: SL LONG 2.5% · SL SHORT 2.0% · candado 0.15% · fee 0.13%")

    e3v = ema(d30['close'], EMA_FAST).values
    e10v = ema(d30['close'], EMA_SLOW).values
    print("")
    print("Simulando [A] Nueva ley 30m EMA3/10 + candado...", flush=True)
    trA, vetos = simulate_cross_lock(d30, e3v, e10v, SL_LONG, TP_LONG,
                                     SL_SHORT, TP_SHORT, WINDOW_A, WARMUP_A,
                                     FEE_RT, use_lock=True)
    sA = summary(trA, 'A: 30m EMA3/10 +lock')

    e7v = ema(d15['close'], 7).values
    e21v = ema(d15['close'], 21).values
    print("Simulando [B] Control 15m EMA7/21 SL1 TP1.5...", flush=True)
    trB, vetosB = simulate_cross_lock(d15, e7v, e21v, SL_B, TP_B,
                                      SL_B, TP_B, WINDOW_B, WARMUP_B,
                                      FEE_RT, use_lock=False)
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

    print("")
    print("  Candado anti-rango [A]: " + str(vetos) + " cruces vetados " +
          "(separacion < " + format(RANGO_UMBRAL, '.2f') + "%)")

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
            print("  [B] Marco viejo: SIN ventaja tras fees (WR " +
                  format(sB['wr'], '.0f') + "%) — sentencia confirmada")
        else:
            print("  [B] Marco viejo: tenia ventaja bruta (WR " +
                  format(sB['wr'], '.0f') + "%) — el live la degrada")
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
    print("  ADVERTENCIA: la ley nueva NO simula la cadencia del cron")
    print("  (se simula la SENAL, no el timing del bot). Pasado no es")
    print("  futuro. El Carbono decide.")
    print("=" * 54)

except Exception as e:
    print("ERROR FATAL: " + str(e), flush=True)
    sys.exit(1)
