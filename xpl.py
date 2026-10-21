#!/usr/bin/env python3
# ==============================================================
# xpl.py — lee "xpl.yml" y ejecuta las tareas activas (PRODUCCIÓN)
#   python xpl.py                     -> todas las tareas activas
#   python xpl.py --tarea saludo      -> solo una
#   python xpl.py --config otro.yml   -> otro YAML
#   python xpl.py --debug             -> nivel DEBUG (sin volcar config)
# Requisitos: Python 3.10+, PyYAML (pip install pyyaml)
# Códigos de salida: 0 = ok | 1 = tareas fallidas | 2 = config inválida
# ==============================================================
import argparse
import logging
import logging.handlers
import sys
from datetime import datetime
from pathlib import Path

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
    """Fail-fast: errores de estructura se detectan antes de ejecutar nada."""
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
    for seccion in ("app", "log", "ajustes"):
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
        # Rotación: evita que el log crezca sin límite en producción
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


# ---------------------------- TAREAS ----------------------------
def tarea_saludo(tarea: dict, cfg: dict) -> str:
    return str(tarea.get("texto", "Hola"))


def tarea_calculo(tarea: dict, cfg: dict) -> str:
    aj = cfg.get("ajustes", {})
    factor = float(aj.get("factor", 1.0))
    limite = max(0, min(int(aj.get("limite_registros", 5)), 1000))
    lineas = [f"  {i:>2} x {factor:<4} = {i * factor}" for i in range(1, limite + 1)]
    return "Tabla de multiplicar (factor del YAML):\n" + "\n".join(lineas)


def tarea_fecha(tarea: dict, cfg: dict) -> str:
    return datetime.now().astimezone().strftime(
        "Hoy es %d/%m/%Y — son las %H:%M:%S %Z")


def tarea_contador(tarea: dict, cfg: dict) -> str:
    try:
        n = int(tarea.get("texto", "3"))
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(n, 1000))
    return "Cuenta: " + ", ".join(str(i) for i in range(1, n + 1))


TAREAS_DISPONIBLES = {
    "saludo": tarea_saludo,
    "calculo": tarea_calculo,
    "fecha": tarea_fecha,
    "contador": tarea_contador,
}


# ---------------------------- EJECUCIÓN ----------------------------
def ejecutar_tareas(cfg: dict, solo_una: str | None) -> int:
    app = cfg.get("app", {})
    print(f"\n=== {app.get('nombre', 'XPL')} v{app.get('version', '?')} ===\n")

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
    p = argparse.ArgumentParser(description="Lee xpl.yml y ejecuta sus tareas.")
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
