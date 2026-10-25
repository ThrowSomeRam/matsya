# Decision engine against baselines across worlds

30 worlds (healthy, borderline, distressed presets x past seeds), eps 0.05 at day 90, 1,000 solve paths, 10,000 fresh paths per world. Small company tenant, overdraft limit Rs 10 lakh, suppliers S1 micro 50% and S2 not MSME 30% on 30-day written terms. Opening balance lowered by the pilot median 90-day minimum (threshold stays 0). Costs in Rs; mean (sd) and median over worlds where the strategy exists.

| strategy | worlds | net cost mean (sd) | net cost median | clamped cost mean | p_short mean (sd) | days below mean |
|---|---|---|---|---|---|---|
| do_nothing | 30 | 0 (0) | 0 | 0 | 0.501 (0.012) | 8.75 |
| discount_all_eligible | 21 | 7,222 (5,892) | 2,903 | 7,222 | 0.387 (0.083) | 5.79 |
| chase_largest_buyer | 30 | 500 (0) | 500 | 500 | 0.470 (0.021) | 7.92 |
| plan | 30 | 61,899 (49,097) | 50,819 | 61,899 | 0.046 (0.005) | 0.40 |

- Do-nothing p_short(90) on fresh paths: mean 0.501, range 0.482-0.531; 30/30 worlds inside 0.2-0.8.
- Plan meets eps strictly (fresh p_short <= 0.05): 24/30. Within Monte Carlo error (<= eps + 2 x 0.0091, the solve and fresh-path se at eps): 30/30.
- Target unreachable with the available actions: 0/30.
- Solver stopped at the time limit: 5/30; MIP gap median 0.0000, max 0.8737.
- Money owed (MSMED claims, reported apart from the plan): 1453 invoices across worlds, 0 worth claiming (net value after tax and penalty > 0), Rs 0 net in total.
- Solve time per world: median 86 s, max 135 s.

## Per world

Loss: plan above eps + 2 se on fresh paths, or a baseline at or below max(eps, plan p_short) for no more net cost.

| world | do-nothing p | plan p (fresh) | plan p (solve) | plan net Rs | plan clamped Rs | actions | target met | solver | gap | loss |
|---|---|---|---|---|---|---|---|---|---|---|
| healthy:0 | 0.503 | 0.041 | 0.040 | 11334.960 | 11334.960 | 7 | True | optimal | 0.0000 | - |
| healthy:1 | 0.483 | 0.046 | 0.040 | 10026.040 | 10026.040 | 7 | True | optimal | 0.0000 | - |
| healthy:2 | 0.496 | 0.044 | 0.040 | 7636.410 | 7636.410 | 7 | True | optimal | 0.0000 | - |
| healthy:3 | 0.493 | 0.052 | 0.039 | 15898.460 | 15898.460 | 8 | True | time limit | 0.3064 | - |
| healthy:4 | 0.489 | 0.039 | 0.040 | 21238.350 | 21238.350 | 7 | True | optimal | 0.0000 | - |
| healthy:5 | 0.510 | 0.037 | 0.038 | 13183.400 | 13183.400 | 4 | True | optimal | 0.0000 | - |
| healthy:6 | 0.493 | 0.040 | 0.030 | 8555.640 | 8555.640 | 4 | True | optimal | 0.0000 | - |
| healthy:7 | 0.498 | 0.043 | 0.040 | 26415.060 | 26415.060 | 11 | True | optimal | 0.0000 | - |
| healthy:8 | 0.501 | 0.043 | 0.040 | 22316.430 | 22316.430 | 11 | True | optimal | 0.0000 | - |
| healthy:9 | 0.496 | 0.044 | 0.040 | 23187.300 | 23187.300 | 10 | True | optimal | 0.0000 | - |
| borderline:0 | 0.508 | 0.052 | 0.040 | 14661.490 | 14661.490 | 13 | True | optimal | 0.0000 | - |
| borderline:1 | 0.531 | 0.038 | 0.040 | 26003.340 | 26003.340 | 14 | True | optimal | 0.0000 | - |
| borderline:2 | 0.492 | 0.039 | 0.040 | 81107.630 | 81107.630 | 10 | True | optimal | 0.0000 | - |
| borderline:3 | 0.514 | 0.053 | 0.040 | 23755.730 | 23755.730 | 9 | True | time limit | 0.1194 | - |
| borderline:4 | 0.506 | 0.052 | 0.040 | 77401.120 | 77401.120 | 8 | True | optimal | 0.0001 | - |
| borderline:5 | 0.515 | 0.047 | 0.040 | 13983.190 | 13983.190 | 11 | True | time limit | 0.3001 | - |
| borderline:6 | 0.501 | 0.049 | 0.040 | 18596.470 | 18596.470 | 13 | True | optimal | 0.0000 | - |
| borderline:7 | 0.517 | 0.049 | 0.040 | 122928.670 | 122928.670 | 6 | True | optimal | 0.0000 | - |
| borderline:8 | 0.522 | 0.043 | 0.040 | 108975.930 | 108975.930 | 4 | True | optimal | 0.0000 | - |
| borderline:9 | 0.510 | 0.047 | 0.040 | 75222.920 | 75222.920 | 15 | True | optimal | 0.0000 | - |
| distressed:0 | 0.505 | 0.043 | 0.040 | 109681.610 | 109681.610 | 8 | True | time limit | 0.8737 | - |
| distressed:1 | 0.491 | 0.046 | 0.040 | 96519.050 | 96519.050 | 10 | True | optimal | 0.0000 | - |
| distressed:2 | 0.482 | 0.040 | 0.040 | 127948.850 | 127948.850 | 10 | True | optimal | 0.0000 | - |
| distressed:3 | 0.500 | 0.054 | 0.040 | 81037.690 | 81037.690 | 7 | True | optimal | 0.0000 | - |
| distressed:4 | 0.490 | 0.040 | 0.040 | 108444.260 | 108444.260 | 9 | True | optimal | 0.0000 | - |
| distressed:5 | 0.486 | 0.043 | 0.032 | 92921.840 | 92921.840 | 14 | True | time limit | 0.8578 | - |
| distressed:6 | 0.512 | 0.047 | 0.040 | 106045.790 | 106045.790 | 8 | True | optimal | 0.0000 | - |
| distressed:7 | 0.501 | 0.049 | 0.040 | 164403.860 | 164403.860 | 6 | True | optimal | 0.0000 | - |
| distressed:8 | 0.499 | 0.048 | 0.040 | 146967.120 | 146967.120 | 3 | True | optimal | 0.0000 | - |
| distressed:9 | 0.486 | 0.057 | 0.040 | 100575.100 | 100575.100 | 7 | True | optimal | 0.0001 | - |

Losses: 0/30.
