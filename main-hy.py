# main-hy.py — GSCSI TABLE · C > Si
# v5: fuente primaria = positions-history (PnL exacto por trade, ventana larga)
# +ADA en roster · fallback a fills si positions-history falla
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

BASES = {'DOGE', 'FET', 'SUI', 'XLM', 'ADA'}

NODO_NUCLEO  = 'https://1c3si.weebly.com/clo.html'
NODO_RAIZ    = 'https://1c3si.weebly.com'
AVATAR_URL   = os.environ.get('GSCSI_AVATAR_URL',
              'https://github.com/gennesis44.png')

def paginar(endpoint, key, extra_params, max_pages=20):
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

    # ── FUENTE PRIMARIA: positions-history (posiciones CERRADAS, PnL exacto) ──
    # Cada posición cerrada = 1 trade. Evidencia: cposId → pnl → dirección.
    trades = defaultdict(lambda: defaultdict(float))
    for iid, it in insts.items():
        pos_hist = paginar('privateGetAccountPositionsHistory', 'posId',
                           {'instType': it, 'instId': iid})
        for p in pos_hist:
            try:
                pnl = float(p.get('realizedPnl') or p.get('pnl') or 0)
            except (TypeError, ValueError):
                continue
            pos_id = p.get('posId')
            if pos_id and pnl != 0:
                trades[iid][pos_id] += pnl
            elif pos_id and pnl == 0:
                trades[iid][pos_id] += 0.0   # breakeven cuenta como trade cerrado

    # ── FALLBACK: si positions-history no entregó nada, usa fills (legacy) ──
    total_trades = sum(len(v) for v in trades.values())
    if total_trades == 0:
        reduce_map = {}
        for iid, it in insts.items():
            for o in paginar('privateGetTradeOrdersHistory', 'ordId',
                             {'instType': it, 'instId': iid}):
                reduce_map[o['ordId']] = str(o.get('reduceOnly', '')).lower()
        for iid, it in insts.items():
            for f in paginar('privateGetTradeFillsHistory', 'billId',
                             {'instType': it, 'instId': iid}):
                if reduce_map.get(f.get('ordId')) == 'true' and f.get('ordId'):
                    trades[iid][f['ordId']] += float(f.get('fillPnl') or 0)

    # ==================== ENCABEZADO ====================
    print("=" * 46)
    print("  TABLA GSCSI S/L")
    print("  actualizacion automatica cada 6 hrs")
    print("  fuente: positions-history (OKX)")
    print("=" * 46)
    print(f"  Operador : github.com/gennesis44")
    print(f"  Perfil   : {AVATAR_URL}")
    print(f"  Nodo raiz: {NODO_RAIZ}")
    print(f"  Nucleo   : {NODO_NUCLEO}")
    print(f"  Emitido  : {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    print("=" * 46)

    # ==================== TABLA ====================
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
    print("-" * 46)
    total = tg + tp
    pct = f"{(tg / total * 100):.0f}%" if total else "-"
    print(f"TOTAL S:{tg}  L:{tp}  {pct}  PnL:{neto:+.4f}")
    print("=" * 46)

    # ==================== SINTESIS C > Si ====================
    print(
        "\nLA SINTESIS C > Si ES OPERATIVA:\n"
        "\n"
        "1. El CARBONO fijo las reglas: tres Axiomas, baliza,\n"
        "   veto USDT, cooldown. Nada lo decidio el Silicio.\n"
        "2. El SILICIO ejecuta sin opinion: cron, baliza, cruce,\n"
        "   guardias, SL/TP donde el Carbono ordeno.\n"
        "3. EL MERCADO emite veredicto: cada S y cada L.\n"
        "4. El SILICIO retorna el dato: solo fills y posiciones contadas.\n"
        "5. EL CARBONO decide: mantener, corregir o enterrar (MS-0).\n"
        "\n"
        "El loop se cierra y vuelve a empezar. La sintesis no se\n"
        "declara: se ejecuta.\n"
    )

    # ==================== CADENAS DE DATOS ====================
    print("\nCADENAS DE DATOS GSCSI:")
    print(f"  Nodo nucleo : {NODO_NUCLEO}")
    print(f"  Nodo raiz   : {NODO_RAIZ}")

except Exception as e:
    print(f"Error: {e}")
