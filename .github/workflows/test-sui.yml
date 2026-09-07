# test-sui.py — diagnostico de permisos y tamanos para SUI
import os, logging, ccxt

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

BASE = 'SUI'   # <-- cambia a 'HBAR' para probar el otro con este mismo archivo

ex = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY'),
    'secret':    os.getenv('OKX_SECRET_KEY') or os.getenv('OKX_API_SECRET'),
    'password':  os.getenv('OKX_PASSWORD') or os.getenv('OKX_PASSPHRASE'),
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'urls': {'api': {'rest': 'https://my.okx.com'}},
})
ex.load_markets()

candidatos = [m for m in ex.markets.values()
              if m.get('base') == BASE and m.get('active')
              and (m.get('swap') or m.get('future'))]

log.info(f"Probando {BASE}: {[m['id'] for m in candidatos]}")

for m in candidatos:
    sym, inst = m['symbol'], m['id']
    ctval = float(m.get('contractSize') or 0)
    try:
        last = ex.fetch_ticker(sym).get('last') or 0
    except Exception:
        last = 0
    valor_1ct = ctval * last if ctval else 0
    etiqueta = f" | 1 contrato = {ctval} {BASE} (~${valor_1ct:.2f})"

    if valor_1ct > 10:
        log.info(f"SKIP       -> {sym}: contrato demasiado grande para 30 EUR{etiqueta}")
        continue

    try:
        d = ex.privatePostTradeOrder({'instId': inst, 'tdMode': 'cross', 'side': 'buy',
                                      'ordType': 'optimal_limit_ioc', 'sz': '1'})['data'][0]
        code, msg = str(d.get('sCode')), d.get('sMsg')
        if code == '0':
            log.info(f"PERMITIDO  -> {sym}{etiqueta}")
            try:
                pos = [p for p in ex.fetch_positions([sym]) if float(p.get('contracts') or 0) > 0]
                if pos:
                    ex.create_order(sym, 'market', 'sell',
                                    ex.amount_to_precision(sym, pos[0]['contracts']),
                                    params={'tdMode': 'cross', 'reduceOnly': True})
                    log.info("           posicion de prueba cerrada")
                else:
                    log.info("           IOC no se lleno; nada que cerrar")
            except Exception as e:
                log.error(f"           revisar posicion en OKX ({sym}): {e}")
        else:
            log.info(f"BLOQUEADO  -> {sym} | {code}: {msg}{etiqueta}")
    except ccxt.ExchangeError as e:
        log.info(f"BLOQUEADO  -> {sym} | {e}{etiqueta}")

log.info(f"Fin de prueba {BASE}.")
