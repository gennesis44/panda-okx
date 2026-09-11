import time
import ccxt

# ════════════════════════════════════════════════════════════════
#  btc-ma.py — BTC futuros OKX · cruces MA (SMA) · backtest + señal
#  Público, sin claves. Schedule: cada 4h en ema workflow o manual.
# ════════════════════════════════════════════════════════════════

FEE_POR_LADO = 0.0005
COMBOS = [
    (5, 10), (5, 20), (7, 21), (8, 21), (9, 21), (10, 21),
    (10, 30), (12, 26), (20, 50), (50, 100), (50, 200),
]

exchange = ccxt.okx({'urls': {'api': {'rest': 'https://my.okx.com'}}})

def elegir_btc():
    """Descubre perp de BTC en OKX y devuelve el de mayor volumen 24h."""
    exchange.load_markets()
    candidatos = [m for m in exchange.markets.values()
                  if m.get('swap') and m.get('base') == 'BTC' and m.get('active')]
    if not candidatos:
        raise SystemExit("No hay perp BTC activo en OKX")
    mejor, mejor_vol = None, -1
    for m in candidatos:
        try:
            t = exchange.fetch_ticker(m['id'])
            vol = float(t.get('quoteVolume') or 0)
        except Exception:
            vol = 0
        print(f"  candidato: {m['id']:<26} vol24h={vol:,.0f}")
        if vol > mejor_vol:
            mejor, mejor_vol = m['id'], vol
    print(f"  → ELEGIDO: {mejor} (mayor liquidez)\n")
    return mejor

def velas(instId, bar, total):
    out, after = [], None
    while len(out) < total:
        params = {'instId': instId, 'bar': bar, 'limit': '100'}
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
    out.reverse()
    return [float(r[4]) for r in out]

def sma(values, n):
    out = [None] * len(values)
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= n:
            acc -= values[i - n]
        if i >= n - 1:
            out[i] = acc / n
    return out

def simular(closes, f, s):
    mf, ms = sma(closes, f), sma(closes, s)
    trades, pos = [], None
    for i in range(1, len(closes)):
        if mf[i] is None or ms[i] is None or mf[i-1] is None or ms[i-1] is None:
            continue
        arriba = mf[i-1] <= ms[i-1] and mf[i] > ms[i]
        abajo  = mf[i-1] >= ms[i-1] and mf[i] < ms[i]
        if pos is None and arriba:
            pos = ('long', closes[i])
        elif pos is None and abajo:
            pos = ('short', closes[i])
        elif pos and pos[0] == 'long' and abajo:
            trades.append((pos[1], closes[i], 'long'))
            pos = ('short', closes[i])
        elif pos and pos[0] == 'short' and arriba:
            trades.append((pos[1], closes[i], 'short'))
            pos = ('long', closes[i])
    if pos:
        trades.append((pos[1], closes[-1], pos[0]))
    res = []
    for e, x, side in trades:
        bruto = (x - e) / e if side == 'long' else (e - x) / e
        res.append(bruto - 2 * FEE_POR_LADO)
    return res

def senal_actual(closes, f, s):
    mf, ms = sma(closes, f), sma(closes, s)
    i = len(closes) - 1
    if mf[i] is None or ms[i] is None:
        return '—', 0
    estado = 'LONG' if mf[i] > ms[i] else 'SHORT'
    edad = 0
    j = i
    while j > 0 and mf[j] is not None and ms[j] is not None:
        e_j = 'LONG' if mf[j] > ms[j] else 'SHORT'
        if e_j != estado:
            break
        edad += 1
        j -= 1
    return estado, edad

instId = elegir_btc()

for bar, n_velas in [('4H', 1500), ('1H', 1200)]:
    print("=" * 74)
    print(f"BTC · MA (SMA) cruces · vela {bar} · {n_velas} velas")
    print("=" * 74)
    try:
        closes = velas(instId, bar, n_velas)
    except Exception as e:
        print("  ✗ error velas:", str(e)[:120])
        continue
    if len(closes) < 250:
        print(f"  ✗ velas insuficientes ({len(closes)})")
        continue
    print(f"  ✓ {len(closes)} velas · precio actual: {closes[-1]:,.1f}\n")

    filas, senales = [], []
    for f, s in COMBOS:
        res = simular(closes, f, s)
        g = sum(1 for r in res if r > 0)
        p = sum(1 for r in res if r < 0)
        pnl = sum(res)
        filas.append((f, s, len(res), g, p, pnl))
        senales.append((f, s) + senal_actual(closes, f, s))

    filas.sort(key=lambda x: -x[5])
    print(f"  MA        TRADES  GANADA  PERDIDA   %      PnL_NETO    2x_NETO")
    print("  " + "-" * 68)
    for f, s, n, g, p, pnl in filas:
        pct = f"{g/(g+p)*100:.0f}%" if (g + p) else "—"
        print(f"  {f}/{s:<6} {n:>6} {g:>7} {p:>8}  {pct:>5} {pnl:>+11.4f} {pnl*2:>+11.4f}")

    print(f"\n  SEÑALES ACTUALES (última vela cerrada {bar}):")
    print(f"  {'MA':<10}{'SEÑAL':<8}{'VELAS DESDE CRUCE'}")
    longs = shorts = 0
    for f, s, est, edad in senales:
        if est == 'LONG':  longs += 1
        elif est == 'SHORT': shorts += 1
        print(f"  {f}/{s:<7}{est:<8}{edad}")
    print(f"\n  CONSENSO {bar}: LONG={longs}  SHORT={shorts}  —",
          "BAJISTA" if shorts > longs else "ALCISTA" if longs > shorts else "NEUTRO")

print("\n⚠ PASADO medido, futuro no prometido. Señal = estado del cruce, no garantía.")
