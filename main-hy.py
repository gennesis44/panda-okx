import os
import sys
import time
import ccxt
from collections import defaultdict

# ── 1. CREDENCIALES: passphrase alineada con el secret OKX_HY_PASSWORD ──
API_KEY    = os.environ.get('OKX_API_KEY', '')
SECRET_KEY = os.environ.get('OKX_SECRET_KEY', '')
PASSPHRASE = os.environ.get('OKX_HY_PASSWORD') or os.environ.get('OKX_PASSWORD') or ''

def bordes_sospechosos(v):
    """Detecta espacios o comillas pegadas por accidente (sin mostrar el valor)."""
    return v != v.strip() or v[:1] in ('"', "'") or v[-1:] in ('"', "'")

print("== DIAGNÓSTICO DE CREDENCIALES (solo longitudes, nunca valores) ==")
print(f"  OKX_API_KEY      presente (len={len(API_KEY)})"    if API_KEY    else "  OKX_API_KEY      VACÍA")
print(f"  OKX_SECRET_KEY   presente (len={len(SECRET_KEY)})" if SECRET_KEY else "  OKX_SECRET_KEY   VACÍA")
print(f"  PASSPHRASE       presente (len={len(PASSPHRASE)})" if PASSPHRASE else "  PASSPHRASE       VACÍA — revisa secret OKX_HY_PASSWORD y el env: del .yml")
if PASSPHRASE and bordes_sospechosos(PASSPHRASE):
    print("  ⚠ PASSPHRASE con espacios/comillas en los bordes — revisa cómo la pegaste en el secret")

if not (API_KEY and SECRET_KEY and PASSPHRASE):
    sys.exit("\nABORTADO: falta credencial. No se envió nada a OKX.")

# ── 2. EXCHANGE ──
exchange = ccxt.okx({
    'apiKey':    API_KEY,
    'secret':    SECRET_KEY,
    'password':  PASSPHRASE,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': 'https://my.okx.com'}},
})
exchange.apiKey   = API_KEY
exchange.secret   = SECRET_KEY
exchange.password = PASSPHRASE

# ── 3. TEST DE AUTENTICACIÓN ÚNICO ──
print("\n== TEST DE AUTENTICACIÓN (account/config) ==")
try:
    cfg = exchange.privateGetAccountConfig()
    print("  ✓ Autenticación OK · UID:", cfg['data'][0].get('uid', '?'))
except Exception as e:
    msg = str(e)
    print("  ✗ FALLO DE AUTENTICACIÓN:", msg[:300])
    print("\nGUÍA DE CÓDIGOS OKX:")
    print("  50111 → API key no existe (mal copiada en el secret)")
    print("  50112 → API key congelada (revisar en OKX app)")
    print("  50113 → Passphrase incorrecta (revisar secret OKX_HY_PASSWORD)")
    print("  50114 → IP restringida (quitar whitelist de IP en la clave)")
    sys.exit(1)

BASES = {'DOGE', 'FET', 'SUI', 'XLM'}

SUBTYPE = {
    '3': 'OPEN LONG',   '4': 'OPEN SHORT',
    '5': 'CLOSE LONG',  '6': 'CLOSE SHORT',
    '100': 'LIQ PARCIAL', '101': 'LIQ TOTAL', '102': 'ADL',
}

def descubrir():
    exchange.load_markets()
    candidatos = []
    for m in exchange.markets.values():
        if (m.get('swap') or m.get('future')) and m['base'].upper() in BASES:
            candidatos.append(m)
    print("\n=== INSTRUMENTOS DETECTADOS ===")
    for m in sorted(candidatos, key=lambda x: x['id']):
        it = m.get('info', {}).get('instType', '?')
        print(f"  {m['id']:<26} instType={it:<8} margen={m.get('settle')}")
    return candidatos

def fetch_fills(instType, instId, max_pages=60):
    out, after, error = [], None, None
    for _ in range(max_pages):
        params = {'instType': instType, 'instId': instId, 'limit': '100'}
        if after:
            params['after'] = after
        try:
            data = exchange.privateGetTradeFillsHistory(params).get('data', [])
        except Exception as e:
            error = str(e)[:200]
            break
        if not data:
            break
        out.extend(data)
        after = data[-1]['billId']
        if len(data) < 100:
            break
        time.sleep(0.25)
    return out, error

def fetch_bills(max_pages=100):
    out, after, error = [], None, None
    for _ in range(max_pages):
        params = {'limit': '100'}
        if after:
            params['after'] = after
        try:
            data = exchange.privateGetAccountBills(params).get('data', [])
        except Exception as e:
            error = str(e)[:200]
            break
        if not data:
            break
        out.extend(data)
        after = data[-1]['billId']
        if len(data) < 100:
            break
        time.sleep(0.25)
    return out, error

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

    bills, err_bills = fetch_bills()
    if err_bills:
        print("\n⚠ Bills también falló:", err_bills)
    for b in bills:
        inst = b.get('instId')
        if not inst or inst.split('-')[0].upper() not in BASES:
            continue
        s = S[inst]
        t = b.get('type'); fee = float(b.get('fee') or 0)
        if t == '2':   s['fills_bills'] += 1
        elif t == '8': s['funding'] += -fee
        elif t == '9': s['penal'] += -fee

    # ══ REPORTE ══
    print(f"\n{'INSTRUMENTO':<26}{'FILLS':>6}{'BILLS':>7}{'ENTRADAS':>9}{'(L/S)':>9}"
          f"{'FUNDING':>10}{'PEN.LIQ':>9}{'PnL':>12}{'COMIS.':>9}")
    print("-" * 97)

    tot_ent = 0
    for base in sorted(BASES):
        filas = sorted([i for i in S if i.split('-')[0].upper() == base])
        if not filas:
            print(f"{base + ' (sin actividad)':<26}")
            continue
        for inst in filas:
            s = S[inst]
            ol = s['clases'].get('OPEN LONG', 0)
            os_ = s['clases'].get('OPEN SHORT', 0)
            ent = len(s['ord_entrada'])
            net_fee = s['fees'] - s['rebates']
            tot_ent += ent
            print(f"{inst:<26}{s['fills']:>6}{s['fills_bills']:>7}{ent:>9}"
                  f"({ol:>3}/{os_:<3}){s['funding']:>10.4f}{s['penal']:>9.4f}"
                  f"{s['pnl']:>+12.4f}{net_fee:>9.4f}")

    print("-" * 97)
    print(f"ENTRADAS TOTALES (órdenes de apertura únicas): {tot_ent}")

    if errores:
        print("\n=== AVISOS DE ENDPOINT (por instrumento) ===")
        for inst, it, e in errores:
            print(f"  {inst} [{it}]: {e}")

except Exception as e:
    print(f"Error consultando el historial: {e}")
