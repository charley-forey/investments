# Agentic AI Trading System

Multi-agent trading system: Claude-powered research/strategy/risk agents propose trades;
a deterministic Python guardrail engine validates, sizes, and executes them through Alpaca.

**Guiding principle:** the LLM proposes, deterministic code disposes. Agents never hold
broker keys or place orders directly.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env   # then fill in Alpaca paper keys + Anthropic key
```

## Usage

```powershell
.venv\Scripts\trading account          # account snapshot (equity, positions, PDT status)
.venv\Scripts\trading quote AAPL       # latest quote
.venv\Scripts\trading propose --symbol AAPL --side buy --qty 5 --limit 180  # manual order through guardrails
.venv\Scripts\trading pending          # list orders awaiting live approval
.venv\Scripts\trading approve <id>     # approve a pending live order
.venv\Scripts\trading status           # kill switch / trade budget / journal summary
```

Run tests: `.venv\Scripts\python -m pytest`

## Layout

- `config/limits.yaml` — every hard limit (sizing, loss kill switch, PDT, wash-sale, options rules, cost hurdle)
- `config/settings.yaml` — universe, schedule, tax rates, paths
- `src/trading/guardrails/` — the only path to the broker
- `src/trading/broker/` — alpaca-py wrappers + account snapshot
- `src/trading/data/` — SQLite journal (audit trail of every proposal/verdict/order/fill)
- `src/trading/agents/`, `src/trading/tools/` — Claude agents + their tools (milestone 2)
- `memory/`, `playbooks/` — agent memory files and versioned strategy playbooks
