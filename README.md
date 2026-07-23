# Multi-Agent Orchestration

A **multi-account messaging orchestration** system: specialist agents discover targets, a central scheduler assigns work, executors carry out actions under risk gates, and a dashboard tracks reliability KPIs.

Built during an Agent Development Engineer internship. This public mirror is **domain-generalized and redacted** — it demonstrates orchestration / harness engineering, not a specific commercial product.

**Author:** Wang Jue ([MITmydreams](https://github.com/MITmydreams)) · USTC CS

## What it is (portfolio framing)

Think of it as an **ops control plane for many messaging identities**:

| Agent | Responsibility |
|-------|----------------|
| **Scout** | Discover candidate communities / channels |
| **Scheduler (CentralBrain)** | Periodic loop: prioritize tasks, respect quotas |
| **Executor** | Perform joins / sends with stage-aware behavior |
| **Content** | LLM + template generation, similarity checks |
| **Risk / Harness** | Rate limits, FloodWait, freeze recovery, circuit breaker |
| **Event** | React to external signals / webhooks |

```
Scout → Scheduler → Executor → Content
              ↓
         Risk gates + state (PostgreSQL / Redis)
              ↓
           Dashboard
```

## Stack

Python 3.11 · asyncio · Telethon · PostgreSQL · Redis · SQLAlchemy · Docker · structlog

## Setup

```bash
cp .env.example .env   # replace every xxxx locally
cp config/proxies.example.json config/proxies.json

docker compose up -d
pip install -e .
python -m src.main
```

Session files, real proxies, and credentials stay **local** (gitignored).

## Security

All secret-shaped fields in examples are `xxxx`.  
Do not commit `.env`, `*.session`, or live proxy lists.

## Resume alignment

Multi-agent orchestration, long-horizon workflow + recovery, session/account state, pre-execution policy gates, monitoring KPIs, Telethon client layer.
