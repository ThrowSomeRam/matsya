# Decision engine against baselines across worlds

30 worlds (healthy, borderline, distressed presets x past seeds), eps 0.05 at day 90, 1,000 solve paths, 10,000 fresh paths per world. Small company tenant, overdraft limit Rs 10 lakh, suppliers S1 micro 50% and S2 not MSME 30% on 30-day written terms. Opening balance lowered by the pilot median 90-day minimum (threshold stays 0). Costs in Rs; mean (sd) and median over worlds where the strategy exists.

| strategy | worlds | net cost mean (sd) | net cost median | clamped cost mean | p_short mean (sd) | days below mean |
|---|---|---|---|---|---|---|
| do_nothing | 30 | 0 (0) | 0 | 0 | 0.501 (0.012) | 8.75 |
| discount_all_eligible | 21 | 7,222 (5,892) | 2,903 | 7,222 | 0.387 (0.083) | 5.79 |
| chase_largest_buyer | 30 | 500 (0) | 500 | 500 | 0.470 (0.021) | 7.92 |
| plan | 30 | 54,431 (45,193) | 27,059 | 54,431 | 0.054 (0.005) | 0.50 |

- Do-nothing p_short(90) on fresh paths: mean 0.501, range 0.482-0.531; 30/30 worlds inside 0.2-0.8.
- Plan meets eps strictly (fresh p_short <= 0.05): 5/30. Within Monte Carlo error (<= eps + 2 x 0.0091, the solve and fresh-path se at eps): 30/30.
- Target unreachable with the available actions: 0/30.
- Solve time per world: median 94 s, max 129 s.

## Per world

Loss: plan above eps + 2 se on fresh paths, or a baseline at or below max(eps, plan p_short) for no more net cost.

| world | do-nothing p | plan p (fresh) | plan p (solve) | plan net Rs | plan clamped Rs | actions | target met | loss |
|---|---|---|---|---|---|---|---|---|
| healthy:0 | 0.503 | 0.046 | 0.048 | 10834.960 | 10834.960 | 6 | True | - |
| healthy:1 | 0.483 | 0.054 | 0.047 | 9831.360 | 9831.360 | 6 | True | - |
| healthy:2 | 0.496 | 0.052 | 0.049 | 6415.960 | 6415.960 | 6 | True | - |
| healthy:3 | 0.493 | 0.059 | 0.050 | 14430.830 | 14430.830 | 11 | True | - |
| healthy:4 | 0.489 | 0.053 | 0.050 | 18900.000 | 18900.000 | 6 | True | - |
| healthy:5 | 0.510 | 0.045 | 0.048 | 12957.620 | 12957.620 | 3 | True | - |
| healthy:6 | 0.493 | 0.052 | 0.049 | 8055.640 | 8055.640 | 3 | True | - |
| healthy:7 | 0.498 | 0.053 | 0.050 | 23232.870 | 23232.870 | 11 | True | - |
| healthy:8 | 0.501 | 0.052 | 0.049 | 20026.020 | 20026.020 | 10 | True | - |
| healthy:9 | 0.496 | 0.056 | 0.050 | 20053.390 | 20053.390 | 13 | True | - |
| borderline:0 | 0.508 | 0.067 | 0.050 | 11123.660 | 11123.660 | 15 | True | - |
| borderline:1 | 0.531 | 0.053 | 0.050 | 20856.760 | 20856.760 | 11 | True | - |
| borderline:2 | 0.492 | 0.055 | 0.050 | 75635.020 | 75635.020 | 10 | True | - |
| borderline:3 | 0.514 | 0.059 | 0.050 | 21231.980 | 21231.980 | 9 | True | - |
| borderline:4 | 0.506 | 0.049 | 0.050 | 70728.290 | 70728.290 | 8 | True | - |
| borderline:5 | 0.515 | 0.052 | 0.050 | 12870.810 | 12870.810 | 8 | True | - |
| borderline:6 | 0.501 | 0.058 | 0.050 | 15633.290 | 15633.290 | 12 | True | - |
| borderline:7 | 0.517 | 0.056 | 0.048 | 121060.170 | 121060.170 | 6 | True | - |
| borderline:8 | 0.522 | 0.048 | 0.050 | 84853.230 | 84853.230 | 8 | True | - |
| borderline:9 | 0.510 | 0.052 | 0.047 | 70202.380 | 70202.380 | 14 | True | - |
| distressed:0 | 0.505 | 0.049 | 0.049 | 95559.470 | 95559.470 | 12 | True | - |
| distressed:1 | 0.491 | 0.054 | 0.049 | 92569.750 | 92569.750 | 11 | True | - |
| distressed:2 | 0.482 | 0.054 | 0.050 | 121855.690 | 121855.690 | 8 | True | - |
| distressed:3 | 0.500 | 0.058 | 0.046 | 79633.580 | 79633.580 | 5 | True | - |
| distressed:4 | 0.490 | 0.056 | 0.050 | 102989.460 | 102989.460 | 7 | True | - |
| distressed:5 | 0.486 | 0.058 | 0.048 | 30885.310 | 30885.310 | 14 | True | - |
| distressed:6 | 0.512 | 0.057 | 0.050 | 102773.190 | 102773.190 | 7 | True | - |
| distressed:7 | 0.501 | 0.058 | 0.049 | 162505.230 | 162505.230 | 5 | True | - |
| distressed:8 | 0.499 | 0.054 | 0.050 | 110094.790 | 110094.790 | 7 | True | - |
| distressed:9 | 0.486 | 0.061 | 0.050 | 85118.430 | 85118.430 | 8 | True | - |

Losses: 0/30.
