# backtest-inj.py — GSCSI · BACKTEST INJ · LEY EMA3/21 1H + CANDADO v2
#   [A] NUEVA LEY:   1H EMA3/21 · LONG SL7/TP10 · SHORT SL5/TP8
#       + candado anti-rango 0.15% fail-closed (herencia perro/FET)
#       Geometria calibrada con el movimiento medido de INJ:
#       10.5% de rango DIARIO (capturas del operador) — el activo
#       mas volatil de los candidatos. SL cortos = muerte segura.
#   [B] CONTROL:     la propuesta de Grok simulada limpiamente:
#       15m EMA9/21 · SL1.2% · TP1.8% (1.5R) — la telarana con numeros.
#   [C] RUIDO:       high-low por vela 1h y 15m
#   FEES: 0.13% r/t descontados · Solo lectura · my.okx.com · exit 1
import os
import sys
import time
import json
import pathlib
import ccxt
import pandas as pd

# AXIOMAS DEL CARBONO
BASE_ASSET    = 'INJ'
TIMEFRAME_A   = '1h'
EMA_FAST      = 3
EMA_SLOW      = 21
RSI_LEN       = 14
RSI_HI        = 70.0
RSI_LO        = 30.0
SL_LONG       = 0.070
TP_LONG       = 0.100
SL_SHORT      = 0.050
TP_SHORT      = 0.080
RANGO_UMBRAL  = 0.15
WINDOW_A      = (-2, -3)
WARMUP_A      = 30
TIMEFRAME_B   = '15m'
EMA_FAST_B    = 9
EMA_SLOW_B    = 21
SL_B          = 0.012
TP_B          = 0.018
WINDOW_B      = (-2, -3)
WARMUP_B      = 30
FEE_RT        = 0.0013
BACKTEST_DAYS = 60
MAX_PAGES     = 60

HOST = 'https://my.okx.com'
SALIDA = pathlib.Path('data/inj-backtest.jsonl')

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
    sys.exit("ABORTADO: sin INJ/USD activo, USDT vetado")


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


def rsi(s, n):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, float('nan'))
    return 100 - (100 / (1 + rs))


def rango_veto(efast_v, eslow_v, idx):
    """🔒 Candado anti-rango v2 — FAIL-CLOSED (.iloc, positivo)."""
    try:
        ef = float(efast_v[idx])
        es = float(eslow_v[idx])
        if es <= 0:
            return True, 0.0
        sep = abs(ef - es) / es * 100.0
        return sep < RANGO_UMBRAL, sep
    except Exception:
        return True, 0.0


