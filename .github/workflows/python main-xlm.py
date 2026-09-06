name: xlm-bot-okx

on:
  schedule:
    # cada 10 min, desfasado del hora en punto (GitHub se congestion a :00)
    - cron: '7,17,27,37,47,57 * * * *'
  workflow_dispatch:
    inputs:
      mode:
        description: 'prod = ciclo normal | test = sonda diagnostico'
        required: false
        default: 'prod'

permissions:
  contents: read

concurrency:
  group: xlm-bot
  cancel-in-progress: false   # NUNCA cancelar una run a mitad de trade

jobs:
  run-bot:
    runs-on: ubuntu-latest
    timeout-minutes: 8
    env:
      SINGLE_CYCLE: '1'
      TEST_MODE: ${{ github.event.inputs.mode == 'test' && '1' || '' }}
      PYTHONUNBUFFERED: '1'
      OKX_API_KEY: ${{ secrets.OKX_API_KEY }}
      OKX_SECRET_KEY: ${{ secrets.OKX_SECRET_KEY }}
      OKX_PASSWORD: ${{ secrets.OKX_PASSWORD }}
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: pip

      - name: Dependencias
        run: pip install -r requirements.txt

      - name: Ciclo del bot
        run: python main.py
