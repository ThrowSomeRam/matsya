# Decision engine: sensitivity to MSMED claim settings

Default world (past seed 0), Rs 20 lakh cushion, small company tenant, overdraft limit Rs 10 lakh, S1 micro 50% and S2 not MSME 30% on 30-day written terms; eps 0.05 at day 90; 1,000 solve and 10,000 fresh paths, same seeds in every run. One world: illustrative, not a population result.

| run | actions | claims | net cost Rs | vs default Rs | clamped Rs | claims net Rs | p_short fresh | plan same as default |
|---|---|---|---|---|---|---|---|---|
| default | 15 | 0 | 28333.63 | 0.00 | 28333.63 | 0.00 | 0.0561 | True |
| claims off | 15 | 0 | 28333.63 | 0.00 | 28333.63 | 0.00 | 0.0569 | False |
| recovery 0.25 | 13 | 0 | 28977.17 | 643.54 | 28977.17 | 0.00 | 0.0546 | False |
| recovery 0.50 | 15 | 0 | 28333.63 | 0.00 | 28333.63 | 0.00 | 0.0561 | True |
| recovery 0.75 | 21 | 6 | 9042.79 | -19290.84 | 28333.63 | -19290.84 | 0.0560 | False |
| penalty 0.05 | 28 | 13 | -25883.16 | -54216.79 | 28333.63 | -54216.79 | 0.0568 | False |
| penalty 0.10 | 15 | 0 | 28333.63 | 0.00 | 28333.63 | 0.00 | 0.0561 | True |
| penalty 0.20 | 15 | 0 | 28333.63 | 0.00 | 28333.63 | 0.00 | 0.0561 | True |

## Plans

- **default**: overdraft day1 x7; supplier_stretch S2:+30d; chase d1fa72363ad6; treds_discount ceda0ddd8f0b; overdraft day30 x1; chase 79e473327638; treds_discount 1e4041eb9158; chase 2f0a6aded60e; treds_discount b73f5691f54b; treds_discount 4e8b755296da; chase 834754774c49; treds_discount 20695b2316b5; chase 7d268006362a; chase 6ab6dfcb0ac5; chase ae0eec12836e
- **claims off**: overdraft day1 x7; supplier_stretch S2:+30d; chase d1fa72363ad6; treds_discount ceda0ddd8f0b; overdraft day30 x1; chase 79e473327638; treds_discount 1e4041eb9158; chase 2f0a6aded60e; treds_discount b73f5691f54b; chase 834754774c49; treds_discount 4e8b755296da; treds_discount 20695b2316b5; chase 7d268006362a; chase ae0eec12836e; chase 28590e8f4879
- **recovery 0.25**: overdraft day1 x8; supplier_stretch S2:+30d; chase d1fa72363ad6; treds_discount ceda0ddd8f0b; treds_discount 1e4041eb9158; chase 79e473327638; treds_discount b73f5691f54b; treds_discount 4e8b755296da; chase 2f0a6aded60e; chase 834754774c49; chase 6ab6dfcb0ac5; chase 7d268006362a; chase ae0eec12836e
- **recovery 0.50**: overdraft day1 x7; supplier_stretch S2:+30d; chase d1fa72363ad6; treds_discount ceda0ddd8f0b; overdraft day30 x1; chase 79e473327638; treds_discount 1e4041eb9158; chase 2f0a6aded60e; treds_discount b73f5691f54b; treds_discount 4e8b755296da; chase 834754774c49; treds_discount 20695b2316b5; chase 7d268006362a; chase 6ab6dfcb0ac5; chase ae0eec12836e
- **recovery 0.75**: overdraft day1 x7; supplier_stretch S2:+30d; chase d1fa72363ad6; treds_discount ceda0ddd8f0b; overdraft day30 x1; chase 79e473327638; treds_discount 1e4041eb9158; chase 2f0a6aded60e; treds_discount b73f5691f54b; treds_discount 4e8b755296da; chase 834754774c49; treds_discount 20695b2316b5; chase 7d268006362a; chase 6ab6dfcb0ac5; chase ae0eec12836e; msmed_interest ce200ff41264; msmed_interest 7b7823ee4281; msmed_interest 592dbd44bd24; msmed_interest a0df87750004; msmed_interest 452cc8ba5466; msmed_interest ce9ed3b5b8e6
- **penalty 0.05**: overdraft day1 x7; supplier_stretch S2:+30d; chase d1fa72363ad6; treds_discount ceda0ddd8f0b; overdraft day30 x1; chase 79e473327638; treds_discount 1e4041eb9158; chase 2f0a6aded60e; treds_discount b73f5691f54b; chase 834754774c49; treds_discount 4e8b755296da; treds_discount 20695b2316b5; chase 7d268006362a; chase ae0eec12836e; chase 28590e8f4879; msmed_interest ce200ff41264; msmed_interest 7b7823ee4281; msmed_interest 57bd5cf5cd2e; msmed_interest d4eaeb7ada20; msmed_interest ce9ed3b5b8e6; msmed_interest 592dbd44bd24; msmed_interest 666dbeafa13c; msmed_interest e9b891b39a17; msmed_interest a0df87750004; msmed_interest ed7e9878e46a; msmed_interest 452cc8ba5466; msmed_interest 7fa2a33a2aef; msmed_interest 4f6b43f8e478
- **penalty 0.10**: overdraft day1 x7; supplier_stretch S2:+30d; chase d1fa72363ad6; treds_discount ceda0ddd8f0b; overdraft day30 x1; chase 79e473327638; treds_discount 1e4041eb9158; chase 2f0a6aded60e; treds_discount b73f5691f54b; treds_discount 4e8b755296da; chase 834754774c49; treds_discount 20695b2316b5; chase 7d268006362a; chase 6ab6dfcb0ac5; chase ae0eec12836e
- **penalty 0.20**: overdraft day1 x7; supplier_stretch S2:+30d; chase d1fa72363ad6; treds_discount ceda0ddd8f0b; overdraft day30 x1; chase 79e473327638; treds_discount 1e4041eb9158; chase 2f0a6aded60e; treds_discount b73f5691f54b; treds_discount 4e8b755296da; chase 834754774c49; treds_discount 20695b2316b5; chase 7d268006362a; chase 6ab6dfcb0ac5; chase ae0eec12836e
