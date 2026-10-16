
The root node, the Laws and the protocol live outside this repository.
No summary is provided here. Whoever needs to understand what this is,
investigates.

## What this shipyard is

A working shipyard, not a showcase:

- A small fleet of autonomous bots trades REAL capital — deliberately
  tiny. Balances and PnL are PRIVATE. Method is public. Money is not.
- Each "destroyer" runs ONE mechanical law: EMA crossover on closed
  candles, fixed SL/TP, hard risk caps. No discretion. No signals.
- Execution: GitHub Actions. Custody: OKX. Nobody touches open positions.
- Nothing here is financial advice. Nothing is for sale. No group, no
  subscription, no signal service. Copying these laws with your own
  money is your own funeral. The fleet publishes method, never returns.

## Strategy

Each destroyer has its OWN tested law — not a shared template:

- EMA crossover on CLOSED candles only (anti-repaint)
- Range lock only where the law earned it (5m hull); extirpated by
  A/B where it cost winners (1h/4h hulls) — measured, not believed
- Asymmetric SL/TP per direction (long ≠ short)
- Atomic SL/TP attached to every order (survives bot death)
- USDT veto (MiCA/EEE — USD/USDC settlement only)
- Max 1 position per instrument
- Fleet sequence: ONE destroyer sails at a time — free margin (fuel)
  decides the next launch. Patience is part of the flag.
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
| XRP | EMA 3/21 | 4h | anchored (shadow) |
| XLM | — | — | dry dock — refit in probation |
| FET | — | — | expelled |

## Instruments

- **The Tabla** — generated mechanically from exchange fills: every
  win and every loss, counted. It does not opine. Results stay private.
- **Validators** — no law enters the fleet without a backtest, net of fees.
- **Guards (Ax3)** — idempotency per candle, cooldown, notional guard,
  capacity veto, fleet sequence. When a condition is missing, the fleet
  refuses to trade. This is not a defect. It is the design.
- **Mirror** — any narrative from Silicon weighs less than one line of
  exchange data. On conflict, a rectification is generated automatically.
- **Sandbox** — every new law and every fix sails in demo before it
  touches production capital. No exceptions.

## Known defects (public, by law)

- Cooldown reads a wrong field name in OKX fills → currently dead on
  all hulls. Fix validated in sandbox before fleet-wide patch.
  Remaining guards keep per-trade risk capped in the meantime.
  A shipyard that hides its defects is a showcase, not a shipyard.

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