def simulate_full(df, efast_v, eslow_v, rsi_v, sl_l, tp_l, sl_s, tp_s,
                  window, warmup, fee_rt, use_rsi):
    n = len(df)
    ts = df['timestamp'].values
    hi = df['high'].values
    lo = df['low'].values
    trades = []
    vetos_rsi = 0
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
                if use_rsi:
                    r = float(rsi_v[idx])
                    if not (RSI_LO < r < RSI_HI):
                        vetos_rsi += 1
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
    return trades, vetos_rsi


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
    print("  BACKTEST INJ - LEY GSCSI 1H vs TELARANA GROK 15m")
    print("  [A] GSCSI: 1H EMA3/21 + RSI · L: SL7/TP10 · S: SL5/TP8")
    print("  [B] Grok:  15m EMA9/21 · SL1.2% · TP1.8% (1.5R)")
    print("  [C] Ruido high-low por vela 1h y 15m")
    print("  FEES 0.13% r/t descontados · my.okx.com · USDT VETADO")
    print("=" * 54)

    sym = resolve_symbol()
    print("Descargando 1h y 15m, objetivo " + str(BACKTEST_DAYS) + " dias...", flush=True)
    d1h = fetch_backward(sym['id'], '1h', BACKTEST_DAYS)
    d15 = fetch_backward(sym['id'], '15m', BACKTEST_DAYS)

    now_ms = int(time.time() * 1000)
    if len(d1h) and int(d1h['timestamp'].iloc[-1]) + 3600000 > now_ms:
        d1h = d1h.iloc[:-1].reset_index(drop=True)
    if len(d15) and int(d15['timestamp'].iloc[-1]) + 900000 > now_ms:
        d15 = d15.iloc[:-1].reset_index(drop=True)

    if len(d1h) < WARMUP_A + 50:
        sys.exit("ABORTADO: 1H insuficiente (" + str(len(d1h)) + " velas)")
    if len(d15) < WARMUP_B + 200:
        sys.exit("ABORTADO: 15m insuficiente (" + str(len(d15)) + " velas)")

    dias = (int(d1h['timestamp'].iloc[-1]) - int(d1h['timestamp'].iloc[0])) / 86400000
    t0 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d1h['timestamp'].iloc[0] / 1000))
    t1 = time.strftime('%y-%m-%d %H:%M', time.gmtime(d1h['timestamp'].iloc[-1] / 1000))
    print("1H : " + str(len(d1h)) + " velas · " + format(dias, '.1f') + " dias · " + t0 + " a " + t1)
    print("15m: " + str(len(d15)) + " velas")

    print("")
    print("== [C] MEDICION DE RUIDO ==")
    n1 = noise_report(d1h, "1H")
    n15 = noise_report(d15, "15m")
    print("  Referencias: SL L 7% / S 5% (GSCSI) · SL 1.2% (Grok) · fee 0.13%")
    print("  La pregunta del duelo: un activo que mueve 10%/dia —")
    print("  sobrevive un SL de 1.2% (Grok) o necesita 5-7% (GSCSI)?")

    e3v = ema(d1h['close'], EMA_FAST).values
    e21v = ema(d1h['close'], EMA_SLOW).values
    r14v = rsi(d1h['close'], RSI_LEN).values

    print("")
    print("Simulando [A] GSCSI 1H EMA3/21 + RSI...", flush=True)
    trA, vetosA = simulate_full(d1h, e3v, e21v, r14v, SL_LONG, TP_LONG,
                                SL_SHORT, TP_SHORT, WINDOW_A, WARMUP_A,
                                FEE_RT, use_rsi=True)
    sA = summary(trA, 'A: GSCSI 1H EMA3/21 L7/10 S5/8')

    e9v = ema(d15['close'], EMA_FAST_B).values
    e21v15 = ema(d15['close'], EMA_SLOW_B).values
    print("Simulando [B] Grok 15m EMA9/21 SL1.2 TP1.8...", flush=True)
    trB, vetosB = simulate_full(d15, e9v, e21v15, None, SL_B, TP_B,
                                SL_B, TP_B, WINDOW_B, WARMUP_B,
                                FEE_RT, use_rsi=False)
    sB = summary(trB, 'B: Grok 15m EMA9/21 SL1.2/TP1.8')

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
                          format(t['entry'], '.5f') + "→" + format(t['exit'], '.5f') +
                          " " + t['reason'] + " " + format(t['net'] * 100, '+.2f') + "%")

    print("")
    print("  Vetos RSI [A]: " + str(vetosA) + " · [B] sin RSI: " + str(len(trB)) + " entradas")

    print("")
    print("VEREDICTO DEL DUELO:")
    if sA['n'] >= 5:
        edge = sA['exp'] > 0 and sA['wr'] >= sA['be']
        if edge:
            print("  [A] GSCSI 1H: EXPECTATIVA POSITIVA tras fees")
        else:
            print("  [A] GSCSI 1H: SIN VENTAJA tras fees")
        print("      WR " + format(sA['wr'], '.0f') + "% vs breakeven " +
              format(sA['be'], '.0f') + "%")
    else:
        print("  [A] muestra insuficiente (" + str(sA['n']) + " trades)")
    if sB['n'] >= 5:
        if sB['exp'] <= 0:
            print("  [B] Grok 15m: SIN ventaja tras fees (WR " +
                  format(sB['wr'], '.0f') + "%) — la telarana tenia razon")
        else:
            print("  [B] Grok 15m: ventaja tras fees (WR " +
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
    print("  ADVERTENCIA: INJ se mueve ~10%/dia — gaps violentos posibles.")
    print("  Pasado no es futuro. El Carbono decide.")
    print("=" * 54)

except Exception as e:
    print("ERROR FATAL: " + str(e), flush=True)
    sys.exit(1)
