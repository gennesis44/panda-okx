#!/usr/bin/env python3
# ==============================================================
# xpl.py — Motor de Ejecución de Trading OKX (PRODUCCIÓN)
# Estrategia: EMA 3 / EMA 21 (30m) | USDC SWAP | 3 Ciclos de Verificación
# Gestión de Riesgo: SL 1.5% | TP 2.3% | Apalancamiento 1x
# Requisitos: Python 3.10+, PyYAML, ccxt (pip install pyyaml ccxt)
# Códigos de salida: 0 = ok | 1 = tareas fallidas | 2 = config inválida
# ==============================================================
import argparse
import logging
import logging.handlers
import sys
import time
from datetime import datetime
from pathlib import Path

import ccxt
import yaml

RUTA_CONFIG_DEFECTO = Path(__file__).resolve().parent / "xpl.yml"
log = logging.getLogger("xpl")


# ---------------------------- CARGA Y VALIDACIÓN ----------------------------
def cargar_config(ruta: Path) -> dict:
    if not ruta.is_file():
        raise FileNotFoundError(f"No existe el archivo de configuración: {ruta}")
    with ruta.open("r", encoding="utf-8") as f:
        try:
            contenido = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Sintaxis YAML inválida en {ruta}:\n{e}") from e
    contenido = contenido or {}
    if not isinstance(contenido, dict):
        raise ValueError("La raíz del YAML debe ser un mapa (clave: valor).")
    return contenido


def validar_config(cfg: dict) -> None:
    """Fail-fast: detección de errores de estructura previa a la ejecución."""
    tareas = cfg.get("tareas", [])
    if not isinstance(tareas, list):
        raise ValueError("'tareas' debe ser una lista.")
    ids = set()
    for i, t in enumerate(tareas):
        if not isinstance(t, dict):
            raise ValueError(f"tareas[{i}] debe ser un mapa.")
        tid = t.get("id")
        if not tid:
            raise ValueError(f"tareas[{i}] no tiene 'id'.")
        if tid in ids:
            raise ValueError(f"id de tarea duplicado: '{tid}'.")
        ids.add(tid)
    for seccion in ("app", "log", "ajustes", "exchange"):
        if seccion in cfg and not isinstance(cfg[seccion], dict):
            raise ValueError(f"'{seccion}' debe ser un mapa.")


# ---------------------------- LOGGING ----------------------------
def configurar_logging(cfg_log: dict, debug: bool) -> None:
    nivel = "DEBUG" if debug else str(cfg_log.get("nivel", "INFO")).upper()
    handlers: list[logging.Handler] = []
    if cfg_log.get("mostrar_en_pantalla", True):
        handlers.append(logging.StreamHandler())
    archivo = cfg_log.get("archivo")
    if archivo:
        handlers.append(logging.handlers.RotatingFileHandler(
            archivo, maxBytes=1_000_000, backupCount=5, encoding="utf-8"))
    if not handlers:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=getattr(logging, nivel, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


# ---------------------------- CÁLCULO EMA ----------------------------
def calcular_ema(precios: list[float], periodo: int) -> float:
    """Calcula la Media Móvil Exponencial (EMA) sobre una serie de precios."""
    if len(precios) < periodo:
        raise ValueError(f"Se necesitan al menos {periodo} velas para calcular la EMA.")
    k = 2 / (periodo + 1)
    ema = sum(precios[:periodo]) / periodo
    for precio in precios[periodo:]:
        ema = (precio * k) + (ema * (1 - k))
    return ema


# ---------------------------- LÓGICA DE TRADING OKX ----------------------------
def inicializar_exchange(cfg: dict) -> ccxt.okx:
    cfg_ex = cfg.get("exchange", {})
    exchange = ccxt.okx({
        "apiKey": cfg_ex.get("api_key", ""),
        "secret": cfg_ex.get("secret", ""),
        "password": cfg_ex.get("passphrase", ""),
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
        }
    })
    
    # Redirección a endpoints my.okx.com / Dominio EEA
    if cfg_ex.get("eea_domain", True):
        exchange.urls["api"] = {
            "rest": "https://my.okx.com",
            "public": "https://my.okx.com",
            "private": "https://my.okx.com",
        }
    
    if cfg_ex.get("sandbox", False):
        exchange.set_sandbox_mode(True)
        
    return exchange


