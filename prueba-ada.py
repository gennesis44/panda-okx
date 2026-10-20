#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# prueba-ada.py — CANONAZO DE PRUEBA DEL ARCA (una bala, recoger, bitacora)
#
# OBJETIVO: confirmar de una vez si la API key puede COMERCIAR en el
# mercado de la flota (X-Perp). No es senal ni estrategia: es un disparo
# al aire con el collar minimo y recogida inmediata. Coste: la fee de
# ida y vuelta (centimos).
#
# SECUENCIA:
#   1) ficha del instrumento (TEST_INST, default ADA-USD_UM_XPERP-310404,
#      1 ct = 10 ADA ~ $2.17)
#   2) orden a MERCADO del minimo con SL/TP anchos atados (+6%/-4%,
#      por si la recogida falla, la posicion se protege sola)
#   3) close-position inmediato
#
# DIAGNOSTICO POR CODIGO (la lectura del humo):
#   sCode 0      -> CANON OPERATIVO: la llave dispara y recoge.
#   50124        -> MURALLA: la llave NO tiene permiso de comercio para
#                   ese mercado (alcance de mercados/criptos de la key).
#   51001        -> instrumento inexistente/mal escrito.
#   51119        -> sin fondos suficientes (llave OK, arca no cubre).
#   50105        -> passphrase incorrecta: pareja llave/passphrase rota.
#                   El JUEZ de variantes prueba tal cual / sin espacios
#                   de borde / sin comillas de borde antes de rendirse.
#   401/50113    -> llave sin permiso Trade o credenciales mal puestas.
#   51000-51099  -> parametro/margen/posicion (se loguea el msg real).
#
# ENV: OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSWORD
#      TEST_INST (instId OKX)  TEST_SIDE (buy|sell)  TEST_MGN (cross|isolated)

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time

import requests

HOST = 'https://my.okx.com'

INST = os.getenv('TEST_INST', 'ADA-USD_UM_XPERP-310404').strip()
SIDE = os.getenv('TEST_SIDE', 'buy').strip().lower()
if SIDE not in ('buy', 'sell'):
    SIDE = 'buy'
MGN = os.getenv('TEST_MGN', 'cross').strip().lower() or 'cross'
MIN_NOTIONAL = 1.0  # USD: la bala nunca baja de ~$1 de valor si se puede

AK0 = os.getenv('OKX_API_KEY', '')
SK0 = os.getenv('OKX_SECRET_KEY', '')
PW0 = os.getenv('OKX_PASSWORD', '')

# Blindaje: GitHub NO recorta espacios ni saltos al pegar un secret.
AK = AK0.strip()
SK = SK0.strip()
PW = PW0.strip()

COIN = INST.split('-')[0]


def log(msg):
    print('[PRUEBA] ' + msg, flush=True)


def okx_headers(method, path, body='', pw=None):
    ts = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec='milliseconds').replace('+00:00', 'Z')
    sign = base64.b64encode(hmac.new(
        SK.encode(), f'{ts}{method}{path}{body}'.encode(),
        hashlib.sha256).digest()).decode()
    return {'OK-ACCESS-KEY': AK, 'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts,
            'OK-ACCESS-PASSPHRASE': PW if pw is None else pw,
            'Content-Type': 'application/json'}


def okx_get(path, auth=True, pw=None):
    headers = okx_headers('GET', path, pw=pw) if auth \
        else {'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/json'}
    r = requests.get(HOST + path, headers=headers, timeout=20)
    try:
        j = r.json()
    except Exception:
        r.raise_for_status()
        raise RuntimeError(f'OKX {path}: HTTP {r.status_code} sin JSON')
    if j.get('code') != '0':
        raise RuntimeError(f'OKX {path}: {j.get("code")} {j.get("msg")}')
    return j['data']


def okx_post(path, payload):
    body = json.dumps(payload)
    r = requests.post(HOST + path, headers=okx_headers('POST', path, body),
                      data=body, timeout=20)
    try:
        j = r.json()
    except Exception:
        r.raise_for_status()
        raise RuntimeError(f'OKX {path}: HTTP {r.status_code} sin JSON')
    if j.get('code') not in ('0', '1'):
        raise RuntimeError(f'OKX {path}: {j.get("code")} {j.get("msg")}')
    return j


