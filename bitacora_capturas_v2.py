#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BITÁCORA DE CAPTURAS · GSCSI-TRADE
Cuaderno temporal para la repo del bot: lee el histórico de posiciones
CERRADAS de OKX (/account/positions-history, tipo SWAP) y lo imprime
en el log de GitHub Actions: cada presa cerrada, ganada 🟢 o perdida 🔴,
con fecha, entrada→salida, PnL neto, horas a bordo + resumen del capitán.

Usa los MISMO secrets que ya tiene la repo: OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSWORD.
Se pega, se corre, se lee el log y se quita. Nada se escribe en la cuenta.
"""
import os
import sys
import csv
from datetime import datetime, timezone


def utc(ms):
    if ms in (None, "", 0):
        return "?"
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def fnum(x, nd=6):
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return "?"


def main():
    limit = 100
    if len(sys.argv) > 1:
        try:
            limit = max(1, min(500, int(sys.argv[1])))
        except Exception:
            pass

    key = os.environ.get("OKX_API_KEY", "").strip()
    sec = os.environ.get("OKX_SECRET_KEY", "").strip()
    pwd = os.environ.get("OKX_PASSWORD", "").strip()

    print("=" * 74)
    print(f"CUADERNO DE CAPTURAS · {datetime.now(timezone.utc).strftime('%d-%m-%Y %H:%M')} UTC")
    print("=" * 74)

    if not (key and sec and pwd):
        print("Faltan secrets: OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSWORD no visibles.")
        return

    import ccxt

    ex = ccxt.okx({
        "apiKey": key,
        "secret": sec,
        "password": pwd,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })

    mundo = "REAL"
    hist = None
    try:
        hist = ex.fetch_positions_history({"instType": "SWAP", "limit": limit})
    except Exception as e:
        msg = str(e)
        if "50119" in msg or "doesn" in msg.lower() and "exist" in msg.lower():
            # la llave no existe en REAL -> probamos el mundo DEMO (llave simulada)
            print("La llave no existe en el mundo REAL -> probando mundo DEMO (x-simulated-trading: 1)...")
            try:
                exd = ccxt.okx({
                    "apiKey": key, "secret": sec, "password": pwd,
                    "enableRateLimit": True,
                    "options": {"defaultType": "swap"},
                })
                exd.headers["x-simulated-trading"] = "1"
                hist = exd.fetch_positions_history({"instType": "SWAP", "limit": limit})
                mundo = "DEMO"
            except Exception as e3:
                print(f"[!] Tampoco abre en DEMO: {e3}")
                print("    -> La llave esta BORRADA o RENOVADA: revisa en OKX Perfil > API")
                print("       (si la renovaste, hay que actualizar los secrets de la repo)")
                return
        else:
            try:
                raw = ex.private_get_account_positions_history({"instType": "SWAP", "limit": str(limit)})
                hist = raw.get("data", [])
            except Exception as e2:
                print(f"[!] OKX no dejo mirar el cuaderno: {e2}")
                return

    print(f"MUNDO DE LA LLAVE: {mundo}" + ("  (cuenta SIMULADA)" if mundo == "DEMO" else ""))
    print()

    if not hist:
        print("Cuaderno limpio: sin posiciones cerradas de SWAP para esta cuenta.")
        print("  (si el bot aun no zarpaba, las capturas swap seran las manuales)")
        return

    rows = []
    for h in hist:
        def g(*names, default=None):
            for n in names:
                v = h.get(n)
                if v not in (None, ""):
                    return v
            return default

        inst = g("instId", "symbol", default="?")
        ship = inst.split("-")[0] if isinstance(inst, str) else "?"
        side = str(g("direction", "posSide", default="?") or "?").lower()
        side = "LONG" if side.startswith("l") else ("SHORT" if side.startswith("s") else side.upper())
        try:
            pnl = float(g("realizedPnl", default=0) or 0)
        except Exception:
            pnl = 0.0
        try:
            fees = float(g("fee", "totalFee", default=0) or 0) + float(g("fundingFee", default=0) or 0)
        except Exception:
            fees = 0.0
        t_close, t_open = g("uTime", "closeTime"), g("openTime")
        dur_h = None
        try:
            dur_h = (int(t_close) - int(t_open)) / 3600000.0
        except Exception:
            pass
        rows.append({
            "cierre": utc(t_close),
            "nave": ship,
            "dir": side,
            "ct": g("closedPos", "closeTotalPos", default="?"),
            "entrada": fnum(g("openAvgPx", "avgEntryPrice")),
            "salida": fnum(g("closeAvgPx", "avgExitPrice")),
            "pnl": pnl,
            "fees": fees,
            "ccy": g("ccy", default="USDT"),
            "lev": g("lever", default=""),
            "h_bordo": (f"{dur_h:.1f}" if dur_h is not None else "?"),
        })

    rows.sort(key=lambda r: r["cierre"])  # cronologico

    wins = [r for r in rows if r["pnl"] > 0]
    loss = [r for r in rows if r["pnl"] < 0]
    zero = [r for r in rows if r["pnl"] == 0]
    pnls = [r["pnl"] for r in rows]
    total = sum(pnls)
    wr = (100.0 * len(wins) / (len(wins) + len(loss))) if (wins or loss) else 0.0
    best = max(pnls) if pnls else 0.0
    worst = min(pnls) if pnls else 0.0

    streak, sgn = 0, 0
    for r in reversed(rows):
        if r["pnl"] == 0:
            continue
        s = 1 if r["pnl"] > 0 else -1
        if sgn == 0:
            sgn, streak = s, 1
        elif s == sgn:
            streak += 1
        else:
            break

    ccy = rows[-1]["ccy"]
    print(f"\nPOSICIONES CERRADAS (SWAP): {len(rows)} · liquidacion en {ccy}")
    print("-" * 74)
    print(f"{'CIERRE':<10} {'NAVE':<6} {'DIR':<6} {'CT':>6} {'ENTRADA':>10} {'SALIDA':>10} {'PNL':>10} {'H.BORDO':>8}")
    print("-" * 74)
    for r in rows:
        flag = "+" if r["pnl"] > 0 else ("-" if r["pnl"] < 0 else "=")
        print(f"{r['cierre']:<10} {r['nave']:<6} {r['dir']:<6} {r['ct']:>6} "
              f"{r['entrada']:>10} {r['salida']:>10} {flag}{r['pnl']:>+9.2f} {r['h_bordo']:>8}")

    print("-" * 74)
    print("RESUMEN DEL CAPITAN")
    print(f"  ganadas: {len(wins)} | perdidas: {len(loss)} | nulas: {len(zero)}")
    print(f"  acierto (winrate): {wr:.1f}%")
    print(f"  PnL total neto: {total:+.2f} {ccy} | mejor: {best:+.2f} | peor: {worst:+.2f}")
    if streak and sgn:
        print(f"  racha actual: {streak} {'ganada(s)' if sgn > 0 else 'perdida(s)'}")

    csv_path = os.path.join(os.getcwd(), "bitacora_capturas.csv")
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"\n(Copia en el workspace del run: {csv_path} — se borra con el runner)")
    except Exception as e:
        print(f"(no pude guardar copia CSV: {e})")


if __name__ == "__main__":
    main()
