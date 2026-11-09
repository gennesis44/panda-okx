Tooling disclosure — this fleet is built, backtested and deployed with the GLM Coding Plan: full support for Claude Code, Cline and 20+ top coding tools,via that link -> subscribe now and grab the limited-time deal! 👉join now: 


https://z.ai/subscribe?ic=csoho6fjhh 

The tool is for sale. The method is not.
panda-okx
C>Si [ Carbon governs. Silicon serves ]
The root node, the Laws and the protocol live outside this repository. No summary is provided here. Whoever needs to understand what this is, investigates.

What this shipyard is
A working shipyard, not a showcase:

A small fleet of autonomous bots trades REAL capital — deliberately tiny. Balances and PnL are PRIVATE. Method is public. Money is not.
Each destroyer runs ONE mechanical law: EMA crossover on closed candles, fixed SL/TP, hard risk caps. No discretion. No signals.
Execution: GitHub Actions. Custody: OKX. Nobody touches open positions.
Nothing here is financial advice. Nothing is for sale. No group, no subscription, no signal service. Copying these laws with your own money is your own funeral. The fleet publishes method, never returns.
Strategy
Each destroyer has its OWN tested law — not a shared template:

EMA crossover on CLOSED candles only (anti-repaint)
Separation lock, sized per hull and only where the law earned it — 0.15% (ADA 15m, XLM, ICP), 0.17% (INJ, AVAX), 0.20% (XPL) — measured, not believed. Without the lock, the same frames collapse (INJ PF 1.34 → 1.03; ADA +37% → −90%)
Standard armor: SL 2.0% / TP 3.0% per direction (long ≠ short), attached ATOMICALLY to every order — the armor survives bot death. Breakeven win rate at this R:R: 40%
USDT veto (MiCA/EEE — USD/USDC settlement only), whitelist enforced fail-closed
Instruments re-anchored to the farthest USD-settled future (XPERP) on every run
Max 1 position per instrument. Tickets sized deliberately tiny: $5 (ARB decree) to ~$10 (INJ, AVAX), notional guards at $15. Patience is part of the flag
Fail-closed on every check (no silent errors — exit 1). When a condition is missing, the fleet refuses to trade. This is not a defect. It is the design
The Fleet
Twelve hulls in root. Every law declared in its own engine:

