# check-xlm.py — SOLO LECTURA. No envia ordenes. Diagnostico de posiciones y fills.
import os
import ccxt

HOST = 'https://my.okx.com'
ex = ccxt.okx({
    'apiKey':   os.getenv('OKX_API_KEY', ''),
    'secret':   os.getenv('OKX_SECRET_KEY', ''),
    'password': os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'options':  {'defaultType': 'swap'},
    'urls':     {'api': {'rest': HOST}},
})
ex.load_markets()

print("=== POSICIONES ABIERTAS (derivados) ===")
abiertas = False
for p in ex.fetch_positions():
    if (p.get('contracts') or 0) > 0:
        abiertas = True
        print(f"{p['symbol']} | {p['side'].upper()} | {p['contracts']} contratos | "
              f"entrada: {p.get('entryPrice')} | PnL no realizado: {p.get('unrealizedPnl')} {p.get('marginCurrency') or ''}")
if not abiertas:
    print("(sin posiciones abiertas)")

print("=== ULTIMOS FILLS CON PnL REALIZADO ===")
for inst_type in ('FUTURES', 'SWAP', 'SPOT'):
    try:
        hist = ex.private_get_trade_fills_history({'instType': inst_type, 'limit': '20'})
        for f in (hist.get('data') or []):
            pnl = f.get('pnl')
            if pnl and float(pnl) != 0:
                print(f"[{inst_type}] {f.get('instId')} | {f.get('ts')} | "
                      f"PnL: {pnl} | fill: {f.get('fillPx')} x {f.get('fillSz')}")
    except Exception as e:
        print(f"[{inst_type}] no disponible: {e}")
