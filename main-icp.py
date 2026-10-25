def auth_and_collateral(ex, has_creds):
    if not has_creds:
        log.info("Sombra publica: sin credenciales — solo lectura de mercado")
        return None
    try:
        bal = ex.fetch_balance()
    except Exception as e:
        fail("[ERROR] Autenticacion/lectura de cuenta fallo: %s — DETENIDO" % str(e)[:200])

    def ff(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    tot_eq = None
    # 1) totEq del crudo OKX (equidad total en USD), si existe y es > 0
    try:
        d0 = bal.get("info", {}).get("data", [{}])[0]
        if d0.get("totEq"):
            tot_eq = ff(d0["totEq"])
    except Exception:
        pass
    # 2) METODO DEL PERRO (probado en flota, 15:32 UTC leyo ~$67.89):
    #    suma de eqUsd por divisa en details
    if not tot_eq:
        try:
            details = bal.get("info", {}).get("data", [{}])[0].get("details") or []
            suma = sum(ff(d.get("eqUsd")) for d in details)
            if suma > 0:
                tot_eq = suma
        except Exception:
            pass
    # 3) Ultimo recurso: bolsillos unificados reales (USD/USDC/USDT)
    if not tot_eq:
        try:
            suma = sum(ff((bal.get(c) or {}).get("total"))
                       for c in ("USD", "USDC", "USDT"))
            if suma > 0:
                tot_eq = suma
        except Exception:
            pass
    try:
        host = str(ex.urls["api"]["private"]).replace("{hostname}", getattr(ex, "hostname", ""))
    except Exception:
        host = "?"
    log.info("Autenticacion OK | host=%s | Colateral real (USD): ~%.2f", host, tot_eq or 0.0)
    return tot_eq
