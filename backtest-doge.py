# backtest-doge.py — Ax2-DOGE · EL PERRO · VALIDADOR DE LA LEY v2 (rev.1)
#   MISION: validar con datos lo que nunca se backtesteó:
#     1) La geometria v2 ACTUAL: EMA3/21 2m + CANDADO anti-rango (0.15%)
#     2) El umbral del candado: 0.0 (sin candado) / 0.10 / 0.15 / 0.25
#     3) LA PREGUNTA DIRECCIONAL: long_only vs long_short vs short_only
#        ("el perro solo sabe aupar?" — shorts 2W/3L es n=5, un outlier v1 domina)
#     4) SL/TP: decreto actual (L: SL2/TP4 · S: SL3/TP4) + grid simetrico
#     5) 2m vs 5m: el costo real de fees en scalping
#   FIDELIDAD AL BOT v2 (main-doge.py):
#     - Senal: cruce EMA3/21 en vela CERRADA + candado (separacion >= umbral)
#     - SIN flip: posicion abierta ignora senales hasta SL/TP (como el live)
#     - Entrada en apertura de la vela siguiente; SL/TP intravela (peor caso)
#     - Cooldown 60 min tras perdida (Ax3) — incluido en la simulacion
#     - Fees: taker 0.05% por lado
#   Instrumento: cadena de fallback USDT-VETADO (XPERP esperado, igual que live).
#   KEYS: variables de entorno. Endpoints publicos.
#   Salida: backtest_results_doge.csv
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
BASE_ASSET = 'DOGE'
HOST = 'https://my.okx.com'
START_DATE = '2026-08-01'
FEE_TAKER = 0.0005
GUARDIA_USD = 15.0
MIN_TRADES_BEST = 10
COOLDOWN_MIN_BT = 60          # mismo cooldown que el bot live (Ax3)

TIMEFRAMES = ['2m', '5m']
EMAS = (3, 21)

CANDADOS = [0.0, 0.10, 0.15, 0.25]   # 0.0 = sin candado (baseline: mide su aporte)
SLTP_SETS = [
    ('decreto-v2', 0.020, 0.040, 0.030, 0.040),   # el decreto actual asimetrico
    ('sl1.0/tp3', 0.010, 0.030, 0.010, 0.030),
    ('sl1.5/tp3', 0.015, 0.030, 0.015, 0.030),
    ('sl1.5/tp4', 0.015, 0.040, 0.015, 0.040),
    ('sl1.5/tp6', 0.015, 0.060, 0.015, 0.060),
    ('sl2/tp3',   0.020, 0.030, 0.020, 0.030),
    ('sl2/tp4',   0.020, 0.040, 0.020, 0.040),
    ('sl2/tp6',   0.020, 0.060, 0.020, 0.060),
    ('sl3/tp4',   0.030, 0.040, 0.030, 0.040),
    ('sl3/tp6',   0.030, 0.060, 0.030, 0.060),
]
MODES = ['long_short', 'long_only', 'short_only']

