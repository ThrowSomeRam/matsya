# Berka outside check (benchmark only)

n = 682 loans (76 bad: status B or D). Features only from transactions before each loan date. Engine at 10,000 paths; fixed flows from the obligations detector; unscheduled flows from the flow forecaster (engine_quantile_*, modes: {'quantile-reg-v1': np.int64(682)}) or the earlier same-weekday bootstrap (engine_bootstrap_*), same seeds. Data: Kaggle copy of PKDD'99 (SOURCES.md), so numbers are comparable only with runs on that copy. Stratified bootstrap, 2,000 resamples. Best balance-only baseline: balance_logistic_cv5. Median history before the loan: 394 days (min 101).

| score | auc | ci95 | minus_balance_logistic_cv5 | diff_ci95 |
|---|---|---|---|---|
| engine_quantile_p_short_90_thr0 | 0.587 | 0.509-0.662 | -0.221 | -0.299--0.140 |
| engine_quantile_p_short_90_thr_payment | 0.609 | 0.529-0.688 | -0.199 | -0.277--0.118 |
| engine_quantile_p_short_30_thr0 | 0.653 | 0.579-0.722 | -0.155 | -0.233--0.074 |
| engine_bootstrap_p_short_90_thr0 | 0.639 | 0.558-0.716 | -0.170 | -0.249--0.087 |
| engine_bootstrap_p_short_90_thr_payment | 0.652 | 0.568-0.730 | -0.156 | -0.237--0.074 |
| engine_bootstrap_p_short_30_thr0 | 0.658 | 0.580-0.733 | -0.150 | -0.230--0.068 |
| balance_last_neg | 0.715 | 0.641-0.784 | -0.093 | -0.169--0.022 |
| balance_mean90_neg | 0.748 | 0.678-0.808 | -0.061 | -0.113--0.011 |
| balance_min90_neg | 0.800 | 0.733-0.859 | -0.008 | -0.038-0.026 |
| balance_logistic_cv5 | 0.808 | 0.742-0.870 | 0.000 | 0.000-0.000 |

Lead time is not measurable: the dataset gives each loan's final status, not when it went bad.
