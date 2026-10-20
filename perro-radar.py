#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# perro-radar.py — RADAR DEL PERRO (DOGE) · DOBLE TAREA INJ/SUI
#   v4 (decreto del Portaaviones, 20-oct: "usar al perro para AMBAS
#   tareas")
#
#   DOCTRINA (palabra del Capitan, literal):
#     "Doge no tiene presas privadas. Inj o sui."
#     "El perro si sale a buscar sus huesos y 've' a SUI o INJ en
#      condiciones favorables, doge hace zarpar a inj o sui o ambos."
#     "Atalaya.py tambien hace salir a inj y sui y toda la flota."
#     "Objetivo: usar al perro para AMBAS tareas."
#
#   TRADUCCION A ACERO — UN SOLO PERRO, DOS SENALES:
#     SENAL 1 — SALIR A MAR (los huesos): hay presa en el agua (posicion
#       abierta de INJ/SUI, sea del Perro o de quien sea: la presa en el
#       agua es presa). Tres gatillos:
#         1) TP     — PnL >= PERRO_TP_PCT (default 4%)
#         2) ESCUDO — PnL <= -PERRO_SL_PCT (default 3%)
#         3) ROTURA — ola 1H rompe EN CONTRA con sep >= PERRO_LOCK_PCT.
#       => close-position total a mercado.
#     SENAL 2 — ZARPAR: la presa esta en seco (sin posicion) y la ola
#       ABRE fresca en la ventana [-2,-3,-4] con sep >= PERRO_LOCK_PCT
#       (misma ley de flota que el Vigia: se caza la apertura, no la
#       continuacion). Direccion: sep>0 LONG, sep<0 SHORT.
#       => entrada a mercado con collar (PERRO_COLLAR_USD, default $10)
#          y SL/TP atados al canon de la flota:
#          LONG SL 2% / TP 4% · SHORT SL 3% / TP 4%.
#       INJ y SUI se evaluan por separado: pueden zarpar los 2 a la vez.
#
#   SIN PRIVANZA — SE COMPARTE PRESA, NO MANDO:
#     El Perro NO tiene presas privadas: INJ/SUI tambien salen con toda
#     la flota cuando la atalaya canta (sus cascos main-inj.py /
#     main-sui.py viven en raiz y LAUNCH_SHIPS los reconoce como
#     lanzables). El Perro va ADELANTE con SU latido (doge.yml, cada 15
#     min) y SU ley: no espera a la atalaya y no la sustituye. Dos
#     caminos al mismo puerto, sin pisarse.
#
#   SU COLLAR GPS (cosido al codigo, imposible de quitar):
#     a) LISTA DURA: presas fijadas en PERRO_PREY (default
#        INJ-USD-SWAP,SUI-USD-SWAP). Nada mas entra en el collar.
#     b) UN ESTADO, UNA MISION: sin posicion -> mira si hay que zarpar;
#        con posicion -> caza huesos. Jamas acumula presas sobre presas.
#     c) IDEMPOTENCIA: clOrdId = MORD/ZARPA+presa+vela cerrada. Re-run
#        en la misma vela = 51016 = ya ordenado = paz.
#     d) BITACORA TOTAL: cada decision queda lograda con PnL, sep y
#        razon. Fail-closed: sin lectura del mar, no grita.
#     e) PERRO_MODE=DRY: noche de solo ladridos (log, sin morder).
#     f) ENTRADAS LIMITADAS: collar $10 por presa (env) + SL/TP atados
#        en la misma orden (atomicos, disparador de mercado).
#
#   Notas de astillero:
#     - El nombre lleva "radar": OWN_MARKS del Vigia lo clasifica como
#       buque de apoyo — la catapulta JAMAS lo lanza como casco.
#     - La atalaya NO SE TOCA: su dict SHIPS y su ley quedan intactos.
#       Y si la presa ya esta cazando, el propio vigia la salta
#       ("YA EN LA MAR") — el freno anti-doble ya viene de fabrica.
#     - main-inj.py / main-sui.py NO se expurgan: son canoneros de la
#       atalaya. DOGE es su radar adelantado, no su dueno.
#     - btc-radar.py (norte-sur) sigue siendo buque de apoyo: intacto.
#
#   v4.1 (20-oct, tras el primer latido real en GitHub Actions):
#     a) INJ-USD-SWAP (coin-margined) NO EXISTE en OKX (51001). Nuevo
#        env PERRO_PREY para apuntar el collar sin tocar codigo.
#     b) Los errores HTTP de OKX (401, etc.) ahora muestran el codigo y
#        msg REALES de OKX en el log (antes un 401 crasheaba a ciegas).
#     c) Un fallo en una presa ya NO mata la ronda: zarpe/mordida caida
#        se loguea y el perro sigue con la siguiente presa.
#
#   v4.2 (20-oct, la presa INJ correcta):
#     a) La presa INJ viva es INJ-USD_UM_XPERP-310711 (X-Perp, settle
#        USD, vence 2031-07-11) — la del "INJUSD UM (X-Perp)" de la app
#        del Capitan y la MISMA que comercia main-inj.py via ccxt
#        (INJ/USD:USD-310711). Ni el perp coin-margined (deslistado)
#        ni el USDT (fuera de doctrina). Collar via PERRO_PREY.
#     b) El perro ahora LEE posiciones de SWAP y FUTURES (los huesos
#        del X-Perp son huesos) y saca la ficha del instrumento de
#        ambos tipos.

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys

