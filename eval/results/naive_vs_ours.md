# Naive vs ours against the truth

30 worlds (healthy, borderline, distressed x past seeds), threshold 0. Oracle: 100 true futures per world; engine 10,000 paths. Errors are means over worlds of |forecast - truth|; paired bootstrap over worlds.

| metric | naive | ours | ours - naive (95% CI) | ours better in |
|---|---|---|---|---|
| median balance error, day 30 (Rs lakh) | 84.71 | 3.77 | -80.95 (-91.70 to -69.27) | 30/30 |
| median balance error, day 60 (Rs lakh) | 44.36 | 6.73 | -37.63 (-45.36 to -30.02) | 30/30 |
| median balance error, day 90 (Rs lakh) | 24.80 | 10.30 | -14.50 (-22.07 to -8.13) | 25/30 |
| p_short(90) error (absolute) | 0.267 | 0.089 | -0.178 (-0.261 to -0.104) | 26/30 |
| flags the shortfall (bar 0.5): right / caught / false alarms | 25/30 / 10/14 / 1 | 27/30 / 11/14 / 0 |  | truth has one in 14/30 |
| flags the shortfall (bar 0.2): right / caught / false alarms | 21/30 / 11/20 / 0 | 26/30 / 16/20 / 0 |  | truth has one in 20/30 |

Flags: the chance of running short by day 90 is at or above the bar. right = agrees with the truth; caught = flagged when the truth has a shortfall; false alarms = flagged when it does not.

## Demo world (borderline, past seed 0, cushion Rs 10 lakh)

| model | p_short(90) | median day 30, Rs lakh | median day 60, Rs lakh | median day 90, Rs lakh |
|---|---|---|---|---|
| truth | 0.460 | 34.46 | 45.46 | 45.01 |
| naive | 0.000 | 128.53 | 91.78 | 36.31 |
| ours | 0.327 | 37.62 | 47.97 | 52.23 |

Bills naive counts as certain cash that ours marks as likely dead (our chance of never being paid >= 0.5): 20, Rs 40.69 lakh in total (ANAND VIJAY CASTINGS PVT LTD, JAI DURGA FORGINGS PVT LTD, OM KRISHNA GARMENTS PVT LTD, SHREE SHAKTI WEAVERS PVT LTD).

| buyer | amount (Rs) | age (days) | days overdue | our chance never paid | never paid in truth |
|---|---|---|---|---|---|
| ANAND VIJAY CASTINGS PVT LTD | 1,053,295 | 335 | 320 | 1.000 | True |
| OM KRISHNA GARMENTS PVT LTD | 327,741 | 276 | 261 | 0.990 | True |
| SHREE SHAKTI WEAVERS PVT LTD | 273,300 | 315 | 270 | 1.000 | True |
| OM KRISHNA GARMENTS PVT LTD | 248,181 | 352 | 322 | 0.998 | True |
| JAI DURGA FORGINGS PVT LTD | 226,120 | 230 | 185 | 0.894 | True |
| JAI DURGA FORGINGS PVT LTD | 223,796 | 173 | 158 | 0.814 | True |
| OM KRISHNA GARMENTS PVT LTD | 212,889 | 193 | 178 | 0.930 | True |
| SHREE SHAKTI WEAVERS PVT LTD | 211,292 | 251 | 221 | 1.000 | True |
| JAI DURGA FORGINGS PVT LTD | 195,480 | 143 | 128 | 0.708 | True |
| JAI DURGA FORGINGS PVT LTD | 166,427 | 295 | 250 | 0.968 | True |
| OM KRISHNA GARMENTS PVT LTD | 159,179 | 328 | 283 | 0.995 | True |
| OM KRISHNA GARMENTS PVT LTD | 151,043 | 287 | 257 | 0.990 | True |
| OM KRISHNA GARMENTS PVT LTD | 132,088 | 180 | 150 | 0.881 | False |
| OM KRISHNA GARMENTS PVT LTD | 121,714 | 331 | 301 | 0.997 | True |
| JAI DURGA FORGINGS PVT LTD | 90,222 | 214 | 184 | 0.891 | True |
| ANAND VIJAY CASTINGS PVT LTD | 85,666 | 217 | 202 | 0.998 | True |
| OM KRISHNA GARMENTS PVT LTD | 84,778 | 170 | 125 | 0.830 | True |
| ANAND VIJAY CASTINGS PVT LTD | 53,087 | 353 | 323 | 1.000 | True |
| JAI DURGA FORGINGS PVT LTD | 26,623 | 339 | 324 | 0.992 | True |
| SHREE SHAKTI WEAVERS PVT LTD | 26,409 | 236 | 221 | 1.000 | True |

## Per world

