# tabla_flota.py — TABLA DE SERVICIO DE LA FLOTA · SOLO LECTURA (NUNCA ORDENA)
#   Decreto Carbono: mirar y pintar la tabla. Cero ordenes, cero energia.
#   Columnas: moneda | buque | EMA | TF | estado EN VIVO calculado con OKX:
#     ACTIVA (posicion abierta + armadura) / VIGILANDO / SOMBRA / COOLDOWN 60m.
#   Ademas: bodega (colateral, notional total vs colateral, uPnL flotante),
#   armadura SL/TP adherida, naves fuera de flota y avisos de descontrol.
#   Refresco: cron */20 en Actions | python tabla_flota.py --loop (terminal vivo).
#   Ax3: HOST my.okx.com | solo GET | falla por seccion, nunca en bloque | sin pandas.
import os
import sys
import time

import ccxt

HOST = 'https://my.okx.com'
REFRESCO_MIN = 20
COOLDOWN_MIN = 60          # canon de flota: 60 min de calma tras peaje

# ---------------- REGISTRO NAVAL (canon de los timones main-*.py) ----------------
# (base, buque, EMA, TF, sombra)  — los mismos datos de la tabla del puente
FLOTA = [
    ('XPL',  'Atunero',      '3/21',               '1H',  False),
    ('ICP',  'Portaaviones', '3/21',               '1H',  False),
    ('INJ',  'INJ',          '7/21',               '15m', False),
    ('SUI',  'Catamaran',    '3/15',               '1H',  False),
    ('DOGE', 'Perro',        '3/21',               '1H',  False),
    ('AVAX', 'Octavo',       '8/34',               '1H',  False),
    ('HBAR', 'Destructor',   'Stack 3/4/10/21/27', '30m', False),
    ('XLM',  'Estelar',      '3/21',               '1H',  False),
    ('XRP',  'Sombra',       '3/21',               '4H',  True),
    ('ADA',  'Alas',         '4/17',               '6H',  False),
]
EN_FLOTA = {n[0] for n in FLOTA}

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'timeout':   20000,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

def _ts(ms):
    try:
        return time.strftime('%d-%b %H:%M', time.gmtime(int(ms) / 1000.0))
    except Exception:
        return str(ms)

def base_of(sym):
    return str(sym).split('/')[0].split('-')[0]

# ---------------- LECTURAS (solo GET) ----------------
def get_colateral():
    try:
        raw = exchange.privateGetAccountBalance()
        dets = ((raw or {}).get('data') or [{}])[0].get('details') or []
        return sum(_f(d.get('eqUsd')) for d in dets), None
    except Exception as e:
        return 0.0, str(e)

def get_posiciones():
    try:
        out = {}
        for p in exchange.fetch_positions():
            ct = abs(_f(p.get('contracts')))   # cortos visibles (patron certificado)
            if ct <= 0:
                continue
            info = p.get('info') or {}
            mark = _f(p.get('markPrice') or info.get('markPx'))
            csize = _f(p.get('contractSize')) or 1.0
            out[base_of(p.get('symbol'))] = {
                'side': (p.get('side') or '?').upper(),
                'ct': ct,
                'notional': ct * csize * mark,
                'upnl': _f(p.get('unrealisedPnl')),
            }
        return out, None
    except Exception as e:
        return {}, str(e)

def get_armadura():
    armor, err = {}, None
    for t in ('oco', 'conditional'):
        try:
            raw = exchange.privateGetTradeOrdersAlgoPending({'ordType': t})
            for a in (raw.get('data') or []):
                if a.get('slTriggerPx') or a.get('tpTriggerPx'):
                    armor[base_of(a.get('instId'))] = True
        except Exception as e:
            err = str(e)
    return armor, err

def get_fills():
    try:
        merged = []
        for it in ('FUTURES', 'SWAP'):
            raw = exchange.privateGetTradeFillsHistory({'instType': it, 'limit': '20'})
            merged += (raw.get('data') or [])
        latest = {}
        for f in merged:
            b = base_of(f.get('instId'))
            ts = int(_f(f.get('ts')))
            if b not in latest or ts > latest[b]['ts']:
                latest[b] = {'ts': ts, 'pnl': _f(f.get('fillPnl') or f.get('pnl'))}
        return latest, None
    except Exception as e:
        return {}, str(e)

