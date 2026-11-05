# ===== MOTOR (de backtest-avax-v2.py, probado) =====
# backtest-avax-v2.py — validador AVAX (GSCSI). Solo lectura: datos publicos OKX, sin claves.
# Mide con realismo lo que el bot hace: cruce EMA en vela 1H cerrada -> entrada en la APERTURA
# de la vela siguiente (+ retraso opcional), SL/TP por high/low (si ambos en la misma vela: SL primero),
# comisiones + slippage en SL, 1 posicion a la vez, cooldown tras perdida.
# Anti-sobreajuste: la rejilla se ordena SOLO con el tramo TRAIN (60%); se informa el TEST (40%) que
# el optimizador nunca vio. Una ley sin ventaja en TEST no entra.
#   python backtest-avax-v2.py            -> descarga OKX (AVAX-USD swap, 1H) ~2 anios
#   python backtest-avax-v2.py --csv f.csv  (columnas ts,open,high,low,close)
#   python backtest-avax-v2.py --synthetic  (prueba de mecanica)
import argparse, itertools, sys, time
import numpy as np
import pandas as pd

FEE_RT = 0.10      # % ida+vuelta (taker 0.05% x2)
SL_SLIP = 0.05     # % extra en salida por SL (mercado)
MAX_HOLD = 24 * 14 # velas maximas (14 dias), cierre a mercado
COOLDOWN = 1       # velas tras perdida

HOST = 'https://my.okx.com'   # mismo host EEE que el bot

def _usdt(m):
    return ('USDT' in str(m.get('id') or '').upper() or str(m.get('settle') or '').upper() == 'USDT'
            or str(m.get('quote') or '').upper() == 'USDT')

def data_candidates(markets):
    """VETO USDT (MiCA/EEE): solo USD/USDC. Orden: swap USD (mas historia) > spot USDC/USD > futuros XPERP.
    Nunca hay fallback a USDT."""
    cands = [m for m in markets if m.get('base') == 'AVAX' and m.get('active') is not False and not _usdt(m)]
    swaps = [m for m in cands if m.get('swap')]
    spots = [m for m in cands if m.get('spot') and str(m.get('quote') or '').upper() in ('USDC', 'USD')]
    futs = sorted([m for m in cands if m.get('future')],
                  key=lambda m: int((m.get('info') or {}).get('expTime') or 0), reverse=True)
    return [m['symbol'] for m in (swaps + spots + futs)]

def pick_data_symbol(markets):
    c = data_candidates(markets)
    return c[0] if c else None

def fetch_hist(ex, inst_id, years):
    """Pagina hacia atras el endpoint publico history-candles de OKX (mas fiable que fetch_ohlcv con since)."""
    method = getattr(ex, 'public_get_market_history_candles', None) or getattr(ex, 'publicGetMarketHistoryCandles')
    stop = int((time.time() - years * 365 * 86400) * 1000)
    rows, after, fails = [], None, 0
    while True:
        req = {'instId': inst_id, 'bar': '1H', 'limit': '100'}
        if after:
            req['after'] = str(after)
        try:
            data = (method(req) or {}).get('data') or []
        except Exception as e:
            fails += 1
            print("  error API (" + str(fails) + "/5):", str(e)[:120], flush=True)
            if fails >= 5:
                break
            time.sleep(2)
            continue
        if not data:
            break
        for r in data:
            if str(r[-1]) == '1':          # solo velas CERRADAS (confirm=1)
                rows.append([int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
        oldest = int(data[-1][0])
        if oldest <= stop or (after is not None and oldest >= after):
            break
        after = oldest
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates('ts').sort_values('ts')
    return df[df.ts >= stop].reset_index(drop=True)

def load_okx(years=2.0):
    import ccxt
    ex = ccxt.okx({'enableRateLimit': True, 'urls': {'api': {'rest': HOST}}})
    ex.load_markets()
    cands = data_candidates(list(ex.markets.values()))
    if not cands:
        sys.exit("No hay instrumento AVAX en USD/USDC. USDT vetado: no se descargan datos. Use --csv.")
    print("Candidatos USD/USDC:", cands, flush=True)
    best = None
    for sym in cands[:4]:
        m = ex.market(sym)
        if _usdt(m):
            sys.exit("VETO USDT: " + sym)
        print("Probando", sym, "(" + str(m['id']) + ") ...", flush=True)
        df = fetch_hist(ex, m['id'], years)
        print("  velas cerradas:", len(df), flush=True)
        if best is None or len(df) > len(best[1]):
            best = (sym, df)
        if len(df) >= 2000:
            break
    sym, df = best
    if len(df) < 500:
        sys.exit("Menos de 500 velas en todos los instrumentos USD/USDC probados. Aporte datos con --csv (ts,open,high,low,close).")
    if len(df) < 2000:
        print("AVISO: solo", len(df), "velas; resultados poco fiables.", flush=True)
    print("Fuente de velas (USD/USDC):", sym, "|", len(df), "velas", flush=True)
    return df

def synthetic(n=17000, seed=7):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.006, n)
    c = 20 * np.exp(np.cumsum(r))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.002, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.002, n)))
    return pd.DataFrame({'ts': np.arange(n) * 3600000, 'open': o, 'high': h, 'low': l, 'close': c})

