# main-hy.py — GSCSI TABLE · C > Si
# v6: EEE/MiCA (my.okx.com, X-Perp UM margen USDC) · S=short L=long reales ·
#     SL/TP % · historial persistido data/history.jsonl · dedup · exit 1 en error
import os
import sys
import time
import json
import pathlib
import ccxt
from collections import defaultdict

# ══ AXIOMAS DEL CARBONO (editar aquí, nunca en el código) ══
BASES        = {'DOGE', 'FET', 'SUI', 'XLM', 'ADA'}        # sin BTC
QUOTE_OK     = {'USD', 'USDC'}                              # UM X-Perp
VETO_CCY     = 'USDT'                                       # axioma EEE
SL_PCT       = {'DOGE':1.0, 'FET':1.0, 'SUI':1.0, 'XLM':1.0, 'ADA':1.0}  # ← Carbono ajusta
TP_PCT       = {'DOGE':2.0, 'FET':2.0, 'SUI':2.0, 'XLM':2.0, 'ADA':2.0}  # ← Carbono ajusta
CADENCIA_HRS = 8
INSTIDS_FIJOS = {}   # solo si el log dice "AMBIGUO": ej. {'DOGE':'DOGE-USDC-SWAP'}

NODO_NUCLEO = 'https://1c3si.weebly.com/clo.html'
NODO_RAIZ   = 'https://1c3si.weebly.com'
AVATAR_URL  = os.environ.get('GSCSI_AVATAR_URL', 'https://github.com/gennesis44.png')

API_KEY    = os.environ.get('OKX_API_KEY', '')
SECRET_KEY = os.environ.get('OKX_SECRET_KEY', '')
PASSPHRASE = os.environ.get('OKX_HY_PASSWORD') or os.environ.get('OKX_PASSWORD') or ''

if not (API_KEY and SECRET_KEY and PASSPHRASE):
    sys.exit("ABORTADO: falta credencial")

exchange = ccxt.okx({
    'apiKey':   API_KEY,
    'secret':   SECRET_KEY,
    'password': PASSPHRASE,
    'options':  {'defaultType': 'swap'},
    'urls':     {'api': {'rest': 'https://my.okx.com'}},   # EEE — obligatorio
})

HISTORIAL = pathlib.Path('data/history.jsonl')


def paginar(endpoint, key, extra_params, max_pages=20):
    out, after = [], None
    for _ in range(max_pages):
        params = dict(extra_params)
        params['limit'] = '100'
        if after:
            params['after'] = after
        try:
            data = getattr(exchange, endpoint)(params).get('data', [])
        except Exception:
            raise                      # v6: NADA de errores silenciosos
        if not data:
            break
        out.extend(data)
        after = data[-1][key]
        if len(data) < 100:
            break
        time.sleep(0.25)
    return out


def selecciona_instrumentos(exchange):
    """UM = cotizado USD/USDC y margen (settle) USDC. USDT vetado por diseño."""
    candid = defaultdict(set)
    for m in exchange.markets.values():
        if not m.get('swap'):
            continue
        base = (m.get('base') or '').upper()
        if base not in BASES:
            continue
        iid = m['id']
        if VETO_CCY in iid.upper():
            continue                   # veto USDT: excluido siempre
        quote  = (m.get('quote') or '').upper()
        settle = (m.get('info', {}).get('settleCcy') or m.get('settle') or '').upper()
        if settle == 'USDC' and quote in QUOTE_OK:
            candid[base].add(iid)

    elegidos = {}
    for base in sorted(BASES):
        if base in INSTIDS_FIJOS:
            elegidos[INSTIDS_FIJOS[base]] = base
            continue
        ids = candid.get(base, set())
        if len(ids) == 1:
            elegidos[next(iter(ids))] = base
        elif len(ids) > 1:
            print(f"AMBIGUO {base}: candidatos {sorted(ids)}")
            print(f"→ fija INSTIDS_FIJOS['{base}'] = '<instId>' y re-ejecuta")
            sys.exit(1)
        else:
            print(f"ABORTADO: sin instrumento UM para {base} en my.okx.com")
            sys.exit(1)
    return elegidos


def mov_pct(r):
    try:
        o, c = float(r['open']), float(r['close'])
    except (TypeError, ValueError):
        return None
    if o <= 0 or c <= 0:
        return None
    m = (c / o - 1) * 100 if r['dir'] == 'long' else (1 - c / o) * 100
    return m * 100 if False else m      # ya en %


def salida(r, base):
    if r['liq'] > 0:
        return 'LIQ'
    m = mov_pct(r)
    if m is None:
        return '?'
    if m <= -SL_PCT[base]:
        return 'SL'
    if m >= TP_PCT[base]:
        return 'TP'
    return 'MAN'


