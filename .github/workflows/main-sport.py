#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
INFORME FLOTA — GSCSI-TRADE
===========================
Radar de capturas: un solo .py que consulta OKX y canta lo que han
capturado los navíos:

  1. ARCA        — colateral total (equity) y USDC libre
  2. EN LA MAR   — posiciones abiertas (entrada, marca, uPnL)
  3. TRAMPAS     — órdenes armadas sin ejecutar
  4. CAPTURAS    — fills ejecutados últimos N días (fee + PnL realizado)
  5. MARCADOR    — W/L por navío (trades cerrados agrupados por orden)

SOLO LECTURA. No abre, no mueve, no cierra nada.
Fail-closed: sin credenciales no zarpa. Jamás imprime secretos.

Uso (mismo entorno que los main-*.py):
    export OKX_API_KEY="..."
    export OKX_SECRET_KEY="..."
    export OKX_PASSPHRASE="..."
    python informe_flota.py                # capturas de los últimos 3 días
    python informe_flota.py --days 7       # últimos 7 días
    python informe_flota.py --demo         # cuenta demo (x-simulated-trading)
    python informe_flota.py --save bitacora.json

Dependencia: pip install ccxt
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    import ccxt
except ImportError:
    print("[FALLO-CERRADO] Falta ccxt:  pip install ccxt")
    sys.exit(2)

# ── Flota conocida (base del instrumento -> nombre de navío) ────────────────
FLOTA = ("DOGE", "ADA", "AVAX", "HBAR", "SUI", "INJ", "XRP", "XLM", "BTC")


def to_f(x):
    try:
        if x is None or x == "":
            return 0.0
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def ship_of(symbol_or_inst):
    base = symbol_or_inst.split("/")[0].split("-")[0].upper()
    return base if base in FLOTA else "?" + base


def fmt_px(v):
    v = to_f(v)
    if v == 0:
        return "-"
    return f"{v:.6g}"


def fmt_usd(v, sign=True):
    v = to_f(v)
    s = "+" if (sign and v > 0) else ""
    return f"{s}${v:,.2f}"


def build_exchange(demo: bool):
    key = os.environ.get("OKX_API_KEY", "")
    sec = os.environ.get("OKX_SECRET_KEY") or os.environ.get("OKX_SECRET", "")
    pwd = os.environ.get("OKX_PASSPHRASE", "")
    faltan = [n for n, v in (("OKX_API_KEY", key),
                             ("OKX_SECRET_KEY", sec),
                             ("OKX_PASSPHRASE", pwd)) if not v]
    if faltan:
        print("[FALLO-CERRADO] Sin credenciales: faltan " + ", ".join(faltan))
        print("                 Exporta las tres y vuelve a intentarlo.")
        sys.exit(2)
    ex = ccxt.okx({
        "apiKey": key,
        "secret": sec,
        "password": pwd,
        "enableRateLimit": True,
        "timeout": 20000,
    })
    if demo:
        ex.set_sandbox_mode(True)
    return ex


# ── Secciones ────────────────────────────────────────────────────────────────

def sec_arca(ex):
    print("\n-- 1. ARCA (colateral) " + "-" * 44)
    bal = ex.fetch_balance()
    d0 = bal.get("info", {}).get("data", [{}])[0]
    total_eq = to_f(d0.get("totalEq"))
    usdc = next((d for d in d0.get("details", []) if d.get("ccy") == "USDC"), {})
    print(f"  equity total : {fmt_usd(total_eq, sign=False)}")
    print(f"  USDC libre   : {fmt_usd(usdc.get('availEq'), sign=False)}"
          f"  (equity USDC {fmt_usd(usdc.get('eq'), sign=False)})")


