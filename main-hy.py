import os
import sys
import time
import ccxt
from collections import defaultdict

API_KEY    = os.environ.get('OKX_API_KEY', '')
SECRET_KEY = os.environ.get('OKX_SECRET_KEY', '')
PASSPHRASE = os.environ.get('OKX_HY_PASSWORD') or os.environ.get('OKX_PASSWORD') or ''

if not (API_KEY and SECRET_KEY and PASSPHRASE):
    sys.exit("ABORTADO: falta credencial")

exchange = ccxt.okx({
    'apiKey':    API_KEY,
    'secret':    SECRET_KEY,
    'password':  PASSPHRASE,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': 'https://my.okx.com'}},
})

print("== TEST DE AUTENTICACIÓN ==")
try:
    cfg = exchange.privateGetAccountConfig()
    print("  ✓ OK · UID:", cfg['data'][0].get('uid', '?'))
except Exception as e:
    print("  ✗ FALLO:", str(e)[:300])
    sys.exit(1)

BASES = {'DOGE', 'FET', 'SUI', 'XLM'}

def descubrir():
    exchange.load_markets()
    candidatos = {}
    for m in exchange.markets.values():
        if (m.get('swap') or m.get('future')) and m['base'].upper() in BASES:
            candidatos[m['id']] = m.get('info', {}).get('instType', 'SWAP')
    print("\n=== INSTRUMENTOS DETECTADOS ===")
    for iid in sorted(candidatos):
        print(f"  {iid:<26} instType={candidatos[iid]}")
    return candidatos

def paginar(endpoint, key, extra_params, max_pages=100):
    out, after, err = [], None, None
    for _ in range(max_pages):
        params = dict(extra_params)
        params['limit'] = '100'
        if after:
            params['after'] = after
        try:
            data = getattr(exchange, endpoint)(params).get('data', [])
        except Exception as e:
            err = str(e)[:150]
            break
        if not data:
            break
        out.extend(data)
        after = data[-1][key]
        if len(data) < 100:
            break
        time.sleep(0.25)
    return out, err

try:
    candidatos = descubrir()

    # ── ÓRDENES → mapa ordId → reduceOnly ──
    reduce_map = {}
    for iid, it in candidatos.items():
        orders, err = paginar('privateGetTradeOrdersHistory', 'ordId',
                              {'instType': it, 'instId': iid})
        for o in orders:
            reduce_map[o['ordId']] = str(o.get('reduceOnly', '')).lower()

    # ── FILLS ──
    S = defaultdict(lambda: {'fills': 0, 'ord_entrada': set(), 'pnl': 0.0,
                             'fees_bills': 0.0, 'funding': 0.0, 'penal': 0.0,
                             'trades': defaultdict(float)})   # ordId cierre → PnL del trade

    for iid, it in candidatos.items():
        fills, err = paginar('privateGetTradeFillsHistory', 'billId',
                             {'instType': it, 'instId': iid})
        if err:
            print(f"  ⚠ fills {iid}: {err}")
        for f in fills:
            s = S[iid]
            s['fills'] += 1
            side = f.get('side')
            ro = reduce_map.get(f.get('ordId'))
            es_apertura = (ro != 'true') if ro is not None else None
            if es_apertura:
                s['ord_entrada'].add(f.get('ordId'))
            pnl = float(f['fillPnl']) if f.get('fillPnl') else 0.0
            s['pnl'] += pnl
            # cada ORDEN de cierre = 1 trade terminado; acumula su PnL
            if ro == 'true' and f.get('ordId'):
                s['trades'][f['ordId']] += pnl

    # ── BILLS: comisiones, funding, liquidación ──
    bills, err_b = paginar('privateGetAccountBills', 'billId', {}, max_pages=60)
    arch,  err_a = paginar('privateGetAccountBillsArchive', 'billId', {}, max_pages=100)
    vistos = set()
    for b in bills + arch:
        bid = b.get('billId')
        if bid in vistos:
            continue
        vistos.add(bid)
        inst = b.get('instId') or ''
        fee  = float(b.get('fee') or 0)
        if inst in S:
            t = b.get('type')
            if t == '2':   S[inst]['fees_bills'] += -fee
            elif t == '8': S[inst]['funding']    += -fee
            elif t == '9': S[inst]['penal']      += -fee

    # ══ REPORTE DETALLADO ══
    print(f"\n{'INSTRUMENTO':<26}{'FILLS':>6}{'ENTRADAS':>9}{'PnL':>12}{'COMIS.':>10}")
    print("-" * 66)
    for base in sorted(BASES):
        filas = sorted([i for i in S if i.split('-')[0].upper() == base])
        if not filas:
            print(f"{base + ' (sin actividad)':<26}")
            continue
        for iid in filas:
            s = S[iid]
            print(f"{iid:<26}{s['fills']:>6}{len(s['ord_entrada']):>9}"
                  f"{s['pnl']:>+12.4f}{s['fees_bills']:>10.4f}")

    # ══ TABLA SOLICITADA: PAR · GANADA · PERDIDA · % ══
    print("\nPAR --------- GANADA -------- PERDIDA -------- % ------- PnL_NETO")
    print("-" * 66)
    tg = tp = 0
    tpnl = 0.0
    for base in sorted(BASES):
        insts = [i for i in S if i.split('-')[0].upper() == base]
        g = p = 0
        pnl_par = 0.0
        for i in insts:
            pnl_par += S[i]['pnl']
            for tp_ in S[i]['trades'].values():
                if tp_ > 0:  g += 1
                elif tp_ < 0: p += 1
        total = g + p
        pct = f"{(g / total * 100):.0f}%" if total else "—"
        pnl_str = f"{pnl_par:+.4f}" if insts else "—"
        print(f"{base:<10}{g:>11}{p:>16}{pct:>11}{pnl_str:>13}")
        tg += g; tp += p; tpnl += pnl_par if insts else 0.0
    print("-" * 66)
    tt = tg + tp
    tpct = f"{(tg / tt * 100):.0f}%" if tt else "—"
    print(f"{'TOTAL':<10}{tg:>11}{tp:>16}{tpct:>11}{tpnl:>+13.4f}")

except Exception as e:
    print(f"Error: {e}")
