# backtest-xlm.py — GSCSI · BACKTEST DEMO XLM (Ax2-D + Ax3.3 RSI-BANDA)
# Simula la estrategia de main-xlm.py sobre histórico real de OKX Europe.
# NO EJECUTA ORDENES. Solo lectura publica (sin credenciales).
# Reglas emuladas (identicas a produccion):
#   - Detonante: cruce EMA7/21 en velas 15m CERRADAS, ventana -2/-3
#   - Brujula: ultima vela 4H CERRADA en paralelo (gate direccion)
#   - Ax3.3: RSI(14) 15m fuera de [30-70] = VETO (variante CON filtro)
#   - SL 1% / TP 1.5% (adjuntos, chequeo intra-vela, SL primero = conservador)
#   - Cooldown 60 min tras perdida · 1 posicion a la vez
#   - Cadencia de chequeo: cada 30 min (cron 3,33 — privilegio XLM)
#   - Entrada ~ apertura de la vela siguiente al cierre de la vela senal
# Honesto por diseno: fees taker incluidos (constante), mismo-candle SL primero,
# cobertura real de historico reportada. Veto USDT · my.okx.com · exit 1 en error.
import os
import sys
import time
import json
import pathlib
import ccxt
import pandas as pd

# ══ AXIOMAS DEL CARBONO (identicos a produccion) ══
BASE_ASSET   = 'XLM'
SL_PCT       = 0.010
TP_PCT       = 0.015
TIMEFRAME    = '15m'
TF_FILTER    = '4h'
COOLDOWN_MIN = 60
RSI_LEN      = 14
RSI_HI       = 70.0
RSI_LO       = 30.0
FEE_PCT      = 0.0005          # taker por lado (conservador) — 0.10% ida y vuelta
BACKTEST_DAYS = 45             # profundidad objetivo de 15m
CHECK_EVERY  = 2               # cron 30 min = chequeo cada 2 velas de 15m
WARMUP       = 30              # velas 15m de arranque para EMAs/RSI honestos
MAX_PAGES    = 60

HOST = 'https://my.okx.com'
SALIDA = pathlib.Path('data/xlm-backtest.jsonl')

exchange = ccxt.okx({
    'enableRateLimit': True,
    'options':  {'defaultType': 'swap'},
    'urls':     {'api': {'rest': HOST}},      # EEE — obligatorio
})


def _exp(m):
    try:
        return int((m.get('info') or {}).get('expTime') or 0)
    except (TypeError, ValueError):
        return 0

def _settle(m):
    return str(m.get('settle') or (m.get('info') or {}).get('settleCcy') or '').upper()

def _is_forbidden(m):
    return _settle(m) == 'USDT'      # VETO USDT (EEE/MiCA)

def resolve_symbol():
    """Regla de produccion: X-Perp = future activo con expTime MAS LEJANO.
    Fallback: swap USD activo. Nunca USDT."""
    exchange.load_markets()
    fut, swp = [], []
    for m in exchange.markets.values():
        if (m.get('base') or '').upper() != BASE_ASSET or not m.get('active'):
            continue
        if _is_forbidden(m):
            continue
        if m.get('future'):
            fut.append(m)
        elif m.get('swap'):
            swp.append(m)
    if fut:
        m = max(fut, key=_exp)
        print(f"Instrumento: {m['id']} (XPERP — vencimiento mas lejano)")
        return m
    if swp:
        m = swp[0]
        print(f"Instrumento: {m['id']} (fallback SWAP-USD)")
        return m
    sys.exit("ABORTADO: sin XLM/USD activo en my.okx.com (USDT vetado)")


def fetch_ohlcv_full(symbol, timeframe, target_ms, limit_per_call=300):
    """Paginacion hacia adelante con 'since'. Devuelve lo que OKX entregue;
    reporta la cobertura real. Aborta si el historico es insuficiente."""
    out, since, pages = [], None, 0
    now_ms = int(time.time() * 1000)
    start = now_ms - target_ms
    since = start
    while pages < MAX_PAGES:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit_per_call)
        if not batch:
            break
        fresh = [r for r in batch if not out or r[0] > out[-1][0]]
        if not fresh:
            break
        out.extend(fresh)
        since = out[-1][0] + 1
        pages += 1
        if out[-1][0] >= now_ms - 1:
            break
        time.sleep(0.15)
    df = pd.DataFrame(out, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
    return df


def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def rsi(s, length=14):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/length, adjust=False).mean()
    rs = gain / loss.replace(0, float('nan'))
    return 100 - (100 / (1 + rs))


