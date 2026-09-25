# panda-okx — GSCSI Trading Suite

Futures trading bots on OKX, operated via GitHub Actions.

## Strategy (per-bot laws — v2, asymmetric SL/TP by direction)

Each destructor has its OWN tested law — not a shared template:

- Signal: EMA crossover on CLOSED candles only (anti-repaint)
- Anti-range lock: EMA separation < 0.15% = veto (no whipsaw entries)
- 60-minute cooldown after a loss (fail-closed)
- Asymmetric SL/TP per direction (long ≠ short)
- Atomic SL/TP attached to every order
- USDT veto (MiCA/EEE — USD/USDC settlement only)
- Position-first: max 1 position per instrument
- Fail-closed on every check (no silent errors — exit 1)

## The Fleet

| Destroyer | Law | TF | Contracts | SL/TP L | SL/TP S | Backtest |
|---|---|---|---|---|---|---|
| SUI (Catamarán) | EMA3/15 | 1h | 10 SUI (~$10) |
