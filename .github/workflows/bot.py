name: bot-ema-okx

on:
  schedule:
    - cron: '*/15 * * * *'     # cada 15 min
  workflow_dispatch:           # lanzamiento manual para pruebas

concurrency:
  group: bot-ema-okx
  cancel-in-progress: true

jobs:
  run-bot:
    runs-on: ubuntu-latest
    timeout-minutes: 8

    # MAPA UNICO DE CREDENCIALES (vale para TODOS los pasos)
    # izquierda: variables que lee bot.py | derecha: nombres REALES de tus secrets
    env:
      OKX_API_KEY: ${{ secrets.OKX_API_KEY }}
      OKX_SECRET_KEY: ${{ secrets.OKX_API_SECRET }}
      OKX_PASSWORD: ${{ secrets.OKX_PASSPHRASE }}
      SINGLE_CYCLE: '1'

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Instalar dependencias
        run: pip install -U "ccxt>=4.4.0" pandas

      - name: Diagnostico de variables (SET/EMPTY + suciedad)
        run: |
          python - <<'EOF'
          import os
          print("--- Diagnostico de entorno ---")
          for v in ['OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSWORD']:
              val = os.getenv(v) or ''
              if not val:
                  print(f"{v}: *** EMPTY *** (secret inexistente o vacio)")
                  continue
              avisos = []
              if val != val.strip():
                  avisos.append('ESPACIOS/SALTOS')
              if val[0] in '\'"':
                  avisos.append('COMILLA_INICIO')
              if val[-1] in '\'"':
                  avisos.append('COMILLA_FIN')
              estado = 'OK' if not avisos else '[' + ', '.join(avisos) + ']'
              print(f"{v}: SET ({len(val)} caracteres) {estado}")
          EOF

      - name: Verificar credenciales OKX (login real)
        run: |
          python - <<'EOF'
          import ccxt, os
          ex = ccxt.okx({
              'apiKey':    (os.getenv('OKX_API_KEY') or '').strip(),
              'secret':    (os.getenv('OKX_SECRET_KEY') or '').strip(),
              'password':  (os.getenv('OKX_PASSWORD') or '').strip(),
              'enableRateLimit': True,
              'options': {'defaultType': 'swap'},
          })
          try:
              bal = ex.fetch_balance()
              usdt = (bal.get('USDT') or {}).get('free')
              print(f"OK: Autenticacion correcta | USDT libre: {usdt}")
          except Exception as e:
              msg = str(e)
              print(f"FALLO de autenticacion: {msg}")
              if '50119' in msg:
                  print("50119 = la API key no existe en OKX REAL:")
                  print(" - key borrada/invalida o creada en DEMO -> crear una nueva en OKX")
                  print(" - o valor del secret sucio (comillas/espacios) -> recrear secrets")
              print("Checklist: key de TRADING REAL, permiso Trade, SIN whitelist de IP.")
              raise SystemExit(1)
          EOF

      - name: Ejecutar ciclo del bot
        run: python bot.py
