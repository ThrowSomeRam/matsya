# Kaggle payment timing (benchmark only)

Train: invoices issued before 2019-07-01 (n = 17644, paid by then 15997, censored 1647), tenants U001, CA02, U013. Test: issued 2019-07-01 to 2019-08-31 (n = 5955), followed 112 days. Baseline: due date.

| model | c_index | c_index_ci | ibs_1_15w | pred_4w | obs_4w | ece_4w | pred_8w | obs_8w | ece_8w | pred_12w | obs_12w | ece_12w |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| payment_timing_model | 0.735 | 0.726-0.745 | 0.031 | 0.874 | 0.903 | 0.033 | 0.949 | 0.961 | 0.014 | 0.978 | 0.989 | 0.013 |
| payment_timing_model_empirical | 0.741 | 0.733-0.748 | 0.027 | 0.878 | 0.903 | 0.028 | 0.951 | 0.961 | 0.011 | 0.981 | 0.989 | 0.009 |
| due_date | 0.601 | 0.593-0.611 | 0.052 | 0.914 | 0.903 | 0.050 | 0.968 | 0.961 | 0.017 | 0.992 | 0.989 | 0.006 |
| cox | 0.770 | 0.763-0.778 | 0.039 | 0.881 | 0.903 | 0.064 | 0.947 | 0.961 | 0.016 | 0.969 | 0.989 | 0.023 |
| random_survival_forest | 0.785 | 0.777-0.793 | 0.028 | 0.902 | 0.903 | 0.007 | 0.955 | 0.961 | 0.007 | 0.979 | 0.989 | 0.012 |
| lgbm_person_period | 0.754 | 0.746-0.765 | 0.026 | 0.906 | 0.903 | 0.005 | 0.963 | 0.961 | 0.003 | 0.992 | 0.989 | 0.003 |

Share paid in each week since issue, predicted / observed:

| model | w1 | w2 | w3 | w4 | w5 | w6 |
|---|---|---|---|---|---|---|
| payment_timing_model | 0.127 / 0.015 | 0.136 / 0.258 | 0.524 / 0.594 | 0.087 / 0.037 | 0.038 / 0.024 | 0.017 / 0.016 |
| payment_timing_model_empirical | 0.029 / 0.015 | 0.245 / 0.258 | 0.545 / 0.594 | 0.058 / 0.037 | 0.034 / 0.024 | 0.018 / 0.016 |
| due_date | 0.017 / 0.015 | 0.105 / 0.258 | 0.766 / 0.594 | 0.025 / 0.037 | 0.032 / 0.024 | 0.014 / 0.016 |
| cox | 0.014 / 0.015 | 0.304 / 0.258 | 0.515 / 0.594 | 0.048 / 0.037 | 0.029 / 0.024 | 0.020 / 0.016 |
| random_survival_forest | 0.009 / 0.015 | 0.260 / 0.258 | 0.599 / 0.594 | 0.035 / 0.037 | 0.021 / 0.024 | 0.016 / 0.016 |
| lgbm_person_period | 0.009 / 0.015 | 0.305 / 0.258 | 0.559 / 0.594 | 0.034 / 0.037 | 0.023 / 0.024 | 0.018 / 0.016 |

Right truncation: of invoices issued Jan-Mar 2019 (n = 8603), 0.0108 took more than 83 days and 0.0045 more than 112 days, so the fiscal-year cut removes little at these horizons. Never-paid calibration is not measurable: the dataset holds no unpaid invoices.
