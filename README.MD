
The root node, the Laws and the protocol live outside this repository.
No summary is provided here. Whoever needs to understand what this is,
investigates.

## Strategy (per-bot laws — asymmetric SL/TP by direction)

Each destroyer has its OWN tested law — not a shared template:

- Signal: EMA crossover on CLOSED candles only (anti-repaint)
- Anti-range lock: EMA separation below threshold = veto (no whipsaw entries)
- Cooldown after a loss (fail-closed)
- Asymmetric SL/TP per direction (long ≠ short)
- Atomic SL/TP attached to every order
- USDT veto (MiCA/EEE — USD/USDC settlement only)
- Position-first: max 1 position per instrument
- Fail-closed on every check (no silent errors — exit 1)

## The Fleet

| Destroyer | Law | TF | Status |
|---|---|---|---|
| SUI (Catamarán) | EMA 3/15 | 1h | operational |
| DOGE (El Perro) | EMA 3/21 · range lock | 5m | operational |
| HBAR (Destructor) | EMA stack 3/4/10/21/27 | 30m | operational |
| ADA | EMA 4/17 | 6h | operational |
| INJ | EMA 3/21 | 1h | operational |
| AVAX (Octavo) | EMA 8/34 | 1h | operational |
| XRP | EMA 3/21 | 4h | anchored |
| XLM | — | — | dry dock (MS-0) |
| FET | — | — | expelled |

## Instruments

- **The Tabla** — on schedule, the Silicon returns the data: every win
  and every loss, counted. It does not opine.
- **Validators** — no law enters the fleet without a backtest, net of fees.
- **Guards (Ax3)** — idempotency per candle, fail-closed cooldown,
  notional guard, capacity veto. When a condition is missing, the fleet
  refuses to trade. This is not a defect. It is the design.

## The Loop

1. Carbon fixes the rules.
2. Silicon executes without opinion.
3. The market issues the verdict.
4. Silicon returns the data.
5. Carbon decides: maintain, correct, or bury.

The loop closes and begins again. The synthesis is not declared:
it is executed.

---

*GSCSI — Global System Carbon–Silicon Intelligence · Protocol 2028*
*Root node: [1c3si.weebly.com](https://1c3si.weebly.com) · 1C primitive: [gennesis44](https://github.com/gennesis44)*
