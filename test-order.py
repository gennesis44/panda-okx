# test-order.py — prueba unica de permisos en el perpetuo
import os, logging, ccxt

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

ex = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY'),
    'secret':    os.getenv('OKX_SECRET_KEY') or os.getenv('OKX_API_SECRET'),
    'password':  os.getenv('OKX_PASSWORD') or os.getenv('OKX_PASSPHRASE'),
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'urls': {'api': {'rest': 'https://my.okx.com'}},
})

symbol = 'DOGE/USD:DOGE'
ex.load_markets()
inst_id = ex.market(symbol)['id']

d = ex.privatePostTradeOrder({'instId': inst_id, 'tdMode': 'cross', 'side': 'buy',
                              'ordType': 'optimal_limit_ioc', 'sz': '1'})['data'][0]
log.info(f"Apertura -> sCode={d['sCode']} | {d.get('sMsg')}")

if d['sCode'] == '0':
    try:
        ex.create_order(symbol, 'market', 'sell', 1,
                        params={'tdMode': 'cross', 'reduceOnly': True})
        log.info("Cierre OK. Permisos de trading confirmados — bot 100% operativo.")
    except Exception as e:
        log.error(f"Cierra manualmente en OKX: {e}")
else:
    log.error(f"Sigue bloqueado: {d['sMsg']}")
