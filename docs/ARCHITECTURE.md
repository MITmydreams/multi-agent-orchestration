# Architecture

## Control loop (~60s)

1. **Scout** — find candidate communities (search, seed mining, related channels, optional web directory).
2. **Evaluate** — score and persist candidates; filter non-writable targets.
3. **Scheduler** — assign join vs message tasks under per-account quotas.
4. **Executor** — run actions with stage-aware behavior (observe → engage → outreach).
5. **Content** — LLM or templates; similarity / anti-spam checks.
6. **Risk** — rate limits, FloodWait, freeze recovery, global circuit breaker.
7. **Event** — optional signal-driven tasks from product webhooks.

## Harness-style controls

| Concern | Mechanism |
|---------|-----------|
| Action constraints | Daily caps on messages, joins, DMs, links |
| Policy | Outreach ratio limits, age-tier policies, hibernate/abandon |
| State | Account / membership / task rows in PostgreSQL |
| Audit | MessageLog, AgentTask, structured logs |
| Recovery | FloodWait timers, retries, circuit breaker, 7×24 loop |

## Persistence

- PostgreSQL — durable state  
- Redis — hot coordination  
- Local session files — never commit  