def ema(a, n):
    return pd.Series(a).ewm(span=n, adjust=False).mean().values

def signals(close, fast, slow, sep_min):
    ef, es = ema(close, fast), ema(close, slow)
    prev_up = ef[:-1] <= es[:-1]; cur_up = ef[1:] > es[1:]
    up = np.zeros(len(close), bool); dn = np.zeros(len(close), bool)
    up[1:] = prev_up & cur_up
    dn[1:] = (~prev_up & (ef[:-1] >= es[:-1])) & (ef[1:] < es[1:])
    sep = np.abs(ef - es) / es * 100
    ok = sep >= sep_min
    return np.where(up & ok, 1, np.where(dn & ok, -1, 0))

def simulate(o, h, l, c, sig, sl, tp, delay, lo_i, hi_i):
    """Devuelve array de retornos netos (%) de los trades con SENAL en [lo_i, hi_i)."""
    n = len(c); res = []; free_at = 0
    idxs = np.nonzero(sig[lo_i:hi_i])[0] + lo_i
    for i in idxs:
        e = i + 1 + delay
        if e >= n - 1 or e < free_at: continue
        side = sig[i]; entry = o[e]
        end = min(n, e + MAX_HOLD)
        hh, ll = h[e:end], l[e:end]
        if side == 1:
            slp, tpp = entry * (1 - sl / 100), entry * (1 + tp / 100)
            hs, ht = ll <= slp, hh >= tpp
        else:
            slp, tpp = entry * (1 + sl / 100), entry * (1 - tp / 100)
            hs, ht = hh >= slp, ll <= tpp
        fs = hs.argmax() if hs.any() else 10**9
        ft = ht.argmax() if ht.any() else 10**9
        if fs == 10**9 and ft == 10**9:
            ex_px, k = c[end - 1], end - e - 1
        elif fs <= ft:
            ex_px, k = (slp * (1 - SL_SLIP / 100) if side == 1 else slp * (1 + SL_SLIP / 100)), fs
        else:
            ex_px, k = tpp, ft
        ret = (ex_px / entry - 1) * 100 * side - FEE_RT
        res.append(ret)
        free_at = e + k + 1 + (COOLDOWN if ret < 0 else 0)
    return np.array(res)

def stats(r):
    if len(r) == 0: return dict(n=0, wr=0, avg=0, pf=0, dd=0, t=0, tot=0)
    w, ls = r[r > 0].sum(), -r[r < 0].sum()
    eq = np.cumsum(r); dd = (eq - np.maximum.accumulate(eq)).min()
    t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 2 and r.std() > 0 else 0
    return dict(n=len(r), wr=100 * (r > 0).mean(), avg=r.mean(), pf=(w / ls if ls > 0 else 9.99), dd=dd, t=t, tot=r.sum())

def grid(o, h, l, c, cut):
    n = len(c); rows = []
    fasts, slows = [3, 5, 8, 10, 13], [21, 34, 55, 89]
    seps, sls = [0, 0.1, 0.17, 0.3], [1.5, 2, 3, 4]
    tps, delays = [2, 3, 4, 5.5, 8], [0, 2]
    for f, s in itertools.product(fasts, slows):
        if f >= s: continue
        for sep in seps:
            sig = signals(c, f, s, sep)
            for sl, tp, d in itertools.product(sls, tps, delays):
                tr = stats(simulate(o, h, l, c, sig, sl, tp, d, 50, cut))
                te = stats(simulate(o, h, l, c, sig, sl, tp, d, cut, n))
                rows.append(dict(fast=f, slow=s, sep=sep, sl=sl, tp=tp, delay=d, be_wr=sl / (sl + tp) * 100,
                                 tr_n=tr['n'], tr_wr=tr['wr'], tr_avg=tr['avg'], tr_t=tr['t'],
                                 te_n=te['n'], te_wr=te['wr'], te_avg=te['avg'], te_pf=te['pf'],
                                 te_dd=te['dd'], te_t=te['t']))
    return pd.DataFrame(rows)