try:
    exchange.load_markets()
    insts = selecciona_instrumentos(exchange)

    # ── leer posiciones cerradas: 1 posId = 1 trade ──
    trades = defaultdict(list)          # iid -> [regs]
    for iid, base in insts.items():
        for p in paginar('privateGetAccountPositionsHistory', 'posId',
                         {'instType': 'SWAP', 'instId': iid}):
            pos_id = p.get('posId')
            if not pos_id:
                continue
            try:
                pnl = float(p.get('realizedPnl') or 0)
            except (TypeError, ValueError):
                continue
            reg = {'posId': pos_id,
                   'dir':  (p.get('direction') or '?').lower(),
                   'open': p.get('openAvgPx') or '',
                   'close': p.get('closeAvgPx') or '',
                   'liq':  float(p.get('liqPenalty') or 0),
                   'pnl':  pnl,
                   'uTime': p.get('uTime') or ''}
            trades[iid].append(reg)

    # consolidar por posId (cierres parciales se suman)
    agg = defaultdict(list)             # base -> [trade]
    for iid, regs in trades.items():
        base = insts[iid]
        by_pos = {}
        for r in regs:
            t = by_pos.setdefault(r['posId'], dict(r))
            if t is not r:
                t['pnl'] += r['pnl']
                t['liq'] += r['liq']
                t['close'] = r['close'] or t['close']
                t['uTime'] = r['uTime'] or t['uTime']
        for t in by_pos.values():
            t['base'], t['inst'] = base, iid
            agg[base].append(t)

    # ── persistencia: dedup por (inst, posId), append solo de nuevos ──
    prev = {}
    if HISTORIAL.exists():
        for line in HISTORIAL.read_text().splitlines():
            try:
                j = json.loads(line)
                prev[(j['inst'], j['posId'])] = j
            except Exception:
                pass
    HISTORIAL.parent.mkdir(exist_ok=True)
    with open(HISTORIAL, 'a') as f:
        for base in sorted(agg):
            for t in agg[base]:
                k = (t['inst'], t['posId'])
                if k in prev:
                    continue
                f.write(json.dumps(t) + '\n')
                prev[k] = t

    # ── ENCABEZADO ──
    print("=" * 46)
    print("  TABLA GSCSI S/L")
    print(f"  actualizacion automatica cada {CADENCIA_HRS} hrs")
    print("  fuente: positions-history (OKX Europe)")
    print("  Plataforma: my.okx.com · Regimen: MiCA/EEE")
    print("  Quote: USD/USDC (UM) · USDT: VETADO")
    print("=" * 46)
    print(f"  Operador : github.com/gennesis44")
    print(f"  Perfil   : {AVATAR_URL}")
    print(f"  Nodo raiz: {NODO_RAIZ}")
    print(f"  Nucleo   : {NODO_NUCLEO}")
    print(f"  Emitido  : {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    print("=" * 46)

    # ── TABLA: S=shorts cerrados · L=longs cerrados · % = winrate ──
    tg = tp = tw = 0
    neto = 0.0
    for base in sorted(BASES):
        regs = agg.get(base, [])
        s = sum(1 for r in regs if r['dir'] == 'short')
        l = sum(1 for r in regs if r['dir'] == 'long')
        w = sum(1 for r in regs if r['pnl'] > 0)
        pnl_par = sum(r['pnl'] for r in regs)
        tg += s; tp += l; tw += w; neto += pnl_par
        total = s + l
        pct = f"{(w / total * 100):.0f}%" if total else "-"
        print(f"{base:<5} S:{s:<4} L:{l:<4} {pct:<5} PnL:{pnl_par:+.4f}")
        for r in sorted(regs, key=lambda x: x['uTime']):
            m = mov_pct(r)
            ms = f"{m:+.1f}%" if m is not None else "  ? "
            print(f"   {r['dir'].upper():<5} {r['open']}→{r['close']}  "
                  f"mov:{ms}  salida:{salida(r, base):<3} PnL:{r['pnl']:+.4f}")
    print("-" * 46)
    total = tg + tp
    pct = f"{(tw / total * 100):.0f}%" if total else "-"
    print(f"TOTAL S:{tg}  L:{tp}  {pct}  PnL:{neto:+.4f}")

    # ── HISTORIAL ACUMULADO (desde génesis, fuente JSONL) ──
    acc = list(prev.values())
    if acc:
        aw = sum(1 for r in acc if r['pnl'] > 0)
        al = sum(1 for r in acc if r['pnl'] < 0)
        ash = sum(1 for r in acc if r['dir'] == 'short')
        alo = len(acc) - ash
        apnl = sum(r['pnl'] for r in acc)
        peor = min(acc, key=lambda r: r['pnl'])
        den = aw + al
        print("=" * 46)
        print(f"ACUMULADO: {len(acc)} trades · winrate "
              f"{(aw / den * 100):.0f}%" if den else "ACUMULADO: sin datos")
        print(f"  shorts:{ash} · longs:{alo} · PnL:{apnl:+.4f}")
        print(f"  peor trade: {peor['base']} {peor['pnl']:+.4f}")

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
    print(f"ERROR FATAL: {e}")          # v6: el run sale ROJO, no verde mentiroso
    sys.exit(1)
