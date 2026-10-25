# Flow forecaster on Berka (benchmark only)

n = 300 accounts (seeded sample with >= 365 days of history before the origin and 90 days after it; origin = 90 days before the last transaction). Target: daily net unscheduled flow (Berka codes mapped by loaders.berka_categories; loan instalments and standing orders are fixed obligations, left out), days 1-90, and its cumulative sum at days 30 and 90. Flow forecaster and bootstrap: 1,000 sample paths; the forecaster's balance term sees the opening balance plus the realised non-unscheduled flows. Same weekday last week is a point forecast (CRPS = absolute error). Lower is better; ratio = model / same weekday last week; paired bootstrap over accounts (2,000 resamples). Data: Kaggle copy of PKDD'99 (SOURCES.md).

| metric | model | mean (Rs) | vs same weekday | diff 95% CI (Rs) |
|---|---|---|---|---|
| crps_daily | flow_forecaster | 791.8 | 0.611 | -671.7 to -362.4 |
| crps_daily | bootstrap_8w | 878.6 | 0.678 | -581.8 to -279.2 |
| crps_daily | same_weekday_last_week | 1,296.2 | 1.000 |  |
| pinball_daily | flow_forecaster | 368.3 | 0.568 | -362.9 to -208.3 |
| pinball_daily | bootstrap_8w | 437.9 | 0.676 | -291.5 to -141.3 |
| pinball_daily | same_weekday_last_week | 648.1 | 1.000 |  |
| crps_cum30 | flow_forecaster | 6,223.0 | 0.369 | -13,876.1 to -7,635.4 |
| crps_cum30 | bootstrap_8w | 6,101.1 | 0.362 | -14,053.5 to -7,815.0 |
| crps_cum30 | same_weekday_last_week | 16,842.9 | 1.000 |  |
| pinball_cum30 | flow_forecaster | 2,809.5 | 0.334 | -7,253.1 to -4,116.9 |
| pinball_cum30 | bootstrap_8w | 2,753.1 | 0.327 | -7,330.8 to -4,202.7 |
| pinball_cum30 | same_weekday_last_week | 8,421.4 | 1.000 |  |
| crps_cum90 | flow_forecaster | 12,634.9 | 0.251 | -48,437.4 to -28,150.4 |
| crps_cum90 | bootstrap_8w | 13,586.7 | 0.270 | -46,858.3 to -27,783.6 |
| crps_cum90 | same_weekday_last_week | 50,368.9 | 1.000 |  |
| pinball_cum90 | flow_forecaster | 5,582.6 | 0.222 | -24,908.5 to -14,789.9 |
| pinball_cum90 | bootstrap_8w | 6,040.0 | 0.240 | -24,213.3 to -14,596.4 |
| pinball_cum90 | same_weekday_last_week | 25,184.4 | 1.000 |  |
