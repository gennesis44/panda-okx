import time
import ccxt

# ════════════════════════════════════════════════════════════════
#  bt-sftp.py — Backtest SISTEMA COMPLETO sobre velas reales OKX
#  Señal: cruce MA (SMA) · SL: 1.5×ATR(14) · TP: 2R (sens. 1.5/3R)
#  Fees: 0.05%/lado. Ambigüedad intrabar (SL y TP en misma vela)
#  se resuelve como SL (peor caso). Público, sin claves.
#  PnL en R = unidades de riesgo → riesgo 1%/trade ⇒ R ≈ % capital
# ════════════════════════════════════════════════════════════════

FEE_LADO = 0.0005
SL_MULT  = 1.5
TP_MULT  = 2.0
TP_SENS  = [1.5, 2.0, 3.0]

OBJETIVOS = [
    ('BTC-USDT-SWAP',            ['4H', '1H']),
    ('DOGE-USD_UM_XPERP-310404', ['4H']),
    ('SUI-USD_UM_XPERP-310404',  ['4H']),
    ('XLM-USD_UM_XPERP-310704',  ['4H']),
    ('FET-USD_UM_XPERP-310912',  ['4H']),
]

COMBOS = [(5,10),(5,20),(7,21),(8,21),(9,21),(10,21),
          (10,30),(12,26),(20,50),(50,100),(50,200)]

exchange = ccxt.okx({'urls': {'api': {'rest': 'https://my.okx.com'}}})

