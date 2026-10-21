#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# main-atalaya.py — EL VIGIA · ÚNICA CATAPULTA DE LA FLOTA · v4.1 (Carbono)
#   MISIÓN VIGIA: vigilar el mar de cada destructor (incluyendo INJ y SUI).
#   Cuando detecta una APERTURA DE OLAS (separación EMA rápida/lenta >= 0.15%)
#   con cruce fresco en la ventana, CANTA LA SEÑAL.
#   MISIÓN CATAPULTA: ejecuta directamente el .py del destructor que viva
#   en la RAÍZ del repo con SINGLE_CYCLE=1.
#   Fail-closed: casco que no compila no se lanza; libro ilegible, no se lanza.
#   Claves env: OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSWORD.

import os
import sys
import time
import subprocess
import py_compile
import ccxt
import pandas as pd
import requests

HOST = 'https://my.okx.com'
LOCK = float(os.getenv('ATALAYA_LOCK', '0.0015'))  # apertura de olas 0.15%
WINDOW = tuple(int(x) for x in os.getenv('ATALAYA_WINDOW', '-2,-3,-4').split(','))

# LEYES DE LA FLOTA (Ampliada con INJ y SUI)
SHIPS = {
    'DOGE': {'tf': '5m',  'fast': 3, 'slow': 21},
    'ADA':  {'tf': '6h',  'fast': 4, 'slow': 17},
    'AVAX': {'tf': '1h',  'fast': 8, 'slow': 34},
    'HBAR': {'tf': '30m', 'fast': 3, 'slow': 21},  # stack real: 3/4/10/21/27
    'XLM':  {'tf': '1h',  'fast': 3, 'slow': 21},
    'XRP':  {'tf': '1h',  'fast': 3, 'slow': 21, 'shadow': True},
    'INJ':  {'tf': '1h',  'fast': 3, 'slow': 21},
    'SUI':  {'tf': '1h',  'fast': 3, 'slow': 21},
}

# CATAPULTA DIRECTA DEL ATALAYA
LAUNCH_DEFAULT = 'DOGE,ADA,AVAX,HBAR,XLM,XRP,INJ,SUI'
LAUNCH_SHIPS = [s.strip().upper() for s in os.getenv('LAUNCH_SHIPS', LAUNCH_DEFAULT).split(',') if s.strip()]

try:
    LAUNCH_TIMEOUT = int(os.getenv('LAUNCH_TIMEOUT', '300'))
except ValueError:
    LAUNCH_TIMEOUT = 300  # tope de seguridad por zarpe (300 segundos)

OWN_MARKS = ('atalaya', 'saldo', 'score', 'grid', 'auditoria', 'informe', 'backtest', 'radar', 'btc', 'xpl')
TF_MS = {
    '1m': 60000, '5m': 300000, '15m': 900000, '30m': 1800000,
    '1h': 3600000, '2h': 7200000, '4h': 14400000, '6h': 21600000,
    '12h': 43200000, '1d': 86400000
}


def env_para_zarpar(base_env):
    """Entorno del navío: el del puente + ciclo único + bitácora viva."""
    env = dict(base_env)
    env['SINGLE_CYCLE'] = '1'      # Un zarpe = un ciclo
    env['PYTHONUNBUFFERED'] = '1'  # Bitácora en vivo
    return env


def script_for_ship(files, ship):
    """Busca el casco del destructor en la raíz del astillero (cwd)."""
    s = ship.lower()
    exactos = sorted(f for f in files if f.lower() in ('main-' + s + '.py', s + '.py'))
    if exactos:
        return exactos[0]
    for f in sorted(files):
        low = f.lower()
        if not low.endswith('.py') or low == 'main-atalaya.py':
            continue
        if any(m in low for m in OWN_MARKS):
            continue
        if s in low:
            return f
    return None


def casco_sano(script):
    """Fail-closed: un casco que no compila no zarpa."""
    try:
        py_compile.compile(script, doraise=True)
        return True
    except Exception:
        return False


def decide_zarpe(pos_abierta, fills_despues):
    """Un cruce = un zarpe. Si hay posición abierta o rastro de fills desde la vela del cruce, no se relanza."""
    if pos_abierta:
        return False, 'YA EN LA MAR (posición abierta)'
    if fills_despues:
        return False, 'YA ZARPÓ por esta señal (fills tras la vela del cruce)'
    return True, 'Mar limpio: sin posición ni rastro desde la señal'


