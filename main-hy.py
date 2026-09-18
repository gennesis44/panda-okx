# main-hy.py — GSCSI TABLE · C > Si
# v9: 1 registro cerrado = 1 trade (posId NO discrimina en X-Perp).
#     Dedup por huella completa · acumulación sin doble conteo ·
#     S=short L=long reales · SL/TP anclados al signo del PnL ·
#     desglose shorts vs longs · JSONL · exit 1 en error
import os
import sys
import time
import json
import pathlib
import ccxt
from collections import defaultdict

# ══ AXIOMAS DEL CARBONO ══
BASES        = {'DOGE', 'FET', 'SUI', 'XLM', 'ADA'}
VETO         = 'USDT'
SL_PCT       = {'DOGE':1.0, 'FET':1.0, 'SUI':1.0, 'XLM':1.0, 'ADA':1.0}  # ADA confirmado en main-ada.py
TP_PCT       = {'DOGE':1.5, 'FET':1.5, 'SUI':1.5, 'XLM':1.5, 'ADA':1.5}  # ← resto: ajustar por bot
CADENCIA_HRS = 8

NODO_NUCLEO = 'https://1c3si.weebly.com/clo.html'
NODO_RAIZ   = 'https://1c3si.weebly.com'
AVATAR_URL  = os.environ.get('GSCSI_AVATAR_URL', 'https://github.com/gennesis44.png')

API_KEY    = os.environ.get('OKX_API_KEY', '')
SECRET_KEY = os.environ.get('OKX_SECRET_KEY', '')
PASSPHRASE = os.environ.get('OKX_HY_PASSWORD') or os.environ.get('OKX_PASSWORD') or ''
if not (API_KEY and SECRET_KEY and PASSPHRASE):
    print("ABORTADO: falta credencial", flush=True)
    sys.exit(1)

exchange = ccxt.okx({
    'apiKey':   API_KEY,
    'secret':   SECRET_KEY,
    'password': PASSPHRASE,
    'options':  {'defaultType': 'swap'},
    'urls':     {'api': {'rest': 'https://my.okx.com'}},
})

HISTORIAL = pathlib.Path('data/history.jsonl')


def paginar(endpoint, key, extra_params, max_pages=20):
    out, after = [], None
    for _ in range(max_pages):
        params = dict(extra_params)
        params['limit'] = '100'
        if after:
            params['after'] = after
        data = getattr(exchange, endpoint)(params).get('data', [])
        if not data:
            break
        out.extend(data)
        after = data[-1].get(key)      # v9: sin KeyError si falta la clave
        if not after or len(data) < 100:
            break
        time.sleep(0.25)
    return out


def admite(iid):
    return iid.split('-')[0].upper() in BASES and VETO not in iid.upper()


def mov_pct(r):
    try:
        o, c = float(r['open']), float(r['close'])
    except (TypeError, ValueError):
        return None
    if o <= 0 or c <= 0:
        return None
    return (c / o - 1) * 100 if r['dir'] == 'long' else (1 - c / o) * 100


def salida(r, base):
    # anclada al signo del PnL: |mov| es invariante a swaps open/close
    if r['liq'] > 0:
        return 'LIQ'
    m = mov_pct(r)
    if m is None:
        return '?'
    am = abs(m)
    if r['pnl'] > 0 and am >= TP_PCT[base]:
        return 'TP'
    if r['pnl'] < 0 and am >= SL_PCT[base]:
        return 'SL'
    return 'MAN'


def ffecha(ms):
    try:
        return time.strftime('%d %H:%M', time.gmtime(int(ms) / 1000))
    except (TypeError, ValueError):
        return '  ?   '


