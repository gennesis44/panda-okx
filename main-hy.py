import os
import time
import ccxt
from collections import defaultdict

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_HY_PASSWORD', ''),
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': 'https://my.okx.com'}},
})

BASES = {'DOGE', 'FET', 'SUI', 'XLM'}

SUBTYPE = {
    '1': 'COMPRA', '2': 'VENTA',
    '3': 'OPEN LONG',   '4': 'OPEN SHORT',
    '5': 'CLOSE LONG',  '6': 'CLOSE SHORT',
    '100': 'LIQ PARCIAL', '101': 'LIQ TOTAL', '102': 'ADL',
}

# ══ PASO 1 — DESCUBRIR TODAS LAS VARIANTES DE TUS 4 ACTIVOS ══
def descubrir():
    exchange.load_markets()
    candidatos = []
    for m in exchange.markets.values():
        if (m.get('swap') or m.get('future')) and m['base'].upper() in BASES:
            candidatos.append(m)
    print("=== INSTRUMENTOS DETECTADOS PARA TUS 4 ACTIVOS ===")
    for m in sorted(candidatos, key=lambda x: x['id']):
        it = m.get('info', {}).get('instType', '?')
        print(f"  {m['id']:<24} instType={it:<6} margen={m.get('settle')}")
    print()
    return candidatos

# ══ PASO 2 — FILLS POR INSTRUMENTO (con aviso si el tipo no está soportado) ══
def fetch_fills(instType, instId, max_pages=60):
    out, after, error = [], None, None
    for _ in range(max_pages):
        params = {'instType': instType, 'instId': instId, 'limit': '100'}
        if after:
            params['after'] = after
        try:
            data = exchange.privateGetTradeFillsHistory(params).get('data', [])
        except Exception as e:
            error = str(e)
            break
        if not data:
            break
        out.extend(data)
        after = data[-1]['billId']
        if len(data) < 100:
            break
        time.sleep(0.25)
    return out, error

# ══ PASO 3 — BILLS: vía alternativa universal (fills + funding + liq) ══
def fetch_bills(max_pages=100):
    out, after = [], None
    for _ in range(max_pages):
        params = {'limit': '100'}
        if after:
            params['after'] = after
        try:
            data = exchange.privateGetAccountBills(params).get('data', [])
        except Exception:
            break
        if not data:
            break
        out.extend(data)
        after = data[-1]['billId']
        if len(data) < 100:
            break
        time.sleep(0.25)
    return out

def clasificar(f):
    st = str(f.get('subType') or '')
    if st in SUBTYPE:
        return SUBTYPE[st]
    side, pos = f.get('side'), f.get('posSide', 'net')
    if pos == 'long':  return 'CLOSE LONG' if side == 'sell' else 'OPEN LONG'
    if pos == 'short': return 'OPEN SHORT' if side == 'sell' else 'CLOSE SHORT'
    return 'SIN MARCA'

try:
    candidatos = descubrir()

    S = defaultdict(lambda: {
        'fills': 0, 'clases': defaultdict(int), 'ord_entrada': set(),
        'pnl': 0.0, 'fees': 0.0, 'rebates': 0.0, 'maker': 0, 'taker': 0,
        'funding': 0.0, 'penal': 0.0, 'fills_bills': 0,
    })

    errores = []
    for m in candidatos:
        instType = m.get('info', {}).get('instType', 'SWAP')
        fills, err = fetch_fills(instType, m['id'])
        if err:
            errores.append((m['id'], instType, err))
        for f in fills:
            s = S[m['id']]
            s['fills'] += 1
            cls = clasificar(f)
            s['clases'][cls] += 1
            if cls in ('OPEN LONG', 'OPEN SHORT'):
                s['ord_entrada'].add(f.get('ordId'))
            if f.get('fillPnl'):
                s['pnl'] += float(f['fillPnl'])
            fee = float(f.get('fillFee') or 0)
            if fee < 0: s['fees']   += -fee
            else:       s['rebates'] += fee
            if f.get('execType') == 'M': s['maker'] += 1
            elif f.get('execType') == 'T': s['taker'] += 1

    # ── Bills: complemento universal (cuenta fills, funding y penalizaciones) ──
    bills = fetch_bills()
    for b in bills:
        inst = b.get('instId')
        if not inst or inst.split('-')[0].upper() not in BASES:
            continue
        s = S[inst]
        t = b.get('type'); fee = float(b.get('fee') or 0)
        if t == '2':   s['fills_bills'] += 1        # trade bill = un fill
        elif t == '8': s['funding'] += -fee          # funding fee (pagado +/recibido -)
        elif t == '9': s['penal'] += -fee            # penalización por liquidación

    # ══ REPORTE ══
    print(f"{'INSTRUMENTO':<24}{'FILLS':>6}{'BILLS':>7}{'ENTRADAS':>9}{'(L/S)':>9}"
          f"{'FUNDING':>10}{'PEN.LIQ':>9}{'PnL':>12}{'COMIS.':>9}")
    print("-" * 95)

    tot_ent = 0
    for base in sorted(BASES):
        filas = sorted([i for i in S if i.split('-')[0].upper() == base])
        if not filas:
            print(f"{base + ' (sin actividad)':<24}")
            continue
        for inst in filas:
            s = S[inst]
            ol = s['clases'].get('OPEN LONG', 0)
            os_ = s['clases'].get('OPEN SHORT', 0)
            ent = len(s['ord_entrada'])
            net_fee = s['fees'] - s['rebates']
            tot_ent += ent
            print(f"{inst:<24}{s['fills']:>6}{s['fills_bills']:>7}{ent:>9}"
                  f"({ol:>3}/{os_:<3}){s['funding']:>10.4f}{s['penal']:>9.4f}"
                  f"{s['pnl']:>+12.4f}{net_fee:>9.4f}")

    print("-" * 95)
    print(f"ENTRADAS TOTALES (órdenes de apertura únicas): {tot_ent}")
    print("FILLS vs BILLS: si FILLS=0 pero BILLS>0 → ese instrumento es X-Perp y")
    print("su historial de fills no está en fills-history; los bills sí lo ven.")
    print("NOTA: PnL/fees/funding en la moneda de margen de cada instrumento.")

    if errores:
        print("\n=== AVISOS DE ENDPOINT ===")
        for inst, it, e in errores:
            print(f"  {inst} [{it}]: {e[:120]}")

except Exception as e:
    print(f"Error consultando el historial: {e}")
