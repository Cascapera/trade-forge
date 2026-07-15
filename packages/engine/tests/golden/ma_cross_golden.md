# Golden backtest — MA cross, worked by hand

The dataset is `ma_cross_golden.csv` (16 H1 EURUSD candles). This worksheet derives every
trade and P&L by hand; `test_golden_backtest.py` asserts the engine reproduces it exactly.

**Setup.** EURUSD: `tick_size = 0.00001`, `tick_value = 1`, so `money = price_move × 100 000 × volume`.
Strategy: enter long when `SMA(2)` **crosses above** `SMA(3)`; stop = `candle_extreme(lookback=2, side=low)`
(the lowest low of the last two closed bars); target = `risk_multiple(rr=2)`. Sizing = `percent_risk(1%)`.
No costs, no slippage — the golden isolates the fill and sizing logic. Initial capital `$10 000`.

## Indicators (close-basis)

| bar | close | SMA2 (fast) | SMA3 (slow) | relation |
|----:|------:|------------:|------------:|:---------|
| 0 | 1.09950 | — | — | |
| 1 | 1.09850 | 1.09900 | — | |
| 2 | 1.09750 | 1.09800 | 1.09850 | fast < slow |
| 3 | 1.10100 | 1.09925 | 1.09900 | **fast crosses above** ⇒ decide entry |
| 4 | 1.10400 | 1.10250 | 1.10083 | (in position) |
| 5 | 1.10900 | 1.10650 | 1.10467 | (target hit) |
| 6 | 1.10600 | 1.10750 | 1.10633 | fast > slow |
| 7 | 1.10300 | 1.10450 | 1.10600 | fast crosses below |
| 8 | 1.10100 | 1.10200 | 1.10333 | fast < slow |
| 9 | 1.10500 | 1.10300 | 1.10300 | equal (not a cross: `>` is strict) |
| 10 | 1.10600 | 1.10550 | 1.10400 | **fast crosses above** ⇒ decide entry |
| 11 | 1.10150 | 1.10375 | 1.10417 | (entry fills, stopped same bar) |
| 12 | 1.09950 | 1.10050 | 1.10233 | fast < slow |
| 13 | 1.09850 | 1.09900 | 1.09983 | fast < slow |
| 14 | 1.09800 | 1.09825 | 1.09867 | fast < slow |
| 15 | 1.09750 | 1.09775 | 1.09800 | fast < slow |

## Trade 1 — target (long)

- **Decision** on bar 3 (cross above). **Stop** = min(low₃, low₂) = min(1.09750, 1.09700) = **1.09700**.
- **Reference** = close₃ = 1.10100. **Stop distance** = 1.10100 − 1.09700 = 0.00400.
- **Size**: risk 1% of $10 000 = $100. Loss per lot at the stop = 0.00400 × 100 000 = $400. **Volume = 100 / 400 = 0.25**.
- **Entry** at the open of bar 4 = **1.10100**. **Target** = 1.10100 + 2 × 0.00400 = **1.10900**.
- Bar 5 high 1.11000 ≥ 1.10900 ⇒ **take-profit at 1.10900**.
- **P&L** = (1.10900 − 1.10100) × 100 000 × 0.25 = 0.00800 × 100 000 × 0.25 = **+$200.00**. Equity → **$10 200**.

## Trade 2 — stop, on the entry bar (long)

- **Decision** on bar 10 (cross above). **Stop** = min(low₁₀, low₉) = min(1.10450, 1.10100) = **1.10100**.
- **Reference** = close₁₀ = 1.10600. **Stop distance** = 1.10600 − 1.10100 = 0.00500.
- **Size**: equity is now $10 200, so risk 1% = **$102**. Loss per lot = 0.00500 × 100 000 = $500.
  Raw volume = 102 / 500 = 0.204, **floored to the 0.01 lot step ⇒ 0.20** (a broker will not fill 0.204 lots).
- **Entry** at the open of bar 11 = **1.10600**. **Target** = 1.10600 + 2 × 0.00500 = 1.11600.
- Bar 11 low 1.10100 ≤ stop 1.10100 ⇒ **stopped out on its own entry bar**, filled at the stop = **1.10100**
  (the protective exit inherits bar 10 as its decision instant, so the lookahead guard accepts it).
- **P&L** = (1.10100 − 1.10600) × 100 000 × 0.20 = −0.00500 × 100 000 × 0.20 = **−$100.00**. Equity → **$10 100**.

## Reconciliation

Sum of net P&L = +200.00 − 100.00 = **+$100.00** = final equity − initial = 10 100 − 10 000. The run ends flat.
