import time
import datetime
import ccxt

# ════════════════════════════════════════════════════════════════
#  senal-hy.py v-LIVE — DETONANTES MA × filtro MA200 · v4H cerrada
#  D1: MA10×MA25   D2: MA25×MA50   D3: MA50×MA200 (evento)   D4: MA100×MA200 (evento)
#  Filtro: solo trades en dirección del régimen MA200
#  SL 1% · TP 2% (2R) · riesgo 1% del capital
#  EJECUTABLES: DOGE/SUI/XLM X-Perp · BTC = RADAR (mín. contrato ~$768)
# ════════════════════════════════════════════════════════════════

CAPITAL = 55.0        # ← EDITAR tras convertir todo a USDT (saldo real)
RIESGO  = 0.01        # 1% del capital por trade
SL_PCT  = 0.01        # SL 1%
TP_PCT  = 0.02        # TP 2% (2R)

# (instId, ejecutable?, min_notional_aprox)
PARES = [
    ('DOGE-USD_UM_XPERP-310404', True,  5),
    ('SUI-USD_UM_XPERP-310404',  True,  5),
    ('XLM-USD_UM_XPERP-310704',  True,  5),
    ('BTC-USDT-SWAP',            False, 768),   # radar: no ejecutable con este capital
]

MAYOR, MENOR = 'MAYOR', 'MENOR'

exchange = ccxt.okx({'urls': {'api': {'rest': 'https://my.okx.com'}}})

def velas(instId, total=260):
    out, after = [], None
    while len(out) < total:
        params = {'instId': instId, 'bar': '4H', 'limit': '100'}
        if after: params['after'] = after
        data = exchange.publicGetMarketHistoryCandles(params).get('data', [])
        if not data: break
        out.extend(data)
        after = data[-1][0]
        if len(data) < 100: break
        time.sleep(0.15)
    out.reverse()
    return [float(r[4]) for r in out]

def sma(v, n):
    out = [None]*len(v); acc = 0.0
    for i, x in enumerate(v):
        acc += x
        if i >= n: acc -= v[i-n]
        if i >= n-1: out[i] = acc/n
    return out

def cruce_nuevo(a_prev, b_prev, a, b):
    if None in (a_prev, b_prev, a, b): return None
    if a_prev <= b_prev and a > b: return 'ALCISTA'
    if a_prev >= b_prev and a < b: return 'BAJISTA'
    return None

print("="*76)
print(f"SISTEMA v-LIVE · v4H CERRADA · SL {SL_PCT*100:.0f}% · TP {TP_PCT*100:.0f}% · riesgo {RIESGO*100:.0f}% de ${CAPITAL:,.2f}")
print("="*76)

for instId, ejecutable, min_not in PARES:
    try:
        c = velas(instId)
    except Exception as e:
        print(f"\n▌ {instId}: ✗ {str(e)[:80]}"); continue
    if len(c) < 220:
        print(f"\n▌ {instId}: ✗ velas insuficientes"); continue

    m10, m25, m50, m100, m200 = sma(c,10), sma(c,25), sma(c,50), sma(c,100), sma(c,200)
    i = len(c) - 1
    precio = c[i]
    regimen = 'ALCISTA' if precio > m200[i] else 'BAJISTA'

    print(f"\n▌ {instId} · última vela 4H cerrada · close {precio:,.4g}")
    print(f"  MA10={m10[i]:,.4g}  MA25={m25[i]:,.4g}  MA50={m50[i]:,.4g}  MA100={m100[i]:,.4g}  MA200={m200[i]:,.4g}")
    print(f"  RÉGIMEN (juez MA200): {regimen}")

    detonantes = []
    for nombre, (f_id, s_id, tipo) in {
        'D1': (m10,  m25,  MENOR),
        'D2': (m25,  m50,  MENOR),
        'D3': (m50,  m200, MAYOR),
        'D4': (m100, m200, MAYOR),
    }.items():
        dir_ = cruce_nuevo(f_id[i-1], s_id[i-1], f_id[i], s_id[i])
        if dir_:
            valido = (dir_ == regimen) or (tipo == MAYOR)
            detonantes.append((nombre, tipo, dir_, valido))

    if not detonantes:
        print("  Sin cruces nuevos en esta vela. (Estado vigente no es señal.)")
    else:
        for nombre, tipo, dir_, valido in detonantes:
            if not valido:
                estado = '— contraria al régimen, VETADO'
            elif not ejecutable:
                estado = '— RADAR (BTC: no ejecutable con capital actual)'
            elif tipo == MAYOR:
                estado = '★ EVENTO RÉGIMEN'
            else:
                estado = '★ DETONANTE'
            print(f"  {nombre} ({'mayor' if tipo==MAYOR else 'menor'}): cruza {dir_} → {estado}")
            if valido and dir_ == regimen:
                lado = 'LONG' if dir_ == 'ALCISTA' else 'SHORT'
                sl   = precio * (1 - SL_PCT) if lado == 'LONG' else precio * (1 + SL_PCT)
                tp   = precio * (1 + TP_PCT) if lado == 'LONG' else precio * (1 - TP_PCT)
                riesgo_usd = CAPITAL * RIESGO
                notional   = riesgo_usd / SL_PCT
                unidades   = notional / precio
                print(f"      → {lado} · entrada≈{precio:,.4g} · SL={sl:,.4g} · TP={tp:,.4g}")
                print(f"      → notional ${notional:,.2f} ≈ {unidades:,.4g} {instId.split('-')[0]}"
                      f" · riesgo ${riesgo_usd:.2f}")
                if ejecutable and notional < min_not:
                    print(f"      ⚠ notional bajo mínimo del contrato (~${min_not}) — verificar en app")
    print("  " + "-"*64)

print("\nREGLAS: solo cruce NUEVO en vela CERRADA · dirección = régimen MA200 ·")
print("        D3/D4 son eventos de régimen (TP puede ampliarse a 3R manual)")
print("⚠ Señales en vela cerrada. Ejecución manual en app. Pasado ≠ futuro.")
