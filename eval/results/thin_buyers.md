# Group shrinkage for thin buyers (payment-timing model)

30 generator worlds (borderline config with 40 buyers), past seeds 0-29. Thin = 1-3 paid bills at as_of, heavy >= 20. Error = |posterior mean - true value|; difference = with shrinkage minus type prior only (negative favours shrinkage), paired bootstrap over buyers.

| buyers | quantity | MAE with shrinkage | MAE type prior only | difference (95% CI) | better in | verdict |
|---|---|---|---|---|---|---|
| thin (n = 252) | weekly chance once due, p | 0.0847 | 0.0816 | +0.0031 (-0.0003 to +0.0065) | 39% | no clear difference |
| thin (n = 252) | weekly chance before due, p_early | 0.0349 | 0.0330 | +0.0019 (+0.0005 to +0.0033) | 42% | shrinkage worse |
| thin (n = 252) | expected weeks to pay once due, 1/p | 5.2439 | 4.5967 | +0.6472 (+0.4152 to +0.8969) | 37% | shrinkage worse |
| heavy (n = 80) | weekly chance once due, p | 0.0537 | 0.0532 | +0.0005 (-0.0016 to +0.0028) | 49% | no clear difference |
| heavy (n = 80) | weekly chance before due, p_early | 0.0165 | 0.0158 | +0.0007 (+0.0002 to +0.0012) | 40% | shrinkage worse |
| heavy (n = 80) | expected weeks to pay once due, 1/p | 1.0835 | 1.0751 | +0.0084 (-0.0335 to +0.0507) | 50% | no clear difference |