TF_MS = {'2m': 120000, '5m': 300000}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_CSV = os.path.join(BASE_DIR, 'backtest_results_doge.csv')

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
        log.info("AMOUNT del bot = 10 ct = $%.2f | guardia $%.2f -> max %d ct",
                 10 * meta['usd_1ct'], GUARDIA_USD,
                 int(GUARDIA_USD // meta['usd_1ct']))

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

# ==================== SENAL: CRUCE EMA3/21 + CANDADO v2 ====================
def ema(s, length):
    return s.ewm(span=length, adjust=False).mean()

def add_signals(df, fast, slow, candado_pct):
    """Cruce EMA fast/slow en vela cerrada. CANDADO v2: si en la vela de la
    senal |EMA_f-EMA_s| < candado_pct (% del precio) -> VETO (rango).
    Devuelve (df con signal, senales_crudas, vetos)."""
    df = df.copy()
    df['e_f'] = ema(df['close'], fast)
    df['e_s'] = ema(df['close'], slow)
    diff = df['e_f'] - df['e_s']
    up = (diff > 0) & (diff.shift(1) <= 0)
    dn = (diff < 0) & (diff.shift(1) >= 0)
    n_vetos = 0
    n_senales = int((up | dn).sum())
    if candado_pct > 0:
        sep = diff.abs() / df['e_s'] * 100.0
        veto = sep < float(candado_pct)
        n_vetos = int(((up | dn) & veto).sum())
        up = up & ~veto
        dn = dn & ~veto
    df['signal'] = np.select([up, dn], [1, -1], default=0)
    df.loc[df.index[:slow], 'signal'] = 0
    return df, n_senales, n_vetos

# ==================== MOTOR (FIEL AL BOT v2: sin flip + cooldown) ====================
def run_backtest(df, sl_l, tp_l, sl_s, tp_s, fee, mode,
                 cooldown_min=COOLDOWN_MIN_BT, pos_pct=1.0):
    cooldown_ms = int(cooldown_min * 60000)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    trades = []
    pos = None
    pending = 0
    cooldown_until = -1

    o = df['open'].values
    h = df['high'].values
    l = df['low'].values
    c = df['close'].values
    sig = df['signal'].values
    ts = df['ts'].values
    n = len(df)

    for i in range(1, n):
        # 1) entrada pendiente en la apertura (respetando cooldown)
        if pending != 0 and pos is None:
            if ts[i] >= cooldown_until:
                side = pending
                entry = o[i]
                sl = entry * (1 - sl_l) if side == 1 else entry * (1 + sl_s)
                tp = entry * (1 + tp_l) if side == 1 else entry * (1 - tp_s)
                pos = {'side': side, 'entry': entry, 'sl': sl, 'tp': tp}
            pending = 0

        # 2) gestionar posicion: SOLO SL/TP (sin flip — fiel al bot v2)
        if pos is not None:
            exit_px = None
            reason = ''
            if pos['side'] == 1:
                if l[i] <= pos['sl']:
                    exit_px, reason = pos['sl'], 'SL'
                elif h[i] >= pos['tp']:
                    exit_px, reason = pos['tp'], 'TP'
            else:
                if h[i] >= pos['sl']:
                    exit_px, reason = pos['sl'], 'SL'
                elif l[i] <= pos['tp']:
                    exit_px, reason = pos['tp'], 'TP'

            if exit_px is not None:
                gross = pos['side'] * (exit_px / pos['entry'] - 1.0)
                net = gross - 2 * fee
                equity *= (1.0 + pos_pct * net)
                peak = max(peak, equity)
                max_dd = max(max_dd, 1 - equity / peak)
                trades.append({'side': pos['side'], 'reason': reason, 'net': net,
                               'ts': int(ts[i])})
                if net < 0:
                    cooldown_until = int(ts[i]) + cooldown_ms   # Ax3
                pos = None

        # 3) captar senal al cierre (si libre y fuera de cooldown)
        if pos is None and pending == 0 and sig[i] != 0:
            if mode == 'long_only' and sig[i] == -1:
                continue
            if mode == 'short_only' and sig[i] == 1:
                continue
            if ts[i] >= cooldown_until:
                pending = int(sig[i])

    # ---- metricas (WR por conteo real, rev.3 style) ----
    n_tr = len(trades)
    longs = [t for t in trades if t['side'] == 1]
    shorts = [t for t in trades if t['side'] == -1]
    wins = [t for t in trades if t['net'] > 0]
    sum_w = sum(t['net'] for t in wins)
    sum_l = sum(abs(t['net']) for t in trades if t['net'] <= 0)
    pf = (sum_w / sum_l) if sum_l > 0 else (np.inf if sum_w > 0 else 0.0)
    return {'total_return': equity - 1.0,
            'n_trades': n_tr,
            'n_wins': len(wins),
            'win_rate': (len(wins) / n_tr) if n_tr else 0.0,
            'profit_factor': pf,
            'avg_net': (sum(t['net'] for t in trades) / n_tr) if n_tr else 0.0,
            'max_drawdown': max_dd,
            'r_tp': sum(1 for t in trades if t['reason'] == 'TP'),
            'r_sl': sum(1 for t in trades if t['reason'] == 'SL'),
            'n_long': len(longs),
            'n_short': len(shorts),
            'wr_long': (sum(1 for t in longs if t['net'] > 0) / len(longs)) if longs else 0.0,
            'wr_short': (sum(1 for t in shorts if t['net'] > 0) / len(shorts)) if shorts else 0.0,
            'pnl_long': sum(t['net'] for t in longs),
            'pnl_short': sum(t['net'] for t in shorts)}

# ==================== MAIN ====================
def main():
    log.info("DOGE EL PERRO — VALIDADOR DE LA LEY v2 rev.1 | host=%s | USDT prohibido", HOST)
    log.info("Fiel al bot: sin flip (solo SL/TP) + cooldown %dm tras perdida + candado v2",
             COOLDOWN_MIN_BT)
    dump_catalog()

    symbol = pick_swap_backtest()
    origen = 'SWAP perpetuo USD/USDC'
    if not symbol:
        symbol = pick_future()
        origen = 'XPERP vencimiento mas lejano (igual que live)'
    if not symbol:
        symbol = pick_spot_fallback()
        origen = 'SPOT USD/USDC (proxy de precio)'
    if not symbol:
        log.error("No hay %s en USD/USDC (swap/futuro/spot) activo. USDT prohibido. Abortando.", BASE_ASSET)
        sys.exit(1)
    log.info("Instrumento BACKTEST: %s [%s]", symbol, origen)
    print_contract_report(contract_meta(symbol), 'BACKTEST')

    hist = {}
    for bar in TIMEFRAMES:
        log.info("Descargando historico %s %s ...", symbol, bar)
        df = fetch_history(symbol, bar, START_DATE)
        if len(df) < 250:
            log.warning("Historico insuficiente %s (%d velas cerradas) — se omite", bar, len(df))
            hist[bar] = None
            continue
        d0 = datetime.fromtimestamp(df['ts'].iloc[0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
        d1 = datetime.fromtimestamp(df['ts'].iloc[-1] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        days = (df['ts'].iloc[-1] - df['ts'].iloc[0]) / 86400000.0
        log.info("%s: %d velas cerradas (%s -> %s) = %.1f dias", bar, len(df), d0, d1, days)
        hist[bar] = {'df': df, 'days': max(days, 1.0)}

    log.info("================ VALIDADOR (decreto + grid + candados + modos) ================")
    results = []
    for bar in TIMEFRAMES:
        hh = hist.get(bar)
        if not hh:
            continue
        df, days = hh['df'], hh['days']
        for candado in CANDADOS:
            dfs, n_sig, n_vetos = add_signals(df, EMAS[0], EMAS[1], candado)
            log.info("TF %s | candado %.2f%% | senales crudas %d -> vetadas %d -> libres %d",
                     bar, candado, n_sig, n_vetos, n_sig - n_vetos)
            for nombre, sl_l, tp_l, sl_s, tp_s in SLTP_SETS:
                for mode in MODES:
                    m = run_backtest(dfs, sl_l, tp_l, sl_s, tp_s, FEE_TAKER, mode)
                    m.update({'timeframe': bar, 'sltp': nombre,
                              'sl_l': sl_l, 'tp_l': tp_l, 'sl_s': sl_s, 'tp_s': tp_s,
                              'candado_pct': candado, 'mode': mode,
                              'signals': n_sig, 'vetoes': n_vetos,
                              'days': round(days, 1),
                              'trades_per_week': round(m['n_trades'] * 7.0 / days, 2)})
                    results.append(m)

    if not results:
        log.error("Sin resultados (historicos insuficientes).")
        sys.exit(1)

    res = pd.DataFrame(results)
    res['profit_factor'] = res['profit_factor'].replace(np.inf, 99.99)
    res.to_csv(RESULTS_CSV, index=False)
    log.info("Resultados completos -> %s", RESULTS_CSV)

    log.info("=========== A) EL DECRETO v2 (L:SL2/TP4 · S:SL3/TP4) vs CANDADO (long_short) ============")
    for bar in TIMEFRAMES:
        sub = res[(res['timeframe'] == bar) & (res['sltp'] == 'decreto-v2')
                  & (res['mode'] == 'long_short')].sort_values('candado_pct')
        for _, r in sub.iterrows():
            log.info("[%s] candado %.2f%% | ret %+.2f%% | trades %d (%.1f/sem) | vetos %d | "
                     "WR %.1f%% | PF %.2f | DD %.1f%% | TP/SL %d/%d | "
                     "longs %d (WR %.0f%% PnL %+.3f) | shorts %d (WR %.0f%% PnL %+.3f) | avg %+.3f%%",
                     bar, r['candado_pct'], r['total_return'] * 100, int(r['n_trades']),
                     r['trades_per_week'], int(r['vetoes']), r['win_rate'] * 100,
                     r['profit_factor'], r['max_drawdown'] * 100,
                     int(r['r_tp']), int(r['r_sl']),
                     int(r['n_long']), r['wr_long'] * 100, r['pnl_long'],
                     int(r['n_short']), r['wr_short'] * 100, r['pnl_short'],
                     r['avg_net'] * 100)

    log.info("=========== B) MEJOR CONFIG POR MODO (grid completo) ============")
    for bar in TIMEFRAMES:
        for mode in MODES:
            sub = res[(res['timeframe'] == bar) & (res['mode'] == mode)
                      & (res['n_trades'] >= MIN_TRADES_BEST)]
            if sub.empty:
                log.info("[%s] %s: sin configs con >=%d trades", bar, mode, MIN_TRADES_BEST)
                continue
            best = sub.sort_values('total_return', ascending=False).iloc[0]
            log.info("[%s] %s | %s candado %.2f%% | SL_l %.1f/TP_l %.1f SL_s %.1f/TP_s %.1f | "
                     "ret %+.2f%% | trades %d | WR %.1f%% | PF %.2f | DD %.1f%%",
                     bar, mode, best['sltp'], best['candado_pct'],
                     best['sl_l'] * 100, best['tp_l'] * 100,
                     best['sl_s'] * 100, best['tp_s'] * 100,
                     best['total_return'] * 100, int(best['n_trades']),
                     best['win_rate'] * 100, best['profit_factor'],
                     best['max_drawdown'] * 100)

    log.info("=========== C) LA PREGUNTA DEL CARBONO: el perro sabe shortear? ============")
    for bar in TIMEFRAMES:
        sub = res[(res['timeframe'] == bar) & (res['sltp'] == 'decreto-v2')
                  & (res['mode'] == 'long_short') & (res['candado_pct'] == 0.15)]
        if sub.empty:
            continue
        r = sub.iloc[0]
        log.info("[%s] decreto+candado0.15 -> LONGS: %d trades PnL %+.3f (WR %.0f%%) | "
                 "SHORTS: %d trades PnL %+.3f (WR %.0f%%)",
                 bar, int(r['n_long']), r['pnl_long'], r['wr_long'] * 100,
                 int(r['n_short']), r['pnl_short'], r['wr_short'] * 100)

    log.info("Veredicto pendiente del Carbono: mantener ley 2m, ajustar candado,")
    log.info("restringir direccion, o migrar TF. Enmiendas SOLO con estos datos.")

if __name__ == "__main__":
    main()
