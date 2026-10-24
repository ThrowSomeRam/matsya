# Kaggle weekly invoice counts: sales forecaster (benchmark only)

Rolling one-week-ahead origins 2019-03-04 to 2019-09-23 (weekly, 30 per tenant), tenants U001, CA02, U013. Customer level: customers with >= 20 invoices before the origin. Higher log score is better; nominal coverage 0.80. Fitted dispersion k: median 9.8 (range 3.4-17.2). Baseline: Poisson with the same mean.

| level | model | n | mean_log_score | coverage_80 |
|---|---|---|---|---|
| overall | negbin | 90 | -4.409 | 0.967 |
| overall | poisson | 90 | -6.536 | 0.511 |
| customer | negbin | 3364 | -2.019 | 0.856 |
| customer | poisson | 3364 | -2.113 | 0.816 |