def bases_en_la_mar(ex):
    """Bases con posición abierta (un solo lectura del arca).
    None = lectura fallida (fail-closed: nadie zarpa esa ronda)."""
    en_mar = set()
    try:
        for p in ex.fetch_positions():
            try:
                if (p.get('contracts') or 0) > 0:
                    base = (p.get('base') or (p.get('symbol') or '').split('/')[0])
                    if base:
                        en_mar.add(str(base).upper())
            except Exception:
                continue
        return en_mar
    except Exception as e:
        print('[CATAPULTA] Sin lectura de posiciones (' + str(e) + ') — fail-closed: nadie zarpa esta ronda')
        return None


def ya_zarpo_fills(ex, sym, bar_close_ms):
    """¿Rastro del navío tras la vela del cruce? (bool, error)
    fail-closed: libro ilegible -> se asume que ya zarpó."""
    if not bar_close_ms:
        return False, None
    try:
        fills = ex.fetch_my_trades(sym, since=bar_close_ms, limit=25)
        return bool(fills), None
    except Exception as e:
        return True, str(e)


def launch_one(script, ship, side, sep):
    """Zarpe: un ciclo único del .py del destructor, con su propia ley."""
    if not casco_sano(script):
        print('[' + ship + '] Casco ROTO (' + script + ' no compila) — no se lanza (fail-closed)')
        return False
    print('>>> CATAPULTA [' + ship + '] ' + side + ' -> python ' + script +
          ' SINGLE_CYCLE=1 (señal sep ' + format(sep * 100, '.2f') + '%)')
    env = env_para_zarpar(os.environ)
    try:
        r = subprocess.run([sys.executable, script], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=LAUNCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        print('[' + ship + '] No regresó en ' + str(LAUNCH_TIMEOUT) + 's — proceso cerrado; clOrdId evita dobles.')
        return False
    except Exception as e:
        print('[' + ship + '] Fallo de lanzamiento: ' + str(e))
        return False

    for line in (r.stdout or '').splitlines()[-40:]:
        print('   │ ' + line)
    for line in (r.stderr or '').splitlines()[-12:]:
        print('   ║ ' + line)

    if r.returncode == 0:
        print('[' + ship + '] Ciclo único completo (exit 0) — de vuelta a puerto')
        return True
    print('[' + ship + '] Regresó con avería (exit ' + str(r.returncode) + ')')
    return False


def catapulta(ex, seniales):
    print('-' * 60)
    files = [f for f in os.listdir('.') if f.lower().endswith('.py')]
    print('CATAPULTA DIRECTA: Ejecuta el .py de la raíz con SINGLE_CYCLE=1.')
    print('Lanzables: ' + ','.join(LAUNCH_SHIPS) + ' | Tope por zarpe: ' + str(LAUNCH_TIMEOUT) + 's')
    
    en_mar = bases_en_la_mar(ex)
    if en_mar is None:
        return

    print('Astillero: ' + ' | '.join(
        n + '->' + (script_for_ship(files, n) or 'SIN CASCO') for n in LAUNCH_SHIPS))
    if en_mar:
        print('Ya en la mar: ' + ', '.join(sorted(en_mar)))

    disparos = 0
    for name, side, sep, bar_close_ms in seniales:
        if name not in LAUNCH_SHIPS:
            print('[' + name + '] Señal válida pero NO lanzable (fuera de LAUNCH_SHIPS)')
            continue
        script = script_for_ship(files, name)
        if not script:
            print('[' + name + '] Sin casco en el astillero (falta su .py en la raíz) — no lanzo')
            continue
        if name in en_mar:
            pos_abierta, fills, err = True, True, None
        else:
            fills, err = ya_zarpo_fills(ex, pick_future(ex, name), bar_close_ms)
            pos_abierta = False

        if err:
            print('[' + name + '] Libro ilegible (' + err + ') — fail-closed: no lanzo')
            continue

        lanza, motivo = decide_zarpe(pos_abierta, fills)
        if not lanza:
            print('[' + name + '] Sin disparo: ' + motivo)
            continue

        if launch_one(script, name, side, sep):
            disparos += 1

    if disparos:
        print('Catapulta: ' + str(disparos) + ' zarpe(s) esta ronda.')
    else:
        print('Catapulta sin disparos esta ronda.')


# CANDADO DE RÉGIMEN
GATE_OKX = 'https://my.okx.com'


def gate_7d(hi, lo, px):
    return px > hi * 0.995, px < lo * 1.005


def estado_mar():
    """(rompe_arriba, rompe_abajo, ok) — BTC-USD-SWAP 1H, mástil 7d."""
    try:
        r = requests.get(
            GATE_OKX + '/api/v5/market/candles',
            params={'instId': 'BTC-USD-SWAP', 'bar': '1H', 'limit': '200'},
            timeout=15)
        r.raise_for_status()
        rows = [x for x in (r.json().get('data') or []) if len(x) > 8 and x[8] == '1']
        if len(rows) < 7 * 24 + 2:
            return False, False, False
        df = pd.DataFrame([x[:6] for x in rows], columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        for col in ('h', 'l', 'c'):
            df[col] = df[col].astype(float)
        df['ts'] = df['ts'].astype('int64')
        df = df.sort_values('ts').reset_index(drop=True)
        win = df.iloc[-7 * 24:]
        up, dn = gate_7d(win['h'].max(), win['l'].min(), df['c'].iloc[-1])
        return up, dn, True
    except Exception:
        return False, False, False


# VIGIA
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
        raise SystemExit('[FALLO-CERRADO] Faltan claves API de OKX')
    return ccxt.okx({
        'apiKey': k, 'secret': s, 'password': p,
        'enableRateLimit': True, 'timeout': 20000,
        'options': {'defaultType': 'swap'},
        'urls': {'api': {'rest': HOST, 'public': HOST, 'private': HOST}},
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
                and (m.get('swap') or (m.get('info') or {}).get('instType') == 'SWAP')
                and str(m.get('settle') or '').upper() != 'USDT'):
            return m['symbol']
    return None


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def watch_ship(ex, name, cfg):
    sym = pick_future(ex, name)
    if not sym:
        print('[' + name + '] Sin instrumento USD activo — omitida')
        return None
    tf = cfg['tf']
    ohlcv = ex.fetch_ohlcv(sym, tf, limit=100)
    df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
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
          ': ' + format(ef.iloc[last], '.6g') + '/' + format(es.iloc[last], '.6g') +
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
        up = (ef.iloc[prev] <= es.iloc[prev] and ef.iloc[idx] > es.iloc[idx])
        dn = (ef.iloc[prev] >= es.iloc[prev] and ef.iloc[idx] < es.iloc[idx])
        if up or dn:
            side = 'LONG' if up else 'SHORT'
            ts = time.strftime('%m-%d %H:%M', time.gmtime(df['ts'].iloc[idx] / 1000))
            print('>>> SEÑAL [' + name + '] ' + side +
                  ' — cruce EMA' + str(cfg['fast']) + '/' + str(cfg['slow']) +
                  ' vela ' + str(i) + ' (' + ts + ' UTC, sep ' + format(sep * 100, '.2f') + '%)')
            return (name, side, sep, int(df['ts'].iloc[idx]) + TF_MS[tf])
        break
    return None


def main():
    ex = build_exchange()
    ex.load_markets()
    raw = ex.privateGetAccountBalance()
    det = ((raw or {}).get('data') or [{}])[0].get('details') or []
    total = sum(_f(d.get('eqUsd')) for d in det)
    print('=' * 60)
    print('EL VIGIA · CATAPULTA DIRECTA — Apertura de olas >= ' +
          format(LOCK * 100, '.2f') + '% | Ventana ' + str(list(WINDOW)) +
          ' | UTC ' + time.strftime('%Y-%m-%d %H:%M', time.gmtime()) +
          ' | Arca ~$' + format(total, '.2f'))
    print('=' * 60)

    seniales = []
    for name, cfg in SHIPS.items():
        try:
            r = watch_ship(ex, name, cfg)
            if r:
                seniales.append(r)
        except Exception as e:
            print('[' + name + '] Fallo de vigía: ' + str(e))

    print('-' * 60)
    if seniales:
        for name, side, sep, _ts in seniales:
            print('>>> ' + name + ' ' + side + ' (sep ' + format(sep * 100, '.2f') + '%)')
    else:
        print('Mar en calma: ninguna nave cumple condiciones.')

    gate_on = os.getenv('ATALAYA_GATE', '1') != '0'
    if not seniales:
        pass
    elif not gate_on:
        print('MAR ABIERTO (Candado de régimen DESARMADO): Catapulta lista.')
        catapulta(ex, seniales)
    else:
        up, dn, ok = estado_mar()
        if not ok:
            print('CANDADO CERRADO (fail-closed): Sin lectura del mar — nadie zarpa esta ronda.')
        elif up or dn:
            print('MAR ABIERTO (' + ('ROMPE ARRIBA' if up else 'ROMPE ABAJO') + ' del mástil 7d): Catapulta lista.')
            catapulta(ex, seniales)
        else:
            print('CANDADO CERRADO (BTC EN RANGO 7d): Señales SOLO en mapa — nadie zarpa.')

    print('Vigía cerrado. Las destructoras ejecutan por su ley.')


if __name__ == '__main__':
    main()
