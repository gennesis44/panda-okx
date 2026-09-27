# backtest-hbar.py — Ax-HBAR · DESTRUCTOR v2 · COMPARADOR DE STACKS (rev.2)
#   MISION: decidir con datos si se quita una EMA al stack de HBAR.
#   La queja del Carbono: el stack 3/4/10/21/27 frena la entrada (candado excesivo).
#   STACKS comparados:
#     A (linea base) : 3/4/10/21/27 · 30m  — confirmar cuantas senales da hoy
#     B (candidato)  : 3/10/21/27  · 30m  — sin EMA4 (redundante con EMA3)
#     C (liviano)    : 3/10/21     · 30m  — cuanto rango se cuela con menos candado
#     D (hermano SUI): 3/15        · 1H   — el estandar ya validado por la flota
#   METRICA CLAVE: senales por semana (frecuencia) + WR/PF/DD/retorno de siempre.
#   SENAL DE STACK: alineacion perfecta de todas las EMAs del set
#     LONG  = e1>e2>...>en recien alineado (transicion)
#     SHORT = e1<e2<...<en recien alineado (transicion)
#   Motor sin lookahead: senal al cierre -> entrada en apertura siguiente;
#     SL/TP intravela (peor caso si ambos); cruce contrario cierra en close.
#   Instrumento: cadena de fallback USDT-VETADO (SWAP USDC > SWAP USD > XPERP > SPOT).
#   KEYS: variables de entorno — mismo fragmento que los bots. Endpoints publicos.
#   Salida: backtest_results_hbar.csv
import os
import sys
import time
import logging
from datetime import datetime, timezone

import ccxt
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

# ==================== CONFIGURACION (Carbono) ====================
BASE_ASSET = 'HBAR'
HOST = 'https://my.okx.com'
START_DATE = '2025-09-01'
FEE_TAKER = 0.0005
GUARDIA_USD = 15.0
MIN_TRADES_BEST = 5

STACKS_30M = [
    ('A', [3, 4, 10, 21, 27]),   # linea base actual (Destructor v2)
    ('B', [3, 10, 21, 27]),      # candidato: sin EMA4
    ('C', [3, 10, 21]),          # liviano: menos candado
]
STACKS_1H = [
    ('D', [3, 15]),              # hermano SUI (referencia)
]
SL_GRID = [0.010, 0.015, 0.020, 0.030]
TP_GRID = [0.015, 0.020, 0.030, 0.045, 0.055]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_CSV = os.path.join(BASE_DIR, 'backtest_results_hbar.csv')

TF_MS = {'30m': 1800000, '1H': 3600000}

# ==================== CLIENTE (MISMO FRAGMENTO DE KEYS) ====================
exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== VETO USDT (reforzado) ====================
def _fields(m):
    info = m.get('info') or {}
    iid = str(info.get('instId') or m.get('id') or '').upper()
    settle = str(info.get('settleCcy') or m.get('settle') or '').upper()
    ctccy = str(info.get('ctValCcy') or '').upper()
    quote = str(m.get('quote') or '').upper()
    return iid, settle, ctccy, quote

def _is_forbidden(m):
    iid, settle, ctccy, quote = _fields(m)
    return 'USDT' in iid or 'USDT' in settle or 'USDT' in ctccy or 'USDT' in quote

def dump_catalog():
    exchange.load_markets()
    n = 0
    log.info("---- CATALOGO %s en OKX (diagnostico) ----", BASE_ASSET)
    for m in exchange.markets.values():
        info = m.get('info') or {}
        iid = str(info.get('instId') or m.get('id') or '').upper()
        if not iid.startswith(BASE_ASSET + '-'):
            continue
        log.info("  instId=%s | tipo=%s | estado=%s | settle=%s | ctVal=%s %s | activo=%s",
                 iid, info.get('instType'), info.get('state'),
                 info.get('settleCcy'), info.get('ctVal'), info.get('ctValCcy'),
                 m.get('active'))
        n += 1
    log.info("---- %d instrumentos %s listados ----", n, BASE_ASSET)

# ==================== SELECTORES (cadena de fallback) ====================
def pick_swap_backtest():
    exchange.load_markets()
    best = None
    rank_map = {'USDC': 0, 'USD': 1}
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET:
            continue
        info = m.get('info') or {}
        if str(info.get('instType') or '').upper() != 'SWAP':
            continue
        if _is_forbidden(m) or not m.get('active', True):
            continue
        settle = str(info.get('settleCcy') or m.get('settle') or '').upper()
        rank = rank_map.get(settle, 2)
        if best is None or rank < best[0]:
            best = (rank, m['symbol'])
    return best[1] if best else None

