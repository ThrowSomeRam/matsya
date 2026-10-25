# Reduced sales at 90, 180 days

Default generator world, 20 pasts x 2,000 paths, learned inputs at a 180-day horizon. Orders x 0.7. Baseline: same world and seed, no scenario. Means over pasts; ES in Rs lakh (unconditional = p_short x mean depth; conditional = mean depth over short paths, mean over pasts with any short path).

| variant | day | p_short base | p_short | d p_short (sd over pasts) | ES uncond base | ES uncond | ES cond base | ES cond |
|---|---|---|---|---|---|---|---|---|
| preset (stickiness 14d) | 90 | 0.284 | 0.234 | -0.050 (0.038) | 3.72 | 2.58 | 11.16 | 9.39 |
| preset (stickiness 14d) | 180 | 0.398 | 0.412 | +0.014 (0.058) | 6.26 | 6.41 | 13.20 | 12.69 |
| stickiness 0d | 90 | 0.284 | 0.170 | -0.114 (0.063) | 3.72 | 1.64 | 11.16 | 8.25 |
| stickiness 0d | 180 | 0.398 | 0.343 | -0.055 (0.058) | 6.26 | 4.87 | 13.20 | 11.47 |
