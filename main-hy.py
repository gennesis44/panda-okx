# main-hy.py — GSCSI TABLE v4 (2026-10-09)
#   CAMBIO v4 (decreto Carbono — privacidad de resultados):
#     - NO muestra PnL por trade ni agregado. Solo direcciones (S/L) y W/L.
#     - NO commitea data/history.jsonl. El historial vive SOLO en OKX
#       (positions-history) y en el artifact del run (privado para el Carbono).
#   Lo que queda publico: que hay trades, en que direccion, y su veredicto.
#   Lo que queda privado: cuantos dolares mueven.
#
#   METODO (v3 intacto):
#     fuente: positions-history (OKX Europe) · 1 cierre = 1 trade
#     my.okx.com · MiCA/EEE · USDT: VETADO
#     leyes v2: asimetricas por direccion · candado anti-rango
#     XLM: dique seco (MS-0) · FET: expulsado (hack)
#     HBAR: DESTRUCTOR v2 (stack EMA 3/4/10/21/27 · 30m)
#   Ax3: fail-closed en cada lectura · sin opinion

import json
import os
import sys
import time
from collections import defaultdict

import ccxt

logging_ok = True
try:
    import logging
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')
    log = logging.getLogger(__name__)
except Exception:
    class _L:
        def info(self, m): print("INFO  " + m, flush=True)
        def warning(self, m): print("WARN  " + m, flush=True)
        def error(self, m): print("ERROR " + m, flush=True)
    log = _L()

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

# ==================== DECRETO ====================
BASE_ASSETS = ['ADA', 'DOGE', 'HBAR', 'INJ', 'SUI', 'XRP']  # flota viva
TIMEFRAME_DAYS = 90
FLEET_NAME = 'GSCSI-TRADE'

HOST = 'https://my.okx.com'

exchange = ccxt.okx({
    'apiKey':    os.getenv('OKX_API_KEY', ''),
    'secret':    os.getenv('OKX_SECRET_KEY', ''),
    'password':  os.getenv('OKX_PASSWORD', ''),
    'enableRateLimit': True,
    'options':   {'defaultType': 'swap'},
    'urls':      {'api': {'rest': HOST}},
})

# ==================== LECTURA (fail-closed) ====================
def fetch_all_closes():
    """Descarga cierres de positions-history paginando hacia atras."""
    all_closes = []
    after = None
    while True:
        params = {'instType': 'ANY', 'limit': '100'}
        if after:
            params['after'] = after
        resp = exchange.private_get_trade_position_history(params)
        rows = (resp or {}).get('data') or []
        if not rows:
            break
        for r in rows:
            try:
                inst = r.get('instId') or ''
                base = inst.split('-')[0] if inst else ''
                if base not in BASE_ASSETS:
                    continue
                direction = 'L' if str(r.get('posSide') or '') in ('long', 'net_long') else 'S'
                # veredicto del cierre
                r_pnl = _f(r.get('realizedPnl'))
                r_type = str(r.get('type') or '')
                verdict = 'WIN'
                if r_type == '2':           # cierre parcial/manual sin TP
                    verdict = 'TP' if r_pnl > 0 else 'LOSS'
                elif r_type == '3':
                    verdict = 'SL'
                elif r_type == '4':
                    verdict = 'TP'
                elif r_type == '5':
                    verdict = 'LOSS'
                elif r_type == '6':
                    verdict = 'TP'
                else:
                    verdict = 'WIN' if r_pnl > 0 else 'LOSS'
                all_closes.append({
                    'base': base,
                    'dir': direction,
                    'verdict': verdict,
                    'ts': int(r.get('uTime') or 0),
                })
            except Exception:
                continue
        if len(rows) < 100:
            break
        after = rows[-1].get('uTime')
        if not after:
            break
        time.sleep(0.2)
    return all_closes

# ==================== TABLA (sin PnL — v4) ====================
def build_table(closes):
    per = defaultdict(lambda: {'S': 0, 'L': 0, 'W': 0, 'loss': 0})
    for c in closes:
        p = per[c['base']]
        p[c['dir']] += 1
        if c['verdict'] in ('WIN', 'TP'):
            p['W'] += 1
        else:
            p['loss'] += 1

    total = {'S': 0, 'L': 0, 'W': 0, 'loss': 0}
    lines = []
    lines.append('=' * 46)
    lines.append('  GSCSI FLEET — VERDICT TABLE v4')
    lines.append('  source: positions-history (OKX Europe) · 1 close = 1 trade')
    lines.append('  my.okx.com · MiCA/EEE · USDT vetoed')
    lines.append('  privacy decree: counts only — no PnL, no amounts')
    lines.append('  XLM: dry dock (MS-0) · FET: expelled')
    lines.append('=' * 46)
    lines.append('  Operator : github.com/gennesis44')
    lines.append('  Emitted  : ' + time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime()))
    lines.append('=' * 46)

    order = sorted(per.keys(), key=lambda b: -(per[b]['W'] + per[b]['loss']))
    for b in order:
        p = per[b]
        n = p['W'] + p['loss']
        wr = p['W'] / n * 100 if n else 0.0
        total['S'] += p['S']; total['L'] += p['L']
        total['W'] += p['W']; total['loss'] += p['loss']
        lines.append('{:<5} S:{:<4} L:{:<4} {:>3} trades  WR {:>4.0f}%'.format(
            b, p['S'], p['L'], n, wr))

    lines.append('-' * 46)
    n_all = total['W'] + total['loss']
    wr_all = total['W'] / n_all * 100 if n_all else 0.0
    lines.append('TOTAL S:{:<4} L:{:<4} trades:{}  WR {:.0f}%'.format(
        total['S'], total['L'], n_all, wr_all))
    lines.append('=' * 46)
    lines.append('  1. Carbon fixes the rules.')
    lines.append('  2. Silicon executes without opinion.')
    lines.append('  3. The market issues the verdict.')
    lines.append('  4. Silicon returns the data.')
    lines.append('  5. Carbon decides: maintain, correct, or bury.')
    lines.append('  The loop closes and begins again.')
    lines.append('=' * 46)
    return '\n'.join(lines)

# ==================== MAIN ====================
def main():
    log.info('GSCSI TABLE v4 | privacy: counts only, no PnL | USDT vetoed')
    raw = exchange.private_get_account_balance()
    details = ((raw or {}).get('data') or [{}])[0].get('details') or []
    total = sum(_f(d.get('eqUsd')) for d in details)
    log.info('Auth OK | host=' + HOST + ' | collateral (USD): ~' + format(total, '.2f'))

    closes = fetch_all_closes()
    if not closes:
        log.info('No closes in window. Nothing to report.')
        table = build_table([])
    else:
        cutoff = (time.time() - TIMEFRAME_DAYS * 86400) * 1000
        recent = [c for c in closes if c['ts'] >= cutoff]
        table = build_table(recent)

    print(table)

    report = 'reporte.txt'
    with open(report, 'w') as fh:
        fh.write(table + '\n')

if __name__ == '__main__':
    main()
