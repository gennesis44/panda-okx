#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# main-score.py — WIN/FAILED de la flota · OKX SWAP · SOLO LECTURA
#   Claves por env: OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSWORD (o OKX_HY_PASSWORD)
#   Uso: python main-score.py [dias]   (def: 3)
#   Salida: W/L por navio + total, PnL de trades cerrados
import os
import sys
import time
import ccxt

HOST = 'https://my.okx.com'
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
FLOTA = ('DOGE', 'ADA', 'AVAX', 'HBAR', 'SUI', 'INJ', 'XRP', 'XLM', 'BTC')


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def ship(sym):
    b = str(sym).split('/')[0].split('-')[0].upper()
    return b if b in FLOTA else '?' + b


def main():
    key = os.getenv('OKX_API_KEY', '')
    sec = os.getenv('OKX_SECRET_KEY', '')
    pwd = os.getenv('OKX_PASSWORD', '') or os.getenv('OKX_HY_PASSWORD', '')
    falta = [n for n, v in (('OKX_API_KEY', key), ('OKX_SECRET_KEY', sec),
                            ('OKX_PASSWORD', pwd)) if not v]
    if falta:
        print('[FALLO-CERRADO] faltan: ' + ', '.join(falta))
        raise SystemExit(2)

    ex = ccxt.okx({
        'apiKey': key, 'secret': sec, 'password': pwd,
        'enableRateLimit': True, 'timeout': 20000,
        'options': {'defaultType': 'swap'},
        'urls': {'api': {'rest': HOST}},
    })

    since = int(time.time() * 1000) - DAYS * 86_400_000
    fills, after = [], None
    for _ in range(30):
        params = {'instType': 'SWAP'}
        if after:
            params['after'] = after
        lot = ex.fetch_my_trades(None, since=since, limit=100, params=params)
        if not lot:
            break
        fills.extend(lot)
        after = lot[-1].get('info', {}).get('billId') or lot[-1].get('id')
        if len(lot) < 100 or _f(lot[-1].get('timestamp')) < since:
            break
    fills = [t for t in fills if _f(t.get('timestamp')) >= since]

    order_pnl, score = {}, {}
    for t in fills:
        i = t.get('info', {})
        pnl = _f(i.get('fillPnl'))
        if pnl == 0.0:
            continue  # fills de apertura no puntuan
        k = (ship(t.get('symbol', '') or i.get('instId', '')),
             i.get('ordId') or t.get('id'))
        order_pnl[k] = order_pnl.get(k, 0.0) + pnl

    for (s, _), pnl in order_pnl.items():
        m = score.setdefault(s, {'w': 0, 'l': 0, 'pnl': 0.0})
        if pnl > 0:
            m['w'] += 1
        else:
            m['l'] += 1
        m['pnl'] += pnl

    tw = tl = 0
    tp = 0.0
    print('=' * 46)
    print('WIN/FAILED flota | ultimos ' + str(DAYS) + ' dias | UTC '
          + time.strftime('%Y-%m-%d %H:%M', time.gmtime()))
    print('=' * 46)
    for s in sorted(score):
        m = score[s]
        tw += m['w']; tl += m['l']; tp += m['pnl']
        print(f"{s:<6} WIN {m['w']}  FAILED {m['l']}  {m['pnl']:+.2f}$")
    print('-' * 46)
    print(f"TOTAL WIN {tw}  FAILED {tl}  {tp:+.2f}$")
    print('=' * 46)


if __name__ == '__main__':
    main()
