# check-xlm.py v3 — SOLO LECTURA. No envia ordenes.
# Lee el HISTORIAL DE ORDENES oficial de OKX (incluye PnL real en cierres)
# y las ordenes SL/TP de XLM. Responde: TP logrado, SL ejecutado o sigue abierta.
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

print("=== 1. POSICIONES ABIERTAS ===")
xlm_abierta = None
for p in ex.fetch_positions():
    if (p.get('contracts') or 0) > 0:
        es_xlm = str(p.get('symbol') or '').upper().startswith('XLM/')
        if es_xlm:
            xlm_abierta = p
        marca = '>>> XLM <<<' if es_xlm else '          '
        print(f"{marca} {p['symbol']} | {p['side'].upper()} | {p['contracts']} contratos | "
              f"entrada: {p.get('entryPrice')} | PnL flotante: {p.get('unrealizedPnl')} "
              f"{p.get('marginCurrency') or ''}")
if xlm_abierta is None:
    print(">>> XLM: SIN posicion abierta (ya se cerro, o nunca se abrio).")

print()
print("=== 2. ORDENES RECIENTES DE XLM (PnL oficial OKX) ===")
hubo = False
for inst_type in ('FUTURES', 'SWAP'):
    try:
        hist = ex.private_get_trade_orders_history({'instType': inst_type, 'limit': '50'})
        for o in (hist.get('data') or []):
            inst = str(o.get('instId') or '')
            if not inst.upper().startswith('XLM'):
                continue
            hubo = True
            cierre = str(o.get('reduceOnly', 'false')) == 'true'
            etiq = 'CIERRE' if cierre else 'apertura'
            pnl = o.get('pnl') or '0'
            res = ''
            if cierre and float(pnl) != 0:
                res = '  <== GANANCIA (TP)' if float(pnl) > 0 else '  <== PERDIDA (SL)'
            print(f"[{inst_type}] {_ts(o.get('uTime'))} UTC | {o.get('side')} "
                  f"{o.get('accFillSz') or o.get('sz')} @ {o.get('avgPx')} | {etiq} | "
                  f"estado: {o.get('state')} | PnL: {pnl} | fee: {o.get('fee')}{res}")
    except Exception as e:
        print(f"[{inst_type}] no disponible: {e}")
if not hubo:
    print("(ninguna orden de XLM en las ultimas 50 ordenes de futuros/swap)")

print()
print("=== 3. SL/TP DE XLM (ordenes condicionales) ===")
for ordtype in ('oco', 'conditional'):
    for inst_type in ('FUTURES', 'SWAP'):
        try:
            algos = ex.private_get_trade_orders_algo_history(
                {'ordType': ordtype, 'instType': inst_type, 'limit': '20'})
            for a in (algos.get('data') or []):
                inst = str(a.get('instId') or '')
                if not inst.upper().startswith('XLM'):
                    continue
                print(f"[{inst_type}] {_ts(a.get('cTime'))} UTC | {ordtype} | "
                      f"estado: {a.get('state')} | TP: {a.get('tpTriggerPx')} | "
                      f"SL: {a.get('slTriggerPx')} | ejecutado a: {a.get('actualPx')}")
        except Exception as e:
            print(f"[{ordtype}/{inst_type}] no disponible: {e}")

print()
print("=== LECTURA RAPIDA ===")
if xlm_abierta is not None:
    print(f"Tu long SIGUE ABIERTA. PnL flotante ahora: {xlm_abierta.get('unrealizedPnl')}")
else:
    print("No hay posicion XLM abierta. Busca en la seccion 2 la linea CIERRE:")
    print("  PnL positivo  -> TP logrado (tu entrada gano ~+1.5%)")
    print("  PnL negativo  -> SL ejecutado (perdio ~-1%)")
