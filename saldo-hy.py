# saldo.py — Saldo OKX v2 · my.okx.com (EEE/MiCA)
#   Colateral total (USD) + balances por moneda + posiciones abiertas
#   Secrets: OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSWORD
import os
import time
import logging

import ccxt

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

HOST = 'https://my.okx.com'

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

def verificar_saldo():
    raw = exchange.privateGetAccountBalance()
    data = (raw or {}).get('data') or []
    if not data:
        log.error("Sin datos de cuenta — ¿keys correctas?")
        return
    details = data[0].get('details') or []
    total = sum(_f(d.get('eqUsd')) for d in details)

    log.info("=" * 56)
    log.info("SALDO OKX | host=" + HOST)
    log.info("Colateral total: ~$" + format(total, '.2f'))
    log.info("-" * 56)
    for d in details:
        ccy = d.get('ccy', '?')
        eq = _f(d.get('eqUsd'))
        if eq > 0.005:
            log.info("  " + ccy.ljust(8) + " ~$" + format(eq, '.2f'))
    log.info("-" * 56)

    try:
        positions = [p for p in exchange.fetch_positions()
                     if _f(p.get('contracts')) > 0]
        if not positions:
            log.info("Posiciones abiertas: ninguna")
        else:
            log.info("Posiciones abiertas:")
            for p in positions:
                lado = str(p.get('side') or '?')
                sym = str(p.get('symbol') or '?')
                ct = _f(p.get('contracts'))
                upnl = _f(p.get('unrealizedPnl'))
                notional = _f(p.get('notional'))
                log.info("  " + sym + " | " + lado + " | " + str(ct) +
                         " ct | nocional ~$" + format(abs(notional), '.2f') +
                         " | PnL " + format(upnl, '+.4f') + " USD")
    except Exception as e:
        log.warning("No se pudieron leer posiciones: " + str(e))
    log.info("=" * 56)

if __name__ == "__main__":
    log.info(">>> saldo.py v2 (" + time.strftime('%Y-%m-%d %H:%M:%S') + ")")
    try:
        verificar_saldo()
    except Exception as e:
        log.error("Fallo: " + str(e))
        raise SystemExit(1)
