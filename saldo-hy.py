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

print("== SALDO DISPONIBLE (margen unificado) ==")
try:
    data = exchange.privateGetAccountBalance({'ccy': 'USDT'}).get('data', [])
    if data:
        d = data[0]
        det = d.get('details', [{}])[0]
        print(f"  USDT equity:     {float(det.get('eq') or 0):>12.4f}")
        print(f"  USDT disponible: {float(det.get('availBal') or 0):>12.4f}")
        print(f"  USDT congelado:  {float(det.get('frozenBal') or 0):>12.4f}")
    print("\n== TODAS LAS MONEDAS CON SALDO ==")
    data2 = exchange.privateGetAccountBalance().get('data', [])
    for c in data2[0].get('details', []):
        eq = float(c.get('eq') or 0)
        if eq != 0:
            print(f"  {c.get('ccy',''):<8} equity={eq:>14.6g}  disp={float(c.get('availBal') or 0):>12.4f}")
    print("\n  → Si hay USDC/DOGE/etc sobrantes, conviértelos a USDT desde la app (Convertir).")
except Exception as e:
    print("  ✗ FALLO:", str(e)[:300])
    sys.exit(1)
