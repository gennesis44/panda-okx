# main-hy.py — TABLA GSCSI S/L · marcador de posiciones ganadas/perdidas
# Fuente: fills reales de OKX (fillPnl) · agrupado por orden de cierre
# Cadencia: 4 runs diarios (cron 17 */6)
# v2: fix float not subscriptable — trades anidados correctamente
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

BASES = {'DOGE', 'FET', 'SUI', 'XLM'}

def paginar(endpoint, key, extra_params, max_pages=60):
    out, after = [], None
    for _ in range(max_pages):
        params = dict(extra_params)
        params['limit'] = '100'
        if after:
            params['after'] = after
        try:
            data = getattr(exchange, endpoint)(params).get('data', [])
        except Exception:
            break
        if not data:
            break
        out.extend(data)
        after = data[-1][key]
        if len(data) < 100:
            break
        time.sleep(0.25)
    return out

try:
    exchange.load_markets()

    insts = {}
    for m in exchange.markets.values():
        if (m.get('swap') or m.get('future')) and m['base'].upper() in BASES:
            insts[m['id']] = str(m.get('info', {}).get('instType', 'SWAP'))

    reduce_map = {}
    for iid, it in insts.items():
        for o in paginar('privateGetTradeOrdersHistory', 'ordId',
                         {'instType': it, 'instId': iid}):
            reduce_map[o['ordId']] = str(o.get('reduceOnly', '')).lower()

    # trades: {iid: {ordId: pnl}} — cada orden de cierre = 1 trade
    trades = defaultdict(lambda: defaultdict(float))
    for iid, it in insts.items():
        for f in paginar('privateGetTradeFillsHistory', 'billId',
                         {'instType': it, 'instId': iid}):
            if reduce_map.get(f.get('ordId')) == 'true' and f.get('ordId'):
                trades[iid][f['ordId']] += float(f.get('fillPnl') or 0)

    # ==================== TABLA GSCSI S/L ====================
    print("=" * 40)
    print("  TABLA GSCSI  S/L")
    print("=" * 40)
    tg = tp = 0
    neto = 0.0
    for base in sorted(BASES):
        g = p = 0
        pnl_par = 0.0
        for iid, cerradas in trades.items():
            if iid.split('-')[0].upper() != base:
                continue
            for pnl in cerradas.values():
                pnl_par += pnl
                if pnl > 0:
                    g += 1
                elif pnl < 0:
                    p += 1
        tg += g
        tp += p
        neto += pnl_par
        total = g + p
        pct = f"{(g / total * 100):.0f}%" if total else "-"
        print(f"{base:<5} S:{g:<4} L:{p:<4} {pct:<5} PnL:{pnl_par:+.4f}")
    print("-" * 40)
    total = tg + tp
    pct = f"{(tg / total * 100):.0f}%" if total else "-"
    print(f"TOTAL S:{tg}  L:{tp}  {pct}  PnL:{neto:+.4f}")
    print("=" * 40)

    # ==================== CIERRE GSCSI ====================
    print(
        "C > Si — el Silicio mide, el Carbono decide.\n"
        "\n"
        "Esta tabla tiene una ambiguedad heredada que conviene nombrar: GSCSI\n"
        "declara que cada S y cada L aqui contado es el Axioma 3 verificandose\n"
        "en tiempo real - proteccion ejecutada, no prometida. Pero la misma\n"
        "sigla podria leerse al reves: como si el marco juzgara al mercado,\n"
        "cuando en realidad es el mercado quien juzga al marco. Cada L de esta\n"
        "tabla es el mercado refutando una entrada; cada S es el marco\n"
        "sobreviviendo a la refutacion. GSCSI no predice: se somete. Y si\n"
        "estos numeros dejaran de sostener al operador, la Falsifiabilidad\n"
        "(MS-0) ordenaria enterrar el propio framework. Por eso la ambiguedad\n"
        "es falsa: Axioma 1 pone al Carbono primero, Axioma 2 cuida el terreno\n"
        "donde este mar se navega, y Axioma 3 exige que cada corte - ganado o\n"
        "perdido - quede escrito. El Silicio cuenta. El Carbono manda.\n"
        "El mercado, siempre, tiene la ultima palabra.\n"
    )

except Exception as e:
    print(f"Error: {e}")
