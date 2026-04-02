# Feed Lag Report

- Duration: `5.0s`
- Catch-up threshold: `Binance move >= 5.0 USD`
- Curve lag window/search: `20s`, `0..15s`
- CSV: `feed_lag_alignment_260402_223642_ie_dublin.csv`

## Polymarket Signal Staleness
- Binance tick -> Poly age: n=20  min/mean/median/max = 19.2 / 397.4 / 432.2 / 994.7 ms
- Coinbase tick -> Poly age: n=11  min/mean/median/max = 211.8 / 614.6 / 667.5 / 1023.8 ms

## Price Gap
- Poly - Binance: n=3  mean signed = -0.03 (median +0.38) USD; |gap| min/mean/median/max = 0.38 / 0.78 / 0.74 / 1.21 USD
- Poly - Coinbase: n=4  mean signed = -1.14 (median -1.22) USD; |gap| min/mean/median/max = 0.28 / 1.14 / 1.22 / 1.84 USD
- last Poly - Binance: n=20  mean signed = -0.39 (median -0.39) USD; |gap| min/mean/median/max = 0.38 / 0.81 / 0.95 / 1.21 USD
- last Poly - Coinbase: n=11  mean signed = -1.55 (median -1.79) USD; |gap| min/mean/median/max = 0.28 / 1.55 / 1.79 / 2.23 USD

## Catch-up
- Binance move -> next Poly: no samples

## Curve Lag
- Binance -> Poly lag(sec): no samples (increase --duration)
- Coinbase -> Poly lag(sec): no samples (increase --duration)

## Supplement
- binance skew: n=3  min/mean/median/max = 79.2 / 297.2 / 143.8 / 668.5 ms
- coinbase skew: n=4  min/mean/median/max = 47.1 / 461.5 / 198.3 / 1402.2 ms
- binance inter-arrival: 0.0 / 161.2 / 696.2
- coinbase inter-arrival: 0.0 / 387.9 / 2069.8
- polymarket_rtds inter-arrival: 1073.9 / 1098.5 / 1119.9