def sec_posiciones(ex):
    print("\n-- 2. NAVIOS EN LA MAR (posiciones) " + "-" * 33)
    pos = ex.fetch_positions()
    vivas = [p for p in pos if to_f(p.get("contracts")) > 0]
    if not vivas:
        print("  (mar en calma — ningún navío con posición)")
        return
    print(f"  {'NAVIO':<6} {'INST':<18} {'SIDE':<6} {'CT':>5} "
          f"{'ENTRY':>11} {'MARK':>11} {'uPnL':>10} {'LEV':>4} MGN")
    for p in sorted(vivas, key=lambda x: x.get("symbol", "")):
        print(f"  {ship_of(p.get('symbol','')):<6} {p.get('symbol',''):<18} "
              f"{str(p.get('side','')):<6} {to_f(p.get('contracts')):>5g} "
              f"{fmt_px(p.get('entryPrice')):>11} {fmt_px(p.get('markPrice')):>11} "
              f"{fmt_usd(p.get('unrealizedPnl')):>10} {fmt_px(p.get('leverage')):>4} "
              f"{p.get('marginMode', '')}")


def sec_trampas(ex):
    print("\n-- 3. TRAMPAS ARMADAS (ordenes abiertas) " + "-" * 28)
    try:
        orders = ex.fetch_open_orders(None, params={"instType": "SWAP"})
    except Exception as e:
        print(f"  [!] sin lectura de ordenes: {type(e).__name__}")
        return
    if not orders:
        print("  (ninguna trampa armada)")
        return
    print(f"  {'NAVIO':<6} {'INST':<18} {'SIDE':<5} {'CT':>6} "
          f"{'PRECIO':>11} {'reduceOnly':>10}")
    for o in orders:
        ro = "SI" if (o.get("reduceOnly") or o.get("info", {}).get("reduceOnly") == "true") else "no"
        print(f"  {ship_of(o.get('symbol','')):<6} {o.get('symbol',''):<18} "
              f"{str(o.get('side','')):<5} {to_f(o.get('amount')):>6g} "
              f"{fmt_px(o.get('price')):>11} {ro:>10}")


def fetch_fills_swap(ex, since_ms, max_pages=30):
    out, after = [], None
    for _ in range(max_pages):
        params = {"instType": "SWAP"}
        if after:
            params["after"] = after
        trades = ex.fetch_my_trades(None, since=since_ms, limit=100, params=params)
        if not trades:
            break
        out.extend(trades)
        last = trades[-1]
        after = last.get("info", {}).get("billId") or last.get("id")
        ts_last = to_f(last.get("timestamp"))
        if len(trades) < 100 or (ts_last and ts_last < since_ms):
            break
    return [t for t in out if to_f(t.get("timestamp")) >= since_ms]


def sec_capturas(ex, since_ms, days):
    print(f"\n-- 4. CAPTURAS (fills ultimos {days} dias) " + "-" * 36)
    fills = fetch_fills_swap(ex, since_ms)
    if not fills:
        print("  (sin capturas en la ventana)")
        return fills
    agg = {}
    for t in fills:
        info = t.get("info", {})
        inst = t.get("symbol", "") or info.get("instId", "")
        a = agg.setdefault(ship_of(inst), {"n": 0, "buy": 0.0, "sell": 0.0,
                                           "fee": 0.0, "pnl": 0.0})
        sz = to_f(t.get("amount")) or to_f(info.get("fillSz"))
        a["n"] += 1
        if str(t.get("side", "")).lower().startswith("b") or info.get("side") == "B":
            a["buy"] += sz
        else:
            a["sell"] += sz
        a["fee"] += to_f(info.get("fillFee"))
        a["pnl"] += to_f(info.get("fillPnl"))
    print(f"  {'NAVIO':<6} {'FILLS':>5} {'BUY_CT':>9} {'SELL_CT':>9} "
          f"{'FEE':>9} {'PnL_REAL':>10} {'NET':>10}")
    tot = {"n": 0, "fee": 0.0, "pnl": 0.0}
    for ship in sorted(agg):
        a = agg[ship]
        tot["n"] += a["n"]; tot["fee"] += a["fee"]; tot["pnl"] += a["pnl"]
        print(f"  {ship:<6} {a['n']:>5} {a['buy']:>9g} {a['sell']:>9g} "
              f"{fmt_usd(a['fee']):>9} {fmt_usd(a['pnl']):>10} "
              f"{fmt_usd(a['pnl'] + a['fee']):>10}")
    print(f"  {'TOTAL':<6} {tot['n']:>5} {'':>9} {'':>9} "
          f"{fmt_usd(tot['fee']):>9} {fmt_usd(tot['pnl']):>10} "
          f"{fmt_usd(tot['pnl'] + tot['fee']):>10}")
    usdt = [t for t in fills if "-USDT-" in (t.get("symbol", "") or "")]
    if usdt:
        print("  [!] BAN USDT VIOLADO: hay fills en pares USDT — registrar en bitacora")
    return fills


