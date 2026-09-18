# main-hy.py — GSCSI TABLE · C > Si
# v7: instrumentos resueltos con la MISMA regla del bot ADA en producción:
#     X-Perp = future (expTime más lejano) · fallback swap USD · settle USDC ·
#     veto USDT · S=short L=long reales · SL/TP % · historial JSONL · exit 1
import os
import sys
import time
import json
import pathlib
import ccxt
from collections import defaultdict

# ══ AXIOMAS DEL CARBONO (editar aquí, nunca en el código) ══
BASES        = {'DOGE', 'FET', 'SUI', 'XLM', 'ADA'}        # sin BTC
SL_PCT       = {'DOGE':1.0, 'FET':1.0, 'SUI':1.0, 'XLM':1.0, 'ADA':1.0}  # ← Carbono ajusta
TP_PCT       = {'DOGE':2.0, 'FET':2.0, 'SUI':2.0, 'XLM':2.0, 'ADA':2.0}  # ← Carbono ajusta
CADENCIA_HRS = 8

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
            raise                      # nada de errores silenciosos
        if not data:
            break
        out.extend(data)
        after = data[-1][key]
        if len(data) < 100:
            break
        time.sleep(0.25)
    return out


# ── resolución de instrumentos: regla de producción (main-ada.py) ──
def _exp(m):
    try:
        return int((m.get('info') or {}).get('expTime') or 0)
    except (TypeError, ValueError):
        return 0

def _settle(m):
    return str(m.get('settle') or (m.get('info') or {}).get('settleCcy') or '').upper()

def _inst_type(m):
    t = str((m.get('info') or {}).get('instType') or '').upper()
    if t in ('SWAP', 'FUTURES'):
        return t
    return 'FUTURES' if m.get('future') else 'SWAP'

def selecciona_instrumentos(exchange):
    """Regla del bot ADA (resolve_symbol):
       1º X-Perp = future activo con expTime MÁS LEJANO, settle USDC
       2º fallback = swap USD activo
       VETO absoluto: settle USDT (EEE/MiCA)."""
    print("INSTRUMENTOS ACTIVOS (regla resolve_symbol del bot ADA):")
    elegidos = {}
    for base in sorted(BASES):
        fut, swp = [], []
        for m in exchange.markets.values():
            if (m.get('base') or '').upper() != base or not m.get('active'):
                continue
            if not (m.get('future') or m.get('swap')):
                continue
            s = _settle(m)
            if s == 'USDT' or s != 'USDC':    # veto USDT + UM = margen USDC
                continue
            (fut if m.get('future') else swp).append(m)
        m = max(fut, key=_exp) if fut else (swp[0] if swp else None)
        if m is None:
            sys.exit(f"ABORTADO: sin X-Perp/swap-USD activo para {base} (settle USDC)")
        it = _inst_type(m)
        elegidos[m['id']] = (base, it)
        origen = 'X-Perp' if fut else 'fallback swap'
        print(f"  {base:<5} → {m['id']:<24} instType={it:<8} "
              f"expTime={(m.get('info') or {}).get('expTime', '?')} ({origen})")
    return elegidos


# ── clasificación de salidas ──
def mov_pct(r):
    try:
        o, c = float(r['open']), float(r['close'])
    except (TypeError, ValueError):
        return None
    if o <= 0 or c <= 0:
        return None
    return (c / o - 1) * 100 if r['dir'] == 'long' else (1 - c / o) * 100

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

    # ── posiciones cerradas: 1 posId = 1 trade ──
    trades = defaultdict(list)          # iid -> [regs]
    for iid, (base, itype) in insts.items():
        for p in paginar('privateGetAccountPositionsHistory', 'posId',
                         {'instType': itype, 'instId': iid}):   # ← instType REAL
            pos_id = p.get('posId')
            if not pos_id:
                continue
            try:
                pnl = float(p.get('realizedPnl') or 0)
            except (TypeError, ValueError):
                continue
            trades[iid].append({
                'posId': pos_id,
                'dir':   (p.get('direction') or '?').lower(),
                'open':  p.get('openAvgPx') or '',
                'close': p.get('closeAvgPx') or '',
                'liq':   float(p.get('liqPenalty') or 0),
                'pnl':   pnl,
                'uTime': p.get('uTime') or '',
            })

    # consolidar cierres parciales por posId
    agg = defaultdict(list)
    for iid, regs in trades.items():
        base = insts[iid][0]
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

    # ── persistencia: dedup por (inst, posId) ──
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
                if k not in prev:
                    f.write(json.dumps(t) + '\n')
                    prev[k] = t

    # ── ENCABEZADO ──
    print("=" * 46)
    print("  TABLA GSCSI S/L")
    print(f"  actualizacion automatica cada {CADENCIA_HRS} hrs")
    print("  fuente: positions-history (OKX Europe)")
    print("  Plataforma: my.okx.com · Regimen: MiCA/EEE")
    print("  Instrumento: X-Perp UM · margen USDC · USDT: VETADO")
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

    # ── ACUMULADO desde génesis (JSONL) ──
    acc = [j for j in prev.values() if j.get('base') in BASES]
    if acc:
        aw  = sum(1 for r in acc if r['pnl'] > 0)
        al  = sum(1 for r in acc if r['pnl'] < 0)
        ash = sum(1 for r in acc if r['dir'] == 'short')
        apnl = sum(r['pnl'] for r in acc)
        den = aw + al
        peor = min(acc, key=lambda r: r['pnl'])
        print("=" * 46)
        if den:
            print(f"ACUMULADO: {len(acc)} trades · winrate {aw / den * 100:.0f}%")
        else:
            print(f"ACUMULADO: {len(acc)} trades (solo breakeven)")
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
    print(f"ERROR FATAL: {e}")
    sys.exit(1)
