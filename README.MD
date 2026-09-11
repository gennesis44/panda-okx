# panda-okx — GSCSI Trading Suite

Futures trading bots on OKX (XLM, DOGE, FET, SUI), operated via GitHub Actions.

## Strategy (Ax2-D)

- EMA7/21 crossover on the signal candle (15m; 30m for FET) — CLOSED candles only
- Regime gate: 4H direction (EMA7 vs EMA21) — trades only WITH the tide
- 60-minute cooldown after a loss (anti-chop guard)
- Atomic SL/TP attached to every order

## Bots

| Bot | Signal TF | Contract | SL/TP |
|---|---|---|---|
| XLM | 15m | 100 XLM = $18 | 1% / 1.5% |
| DOGE | 15m | 20 DOGE = $1.7 | 1% / 1.5% |
| FET | 30m | 20 FET = $3.5 | 1% / 1.5% |
| SUI | 15m | 2 SUI = $1.5 | 1.5% / 2.5% |

## Tools

- main-hy.py = scoreboard (real winrate and PnL)
- okx_radar.py, senal-hy.py = observers (read-only)
- ema-hy.py, bt-sftp.py, btc-ma.py = backtests

Educational use — not financial advice. C > Si — Operator SHA-256