import requests

HOST = 'https://my.okx.com'

# ---- COLLAR GPS: presas (env PERRO_PREY por si el Capitan cambia una) ----
PREY = tuple(p.strip() for p in os.getenv(
    'PERRO_PREY', 'INJ-USD-SWAP,SUI-USD-SWAP').split(',') if p.strip())

TP_PCT = float(os.getenv('PERRO_TP_PCT', '4.0'))
SL_PCT = float(os.getenv('PERRO_SL_PCT', '3.0'))
LOCK_PCT = float(os.getenv('PERRO_LOCK_PCT', '0.15'))
MODE = os.getenv('PERRO_MODE', 'LIVE').strip().upper()
TF = os.getenv('PERRO_TF', '1H').strip()  # mapa OKX: horas MAYUSCULAS
# --- canon del zarpe (Senal 2) ---
COLLAR_USD = float(os.getenv('PERRO_COLLAR_USD', '10.0'))
SL_LONG_PCT = float(os.getenv('PERRO_SL_LONG_PCT', '2.0'))
SL_SHORT_PCT = float(os.getenv('PERRO_SL_SHORT_PCT', '3.0'))
LEVER = os.getenv('PERRO_LEVER', '').strip()
MGN = os.getenv('PERRO_MGN', 'cross').strip().lower() or 'cross'

AK = os.getenv('OKX_API_KEY', '')
SK = os.getenv('OKX_SECRET_KEY', '')
PW = os.getenv('OKX_PASSWORD', '')


def log(msg):
    print('[DOGE] ' + msg, flush=True)


def okx_headers(method, path, body=''):
    ts = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec='milliseconds').replace('+00:00', 'Z')
    sign = base64.b64encode(hmac.new(
        SK.encode(), f'{ts}{method}{path}{body}'.encode(),
        hashlib.sha256).digest()).decode()
    return {'OK-ACCESS-KEY': AK, 'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': ts, 'OK-ACCESS-PASSPHRASE': PW,
            'Content-Type': 'application/json'}


def okx_get(path, auth=True):
    headers = okx_headers('GET', path) if auth \
        else {'Content-Type': 'application/json'}
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


def ema(vals, n):
    k = 2.0 / (n + 1.0)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1.0 - k)
    return e


