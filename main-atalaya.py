import os, time, ccxt, pandas as pd
def f(x):
    try:
        return float(x)
    except Exception:
        return 0.0
SHIPS = {
    'DOGE': ('5m', 3, 21),
    'ADA': ('6h', 4, 17),
    'AVAX': ('1h', 8, 34),
    'HBAR': ('30m', 3, 21),
    'XLM': ('1h', 3, 21),
}
k = os.getenv('OKX_API_KEY', '')
s = os.getenv('OKX_SECRET_KEY', '')
p = os.getenv('OKX_PASSWORD', '')
if not (k and s and p):
    raise SystemExit('[FALLO-CERRADO] keys')
ex = ccxt.okx({
    'apiKey': k, 'secret': s, 'password': p,
    'enableRateLimit': True, 'timeout': 20000,
    'options': {'defaultType': 'swap'},
    'urls': {'api': {'rest': 'https://my.okx.com'}},
})
ex.load_markets()
fut = {}
for m in ex.markets.values():
    b = m.get('base')
    if (b in SHIPS and m.get('active')
            and m.get('future')
            and str(m.get('settle') or '') != 'USDT'):
        e = f((m.get('info') or {}).get('expTime'))
        if e >= fut.get(b, (0, None))[0]:
            fut[b] = (e, m['symbol'])
print('EL VIGIA | UTC',
      time.strftime('%m-%d %H:%M', time.gmtime()))
sen = []
for name, (tf, nf, ns) in SHIPS.items():
    sym = fut.get(name, (0, None))[1]
    if not sym:
        print(name, 'sin XPERP USD activo')
        continue
    try:
        o = ex.fetch_ohlcv(sym, tf, limit=100)
        df = pd.DataFrame(
            o, columns=['t', 'o', 'h', 'l', 'c', 'v'])
                  - name: Chequeo de sintaxis
      run: python -m py_compile main-atalaya.py
        if tf.endswith('m'):
            ms *= 60000
        else:
            ms *= 3600000
        if int(df['t'].iloc[-1]) + ms > int(
                time.time() * 1000):
            df = df.iloc[:-1]
        ef = df['c'].ewm(span=nf, adjust=False).mean()
        es = df['c'].ewm(span=ns, adjust=False).mean()
        n = len(df) - 1
        sp0 = abs(ef.iloc[n] - es.iloc[n]) / es.iloc[n]
        est = 'ALZA' if ef.iloc[n] > es.iloc[n] else 'BAJA'
        print(name, sym, tf, '| sep',
              format(sp0 * 100, '.2f') + '% |', est)
              for i in (-2, -3, -4):
            j = n + 1 + i
            if j < ns + 2:
                continue
            sp = abs(ef.iloc[j] - es.iloc[j]) / es.iloc[j]
            if sp < 0.0015:
                continue
            up = (ef.iloc[j - 1] <= es.iloc[j - 1]
                  and ef.iloc[j] > es.iloc[j])
            dn = (ef.iloc[j - 1] >= es.iloc[j - 1]
                  and ef.iloc[j] < es.iloc[j])
            if up or dn:
                lado = 'LONG' if up else 'SHORT'
                print('>>> SENAL', name, lado,
                      format(sp * 100, '.2f') + '%')
                sen.append(name + ' ' + lado)
                break
    except Exception as e:
        print(name, 'fallo:', str(e))
print('-' * 30)
print('SENIALES:', sen if sen else 'mar en calma')