def seleccionar_simbolo_usdc_lejano(exchange: ccxt.okx, par_base: str) -> str:
    """
    Filtra y selecciona contratos SWAP/Futures en USDC (vetando USDT)
    y prioriza el vencimiento más lejano si existen múltiples series.
    """
    markets = exchange.load_markets()
    candidatos = []
    
    for symbol, market in markets.items():
        if market.get("settle") == "USDC" and market.get("base") == par_base:
            if "USDT" in symbol:
                continue
            if market.get("swap", False) or market.get("future", False):
                expiry = market.get("expiry", 0) or 0
                candidatos.append((expiry, symbol))
                
    if not candidatos:
        raise RuntimeError(f"No se encontraron mercados USDC Swap para '{par_base}' (USDT vetado).")
    
    candidatos.sort(key=lambda x: x[0], reverse=True)
    simbolo_elegido = candidatos[0][1]
    log.info("Símbolo USDC seleccionado (más lejano): %s", simbolo_elegido)
    return simbolo_elegido


def tarea_ema_trading(tarea: dict, cfg: dict) -> str:
    exchange = inicializar_exchange(cfg)
    base_asset = tarea.get("base_asset", "BTC").upper()
    timeframe = tarea.get("timeframe", "30m")
    max_intentos = int(tarea.get("intentos", 3))
    
    # 1. Selección de símbolo USDC excluyendo USDT
    symbol = seleccionar_simbolo_usdc_lejano(exchange, base_asset)
    market = exchange.market(symbol)
    
    # 2. Configurar Apalancamiento a 1x
    try:
        exchange.set_leverage(1, symbol, params={"mgnMode": "isolated"})
    except Exception as e:
        log.warning("No se pudo fijar apalancamiento a 1x: %s", e)

    resultados_ciclos = []

    # 3. Bucle de verificación (Entra 3 veces por ejecución)
    for ciclo in range(1, max_intentos + 1):
        log.info("Iniciando ciclo de verificación %d/%d para %s (%s)", ciclo, max_intentos, symbol, timeframe)
        
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=50)
            precios_cierre = [v[4] for v in ohlcv]
            
            ema3 = calcular_ema(precios_cierre, 3)
            ema21 = calcular_ema(precios_cierre, 21)
            precio_actual = precios_cierre[-1]
            
            distancia_ema_pct = (abs(ema3 - ema21) / ema21) * 100
            log.info("[Ciclo %d] %s %s -> EMA3: %.4f | EMA21: %.4f | Separación: %.3f%%", 
                     ciclo, symbol, timeframe, ema3, ema21, distancia_ema_pct)

            # Filtro de apertura mínima de 0.15%
            if distancia_ema_pct < 0.15:
                resultados_ciclos.append(f"Ciclo {ciclo}: Sin entrada (Separación {distancia_ema_pct:.3f}% < 0.15%)")
                if ciclo < max_intentos:
                    time.sleep(2)
                continue

            # Dimensionamiento por valor nominal del contrato
            val_contrato = float(market.get("contractSize", 1.0))
            monto_contratos = 1 if val_contrato > 0.50 else 3

            # Dirección y Riesgo (SL 1.5% | TP 2.3%)
            if ema3 > ema21:
                lado = "buy"
                precio_sl = precio_actual * (1.0 - 0.015)
                precio_tp = precio_actual * (1.0 + 0.023)
            else:
                lado = "sell"
                precio_sl = precio_actual * (1.0 + 0.015)
                precio_tp = precio_actual * (1.0 - 0.023)

            params = {
                "stopLoss": {"triggerPrice": exchange.price_to_precision(symbol, precio_sl), "orderPrice": "-1"},
                "takeProfit": {"triggerPrice": exchange.price_to_precision(symbol, precio_tp), "orderPrice": "-1"}
            }

            if tarea.get("ejecutar_orden", False):
                orden = exchange.create_order(
                    symbol=symbol, type="market", side=lado, amount=monto_contratos, params=params
                )
                return (
                    f"[+] CONDICIÓN CUMPLIDA Y ORDEN EJECUTADA EN CICLO {ciclo} [{lado.upper()}] en {symbol}\n"
                    f"Temporalidad: {timeframe} | Separación EMAs: {distancia_ema_pct:.3f}% | Contratos: {monto_contratos}\n"
                    f"Precio: {precio_actual:.4f} | SL (1.5%): {precio_sl:.4f} | TP (2.3%): {precio_tp:.4f}\n"
                    f"ID Orden: {orden.get('id')}"
                )
            else:
                return (
                    f"[SIMULACIÓN] CONDICIÓN CUMPLIDA EN CICLO {ciclo} [{lado.upper()}] en {symbol}\n"
                    f"Temporalidad: {timeframe} | Separación EMAs: {distancia_ema_pct:.3f}% (>= 0.15%)\n"
                    f"Contratos: {monto_contratos} (Val: {val_contrato} USDC) | Precio: {precio_actual:.4f}\n"
                    f"Stop Loss (-1.5%): {precio_sl:.4f} | Take Profit (+2.3%): {precio_tp:.4f}"
                )

        except Exception as e:
            log.error("Error en ciclo %d para %s: %s", ciclo, symbol, e)
            resultados_ciclos.append(f"Ciclo {ciclo}: Error ({e})")
        
        if ciclo < max_intentos:
            time.sleep(2)

    return f"Resumen de 3 ciclos completados en {symbol} ({timeframe}):\n" + "\n".join(resultados_ciclos)