def ema_series(vals, n):
    k = 2.0 / (n + 1.0)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def sep_1h(inst_id):
    """Sep EMA3/21 sobre velas CERRADAS (ley de flota, sin vela viva).
    Devuelve (sep_actual, [seps cronologicas], ts vela cerrada, e3, e21)."""
    data = okx_get(f'/api/v5/market/candles?instId={inst_id}'
                   f'&bar={TF}&limit=40', auth=False)
    rows = [r for r in data if len(r) > 4]
    if len(rows) < 25:
        raise RuntimeError(f'pocas velas ({len(rows)})')
    closes = [float(r[4]) for r in reversed(rows[1:])]  # rows[0] = vela viva
    last_ts = int(rows[1][0])  # vela cerrada mas nueva
    e3s = ema_series(closes, 3)
    e21s = ema_series(closes, 21)
    seps = [(a - b) / b * 100.0 for a, b in zip(e3s, e21s)]
    return seps[-1], seps, last_ts, e3s[-1], e21s[-1]


def tick_decimals(tick_str):
    s = tick_str.lower()
    if '.' in s and 'e' not in s:
        d = s.split('.')[1].rstrip('0')
        return len(d) if d else 1
    return 8


def ronda(px, tick, dec):
    return f'{round(round(px / tick) * tick, dec):.{dec}f}'


def instrumento(inst_id):
    """Ficha del instrumento: perp (SWAP) primero, X-Perp/futuro
    (FUTURES) despues — la presa puede vivir en cualquiera de los dos."""
    for t in ('SWAP', 'FUTURES'):
        try:
            d = okx_get(f'/api/v5/public/instruments?instType={t}'
                        f'&instId={inst_id}', auth=False)
        except Exception:
            d = []
        if d:
            i = d[0]
            return (float(i['ctVal']), float(i['lotSz']),
                    float(i['minSz']), i['tickSz'])
    raise RuntimeError(f'{inst_id}: instrumento desconocido')


def cruce_fresco(seps):
    """El cruce debe haber nacido en la ventana [-2,-3,-4] de velas
    cerradas (ley de flota: se caza la apertura, no la continuacion)."""
    n = len(seps)
    for w in (-2, -3, -4):
        i = n + w
        if i < 1:
            continue
        antes, ahora = seps[i - 1], seps[i]
        if (antes <= 0 < ahora) or (antes >= 0 > ahora):
            return i
    return None


