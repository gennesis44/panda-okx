import os, logging, ccxt

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

ex = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY'),
    'secret':    os.getenv('OKX_SECRET_KEY') or os.getenv('OKX_API_SECRET'),
    'password':  os.getenv('OKX_PASSWORD') or os.getenv('OKX_PASSPHRASE'),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': 'https://my.okx.com'}},
})
ex.load_markets()

candidatos = [m for m in ex.markets.values()
              if m.get('base') == 'DOGE' and m.get('active')
              and (m.get('swap') or m.get('future'))]

log.info(f"Probando: {[m['id'] for m in candidatos]}")

for m in candidatos:
    sym, inst = m['symbol'], m['id']
    try:
        d = ex.privatePostTradeOrder({'instId': inst, 'tdMode': 'cross', 'side': 'buy',
                                      'ordType': 'optimal_limit_ioc', 'sz': '1'})['data'][0]
        code, msg = str(d.get('sCode')), d.get('sMsg')
        if code == '0':
            log.info(f"PERMITIDO  -> {sym} ({inst})")
            try:
                pos = [p for p in ex.fetch_positions([sym]) if float(p.get('contracts') or 0) > 0]
                if pos:
                    ex.create_order(sym, 'market', 'sell',
                                    ex.amount_to_precision(sym, pos[0]['contracts']),
                                    params={'tdMode': 'cross', 'reduceOnly': True})
                    log.info("           posicion de prueba cerrada")
                else:
                    log.info("           la IOC no se lleno; nada que cerrar")
            except Exception as e:
                log.error(f"           revisa posicion manualmente en OKX ({sym}): {e}")
        else:
            log.info(f"BLOQUEADO  -> {sym} ({inst}) | {code}: {msg}")
    except ccxt.ExchangeError as e:
        log.info(f"BLOQUEADO  -> {sym} ({inst}) | {e}")

log.info("Fin de prueba.")