def sec_marcador(fills):
    print("\n-- 5. MARCADOR (trades cerrados por orden) " + "-" * 30)
    if not fills:
        print("  (nada que puntuar)")
        return
    por_orden = {}
    for t in fills:
        info = t.get("info", {})
        pnl = to_f(info.get("fillPnl"))
        if pnl == 0.0:
            continue  # fills de apertura no puntúan
        key = (ship_of(t.get("symbol", "")), info.get("ordId") or t.get("id"))
        por_orden[key] = por_orden.get(key, 0.0) + pnl
    marcador = {}
    for (ship, _), pnl in por_orden.items():
        m = marcador.setdefault(ship, {"w": 0, "l": 0, "pnl": 0.0})
        m["w" if pnl > 0 else "l"] += 1
        m["pnl"] += pnl
    tw = tl = 0
    tpnl = 0.0
    print(f"  {'NAVIO':<6} {'W':>3} {'L':>3} {'PnL_NET':>10}")
    for ship in sorted(marcador):
        m = marcador[ship]
        tw += m["w"]; tl += m["l"]; tpnl += m["pnl"]
        print(f"  {ship:<6} {m['w']:>3} {m['l']:>3} {fmt_usd(m['pnl']):>10}")
    print(f"  {'TOTAL':<6} {tw:>3} {tl:>3} {fmt_usd(tpnl):>10}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Informe Flota GSCSI-TRADE (solo lectura)")
    ap.add_argument("--days", type=int, default=3, help="ventana de capturas en dias (def: 3)")
    ap.add_argument("--demo", action="store_true", help="usar cuenta demo OKX")
    ap.add_argument("--save", metavar="JSON", help="guardar fills crudos en JSON")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    since_ms = int(now.timestamp() * 1000) - args.days * 86_400_000

    print("=" * 62)
    print(f"  INFORME FLOTA — GSCSI-TRADE   UTC {now:%Y-%m-%d %H:%M:%S}"
          f"   demo={'SI' if args.demo else 'NO'}")
    print("=" * 62)

    ex = build_exchange(args.demo)
    try:
        ex.load_markets()
    except Exception as e:
        print(f"[FALLO-CERRADO] Sin contacto con OKX: {type(e).__name__}: {e}")
        sys.exit(1)

    sec_arca(ex)
    sec_posiciones(ex)
    sec_trampas(ex)

    fills = []
    try:
        fills = sec_capturas(ex, since_ms, args.days)
    except Exception as e:
        print(f"  [!] capturas no disponibles: {type(e).__name__}: {e}")
    sec_marcador(fills)

    if args.save:
        try:
            crudo = {
                "generado_utc": now.isoformat(),
                "dias": args.days,
                "fills": [
                    {"ts": t.get("timestamp"), "symbol": t.get("symbol"),
                     "side": t.get("side"), "amount": t.get("amount"),
                     "price": t.get("price"),
                     "pnl": t.get("info", {}).get("fillPnl"),
                     "fee": t.get("info", {}).get("fillFee"),
                     "ordId": t.get("info", {}).get("ordId"),
                     "clOrdId": t.get("info", {}).get("clOrdId")}
                    for t in fills
                ],
            }
            with open(args.save, "w", encoding="utf-8") as f:
                json.dump(crudo, f, indent=1, ensure_ascii=False)
            print(f"\n[bitacora] datos crudos -> {args.save}")
        except OSError as e:
            print(f"[!] no se pudo guardar {args.save}: {e}")

    print("\nRitual: verde=silencio · presa=bitacora · rojo=registro")
    print("PAX:.")


if __name__ == "__main__":
    main()
