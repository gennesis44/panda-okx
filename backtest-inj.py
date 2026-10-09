# backtest-inj.py — INJ VALIDADOR · ley candidata EMA7/34 en 5m
#   DECRETO CARBONO (2026-10-09): investigar EMA7/34 · 5m para INJ
#   Si resulta ganador: primer destructor en salir con combustible minimo.
#
#   METODO GSCSI (idéntico al de los validadores vivos):
#     - velas CERRADAS (anti-repaint), sin vela en formacion
#     - fees 0.13% r/t descontados de cada trade
#     - XPERP vencimiento mas lejano · USDT PROHIBIDO
#     - candado: rejilla de separacion minima (los cruces minimos se
#       ignoran por decreto del Carbono — el grid encuentra el umbral)
#     - fail-closed: descarga incompleta o dato invalido = DETENIDO (exit 1)
#
#   SALIDA:
#     A) grid de candado (0.00/0.05/0.10/0.15/0.20%) con SL/TP del decreto
#     B) desglose LONGS/SHORTS del mejor grid
#     C) insumo para fijar el min% que separa senal de ruido
#     resultados -> data/inj-backtest.jsonl (trades del mejor grid)
#
# Ax3: sin secrets (velas publicas) · XPERP mas lejano · exit 1 ante dato
#      invalido · sin opinion. Correccion A565G: paginacion desde el pasado
#      y guardia dura de descarga (80% minimo o muere).

import json
import os
import sys
import time

import ccxt
import pandas as pd

try:
    import logging
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')
    log = logging.getLogger(__name__)
except Exception:
    class _L:
        def info(self, m): print("INFO  " + m, flush=True)
        def warning(self, m): print("WARN  " + m, flush=True)
        def error(self, m): print("ERROR " + m, flush=True)
    log = _L()

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

# ==================== DECRETO CARBONO ====================
BASE_ASSET = 'INJ'
TIMEFRAME = '5m'
EMA_FAST = 7
EMA_SLOW = 34
SL_LONG = 0.030     # 3.0%
TP_LONG = 0.060     # 6.0%
SL_SHORT = 0.030    # 3.0%
TP_SHORT = 0.060    # 6.0%
FEE_RT = 0.0013     # 0.13% r/t
DAYS = 68
GRID_LOCK = [0.0, 0.0005, 0.0010, 0.0015, 0.0020]
WARMUP = max(EMA_SLOW * 3, 100)
DATA_DIR = 'data'
OUT_JSONL = os.path.join(DATA_DIR, 'inj-backtest.jsonl')
HOST = 'https://www.okx.com'

exchange = ccxt.okx({
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'urls': {'api': {'rest': HOST}},
})

# ==================== INSTRUMENTO (XPERP — NUNCA USDT) ====================
def resolve_symbol():
    exchange.load_markets()
    candidatos = []
    for m in exchange.markets.values():
        if (m.get('base') != BASE_ASSET or not m.get('active')
                or not (m.get('future') or m.get('swap'))):
            continue
        settle = str(m.get('settle') or '').upper()
        if settle == 'USDT':
            continue
        info = m.get('info') or {}
        try:
            exp = int(info.get('expTime') or 0)
        except (TypeError, ValueError):
            exp = 0
        candidatos.append((exp, m['symbol']))
    if not candidatos:
        log.error("No hay " + BASE_ASSET + "/USD activo sin USDT. DETENIDO.")
        sys.exit(1)
    candidatos.sort(reverse=True)
    sym = candidatos[0][1]
    m = exchange.market(sym)
    log.info("Instrumento BACKTEST: " + str(sym) + " [XPERP mas lejano]")
    log.info("settle=" + str(m.get('settle')) +
             " | 1 ct = " + str(m.get('contractSize')) + " " + BASE_ASSET)
    return sym

