# backtest-fet.py — GSCSI · BACKTEST FET · 3 variantes en una corrida:
#   [A] NUEVA LEY (decreto 20-sep): 1H EMA5/100 · LONG SL6/TP7.5 · SHORT SL3/TP4
#   [B] CONTROL (bot enterrado):    30m EMA7/21 · SL1/TP1.5 · 1 posicion
#   [C] MEDICION DE RUIDO:          high-low por vela 1H y 15m (hipotesis Gemini/Grok)
# Solo lectura publica (sin credenciales) · my.okx.com · veto USDT · exit 1
# NOTA honesta: sin fees por simplicidad de comparacion — el ratio bruto
#   ya indica viabilidad; para EV neta restar ~0.13% r/t (fees+slip).
import os
import sys
import time
import json
import pathlib
import ccxt
import pandas as pd
import numpy as np

# ══ AXIOMAS DEL CARBONO ══
BASE_ASSET   = 'FET'
# [A] Nueva ley
TIMEFRAME_A  = '1h'
EMA_FAST     = 5
EMA_SLOW     = 100
SL_LONG,  TP_LONG  = 0.060, 0.075
SL_SHORT, TP_SHORT = 0.030, 0.040
WINDOW_A     = (-2, -3, -4)
WARMUP_A     = 130
# [B] Control (bot enterrado)
TIMEFRAME_B  = '30m'
SL_B, TP_B   = 0.010, 0.015
WINDOW_B     = (-2, -3)
WARMUP_B     = 30
BACKTEST_DAYS = 90
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
        if _settle(m) == 'USDT':          # veto USDT
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