| world | truth p | naive p | ours p | median d30 truth / naive / ours (Rs lakh) | median d60 truth / naive / ours (Rs lakh) | median d90 truth / naive / ours (Rs lakh) |
|---|---|---|---|---|---|---|
| healthy:0 | 0.020 | 0.000 | 0.002 | 43.75 / 139.22 / 47.70 | 65.66 / 115.67 / 68.27 | 79.60 / 73.38 / 83.98 |
| healthy:1 | 0.020 | 0.000 | 0.014 | 43.54 / 96.25 / 44.07 | 55.58 / 75.43 / 64.08 | 69.76 / 35.77 / 80.64 |
| healthy:2 | 0.020 | 0.000 | 0.019 | 44.28 / 151.61 / 43.24 | 63.41 / 117.51 / 59.61 | 77.70 / 74.88 / 74.45 |
| healthy:3 | 0.200 | 1.000 | 0.194 | 28.44 / 49.85 / 23.93 | 40.27 / 19.12 / 29.97 | 56.62 / -19.90 / 39.47 |
| healthy:4 | 0.140 | 0.000 | 0.055 | 32.65 / 74.59 / 35.23 | 41.27 / 52.91 / 45.95 | 49.45 / 13.20 / 60.87 |
| healthy:5 | 0.000 | 0.000 | 0.002 | 55.42 / 161.07 / 53.33 | 87.80 / 135.01 / 73.90 | 119.37 / 96.37 / 90.84 |
| healthy:6 | 0.140 | 0.000 | 0.008 | 37.61 / 138.97 / 50.06 | 60.93 / 110.81 / 65.00 | 78.81 / 72.14 / 76.17 |
| healthy:7 | 0.120 | 0.000 | 0.055 | 38.68 / 160.54 / 44.92 | 39.08 / 119.00 / 48.67 | 49.31 / 77.48 / 61.31 |
| healthy:8 | 0.030 | 0.000 | 0.071 | 42.85 / 128.12 / 36.76 | 45.12 / 95.48 / 44.17 | 58.51 / 54.92 / 55.63 |
| healthy:9 | 0.380 | 0.000 | 0.162 | 27.96 / 133.47 / 28.98 | 24.67 / 97.16 / 32.29 | 28.85 / 58.84 / 39.77 |
| borderline:0 | 0.190 | 0.000 | 0.098 | 34.46 / 128.53 / 37.62 | 45.46 / 91.78 / 47.97 | 45.01 / 36.31 / 52.23 |
| borderline:1 | 0.260 | 0.161 | 0.186 | 33.66 / 86.52 / 34.97 | 34.86 / 53.89 / 44.48 | 34.78 / 2.41 / 51.10 |
| borderline:2 | 0.270 | 0.000 | 0.294 | 32.89 / 140.08 / 31.24 | 39.98 / 92.50 / 36.17 | 44.82 / 36.45 / 39.49 |
| borderline:3 | 0.760 | 1.000 | 0.796 | 16.66 / 40.51 / 15.28 | 15.01 / -1.41 / 11.97 | 22.29 / -51.71 / 11.86 |
| borderline:4 | 0.600 | 1.000 | 0.433 | 22.55 / 64.57 / 25.38 | 19.59 / 31.36 / 26.53 | 21.65 / -19.87 / 32.02 |
| borderline:5 | 0.110 | 0.000 | 0.053 | 44.09 / 151.88 / 44.29 | 67.37 / 114.28 / 55.24 | 84.69 / 64.12 / 62.78 |
| borderline:6 | 0.430 | 0.000 | 0.118 | 26.52 / 129.46 / 40.48 | 36.72 / 90.36 / 46.22 | 45.78 / 40.79 / 47.37 |
| borderline:7 | 0.660 | 0.000 | 0.411 | 25.12 / 148.81 / 33.17 | 16.51 / 94.31 / 26.08 | 10.95 / 39.76 / 27.89 |
| borderline:8 | 0.530 | 0.000 | 0.473 | 29.45 / 117.72 / 26.90 | 23.01 / 73.16 / 24.77 | 23.81 / 20.69 / 26.43 |
| borderline:9 | 0.890 | 0.000 | 0.717 | 19.23 / 123.14 / 18.84 | 4.66 / 75.61 / 12.57 | 1.18 / 26.00 / 10.57 |
| distressed:0 | 0.630 | 0.990 | 0.605 | 24.23 / 114.48 / 25.32 | 23.87 / 64.08 / 23.97 | 9.63 / -5.14 / 16.30 |
| distressed:1 | 0.820 | 1.000 | 0.626 | 21.83 / 76.86 / 24.22 | 15.14 / 30.52 / 23.22 | -1.15 / -33.39 / 18.60 |
| distressed:2 | 0.780 | 0.714 | 0.771 | 19.52 / 130.40 / 20.68 | 15.62 / 68.73 / 13.42 | 8.82 / -1.49 / 5.30 |
| distressed:3 | 0.990 | 1.000 | 0.986 | 4.47 / 30.62 / 5.80 | -11.23 / -23.13 / -7.15 | -13.93 / -85.25 / -16.67 |
| distressed:4 | 0.930 | 1.000 | 0.873 | 12.29 / 53.02 / 14.72 | -2.56 / 7.68 / 5.10 | -7.98 / -55.77 / 0.12 |
| distressed:5 | 0.390 | 0.000 | 0.344 | 33.48 / 142.34 / 34.96 | 47.72 / 92.68 / 35.26 | 48.03 / 30.48 / 33.24 |
| distressed:6 | 0.890 | 0.001 | 0.521 | 14.37 / 121.08 / 30.69 | 12.80 / 70.48 / 27.22 | 5.20 / 9.35 / 19.19 |
| distressed:7 | 0.980 | 0.530 | 0.855 | 11.59 / 136.41 / 20.53 | -7.98 / 68.14 / 1.79 | -24.65 / -0.17 / -7.87 |
| distressed:8 | 0.890 | 1.000 | 0.898 | 16.21 / 106.80 / 15.56 | -2.44 / 49.68 / 2.04 | -12.06 / -15.37 / -7.36 |
| distressed:9 | 1.000 | 1.000 | 0.973 | 9.92 / 112.24 / 8.62 | -14.21 / 52.80 / -8.41 | -32.42 / -8.70 / -20.69 |

