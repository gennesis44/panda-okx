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
        print("  → La cuenta está VACÍA: para arrancar en vivo hay que INYECTAR capital.")
    else:
        print(f"  {'MONEDA':<8}{'EQUITY':>16}{'DISPONIBLE':>16}{'CONGELADO':>14}")
        print("  " + "-" * 56)
        for ccy, eq, disp, frz in sorted(filas, key=lambda x: -x[1]):
            print(f"  {ccy:<8}{eq:>16,.6g}{disp:>16,.6g}{frz:>14,.6g}")
        print("\n  USDT/USDC disponible = capital arrancable directo.")
        print("  Otras monedas: convertir a USDT en la app (Convertir) si quieres sumarlas.")

except Exception as e:
    print("  ✗ FALLO:", str(e)[:300])
    sys.exit(1)
