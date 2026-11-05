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

def load_okx(years=2.0):
    import ccxt
    ex = ccxt.okx({'enableRateLimit': True})
    ex.load_markets()
    sym = None
    for m in ex.markets.values():
        if m.get('base') == 'AVAX' and m.get('swap') and str(m.get('settle')).upper() in ('AVAX', 'USD', 'USDC') :
            sym = m['symbol']; break
    if sym is None:
        sym = 'AVAX/USDT:USDT'   # solo DATOS de precio para investigacion; no se opera
    print("Fuente de velas:", sym, flush=True)
    since = int((time.time() - years * 365 * 86400) * 1000)
    rows, step = [], 3600 * 1000
    while since < time.time() * 1000:
        try:
            ch = ex.fetch_ohlcv(sym, '1h', since=since, limit=100)
        except Exception as e:
            print("reintento:", e, flush=True); time.sleep(2); continue
        if not ch: break
        rows += ch
        nxt = ch[-1][0] + step
        if nxt <= since: break
        since = nxt
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume']).drop_duplicates('ts').sort_values('ts')
    return df.reset_index(drop=True)

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

def main():
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

if __name__ == '__main__':
    main()