# ==================== DATOS (A565G: desde el pasado, guardia dura) ====================
def fetch_closed(symbol, timeframe, target_candles):
    """Pagina ARRANCANDO DEL PASADO avanzando hacia el presente.
    Fail-closed: <80% del objetivo = DETENIDO (exit 1)."""
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    now_ms = int(time.time() * 1000)
    since = now_ms - target_candles * tf_ms
    out = []
    while len(out) < target_candles:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=300)
        if not batch:
            break
        out.extend(batch)
        since = batch[-1][0] + 1
        time.sleep(exchange.rateLimit / 1000.0)
    df = pd.DataFrame(out, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
    if len(df) == 0:
        log.error("Descarga vacia. DETENIDO.")
        sys.exit(1)
    if int(df['timestamp'].iloc[-1]) + tf_ms > now_ms:
        df = df.iloc[:-1].reset_index(drop=True)
    if len(df) < target_candles * 0.8:
        log.error("Descarga incompleta: " + str(len(df)) + "/" +
                  str(target_candles) + " velas. DETENIDO (fail-closed).")
        sys.exit(1)
    if len(df) < WARMUP + 50:
        log.error("Velas insuficientes (" + str(len(df)) + "). DETENIDO.")
        sys.exit(1)
    log.info(timeframe + ": " + str(len(df)) + " velas cerradas (" +
             time.strftime('%Y-%m-%d', time.gmtime(df['timestamp'].iloc[0] / 1000)) +
             " -> " + time.strftime('%Y-%m-%d %H:%M', time.gmtime(df['timestamp'].iloc[-1] / 1000)) +
             ") = " + format(len(df) * tf_ms / 86400000.0, '.1f') + " dias")
    return df

# ==================== MOTOR ====================
def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def simulate(df, lock_pct, sl_l, tp_l, sl_s, tp_s):
    """Cruce EMA7/34 en velas cerradas + candado por separacion.
    Una posicion por cruce; sale por SL o TP (sin flip)."""
    e_f = ema(df['close'], EMA_FAST).values
    e_s = ema(df['close'], EMA_SLOW).values
    c = df['close'].values
    ts = df['timestamp'].values
    n = len(df)
    trades = []
    in_pos = False
    side = None
    entry = sl = tp = 0.0
    entry_i = 0
    for i in range(WARMUP, n):
        if in_pos:
            if side == 'LONG':
                hit_sl = c[i] <= sl
                hit_tp = c[i] >= tp
            else:
                hit_sl = c[i] >= sl
                hit_tp = c[i] <= tp
            if hit_sl and hit_tp:
                # ambigua (gap de vela atraviesa ambos): conserva = conservador
                if side == 'LONG':
                    hit_tp = False
                else:
                    hit_tp = False
            if hit_sl:
                exit_price = sl
            elif hit_tp:
                exit_price = tp
            else:
                continue
            gross = (exit_price - entry) / entry
            if side == 'SHORT':
                gross = -gross
            net = gross - FEE_RT
            trades.append({'ts_in': int(ts[entry_i]), 'ts_out': int(ts[i]),
                           'side': side, 'entry': entry, 'exit': exit_price,
                           'net_pct': net, 'lock': lock_pct})
            in_pos = False
            continue
        prev_f = e_f[i - 1]
        prev_s = e_s[i - 1]
        curr_f = e_f[i]
        curr_s = e_s[i]
        sep = abs(curr_f - curr_s) / curr_s if curr_s else 0.0
        up = (prev_f <= prev_s) and (curr_f > curr_s)
        down = (prev_f >= prev_s) and (curr_f < curr_s)
        if (up or down) and sep >= lock_pct:
            side = 'LONG' if up else 'SHORT'
            entry = c[i]
            if side == 'LONG':
                sl = entry * (1 - sl_l)
                tp = entry * (1 + tp_l)
            else:
                sl = entry * (1 + sl_s)
                tp = entry * (1 - tp_s)
            entry_i = i
            in_pos = True
    return trades

def summarize(trades, label):
    t = len(trades)
    if t == 0:
        return {'label': label, 'trades': 0}
    wins = [x for x in trades if x['net_pct'] > 0]
    losses = [x for x in trades if x['net_pct'] <= 0]
    gw = sum(x['net_pct'] for x in wins)
    gl = abs(sum(x['net_pct'] for x in losses))
    ret = sum(x['net_pct'] for x in trades)
    wr = len(wins) / t
    pf = (gw / gl) if gl > 0 else float('inf')
    cum = 1.0
    peak = 1.0
    dd = 0.0
    for x in trades:
        cum *= (1 + x['net_pct'])
        peak = max(peak, cum)
        dd = min(dd, cum / peak - 1)
    return {'label': label, 'trades': t, 'wins': len(wins),
            'losses': len(losses), 'wr': wr, 'ret': ret,
            'pf': pf, 'dd': dd, 'avg': ret / t}

# ==================== MAIN ====================
def main():
    log.info("INJ VALIDADOR — ley candidata EMA" + str(EMA_FAST) + "/" +
             str(EMA_SLOW) + " " + TIMEFRAME + " | fees " +
             format(FEE_RT * 100, '.2f') + "% r/t | USDT prohibido")
    sym = resolve_symbol()
    target = int(DAYS * 24 * 12)
    df = fetch_closed(sym, TIMEFRAME, target)

    log.info("================ VALIDADOR (grid de candado) ================")
    results = []
    for lock in GRID_LOCK:
        trades = simulate(df, lock, SL_LONG, TP_LONG, SL_SHORT, TP_SHORT)
        s = summarize(trades, 'lock ' + format(lock * 100, '.2f') + '%')
        results.append((lock, trades, s))
        if s['trades'] == 0:
            log.info("[5m] " + s['label'] + " | sin trades")
            continue
        log.info("[5m] " + s['label'] +
                 " | ret " + format(s['ret'] * 100, '+.2f') + "%" +
                 " | trades " + str(s['trades']) +
                 " (" + format(s['trades'] / (DAYS / 7.0), '.1f') + "/sem)" +
                 " | WR " + format(s['wr'] * 100, '.1f') + "%" +
                 " | PF " + format(s['pf'], '.2f') +
                 " | DD " + format(s['dd'] * 100, '.1f') + "%" +
                 " | avg " + format(s['avg'] * 100, '+.3f') + "%")

    best = None
    for lock, trades, s in results:
        if s.get('trades', 0) >= 10 and s.get('pf', 0) > 1.0:
            if best is None or s['pf'] > best[2]['pf']:
                best = (lock, trades, s)

    if best:
        lock, trades, s = best
        log.info("=========== MEJOR GRID: candado " + format(lock * 100, '.2f') + "% ===========")
        longs = [x for x in trades if x['side'] == 'LONG']
        shorts = [x for x in trades if x['side'] == 'SHORT']
        sL = summarize(longs, 'longs')
        sS = summarize(shorts, 'shorts')
        if sL['trades']:
            log.info("  LONGS : " + str(sL['trades']) + " trades | WR " +
                     format(sL['wr'] * 100, '.0f') + "% | ret " +
                     format(sL['ret'] * 100, '+.2f') + "%")
        if sS['trades']:
            log.info("  SHORTS: " + str(sS['trades']) + " trades | WR " +
                     format(sS['wr'] * 100, '.0f') + "% | ret " +
                     format(sS['ret'] * 100, '+.2f') + "%")
        _persist(trades)
    else:
        log.info("=========== SIN CONFIG GANADORA (>=10 trades y PF>1) ===========")
        log.info("Veredicto: la ley candidata NO supera el filtro. El Carbono decide.")
        _persist([])

    log.info("================================================================")
    log.info("ADVERTENCIA: muestra corta por diseño. Pasado no es futuro.")
    log.info("El Carbono decide.")

def _persist(trades):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(OUT_JSONL, 'w') as fh:
            for x in trades:
                fh.write(json.dumps(x) + "\n")
        log.info("Trades guardados -> " + OUT_JSONL +
                 " (" + str(len(trades)) + " lineas)")
    except Exception as e:
        log.warning("No se pudo persistir jsonl: " + str(e))

if __name__ == "__main__":
    main()