def zarpar(inst, coin, posmode):
    """SENAL 2 — AVISO DE ZARPAR: presa en seco + ola abriendo fresca
    con sep >= candado => entra a mercado con collar y SL/TP atados."""
    try:
        sep, seps, last_ts, e3, e21 = sep_1h(inst)
    except Exception as e:
        if '51001' in str(e):
            log(f'[{coin}] presa INEXISTENTE en OKX (deslistada o mal '
                f'escrita). El perro la da por muerta esta ronda.')
        else:
            log(f'[{coin}] collar: sin lectura del mar ({e}). Fail-closed.')
        return
    nacimiento = cruce_fresco(seps)
    lado = 'LONG' if sep > 0 else 'SHORT'
    log(f'[{coin}] radar: sep {sep:+.2f}% | EMA3/21 {TF}: '
        f'{e3:.6g}/{e21:.6g} | cruce fresco: '
        f'{"si (en ventana)" if nacimiento else "no (ola vieja)"}')
    if not nacimiento or abs(sep) < LOCK_PCT:
        log(f'[{coin}] SOSTIENE: sin condiciones optimas para zarpar. '
            f'El perro espera tumbado.')
        return
    try:
        ct_val, lot, mins, tick_s = instrumento(inst)
        tick = float(tick_s)
        dec = tick_decimals(tick_s)
        d = okx_get(f'/api/v5/market/ticker?instId={inst}', auth=False)
        px = float(d[0]['last'])
    except Exception as e:
        log(f'[{coin}] fail-closed: no puedo leer la presa ({e}).')
        return
    sz = int((COLLAR_USD / (ct_val * px)) / lot) * lot
    if sz < mins:
        log(f'[{coin}] no cabe en el collar (${COLLAR_USD:g}): '
            f'1 ct = {ct_val:g} {coin} ~ ${ct_val * px:.2f}. Fail-closed.')
        return
    if lado == 'LONG':
        sl_px = px * (1 - SL_LONG_PCT / 100.0)
        tp_px = px * (1 + TP_PCT / 100.0)
    else:
        sl_px = px * (1 + SL_SHORT_PCT / 100.0)
        tp_px = px * (1 - TP_PCT / 100.0)
    stamp = dt.datetime.fromtimestamp(last_ts / 1000, dt.timezone.utc)\
        .strftime('%Y%m%d%H%M')
    cl = f'ZARPA{coin}{stamp}'[:32]
    orden = {'instId': inst, 'tdMode': MGN,
             'side': 'buy' if lado == 'LONG' else 'sell',
             'ordType': 'market', 'sz': f'{sz:g}', 'clOrdId': cl,
             'tag': 'dogeradar',
             'attachAlgoOrds': [{'tpTriggerPx': ronda(tp_px, tick, dec),
                                 'tpOrdPx': '-1',
                                 'slTriggerPx': ronda(sl_px, tick, dec),
                                 'slOrdPx': '-1'}]}
    if posmode == 'long_short_mode':
        orden['posSide'] = 'long' if lado == 'LONG' else 'short'
    if LEVER:
        try:
            lev = {'instId': inst, 'lever': str(LEVER), 'mgnMode': MGN}
            if posmode == 'long_short_mode':
                lev['posSide'] = orden['posSide']
            okx_post('/api/v5/account/set-leverage', lev)
        except Exception as e:
            log(f'[{coin}] apalancamiento no fijado ({e}); '
                f'sigue el que tenga el arca.')
    resumen = (f'ZARPA {lado} {sz:g} ct (~${sz * ct_val * px:.2f}) | '
               f'SL {ronda(sl_px, tick, dec)} / '
               f'TP {ronda(tp_px, tick, dec)} atados')
    if MODE == 'DRY':
        log(f'[{coin}] LADRA: AVISO DE ZARPAR {resumen} | DRY: solo ladrido.')
        return
    try:
        j = okx_post('/api/v5/trade/order', orden)
    except Exception as e:
        log(f'[{coin}] fallo de zarpe (no mata la ronda): {e}')
        return
    dd = (j.get('data') or [{}])[0]
    scode = dd.get('sCode', j.get('code', '?'))
    smsg = dd.get('sMsg', j.get('msg', ''))
    if scode == '0':
        log(f'[{coin}] SENAL EMITIDA: {resumen} | clOrdId {cl}')
    elif scode == '51016':
        log(f'[{coin}] ya ordenado este ciclo (51016): idempotencia. Paz.')
    else:
        log(f'[{coin}] fallo de zarpe: {scode} {smsg}')


