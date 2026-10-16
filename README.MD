The root node, the Laws and the protocol live outside this repository.No summary is provided here. Whoever needs to understand what this is,investigates.

What this shipyard is (for visitors)
This is a working shipyard, not a showcase:

A small fleet of autonomous trading bots operates on REAL capital —deliberately tiny. Balances, PnL and results are PRIVATE. The methodis public. The code is public. The money is not.
Each bot ("destroyer") executes ONE mechanical law: EMA crossover onclosed candles, fixed SL/TP, hard risk caps. No discretion. No signals.No predictions. The market is the only judge.
Execution runs on GitHub Actions. Custody lives on OKX. Nobody —human or AI — touches an open position.
Nothing here is financial advice. Nothing is for sale. There is nogroup, no subscription, no signal service. If you came for signals:there are none. If you came for PnL screenshots: welded shut.If you came to see how discipline is engineered: read the code.
Copying these laws with your own money is your own funeral. The fleetpublishes method, never returns.
Strategy (per-bot laws — asymmetric SL/TP by direction)
Each destroyer has its OWN tested law — not a shared template:

Signal: EMA crossover on CLOSED candles only (anti-repaint)
Range lock: only where the law earned it (5m hull). Extirpated byA/B where it cost winners (1h/4h hulls) — measured, not believed
Cooldown after a loss (fail-closed by design — see Known defects)
Asymmetric SL/TP per direction (long ≠ short)
Atomic SL/TP attached to every order (survives bot death)
USDT veto (MiCA/EEE — USD/USDC settlement only)
Position-first: max 1 position per instrument
Fleet sequence: ONE destroyer sails at a time. Free margin (fuel)decides the next launch. Patience is part of the flag.
Fail-closed on every check (no silent errors — exit 1)
The Fleet
Destroyer Law TF Status
SUI (Catamarán) EMA 3/15 1h operational
DOGE (El Perro) EMA 3/21 · range lock 5m operational
HBAR (Destructor) EMA stack 3/4/10/21/27 30m operational
ADA EMA 4/17 6h operational
INJ EMA 3/21 1h operational
AVAX (Octavo) EMA 8/34 1h operational
XRP EMA 3/21 4h anchored (shadow ledger)
XLM — — dry dock — refit in probation (testnet first)
FET — — expelled
Instruments
The Tabla — generated mechanically from exchange fills: every winand every loss, counted. It does not opine. Its results stay private.
Validators — no law enters the fleet without a backtest, net of fees.
Guards (Ax3) — idempotency per candle, cooldown, notional guard,capacity veto, fleet sequence. When a condition is missing, the fleetrefuses to trade. This is not a defect. It is the design.
Mirror — any narrative from Silicon weighs less than one line ofexchange data. On conflict, a rectification is generated automatically.
Sandbox — every new law and every fix sails in demo before ittouches production capital. No exceptions.
Known defects (public, by law)
Cooldown reads a wrong field name in OKX fills → currently dead onall hulls. Fix is validated in sandbox before fleet-wide patch.Remaining guards keep per-trade risk capped in the meantime.A shipyard that hides its defects is a showcase, not a shipyard.
The Loop
Carbon fixes the rules.
Silicon executes without opinion.
The market issues the verdict.
Silicon returns the data.
Carbon decides: maintain, correct, or bury.
The loop closes and begins again. The synthesis is not declared:it is executed.

GSCSI — Global System Carbon–Silicon Intelligence · Protocol 2028Root node: 1c3si.weebly.com · 1C primitive: gennesis44
