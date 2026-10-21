#!/usr/bin/env python3
# ==============================================================
# xpl.py — SCRIPT DE EJEMPLO QUE LEE UN ARCHIVO YAML
# ==============================================================
# ¿Qué hace este script?
#   1. Lee el archivo de configuración "xpl.yml" (mismo directorio).
#   2. Recorre la lista de "tareas" definida en ese YAML.
#   3. Ejecuta cada tarea activa e imprime resultados por consola.
#
# ¿Cómo se usa?
#   python xpl.py                       -> ejecuta TODAS las tareas activas
#   python xpl.py --tarea saludo        -> ejecuta solo la tarea "saludo"
#   python xpl.py --config otro.yml     -> usa otro archivo de configuración
#   python xpl.py --tarea calculo --debug -> muestra info de depuración
#
# Requisitos:
#   - Python 3.10+
#   - PyYAML  (instalar con:  pip install pyyaml)
# ==============================================================

# --- IMPORTACIONES --------------------------------------------
# pathlib.Path : manejo moderno de rutas de archivos (orientado a objetos)
# argparse     : parsea argumentos de línea de comandos (--tarea, --config...)
# logging      : sistema estándar de registro de eventos (INFO, ERROR...)
# sys          : acceso a constantes del intérprete (sys.exit, sys.stderr)
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# yaml : librería externa (PyYAML) que convierte texto YAML -> dict/listas
import yaml

# Ruta por defecto del config: la carpeta donde VIVE este script.
# Path(__file__) es la ruta de este .py; .parent su carpeta contenedora.
RUTA_CONFIG_DEFECTO = Path(__file__).parent / "xpl.yml"

# ------------------------------------------------------------ --
# CARGA DEL YAML
# ------------------------------------------------------------ --
def cargar_config(ruta: Path) -> dict:
    """Lee un archivo YAML y devuelve su contenido como diccionario.

    Errores controlados:
      - FileNotFoundError : el archivo no existe en la ruta dada.
      - yaml.YAMLError    : el YAML tiene errores de sintaxis
                            (p.ej. mezclas de tabuladores y espacios).
    """
    if not ruta.exists():
        # raise lanza la excepción y corta la ejecución hacia quien llame
        raise FileNotFoundError(f"No existe el archivo de configuración: {ruta}")

    with ruta.open("r", encoding="utf-8") as f:
        try:
            # yaml.safe_load: convierte el YAML en objetos Python
            # (dicts, listas, str, int, bool...). "safe" = no ejecuta
            # código arbitrario del YAML, siempre preferible a yaml.load.
            contenido = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise yaml.YAMLError(f"Sintaxis YAML inválida en {ruta}:\n{e}")

    # Si el archivo está vacío, safe_load devuelve None -> devolvemos {}
    return contenido or {}


