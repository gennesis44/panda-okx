# backtest-stx.py — Ax2-STX · EL CATAMARAN · BACKTEST pre-activacion (rev.3)
#   REV.3: seleccion de instrumento robusta + veto USDT reforzado:
#     - Veto USDT revisa instId + settleCcy + ctValCcy + quote (cubre X-Perp)
#     - SWAP: acepta cualquier perpétuo STX USD/USDC NO-USDT (prioridad settle USDC)
#     - FUTURES: XPERP vencimiento mas lejano (igual que SUI live)
#     - SPOT: ultimo recurso (proxy de precio)
#     - CATALOGO completo en el log SIEMPRE (diagnostico a la vista)
#   USDT PROHIBIDO en todo el arbol (MiCA/EEE). KEYS: variables de entorno.
#   Salidas: backtest_results_stx.csv | trades_mejor_set_stx.csv
import os
import sys
import time
import logging
from datetime import datetime, timezone

import ccxt
import yaml
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

# ==================== RUTAS / CONSTANTES ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.stx.yml')
RESULTS_CSV = os.path.join(BASE_DIR, 'backtest_results_stx.csv')
TRADES_CSV = os.path.join(BASE_DIR, 'trades_mejor_set_stx.csv')

HOST = 'https://my.okx.com'
BASE_ASSET = 'STX'

TF_MS = {'1m': 60000, '3m': 180000, '5m': 300000, '15m': 900000,
         '30m': 1800000, '1H': 3600000, '2H': 7200000, '4H': 14400000,
         '6H': 21600000, '12H': 43200000, '1D': 86400000, '1W': 604800000}
TF_ALIASES = {'1h': '1H', '4h': '4H', '1d': '1D', '2h': '2H',
              '6h': '6H', '12h': '12H', '1w': '1W'}

# ==================== CLIENTE (MISMO FRAGMENTO DE KEYS QUE SUI) ====================
exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== CAMPOS / VETO USDT (reforzado) ====================
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

# ==================== CATALOGO (diagnostico SIEMPRE) ====================
def dump_stx_catalog():
    exchange.load_markets()
    n = 0
    log.info("---- CATALOGO STX en OKX (diagnostico rev.3) ----")
    for m in exchange.markets.values():
        info = m.get('info') or {}
        iid = str(info.get('instId') or m.get('id') or '').upper()
        if not iid.startswith(BASE_ASSET + '-'):
            continue
        log.info("  instId=%s | tipo=%s | estado=%s | settle=%s | ctVal=%s %s | "
                 "ccxt=%s | quote=%s | activo=%s",
                 iid, info.get('instType'), info.get('state'),
                 info.get('settleCcy'), info.get('ctVal'), info.get('ctValCcy'),
                 m.get('symbol'), m.get('quote'), m.get('active'))
        n += 1
    log.info("---- %d instrumentos STX listados ----", n)

# ==================== SELECTORES (cadena de fallback) ====================
def pick_swap_backtest():
    """Cualquier SWAP STX no-USDT. Prioridad: settle USDC > USD > otro (coin-margined)."""
    exchange.load_markets()
    best = None                       # (rank, symbol)
    rank_map = {'USDC': 0, 'USD': 1}
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET:
            continue
        info = m.get('info') or {}
        if str(info.get('instType') or '').upper() != 'SWAP':
            continue
        if _is_forbidden(m):
            continue
        if not m.get('active', True):
            continue
        settle = str(info.get('settleCcy') or m.get('settle') or '').upper()
        rank = rank_map.get(settle, 2)
        if best is None or rank < best[0]:
            best = (rank, m['symbol'])
    return best[1] if best else None

def pick_future():
    exchange.load_markets()
    best = None                       # (expTime, symbol)
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET:
            continue
        info = m.get('info') or {}
        if str(info.get('instType') or '').upper() != 'FUTURES':
            continue
        if _is_forbidden(m):
            continue
        if not m.get('active', True):
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
    lev = str(info.get('maxLever') or 'spot')
    exp = int(_f(info.get('expTime')))
    price = _f((exchange.fetch_ticker(symbol) or {}).get('last') or 0)
    usd_1ct = ctval if ccy in ('USD', 'USDC', 'USDT') else ctval * price
    return {'symbol': symbol, 'inst_id': str(info.get('instId') or market.get('id') or ''),
            'inst_type': inst_type, 'ctval': ctval, 'ccy': ccy, 'settle': settle,
            'min_sz': min_sz, 'lot_sz': lot_sz, 'tick': tick, 'lev': lev,
            'exp': exp, 'price': price, 'usd_1ct': usd_1ct}