# ---------------- ESTADO EN VIVO POR NAVE ----------------
def estado_de(nave, pos, armor, fills, now):
    base = nave[0]
    if nave[4]:
        return 'SOMBRA (mira, no dispara)', False
    f = fills.get(base)
    cooldown = bool(f and now - f['ts'] <= COOLDOWN_MIN * 60 * 1000 and f['pnl'] < 0)
    p = pos.get(base)
    if p:
        arm = 'SL/TP OK' if armor.get(base) else '!! SIN ARMADURA !!'
        return 'ACTIVA %s %.0fct uPnL %+.2f$ | %s' % (p['side'], p['ct'], p['upnl'], arm), \
               (not armor.get(base))
    if cooldown:
        return 'VIGILANDO cooldown %dm (peaje %.2f$)' % (
            int((now - f['ts']) / 60000), abs(f['pnl'])), False
    if f and now - f['ts'] <= 24 * 3600 * 1000:
        return 'VIGILANDO (ult fill %s)' % _ts(f['ts']), False
    return 'VIGILANDO (sin senal legal)', False

# ---------------- TABLA ----------------
FMT = '%-2s %-6s %-13s %-20s %-4s %s'
ANCHO = 78

def render():
    now = int(time.time() * 1000)
    alertas = []
    colateral, e1 = get_colateral()
    pos, e2 = get_posiciones()
    armor, e3 = get_armadura()
    fills, e4 = get_fills()
    for e in (e1, e2, e3, e4):
        if e:
            alertas.append('Seccion con fallo (tabla parcial): ' + e)
    print('=' * ANCHO)
    print('TABLA DE SERVICIO DE LA FLOTA · solo lectura · %s UTC · %s' % (
        time.strftime('%d-%b %H:%M', time.gmtime(now / 1000.0)), HOST))
    print('=' * ANCHO)
    print(FMT % ('#', 'MONEDA', 'BUQUE', 'EMA', 'TF', 'ESTADO'))
    print('-' * ANCHO)
    abiertas, sin_armadura = [], []
    for i, nave in enumerate(FLOTA, 1):
        est, peligro = estado_de(nave, pos, armor, fills, now)
        if nave[0] in pos:
            abiertas.append(nave[0])
            if not armor.get(nave[0]):
                sin_armadura.append(nave[0])
        print(FMT % (i, nave[0], nave[1], nave[2], nave[3], est))
    for b in sorted(pos):
        if b not in EN_FLOTA:
            print(FMT % ('!!', b, 'FUERA DE FLOTA', '-', '-',
                         'ACTIVA %s %.0fct' % (pos[b]['side'], pos[b]['ct'])))
            alertas.append('Nave fuera de flota con posicion: ' + b)
            abiertas.append(b)
    print('-' * ANCHO)
    notional = sum(pos[b]['notional'] for b in abiertas if b in pos)
    upnl = sum(pos[b]['upnl'] for b in abiertas if b in pos)
    exp = (notional / colateral * 100.0) if colateral > 0 else 0.0
    print('BODEGA: colateral ~%.2f$ | abiertas %d/10 | notional ~%.2f$ (%.0f%% del colateral)'
          % (colateral, len(abiertas), notional, exp))
    print('uPnL flotante: %+.2f$' % upnl)
    if abiertas:
        print('ARMADURA SL/TP: %d/%d posiciones adheridas' % (len(abiertas) - len(sin_armadura), len(abiertas)))
    else:
        print('ARMADURA SL/TP: n/a (flota en vigilancia pura)')
    if sin_armadura:
        alertas.append('Posicion SIN armadura: ' + ', '.join(sin_armadura))
    if exp > 80:
        alertas.append('Exposicion alta: %.0f%% del colateral' % exp)
    if len(abiertas) >= 4:
        alertas.append('Muchas naves zarparon a la vez: %d' % len(abiertas))
    if alertas:
        print('-' * ANCHO)
        print('AVISOS:')
        for a in alertas:
            print('  !! ' + a)
    print('=' * ANCHO)

if __name__ == '__main__':
    loop = '--loop' in sys.argv or os.getenv('TABLA_LOOP') == '1'
    if loop:
        while True:
            print('\033[2J\033[H', end='')
            render()
            time.sleep(REFRESCO_MIN * 60)
    else:
        render()
