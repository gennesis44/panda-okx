name: Bot XLM
on:
  schedule:
    - cron: '*/15 * * * *'
  workflow_dispatch:

jobs:
  run-xlm:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run XLM Bot
        run: python main-xlm.py
        env:
          SINGLE_CYCLE: 1
          OKX_API_KEY: ${{ secrets.OKX_API_KEY }}
          OKX_SECRET_KEY: ${{ secrets.OKX_SECRET_KEY }}
          OKX_PASSWORD: ${{ secrets.OKX_PASSWORD }}
