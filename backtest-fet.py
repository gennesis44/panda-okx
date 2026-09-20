# backtest-fet.py — GSCSI · BACKTEST FET · 3 variantes en una corrida
#   [A] NUEVA LEY:   1H EMA5/100 · LONG SL6/TP7.5 · SHORT SL3/TP4
#   [B] CONTROL:     30m EMA7/21 · SL1/TP1.5  (bot enterrado)
#   [C] RUIDO:       high-low por vela (hipotesis Gemini/Grok)
# v2 fix: paginacion BACKWARD desde el presente (la serie XPERP FET-2031
#   es joven — pedir 'since' de 90 dias atras devolvia 0 velas).
#   Reporta cobertura real de datos. Solo lectura publica. exit 1 en error.
import os
import sys
import time
import json
import pathlib
import ccxt
import pandas as pd

# ══ AXIOMAS DEL CARBONO ══
BASE_ASSET   = 'FET'
TIMEFRAME_A  = '1h'
EMA_FAST     = 5
EMA_SLOW     = 100
SL_LONG,  TP_LONG  = 0.060, 0.075
SL_SHORT, TP_SHORT = 0.030, 0.040
WINDOW_A     = (-2, -3, -4)
WARMUP_A     = 130
TIMEFRAME_B  = '30m'
SL_B, TP_B   = 0.010, 0.015
WINDOW_B     = (-2, -3)
WARMUP_B     = 30
BACKTEST_DAYS = 60            # objetivo — se descarga lo que EXISTA de historia
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
        print(f"Instrumento: {m['id']} (XPERP)")
        return m
    if swp:
        print(f"Instrumento: {swp[0]['id']} (fallback swap-USD)")
        return swp[0]
    sys.exit("ABORTADO: sin FET/USD activo (USDT vetado)")


def fetch_backward(symbol, timeframe, days):
    """Descarga desde AHORA hacia atras con 'until'. Robusto para
    instrumentos recientemente listados: devuelve lo que exista."""
    now_ms = int(time.time() * 1000)
    target = now_ms - days * 86400000
    out, until, pages = [], None, 0
    while pages < MAX_PAGES:
        params = {'until': until} if until else {}
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
    # recorte suave al objetivo (tolerancia de 1 vela)
    if len(df):
        df = df[df['timestamp'] >= target - 7200000].reset_index(drop=True)
    return df


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def simulate_cross(df, efast_v, eslow_v, sl_pct, tp_pct, window, warmup,
                   asymmetric=False, sl_s=None, tp_s=None):
    """Simulador: cruce en ventana de velas CERRADAS, entrada a apertura
    de la vela siguiente, SL primero dentro de la misma vela (conservador)."""
    n = len(df)
    ts = df['timestamp'].values
    hi, lo = df['high'].values, df['low'].values
    trades = []
    i = warmup
    while i < n - 1:
        sig, s_ts = None, None
        for k in window:
            idx = i + k
            if idx < warmup or idx >= n:
                continue
            up = efast_v[idx - 1] <= eslow_v[idx - 1] and efast_v[idx] > eslow_v[idx]
            dn = efast_v[idx - 1] >= eslow_v[idx - 1] and efast_v[idx] < eslow_v[idx]
            if up:
                sig, s_ts = 'LONG', int(ts[idx])
            elif dn:
                sig, s_ts = 'SHORT', int(ts[idx])
            if sig:
                break
        if not sig:
            i += 1
            continue
        entry_i = i + 1
        entry = df['open'].values[entry_i]
        if sig == 'LONG':
            sl, tp = entry * (1 - sl_pct), entry * (1 + tp_pct)
        else:
            sl = entry * (1 + (sl_s if sl_s is not None else sl_pct))
            tp = entry * (1 - (tp_s if tp_s is not None else tp_pct))
        exit_i, reason, px = None, None, None
        for j in range(entry_i, n):
            if sig == 'LONG':
                if lo[j] <= sl:
                    exit_i, reason, px = j, 'SL', sl; break
                if hi[j] >= tp:
                    exit_i, reason, px = j, 'TP', tp; break
            else:
                if hi[j] >= sl:
                    exit_i, reason, px = j, 'SL', sl; break
                if lo[j] <= tp:
                    exit_i, reason, px = j, 'TP', tp; break
        if exit_i is None:
            exit_i, reason, px = n - 1, 'OPEN', df['close'].values[n - 1]
        gross = (px / entry - 1) if sig == 'LONG' else (1 - px / entry)
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
    print(f"  {label}: media={hl.mean():.2f}% · mediana={hl.median():.2f}% · "
          f"P75={hl.quantile(.75):.2f}% · P90={hl.quantile(.90):.2f}% · "
          f"max={hl.max():.2f}%")
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
    pf = (sum(t['net'] for t in wins) / abs(sum(t['net'] for t in losses))
          if losses and sum(t['net'] for t in losses) != 0 else 0)
    be = avgL / (avgL + avgW) * 100 if avgW > 0 and avgL > 0 else 0
    return {'label': label, 'n': n, 'wr': wr, 'exp': exp, 'net': net,
            'tp': sum(1 for t in closed if t['reason'] == 'TP'),
            'sl': sum(1 for t in closed if t['reason'] == 'SL'),
            'pf': pf, 'be': be}


try:
    print("=" * 54)
    print("  BACKTEST FET — 3 variantes en una corrida")
    print("  [A] Nueva ley 1H EMA5/100 (SL6/TP7.5 · SL3/TP4)")
    print("  [B] Control bot enterrado 30m EMA7/21 (SL1/TP1.5)")
    print("  [C] Ruido: high-low por vela (hipotesis Gemini/Grok)")
    print("  fuente: velas publicas my.okx.com · USDT VETADO")
    print("=" * 54)

    sym = resolve_symbol()
    print(f"Descargando 1h y 30m (objetivo ~{BACKTEST_DAYS} dias, "
          f"paginando hacia atras)...", flush=True)
    d1h = fetch_backward(sym['id'], '1h', BACKTEST_DAYS)
    d30 = fetch_backward(sym['id'], '30m', BACKTEST_DAYS)

    # descartar vela en formacion
    now_ms = int(time.time() * 1000)
    if len(d1h) and int(d1h['timestamp'].iloc[-1]) + 3600000 > now_ms:
        d1h = d1h.iloc[:-1].reset_index(drop=True)
    if len(d30) and int(d30['timestamp'].iloc[-1]) + 1800000 > now_ms:
        d30 = d30.iloc[:-1].reset_index(drop=True)

    if len(d1h) < WARMUP_A + 50:
        sys.exit(f"ABORTADO: 1H insuficiente ({len(d1h)} velas — "
                 f"la serie puede ser muy joven)")
    if len(d30) < WARMUP_B + 50:
        sys.exit(f"ABORTADO: 30m insuficiente ({len(d30)} velas)")

    dias = (int(d1h['timestamp'].iloc[-1]) - int(d1h['timestamp'].iloc[0])) / 86400000
    print(f"1H : {len(d1h)} velas · {dias:.1f} dias de cobertura real · "
          f"{time.strftime('%y-%m-%d %H:%M', time.gmtime(d1h['timestamp'].iloc[0]/1000))} → "
          f"{time.strftime('%y-%m-%d %H:%M', time.gmtime(d1h['timestamp'].iloc[-1]/1000))}")
    print(f"30m: {len(d30)} velas")

    # ── [C] RUIDO ──
    print("\n== [C] MEDICION DE RUIDO (