def robust_mask(R):
    # Ganar en TRAIN y en TEST, con PF_test>1.3 y significancia en TEST (t>=2).
    return (R.tr_n >= 30) & (R.te_n >= 15) & (R.tr_avg > 0) & (R.te_avg > 0) & (R.te_pf > 1.3) & (R.te_t >= 2)

def shuffled(o, h, l, c, seed):
    """Control nulo: baraja las velas (retornos relativos), destruye toda senal de tendencia."""
    rng = np.random.default_rng(seed)
    pc = np.r_[c[0], c[:-1]]
    ro, rh, rl, rc = o / pc, h / pc, l / pc, c / pc
    idx = rng.permutation(len(c))
    ro, rh, rl, rc = ro[idx], rh[idx], rl[idx], rc[idx]
    cc = c[0] * np.cumprod(rc); pcc = np.r_[c[0], cc[:-1]]
    return ro * pcc, rh * pcc, rl * pcc, cc

def main_v2():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv'); ap.add_argument('--synthetic', action='store_true')
    ap.add_argument('--years', type=float, default=2.0)
    ap.add_argument('--nulls', type=int, default=5)
    a = ap.parse_args()
    df = synthetic() if a.synthetic else (pd.read_csv(a.csv) if a.csv else load_okx(a.years))
    df = df.dropna().reset_index(drop=True)
    o, h, l, c = (df[k].values.astype(float) for k in ('open', 'high', 'low', 'close'))
    n = len(c); cut = int(n * 0.6)
    print("Velas:", n, "| TRAIN:", cut, "| TEST:", n - cut, flush=True)

    R = grid(o, h, l, c, cut)
    R.to_csv('avax_grid.csv', index=False)
    pd.set_option('display.width', 220); pd.set_option('display.max_columns', 30)
    cols = ['fast', 'slow', 'sep', 'sl', 'tp', 'delay', 'be_wr', 'tr_n', 'tr_wr', 'tr_avg', 'te_n', 'te_wr', 'te_avg', 'te_pf', 'te_dd']
    print("Combinaciones:", len(R))

    base = R[(R.fast == 3) & (R.slow == 21) & (R.sep == 0.17) & (R.sl == 4) & (R.tp == 5.5) & (R.delay == 2)]
    print("\n== LEY v4 (3/21, sep .17, SL4/TP5.5, retraso 2h) ==\n", base[cols].round(3).to_string(index=False))
    b2 = R[(R.fast == 8) & (R.slow == 34) & (R.sep == 0) & (R.sl == 4) & (R.tp == 5.5) & (R.delay == 0)]
    print("\n== LEY PREVIA 8/34 (sin filtro, SL4/TP5.5) ==\n", b2[cols].round(3).to_string(index=False))

    rm = robust_mask(R); n_real = int(rm.sum())
    nulls = []
    for k in range(a.nulls):
        oo, hh, ll, cc = shuffled(o, h, l, c, 100 + k)
        nulls.append(int(robust_mask(grid(oo, hh, ll, cc, cut)).sum()))
        print("control nulo", k + 1, "->", nulls[-1], "combinaciones 'robustas' por puro azar", flush=True)
    null_mean = float(np.mean(nulls)) if nulls else 0.0
    print("\nROBUSTAS en datos reales:", n_real, "| por azar (velas barajadas), media:", round(null_mean, 1), "max:", max(nulls) if nulls else 0)
    if n_real <= max(nulls + [0]) * 1.5 + 2:
        print("VEREDICTO: indistinguible del azar. NO hay evidencia de ventaja -> NO armar AVAX con ninguna de estas leyes.")
    else:
        print("VEREDICTO: hay mas candidatas que las esperables por azar. Revisar las de abajo, preferir las que estan en una")
        print("ZONA estable (parametros vecinos tambien ganan), y pasar a sombra antes de armar.")
    print(R[rm].sort_values('te_avg', ascending=False).head(15)[cols].round(3).to_string(index=False) if n_real else "(ninguna)")
    print("\nTP-rate no es el objetivo: compara wr con be_wr (WR de equilibrio). Solo importa te_avg > 0 tras comisiones.")



