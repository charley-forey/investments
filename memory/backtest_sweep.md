# backtest sweep — 2026-07-29T11:43+00:00

| strategy | verdict | R vs buy-and-hold | symbols beating it | R/trade | trades |
|---|---|---|---|---|---|
| sma-crossover | fail | -0.7 | 37/87 | +0.536 | 3632 |
| extended-from-sma | fail | -1.7 | 33/88 | +0.271 | 9088 |
| momentum-continuation | fail | -1.8 | 33/88 | +0.265 | 8614 |
| breakout | fail | -2.0 | 33/88 | +0.389 | 10084 |
| trend-pullback-long | fail | -2.5 | 29/88 | +0.402 | 8794 |


PASS = positive mean out-of-sample R across walk-forward folds and positive on >=60% of symbols. A validated strategy trades at full size; an unproven one at 25%.

## Alpha per trade by market regime (R vs an exposure-matched passive hold; trade count in parens)

| strategy | down/calm | down/elevated | down/normal | sideways/calm | sideways/elevated | sideways/normal | up/calm | up/elevated | up/normal |
|---|---|---|---|---|---|---|---|---|---|
| sma-crossover | +0.17 (4) | +0.43 (591) | -1.78 (387) | -0.32 (507) | -2.29 (143) | +0.10 (554) | -0.38 (962) | +1.45 (69) | +0.87 (373) |
| extended-from-sma | -0.29 (6) | +0.12 (1214) | -0.47 (660) | +0.07 (989) | -1.05 (353) | +0.15 (1150) | -0.12 (2998) | +0.44 (338) | +0.21 (1280) |
| momentum-continuation | -0.20 (15) | -0.05 (972) | -0.48 (559) | +0.04 (1039) | -1.07 (232) | +0.02 (1174) | -0.13 (3025) | +0.54 (287) | +0.22 (1212) |
| breakout | +0.57 (16) | +0.27 (1320) | -0.98 (713) | -0.19 (1249) | -2.15 (442) | +0.28 (1401) | -0.23 (3113) | +0.54 (320) | +0.21 (1398) |
| trend-pullback-long | +0.63 (9) | +0.39 (871) | -0.85 (584) | -0.19 (1197) | -1.85 (399) | +0.36 (1117) | -0.27 (2978) | +0.18 (335) | +0.22 (1304) |

Live position sizing is scaled by these: a strategy is cut toward 25% in regimes where it measured negative, full size where positive. The regime is read off SPY with the same classifier the live path uses, so history and today cannot disagree.
