# Decision engine: CVaR plan vs chance-constrained plan

30 worlds (healthy, borderline, distressed x past seeds 0-9), 1,000 solve paths, 10,000 fresh paths. Owner target p_short(90) <= 0.05; CVaR alpha 0.05, budget Rs 0. Means over worlds.

| plan | cost mean (median), Rs | p_short(90) mean | meets target (p <= 0.05) | within MC error | expected shortfall if short, Rs | expected shortfall over all futures, Rs | worst-5% average shortfall, Rs | solve s median (max) | optimal / time limit |
|---|---|---|---|---|---|---|---|---|---|
| do_nothing | 0 (0) | 0.4980 | 0/30 | 0/30 | 1,119,050 | 558,867 | 3,167,241 |  |  |
| chance | 54,106 (26,209) | 0.0467 | 19/30 | 26/30 | 640,485 | 30,701 | 612,092 | 31.1 (60.1) | 26/4 |
| cvar | 115,801 (121,524) | 0.0075 | 30/30 | 30/30 | 540,676 | 4,811 | 96,224 | 1.4 (4.2) | 30/0 |

Paired cost difference, CVaR minus chance-constrained: mean Rs +61,695 (95% CI +49,676 to +73,543); CVaR dearer in 30/30 worlds.
Owner target met on fresh paths: chance-constrained 19/30, CVaR 30/30. Default plan by the rule set in advance: **cvar**.

## Stress: payment chances x 0.85, order rate x 0.9 (plans not re-solved)

| plan | p_short(90) normal | p_short(90) stressed | change | meets target stressed | worst-5% normal, Rs | worst-5% stressed, Rs | change, Rs |
|---|---|---|---|---|---|---|---|
| do_nothing | 0.4980 | 0.6394 | +0.1414 | 0/30 | 3,167,241 | 3,782,850 | +615,609 |
| chance | 0.0467 | 0.1136 | +0.0670 | 0/30 | 612,092 | 1,353,151 | +741,059 |
| cvar | 0.0075 | 0.0280 | +0.0205 | 27/30 | 96,224 | 378,575 | +282,351 |

