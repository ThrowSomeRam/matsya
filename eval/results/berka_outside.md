# Berka outside check (benchmark only)

n = 682 loans (76 bad: status B or D). Features only from transactions before each loan date. Engine at 10,000 paths; flows from the obligations detector and the unscheduled bootstrap (no flow forecaster yet). Stratified bootstrap, 2,000 resamples. Best balance-only baseline: balance_logistic_cv5. Median history before the loan: 394 days (min 101).

| score | auc | ci95 | minus_balance_logistic_cv5 | diff_ci95 |
|---|---|---|---|---|
| engine_p_short_90_thr0 | 0.639 | 0.558-0.716 | -0.170 | -0.249--0.087 |
| engine_p_short_90_thr_payment | 0.652 | 0.568-0.730 | -0.156 | -0.237--0.074 |
| engine_p_short_30_thr0 | 0.658 | 0.580-0.733 | -0.150 | -0.230--0.068 |
| balance_last_neg | 0.715 | 0.641-0.784 | -0.093 | -0.169--0.022 |
| balance_mean90_neg | 0.748 | 0.678-0.808 | -0.061 | -0.113--0.011 |
| balance_min90_neg | 0.800 | 0.733-0.859 | -0.008 | -0.038-0.026 |
| balance_logistic_cv5 | 0.808 | 0.742-0.870 | 0.000 | 0.000-0.000 |

Lead time is not measurable: the dataset gives each loan's final status, not when it went bad.