def ffecha(ms):
    try:
        return time.strftime('%y-%m-%d %H:%M', time.gmtime(int(ms) / 1000))
    except (TypeError, ValueError):
        return '?'

def fmt_pct(x):
    return f"{x * 100:+.2f}%"


def simulate(c15, e7, e21, r14, idx4_of, e7h, e21h, use_rsi_filter):
    """Corre la simulacion completa. Devuelve (trades, contadores)."""
    n = len(c15)
    ts = c15['timestamp'].values
    op = c15['open'].values
    hi = c15['high'].values
    lo = c15['low'].values

    trades = []
    cnt = {'raw_crosses': 0, 'gate_blocked': 0, 'rsi_blocked': 0,
           'cooldown_blocked': 0, 'busy_blocked': 0}
    in_pos = False
    last_exit_loss = False
    cooldown_until = 0
    i = WARMUP

    while i < n - 1:
        if in_pos:
            i += 1
            continue
        # cadencia 30 min: chequear solo cada CHECK_EVERY velas
        if i % CHECK_EVERY != 0:
            i += 1
            continue
        open_next_ms = int(ts[i + 1])
        if open_next_ms < cooldown_until:
            cnt['cooldown_blocked'] += 1
            i += 1
            continue

        # gate 4H: ultima vela 4H CERRADA al momento del chequeo
        i4 = idx4_of[i]
        if i4 is None or i4 < 24:
            i += 1
            continue
        trend_4h = bool(e7h[i4] > e21h[i4])

        # ventana -2/-3 = {i, i-1}, orden de produccion: up@i, down@i, up@i-1, down@i-1
        signal = None
        for k in (i, i - 1):
            if k < WARMUP:
                continue
            up   = e7[k - 1] <= e21[k - 1] and e7[k] > e21[k]
            down = e7[k - 1] >= e21[k - 1] and e7[k] < e21[k]
            if up or down:
                cnt['raw_crosses'] += 1
            if up and trend_4h:
                signal = 'LONG'
            elif down and not trend_4h:
                signal = 'SHORT'
            elif up or down:
                cnt['gate_blocked'] += 1
            if signal:
                break
        if not signal:
            i += 1
            continue

        # Ax3.3: RSI de la vela cerrada mas nueva (i), igual que produccion
        if use_rsi_filter:
            r = r14[i]
            if not (RSI_LO < r < RSI_HI):
                cnt['rsi_blocked'] += 1
                i += 1
                continue

        # entrada ~ apertura de la vela siguiente (fill de mercado tras el cierre)
        entry_idx = i + 1
        entry = op[entry_idx]
        if signal == 'LONG':
            sl, tp = entry * (1 - SL_PCT), entry * (1 + TP_PCT)
        else:
            sl, tp = entry * (1 + SL_PCT), entry * (1 - TP_PCT)

        # caminar hasta SL o TP (conservador: SL primero en la misma vela)
        exit_idx, reason, exit_px = None, None, None
        j = entry_idx
        while j < n:
            if signal == 'LONG':
                if lo[j] <= sl:
                    exit_idx, reason, exit_px = j, 'SL', sl
                    break
                if hi[j] >= tp:
                    exit_idx, reason, exit_px = j, 'TP', tp
                    break
            else:
                if hi[j] >= sl:
                    exit_idx, reason, exit_px = j, 'SL', sl
                    break
                if lo[j] <= tp:
                    exit_idx, reason, exit_px = j, 'TP', tp
                    break
            j += 1

        if exit_idx is None:
            # sin salida en los datos: cerrar a ultimo precio, marcar OPEN
            exit_idx, reason, exit_px = n - 1, 'OPEN', c15['close'].values[n - 1]

        gross = (exit_px / entry - 1) if signal == 'LONG' else (1 - exit_px / entry)
        net = gross - FEE_PCT * 2
        trades.append({
            'dir': signal, 'entry_time': int(ts[entry_idx]), 'entry': float(entry),
            'exit_time': int(ts[exit_idx]), 'exit': float(exit_px),
            'reason': reason, 'gross': float(gross), 'net': float(net),
            'variant': 'RSI' if use_rsi_filter else 'NO-RSI',
        })

        if reason == 'SL':
            last_exit_loss = True
            cooldown_until = int(ts[exit_idx]) + COOLDOWN_MIN * 60000
        else:
            last_exit_loss = False

        if reason == 'OPEN':
            break                      # sin datos futuros: fin de simulacion
        in_pos = False
        i = exit_idx + 1

    # contadores de ocupacion aproximados
    return trades, cnt