# ------------------------------------------------------------ --
# CONFIGURAR EL LOGGING
# ------------------------------------------------------------ --
def configurar_logging(cfg_log: dict, debug: bool) -> None:
    """Configura el módulo logging según la sección 'log' del YAML.

    cfg_log  -> diccionario con las claves nivel / archivo / mostrar_en_pantalla
    debug    -> si es True (flag --debug), fuerza el nivel DEBUG
    """
    # .get("clave", defecto) lee la clave y, si no existe, devuelve "defecto"
    nivel = "DEBUG" if debug else str(cfg_log.get("nivel", "INFO")).upper()

    # handlers = destinos donde van los mensajes de registro
    handlers: list[logging.Handler] = []

    if cfg_log.get("mostrar_en_pantalla", True):
        # StreamHandler imprime en consola (stderr por defecto)
        handlers.append(logging.StreamHandler())

    archivo = cfg_log.get("archivo")
    if archivo:
        # FileHandler añade cada mensaje al archivo indicado
        handlers.append(logging.FileHandler(archivo, encoding="utf-8"))

    # basicConfig inicializa el sistema de logging una sola vez
    logging.basicConfig(
        level=getattr(logging, nivel, logging.INFO),   # texto -> constante
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


# ------------------------------------------------------------ --
# FUNCIONES DE CADA TIPO DE TAREA
# ------------------------------------------------------------ --
# Cada función recibe el diccionario de la tarea y el config global.
# Devuelve un string con el resultado (o None si no produce salida).

def tarea_saludo(tarea: dict, cfg: dict) -> str:
    """Tarea 'saludo': imprime el texto definido en el YAML."""
    # La app demo siempre imprime; esto ilustra devolver un string.
    return tarea.get("texto", "Hola")


def tarea_calculo(tarea: dict, cfg: dict) -> str:
    """Tarea 'calculo': usa el 'factor' de la sección 'ajustes'."""
    factor = float(cfg.get("ajustes", {}).get("factor", 1.0))
    limite = int(cfg.get("ajustes", {}).get("limite_registros", 5))
    # enumerate genera pares (posición, valor): 1..limite
    lineas = [f"  {i:>2} x {factor:<4} = {i * factor}" for i in range(1, limite + 1)]
    return "Tabla de multiplicar (factor del YAML):\n" + "\n".join(lineas)


def tarea_fecha(tarea: dict, cfg: dict) -> str:
    """Tarea 'fecha': muestra fecha y hora actuales formateadas."""
    # %d/%m/%Y -> día/mes/año; %H:%M:%S -> hora:minuto:segundo
    return datetime.now().strftime("Hoy es %d/%m/%Y — son las %H:%M:%S")


def tarea_contador(tarea: dict, cfg: dict) -> str:
    """Tarea 'contador': cuenta de 1 hasta N (N viene en el campo texto)."""
    try:
        n = int(tarea.get("texto", "3"))     # convierte el texto a entero
    except ValueError:
        n = 3                                 # valor por defecto si no es número
    return "Cuenta: " + ", ".join(str(i) for i in range(1, n + 1))


# Mapa tipo_de_tarea -> función que la ejecuta.
# Así NO hace falta un if/elif gigante: añadir un tipo nuevo
# es solo escribir una función y registrarla aquí.
TAREAS_DISPONIBLES = {
    "saludo":   tarea_saludo,
    "calculo":  tarea_calculo,
    "fecha":    tarea_fecha,
    "contador": tarea_contador,
}


# ------------------------------------------------------------ --
# EJECUCIÓN DE TAREAS
# ------------------------------------------------------------ --
def ejecutar_tareas(cfg: dict, solo_una: str | None, debug: bool) -> int:
    """Recorre las tareas del YAML y ejecuta las activas.

    Devuelve el número de tareas fallidas (0 = todo bien).
    """
    nombre_app = cfg.get("app", {}).get("nombre", "XPL")
    version = cfg.get("app", {}).get("version", "?")
    print(f"\n=== {nombre_app} v{version} ===\n")

    tareas = cfg.get("tareas", [])
    if not tareas:
        logging.warning("El YAML no define ninguna sección 'tareas'.")
        return 0

    fallos = 0
    ejecutadas = 0

    for tarea in tareas:
        tid = tarea.get("id", "sin-id")

        # Filtro por --tarea: si pediste una concreta, salta las demás
        if solo_una and tid != solo_una:
            continue

        if not tarea.get("activa", False):
            logging.info(f"Tarea '{tid}' está desactivada (activa: false). Se omite.")
            continue

        tipo = tarea.get("tipo", "")
        funcion = TAREAS_DISPONIBLES.get(tipo)

        if funcion is None:
            # Tipo desconocido: registrar y seguir con la siguiente
            logging.error(f"Tarea '{tid}': tipo '{tipo}' no reconocido.")
            fallos += 1
            continue

        if debug:
            logging.debug(f"Ejecutando tarea '{tid}' de tipo '{tipo}' con datos {tarea}")

        try:
            resultado = funcion(tarea, cfg)
            print(f"[{tid}]\n{resultado}\n")
            ejecutadas += 1
        except Exception as e:
            # Captura genérica SOLO en el borde: la app no debe
            # morir por una tarea fallida, pero sí debe avisar.
            logging.error(f"Tarea '{tid}' falló: {e}")
            fallos += 1

    if solo_una and ejecutadas == 0 and fallos == 0:
        print(f"No se encontró ninguna tarea activa llamada '{solo_una}'.")
        print(f"IDs disponibles: {[t.get('id') for t in tareas]}")

    print(f"Tareas ejecutadas: {ejecutadas} | Fallidas: {fallos}")
    return fallos


# ------------------------------------------------------------ --
# ARGUMENTOS DE LÍNEA DE COMANDOS
# ------------------------------------------------------------ --
def parsear_args() -> argparse.Namespace:
    """Define y parsea los argumentos aceptados por terminal."""
    parser = argparse.ArgumentParser(
        description="Script demo: lee xpl.yml y ejecuta las tareas definidas en él."
    )
    parser.add_argument(
        "--config", type=Path, default=RUTA_CONFIG_DEFECTO,
        help=f"Ruta del YAML (por defecto: {RUTA_CONFIG_DEFECTO.name})",
    )
    parser.add_argument(
        "--tarea", metavar="ID", default=None,
        help="Ejecuta solo la tarea con ese id (p.ej. --tarea saludo)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Activa mensajes de depuración detallados",
    )
    # action="store_true": el flag no lleva valor; si aparece -> True
    return parser.parse_args()


# ------------------------------------------------------------ --
# FUNCIÓN PRINCIPAL
# ------------------------------------------------------------ --
def main() -> None:
    """Punto de entrada: orquesta config -> logging -> tareas -> salida."""
    args = parsear_args()

    try:
        cfg = cargar_config(args.config)
    except (FileNotFoundError, yaml.YAMLError) as e:
        # Errores "esperados": mensaje limpio para el usuario
        print(f"ERROR al leer la configuración: {e}", file=sys.stderr)
        sys.exit(1)                     # código 1 = terminó con error

    configurar_logging(cfg.get("log", {}), args.debug)

    if args.debug:
        logging.debug(f"Configuración cargada:\n{yaml.safe_dump(cfg, allow_unicode=True)}")

    fallos = ejecutar_tareas(cfg, args.tarea, args.debug)

    # El código de salida informa al sistema operativo:
    # 0 = correcto, 1 = hubo fallos (útil en automatizaciones/CI)
    sys.exit(1 if fallos else 0)


# Este bloque SOLO se ejecuta si lanzas el archivo directamente
# (python xpl.py), no si otro script lo importa (import xpl).
if __name__ == "__main__":
    main()
