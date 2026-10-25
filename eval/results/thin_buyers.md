# Group shrinkage for thin buyers (payment-timing model)

30 generator worlds (borderline config with 40 buyers), past seeds 0-29. Thin = 1-3 paid bills at as_of, heavy >= 20. Error = |posterior mean - true value|; difference = with shrinkage minus type prior only (negative favours shrinkage), paired bootstrap over buyers.

| buyers | quantity | MAE with shrinkage | MAE type prior only | difference (95% CI) | better in | verdict |
|---|---|---|---|---|---|---|
| thin (n = 252) | weekly chance once due, p | 0.0851 | 0.0816 | +0.0035 (+0.0002 to +0.0069) | 40% | shrinkage worse |
| thin (n = 252) | weekly chance before due, p_early | 0.0348 | 0.0330 | +0.0018 (+0.0005 to +0.0032) | 43% | shrinkage worse |
| thin (n = 252) | expected weeks to pay once due, 1/p | 7.9638 | 4.5967 | +3.3671 (+1.4598 to +6.0590) | 34% | shrinkage worse |
| heavy (n = 80) | weekly chance once due, p | 0.0538 | 0.0532 | +0.0006 (-0.0015 to +0.0029) | 49% | no clear difference |
| heavy (n = 80) | weekly chance before due, p_early | 0.0165 | 0.0158 | +0.0007 (+0.0002 to +0.0012) | 40% | shrinkage worse |
| heavy (n = 80) | expected weeks to pay once due, 1/p | 1.0873 | 1.0751 | +0.0122 (-0.0314 to +0.0570) | 50% | no clear difference |
