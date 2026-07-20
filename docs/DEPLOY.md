# Deployment — Running Unattended (Milestone 5)

The system is a single long-running daemon (`trading daemon`) plus an optional
real-time fill stream (`trading stream`). It survives individual cycle failures
on its own (each scheduled job is failure-isolated), self-monitors via a
watchdog, and backs up its journal nightly.

## Prerequisites

- `.env` filled in (Alpaca keys, `ANTHROPIC_API_KEY`, optional `DISCORD_WEBHOOK_URL`).
- `pip install -e .` in the target environment.
- Confirm it runs interactively first: `trading status`, then `trading daemon`.

## Phase A — Windows service (this PC) via NSSM

[NSSM](https://nssm.cc/) runs any executable as an auto-restarting Windows service.

```powershell
# 1. Install NSSM (e.g. via choco) then:
nssm install TradingDaemon "C:\Users\charl\desktop\trading\.venv\Scripts\trading.exe" daemon
nssm set TradingDaemon AppDirectory "C:\Users\charl\desktop\trading"
nssm set TradingDaemon AppStdout "C:\Users\charl\desktop\trading\data\daemon.log"
nssm set TradingDaemon AppStderr "C:\Users\charl\desktop\trading\data\daemon.log"
nssm set TradingDaemon AppExit Default Restart      # restart on any exit
nssm set TradingDaemon AppRestartDelay 10000        # 10s before restart
nssm start TradingDaemon
```

Manage: `nssm status TradingDaemon`, `nssm restart TradingDaemon`, `nssm stop TradingDaemon`.

For the real-time fill stream, install a second service the same way with the
`stream` argument (`nssm install TradingStream ...trading.exe stream`).

### Kill-switch drill (recommended before trusting it)
```powershell
Stop-Process -Name trading -Force   # simulate a crash
# NSSM restarts it within AppRestartDelay; confirm with `trading status`
```

## Phase B — Linux VPS via systemd (true 24/7)

Copy the repo to the VPS, create a venv, `pip install -e .`, and add
`/etc/systemd/system/trading.service`:

```ini
[Unit]
Description=Agentic trading daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/trading
EnvironmentFile=/opt/trading/.env
ExecStart=/opt/trading/.venv/bin/trading daemon
Restart=always
RestartSec=10
StandardOutput=append:/var/log/trading.log
StandardError=append:/var/log/trading.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trading
sudo systemctl status trading
journalctl -u trading -f      # live logs
```

A second unit (`trading-stream.service` with `ExecStart=... trading stream`)
runs the real-time fill websocket.

## What runs automatically once the daemon is up

| Job | Schedule | Purpose |
|---|---|---|
| calendar | 15 min before premarket | refresh `data/calendar.json` earnings dates |
| premarket | 08:30 ET Mon–Fri | research + watchlist |
| intraday | every 15 min, market hours | propose → risk → execute |
| postclose | 16:30 ET Mon–Fri | scoring + lessons + counterfactuals + EOD note |
| weekend | Sat 10:00 | lifecycle + allocation + calibration + signal research + playbook auto-apply |
| watchdog | every 30 min | alerts if heartbeats go stale |
| daily_summary | 16:45 ET Mon–Fri | "alive + what I did" + 24h cost to Discord |
| backup | 23:30 daily | rotated journal DB snapshot (keeps 14) |

## Paper-proof checklist (M18 lite) — do this before trusting unattended paper

```powershell
trading paper-proof          # go/no-go: mode, sizing, calendar, schema, Discord
trading calendar             # one-shot earnings calendar refresh
trading preflight            # broker + Anthropic + DB
trading daemon               # leave running; NSSM optional (see above)
# optional: trading stream   # real-time fills
```

After the first submitted trade fills:

1. `trading sync` — confirm fill + tax lot with `proposal_id`
2. Next `postclose` — `scores` row linked to that proposal; lessons in `memory/lessons.md`
3. Discord webhook should ping on the fill (if `DISCORD_WEBHOOK_URL` is set)
4. After 5+ days, aged vetoes get `proposal_outcomes` grades (counterfactuals)

Exit criteria for "paper has a pulse": ≥1 filled trade with linked score, and strategy
no longer repeating oversized notionals (sizing precheck + recent-failure context).

## Monitoring

- `trading status` — mode, kill switch, budgets, health, last successful cycle, 24h cost.
- `trading watchdog` — one-shot health check (exit code 1 if unhealthy); alerts via Discord.
- `trading backup` — manual journal backup.
- Journal backups: `data/backups/journal-<timestamp>.db`.

## Notes / hardening for live

- Run under a dedicated low-privilege user; keep `.env` readable only by that user.
- Keep `mode: paper` in `config/limits.yaml` until the strategy lifecycle has
  promoted tags to `small-live`; live orders for uncleared strategies are rejected
  by the `strategy_stage` guardrail regardless.
- The inbound "approve from your phone" bot (turning a Discord reply into
  `trading approve <id>`) is not yet built — approval is via the CLI today, with
  an outbound Discord ping telling you what's pending. Adding it needs a Discord
  bot token; see ROADMAP.