def fetch_full(symbol, timeframe, days, limit_per=300):
    target = int(time.time() * 1000) - days * 86400000
    out, since, pages = [], target, 0
    while pages < MAX_PAGES:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit_per)
        if not batch:
            break
        fresh = [r for r in batch if not out or r[0] > out[-1][0]]
        if not fresh:
            break
        out.extend(fresh)
        since = out[-1][0] + 1
        pages += 1
        if out[-1][0] >= int(time.time() * 1000) - 1:
            break
        time.sleep(0.15)
    df = pd.DataFrame(out, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
    return df


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def simulate_cross(df, efast_v, eslow_v, sl_pct, tp_pct, window, warmup,
                   asymmetric=False, sl_s=None, tp_s=None):
    """Simulador generico: cruces en ventana, SL/TP por vela (SL primero)."""
    n = len(df)
    ts = df['timestamp'].values
    hi, lo = df['high'].values, df['low'].values
    trades = []
    i = warmup
    while i < n - 1:
        sig = None
        for k in window:
            idx = i + k if k < 0 else k
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
            sl = entry * (1 - sl_pct); tp = entry * (1 + tp_pct)
        else:
            sl = entry * (1 + (sl_s or sl_pct)); tp = entry * (1 - (tp_s or tp_pct))
        j, reason, px = None, None, None
        while j is None or j < n:
            if j is None:
                j = entry_i
            if j >= n:
                exit_i, reason, px = n - 1, 'OPEN', df['close'].values[n - 1]
                break
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
            j += 1
        if reason is None:
            exit_i, reason, px = n - 1, 'OPEN', df['close'].values[n - 1]
        gross = (px / entry - 1) if sig == 'LONG' else (1 - px / entry)
        trades.append({'dir': sig, 'entry_time': int(ts[entry_i]), 'entry': float(entry),
                       'exit': float(px), 'reason': reason, 'net': float(gross),
                       'signal_time': s_ts})
        if reason == 'OPEN':
            break
        i = exit_i + 1
    return trades


def noise_report(df, label):
    hl = (df['high'] / df['low'] - 1) * 100
    print(f"  {label}: media={hl.mean():.2f}% · mediana={hl.median():.2f}% · "
          f"P75={hl.quantile(.75):.2f}% · P90={hl.quantile(.90):.2f}% · "
          f"max={hl.max():.2f}%")
    return {'mean': float(hl.mean()), 'median': float(hl.median()),
            'p75': float(hl.quantile(.75)), 'p90': float(hl.quantile(.90))}


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
    print(f"Descargando 1h y 30m (~{BACKTEST_DAYS} dias)...")
    d1h = fetch_full(sym['id'], '1h', BACKTEST_DAYS)
    d30 = fetch_full(sym['id'], '30m', BACKTEST_DAYS)

    # descartar vela en formacion
    now_ms = int(time.time() * 1000)
    if len(d1h) and int(d1h['timestamp'].iloc[-1]) + 3600000 > now_ms:
        d1h = d1h.iloc[:-1].reset_index(drop=True)
    if len(d30) and int(d30['timestamp'].iloc[-1]) + 1800000 > now_ms:
        d30 = d30.iloc[:-1].reset_index(drop=True)

    if len(d1h) < WARMUP_A + 50:
        sys.exit(f"ABORTADO: 1H insuficiente ({len(d1h)})")
    if len(d30) < WARMUP_B + 50:
        sys.exit(f"ABORTADO: 30m insuficiente ({len(d30)})")

    dias = (int(d1h['timestamp'].iloc[-1]) - int(d1h['timestamp'].iloc[0])) / 86400000
    print(f"1H : {len(d1h)} velas · {dias:.0f} dias")
    print(f"30m: {len(d30)} velas")

    # ── [C] RUIDO ──
    print("\n== [C] MEDICION DE RUIDO (high-low por vela) ==")
    n1 = noise_report(d1h, "1H ")
    n15 = noise_report(fetch_full(sym['id'], '15m', 30), "15m(30d)")
    hip = (n1['p75'] > 2.2) or (n15['p90'] > 2.2)
    print(f"  Hipotesis Gemini/Grok (ruido > 2.2% en FET): "
          f"{'CONFIRMADA' if hip else 'REFUTADA a este marco'}")

    # ── [A] NUEVA LEY ──
    e5v = ema(d1h['close'], EMA_FAST).values
    e100v = ema(d1h['close'], EMA_SLOW).values
    print("\nSimulando [A] Nueva ley 1H EMA5/100...", flush=True)
    trA = simulate_cross(d1h, e5v, e100v, SL_LONG, TP_LONG, WINDOW_A,
                         WARMUP_A, asymmetric=True, sl_s=SL_SHORT, tp_s=TP_SHORT)
    sA = summary(trA, 'A: 1H EMA5/100 SL6/TP7.5')

    # ── [B] CONTROL ──
    e7v = ema(d30['close'], 7).values
    e21v = ema(d30['close'], 21).values
    print("Simulando [B] Control 30m EMA7/21 SL1/TP1.5...", flush=True)
    trB = simulate_cross(d30, e7v, e21v, SL_B, TP_B, WINDOW_B, WARMUP_B)
    sB = summary(trB, 'B: 30m EMA7/21 SL1/TP1.5')

    # ── RESULTADOS ──
    print("\n" + "=" * 54)
    print("RESULTADOS (brutos, sin fees):")
    print("=" * 54)
    for s in (sA, sB):
        if s['n']:
            print(f"  {s['label']}")
            print(f"    trades:{s['n']}  TP:{s['tp']} SL:{s['sl']}  "
                  f"WR:{s['wr']:.0f}% (breakeven~{s['be']:.0f}%)  "
                  f"expect:{s['exp'] * 100:+.2f}%/trade  net:{s['net'] * 100:+.1f}%  "
                  f"PF:{s['pf']:.2f}")
        else:
            print(f"  {s['label']}: sin trades cerrados")

    # veredicto comparativo
    print("\nVEREDICTO:")
    if sA['n'] >= 5:
        edge = sA['exp'] > 0 and sA['wr'] >= sA['be']
        print(f"  [A] Nueva ley: {'EXPECTATIVA POSITIVA' if edge else 'SIN VENTAJA demostrada'} "
              f"(WR {sA['wr']:.0f}% vs breakeven {sA['be']:.0f}%)")
    else:
        print("  [A] Nueva ley: muestra insuficiente (cruces EMA5/100 son raros por diseño)")
    if sB['n'] >= 5:
        print(f"  [B] Bot enterrado: {'tenia ventaja' if sB['exp'] > 0 else 'NO tenia ventaja'} "
              f"(WR {sB['wr']:.0f}%, expect {sB['exp'] * 100:+.2f}%) — "
              f"el problema era el marco, no el activo" if sB['exp'] <= 0 else
              f"  [B] Bot enterrado: tenia ventaja bruta (WR {sB['wr']:.0f}% — la ejecucion lo enterró)")
    else:
        print("  [B] Control: muestra insuficiente")

    # persistencia variante A
    SALIDA.parent.mkdir(exist_ok=True)
    with open(SALIDA, 'w') as f:
        for t in trA:
            f.write(json.dumps(t) + '\n')
    print(f"\nTrades [A] guardados en {SALIDA}")

    print("\n" + "=" * 54)
    print("  ADVERTENCIA: pasado ≠ futuro. Brutos sin fees (~0.13% r/t).")
    print("  Mismo-vela SL-primero = conservador. El Carbono decide.")
    print("=" * 54)

except Exception as e:
    print(f"ERROR FATAL: {e}", flush=True)
    sys.exit(1)