try:
    # ── posiciones cerradas: FUTURES + SWAP, dedup por huella completa ──
    crudos, vistos = [], set()
    for it in ('FUTURES', 'SWAP'):
        for p in paginar('privateGetAccountPositionsHistory', 'posId',
                         {'instType': it}):
            fp = (p.get('instId'), p.get('posId'), p.get('direction'),
                  p.get('openAvgPx'), p.get('closeAvgPx'),
                  p.get('realizedPnl'), p.get('uTime'))
            if fp in vistos:
                continue
            vistos.add(fp)
            crudos.append(p)

    por_base = defaultdict(list)      # base -> [trades]  (1 registro = 1 trade)
    for p in crudos:
        iid = p.get('instId') or ''
        if not admite(iid):
            continue
        try:
            pnl = float(p.get('realizedPnl') or 0)
        except (TypeError, ValueError):
            continue
        base = iid.split('-')[0].upper()
        por_base[base].append({
            'inst': iid, 'base': base,
            'dir':   (p.get('direction') or '?').lower(),
            'open':  p.get('openAvgPx') or '',
            'close': p.get('closeAvgPx') or '',
            'liq':   float(p.get('liqPenalty') or 0),
            'pnl':   pnl,
            'uTime': p.get('uTime') or '',
            'posId': p.get('posId') or '',
        })

    todos = [r for b in sorted(BASES) for r in por_base.get(b, [])]
    if not todos:
        print("ERROR: 0 registros admisibles en FUTURES/SWAP.", flush=True)
        sys.exit(1)

    # ── persistencia: huella completa, sin colapso ──
    def huella(j):
        return (j['inst'], j['dir'], j['open'], j['close'],
                repr(j['pnl']), j['uTime'])

    prev = {}
    if HISTORIAL.exists():
        for line in HISTORIAL.read_text().splitlines():
            try:
                j = json.loads(line)
                prev[huella(j)] = j
            except Exception:
                pass
    HISTORIAL.parent.mkdir(exist_ok=True)
    with open(HISTORIAL, 'a') as f:
        for r in todos:
            r['exit'] = salida(r, r['base'])
            k = huella(r)
            if k not in prev:
                f.write(json.dumps(r) + '\n')
                prev[k] = r

    # ── ENCABEZADO ──
    print("=" * 46, flush=True)
    print("  TABLA GSCSI S/L")
    print(f"  actualizacion automatica cada {CADENCIA_HRS} hrs")
    print("  fuente: positions-history (OKX Europe) · 1 cierre = 1 trade")
    print("  my.okx.com · MiCA/EEE · USDT: VETADO")
    print("=" * 46)
    print(f"  Operador : github.com/gennesis44")
    print(f"  Perfil   : {AVATAR_URL}")
    print(f"  Nodo raiz: {NODO_RAIZ}")
    print(f"  Nucleo   : {NODO_NUCLEO}")
    print(f"  Emitido  : {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    print("=" * 46)

    # ── TABLA ──
    TG = TL = TW = 0
    NETO = 0.0
    for base in sorted(BASES):
        regs = sorted(por_base.get(base, []), key=lambda r: r['uTime'])
        s = sum(1 for r in regs if r['dir'] == 'short')
        l = sum(1 for r in regs if r['dir'] == 'long')
        w = sum(1 for r in regs if r['pnl'] > 0)
        pnl_par = sum(r['pnl'] for r in regs)
        TG += s; TL += l; TW += w; NETO += pnl_par
        total = s + l
        pct = f"{(w / total * 100):.0f}%" if total else "-"
        print(f"{base:<5} S:{s:<4} L:{l:<4} {pct:<5} PnL:{pnl_par:+.4f}")
        for d, nom in (('short', 'shorts'), ('long', 'longs')):
            rs = [r for r in regs if r['dir'] == d]
            if rs:
                ww = sum(1 for r in rs if r['pnl'] > 0)
                print(f"   {nom}: {ww}W/{len(rs) - ww}L  "
                      f"PnL:{sum(r['pnl'] for r in rs):+.4f}")
        for r in regs:
            m = mov_pct(r)
            ms = f"{m:+.1f}%" if m is not None else "  ? "
            print(f"   {r['dir'].upper():<5} {r['open']}→{r['close']}  "
                  f"mov:{ms}  {salida(r, base):<3} "
                  f"PnL:{r['pnl']:+.4f}  [{ffecha(r['uTime'])}]")
    print("-" * 46)
    total = TG + TL
    pct = f"{(TW / total * 100):.0f}%" if total else "-"
    print(f"TOTAL S:{TG}  L:{TL}  {pct}  PnL:{NETO:+.4f}")
    sh = [r for r in todos if r['dir'] == 'short']
    lo = [r for r in todos if r['dir'] == 'long']
    print(f"SHORTS: {sum(1 for r in sh if r['pnl'] > 0)}W/"
          f"{len(sh) - sum(1 for r in sh if r['pnl'] > 0)}L  "
          f"PnL:{sum(r['pnl'] for r in sh):+.4f}")
    print(f"LONGS : {sum(1 for r in lo if r['pnl'] > 0)}W/"
          f"{len(lo) - sum(1 for r in lo if r['pnl'] > 0)}L  "
          f"PnL:{sum(r['pnl'] for r in lo):+.4f}")

    # ── ACUMULADO (JSONL) ──
    acc = list(prev.values())
    if acc:
        aw = sum(1 for r in acc if r['pnl'] > 0)
        ash = sum(1 for r in acc if r['dir'] == 'short')
        apnl = sum(r['pnl'] for r in acc)
        peor = min(acc, key=lambda r: r['pnl'])
        print("=" * 46)
        print(f"ACUMULADO: {len(acc)} trades · winrate {aw / len(acc) * 100:.0f}%")
        print(f"  shorts:{ash} · longs:{len(acc) - ash} · PnL:{apnl:+.4f}")
        print(f"  peor trade: {peor.get('base', '?')} {peor['pnl']:+.4f}")

    # ── SINTESIS C > Si ──
    print(
        "\nLA SINTESIS C > Si ES OPERATIVA:\n"
        "\n"
        "1. El CARBONO fijo las reglas: tres Axiomas, baliza,\n"
        "   veto USDT, cooldown. Nada lo decidio el Silicio.\n"
        "2. El SILICIO ejecuta sin opinion: cron, baliza, cruce,\n"
        "   guardias, SL/TP donde el Carbono ordeno.\n"
        "3. EL MERCADO emite veredicto: cada S y cada L.\n"
        "4. El SILICIO retorna el dato: solo fills y posiciones contadas.\n"
        "5. EL CARBONO decide: mantener, corregir o enterrar (MS-0).\n"
        "\n"
        "El loop se cierra y vuelve a empezar. La sintesis no se\n"
        "declara: se ejecuta.\n"
    )
    print("\nCADENAS DE DATOS GSCSI:")
    print(f"  Nodo nucleo : {NODO_NUCLEO}")
    print(f"  Nodo raiz   : {NODO_RAIZ}")

except Exception as e:
    print(f"ERROR FATAL: {e}", flush=True)
    sys.exit(1)