TAREAS_DISPONIBLES = {
    "ema_trading": tarea_ema_trading,
}


# ---------------------------- EJECUCIÓN ----------------------------
def ejecutar_tareas(cfg: dict, solo_una: str | None) -> int:
    app = cfg.get("app", {})
    print(f"\n=== {app.get('nombre', 'XPL Engine')} v{app.get('version', '2.1.0')} ===\n")

    tareas = cfg.get("tareas", [])
    if not tareas:
        log.warning("El YAML no define ninguna tarea.")
        return 0

    fallos = ejecutadas = 0
    for tarea in tareas:
        tid = tarea["id"]
        if solo_una and tid != solo_una:
            continue
        if not tarea.get("activa", False):
            log.info("Tarea '%s' desactivada. Se omite.", tid)
            continue

        funcion = TAREAS_DISPONIBLES.get(tarea.get("tipo", ""))
        if funcion is None:
            log.error("Tarea '%s': tipo '%s' no reconocido.", tid, tarea.get("tipo"))
            fallos += 1
            continue

        log.debug("Ejecutando '%s' (tipo '%s')", tid, tarea.get("tipo"))
        try:
            print(f"[{tid}]\n{funcion(tarea, cfg)}\n")
            ejecutadas += 1
        except Exception:
            log.exception("Tarea '%s' falló", tid)
            fallos += 1

    if solo_una and ejecutadas == 0 and fallos == 0:
        log.error("No hay ninguna tarea activa con id '%s'. Disponibles: %s",
                  solo_una, [t["id"] for t in tareas])
        return 1

    print(f"Tareas ejecutadas: {ejecutadas} | Fallidas: {fallos}")
    return fallos


def parsear_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Motor XPL — Estrategia EMA OKX USDC 30m.")
    p.add_argument("--config", type=Path, default=RUTA_CONFIG_DEFECTO)
    p.add_argument("--tarea", metavar="ID", default=None)
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parsear_args()
    try:
        cfg = cargar_config(args.config)
        validar_config(cfg)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR de configuración: {e}", file=sys.stderr)
        sys.exit(2)

    configurar_logging(cfg.get("log", {}), args.debug)
    try:
        fallos = ejecutar_tareas(cfg, args.tarea)
    except KeyboardInterrupt:
        log.warning("Interrumpido por el usuario.")
        sys.exit(130)
    sys.exit(1 if fallos else 0)


if __name__ == "__main__":
    main()