# ===== VALIDADOR v3 =====
# avax-validador-v3.py — GSCSI · AVAX. SOLO LECTURA (jamas envia ordenes). USDT vetado: solo USD/USDC.
# AUTOCONTENIDO: incluye el motor de backtest-avax-v2.py (no importa otros archivos).
#  1) HISTORIA LARGA: velas 1H de Coinbase AVAX-USD (USD) ~4 anios; fallback OKX USDC/USD.
#  2) Rejilla + control nulo (velas barajadas) sobre la historia larga.
#  3) Leyes v4 y 8/34 por SEMESTRE junto al movimiento del mercado: separa ley de regimen.
#  4) CONCILIACION: tus trades reales vs las senales del bot (cruce fresco? direccion vigente? retraso?)
#     --trades csv  -> data/avax_trades.csv  (entry_ts,side,entry_px,exit_px,pnl)
#     --trades okx  -> positions-history de OKX (GET, necesita claves en env)
import argparse, importlib.util, json, os, sys, time, urllib.request
import numpy as np
import pandas as pd


def _f(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0

# ---------- 1) Coinbase AVAX-USD ----------
def cb_page(start_s, end_s):
    url = ("https://api.exchange.coinbase.com/products/AVAX-USD/candles?granularity=3600&start=" +
           time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(start_s)) + "&end=" +
           time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(end_s)))
    req = urllib.request.Request(url, headers={'User-Agent': 'gscsi-validator/3'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def load_coinbase(years, page=cb_page):
    now = int(time.time() // 3600 * 3600)      # inicio de la hora actual: vela abierta excluida
    stop = now - int(years * 365 * 86400)
    rows, end, empty = [], now, 0
    while end > stop:
        start = max(stop, end - 300 * 3600)
        data = None
        for t in range(3):
            try: data = page(start, end); break
            except Exception as e:
                print("  coinbase reintento:", str(e)[:100], flush=True); time.sleep(2 + 2 * t)
        if data is None: break
        if not data:
            empty += 1
            if empty >= 6: break           # antes del listado
        else:
            empty = 0
            rows += [[int(r[0]) * 1000, r[3], r[2], r[1], r[4], r[5]] for r in data if int(r[0]) + 3600 <= now]
        end = start
        time.sleep(0.15)
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume']).astype(float)
    df['ts'] = df['ts'].astype('int64')
    return df.drop_duplicates('ts').sort_values('ts').reset_index(drop=True)

# ---------- 3) semestres ----------
def by_half(df, o, h, l, c):
    dt = pd.to_datetime(df['ts'], unit='ms')
    key = (dt.dt.year.astype(str) + 'H' + np.where(dt.dt.month > 6, '2', '1')).values
    laws = {'v4 3/21 sep.17 SL4/TP5.5 d2': (3, 21, 0.17, 4, 5.5, 2),
            '8/34 sin filtro SL4/TP5.5 d0': (8, 34, 0.0, 4, 5.5, 0)}
    sigs = {k: signals(c, v[0], v[1], v[2]) for k, v in laws.items()}
    print("\n== POR SEMESTRE (mercado = cambio del precio; n, WR%, media %/trade neta) ==")
    print("%-8s %9s | %s" % ("periodo", "mercado%", " | ".join("%-28s" % k for k in laws)))
    out = []
    for p in sorted(set(key)):
        idx = np.nonzero(key == p)[0]; lo, hi = max(idx[0], 50), idx[-1] + 1
        if hi - lo < 200: continue
        mk = (c[hi - 1] / c[lo] - 1) * 100
        cells = []
        for k, v in laws.items():
            s = stats(simulate(o, h, l, c, sigs[k], v[3], v[4], v[5], lo, hi))
            cells.append("n=%-3d WR=%4.1f avg=%+6.2f" % (s['n'], s['wr'], s['avg'])); out.append((p, k, s['n'], s['avg']))
        print("%-8s %+8.1f%% | %s" % (p, mk, " | ".join("%-28s" % x for x in cells)))
    pos = {k: sum(1 for (_, kk, n, a) in out if kk == k and n >= 5 and a > 0) for k in laws}
    tot = {k: sum(1 for (_, kk, n, a) in out if kk == k and n >= 5) for k in laws}
    for k in laws: print("  %s: gana en %d de %d semestres" % (k, pos[k], tot[k]))

# ---------- 4) conciliacion ----------
def load_trades(mode, path):
    if mode == 'csv':
        if not os.path.exists(path):
            print("(sin", path, "- plantilla: entry_ts,side,entry_px,exit_px,pnl ; entry_ts ISO UTC o ms)"); return None
        t = pd.read_csv(path)
        t['entry_ms'] = [int(x) if str(x).isdigit() else int(pd.Timestamp(x, tz='UTC').timestamp() * 1000) for x in t['entry_ts']]
        t['side'] = t['side'].astype(str).str.upper().str[0].map({'L': 'LONG', 'S': 'SHORT'})
        return t
    if mode == 'okx':
        import ccxt
        ex = ccxt.okx({'apiKey': os.getenv('OKX_API_KEY', ''), 'secret': os.getenv('OKX_SECRET_KEY', ''),
                       'password': os.getenv('OKX_PASSWORD', ''), 'enableRateLimit': True,
                       'urls': {'api': {'rest': HOST}}})
        rows = []
        for it in ('FUTURES', 'SWAP'):
            try:
                r = ex.private_get_account_positions_history({'instType': it, 'limit': '100'})
            except Exception as e:
                print("positions-history", it, "->", str(e)[:150]); continue
            for d in r.get('data') or []:
                iid = str(d.get('instId', '')).upper()
                if not iid.startswith('AVAX-USD') or 'USDT' in iid: continue
                op, cl, pnl = _f(d.get('openAvgPx')), _f(d.get('closeAvgPx')), _f(d.get('pnl'))
                dr = str(d.get('direction', '')).lower()
                side = 'LONG' if dr == 'long' else 'SHORT' if dr == 'short' else ('LONG' if (cl - op) * pnl > 0 else 'SHORT')
                rows.append(dict(entry_ms=int(d.get('cTime') or 0), side=side, entry_px=op, exit_px=cl, pnl=pnl, inst=iid))
        return pd.DataFrame(rows) if rows else None
    return None

def reconcile(df, trades, fast, slow, sep_min, sl, tp):
    ts, h, l, c = df.ts.values, df.high.values, df.low.values, df.close.values
    ef, es = ema(c, fast), ema(c, slow)
    print("\n== CONCILIACION: %d trades reales vs ley EMA%d/%d sep>=%.2f%% (la que operaba) ==" % (len(trades), fast, slow, sep_min))
    rows = []
    for _, t in trades.iterrows():
        em = int(t['entry_ms']); k = int(np.searchsorted(ts, em - 3600000, side='right') - 1)
        if k < slow + 8 or em > ts[-1] + 3600000 or em < ts[0]:
            print("  trade fuera del rango de velas:", pd.Timestamp(em, unit='ms')); continue
        cross, j = 0, None
        for j in range(0, 8):
            i = k - j
            if ef[i - 1] <= es[i - 1] and ef[i] > es[i]: cross = 1; break
            if ef[i - 1] >= es[i - 1] and ef[i] < es[i]: cross = -1; break
        else: cross = 0
        side = 1 if t['side'] == 'LONG' else -1
        state_ok = (ef[k] > es[k]) == (side == 1)
        sep = abs(ef[k - j] - es[k - j]) / es[k - j] * 100 if cross else 0
        delay_h = (em - (ts[k - j] + 3600000)) / 3600000 if cross else None
        # resultado simulado con SL/TP de la ley, desde el precio real de entrada
        ep = _f(t['entry_px']); b0 = int(np.searchsorted(ts, em, side='right') - 1); out = 'ABIERTO'
        for b in range(b0, min(len(c), b0 + 24 * 14)):
            hs = (l[b] <= ep * (1 - sl / 100)) if side == 1 else (h[b] >= ep * (1 + sl / 100))
            ht = (h[b] >= ep * (1 + tp / 100)) if side == 1 else (l[b] <= ep * (1 - tp / 100))
            if hs: out = 'SL'; break
            if ht: out = 'TP'; break
        rows.append(dict(fecha=str(pd.Timestamp(em, unit='ms'))[:16], lado=t['side'], cruce_fresco=bool(cross),
                         dir_cruce=('LONG' if cross == 1 else 'SHORT' if cross == -1 else '-'),
                         coincide=(cross == side), sep_ok=bool(cross and sep >= sep_min), estado_vigente=bool(state_ok),
                         retraso_h=None if delay_h is None else round(delay_h, 1), sep=None if not cross else round(sep, 3),
                         real=('+' if _f(t.get('pnl')) > 0 else '-'), sim=out))
    R = pd.DataFrame(rows)
    if R.empty: print("  (sin trades conciliables)"); return
    pd.set_option('display.width', 220); print(R.to_string(index=False)); R.to_csv('avax_conciliacion.csv', index=False)
    n = len(R)
    print("\nRESUMEN: n=%d | con cruce fresco en direccion operada: %d | contra el estado vigente: %d | entrada >2h tras el cruce: %d"
          % (n, int(R.coincide.sum()), int((~R.estado_vigente).sum()), int((R.retraso_h.fillna(0) > 2).sum())))
    print("         real ganadores: %d/%d | simulados TP: %d, SL: %d" % ((R.real == '+').sum(), n, (R.sim == 'TP').sum(), (R.sim == 'SL').sum()))
    print("LECTURA: muchas 'contra estado'/'sin cruce' = el bot entro mal (bug). Si coinciden y el sim tambien da SL = mercado/ley.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=float, default=4.5); ap.add_argument('--source', default='coinbase')
    ap.add_argument('--trades', default='csv'); ap.add_argument('--trades-file', default='data/avax_trades.csv')
    ap.add_argument('--synthetic', action='store_true'); ap.add_argument('--nulls', type=int, default=5)
    a = ap.parse_args()
    if a.synthetic: df = synthetic()
    else:
        df = load_coinbase(a.years) if a.source == 'coinbase' else pd.DataFrame()
        if len(df) < 2000:
            print("Coinbase insuficiente (%d velas) -> fallback OKX USD/USDC" % len(df), flush=True); df = load_okx(2.0)
    o, h, l, c = (df[k].values.astype(float) for k in ('open', 'high', 'low', 'close'))
    n = len(c); cut = int(n * 0.6)
    t0, t1 = pd.Timestamp(int(df.ts.iloc[0]), unit='ms'), pd.Timestamp(int(df.ts.iloc[-1]), unit='ms')
    print("Velas: %d | %s -> %s | TRAIN %d | TEST %d" % (n, str(t0)[:10], str(t1)[:10], cut, n - cut), flush=True)

    R = grid(o, h, l, c, cut); R.to_csv('avax_grid_largo.csv', index=False)
    cols = ['fast', 'slow', 'sep', 'sl', 'tp', 'delay', 'be_wr', 'tr_n', 'tr_wr', 'tr_avg', 'te_n', 'te_wr', 'te_avg', 'te_pf', 'te_dd']
    pd.set_option('display.width', 220); pd.set_option('display.max_columns', 30)
    for nm, q in (("LEY v4 (3/21 sep.17 SL4/TP5.5 d2)", (R.fast == 3) & (R.slow == 21) & (R.sep == 0.17) & (R.sl == 4) & (R.tp == 5.5) & (R.delay == 2)),
                  ("LEY 8/34 (sin filtro SL4/TP5.5 d0)", (R.fast == 8) & (R.slow == 34) & (R.sep == 0) & (R.sl == 4) & (R.tp == 5.5) & (R.delay == 0))):
        print("\n==", nm, "==\n", R[q][cols].round(3).to_string(index=False))
    rm = robust_mask(R); n_real = int(rm.sum()); nulls = []
    for k in range(a.nulls):
        nulls.append(int(robust_mask(grid(*shuffled(o, h, l, c, 100 + k), cut)).sum()))
        print("control nulo", k + 1, "->", nulls[-1], flush=True)
    print("\nROBUSTAS reales:", n_real, "| azar media:", round(float(np.mean(nulls)), 1), "max:", max(nulls))
    print("VEREDICTO:", "indistinguible del azar -> NO armar AVAX." if n_real <= max(nulls) * 1.5 + 2
          else "supera el azar: revisar la zona estable y pasar a SOMBRA antes de armar.")
    print(R[rm].sort_values('te_avg', ascending=False).head(12)[cols].round(3).to_string(index=False) if n_real else "(ninguna)")
    by_half(df, o, h, l, c)

    if a.trades != 'none':
        T = load_trades(a.trades, a.trades_file)
        if T is not None and len(T): reconcile(df, T, 3, 21, 0.17, 4.0, 5.5)
        else: print("\n(Sin trades para conciliar. Sube data/avax_trades.csv o usa --trades okx.)")

if __name__ == '__main__':
    main()
