# main-hy.py — GSCSI TABLE v3.1 · C > Si
#   FLOTA v2: SUI · XRP · ADA · DOGE · INJ (XLM dique · FET expulsado)
#   v3.1 FIX: SALIDA_REF en unidades de PORCENTAJE (coherente con mov_pct)
#     — el bug v3 mezclaba decimal vs % → clasificaciones falsas
#   SALIDA_REF asimétrica por dirección (ley de cada destructor v2)
#   Clasificación: TP si |mov| >= 70% del TP · SL si >= 70% del SL ·
#     WIN/LOSS/BE si no cuadra
#   JSONL · dedup por huella · exit 1 en error
import os
import sys
import time
import json
import pathlib
import ccxt
from collections import defaultdict

# ══ AXIOMAS DEL CARBONO ══
BASES = {'SUI', 'XRP', 'ADA', 'DOGE', 'INJ'}   # flota v2
VETO  = 'USDT'
CADENCIA_HRS = 8

# Leyes v2 en UNIDADES DE PORCENTAJE (coherente con mov_pct)
#   formato: base → direccion → (SL%, TP%)
SALIDA_REF = {
    'SUI':  {'long': (4.0, 5.5),  'short': (3.0, 4.5)},
    'ADA':  {'long': (3.5, 5.0),  'short': (2.5, 4.0)},
    'DOGE': {'long': (2.0, 4.0),  'short': (3.0, 4.0)},
    'XRP':  {'long': (6.0, 7.5),  'short': (4.5, 6.0)},
    'INJ':  {'long': (7.0, 10.0), 'short': (5.0, 8.0)},
}

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
        after = data[-1].get(key)
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
    """Clasificación con la ley asimétrica v2 (unidades de %).
    TP si |mov| >= 70% del TP · SL si |mov| >= 70% del SL
    (en la dirección del trade). WIN/LOSS si no cuadra."""
    if r['liq'] > 0:
        return 'LIQ'
    m = mov_pct(r)
    ref = SALIDA_REF.get(base, {}).get(r['dir'])
    if ref is not None and m is not None:
        sl_ref, tp_ref = ref
        am = abs(m)
        if r['pnl'] > 0 and am >= tp_ref * 0.7:
            return 'TP'
        if r['pnl'] < 0 and am >= sl_ref * 0.7:
            return 'SL'
    if r['pnl'] > 0:
        return 'WIN'
    if r['pnl'] < 0:
        return 'LOSS'
    return 'BE'


def ffecha(ms):
    try:
        return time.strftime('%d %H:%M', time.gmtime(int(ms) / 1000))
    except (TypeError, ValueError):
        return '  ?   '


try:
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

    por_base = defaultdict(list)
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

    print("=" * 46, flush=True)
    print("  TABLA GSCSI S/L — FLOTA v2")
    print(f"  actualizacion automatica cada {CADENCIA_HRS} hrs")
    print("  fuente: positions-history (OKX Europe) · 1 cierre = 1 trade")
    print("  my.okx.com · MiCA/EEE · USDT: VETADO")
    print("  Leyes v2: asimetricas por direccion · candado anti-rango")
    print("  ⚰️ XLM: dique seco (MS-0) · ⚰️ FET: expulsado (hack)")
    print("=" * 46)
    print(f"  Operador : github.com/gennesis44")
    print(f"  Perfil   : {AVATAR_URL}")
    print(f"  Nodo raiz: {NODO_RAIZ}")
    print(f"  Nucleo   : {NODO_NUCLEO}")
    print(f"  Emitido  : {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    print("=" * 46)

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
                  f"mov:{ms}  {salida(r, base):<4} "
                  f"PnL:{r['pnl']:+.4f}  [{ffecha(r['uTime'])}]")
    print("-" * 46)
    total = TG + TL
    pct = f"{(TW / total * 100):.0f}%" if total else "-"
    print(f"TOTAL S:{TG}  L:{TL}  {pct}  PnL:{NETO:+.4f}")
    sh = [r for r in todos if r['dir'] == 'short']
    lo = [r for r in todos if r['dir'] == 'long']
    for nom, arr in (('SHORTS', sh), ('LONGS ', lo)):
        w_ = sum(1 for r in arr if r['pnl'] > 0)
        print(f"{nom}: {w_}W/{len(arr) - w_}L  "
              f"PnL:{sum(r['pnl'] for r in arr):+.4f}")

    acc = list(prev.values())
    if acc:
        aw  = sum(1 for r in acc if r['pnl'] > 0)
        ash = sum(1 for r in acc if r['dir'] == 'short')
        apnl = sum(r['pnl'] for r in acc)
        wins = [r['pnl'] for r in acc if r['pnl'] > 0]
        loss = [r['pnl'] for r in acc if r['pnl'] < 0]
        avgW = sum(wins) / len(wins) if wins else 0.0
        avgL = abs(sum(loss) / len(loss)) if loss else 0.0
        payoff = format(avgW / avgL, '.2f') if avgW > 0 and avgL > 0 else '-'
        peor = min(acc, key=lambda r: r['pnl'])
        mejor = max(acc, key=lambda r: r['pnl'])
        print("=" * 46)
        print(f"ACUMULADO: {len(acc)} trades · winrate {aw / len(acc) * 100:.0f}%")
        print(f"  shorts:{ash} · longs:{len(acc) - ash} · PnL:{apnl:+.4f}")
        print(f"  expectativa: {apnl / len(acc):+.4f}/trade · payoff: {payoff}")
        print(f"  peor: {peor.get('base','?')} {peor['pnl']:+.4f} · "
              f"mejor: {mejor.get('base','?')} {mejor['pnl']:+.4f}")

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
