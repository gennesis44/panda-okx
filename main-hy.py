import os
import sys
import time
import datetime
import ccxt
from collections import defaultdict

# ════════════════════════════════════════════════════════════════
#  main-hy.py — Historial de futuros OKX (DOGE / FET / SUI / XLM)
#  Variables (inyectadas por main-hy.yml):
#    OKX_API_KEY      ← secret OKX_HY_KEY
#    OKX_SECRET_KEY   ← secret OKX_HY_PASS
#    OKX_HY_PASSWORD  ← secret OKX_HY_PASSWORD
# ════════════════════════════════════════════════════════════════

API_KEY    = os.environ.get('OKX_API_KEY', '')
SECRET_KEY = os.environ.get('OKX_SECRET_KEY', '')
PASSPHRASE = os.environ.get('OKX_HY_PASSWORD') or os.environ.get('OKX_PASSWORD') or ''

if not (API_KEY and SECRET_KEY and PASSPHRASE):
    sys.exit("ABORTADO: falta credencial (OKX_API_KEY / OKX_SECRET_KEY / OKX_HY_PASSWORD)")

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
    print("  50105 → passphrase incorrecta · 50111/50113 → API key inválida · 50114 → IP restringida")
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
    """Pagina un endpoint privado OKX hacia el pasado usando 'after'."""
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

def fetch_bills_all():
    """Bills recientes (7 días) + archivo (3 meses), deduplicados por billId."""
    recientes, e1 = paginar('privateGetAccountBills', 'billId', {}, max_pages=60)
    archivo,   e2 = paginar('privateGetAccountBillsArchive', 'billId', {}, max_pages=100)
    vistos, merged = set(), []
    for b in recientes + archivo:
        bid = b.get('billId')
        if bid in vistos:
            continue
        vistos.add(bid)
        merged.append(b)
    return merged, (e1 or e2)

try:
    candidatos = descubrir()

    # ── 1. ÓRDENES → mapa ordId → reduceOnly (fuente de verdad open/close en modo net) ──
    reduce_map = {}
    errores_ord = []
    for iid, it in candidatos.items():
        orders, err = paginar('privateGetTradeOrdersHistory', 'ordId',
                              {'instType': it, 'instId': iid}, max_pages=60)
        if err:
            errores_ord.append((iid, err))
        for o in orders:
            reduce_map[o['ordId']] = str(o.get('reduceOnly', '')).lower()

    # ── 2. FILLS clasificados vía reduceOnly ──
    S = defaultdict(lambda: {'fills': 0, 'clases': defaultdict(int), 'ord_entrada': set(),
                             'pnl': 0.0, 'fees_bills': 0.0, 'funding': 0.0, 'penal': 0.0})
    bills_otros = defaultdict(list)

    for iid, it in candidatos.items():
        fills, err = paginar('privateGetTradeFillsHistory', 'billId',
                             {'instType': it, 'instId': iid}, max_pages=60)
        if err:
            print(f"  ⚠ fills {iid}: {err}")
        for f in fills:
            s = S[iid]
            s['fills'] += 1
            side = f.get('side')
            ro = reduce_map.get(f.get('ordId'))
            if ro is not None:
                cls = ('OPEN ' if ro != 'true' else 'CLOSE ') + ('LONG' if side == 'buy' else 'SHORT')
            else:
                cls = 'SIN MARCA'
            s['clases'][cls] += 1
            if cls.startswith('OPEN'):
                s['ord_entrada'].add(f.get('ordId'))
            if f.get('fillPnl'):
                s['pnl'] += float(f['fillPnl'])

    # ── 3. BILLS (7 días + 3 meses): fees, funding, penalización ──
    bills, err_b = fetch_bills_all()
    if err_b:
        print("  ⚠ bills:", err_b)
    for b in bills:
        inst = b.get('instId') or ''
        fee  = float(b.get('fee') or 0)
        if inst in candidatos:
            s = S[inst]
            t = b.get('type')
            if t == '2':   s['fees_bills'] += -fee
            elif t == '8': s['funding']    += -fee
            elif t == '9': s['penal']      += -fee
        elif inst.split('-')[0].upper() in BASES:
            bills_otros[inst].append(b)

    # ══ REPORTE ══
    print(f"\n{'INSTRUMENTO':<26}{'FILLS':>6}{'ENTRADAS':>9}{'(L/S)':>9}{'CIERRES':>9}"
          f"{'FUNDING':>10}{'PEN.LIQ':>9}{'PnL':>12}{'COMIS.':>10}")
    print("-" * 100)
    tot_ent = 0
    for base in sorted(BASES):
        filas = sorted([i for i in S if i.split('-')[0].upper() == base])
        if not filas:
            print(f"{base + ' (sin actividad)':<26}")
            continue
        for iid in filas:
            s = S[iid]
            ol = s['clases'].get('OPEN LONG', 0)
            os_ = s['clases'].get('OPEN SHORT', 0)
            cl = s['clases'].get('CLOSE LONG', 0) + s['clases'].get('CLOSE SHORT', 0)
            sm = s['clases'].get('SIN MARCA', 0)
            ent = len(s['ord_entrada'])
            tot_ent += ent
            extra = f" [{sm} sin marca]" if sm else ""
            print(f"{iid:<26}{s['fills']:>6}{ent:>9}({ol:>3}/{os_:<3}){cl:>9}"
                  f"{s['funding']:>10.4f}{s['penal']:>9.4f}{s['pnl']:>+12.4f}{s['fees_bills']:>10.4f}{extra}")

    print("-" * 100)
    print(f"ENTRADAS TOTALES (órdenes de apertura únicas): {tot_ent}")

    if errores_ord:
        print("\n=== AVISOS ORDERS-HISTORY (afecta conteo de entradas) ===")
        for iid, e in errores_ord:
            print(f"  {iid}: {e}")

    if bills_otros:
        print("\n=== BILLS FUERA DEL LISTADO (identificación) ===")
        for inst, bs in sorted(bills_otros.items()):
            print(f"  {inst}: {len(bs)} bill(s)")
            for b in bs[:5]:
                ts = datetime.datetime.fromtimestamp(int(b['ts'])/1000, tz=datetime.timezone.utc)
                print(f"     tipo={b.get('type')} subType={b.get('subType','')} "
                      f"fee={b.get('fee')} {b.get('ccy','')} · {ts:%Y-%m-%d}")

except Exception as e:
    print(f"Error: {e}")