def pick_future():
    exchange.load_markets()
    best = None
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET:
            continue
        info = m.get('info') or {}
        if str(info.get('instType') or '').upper() != 'FUTURES':
            continue
        if _is_forbidden(m) or not m.get('active', True):
            continue
        try:
            exp = int(info.get('expTime') or 0)
        except (TypeError, ValueError):
            exp = 0
        if exp <= 0:
            continue
        if best is None or exp > best[0]:
            best = (exp, m['symbol'])
    return best[1] if best else None

def pick_spot_fallback():
    exchange.load_markets()
    preferidos = []
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET or not m.get('spot'):
            continue
        if _is_forbidden(m) or not m.get('active', True):
            continue
        quote = str(m.get('quote') or '').upper()
        if quote == 'USDC':
            preferidos.append((0, m['symbol']))
        elif quote == 'USD':
            preferidos.append((1, m['symbol']))
    if not preferidos:
        return None
    preferidos.sort()
    return preferidos[0][1]

# ==================== VALOR DE 1 CONTRATO ====================
def contract_meta(symbol):
    market = exchange.market(symbol)
    info = market.get('info') or {}
    inst_type = str(info.get('instType') or ('SPOT' if market.get('spot') else '?')).upper()
    if inst_type == 'SPOT':
        ctval, ccy = 1.0, str(market.get('base') or BASE_ASSET).upper()
    else:
        ctval = _f(market.get('contractSize') or info.get('ctVal') or 1)
        ccy = str(info.get('ctValCcy') or BASE_ASSET).upper()
    settle = str(info.get('settleCcy') or market.get('settle') or '-').upper()
    min_sz = _f(info.get('minSz') or 1)
    lot_sz = _f(info.get('lotSz') or 1)
    tick = str(info.get('tickSz') or '?')
    exp = int(_f(info.get('expTime')))
    price = _f((exchange.fetch_ticker(symbol) or {}).get('last') or 0)
    usd_1ct = ctval if ccy in ('USD', 'USDC', 'USDT') else ctval * price
    return {'symbol': symbol, 'inst_id': str(info.get('instId') or market.get('id') or ''),
            'inst_type': inst_type, 'ctval': ctval, 'ccy': ccy, 'settle': settle,
            'min_sz': min_sz, 'lot_sz': lot_sz, 'tick': tick,
            'exp': exp, 'price': price, 'usd_1ct': usd_1ct}