def tick_decimals(tick_str):
    s = tick_str.lower()
    if '.' in s and 'e' not in s:
        d = s.split('.')[1].rstrip('0')
        return len(d) if d else 1
    return 8


def ronda(px, tick, dec):
    return f'{round(round(px / tick) * tick, dec):.{dec}f}'


def instrumento(inst_id):
    for t in ('SWAP', 'FUTURES'):
        try:
            d = okx_get(f'/api/v5/public/instruments?instType={t}'
                        f'&instId={inst_id}', auth=False)
        except Exception:
            d = []
        if d:
            return t, d[0]
    raise RuntimeError(f'{inst_id}: instrumento desconocido (51001)')


def main():
    global PW
    log('=== CANONAZO DE PRUEBA: una bala al aire, recogida inmediata. '
        'Objetivo: saber si la llave dispara ===')
    if not (AK and SK and PW):
        log('sin llaves del arca: fail-closed, ni un disparo.')
        sys.exit(1)
    # JUEZ DE VARIANTES: el 50105 solo dice que ESA passphrase no casa.
    # Antes de condenar a la pareja, prueba el pegado sucio tipico:
    variantes = []
    for etiq, pw in (('tal cual', PW0),
                     ('sin espacios al borde', PW0.strip())):
        if pw and pw not in [v[1] for v in variantes]:
            variantes.append((etiq, pw))
    s = PW0.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in '"\'':
        s2 = s[1:-1].strip()
        if s2 and s2 not in [v[1] for v in variantes]:
            variantes.append(('sin comillas al borde', s2))
    cfg = None
    for etiq, pw in variantes:
        try:
            cfg = okx_get('/api/v5/account/config', pw=pw)
            PW = pw
            break
        except RuntimeError as e:
            if '50105' in str(e):
                log(f'juez: passphrase {etiq} -> 50105 rechazada')
                continue
            log(f'FATAL: el arca no responde ({e}).')
            sys.exit(1)
        except Exception as e:
            log(f'FATAL: el arca no responde ({e}).')
            sys.exit(1)
    if cfg is None:
        log('VEREDICTO DE PAREJA: 50105 en TODAS las variantes. La '
            'passphrase del secret NO pertenece a esta API key. Ojo: '
            '50105 y no 50111 = la llave EXISTE en OKX; con 9 llaves '
            'anotadas, la pareja esta mezclada o mal anotada. CAMINO B: '
            'llave nueva con passphrase propia anotada al crearla + '
            'permiso Trade + los 3 secrets pegados juntos de corrido.')
        sys.exit(1)
    if PW != PW0:
        log('NOTA: la passphrase del secret tenia suciedad de pegado '
            '(espacios/comillas al borde); el canon dispara con la '
            'variante limpia. Al re-pegar el secret, hazlo sin bordes.')
    c = cfg[0] if cfg else {}
    posmode = c.get('posMode', 'net_mode')
    log(f'arca: acctLv {c.get("acctLv", "?")} | modo posicion {posmode} '
        f'| la llave LEE el arca (passphrase EMPAREJADA)')

    try:
        tipo, i = instrumento(INST)
    except Exception as e:
        log(f'FATAL: {e}')
        sys.exit(1)
    ct_val, lot, mins = (float(i['ctVal']), float(i['lotSz']),
                         float(i['minSz']))
    tick_s = i['tickSz']
    tick, dec = float(tick_s), tick_decimals(tick_s)
    log(f'presa de prueba: {INST} ({tipo}, {i.get("ctType")}, '
        f'settle {i.get("settleCcy", "?")}, estado {i.get("state")})')
    if i.get('state') != 'live':
        log(f'FATAL: el instrumento no esta live ({i.get("state")}).')
        sys.exit(1)
    try:
        d = okx_get(f'/api/v5/market/ticker?instId={INST}', auth=False)
        px = float(d[0]['last'])
    except Exception as e:
        log(f'FATAL: sin precio ({e}).')
        sys.exit(1)
    ct_cost = ct_val * px
    sz = mins
    while sz * ct_cost < MIN_NOTIONAL and sz < mins + 100 * lot:
        sz += lot
    log(f'disparo: {SIDE} {sz:g} ct x {ct_val:g} {COIN} @ {px} '
        f'= ~${sz * ct_cost:.2f} de collar | fee de ida y vuelta como '
        f'unico coste')

    if SIDE == 'buy':
        tp_px, sl_px = px * 1.06, px * 0.96
    else:
        tp_px, sl_px = px * 0.94, px * 1.04
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')
    cl = f'PRUEBA{COIN}{stamp}'[:32]
    orden = {'instId': INST, 'tdMode': MGN, 'side': SIDE,
             'ordType': 'market', 'sz': f'{sz:g}', 'clOrdId': cl,
             'tag': 'pruebacanon',
             'attachAlgoOrds': [{'tpTriggerPx': ronda(tp_px, tick, dec),
                                 'tpOrdPx': '-1',
                                 'slTriggerPx': ronda(sl_px, tick, dec),
                                 'slOrdPx': '-1'}]}
    if posmode == 'long_short_mode':
        orden['posSide'] = 'long' if SIDE == 'buy' else 'short'
    log(f'orden a mercado con collar atado: TP {ronda(tp_px, tick, dec)} '
        f'/ SL {ronda(sl_px, tick, dec)} (anchos, solo proteccion)')

    try:
        j = okx_post('/api/v5/trade/order', orden)
    except Exception as e:
        log(f'FATAL en el disparo: {e}')
        sys.exit(1)
    dd = (j.get('data') or [{}])[0]
    scode = dd.get('sCode', j.get('code', '?'))
    smsg = dd.get('sMsg', j.get('msg', ''))

    if scode != '0':
        if scode == '50124':
            log(f'VERDICTO: MURALLA 50124 confirmada — la llave NO tiene '
                f'permiso de comercio para este mercado. Solucion: nueva '
                f'key con alcance TODOS los mercados/criptos; si aun asi '
                f'falla, la muralla es de la cuenta (KYC L2 / acuerdo de '
                f'derivados / region).')
        elif scode == '51001':
            log(f'VERDICTO: 51001 instrumento inexistente — revisa el '
                f'instId ({INST}).')
        elif scode == '51119':
            log(f'VERDICTO: 51119 sin fondos suficientes — la llave '
                f'DISPARA (permisos OK), pero el arca no cubre el collar.')
        elif scode == '51016':
            log(f'VERDICTO: 51016 ya ordenado este ciclo — idempotencia, '
                f'paz.')
        else:
            log(f'VERDICTO: el disparo fue rechazado: {scode} {smsg}')
        sys.exit(1)

    log(f'BOOM: orden ACEPTADA (ordId {dd.get("ordId", "?")}, clOrdId '
        f'{cl}). La llave COMERCIA en {tipo}.')
    time.sleep(2)
    cl2 = f'RECOGE{COIN}{stamp}'[:32]
    recoge = {'instId': INST, 'mkt': 'market', 'autoCxl': False,
              'clOrdId': cl2, 'tag': 'pruebacanon'}
    if posmode == 'long_short_mode':
        recoge['posSide'] = 'long' if SIDE == 'buy' else 'short'
    try:
        k = okx_post('/api/v5/trade/close-position', recoge)
        dk = (k.get('data') or [{}])[0]
        kcode = dk.get('sCode', k.get('code', '?'))
        kmsg = dk.get('sMsg', k.get('msg', ''))
    except Exception as e:
        log(f'ATENCION: posicion de prueba ABIERTA y la recogida fallo '
            f'({e}). SL/TP anchos atados (+6%/-4%) la protegen; recoge a '
            f'mano cuando quieras.')
        sys.exit(1)
    if kcode == '0':
        log(f'RECOGIDA OK (ordId {dk.get("ordId", "?")}). Test completo: '
            f'el canon DISPARA y RECOGE. Fleet operativa con esta llave.')
    elif kcode == '51016':
        log(f'RECOGIDA: 51016 (nada que cerrar / ya ordenado). Paz.')
    else:
        log(f'ATENCION: recogida rechazada: {kcode} {kmsg}. La posicion '
            f'de prueba sigue con SL/TP atados; recoge a mano.')
        sys.exit(1)


if __name__ == '__main__':
    main()
