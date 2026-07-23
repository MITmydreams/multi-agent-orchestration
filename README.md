# Multi-Agent Orchestration (Telegram Ops)

Production-style **multi-agent orchestration** system I built/operated as an Agent Development Engineer internship project.

Specialist agents are coordinated by a central scheduler, with persisted workflow state, pre-execution risk gates, failure recovery, and an ops dashboard — aligned with **Agent Workflow / Agent Harness** concerns (constraints, policy, state, audit, recovery).

> **Portfolio note:** Domain was Telegram growth operations for a product (“The Button”). This public mirror is **redacted**: no sessions, phone numbers, proxies, API keys, group lists, or onboarding runbooks with credentials. Code focuses on orchestration architecture.

**Author:** Wang Jue ([MITmydreams](https://github.com/MITmydreams)) · USTC CS

## Agent topology

```
Scout → Scheduler (CentralBrain) → Infiltrator/Executor → Content → Risk
         │                              │
         ├─ PostgreSQL workflow/state   ├─ FloodWait / rate limits
         ├─ Redis hot session           └─ Circuit breaker
         └─ Dashboard KPIs
```

| Agent / module | Path | Role |
|----------------|------|------|
| Scout | `src/agents/scout/` | Discover candidate groups (search / mining / web) |
| Scheduler | `src/brain/scheduler.py` | Periodic orchestration loop, task assignment |
| Infiltrator | `src/agents/infiltrator/` | Join / message execution under policy |
| Content | `src/agents/content/`, `src/ai/` | AI + template generation, similarity checks |
| Risk / Harness | `src/brain/risk_engine.py`, `circuit_breaker.py`, `age_policy.py`, `src/ai/anti_spam.py` | Rate limits, Freeze/FloodWait, global trip |
| Clients | `src/tg_clients/` | Telethon user clients, proxy pool abstraction |
| Models | `src/models/` | Account / group / task / message persistence |
| Dashboard | `dashboard/` | Health, progress, KPI monitoring |

## Stack

Python 3.11 · asyncio · Telethon · PostgreSQL · Redis · SQLAlchemy · Docker · structlog

## Setup (local)

```bash
cp .env.example .env          # fill keys locally — never commit .env
cp config/proxies.example.json config/proxies.json

# DB / Redis via docker-compose
docker compose up -d

pip install -e .
# point session files locally under tdlib_sessions/ (gitignored)
python -m src.main
```

## Security / redaction

**Not included in this repo:**

- Telegram `.session` / tdata  
- Real phone numbers, 2FA, account dumps  
- Live proxy credentials  
- Production `.env`  
- Discovered group CSVs / operational logs  
- Credential-bearing runbooks  

If you have an older private copy of this tree, **rotate** Telegram API credentials, AI keys, and proxy passwords.

## Resume alignment

Matches the internship bullets: multi-agent orchestration, long-horizon workflow + recovery, session/account state layer, pre-execution policy gates, monitoring KPIs, Telethon communication layer.
