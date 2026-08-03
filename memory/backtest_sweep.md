# backtest sweep — 2026-08-03T21:35+00:00

| strategy | verdict | R vs buy-and-hold | symbols beating it | R/trade | trades |
|---|---|---|---|---|---|
| sma-crossover | fail | -0.5 | 35/87 | +0.555 | 3637 |
| mean-reversion | fail | -1.5 | 28/88 | +0.073 | 8061 |
| extended-from-sma | fail | -2.0 | 30/88 | +0.262 | 9115 |
| breakout | fail | -2.5 | 30/88 | +0.377 | 10101 |
| trend-pullback-long | fail | -2.6 | 30/88 | +0.409 | 8794 |
| trend-pullback-short | **PASS** | -5.7 | 19/87 | -0.039 | 7777 |
| momentum-continuation | fail | -6.3 | 23/88 | +0.128 | 14895 |
| breakdown | fail | -9.7 | 13/88 | -0.070 | 10108 |


PASS = positive mean out-of-sample R across walk-forward folds and positive on >=60% of symbols. A validated strategy trades at full size; an unproven one at 25%.

## Alpha per trade by market regime (R vs an exposure-matched passive hold; trade count in parens)

| strategy | down/calm | down/elevated | down/normal | sideways/calm | sideways/elevated | sideways/normal | up/calm | up/elevated | up/normal |
|---|---|---|---|---|---|---|---|---|---|
| sma-crossover | +0.20 (4) | +0.40 (589) | -1.77 (388) | -0.26 (506) | -2.29 (140) | +0.02 (562) | -0.43 (958) | +1.39 (70) | +1.18 (378) |
| mean-reversion | +1.29 (17) | -0.14 (887) | -0.13 (350) | -0.06 (990) | -0.60 (140) | -0.04 (1075) | -0.07 (2943) | +0.11 (322) | +0.02 (1265) |
| extended-from-sma | -0.28 (6) | +0.11 (1210) | -0.49 (659) | +0.08 (993) | -1.05 (353) | +0.13 (1170) | -0.12 (3007) | +0.44 (338) | +0.19 (1279) |
| breakout | +0.60 (16) | +0.25 (1316) | -1.03 (712) | -0.16 (1251) | -2.16 (442) | +0.22 (1427) | -0.23 (3108) | +0.54 (320) | +0.18 (1397) |
| trend-pullback-long | +0.66 (9) | +0.41 (868) | -0.90 (585) | -0.17 (1194) | -1.85 (397) | +0.32 (1129) | -0.26 (2971) | +0.18 (335) | +0.20 (1306) |
| trend-pullback-short | -0.22 (26) | +0.43 (1146) | -1.30 (418) | -0.31 (998) | -1.34 (223) | -0.32 (1239) | -0.53 (2322) | -0.57 (282) | +0.00 (1140) |
| momentum-continuation | +0.35 (39) | +0.11 (1864) | -0.63 (913) | -0.08 (1729) | -1.02 (446) | -0.14 (2206) | -0.19 (4738) | -0.09 (517) | +0.08 (2271) |
| breakdown | +1.11 (20) | +0.42 (1047) | -1.31 (427) | -0.32 (1246) | -1.16 (211) | -0.37 (1316) | -0.55 (3706) | -0.66 (364) | -0.08 (1672) |

Live position sizing is scaled by these: a strategy is cut toward 25% in regimes where it measured negative, full size where positive. The regime is read off SPY with the same classifier the live path uses, so history and today cannot disagree.
