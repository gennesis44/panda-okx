# ==============================================================
# .github/workflows/xpl.yml — GitHub ACTIONS para xpl.py
# ==============================================================
# OJO: este archivo NO es el config del bot. Es la "orden de
# trabajo" que le dice a GitHub como ejecutar tu xpl.py solo.
#
# Que hace cada vez que arranca:
#   1. Baja tu repositorio
#   2. Instala Python 3.12 y la libreria PyYAML
#   3. Ejecuta:  python xpl.py
#
# IMPORTANTE: en la RAIZ del repo deben estar ademas:
#   - xpl.py   (el script)
#   - xpl.yml  (su config: secciones app / log / ajustes / tareas)
# ==============================================================

name: xpl

# CUANDO se ejecuta:
on:
  schedule:
    - cron: "*/30 * * * *"   # cada 30 min (GitHub usa hora UTC; las velas
                             # de 30m cierran a las :00 y :30 UTC, encaja).
                             # La primera ejecucion programada puede tardar
                             # hasta ~30 min en aparecer: es normal.
  workflow_dispatch:          # boton "Run workflow" para probarlo a mano

# Evita que se pisen dos ejecuciones a la vez
concurrency:
  group: xpl
  cancel-in-progress: false

jobs:
  xpl:
    runs-on: ubuntu-latest    # maquina Linux que presta GitHub (gratis)
    timeout-minutes: 10       # si algo se cuelga, corta a los 10 min
    steps:
      - name: Bajar el codigo del repo
        uses: actions/checkout@v4

      - name: Instalar Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Instalar librerias
        run: pip install pyyaml requests

      - name: Ejecutar xpl.py
        run: python xpl.py
