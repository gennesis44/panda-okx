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

print("== POSICIONES ABIERTAS ==")
try:
    # Sin 'instType': OKX devuelve TODAS las posiciones de la cuenta
    data = exchange.privateGetAccountPositions({}).get('data', [])
    abiertas = [p for p in data if float(p.get('pos') or 0) != 0]
    if not abiertas:
        print("  ✓ NINGUNA posición abierta. No hay nada que cerrar.")
    else:
        print(f"  {len(abiertas)} posición(es) abierta(s):\n")
        print(f"  {'INSTRUMENTO':<26}{'LADO':<7}{'TAMAÑO':>12}{'ENTRY':>14}{'PnL_NO_REAL':>14}")
        print("  " + "-" * 76)
        for p in abiertas:
            lado = p.get('posSide', 'net')
            if lado == 'net':
                lado = 'LONG' if float(p['pos']) > 0 else 'SHORT'
            print(f"  {p['instId']:<26}{lado:<7}{p['pos']:>12}"
                  f"{float(p.get('avgPx') or 0):>14.6g}"
                  f"{float(p.get('upl') or 0):>+14.4f}  {p.get('posCcy','')}")
        print("\n  → Ciérralas a mercado desde la app (Trade → posición → cerrar).")
        print("    Esta clave es read-only: no puede ejecutar el cierre.")
except Exception as e:
    print("  ✗ FALLO:", str(e)[:300])
    sys.exit(1)
