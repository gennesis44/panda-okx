import time
import ccxt

# ════════════════════════════════════════════════════════════════
#  ema-hy.py — Backtester EMA sobre datos REALES de OKX (público)
#  Sin claves: solo endpoints públicos. Vela 4H, ~1500 velas/par.
#  Coste por trade: 0.05% taker por lado (0.10% ida y vuelta).
# ════════════════════════════════════════════════════════════════

TF_BAR       = '4H'
N_VELAS      = 1500
FEE_POR_LADO = 0.0005     # 0.05% taker OKX futuros

PARES = [
    'DOGE-USD_UM_XPERP-310404',
    'SUI-USD_UM_XPERP-310404',
    'XLM-USD_UM_XPERP-310704',
    'FET-USD_UM_XPERP-310912',
]

COMBOS = [
    (5, 10), (5, 20), (7, 21), (8, 21), (9, 21), (10, 21),
    (10, 30), (12, 26), (20, 50), (50, 100), (50, 200),
]

exchange = ccxt.okx({'urls': {'api': {'rest': 'https://my.okx.com'}}})

def velas(instId, total):
    """Descarga 'total' velas de un instrumento (endpoint público, paginado)."""
    out, after = [], None
    while len(out) < total:
        params = {'instId': instId, 'bar': TF_BAR, 'limit': '100'}
        if after:
            params['after'] = after
        data = exchange.publicGetMarketHistoryCandles(params).get('data', [])
        if not data:
            break
        out.extend(data)
        after = data[-1][0]
        if len(data) < 100:
            break
        time.sleep(0.15)
    out.reverse()   # → orden ascendente por tiempo
    return [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])] for r in out]

def ema(values, n):
    if len(values) < n:
        return [None] * len(values)
    out = [None] * (n - 1)
    seed = sum(values[:n]) / n
    out.append(seed)
    k = 2 / (n + 1)
    prev = seed
    for v in values[n:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out

def simular(closes, f, s):
    """Cruce EMA: long al cruce alcista, short al bajista. Siempre en mercado.
    Ejecución a close de la vela de confirmación. Fees incluidos."""
    ef, es = ema(closes, f), ema(closes, s)
    trades = []
    pos = None
    for i in range(1, len(closes)):
        if ef[i] is None or es[i] is None or ef[i-1] is None or es[i-1] is None:
            continue
        cruza_arriba = ef[i-1] <= es[i-1] and ef[i] > es[i]
        cruza_abajo  = ef[i-1] >= es[i-1] and ef[i] < es[i]
        if pos is None and cruza_arriba:
            pos = ('long', closes[i])
        elif pos is None and cruza_abajo:
            pos = ('short', closes[i])
        elif pos and pos[0] == 'long' and cruza_abajo:
            trades.append((pos[1], closes[i], 'long'))
            pos = ('short', closes[i])
        elif pos and pos[0] == 'short' and cruza_arriba:
            trades.append((pos[1], closes[i], 'short'))
            pos = ('long', closes[i])
    if pos:
        trades.append((pos[1], closes[-1], pos[0]))
    # PnL neto % por trade (fees ida y vuelta)
    res = []
    for entrada, salida, side in trades:
        bruto = (salida - entrada) / entrada if side == 'long' else (entrada - salida) / entrada
        res.append(bruto - 2 * FEE_POR_LADO)
    return res

print("=" * 72)
print(f"BACKTESTER EMA · velas {TF_BAR} reales de OKX · coste {2*FEE_POR_LADO*100:.2f}%/trade")
print("=" * 72)

resumen_global = {}
for instId in PARES:
    print(f"\n### {instId} — descargando velas…")
    try:
        v = velas(instId, N_VELAS)
    except Exception as e:
        print(f"  ✗ error descargando: {str(e)[:120]}")
        continue
    if len(v) < 250:
        print(f"  ✗ velas insuficientes ({len(v)}) — par omitido")
        continue
    closes = [x[4] for x in v]
    print(f"  ✓ {len(v)} velas")
    bh = (closes[-1] - closes[0]) / closes[0] - 2 * FEE_POR_LADO   # buy & hold neto

    filas = []
    for f, s in COMBOS:
        res = simular(closes, f, s)
        g = sum(1 for r in res if r > 0)
        p = sum(1 for r in res if r < 0)
        tot_pnl = sum(res)
        filas.append((f, s, len(res), g, p, tot_pnl))

    filas.sort(key=lambda x: -x[5])
    print(f"\n  EMA       TRADES  GANADA  PERDIDA   %      PnL_NETO    2x_NETO")
    print("  " + "-" * 66)
    for f, s, n, g, p, pnl in filas:
        pct = f"{g/(g+p)*100:.0f}%" if (g + p) else "—"
        print(f"  {f}/{s:<6} {n:>6} {g:>7} {p:>8}  {pct:>5} {pnl:>+11.4f} {pnl*2:>+11.4f}")
    mejor = filas[0]
    print(f"  MEJOR: EMA{mejor[0]}/{mejor[1]} → {mejor[5]:+.4f} neto  |  Buy&Hold neto: {bh:+.4f}")
    if mejor[5] > bh:
        print("  → La mejor EMA BATE al buy&hold en esta ventana.")
    else:
        print("  → Ninguna EMA bate al buy&hold en esta ventana. Dato, no opinión.")
    resumen_global[instId] = (mejor, bh)

print("\n" + "=" * 72)
print("RESUMEN — mejor combinación por par")
print("=" * 72)
for instId, ((f, s, n, g, p, pnl), bh) in resumen_global.items():
    base = instId.split('-')[0]
    pct = f"{g/(g+p)*100:.0f}%" if (g + p) else "—"
    print(f"  {base:<6} EMA{f}/{s:<4} trades={n:<4} win={pct:<4} neto={pnl:+.4f}  2x={pnl*2:+.4f}  (BH={bh:+.4f})")

print("\n⚠ ADVERTENCIA FALSABLE: esto mide el PASADO, no predice el futuro.")
print("  El mejor combo histórico tiende a sobreajustar (overfitting).")
print("  Úsalo como mapa de régimen, no como promesa.")
