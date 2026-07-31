# backtest sweep — 2026-07-30T21:35+00:00

| strategy | verdict | R vs buy-and-hold | symbols beating it | R/trade | trades |
|---|---|---|---|---|---|
| sma-crossover | fail | -0.7 | 36/87 | +0.537 | 3633 |
| mean-reversion | fail | -1.2 | 33/88 | +0.082 | 8051 |
| extended-from-sma | fail | -1.8 | 33/88 | +0.270 | 9096 |
| breakout | fail | -2.2 | 32/88 | +0.386 | 10097 |
| trend-pullback-long | fail | -2.6 | 31/88 | +0.400 | 8795 |
| trend-pullback-short | **PASS** | -5.6 | 19/87 | -0.037 | 7771 |
| momentum-continuation | fail | -6.2 | 22/88 | +0.126 | 14889 |
| breakdown | fail | -9.6 | 13/88 | -0.070 | 10102 |


PASS = positive mean out-of-sample R across walk-forward folds and positive on >=60% of symbols. A validated strategy trades at full size; an unproven one at 25%.

## Alpha per trade by market regime (R vs an exposure-matched passive hold; trade count in parens)

| strategy | down/calm | down/elevated | down/normal | sideways/calm | sideways/elevated | sideways/normal | up/calm | up/elevated | up/normal |
|---|---|---|---|---|---|---|---|---|---|
| sma-crossover | +0.17 (4) | +0.44 (591) | -1.78 (387) | -0.26 (506) | -2.28 (141) | +0.05 (555) | -0.38 (959) | +1.41 (70) | +0.84 (378) |
| mean-reversion | +1.27 (17) | -0.15 (885) | -0.16 (350) | -0.05 (989) | -0.60 (140) | -0.03 (1069) | -0.08 (2937) | +0.11 (321) | +0.11 (1271) |
| extended-from-sma | -0.29 (6) | +0.13 (1211) | -0.47 (659) | +0.07 (994) | -1.05 (352) | +0.13 (1157) | -0.12 (2998) | +0.44 (338) | +0.20 (1281) |
| breakout | +0.57 (16) | +0.28 (1319) | -0.99 (712) | -0.17 (1252) | -2.15 (441) | +0.24 (1411) | -0.24 (3114) | +0.54 (320) | +0.20 (1400) |
| trend-pullback-long | +0.63 (9) | +0.40 (871) | -0.86 (585) | -0.16 (1200) | -1.85 (399) | +0.32 (1119) | -0.27 (2971) | +0.18 (335) | +0.20 (1306) |
| trend-pullback-short | -0.25 (25) | +0.43 (1153) | -1.30 (418) | -0.31 (999) | -1.33 (223) | -0.32 (1228) | -0.53 (2321) | -0.56 (282) | +0.01 (1139) |
| momentum-continuation | +0.33 (38) | +0.10 (1867) | -0.61 (913) | -0.08 (1729) | -1.02 (445) | -0.14 (2193) | -0.19 (4733) | -0.08 (516) | +0.09 (2283) |
| breakdown | +1.04 (20) | +0.41 (1048) | -1.31 (426) | -0.31 (1245) | -1.16 (211) | -0.35 (1311) | -0.55 (3697) | -0.65 (363) | -0.09 (1682) |

Live position sizing is scaled by these: a strategy is cut toward 25% in regimes where it measured negative, full size where positive. The regime is read off SPY with the same classifier the live path uses, so history and today cannot disagree.