def cazar(posmode):
    posiciones = (okx_get('/api/v5/account/positions?instType=SWAP')
                  + okx_get('/api/v5/account/positions?instType=FUTURES'))
    vivas = {p['instId']: p for p in posiciones
             if p.get('instId') in PREY and abs(float(p.get('pos') or 0)) > 0}
    log(f'perro: salidas TP>={TP_PCT}% | escudo<=-{SL_PCT}% | '
        f'rotura>={LOCK_PCT}% en contra | zarpar: cruce fresco en '
        f'[-2,-3,-4] con sep>={LOCK_PCT}% | collar ${COLLAR_USD:g} | '
        f'modo {MODE} | mar {TF}')
    salidas = 0
    for inst in PREY:
        coin = inst.split('-')[0]
        p = vivas.get(inst)
        if not p:
            zarpar(inst, coin, posmode)
            continue
        pos = float(p['pos'])
        raw_side = p.get('posSide', 'net')
        hedge = raw_side in ('long', 'short')
        side = raw_side.upper() if hedge else \
            ('LONG' if pos > 0 else 'SHORT')
        upl = float(p.get('upl') or 0.0)
        notional = float(p.get('notionalUsd') or 0.0)
        if notional:
            pnl = upl / notional * 100.0
        else:
            pnl = float(p.get('uplRatio') or 0.0) * 100.0
        avg = p.get('avgPx', '?')
        try:
            sep, seps, last_ts, e3, e21 = sep_1h(inst)
        except Exception as e:
            if '51001' in str(e):
                log(f'[{coin}] presa INEXISTENTE en OKX (deslistada o '
                    f'mal escrita). El perro la da por muerta esta ronda.')
            else:
                log(f'[{coin}] collar: sin lectura del mar ({e}). '
                    f'Fail-closed: no muerde.')
            continue
        log(f'[{coin}] GPS: pos {side} {abs(pos):g} ct @ {avg} | '
            f'PnL {pnl:+.2f}% | EMA3/21 {TF}: {e3:.6g}/{e21:.6g} '
            f'(sep {sep:+.2f}%)')
        razon = None
        if pnl >= TP_PCT:
            razon = f'TP (PnL {pnl:+.2f}% >= {TP_PCT}%)'
        elif pnl <= -SL_PCT:
            razon = f'ESCUDO (PnL {pnl:+.2f}% <= -{SL_PCT}%)'
        elif side == 'LONG' and sep <= -LOCK_PCT:
            razon = f'ROTURA en contra (LONG, sep {sep:+.2f}%' \
                    f' <= -{LOCK_PCT}%)'
        elif side == 'SHORT' and sep >= LOCK_PCT:
            razon = f'ROTURA en contra (SHORT, sep {sep:+.2f}%' \
                    f' >= +{LOCK_PCT}%)'
        if not razon:
            log(f'[{coin}] SOSTIENE: condiciones no optimas. '
                f'El perro espera tumbado.')
            continue
        log(f'[{coin}] LADRA: {razon}')
        if MODE == 'DRY':
            log(f'[{coin}] DRY: solo ladrido, no muerde.')
            continue
        stamp = dt.datetime.fromtimestamp(last_ts / 1000, dt.timezone.utc)\
            .strftime('%Y%m%d%H%M')
        cl = f'MORD{coin}{stamp}'[:32]
        payload = {'instId': inst, 'mkt': 'market', 'autoCxl': False,
                   'clOrdId': cl, 'tag': 'dogeradar'}
        if hedge:
            payload['posSide'] = raw_side
        try:
            j = okx_post('/api/v5/trade/close-position', payload)
        except Exception as e:
            log(f'[{coin}] fallo de mordida (no mata la ronda): {e}')
            continue
        d = (j.get('data') or [{}])[0]
        scode = d.get('sCode', j.get('code', '?'))
        smsg = d.get('sMsg', j.get('msg', ''))
        if scode == '0':
            salidas += 1
            log(f'[{coin}] SENAL EMITIDA: SALIR A MAR. '
                f'Salida total a mercado ejecutada '
                f'(clOrdId {cl}, ordId {d.get("ordId", "?")})')
        elif scode == '51016':
            log(f'[{coin}] ya ordenado este ciclo (51016): '
                f'idempotencia del collar. Paz.')
        else:
            log(f'[{coin}] fallo de mordida: {scode} {smsg}')
    return salidas


def main():
    log('=== RADAR DEL PERRO (DOGE) — DOBLE TAREA: huesos de salida y '
        'aviso de zarpar para INJ/SUI (presas compartidas, sin privanza; '
        'la atalaya tambien saca la flota). Va suelto; el Portaaviones '
        'mira ===')
    if not (AK and SK and PW):
        log('sin llaves del arca: el perro se queda en la perrera. '
            'Fail-closed.')
        sys.exit(1)
    try:
        cfg = okx_get('/api/v5/account/config')
        posmode = cfg[0].get('posMode', 'net_mode') if cfg else 'net_mode'
        log(f'arca en modo posicion: {posmode}')
        salidas = cazar(posmode)
    except RuntimeError as e:
        log(f'FATAL: {e}')
        sys.exit(1)
    log(f'RADAR del Perro cerrado: senales de SALIDA: {salidas}. '
        f'DOGE vuelve a su cubil. El Portaaviones mira desde el oeste '
        f'de Japon.')


if __name__ == '__main__':
    main()