def summarize(trades, label):
    closed = [t for t in trades if t['reason'] != 'OPEN']
    wins   = [t for t in closed if t['net'] > 0]
    losses = [t for t in closed if t['net'] <= 0]
    net_sum = sum(t['net'] for t in closed)
    avgW = sum(t['net'] for t in wins) / len(wins) if wins else 0.0
    avgL = abs(sum(t['net'] for t in losses) / len(losses)) if losses else 0.0
    wr = (len(wins) / len(closed) * 100) if closed else 0.0
    exp = (net_sum / len(closed)) if closed else 0.0
    be_wr = (avgL / (avgL + avgW) * 100) if (avgW > 0 and avgL > 0) else 0.0
    return {'label': label, 'n': len(closed), 'wins': len(wins),
            'losses': len(losses), 'wr': wr, 'exp': exp,
            'net': net_sum, 'avgW': avgW, 'avgL': avgL, 'be_wr': be_wr,
            'tp': sum(1 for t in closed if t['reason'] == 'TP'),
            'sl': sum(1 for t in closed if t['reason'] == 'SL'),
            'open': len(trades) - len(closed)}


try:
    print("=" * 52)
    print("  BACKTEST DEMO — XLM · Ax2-D + Ax3.3 RSI-BANDA")
    print("  SIMULACION — NO SE EJECUTAN ORDENES")
    print("  fuente: velas publicas OKX Europe · my.okx.com")
    print("  USDT: VETADO · fees taker incluidos (0.05%/lado)")
    print("=" * 52)

    sym = resolve_symbol()

    target_ms = BACKTEST_DAYS * 24 * 3600 * 1000
    print(f"Descargando {TIMEFRAME} (~{BACKTEST_DAYS} dias objetivo)...", flush=True)
    c15 = fetch_ohlcv_full(sym['id'], TIMEFRAME, target_ms)
    print(f"Descargando {TF_FILTER}...", flush=True)
    c240 = fetch_ohlcv_full(sym['id'], TF_FILTER, max(target_ms, 60 * 24 * 3600 * 1000))

    # descartar vela en formacion (la ultima, si aun no cerro)
    now_ms = int(time.time() * 1000)
    if len(c15) and int(c15['timestamp'].iloc[-1]) + 15 * 60000 > now_ms:
        c15 = c15.iloc[:-1].reset_index(drop=True)
    if len(c240) and int(c240['timestamp'].iloc[-1]) + 240 * 60000 > now_ms:
        c240 = c240.iloc[:-1].reset_index(drop=True)

    if len(c15) < 400:
        sys.exit(f"ABORTADO: historico 15m insuficiente ({len(c15)} velas)")
    if len(c240) < 30:
        sys.exit(f"ABORTADO: historico 4H insuficiente ({len(c240)} velas)")

    dias = (int(c15['timestamp'].iloc[-1]) - int(c15['timestamp'].iloc[0])) / 86400000
    print(f"15m: {len(c15)} velas · {dias:.1f} dias · "
          f"{ffecha(c15['timestamp'].iloc[0])} → {ffecha(c15['timestamp'].iloc[-1])}")
    print(f"4H : {len(c240)} velas")

    # indicadores 15m (causales, calculados una vez sobre todo el historico)
    e7, e21 = ema(c15['close'], 7).values, ema(c15['close'], 21).values
    r14 = rsi(c15['close'], RSI_LEN).values

    # indicadores 4H + mapa 15m -> ultima 4H cerrada
    e7h = ema(c240['close'], 7).values
    e21h = ema(c240['close'], 21).values
    close4_ms = c240['timestamp'].values + 240 * 60000
    import numpy as np
    idx4_of = np.searchsorted(close4_ms, c15['timestamp'].values, side='right') - 1
    idx4_of = [int(x) if x >= 0 else None for x in idx4_of]

    # ── DOS SIMULACIONES: con y sin el candado RSI ──
    print("\nSimulando CON filtro RSI [30-70] (produccion)...", flush=True)
    tr_rsi, cnt_rsi = simulate(c15, e7, e21, r14, idx4_of, e7h, e21h, True)
    print("Simulando SIN filtro RSI (contraste)...", flush=True)
    tr_no, cnt_no = simulate(c15, e7, e21, r14, idx4_of, e7h, e21h, False)

    # ── DETALLE de la variante canonica (produccion) ──
    print("\n" + "-" * 52)
    print("TRADES SIMULADOS (variante PRODUCCION, con RSI):")
    print("-" * 52)
    notional_ref = None
    for t in tr_rsi:
        if notional_ref is None:
            notional_ref = 100.0 * t['entry']    # 1 contrato = 100 XLM
        usd = notional_ref * t['net']
        print(f"  {ffecha(t['entry_time'])} {t['dir']:<5} "
              f"{t['entry']:.5f}→{t['exit']:.5f}  {t['reason']:<4} "
              f"net:{fmt_pct(t['net'])}  (~${usd:+.3f})")
    if not tr_rsi:
        print("  (ninguna entrada cumplio todas las condiciones)")

    s_rsi = summarize(tr_rsi, 'CON RSI (produccion)')
    s_no = summarize(tr_no, 'SIN RSI (contraste)')

    def line(s):
        be = f"{s['be_wr']:.0f}%" if s['be_wr'] else "-"
        return (f"  {s['label']:<20} trades:{s['n']:<4} "
                f"W:{s['wins']} L:{s['losses']}  WR:{s['wr']:.0f}%  "
                f"expect:{s['exp'] * 100:+.3f}%/trade  net:{fmt_pct(s['net'])}  "
                f"payoff:{(s['avgW'] / s['avgL']):.2f}" if s['avgL'] else
                f"  {s['label']:<20} trades:{s['n']:<4} WR:{s['wr']:.0f}%  net:{fmt_pct(s['net'])}")

    print("\n" + "=" * 52)
    print("COMPARATIVA (neto de fees):")
    print("=" * 52)
    for s in (s_rsi, s_no):
        if s['n']:
            be = f" · WR breakeven~{s['be_wr']:.0f}%"
            print(f"  {s['label']:<20} trades:{s['n']:<3} "
                  f"TP:{s['tp']} SL:{s['sl']} OPEN:{s['open']}  "
                  f"WR:{s['wr']:.0f}% ({s['wins']}W/{s['losses']}L)  "
                  f"expect:{s['exp'] * 100:+.3f}%/trade  net:{fmt_pct(s['net'])}{be}")
        else:
            print(f"  {s['label']:<20} sin trades cerrados")

    print(f"\nFILTROS (produccion): cruces crudos:{cnt_rsi['raw_crosses']} · "
          f"vetados por 4H:{cnt_rsi['gate_blocked']} · "
          f"vetados por RSI:{cnt_rsi['rsi_blocked']} · "
          f"cooldown:{cnt_rsi['cooldown_blocked']}")

    # veredicto automatico del contraste
    print("\nVEREDICTO DEL CANDADO RSI:")
    if s_rsi['n'] >= 5 and s_no['n'] >= 5:
        d = s_rsi['exp'] - s_no['exp']
        if d > 0.0005:
            print(f"  El candado RSI MEJORA la expectativa: "
                  f"{s_no['exp'] * 100:+.3f}% → {s_rsi['exp'] * 100:+.3f}%/trade")
        elif d < -0.0005:
            print(f"  El candado RSI EMPEORA la expectativa: "
                  f"{s_no['exp'] * 100:+.3f}% → {s_rsi['exp'] * 100:+.3f}%/trade")
        else:
            print("  El candado RSI es NEUTRO en este historico")
        print(f"  (muestra pequeña: {s_rsi['n']} vs {s_no['n']} trades — "
              f"no es estadistica definitiva)")
    else:
        print("  Muestra insuficiente (<5 trades por variante) — ampliar BACKTEST_DAYS")

    # ── persistencia: variante canonica a JSONL ──
    SALIDA.parent.mkdir(exist_ok=True)
    with open(SALIDA, 'w') as f:
        for t in tr_rsi:
            f.write(json.dumps(t) + '\n')
    print(f"\nTrades variante produccion guardados en {SALIDA}")

    print("\n" + "=" * 52)
    print("  ADVERTENCIA: backtest = pasado. Fees/slippage aproximados.")
    print("  Mismo-vela SL-primero = conservador. La simulacion no")
    print("  garantiza resultados futuros. El Carbono decide.")
    print("=" * 52)

except Exception as e:
    print(f"ERROR FATAL: {e}", flush=True)
    sys.exit(1)