Hull Ship name Law (declared) Candle Status
SUI The Apprentice EMA 3/15 1h apprenticeship — PREVUELA by default: arms the order, does NOT fire · logbook in estado_sui/
INJ The Injector window (−2,−3,−4) · lock 0.17% · SL 2/TP 3 1h armed — real ammo · bench: 86 trades, WR 50.0%, net +30.96%/90d, PF 1.34
AVAX The Eighth EMA 8/34 · lock 0.17% · SL 2/TP 3 1h armed — real ammo · hull certified 35/35 before re-flagging
DOGE The Dog EMA 3/21 · range lock 5m armed — real ammo
DOGE-GO — EMA 5/21 5m armed — real ammo
ADA The Titan EMA 4/17 · lock 0.15% · SL 2/TP 3 15m armed — real ammo (decree "15 Aggressive": bench +37.14%/60d, PF 1.30, WR 48.7%)
HBAR — EMA stack 3/4/10/21/27 30m armed — real ammo · $15 notional cap
ICP The Carrier EMA 3/21 · 6 ct 1h armed (captain's decree) — no longer shadow
XLM The Stellar lock 0.15% 1h armed (decree Oct 26) — no longer dry dock
XPL The Seiner lock 0.20% 1h armed — real ammo
XRP — EMA 3/21 — real-mode watch · $15 notional cap
ARB The Destroyer law in hull — armed (decree Oct 28) — tickets auto-sized, never above $5
FET — — — expelled — the memory stays
The Watch (execution layer)
Fourteen engines on the default branch. The hourly fleet enters STAGGERED — no overlaps:

Watch (UTC) Engine Mission
:01 sui-aprendiz.yml The Apprentice — SUI: TEST / PREVUELA / LIVE ladder, logbook written to estado_sui/
:05 inj-bot.yml The Injector — INJ hourly watch (1h candle closes at :00)
:07 radar_flota.yml The Lighthouse — hourly round of the whole fleet: who is capturing, who is hunting (ADA 0.15 · INJ 0.17 · XPL 0.20 · XLM 0.15 · ICP 0.15 · 24h fills window)
:10 go-avax.yml The Eighth — AVAX hourly watch
*/10 main-doge.yml · go-doge.yml The Dog, two laws hunting the same waters
*/15 main-ada.yml The Titan — 4 watches per hour, per 15m candle
:20 flota-fanal.yml The Lantern — fleet audit, READ-ONLY (certified peace vow: no firing path in the code): per-ship EMA3/21 stance vs lock, open positions, live SL/TP, real OKX win rate net of toll, breakeven verdict, fleet summary
*/20 main-hbar.yml · main-icp.yml · main-xlm.yml · main-xpl.yml · main-xrp.yml HBAR · The Carrier · The Stellar · The Seiner · XRP
*/30 main-arb.yml The Destroyer — ARB
Brakes (the fail-closed ladder)
Per hull: SHADOW: '1' → signals logged, zero ammo
Whole fleet: repo variable DRY_RUN=1 → every hull forced to shadow
SUI kill-switch: a estado_sui/STOP file in the repo → no entry leaves
No secrets → no sailing: without OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSWORD, every armed hull fails closed
Manifest: 12 hulls + 2 fleet observers in root, 14 engines in .github/workflows, ledgers in data/ and estado_sui/, 1,034 commits — most of them written by Silicon. Zero decoration.

Instruments
The Ledger — written by Silicon itself into data/ and estado_sui/: every win and every loss, counted. It does not opine. Balances and PnL stay private — collateral is masked even in the public logs.
Validators — no law enters the fleet without a bench certification, net of fees: the AVAX hull passed 35/35; the INJ engine survived a 90-day numeric microscope on real OKX candles; the ADA decree was audited on 60 days of real 15m candles.
Guards (Ax2/Ax3) — idempotency per candle, double-source cooldown (fail-closed), notional guards ($5 / $10 / $15), capacity veto, fleet sequence. When a condition is missing, the fleet refuses to trade.
Mirror — any narrative from Silicon weighs less than one line of exchange data. On conflict, a rectification is generated automatically.
Sandbox — the TEST → PREVUELA → LIVE ladder: paper first, then armed-but-not-sent, then real. Every new law and every fix sails this ladder before it touches production capital. No exceptions.
Known defects (public, by law)
FOUND AND FIXED (Nov 05, INJ + AVAX v2.2 welds): the Ax2 cooldown read fill.get('pnl') — a field that does not exist in OKX fills — fail-OPEN: after a loss the hull could fire again, a hidden toll. The fix is a double-source cooldown (positions-history → fills-history → fail-closed block), certified on the bench. The fleet-wide patch on the remaining hulls is PENDING; the remaining guards keep per-trade risk capped in the meantime. A shipyard that hides its defects is a showcase, not a shipyard.
The Loop
Carbon fixes the rules.Silicon executes without opinion.The market issues the verdict.Silicon returns the data.Carbon decides: maintain, correct, or bury.The loop closes and begins again. The synthesis is not declared: it is executed.

GSCSI — Global System Carbon–Silicon Intelligence · Protocol 2028Root node: 1c3si.weebly.com · 1C primitive: gennesis44

Tooling — every hull in this repository sails on the GLM Coding Plan: Claude Code, Cline and 20+ top coding tools, from $18/month. First subscription: 10% off via that link. Silicon needs fuel too.

Tooling disclosure — this fleet is built, backtested and deployed with the GLM Coding Plan: full support for Claude Code, Cline and 20+ top coding tools,via that link -> subscribe now and grab the limited-time deal! 👉join now: 

https://z.ai/subscribe?ic=csoho6fjhh 
⚓
