#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# atalaya.py — EL VIGÍA DE LA FLOTA · SOLO LECTURA
#   MISIÓN: vigilar el mar de cada destructor. Cuando detecta una
#   APERTURA DE OLAS (separación EMA rápida/lenta >= 0.15%) con cruce
#   fresco en la ventana, CANTA LA SEÑAL para la nave que cumple.
#   No abre nada: la ejecución la hace cada destructor con su ley.
#   PAREJA DE BTC-RADAR: el radar escanea TODO el océano; el atalaya
#   vigila las aguas exactas donde navega la flota.
#   Claves env: OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSWORD
import os
import time
import ccxt
import pandas as pd

HOST = 'https://my.okx.com'
LOCK = float(os.getenv('ATALAYA_LOCK', '0.0015'))  # apertura de olas 0.15%
WINDOW = tuple(int(x) for x in
               os.getenv('ATALAYA_WINDOW', '-2,-3,-4').split(','))

# LEYES DE LA FLOTA (mismo registro que el marcador)
SHIPS = {
    'DOGE': {'tf': '5m',  'fast': 3, 'slow': 21},
    'ADA':  {'tf': '6h',  'fast': 4, 'slow': 17},
    'AVAX': {'tf': '1h',  'fast': 8, 'slow': 34},
    'HBAR': {'tf': '30m', 'fast': 3, 'slow': 21},  # stack real: 3/4/10/21/27
    'XLM':  {'tf': '1h',  'fast': 3, 'slow': 21},
    'XRP':  {'tf': '1h',  'fast': 3, 'slow': 21,
             'shadow': True},
}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def build_exchange():
    k = os.getenv('OKX_API_KEY', '')
    s = os.getenv('OKX_SECRET_KEY', '')
    p = os.getenv('OKX_PASSWORD', '')
    if not (k and s and p):
        raise SystemExit('[FALLO-CERRADO] faltan keys')
    return ccxt.okx({
        'apiKey': k, 'secret': s, 'password': p,
        'enableRateLimit': True, 'timeout': 20000,
        'options': {'defaultType': 'swap'},
        'urls': {'api': {'rest': HOST}},
    })


def pick_future(ex, base):
    best, exp_max = None, -1
    for m in ex.markets.values():
        if (m.get('base') == base and m.get('future')
                and m.get('active')
                and str(m.get('settle') or '').upper() != 'USDT'):
            try:
                exp = int((m.get('info') or {}).get('expTime') or 0)
            except (TypeError, ValueError):
                exp = 0
            if exp > exp_max:
                best, exp_max = m['symbol'], exp
    if best:
        return best
    for m in ex.markets.values():
        if (m.get('base') == base and m.get('active')
                and (m.get('swap') or
                     (m.get('info') or {}).get('instType') == 'SWAP')
                and str(m.get('settle') or '').upper() != 'USDT'):
            return m['symbol']
    return None


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def watch_ship(ex, name, cfg):
    sym = pick_future(ex, name)
    if not sym:
        print('[' + name + '] sin instrumento USD activo — omitida')
        return None
    tf = cfg['tf']
    ohlcv = ex.fetch_ohlcv(sym, tf, limit=100)
    df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l',
                                      'c', 'v'])
    now_ms = int(time.time() * 1000)
    ms = _f(tf.replace('m', '60000').replace('h', '3600000'))
    if int(df['ts'].iloc[-1]) + ms > now_ms:
        df = df.iloc[:-1].reset_index(drop=True)
    ef = ema(df['c'], cfg['fast'])
    es = ema(df['c'], cfg['slow'])
    last = len(df) - 1
    sep_now = abs(ef.iloc[last] - es.iloc[last]) / es.iloc[last]
    estado = 'ALZA' if ef.iloc[last] > es.iloc[last] else 'BAJA'
    tag = ' (SHADOW)' if cfg.get('shadow') else ''
    print('[' + name + '] ' + sym + ' ' + tf +
          ' | EMA' + str(cfg['fast']) + '/' + str(cfg['slow']) +
          ': ' + format(ef.iloc[last], '.6g') + '/' +
          format(es.iloc[last], '.6g') +
          ' | sep ' + format(sep_now * 100, '.2f') + '%' +
          ' | estado ' + estado + tag)
    for i in WINDOW:
        idx = len(df) + i
        prev = idx - 1
        if prev < cfg['slow'] + 2:
            continue
        sep = abs(ef.iloc[idx] - es.iloc[idx]) / es.iloc[idx]
        if sep < LOCK:
            continue
        up = (ef.iloc[prev] <= es.iloc[prev]
              and ef.iloc[idx] > es.iloc[idx])
        dn = (ef.iloc[prev] >= es.iloc[prev]
              and ef.iloc[idx] < es.iloc[idx])
        if up or dn:
            side = 'LONG' if up else 'SHORT'
            ts = time.strftime(
                '%m-%d %H:%M',
                time.gmtime(df['ts'].iloc[idx] / 1000))
            print('>>> SEÑAL [' + name + '] ' + side +
                  ' — cruce EMA' + str(cfg['fast']) + '/' +
                  str(cfg['slow']) + ' vela ' + str(i) +
                  ' (' + ts + ' UTC, sep ' +
                  format(sep * 100, '.2f') + '%)' +
                  ' — el destructor cumple condiciones')
            return (name, side, sep)
        break
    return None


def main():
    ex = build_exchange()
    ex.load_markets()
    raw = ex.privateGetAccountBalance()
    det = ((raw or {}).get('data') or [{}])[0].get('details') or []
    total = sum(_f(d.get('eqUsd')) for d in det)
    print('=' * 60)
    print('EL VIGÍA — apertura de olas >= ' +
          format(LOCK * 100, '.2f') + '% | ventana ' +
          str(list(WINDOW)) + ' | UTC ' +
          time.strftime('%Y-%m-%d %H:%M', time.gmtime()) +
          ' | arca ~$' + format(total, '.2f'))
    print('=' * 60)
    senales = []
    for name, cfg in SHIPS.items():
        try:
            r = watch_ship(ex, name, cfg)
            if r:
                senales.append(r)
        except Exception as e:
            print('[' + name + '] fallo de vigía: ' + str(e))
    print('-' * 60)
    if senales:
        for name, side, sep in senales:
            print('>>> ' + name + ' ' + side +
                  ' (sep ' + format(sep * 100, '.2f') + '%)')
    else:
        print('Mar en calma: ninguna nave cumple condiciones.')
    print('Vigía cerrado. Las destructoras ejecutan por su ley.')


if __name__ == '__main__':
    main()
