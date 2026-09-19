# check-xlm.py v2 — SOLO LECTURA. No envia ordenes.
# Muestra TODOS los fills recientes (entradas incluidas) y destaca XLM.
import os
import time
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

def _ts(ms):
    return time.strftime('%Y-%m-%d %H:%M', time.gmtime(int(ms or 0) / 1000))

print("=== POSICIONES ABIERTAS (derivados) ===")
abiertas = False
for p in ex.fetch_positions():
    if (p.get('contracts') or 0) > 0:
        abiertas = True
        print(f"{p['symbol']} | {p['side'].upper()} | {p['contracts']} contratos | "
              f"entrada: {p.get('entryPrice')} | PnL no realizado: {p.get('unrealizedPnl')} "
              f"{p.get('marginCurrency') or ''}")
if not abiertas:
    print("(sin posiciones abiertas)")

print()
print("=== TODOS LOS MOVIMIENTOS RECIENTES (entradas y cierres) ===")
print("    (>>> XLM <<< = operacion de XLM)")
total_xlm = 0
for inst_type in ('FUTURES', 'SWAP', 'SPOT'):
    try:
        hist = ex.private_get_trade_fills_history({'instType': inst_type, 'limit': '30'})
        for f in (hist.get('data') or []):
            inst_id = str(f.get('instId') or '')
            es_xlm = inst_id.upper().startswith('XLM')
            marca = '>>> XLM <<<' if es_xlm else '         '
            if es_xlm:
                total_xlm += 1
            tipo = 'CIERRE' if str(f.get('reduceOnly', '0')) == 'true' else 'apertura'
            pnl = f.get('pnl') or '0'
            fee = f.get('fee') or '0'
            print(f"[{inst_type}] {marca} | {_ts(f.get('ts'))} UTC | {inst_id} | "
                  f"{f.get('side')} {f.get('fillSz')} @ {f.get('fillPx')} | {tipo} | "
                  f"PnL: {pnl} | fee: {fee}")
    except Exception as e:
        print(f"[{inst_type}] no disponible: {e}")

print()
if total_xlm == 0:
    print(">>> RESULTADO: CERO operaciones de XLM en los ultimos 30 registros de cada tipo.")
    print("    El long de XLM que recuerdas NO paso por esta cuenta (o es mas antiguo).")
else:
    print(f">>> RESULTADO: {total_xlm} movimiento(s) de XLM encontrados (marcados arriba).")
