# backtest-stx.py — Ax2-STX · EL CATAMARAN · BACKTEST pre-activacion
#   MISION: decidir con datos la estrategia STX ANTES de activar live.
#   Entregables:
#     - Marco temporal ganador (1H / 4H / 1D)
#     - SL/TP optimos por grid-search (incluye proporciones decreto SUI: 4/5.5 y 3/4.5)
#     - Valor de 1 contrato y minimo por orden (leido de la API, no suposiciones)
#   Instrumento BACKTEST: SWAP USDC (maxima historia) — USDT PROHIBIDO (MiCA/EEE)
#   Instrumento VIVO (referencia): XPERP STX/USD vencimiento mas lejano, fallback SWAP USD
#   KEYS: variables de entorno — MISMO fragmento que main-sui.py
#     Termux/Linux: export OKX_API_KEY='...' ; export OKX_SECRET_KEY='...' ; export OKX_PASSWORD='...'
#   NOTA: usa SOLO endpoints publicos (no gasta keys ni fondos).
#   Salidas: backtest_results_stx.csv | trades_mejor_set_stx.csv
#   Requisitos: pip install ccxt pandas numpy pyyaml
#   Reglas del motor (sin lookahead):
#     - Senal al CIERRE de vela i -> entrada en APERTURA de i+1
#     - SL/TP intravela (high/low); si ambos tocan en la misma vela -> SL (peor caso)
#     - Cruce contrario -> cierre en close + giro en la apertura siguiente
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
QUOTE_PROHIBIDO = 'USDT'

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

# ==================== INSTRUMENTO (NUNCA USDT) ====================
def _is_forbidden(symbol):
    settle = str(exchange.market(symbol).get('settle') or '').upper()
    return settle == QUOTE_PROHIBIDO

def pick_swap_usdc():
    exchange.load_markets()
    fallback_usd = None
    for m in exchange.markets.values():
        if m.get('base') != BASE_ASSET or not m.get('active'):
            continue
        if not (m.get('swap') or str((m.get('info') or {}).get('instType') or '').upper() == 'SWAP'):
            continue
        if _is_forbidden(m['symbol']):
            continue
        settle = str(m.get('settle') or '').upper()
        if settle == 'USDC':
            return m['symbol']
        if settle == 'USD' and fallback_usd is None:
            fallback_usd = m['symbol']
    return fallback_usd

def pick_future():
    exchange.load_markets()
    candidatos = []
    for m in exchange.markets.values():
        if (m.get('base') == BASE_ASSET and m.get('future') and m.get('active')
                and not _is_forbidden(m['symbol'])):
            info = m.get('info') or {}
            try:
                exp = int(info.get('expTime') or 0)
            except (TypeError, ValueError):
                exp = 0
            candidatos.append((exp, m['symbol']))
    if not candidatos:
        return None
    candidatos.sort(reverse=True)
    return candidatos[0][1]

# ==================== VALOR DE 1 CONTRATO ====================
def contract_meta(symbol):
    market = exchange.market(symbol)
    info = market.get('info') or {}
    ctval = _f(market.get('contractSize') or info.get('ctVal') or 1)
    ccy = str(info.get('ctValCcy') or BASE_ASSET).upper()
    settle = str(info.get('settleCcy') or market.get('settle') or '?').upper()
    min_sz = _f(info.get('minSz') or 1)
    lot_sz = _f(info.get('lotSz') or 1)
    tick = str(info.get('tickSz') or '?')
    lev = str(info.get('maxLever') or '?')
    exp = int(_f(info.get('expTime')))
    price = _f((exchange.fetch_ticker(symbol) or {}).get('last') or 0)
    usd_1ct = ctval if ccy in ('USD', 'USDC', 'USDT') else ctval * price
    return {'symbol': symbol, 'ctval': ctval, 'ccy': ccy, 'settle': settle,
            'min_sz': min_sz, 'lot_sz': lot_sz, 'tick': tick, 'lev': lev,
            'exp': exp, 'price': price, 'usd_1ct': usd_1ct}

def print_contract_report(meta, etiqueta, max_notional):
    log.info("---- CONTRATO [%s] ----", etiqueta)
    log.info("Simbolo: %s | settle=%s", meta['symbol'], meta['settle'])
    if meta['exp'] > 0:
        vto = datetime.fromtimestamp(meta['exp'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
        log.info("Vencimiento: %s (XPERP = vencimiento mas lejano)", vto)
    log.info("1 contrato = %s %s | precio ~%s | nocional 1 ct ~$%.4f",
             meta['ctval'], meta['ccy'], meta['price'], meta['usd_1ct'])
    log.info("Minimo por orden: %s contrato(s) ~$%.4f | lot %s | tick %s | lev max %sx",
             meta['min_sz'], meta['min_sz'] * meta['usd_1ct'],
             meta['lot_sz'], meta['tick'], meta['lev'])
    if meta['usd_1ct'] > 0:
        n_max = int(max_notional // meta['usd_1ct'])
        log.info("Con guardia $%.2f (misma que SUI): max %d contrato(s) por orden",
                 max_notional, n_max)

# ==================== HISTORICO (velas cerradas SIEMPRE) ====================
def _candles_raw(inst_id, bar, limit=300, after=None, history=False):
    req = {'instId': inst_id, 'bar': bar, 'limit': str(limit)}
    if after:
        req['after'] = str(after)
    method = exchange.publicGetMarketHistoryCandles if history else exchange.publicGetMarketCandles
    resp = method(req)
    return resp.get('data') or []

def fetch_history(symbol, bar, start_date):
    tf_ms = TF_MS[bar]
    start_ms = int(datetime.strptime(start_date, '%Y-%m-%d')
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    inst_id = exchange.market(symbol)['id']
    rows = _candles_raw(inst_id, bar, 300)
    if not rows:
        log.warning("Sin velas iniciales %s %s", symbol, bar)
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
            log.warning("Historico insuficiente %s (%d velas) — se omite", bar, len(df))
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
    top = validos if len(validos) >= 5 else res
    top = top.head(top_n)
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
    log.info("STX CATAMARAN — BACKTEST pre-activacion | host=%s | USDT prohibido", HOST)
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

    exchange.load_markets()
    max_notional = _f(cfg.get('report', {}).get('max_notional', 15.0)) or 15.0

    # 1) instrumentos + valor de 1 contrato --------------------
    bt_symbol = str(cfg.get('instrument', {}).get('backtest_symbol') or '').strip()
    if not bt_symbol:
        bt_symbol = pick_swap_usdc()
    if not bt_symbol:
        log.error("No hay SWAP %s en USD/USDC activo. USDT prohibido. Abortando.", BASE_ASSET)
        sys.exit(1)
    log.info("Instrumento BACKTEST: %s", bt_symbol)
    print_contract_report(contract_meta(bt_symbol), 'BACKTEST', max_notional)

    live = pick_future()
    if live:
        print_contract_report(contract_meta(live), 'VIVO (vencimiento mas lejano)', max_notional)
    else:
        log.warning("Sin futuro con vencimiento activo para %s (el swap cubre el rol).", BASE_ASSET)

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
