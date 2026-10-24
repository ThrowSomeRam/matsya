# Kaggle payment timing (benchmark only)

Train: invoices issued before 2019-07-01 (n = 17644, paid by then 15997, censored 1647), tenants U001, CA02, U013. Test: issued 2019-07-01 to 2019-08-31 (n = 5955), followed 112 days. Baseline: due date.

| model | c_index | c_index_ci | ibs_1_15w | pred_4w | obs_4w | ece_4w | pred_8w | obs_8w | ece_8w | pred_12w | obs_12w | ece_12w |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| payment_timing_model | 0.735 | 0.727-0.743 | 0.031 | 0.874 | 0.903 | 0.033 | 0.949 | 0.961 | 0.014 | 0.978 | 0.989 | 0.013 |
| due_date | 0.601 | 0.593-0.611 | 0.052 | 0.914 | 0.903 | 0.050 | 0.968 | 0.961 | 0.017 | 0.992 | 0.989 | 0.006 |
| cox | 0.770 | 0.763-0.777 | 0.039 | 0.881 | 0.903 | 0.064 | 0.947 | 0.961 | 0.016 | 0.969 | 0.989 | 0.023 |
| random_survival_forest | 0.790 | 0.781-0.797 | 0.028 | 0.903 | 0.903 | 0.005 | 0.956 | 0.961 | 0.006 | 0.979 | 0.989 | 0.010 |
| lgbm_person_period | 0.754 | 0.744-0.764 | 0.026 | 0.906 | 0.903 | 0.005 | 0.963 | 0.961 | 0.003 | 0.992 | 0.989 | 0.003 |

Right truncation: of invoices issued Jan-Mar 2019 (n = 8603), 0.0108 took more than 83 days and 0.0045 more than 112 days, so the fiscal-year cut removes little at these horizons. Never-paid calibration is not measurable: the dataset holds no unpaid invoices.
