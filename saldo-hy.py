# saldo-hy.py — GSCSI · CUENTA COMPLETA EN UNA CORRIDA
#   [1] Patrimonio + balances por moneda
#   [2] Posiciones ABIERTAS (futuros, con PnL flotante y % recorrido)
#   [3] CIERRES recientes (positions-history, con PnL realizado)
# Solo lectura · sin órdenes · my.okx.com · EEE · fail-closed (exit 1)
import os
import sys
import time
import ccxt

API_KEY    = os.environ.get('OKX_API_KEY', '')
SECRET_KEY = os.environ.get('OKX_SECRET_KEY', '')
PASSPHRASE = os.environ.get('OKX_HY_PASSWORD') or os.environ.get('OKX_PASSWORD') or ''

if not (API_KEY and SECRET_KEY and PASSPHRASE):
    sys.exit("ABORTADO: falta credencial")

exchange = ccxt.okx({
    'apiKey':    API_KEY,
    'secret':    SECRET_KEY,
    'password':  PASSPHRASE,
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': 'https://my.okx.com'}},   # EEE — obligatorio
})


def ffecha(ms):
    try:
        return time.strftime('%d %H:%M', time.gmtime(int(ms) / 1000))
    except (TypeError, ValueError):
        return '?'

def fnum(x, dec=6):
    try:
        return f"{float(x):,.{dec}g}"
    except (TypeError, ValueError):
        return '?'


def seccion_patrimonio():
    print("== [1] PATRIMONIO (margen unificado) ==")
    data = exchange.privateGetAccountBalance({}).get('data', [])
    if not data or not data[0].get('details'):
        print("  La cuenta no devuelve detalles de balance.")
        return
    root = data[0]
    print(f"  Equity total de la cuenta: {float(root.get('totalEq') or 0):,.2f} USD")

    filas = []
    for c in root.get('details', []):
        eq = float(c.get('eq') or 0)
        if eq == 0:
            continue
        filas.append((c.get('ccy', '?'), eq,
                      float(c.get('availBal') or 0) + float(c.get('availEq') or 0),
                      float(c.get('frozenBal') or 0)))
    if not filas:
        print("  ✓ Sin monedas con saldo distinto de cero.")
        return
    print(f"  {'MONEDA':<8}{'EQUITY':>16}{'DISPONIBLE':>16}{'CONGELADO':>14}")
    print("  " + "-" * 56)
    for ccy, eq, disp, frz in sorted(filas, key=lambda x: -x[1]):
        print(f"  {ccy:<8}{fnum(eq):>16}{fnum(disp):>16}{fnum(frz, 2):>14}")


def seccion_abiertas():
    print("\n== [2] POSICIONES ABIERTAS ==")
    raw = []
    try:
        raw = exchange.privateGetAccountPositions({}).get('data', [])
    except Exception:
        pass
    if not raw:
        try:
            raw = exchange.fetch_positions()
        except Exception as e:
            print(f"  ✗ No se pudieron leer posiciones: {str(e)[:200]}")
            return

    abiertas = [p for p in raw if float(p.get('contracts') or p.get('pos') or 0) > 0]
    if not abiertas:
        print("  ✓ Sin posiciones abiertas.")
        return

    for p in abiertas:
        info = p.get('info') or {}
        iid  = info.get('instId') or (p.get('symbol') or '?')
        base = iid.split('-')[0]
        lado = (p.get('side') or info.get('posSide') or '?').upper()
        contr= float(p.get('contracts') or p.get('pos') or 0)
        entrada = float(p.get('entryPrice') or info.get('avgPx') or 0)
        actual  = float(p.get('markPrice') or info.get('markPx') or 0)
        upl     = float(p.get('unrealizedPnl') or info.get('upl') or 0)
        pct     = (actual / entrada - 1) * 100 if entrada > 0 else 0.0
        lev     = info.get('lever') or info.get('leverage') or '?'
        mgn     = info.get('mgnMode') or '?'
        liq     = info.get('liqPx') or info.get('liq_price') or '-'
        print(f"  {base:<6} {lado:<6} {contr:g} ct @ {entrada:.5f} → {actual:.5f}  "
              f"| PnL flot: {upl:+.4f} ({pct:+.2f}%)  | {lev}x {mgn}  | liq: {liq}")

    # andamios SL/TP pendientes por instrumento
    print("  --- SL/TP pendientes (algo) ---")
    vistos = set()
    for p in abiertas:
        info = p.get('info') or {}
        iid  = info.get('instId') or '?'
        base = iid.split('-')[0]
        if base in vistos:
            continue
        vistos.add(base)
        try:
            algos = exchange.privateGetTradeOrdersAlgoPending({
                'instId': iid}).get('data', [])
            if not algos:
                print(f"  {base:<6}: sin algo-pending visible "
                      f"(los adjuntos a la orden madre no siempre se listan)")
            for a in algos[:2]:
                print(f"  {base:<6}: TP-trigger={a.get('tpTriggerPx','?')}  "
                      f"SL-trigger={a.get('slTriggerPx','?')}  "
                      f"state={a.get('state','?')}")
        except Exception as e:
            print(f"  {base:<6}: no legible ({str(e)[:120]})")


def seccion_cierres():
    print("\n== [3] CIERRES RECIENTES ==")
    hist = []
    for it in ('FUTURES', 'SWAP'):
        try:
            hist += exchange.privateGetAccountPositionsHistory({
                'instType': it, 'limit': '10'}).get('data', [])
        except Exception as e:
            print(f"  ✗ positions-history [{it}]: {str(e)[:150]}")
    if not hist:
        print("  ✓ Sin cierres recientes visibles.")
        return

    # dedup por huella, más recientes primero
    vistos, limpios = set(), []
    for h in hist:
        k = (h.get('instId'), h.get('direction'), h.get('openAvgPx'),
             h.get('closeAvgPx'), h.get('realizedPnl'), h.get('uTime'))
        if k in vistos:
            continue
        vistos.add(k)
        limpios.append(h)
    limpios.sort(key=lambda h: h.get('uTime') or 0, reverse=True)

    print(f"  {'ACTIVO':<6}{'DIR':<7}{'ENTRADA':>11}{'SALIDA':>11}{'PnL':>11}  HORA (UTC)")
    print("  " + "-" * 62)
    for h in limpios[:10]:
        base = (h.get('instId') or '?').split('-')[0]
        pnl  = float(h.get('realizedPnl') or 0)
        marca = ' ←← MANUAL?' if pnl != 0 and abs(pnl) < 0.2 and base in ('FET',) else ''
        print(f"  {base:<6}{(h.get('direction') or '?').upper():<7}"
              f"{fnum(h.get('openAvgPx'), 5):>11}{fnum(h.get('closeAvgPx'), 5):>11}"
              f"{pnl:>+11.4f}  [{ffecha(h.get('uTime'))}]{marca}")


try:
    print("=" * 56)
    print("  CUENTA GSCSI COMPLETA — solo lectura")
    print("  my.okx.com · EEE · USDT: VETADO")
    print(f"  Emitido: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    print("=" * 56)
    seccion_patrimonio()
    seccion_abiertas()
    seccion_cierres()
    print("\n  USDT/USDC disponible = capital arrancable directo.")
except Exception as e:
    print(f"ERROR FATAL: {e}", flush=True)
    sys.exit(1)