def velas(instId, bar, total):
    out, after = [], None
    while len(out) < total:
        params = {'instId': instId, 'bar': bar, 'limit': '100'}
        if after: params['after'] = after
        data = exchange.publicGetMarketHistoryCandles(params).get('data', [])
        if not data: break
        out.extend(data)
        after = data[-1][0]
        if len(data) < 100: break
        time.sleep(0.15)
    out.reverse()
    return [(float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in out]  # o,h,l,c

def sma(v, n):
    out = [None]*len(v); acc = 0.0
    for i, x in enumerate(v):
        acc += x
        if i >= n: acc -= v[i-n]
        if i >= n-1: out[i] = acc/n
    return out

def atr_series(h, l, c, n=14):
    atrs = [None]*len(c)
    tr = [None] + [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
    acc = 0.0
    for i in range(1, len(c)):
        acc += tr[i]
        if i > n: acc -= tr[i-n]
        if i >= n: atrs[i] = acc/n
    return atrs

def simular(c, h, l, mf, ms, atrs, sl_mult, tp_mult):
    trades, pos = [], None
    for i in range(1, len(c)):
        if mf[i] is None or ms[i] is None or mf[i-1] is None or ms[i-1] is None:
            continue
        arriba = mf[i-1] <= ms[i-1] and mf[i] > ms[i]
        abajo  = mf[i-1] >= ms[i-1] and mf[i] < ms[i]

        if pos is not None:
            tipo, px = None, None
            if pos['side'] == 'long':
                if l[i] <= pos['sl']:                    tipo, px = 'SL', pos['sl']
                elif h[i] >= pos['tp']:                  tipo, px = 'TP', pos['tp']
                elif abajo:                              tipo, px = 'SEN', c[i]
            else:
                if h[i] >= pos['sl']:                    tipo, px = 'SL', pos['sl']
                elif l[i] <= pos['tp']:                  tipo, px = 'TP', pos['tp']
                elif arriba:                             tipo, px = 'SEN', c[i]
            if tipo:
                bruto = (px-pos['entry'])/pos['entry'] if pos['side']=='long' else (pos['entry']-px)/pos['entry']
                neto = bruto - 2*FEE_LADO
                trades.append((neto, neto/(pos['r_pct']), tipo))
                pos = None

        if pos is None and atrs[i] is not None:
            if arriba:
                rd = sl_mult*atrs[i]
                pos = {'side':'long', 'entry':c[i], 'sl':c[i]-rd, 'tp':c[i]+tp_mult*rd, 'r_pct':rd/c[i]}
            elif abajo:
                rd = sl_mult*atrs[i]
                pos = {'side':'short', 'entry':c[i], 'sl':c[i]+rd, 'tp':c[i]-tp_mult*rd, 'r_pct':rd/c[i]}

    if pos:
        bruto = (c[-1]-pos['entry'])/pos['entry'] if pos['side']=='long' else (pos['entry']-c[-1])/pos['entry']
        neto = bruto - 2*FEE_LADO
        trades.append((neto, neto/(pos['r_pct']), 'FIN'))
    return trades

def stats(trades):
    n = len(trades)
    g = sum(1 for t in trades if t[0] > 0)
    p = sum(1 for t in trades if t[0] < 0)
    tp = sum(1 for t in trades if t[2]=='TP')
    sl = sum(1 for t in trades if t[2]=='SL')
    sen = sum(1 for t in trades if t[2] in ('SEN','FIN'))
    pnl = sum(t[0] for t in trades)
    pnlR = sum(t[1] for t in trades)
    racha = mx = 0
    for t in trades:
        racha = racha+1 if t[0] < 0 else 0
        mx = max(mx, racha)
    return n, g, p, tp, sl, sen, pnl, pnlR, mx

print("="*78)
print(f"BT SISTEMA COMPLETO · CRUCE MA × SL({SL_MULT}×ATR14) × TP({TP_MULT}R) · fees {2*FEE_LADO*100:.2f}%/trade")
print("Ambigüedad intrabar → SL (peor caso) · PnL_R = unidades de riesgo (1R = 1% capital)")
print("="*78)

for instId, bars in OBJETIVOS:
    for bar in bars:
        print(f"\n### {instId} · vela {bar}")
        try:
            v = velas(instId, bar, 1500)
        except Exception as e:
            print(f"  ✗ descarga: {str(e)[:100]}"); continue
        if len(v) < 250:
            print(f"  ✗ velas insuficientes ({len(v)}) — omitido"); continue
        o = [x[0] for x in v]; h = [x[1] for x in v]
        l = [x[2] for x in v]; c = [x[3] for x in v]
        atrs = atr_series(h, l, c)
        print(f"  ✓ {len(v)} velas · precio actual {c[-1]:,.4g}\n")

        filas = []
        for f, s in COMBOS:
            mf, ms = sma(c, f), sma(c, s)
            tr = simular(c, h, l, mf, ms, atrs, SL_MULT, TP_MULT)
            filas.append((f, s, stats(tr)))
        filas.sort(key=lambda x: -x[2][7])   # por PnL_R

        print(f"  {'MA':<8}{'TR':>4}{'GAN':>5}{'PER':>5}{'%':>5}{'TP/SL/SEN':>12}{'PnL%':>10}{'PnL_R':>9}{'RACHA':>7}")
        print("  " + "-"*64)
        for f, s, (n,g,p,tp,sl,sen,pnl,pnlR,mx) in filas:
            pct = f"{g/(g+p)*100:.0f}%" if (g+p) else "—"
            print(f"  {f}/{s:<5}{n:>5}{g:>5}{p:>5}{pct:>5}"
                  f"{str(tp)+'/'+str(sl)+'/'+str(sen):>12}{pnl:>+10.4f}{pnlR:>+9.2f}{mx:>7}")

        mejor = filas[0]
        print(f"\n  SENSIBILIDAD TP (mejor combo {mejor[0]}/{mejor[1]}):")
        for tpm in TP_SENS:
            mf, ms = sma(c, mejor[0]), sma(c, mejor[1])
            tr = simular(c, h, l, mf, ms, atrs, SL_MULT, tpm)
            n,g,p,tp,sl,sen,pnl,pnlR,mx = stats(tr)
            pct = f"{g/(g+p)*100:.0f}%" if (g+p) else "—"
            print(f"    TP {tpm}R → trades={n:<4} win={pct:<5} PnL={pnl:+.4f}  R={pnlR:+.2f}  racha={mx}")

print("\n" + "="*78)
print("REGLA DE GESTIÓN si el sistema gobierna las entradas:")
print("  riesgo por trade = 1% del capital · tamaño = (capital×1%) ÷ dist_SL")
print("  PnL_R total ≈ % del capital ganado/perdido en la ventana (sin componer)")
print("="*78)
print("⚠ PASADO medido, no futuro prometido. El mejor combo por par sobreajusta;")
print("  lo robusto es lo positivo en VARIOS pares a la vez. Validar en vivo 2 semanas.")
