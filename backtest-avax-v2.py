# avax-validador-v3.py — GSCSI · AVAX. SOLO LECTURA (jamas envia ordenes). USDT vetado: solo USD/USDC.
# Requiere backtest-avax-v2.py en la misma carpeta (reutiliza su motor probado).
#  1) HISTORIA LARGA: velas 1H de Coinbase AVAX-USD (USD) ~4 anios; fallback OKX USDC/USD.
#  2) Rejilla + control nulo (velas barajadas) sobre la historia larga.
#  3) Leyes v4 y 8/34 por SEMESTRE junto al movimiento del mercado: separa ley de regimen.
#  4) CONCILIACION: tus trades reales vs las senales del bot (cruce fresco? direccion vigente? retraso?)
#     --trades csv  -> data/avax_trades.csv  (entry_ts,side,entry_px,exit_px,pnl)
#     --trades okx  -> positions-history de OKX (GET, necesita claves en env)
import argparse, importlib.util, json, os, sys, time, urllib.request
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('bt2', os.path.join(HERE, 'backtest-avax-v2.py'))
bt = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bt)

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
    sigs = {k: bt.signals(c, v[0], v[1], v[2]) for k, v in laws.items()}
    print("\n== POR SEMESTRE (mercado = cambio del precio; n, WR%, media %/trade neta) ==")
    print("%-8s %9s | %s" % ("periodo", "mercado%", " | ".join("%-28s" % k for k in laws)))
    out = []
    for p in sorted(set(key)):
        idx = np.nonzero(key == p)[0]; lo, hi = max(idx[0], 50), idx[-1] + 1
        if hi - lo < 200: continue
        mk = (c[hi - 1] / c[lo] - 1) * 100
        cells = []
        for k, v in laws.items():
            s = bt.stats(bt.simulate(o, h, l, c, sigs[k], v[3], v[4], v[5], lo, hi))
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
                       'urls': {'api': {'rest': bt.HOST}}})
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
    ef, es = bt.ema(c, fast), bt.ema(c, slow)
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
    if a.synthetic: df = bt.synthetic()
    else:
        df = load_coinbase(a.years) if a.source == 'coinbase' else pd.DataFrame()
        if len(df) < 2000:
            print("Coinbase insuficiente (%d velas) -> fallback OKX USD/USDC" % len(df), flush=True); df = bt.load_okx(2.0)
    o, h, l, c = (df[k].values.astype(float) for k in ('open', 'high', 'low', 'close'))
    n = len(c); cut = int(n * 0.6)
    t0, t1 = pd.Timestamp(int(df.ts.iloc[0]), unit='ms'), pd.Timestamp(int(df.ts.iloc[-1]), unit='ms')
    print("Velas: %d | %s -> %s | TRAIN %d | TEST %d" % (n, str(t0)[:10], str(t1)[:10], cut, n - cut), flush=True)

    R = bt.grid(o, h, l, c, cut); R.to_csv('avax_grid_largo.csv', index=False)
    cols = ['fast', 'slow', 'sep', 'sl', 'tp', 'delay', 'be_wr', 'tr_n', 'tr_wr', 'tr_avg', 'te_n', 'te_wr', 'te_avg', 'te_pf', 'te_dd']
    pd.set_option('display.width', 220); pd.set_option('display.max_columns', 30)
    for nm, q in (("LEY v4 (3/21 sep.17 SL4/TP5.5 d2)", (R.fast == 3) & (R.slow == 21) & (R.sep == 0.17) & (R.sl == 4) & (R.tp == 5.5) & (R.delay == 2)),
                  ("LEY 8/34 (sin filtro SL4/TP5.5 d0)", (R.fast == 8) & (R.slow == 34) & (R.sep == 0) & (R.sl == 4) & (R.tp == 5.5) & (R.delay == 0))):
        print("\n==", nm, "==\n", R[q][cols].round(3).to_string(index=False))
    rm = bt.robust_mask(R); n_real = int(rm.sum()); nulls = []
    for k in range(a.nulls):
        nulls.append(int(bt.robust_mask(bt.grid(*bt.shuffled(o, h, l, c, 100 + k), cut)).sum()))
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
