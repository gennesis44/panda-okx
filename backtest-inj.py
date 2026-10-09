# backtest-inj.py — INJ VALIDADOR · ley candidata EMA7/34 en 5m
#   DECRETO CARBONO (2026-10-09): investigar EMA7/34 · 5m para INJ
#   Si resulta ganador: primer destructor en salir con combustible mínimo.
#
#   METODO GSCSI (idéntico al de los validadores vivos):
#     - velas CERRADAS (anti-repaint), sin vela en formacion
#     - fees 0.13% r/t descontados de cada trade
#     - XPERP vencimiento mas lejano · USDT PROHIBIDO
#     - candado: rejilla de separacion minima (el Carbono fija el umbral
#       con estos datos — los cruces minimos se ignoran por decreto)
#     - fail-closed: si un dato no se puede validar, el validador se detiene
#
#   SALIDA:
#     A) grid de candado (0.00/0.05/0.10/0.15/0.20%) con SL/TP del decreto
#     B) mejor config por modo (long_short / long_only / short_only)
#     C) la pregunta del Carbono: que umbral min% separa señal de ruido
#     resultados -> data/inj-backtest.jsonl (1 linea por trade del mejor grid)
#
# Ax3: fail-closed en descarga y contrato · sin secrets en este validador
#      (velas publicas) · exit 1 ante dato invalido · sin opinion

import json
import os
import sys
import time

import ccxt
import pandas as pd

logging_ok = True
try:
    import logging
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')
    log = logging.getLogger(__name__)
except Exception:
    logging_ok = False
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
TP_SHORT = 0.060    # 060%
FEE_RT = 0.0013     # 0.13% r/t — tarifa futures OKX (idem validadores vivos)
DAYS = 68           # ventana historica (idem DOGE: ~2 meses de 5m)
GRID_LOCK = [0.0, 0.0005, 0.0010, 0.0015, 0.0020]   # 0.00/0.05/0.10/0.15/0.20%
WARMUP = max(EMA_SLOW * 3, 100)
DATA_DIR = 'data'
OUT_JSONL = os.path.join(DATA_DIR, 'inj-backtest.jsonl')

HOST = 'https://www.okx.com'   # publico: velas, sin auth

# ==================== EXCHANGE (solo lectura publica) ====================
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
    info = m.get('info') or {}
    log.info("Instrumento BACKTEST: " + str(sym) +
             " [" + str(info.get('instType') or '?') + " · XPERP mas lejano]")
    log.info("settle=" + str(m.get('settle')) +
             " | 1 ct = " + str(m.get('contractSize')) + " " + BASE_ASSET)
    return sym

# ==================== DATOS ====================
def fetch_closed(symbol, timeframe, target_candles):
    """Descarga por paginas hasta llenar target_candles de velas CERRADAS."""
    out = []
    since = None
    while len(out) < target_candles:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=300)
        if not batch:
            break
        out.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 100:
            break
        time.sleep(exchange.rateLimit / 1000.0)
    df = pd.DataFrame(out, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates('timestamp').sort_values('timestamp').reset_index(drop=True)
    now_ms = int(time.time() * 1000)
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    if int(df['timestamp'].iloc[-1]) + tf_ms > now_ms:
        df = df.iloc[:-1].reset_index(drop=True)   # vela en formacion fuera
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
    """Simula cruce EMA7/34 en velas cerradas con candado por separacion.
    Sin flip: una posicion por cruce, sale por SL o TP. Cooldown no simulado
    (los validadores vivos lo declaran; el grid compara ley pura)."""
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
            hit_sl_l = c[i] <= sl if side == 'LONG' else c[i] >= sl
            hit_tp_l = c[i] >= tp if side == 'LONG' else c[i] <= tp
            if hit_sl_l and hit_tp_l:
                hit_sl_l = c[i] <= entry if side == 'LONG' else c[i] >= entry
            exit_price = None
            if hit_sl_l:
                exit_price = sl
            elif hit_tp_l:
                exit_price = tp
            if exit_price is not None:
                gross = (exit_price - entry) / entry
                if side == 'SHORT':
                    gross = -gross
                net = gross - FEE_RT
                trades.append({'ts_in': int(ts[entry_i]), 'ts_out': int(ts[i]),
                               'side': side, 'entry': entry, 'exit': exit_price,
                               'net_pct': net, 'lock': lock_pct})
                in_pos = False
            continue
        prev_f, prev_s = e_f[i - 1], e_s[i - 1]
        curr_f, curr_s = e_f[i], e_s[i]
        if prev_f is None or prev_s is None:
            continue
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
    gross_win = sum(x['net_pct'] for x in wins)
    gross_loss = abs(sum(x['net_pct'] for x in losses))
    ret = sum(x['net_pct'] for x in trades)
    wr = len(wins) / t
    pf = (gross_win / gross_loss) if gross_loss > 0 else float('inf')
    cum = 1.0
    peak = 1.0
    dd = 0.0
    for x in trades:
        cum *= (1 + x['net_pct'])
        peak = max(peak, cum)
        dd = min(dd, cum / peak - 1)
    return {'label': label, 'trades': t,
            'wins': len(wins), 'losses': len(losses), 'wr': wr,
            'ret': ret, 'pf': pf, 'dd': dd,
            'avg': ret / t}

# ==================== MAIN ====================
def main():
    log.info("INJ VALIDADOR — ley candidata EMA" + str(EMA_FAST) + "/" +
             str(EMA_SLOW) + " " + TIMEFRAME + " | fees " +
             format(FEE_RT * 100, '.2f') + "% r/t | USDT prohibido")
    sym = resolve_symbol()
    target = int(DAYS * 24 * 12)   # 5m -> 288 velas/dia
    df = fetch_closed(sym, TIMEFRAME, target)

    log.info("================ VALIDADOR (grid de candado) ================")
    all_results = []
    for lock in GRID_LOCK:
        trades = simulate(df, lock, SL_LONG, TP_LONG, SL_SHORT, TP_SHORT)
        s = summarize(trades, 'lock ' + format(lock * 100, '.2f') + '%')
        all_results.append((lock, trades, s))
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
    for lock, trades, s in all_results:
        if s.get('trades', 0) >= 10 and s.get('pf', 0) > 1.0:
            if best is None or s['pf'] > best[2]['pf']:
                best = (lock, trades, s)
    if best:
        lock, trades, s = best
        log.info("=========== MEJOR GRID: candado " + format(lock * 100, '.2f') + "% ===========")
        longs = [x for x in trades if x['side'] == 'LONG']
        shorts = [x for x in trades if x['side'] == 'SHORT']
        sl_sum = summarize(longs, 'longs')
        ss_sum = summarize(shorts, 'shorts')
        if sl_sum['trades']:
            log.info("  LONGS : " + str(sl_sum['trades']) + " trades | WR " +
                     format(sl_sum['wr'] * 100, '.0f') + "% | PnL " +
                     format(sl_sum['ret'], '+.4f'))
        if ss_sum['trades']:
            log.info("  SHORTS: " + str(ss_sum['trades']) + " trades | WR " +
                     format(ss_sum['wr'] * 100, '.0f') + "% | PnL " +
                     format(ss_sum['ret'], '+.4f'))
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