def print_contract_report(meta, etiqueta, max_notional):
    log.info("---- CONTRATO [%s] ----", etiqueta)
    log.info("instId=%s | ccxt=%s | tipo=%s | settle=%s",
             meta['inst_id'], meta['symbol'], meta['inst_type'], meta['settle'])
    if meta['exp'] > 0:
        vto = datetime.fromtimestamp(meta['exp'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
        log.info("Vencimiento: %s (XPERP mas lejano)", vto)
    log.info("1 contrato = %s %s | precio ~%s | nocional 1 ct ~$%.4f",
             meta['ctval'], meta['ccy'], meta['price'], meta['usd_1ct'])
    log.info("Minimo por orden: %s contrato(s) ~$%.4f | lot %s | tick %s | lev max %s",
             meta['min_sz'], meta['min_sz'] * meta['usd_1ct'],
             meta['lot_sz'], meta['tick'], meta['lev'])
    if meta['usd_1ct'] > 0:
        log.info("Con guardia $%.2f (misma que SUI): max %d contrato(s) por orden",
                 max_notional, int(max_notional // meta['usd_1ct']))

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
    df = df[df['confirm'].astype(str) == '1']     # descarta vela en formacion
    for c in ('open', 'high', 'low', 'close'):
        df[c] = df[c].astype(float)
    return df.reset_index(drop=True)

# ==================== SENAL: CRUCE DE EMAs ====================
def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def add_signals(df, fast, slow):
    df = df.copy()
    df['ema_f'] = ema(df['close'], fast)
    df['ema_s'] = ema(df['close'], slow)
    diff = df['ema_f'] - df['ema_s']
    up = (diff > 0) & (diff.shift(1) <= 0)
    dn = (diff < 0) & (diff.shift(1) >= 0)
    df['signal'] = np.select([up, dn], [1, -1], default=0)
    df.loc[df.index[:slow], 'signal'] = 0         # warmup: EMAs aun no estables
    return df

# ==================== MOTOR DE BACKTEST ====================
def run_backtest(df, sl_pct, tp_pct, fee, mode='long_short', pos_pct=1.0):
    fee = float(fee)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    trades = []
    pos = None
    pending = 0

    o = df['open'].values
    h = df['high'].values
    l = df['low'].values
    c = df['close'].values
    sig = df['signal'].values
    ts = df['ts'].values
    n = len(df)

    for i in range(1, n):
        if pending != 0 and pos is None:
            side = pending
            entry = o[i]
            sl = entry * (1 - sl_pct) if side == 1 else entry * (1 + sl_pct)
            tp = entry * (1 + tp_pct) if side == 1 else entry * (1 - tp_pct)
            pos = {'side': side, 'entry': entry, 'sl': sl, 'tp': tp, 't_in': ts[i]}
            pending = 0

        if pos is not None:
            exit_px = None
            reason = ''
            if pos['side'] == 1:
                if l[i] <= pos['sl']:
                    exit_px, reason = pos['sl'], 'SL'
                elif h[i] >= pos['tp']:
                    exit_px, reason = pos['tp'], 'TP'
                elif sig[i] == -1:
                    exit_px, reason = c[i], 'CRUCE'
            else:
                if h[i] >= pos['sl']:
                    exit_px, reason = pos['sl'], 'SL'
                elif l[i] <= pos['tp']:
                    exit_px, reason = pos['tp'], 'TP'
                elif sig[i] == 1:
                    exit_px, reason = c[i], 'CRUCE'

            if exit_px is not None:
                gross = pos['side'] * (exit_px / pos['entry'] - 1.0)
                net = gross - 2 * fee
                equity *= (1.0 + pos_pct * net)
                peak = max(peak, equity)
                max_dd = max(max_dd, 1 - equity / peak)
                trades.append({
                    'side': 'LONG' if pos['side'] == 1 else 'SHORT',
                    'entry_ts': int(pos['t_in']),
                    'entry_px': round(pos['entry'], 8),
                    'exit_ts': int(ts[i]),
                    'exit_px': round(float(exit_px), 8),
                    'reason': reason,
                    'pnl_pct': round(net * 100, 3),
                })
                if mode == 'long_short' and reason == 'CRUCE':
                    pending = sig[i]
                pos = None

        if pos is None and pending == 0 and sig[i] != 0:
            if not (mode == 'long_only' and sig[i] == -1):
                pending = sig[i]

    pnls = [t['pnl_pct'] for t in trades]
    n_tr = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (np.inf if wins else 0.0)
    metrics = {
        'total_return': equity - 1.0,
        'n_trades': n_tr,
        'win_rate': (len(wins) / n_tr) if n_tr else 0.0,
        'profit_factor': pf,
        'avg_pnl': (sum(pnls) / n_tr) if n_tr else 0.0,
        'max_drawdown': max_dd,
        'equity_final': equity,
    }
    return metrics, trades

# ==================== GRID SEARCH ====================
def grid_search(cfg, symbol):
    bt = cfg['backtest']
    fee = _f(cfg.get('fees', {}).get('taker', 0.0005)) or 0.0005
    mode = bt.get('mode', 'long_short')
    pos_pct = _f(bt.get('position_pct', 1.0)) or 1.0
    min_trades = int(_f(bt.get('min_trades', 10)))
    pairs = [(f, s) for f in bt['ema_fast'] for s in bt['ema_slow'] if f < s]
    sls = bt['sl_pct']
    tps = bt['tp_pct']
    total = len(bt['timeframes']) * len(pairs) * len(sls) * len(tps)
    log.info("Grid: %d TF x %d pares EMA x %d SL x %d TP = %d combinaciones",
             len(bt['timeframes']), len(pairs), len(sls), len(tps), total)
    results = []
    done = 0
    t0 = time.time()
    cache = {}
    for bar in bt['timeframes']:
        bar = TF_ALIASES.get(str(bar), str(bar))
        if bar not in TF_MS:
            log.warning("Timeframe no reconocido: %s — omitido", bar)
            continue
        log.info("Descargando historico %s %s ...", symbol, bar)
        df = fetch_history(symbol, bar, bt['start_date'])
        if len(df) < 250:
            log.warning("Historico insuficiente %s (%d velas cerradas) — se omite", bar, len(df))
            continue
        cache[bar] = df
        d0 = datetime.fromtimestamp(df['ts'].iloc[0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
        d1 = datetime.fromtimestamp(df['ts'].iloc[-1] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        log.info("%s: %d velas cerradas (%s -> %s)", bar, len(df), d0, d1)
        for (f, s) in pairs:
            dfs = add_signals(df, f, s)
            for sl in sls:
                for tp in tps:
                    m, _ = run_backtest(dfs, sl, tp, fee, mode, pos_pct)
                    m.update({'timeframe': bar, 'ema_fast': f, 'ema_slow': s,
                              'sl_pct': sl, 'tp_pct': tp, 'candles': len(df)})
                    m['valido'] = m['n_trades'] >= min_trades
                    results.append(m)
                    done += 1
                    if done % 100 == 0:
                        log.info("Progreso %d/%d ...", done, total)
    log.info("Grid completado: %d/%d combinaciones en %.1fs", done, total, time.time() - t0)
    return pd.DataFrame(results), cache

# ==================== RANKING ====================
def report_top(res, cfg):
    top_n = int(_f(cfg.get('report', {}).get('top_n', 15)))
    res = res.sort_values('total_return', ascending=False).reset_index(drop=True)
    validos = res[res['valido']]
    if len(validos) == 0:
        log.warning("Ningun set alcanzo min_trades — se muestra ranking sin filtro.")
    top = (validos if len(validos) >= 5 else res).head(top_n)
    log.info("================ TOP %d (por retorno total) ================", len(top))
    for _, r in top.iterrows():
        log.info("TF %s | EMA %d/%d | SL %.1f%% TP %.1f%% | ret %+.2f%% | trades %d | "
                 "WR %.1f%% | PF %.2f | DD %.1f%%",
                 r['timeframe'], r['ema_fast'], r['ema_slow'],
                 r['sl_pct'] * 100, r['tp_pct'] * 100,
                 r['total_return'] * 100, r['n_trades'], r['win_rate'] * 100,
                 min(r['profit_factor'], 99.99), r['max_drawdown'] * 100)
    return top.iloc[0]

# ==================== MAIN ====================
def main():
    log.info("STX CATAMARAN — BACKTEST rev.3 | host=%s | USDT prohibido", HOST)
    if not os.path.exists(CONFIG_PATH):
        log.error("Falta config.stx.yml junto al script.")
        sys.exit(1)
    with open(CONFIG_PATH, 'r', encoding='utf-8') as fh:
        cfg = yaml.safe_load(fh) or {}
    bt = cfg.get('backtest') or {}
    for k in ('start_date', 'timeframes', 'ema_fast', 'ema_slow', 'sl_pct', 'tp_pct'):
        if k not in bt:
            log.error("Falta '%s' en config.stx.yml", k)
            sys.exit(1)

    max_notional = _f(cfg.get('report', {}).get('max_notional', 15.0)) or 15.0

    # 0) catalogo real a la vista --------------------------------
    dump_stx_catalog()

    # 1) instrumento BACKTEST con cadena de fallback -------------
    bt_symbol = str(cfg.get('instrument', {}).get('backtest_symbol') or '').strip()
    origen = 'config'
    if not bt_symbol:
        bt_symbol = pick_swap_backtest()
        origen = 'SWAP perpetuo USD/USDC (sin vencimiento)'
    if not bt_symbol:
        bt_symbol = pick_future()
        origen = 'XPERP vencimiento mas lejano (historico desde su listado)'
    if not bt_symbol:
        bt_symbol = pick_spot_fallback()
        origen = 'SPOT USD/USDC (proxy de precio — no hay derivados limpios)'
    if not bt_symbol:
        log.error("No hay STX en USD/USDC (swap/futuro/spot) activo. USDT prohibido. Abortando.")
        sys.exit(1)
    log.info("Instrumento BACKTEST: %s [%s]", bt_symbol, origen)
    print_contract_report(contract_meta(bt_symbol), 'BACKTEST', max_notional)

    live = pick_future()
    if live:
        print_contract_report(contract_meta(live), 'VIVO (vencimiento mas lejano)', max_notional)
    else:
        log.warning("Sin futuro con vencimiento activo para %s.", BASE_ASSET)

    # 2) grid search -------------------------------------------
    res, cache = grid_search(cfg, bt_symbol)
    if res.empty:
        log.error("Sin resultados de backtest.")
        sys.exit(1)

    # 3) ranking -----------------------------------------------
    best = report_top(res, cfg)

    # 4) CSVs ---------------------------------------------------
    if cfg.get('report', {}).get('save_csv', True):
        res['profit_factor'] = res['profit_factor'].replace(np.inf, 99.99)
        res.to_csv(RESULTS_CSV, index=False)
        log.info("Resultados completos -> %s", RESULTS_CSV)
        if best['timeframe'] in cache:
            dfs = add_signals(cache[best['timeframe']], int(best['ema_fast']), int(best['ema_slow']))
            _, trades = run_backtest(dfs, float(best['sl_pct']), float(best['tp_pct']),
                                     _f(cfg['fees']['taker']), bt.get('mode', 'long_short'),
                                     _f(bt.get('position_pct', 1.0)))
            pd.DataFrame(trades).to_csv(TRADES_CSV, index=False)
            log.info("Trades del mejor set (%d) -> %s", len(trades), TRADES_CSV)

    log.info(">>> MEJOR SET: TF %s | EMA %d/%d | SL %.1f%% / TP %.1f%% | "
             "ret %+.2f%% | %d trades | WR %.1f%% | DD %.1f%%",
             best['timeframe'], int(best['ema_fast']), int(best['ema_slow']),
             best['sl_pct'] * 100, best['tp_pct'] * 100, best['total_return'] * 100,
             int(best['n_trades']), best['win_rate'] * 100, best['max_drawdown'] * 100)
    log.info("Activacion live: SOLO si el analisis y este backtest lo determinan.")

if __name__ == "__main__":
    main()
