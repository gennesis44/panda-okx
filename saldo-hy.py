import os
import sys
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
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': 'https://my.okx.com'}},
})

print("== PATRIMONIO DE LA CUENTA (margen unificado) ==")
try:
    data = exchange.privateGetAccountBalance({}).get('data', [])
    if not data or not data[0].get('details'):
        print("  La cuenta no devuelve detalles de balance (¿vacía por completo?)")
        sys.exit(0)

    root = data[0]
    print(f"  Equity total de la cuenta: {float(root.get('totalEq') or 0):,.2f} USD\n")

    filas = []
    for c in root.get('details', []):
        eq  = float(c.get('eq') or 0)
        if eq == 0:
            continue
        filas.append((
            c.get('ccy', '?'),
            eq,
            float(c.get('availBal') or 0) + float(c.get('availEq') or 0),
            float(c.get('frozenBal') or 0),
        ))

    if not filas:
        print("  ✓ No hay ninguna moneda con saldo distinto de cero.")
    else:
        print(f"  {'MONEDA':<8}{'EQUITY':>16}{'DISPONIBLE':>16}{'CONGELADO':>14}")
        print("  " + "-" * 56)
        for ccy, eq, disp, frz in sorted(filas, key=lambda x: -x[1]):
            print(f"  {ccy:<8}{eq:>16,.6g}{disp:>16,.6g}{frz:>14,.6g}")

    # ─────────────────────────────────────────────────────────
    # POSICIONES ABIERTAS (futuros) — FET incluida con andamios
    # ─────────────────────────────────────────────────────────
    print("\n== POSICIONES ABIERTAS ==")
    try:
        raw = exchange.privateGetAccountPositions({}).get('data', [])
    except Exception:
        raw = []
    # fallback a ccxt si el endpoint crudo no responde
    if not raw:
        try:
            raw = exchange.fetch_positions()
        except Exception as e:
            print(f"  ✗ No se pudieron leer posiciones: {str(e)[:200]}")
            raw = []

    posiciones = []
    for p in raw:
        contratos = float(p.get('contracts') or p.get('pos') or 0)
        if contratos > 0:
            posiciones.append(p)

    if not posiciones:
        print("  ✓ Sin posiciones abiertas.")
    else:
        print(f"  {'ACTIVO':<10}{'LADO':<7}{'CONTR.':>7}{'ENTRADA':>12}"
              f"{'ACTUAL':>12}{'PnL FLOT':>12}{'RECOCIDO':>10}")
        print("  " + "-" * 74)
        for p in posiciones:
            info  = p.get('info') or {}
            iid   = info.get('instId') or (p.get('symbol') or '?')
            base  = iid.split('-')[0] if iid else '?'
            lado  = (p.get('side') or info.get('posSide') or '?').upper()
            entrada = float(p.get('entryPrice') or info.get('avgPx') or 0)
            actual  = float(p.get('markPrice') or info.get('markPx') or 0)
            upl     = float(p.get('unrealizedPnl') or info.get('upl') or 0)
            pct     = (actual / entrada - 1) * 100 if entrada > 0 else 0.0
            lever   = info.get('lever') or '?'
            marca   = ' ←← MANUAL' if info.get('clOrdId') or base == 'FET' else ''
            print(f"  {base:<10}{lado:<7}{contratos:>7g}{entrada:>12.5f}"
                  f"{actual:>12.5f}{upl:>12.4f}{pct:>+9.2f}%  {lever}x{marca}")
            # detalle raw útil
            print(f"     ↳ inst={iid} | mgn={info.get('mgnMode','?')} "
                  f"| liqPx={info.get('liqPx') or '-'} | uTime={info.get('uTime','?')}")

        # SL/TP adjuntos por posición (attachAlgoOrds — via órdenes de algoritmo)
        print("\n== ANDAMIOS (SL/TP adjuntos) ==")
        for p in posiciones:
            info = p.get('info') or {}
            iid  = info.get('instId') or '?'
            base = iid.split('-')[0]
            try:
                algos = exchange.privateGetTradeOrdersAlgoPending({
                    'ordType': 'oco', 'instId': iid}).get('data', [])
                if not algos:
                    algos = exchange.privateGetTradeOrdersAlgoPending({
                        'ordType': 'conditional', 'instId': iid}).get('data', [])
                if not algos:
                    print(f"  {base}: sin algo-pending visible (SL/TP pueden "
                          f"estar como adjuntos de la orden madre)")
                for a in algos[:2]:
                    print(f"  {base}: TP-trigger={a.get('tpTriggerPx','?')} "
                          f"| SL-trigger={a.get('slTriggerPx','?')} "
                          f"| estado={a.get('state','?')}")
            except Exception as e:
                print(f"  {base}: no se pudo leer algo-pending ({str(e)[:120]})")

    print("\n  USDT/USDC disponible = capital arrancable directo.")
    print("  Otras monedas: convertir a USDT en la app (Convertir) si quieres sumarlas.")

except Exception as e:
    print("  ✗ FALLO:", str(e)[:300])
    sys.exit(1)
