# Synthetic vs real invoices (benchmark only)

Generator: default (borderline) worlds, past seeds [0, 1, 2, 3, 4], paid invoices issued by as_of. Real: Kaggle tenants U001, CA02, U013, issued 2019-01-01 to 2019-09-30. Both cut at days to pay <= 112 (the Kaggle truncation). Features are scale-free (amount relative to the tenant median, weekly count relative to the tenant mean). Kaggle has no part payments, so the part-payment rate separates trivially; the second variant drops it. Kaggle tenants have about 600 customers each against the generator's 10 buyers, so customer share is structural too; the third variant keeps only per-invoice features.

## all_features: AUC 0.999 (5-fold CV, n = 28087: synthetic 1807, real 26280)

| feature | perm_importance_auc_drop | univariate_auc |
|---|---|---|
| customer_part_pay_rate | 0.111 | 0.982 |
| customer_share | 0.003 | 0.775 |
| terms | 0.001 | 0.792 |
| week_count_rel | 0.001 | 0.619 |
| days_to_pay | 0.001 | 0.844 |
| days_late | 0.000 | 0.730 |
| log_amount_rel | 0.000 | 0.511 |

## without_part_pay: AUC 0.999 (5-fold CV, n = 28087: synthetic 1807, real 26280)

| feature | perm_importance_auc_drop | univariate_auc |
|---|---|---|
| customer_share | 0.120 | 0.775 |
| terms | 0.016 | 0.792 |
| week_count_rel | 0.001 | 0.619 |
| days_to_pay | 0.000 | 0.844 |
| log_amount_rel | 0.000 | 0.511 |
| days_late | 0.000 | 0.730 |

## invoice_level: AUC 1.000 (5-fold CV, n = 28087: synthetic 1807, real 26280)

| feature | perm_importance_auc_drop | univariate_auc |
|---|---|---|
| week_count_rel | 0.125 | 0.619 |
| terms | 0.007 | 0.792 |
| days_to_pay | 0.000 | 0.844 |
| log_amount_rel | 0.000 | 0.511 |
| days_late | 0.000 | 0.730 |

## Feature distributions

| statistic | log_amount_rel | terms | days_to_pay | days_late | customer_part_pay_rate | week_count_rel | customer_share | source |
|---|---|---|---|---|---|---|---|---|
| mean | 0.00 | 30.23 | 44.13 | 13.90 | 0.10 | 1.28 | 0.16 | synthetic |
| std | 0.81 | 12.33 | 24.46 | 24.45 | 0.04 | 0.56 | 0.10 | synthetic |
| 25% | -0.54 | 15.00 | 26.00 | 0.00 | 0.08 | 0.87 | 0.10 | synthetic |
| 50% | -0.00 | 30.00 | 42.00 | 10.00 | 0.10 | 1.16 | 0.13 | synthetic |
| 75% | 0.53 | 45.00 | 60.00 | 28.00 | 0.12 | 1.67 | 0.18 | synthetic |
| mean | -0.37 | 17.42 | 18.26 | 0.85 | 0.00 | 1.03 | 0.07 | kaggle |
| std | 1.79 | 11.00 | 13.04 | 10.44 | 0.00 | 0.13 | 0.10 | kaggle |
| 25% | -1.35 | 15.00 | 13.00 | -3.00 | 0.00 | 0.97 | 0.00 | kaggle |
| 50% | 0.00 | 15.00 | 15.00 | 0.00 | 0.00 | 1.04 | 0.02 | kaggle |
| 75% | 0.99 | 15.00 | 17.00 | 2.00 | 0.00 | 1.11 | 0.13 | kaggle |