def print_contract_report(meta, etiqueta):
    log.info("---- CONTRATO [%s] ----", etiqueta)
    log.info("instId=%s | ccxt=%s | tipo=%s | settle=%s",
             meta['inst_id'], meta['symbol'], meta['inst_type'], meta['settle'])
    if meta['exp'] > 0:
        vto = datetime.fromtimestamp(meta['exp'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
        log.info("Vencimiento: %s (XPERP mas lejano)", vto)
    log.info("1 contrato = %s %s | precio ~%s | nocional 1 ct ~$%.4f",
             meta['ctval'], meta['ccy'], meta['price'], meta['usd_1ct'])
    log.info("Minimo por orden: %s contrato(s) ~$%.4f | lot %s | tick %s",
             meta['min_sz'], meta['min_sz'] * meta['usd_1ct'], meta['lot_sz'], meta['tick'])
    if meta['usd_1ct'] > 0:
        n_max = int(GUARDIA_USD // meta['usd_1ct'])
        log.info("Con guardia $%.2f: max %d contrato(s) por orden%s",
                 GUARDIA_USD, n_max,
                 "  << ATENCION: 0 ct = guardia no alcanza ni para 1" if n_max == 0 else "")

# ==================== HISTORICO (velas cerradas SIEMPRE) ====================
def _candles_raw(inst_id, bar, limit=300, after=None, history=False):
    req = {'instId': inst_id, 'bar': bar, 'limit': str(limit)}
    if after:
        req['after'] = str(after)
    method = exchange.publicGetMarketHistoryCandles if history else exchange.publicGetMarketCandles
    resp = method(req)
    return resp.get('data') or []

def fetch_history(symbol, bar, start_date):
    start_ms = int(datetime.strptime(start_date, '%Y-%m-%d')
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    inst_id = exchange.market(symbol)['id']
    rows = _candles_raw(inst_id, bar, 300)
    if not rows:
        log.warning("Sin velas iniciales %s %s", inst_id, bar)
        return pd.DataFrame()
    all_rows = list(rows)
    oldest = int(rows[-1][0])
    while oldest > start_ms:
        hist = _candles_raw(inst_id, bar, 100, after=oldest, history=True)
        if not hist:
            break
        all_rows.extend(hist)
        new_oldest = int(hist[-1][0])
        if new_oldest >= oldest:
            break
        oldest = new_oldest
        time.sleep(0.15)
    cols = ['ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'volCcyQuote', 'confirm']
    df = pd.DataFrame([r[:9] for r in all_rows], columns=cols)
    df['ts'] = df['ts'].astype('int64')
    df = df.drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
    df = df[df['ts'] >= start_ms]
    df = df[df['confirm'].astype(str) == '1']
    for c in ('open', 'high', 'low', 'close'):
        df[c] = df[c].astype(float)
    return df.reset_index(drop=True)

# ==================== SENAL: ALINEACION DE STACK ====================
def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def add_stack_signals(df, emas):
    df = df.copy()
    cols = []
    for n in emas:
        col = 'ema' + str(n)
        df[col] = ema(df['close'], n)
        cols.append(col)
    up = pd.Series(True, index=df.index)
    dn = pd.Series(True, index=df.index)
    for i in range(len(cols) - 1):
        up = up & (df[cols[i]] > df[cols[i + 1]])
        dn = dn & (df[cols[i]] < df[cols[i + 1]])
    up_prev = up.shift(1).fillna(False)
    dn_prev = dn.shift(1).fillna(False)
    sig_up = up & ~up_prev
    sig_dn = dn & ~dn_prev
    df['signal'] = np.select([sig_up, sig_dn], [1, -1], default=0)
    df.loc[df.index[:max(emas)], 'signal'] = 0
    return df

def count_signals(df):
    return int((df['signal'] != 0).sum())

# ==================== MOTOR DE BACKTEST ====================
def run_backtest(df, sl_pct, tp_pct, fee, pos_pct=1.0):
    fee = float(fee)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    n_trades = 0
    wins = 0.0
    losses = 0.0
    pos = None
    pending = 0

    o = df['open'].values
    h = df['high'].values
    l = df['low'].values
    c = df['close'].values
    sig = df['signal'].values
    n = len(df)

    for i in range(1, n):
        if pending != 0 and pos is None:
            side = pending
            entry = o[i]
            sl = entry * (1 - sl_pct) if side == 1 else entry * (1 + sl_pct)
            tp = entry * (1 + tp_pct) if side == 1 else entry * (1 - tp_pct)
            pos = {'side': side, 'entry': entry, 'sl': sl, 'tp': tp}
            pending = 0

        if pos is not None:
            exit_px = None
            if pos['side'] == 1:
                if l[i] <= pos['sl']:
                    exit_px = pos['sl']
                elif h[i] >= pos['tp']:
                    exit_px = pos['tp']
                elif sig[i] == -1:
                    exit_px = c[i]
            else:
                if h[i] >= pos['sl']:
                    exit_px = pos['sl']
                elif l[i] <= pos['tp']:
                    exit_px = pos['tp']
                elif sig[i] == 1:
                    exit_px = c[i]

            if exit_px is not None:
                gross = pos['side'] * (exit_px / pos['entry'] - 1.0)
                net = gross - 2 * fee
                equity *= (1.0 + pos_pct * net)
                peak = max(peak, equity)
                max_dd = max(max_dd, 1 - equity / peak)
                n_trades += 1
                if net > 0:
                    wins += net
                else:
                    losses += abs(net)
                if sig[i] != 0 and ((pos['side'] == 1 and sig[i] == -1)
                                    or (pos['side'] == -1 and sig[i] == 1)):
                    pending = sig[i]
                pos = None

        if pos is None and pending == 0 and sig[i] != 0:
            pending = sig[i]

    pnls_wr = (wins / n_trades) if n_trades else 0.0
    pf = (wins / losses) if losses > 0 else (np.inf if wins > 0 else 0.0)
    return {'total_return': equity - 1.0, 'n_trades': n_trades,
            'win_rate': pnls_wr, 'profit_factor': pf, 'max_drawdown': max_dd}

# ==================== MAIN ====================
def main():
    log.info("HBAR DESTRUCTOR — COMPARADOR DE STACKS rev.2 | host=%s | USDT prohibido", HOST)
    dump_catalog()

    # 1) instrumento BACKTEST -----------------------------------
    symbol = pick_swap_backtest()
    origen = 'SWAP perpetuo USD/USDC'
    if not symbol:
        symbol = pick_future()
        origen = 'XPERP vencimiento mas lejano'
    if not symbol:
        symbol = pick_spot_fallback()
        origen = 'SPOT USD/USDC (proxy de precio)'
    if not symbol:
        log.error("No hay %s en USD/USDC (swap/futuro/spot) activo. USDT prohibido. Abortando.", BASE_ASSET)
        sys.exit(1)
    log.info("Instrumento BACKTEST: %s [%s]", symbol, origen)
    print_contract_report(contract_meta(symbol), 'BACKTEST')

    # 2) historicos ----------------------------------------------
    hist = {}
    for bar in ('30m', '1H'):
        log.info("Descargando historico %s %s ...", symbol, bar)
        df = fetch_history(symbol, bar, START_DATE)
        if len(df) < 250:
            log.warning("Historico insuficiente %s (%d velas cerradas) — stacks en %s se omiten",
                        bar, len(df), bar)
            hist[bar] = None
            continue
        d0 = datetime.fromtimestamp(df['ts'].iloc[0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
        d1 = datetime.fromtimestamp(df['ts'].iloc[-1] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        days = (df['ts'].iloc[-1] - df['ts'].iloc[0]) / 86400000.0
        log.info("%s: %d velas cerradas (%s -> %s) = %.1f dias", bar, len(df), d0, d1, days)
        hist[bar] = {'df': df, 'days': max(days, 1.0)}

    # 3) comparador de stacks ------------------------------------
    log.info("================ COMPARADOR DE STACKS ================")
    log.info("Senales/semana = frecuencia de entrada del candado (la metrica de la queja)")
    results = []
    for bar, stacks in (('30m', STACKS_30M), ('1H', STACKS_1H)):
        h = hist.get(bar)
        if not h:
            continue
        df, days = h['df'], h['days']
        for nombre, emas in stacks:
            dfs = add_stack_signals(df, emas)
            n_sig = count_signals(dfs)
            sig_sem = n_sig * 7.0 / days
            log.info("Stack %s %s · %s | senales: %d (%.1f/semana)",
                     nombre, emas, bar, n_sig, sig_sem)
            for sl in SL_GRID:
                for tp in TP_GRID:
                    m = run_backtest(dfs, sl, tp, FEE_TAKER)
                    m.update({'stack': nombre, 'emas': '/'.join(str(x) for x in emas),
                              'timeframe': bar, 'sl_pct': sl, 'tp_pct': tp,
                              'signals': n_sig, 'signals_per_week': round(sig_sem, 2),
                              'days': round(days, 1)})
                    results.append(m)

    if not results:
        log.error("Sin resultados (historicos insuficientes).")
        sys.exit(1)

    res = pd.DataFrame(results)
    res.to_csv(RESULTS_CSV, index=False)
    log.info("Resultados completos -> %s", RESULTS_CSV)

    # 4) sintesis por stack (mejor config con >= MIN_TRADES_BEST) --
    log.info("================ SINTESIS (mejor config por stack) ================")
    for (nombre, emas), bar in [(s, '30m') for s in STACKS_30M] + [(s, '1H') for s in STACKS_1H]:
        sub = res[(res['stack'] == nombre) & (res['timeframe'] == bar)]
        if sub.empty:
            continue
        viable = sub[sub['n_trades'] >= MIN_TRADES_BEST]
        elegir = viable if len(viable) else sub
        best = elegir.sort_values('total_return', ascending=False).iloc[0]
        aviso = '' if len(viable) else ('  << OJO: <' + str(MIN_TRADES_BEST) + ' trades (muestra debil)')
        log.info("Stack %s [%s] %s | SL %.1f%% TP %.1f%% | ret %+.2f%% | trades %d | "
                 "sen/sem %.1f | WR %.1f%% | PF %.2f | DD %.1f%%%s",
                 nombre, best['emas'], bar, best['sl_pct'] * 100, best['tp_pct'] * 100,
                 best['total_return'] * 100, int(best['n_trades']),
                 best['signals_per_week'], best['win_rate'] * 100,
                 min(best['profit_factor'], 99.99), best['max_drawdown'] * 100, aviso)

    log.info("El Carbono decide: mantener A, cortar a B, abrir a C o migrar a D.")
    log.info("Activacion de cambios en main: SOLO con este veredicto ratificado.")

if __name__ == "__main__":
    main()
